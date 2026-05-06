"""SMD writers for skinned reference meshes and per-clip animation SMDs.

Companion to [smd_export.py](smd_export.py) (which handles single-root static
props). Both writers build on ``srctools.smd.{Mesh, Bone, BoneFrame, Vertex,
Triangle}``. Coordinate conversion lives in one place
(``CoordinateMode``) so the skinned reference SMD and the animation SMDs
share an identical bind pose.

S2V skinned exports:
    Source 2 Viewer bakes a Y-up→Z-up axis-swap rotation into the root joint's
    rest_rotation (typically the quaternion [0.5, 0.5, 0.5, 0.5], a 120°
    rotation around (1,1,1)). For these files we pass joint TRS values
    through verbatim — the math is internally consistent: at the bind pose,
    Source's runtime computes ``inv(bone_world_at_bind) * bone_world_at_frame
    = identity`` and the vertices remain in vmdl-space. Animation frames are
    layered on top of the same baked rotation, so motion plays correctly.

Non-S2V exports (Blender Y-up etc.):
    Bones are typically authored in glTF Y-up convention. We multiply the
    user's ``scale`` into translations and apply a +90° X axis-swap to the
    root joint's rest TRS (and to root-targeted animation channels). This
    is best-effort: complex armatures with intermediate axis-correction
    nodes may still need user post-processing in the QC.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import trimesh
from scipy.spatial.transform import Rotation as R

from srctools.math import Angle, Vec
from srctools.smd import Bone, BoneFrame, Mesh, Triangle, Vertex

from .gltf_animation import GltfClip, GltfSkin
from .smd_export_helpers import pick_material, resolve_face_materials, resolve_uvs

# +90° rotation around X, used to convert glTF Y-up into Source Z-up.
_AXIS_SWAP_QUAT = np.array([np.sin(np.pi / 4), 0.0, 0.0, np.cos(np.pi / 4)], dtype=np.float32)


@dataclass
class CoordinateMode:
    """Controls how joint TRS values get mapped into Source space.

    - ``scale``: multiplier applied to every translation. For S2V skinned
      exports this is 1.0 (already in inches); for Blender Y-up exports it
      is the user's scale factor (default 40.0 ≈ 1m → ~40 inches).
    - ``swap_axes``: whether to apply +90° X axis-swap. False for S2V
      skinned; True for non-S2V when ``axis_conversion`` is on.
    """
    scale: float = 1.0
    swap_axes: bool = False


def _quat_mul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Hamilton product for quaternions stored as [x,y,z,w]."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return np.array([
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    ], dtype=np.float32)


def _quat_to_pyr_degrees(q_xyzw: np.ndarray) -> Tuple[float, float, float]:
    """Convert quaternion to Source's pitch/yaw/roll Euler in degrees.

    Source's ``Angle`` class stores (pitch, yaw, roll) where rotations are
    applied in order: roll around X, pitch around Y, yaw around Z (intrinsic).
    Scipy's ``Rotation.as_euler('YZX')`` does NOT match this directly; we use
    'ZYX' (yaw, pitch, roll) order which matches Source's convention when
    interpreted as (yaw, pitch, roll).
    """
    # scipy expects [x, y, z, w]
    rot = R.from_quat(q_xyzw)
    # Source's Angle.from_matrix uses Z(yaw) Y(pitch) X(roll) intrinsic.
    yaw, pitch, roll = rot.as_euler('ZYX', degrees=True)
    return float(pitch), float(yaw), float(roll)


def _smd_angle_from_pyr(pit: float, yaw: float, rol: float) -> Angle:
    """Construct a ``srctools.Angle`` whose slot order, when dumped by
    ``srctools.smd``, produces the SMD/QC ``(rot_x, rot_y, rol_z)`` =
    ``(roll, pitch, yaw)`` column order that studiomdl expects.

    srctools' ``BoneFrame`` exporter writes ``Angle.pitch``, ``Angle.yaw``,
    ``Angle.roll`` to columns 5/6/7 verbatim. SMD bone rotation columns are
    rotation-around-X, Y, Z respectively — i.e. (roll, pitch, yaw) in
    Source's pitch/yaw/roll naming. Stuffing roll → pitch-slot, pitch →
    yaw-slot, yaw → roll-slot makes the on-disk columns come out as
    (roll, pitch, yaw). Without this fix, studiomdl mis-reads each
    component and bones end up at cycled world transforms even though
    deformation still cancels (visible as bones drawn far from the mesh).
    """
    return Angle(rol, pit, yaw)


def _apply_coord_to_root_trs(t: np.ndarray, q: np.ndarray, coord: CoordinateMode
                              ) -> Tuple[np.ndarray, np.ndarray]:
    """Apply scale + (optional) axis-swap to a ROOT-joint TRS.

    Children of root inherit the swap through the bone hierarchy, so we only
    rotate the root. Translations of the root scale by ``coord.scale``.
    """
    t_out = t * coord.scale
    if coord.swap_axes:
        # Pre-multiply by the axis-swap so the root carries the conversion.
        q_out = _quat_mul(_AXIS_SWAP_QUAT, q)
        # Translation of root is rotated as well so children land in the right
        # parent-local frame after the swap is propagated through the chain.
        t_out = R.from_quat(_AXIS_SWAP_QUAT).apply(t_out)
    else:
        q_out = q
    return t_out.astype(np.float32), q_out.astype(np.float32)


def _apply_coord_to_child_translation(t: np.ndarray, coord: CoordinateMode) -> np.ndarray:
    """Children of root see only the scale; the axis-swap is in their parent."""
    return (t * coord.scale).astype(np.float32)


def _build_bone_tree(skin: GltfSkin) -> Tuple[Dict[str, Bone], List[Bone]]:
    """Construct ``srctools.smd.Bone`` instances mirroring ``skin.joints``.

    Returns ``(bones_by_name, bones_in_skin_order)``. Names are deduplicated
    by appending ``_<idx>`` if a collision occurs (rare — glTF allows duplicate
    node names).
    """
    bones_by_name: Dict[str, Bone] = {}
    bones_in_order: List[Bone] = []

    used_names: set = set()
    final_names: List[str] = []
    for i, j in enumerate(skin.joints):
        name = j.name
        if name in used_names:
            name = f"{j.name}_{i}"
        used_names.add(name)
        final_names.append(name)

    for i, j in enumerate(skin.joints):
        parent_bone = (
            bones_in_order[j.parent_joint_idx]
            if j.parent_joint_idx is not None else None
        )
        bone = Bone(final_names[i], parent_bone)
        bones_in_order.append(bone)
        bones_by_name[final_names[i]] = bone

    return bones_by_name, bones_in_order


def _frame_to_bone_frames(skin: GltfSkin,
                          bones_in_order: List[Bone],
                          frame_data: Dict[int, Tuple[np.ndarray, np.ndarray, np.ndarray]],
                          coord: CoordinateMode) -> List[BoneFrame]:
    """Translate sampled per-joint TRS into ``BoneFrame`` list for one frame."""
    out: List[BoneFrame] = []
    for joint_idx, joint in enumerate(skin.joints):
        t, q, _scale = frame_data[joint_idx]
        if joint.parent_joint_idx is None:
            t_src, q_src = _apply_coord_to_root_trs(t, q, coord)
        else:
            t_src = _apply_coord_to_child_translation(t, coord)
            q_src = q
        pit, yaw, rol = _quat_to_pyr_degrees(np.asarray(q_src, dtype=np.float64))
        out.append(BoneFrame(
            bones_in_order[joint_idx],
            Vec(float(t_src[0]), float(t_src[1]), float(t_src[2])),
            _smd_angle_from_pyr(pit, yaw, rol),
        ))
    return out


def compute_root_bind_pyr(
    skin: GltfSkin, coord: CoordinateMode
) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
    """Return the skin's root joint bind as ``(position_xyz, pyr_degrees)``.

    Identical math to ``derive_definebone_lines`` and ``_frame_to_bone_frames``
    so the static physics SMD's "root" bone can be authored to match the main
    skeleton's root bind. Without this match, studiomdl bakes physics vertices
    at ``inv(identity)`` and the runtime applies the main skeleton's non-zero
    root rotation as an extra transform — visible as a tilted physics mesh.
    """
    root = skin.joints[skin.root_joint_idx]
    t, q = _apply_coord_to_root_trs(root.rest_translation, root.rest_rotation, coord)
    pit, yaw, rol = _quat_to_pyr_degrees(np.asarray(q, dtype=np.float64))
    return (float(t[0]), float(t[1]), float(t[2])), (pit, yaw, rol)


def derive_definebone_lines(skin: GltfSkin, coord: CoordinateMode) -> List[str]:
    """Build $definebone lines from the skin's bind pose.

    Returns a list of lines like
    ``$definebone "name" "parent" X Y Z XR YR ZR 0 0 0 0 0 0``. The bind
    pose is the same data that goes into frame 0 of the animation SMD, so
    studiomdl will not reject the model for skeleton mismatch.
    """
    lines: List[str] = []
    for i, j in enumerate(skin.joints):
        parent_name = '' if j.parent_joint_idx is None else skin.joints[j.parent_joint_idx].name
        if j.parent_joint_idx is None:
            t, q = _apply_coord_to_root_trs(j.rest_translation, j.rest_rotation, coord)
        else:
            t = _apply_coord_to_child_translation(j.rest_translation, coord)
            q = j.rest_rotation
        pit, yaw, rol = _quat_to_pyr_degrees(np.asarray(q, dtype=np.float64))
        # $definebone columns 5/6/7 are (rot_x, rot_y, rot_z) = (roll, pitch, yaw).
        lines.append(
            f'$definebone "{j.name}" "{parent_name}" '
            f'{t[0]:.6f} {t[1]:.6f} {t[2]:.6f} '
            f'{rol:.6f} {pit:.6f} {yaw:.6f} '
            f'0 0 0 0 0 0'
        )
    return lines


# ---------------------------------------------------------------------------
# Animation SMD (skeleton + frames, no triangles)
# ---------------------------------------------------------------------------

class SmdAnimationExporter:
    """Animation-only SMD writer — nodes block + skeleton block, no triangles."""

    @staticmethod
    def write_animation(
        skin: GltfSkin,
        frames: List[Dict[int, Tuple[np.ndarray, np.ndarray, np.ndarray]]],
        out_path: Path,
        coord: CoordinateMode,
    ) -> Tuple[bool, str]:
        if not skin.joints:
            return (False, "Skin has no joints")
        if not frames:
            return (False, "No animation frames to write")

        _, bones_in_order = _build_bone_tree(skin)

        smd = Mesh(
            bones={b.name: b for b in bones_in_order},
            animation={},
            triangles=[],
        )
        for t_idx, frame in enumerate(frames):
            smd.animation[t_idx] = _frame_to_bone_frames(skin, bones_in_order, frame, coord)

        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "wb") as f:
                smd.export(f)
        except Exception as e:
            return (False, f"Animation SMD write failed: {e}")
        return (True, "")


# ---------------------------------------------------------------------------
# Skinned reference SMD
# ---------------------------------------------------------------------------

class SmdSkeletalExporter:
    """Skinned-mesh SMD writer — bone tree + bind pose + triangles with weights."""

    @staticmethod
    def write_skinned(
        mesh: trimesh.Trimesh,
        skin: GltfSkin,
        vertex_joints: np.ndarray,    # (N, 4) uint16
        vertex_weights: np.ndarray,   # (N, 4) float32, rows sum ≈ 1.0
        out_path: Path,
        material_name: str,
        coord: CoordinateMode,
        flip_v: bool = False,
        force_uv_zero: bool = False,
        override_normals: Optional[np.ndarray] = None,
        uv_mode: str = 'preserve',
    ) -> Tuple[bool, str]:
        if mesh is None or not hasattr(mesh, 'faces') or len(mesh.faces) == 0:
            return (False, "Mesh has no faces")
        vertices = mesh.vertices
        faces = mesh.faces
        if len(vertices) == 0:
            return (False, "Mesh has no vertices")

        if vertex_joints is None or vertex_weights is None:
            return (False, "Missing JOINTS_0/WEIGHTS_0 — cannot write skinned SMD")
        if len(vertex_joints) != len(vertices) or len(vertex_weights) != len(vertices):
            return (False,
                    f"Skin attribute length mismatch "
                    f"(verts={len(vertices)} joints={len(vertex_joints)} weights={len(vertex_weights)})")

        # Validate face indices
        max_index = len(vertices) - 1
        valid_mask = np.all((faces >= 0) & (faces <= max_index), axis=1)
        face_index_map = np.nonzero(valid_mask)[0]
        faces = faces[valid_mask]
        if len(faces) == 0:
            return (False, "No valid faces after index validation")

        # Normals
        if override_normals is not None:
            normals = override_normals
        else:
            try:
                normals = mesh.vertex_normals
                if len(normals) != len(vertices):
                    normals = None
            except Exception:
                normals = None

        finite_verts = np.isfinite(vertices).all(axis=1)
        if normals is not None:
            finite_normals = np.isfinite(normals).all(axis=1)
            finite_faces = (np.all(finite_verts[faces], axis=1)
                            & np.all(finite_normals[faces], axis=1))
        else:
            finite_faces = np.all(finite_verts[faces], axis=1)
        faces = faces[finite_faces]
        face_index_map = face_index_map[finite_faces]
        if len(faces) == 0:
            return (False, "No valid faces after filtering")

        uvs = resolve_uvs(mesh, uv_mode, force_uv_zero)
        face_materials, material_names = resolve_face_materials(mesh)

        # Build bones + bind pose frame
        _, bones_in_order = _build_bone_tree(skin)
        bind_frame_data: Dict[int, Tuple[np.ndarray, np.ndarray, np.ndarray]] = {
            i: (j.rest_translation, j.rest_rotation, j.rest_scale)
            for i, j in enumerate(skin.joints)
        }
        bind_frame = _frame_to_bone_frames(skin, bones_in_order, bind_frame_data, coord)

        # Pre-build per-vertex link lists (Bone, weight) filtered to weight > 0.
        n_joints = len(skin.joints)
        vertex_links: List[List[Tuple[Bone, float]]] = []
        for vidx in range(len(vertices)):
            row_j = vertex_joints[vidx]
            row_w = vertex_weights[vidx]
            links: List[Tuple[Bone, float]] = []
            for k in range(4):
                w = float(row_w[k])
                if w <= 0.0:
                    continue
                ji = int(row_j[k])
                if 0 <= ji < n_joints:
                    links.append((bones_in_order[ji], w))
            if not links:
                # Unweighted vertex; fall back to root with weight 1.0.
                links = [(bones_in_order[skin.root_joint_idx], 1.0)]
            else:
                # Renormalise after filtering.
                tot = sum(w for _, w in links)
                if tot > 0:
                    links = [(b, w / tot) for b, w in links]
            vertex_links.append(links)

        smd = Mesh(
            bones={b.name: b for b in bones_in_order},
            animation={0: bind_frame},
            triangles=[],
        )

        default_normal = (0.0, 0.0, 1.0)
        for local_idx, face in enumerate(faces):
            original_face_idx = int(face_index_map[local_idx])
            mat = pick_material(original_face_idx, face_materials, material_names, material_name)

            verts = []
            for vidx in face:
                v = vertices[vidx]
                if normals is not None:
                    n = normals[vidx]
                else:
                    n = default_normal
                if uvs is not None:
                    u = float(uvs[vidx][0])
                    tv = float(uvs[vidx][1])
                else:
                    u = 0.0
                    tv = 0.0
                if flip_v:
                    tv = 1.0 - tv

                verts.append(Vertex(
                    Vec(float(v[0]), float(v[1]), float(v[2])),
                    Vec(float(n[0]), float(n[1]), float(n[2])),
                    u, tv,
                    list(vertex_links[int(vidx)]),
                ))
            smd.triangles.append(Triangle(mat, verts[0], verts[1], verts[2]))

        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "wb") as f:
                smd.export(f)
        except Exception as e:
            return (False, f"Skinned SMD write failed: {e}")
        return (True, "")


# ---------------------------------------------------------------------------
# Helpers used by tools: clip-name → loop heuristic
# ---------------------------------------------------------------------------

_LOOP_PATTERNS = ('idle', 'loop', 'walk', 'run', 'breathe', 'cycle')


def is_loop_clip(name: str) -> bool:
    n = name.lower()
    return any(p in n for p in _LOOP_PATTERNS)


def sanitize_clip_filename(name: str) -> str:
    """Make a clip name safe for use as part of an SMD filename."""
    out_chars: List[str] = []
    for ch in name:
        if ch.isalnum() or ch in '_-':
            out_chars.append(ch)
        else:
            out_chars.append('_')
    s = ''.join(out_chars).strip('_')
    return s or 'clip'
