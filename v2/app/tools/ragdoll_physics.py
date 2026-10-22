"""Ragdoll back-porting core: turn a skinned Source 2 model into a Source 1
jointed collision model (``$collisionjoints``).

Pipeline (Phase 1 — humanoid):
  1. Detect a humanoid skeleton and retarget its bone names to ValveBiped.
  2. Pick the ValveBiped bones to simulate (≤32 ragdoll parts).
  3. Assign every render vertex to its nearest *simulated* bone (fingers,
     twist and helper bones fold into their parent's hull).
  4. Build one convex hull per simulated bone — the theanine3D
     "collision from weights" idea: a vphysics ragdoll part is exactly one
     convex hull weighted 1.0 to a single bone.
  5. Write the ragdoll physics SMD and emit the ``$collisionjoints`` /
     ``$jointconstrain`` QC block.

Qt-free. Hulls are computed from the *already coordinate-converted* render
mesh (same space the skinned reference SMD is written in), so the physics
SMD and reference SMD share one bind pose.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, TYPE_CHECKING

import numpy as np
import trimesh

from ..core.bone_maps import SOURCE2_TO_VALVEBIPED, valvebiped_name
from ..core import ragdoll_presets as presets
from ..utils.phys_kv3 import (PhysData, joint_constraint_rows_v2,
                              parse_phys_dump, shape_points)
from .smd_animation_export import CoordinateMode, SmdRagdollPhysicsExporter

if TYPE_CHECKING:  # avoid a hard import cycle at runtime
    from .gltf_animation import GltfSkin

LogFn = Callable[[str], None]


# ---------------------------------------------------------------------------
# Skeleton detection + retargeting
# ---------------------------------------------------------------------------

def is_humanoid_skeleton(joint_names: List[str], *, threshold: int = 6) -> bool:
    """True when enough of the skin's bones map to ValveBiped humanoid bones.

    Uses the Source 2 names (call BEFORE retargeting). ``threshold`` is the
    minimum number of mapped bones — six comfortably clears pelvis + a couple
    spine/limb bones while rejecting props and non-biped creatures.
    """
    hits = sum(1 for n in joint_names if n in SOURCE2_TO_VALVEBIPED)
    return hits >= threshold


def retarget_skeleton_to_valvebiped(skin: "GltfSkin") -> List[str]:
    """Rename ``skin.joints[*].name`` to ValveBiped in place.

    Unmapped bones (cloth, custom, already-Source-1) keep their name. Returns
    a short log of how many bones were renamed. Mutating the shared skin means
    the reference SMD, animation SMDs and physics SMD all emit the same
    ValveBiped names with no extra Bone Backport pass.
    """
    renamed = 0
    for j in skin.joints:
        mapped = SOURCE2_TO_VALVEBIPED.get(j.name)
        if mapped and mapped != j.name:
            j.name = mapped
            renamed += 1
    return [f"retargeted {renamed} bone(s) to ValveBiped names"]


# ---------------------------------------------------------------------------
# Simulated-bone selection
# ---------------------------------------------------------------------------

def select_sim_bones(skin: "GltfSkin", *, max_parts: int = 32) -> List[Tuple[int, str]]:
    """Return ``[(joint_idx, bone_name)]`` for the humanoid sim bones present
    in the (already-retargeted) skeleton, in preset order with the root first.

    Capped at ``max_parts`` (the Source ragdoll limit is 32). Matching is
    case-insensitive so minor casing differences in the export don't drop a
    bone.
    """
    by_lower = {}
    for idx, j in enumerate(skin.joints):
        by_lower.setdefault(j.name.lower(), idx)  # first occurrence wins

    out: List[Tuple[int, str]] = []
    for name in presets.HUMANOID_SIM_BONES:
        idx = by_lower.get(name.lower())
        if idx is not None:
            out.append((idx, skin.joints[idx].name))
        if len(out) >= max_parts:
            break
    return out


# ---------------------------------------------------------------------------
# Vertex → simulated-bone assignment
# ---------------------------------------------------------------------------

def _nearest_sim_ancestor_map(skin: "GltfSkin", sim_set: set) -> np.ndarray:
    """For every joint, the index of its nearest simulated ancestor (itself if
    it is simulated), or -1 if none exists up to the root.
    """
    n = len(skin.joints)
    out = np.full(n, -1, dtype=np.int64)

    def resolve(ji: int) -> int:
        path: List[int] = []
        cur: Optional[int] = ji
        while cur is not None and cur not in sim_set:
            path.append(cur)
            cur = skin.joints[cur].parent_joint_idx
        result = cur if cur is not None else -1
        for p in path:
            out[p] = result
        out[ji] = ji if ji in sim_set else result
        return int(out[ji])

    for ji in range(n):
        if out[ji] == -1:
            resolve(ji)
    return out


def assign_vertices_to_sim_bones(
    skin: "GltfSkin",
    sim_indices: List[int],
    vertex_joints: np.ndarray,   # (N, 4) uint16
    vertex_weights: np.ndarray,  # (N, 4) float32
) -> np.ndarray:
    """Map each vertex to one simulated joint index.

    A vertex follows its dominant (highest-weight) bone up the hierarchy to
    the nearest simulated bone. Unweighted / orphaned vertices fall back to
    the root sim bone, so all geometry lands in some hull.
    """
    n_joints = len(skin.joints)
    sim_set = set(int(i) for i in sim_indices)
    root_idx = int(sim_indices[0]) if sim_indices else 0

    joint_to_sim = _nearest_sim_ancestor_map(skin, sim_set)

    weights = np.asarray(vertex_weights, dtype=np.float64)
    joints = np.asarray(vertex_joints, dtype=np.int64)
    n = len(joints)
    if n == 0:
        return np.empty(0, dtype=np.int64)

    dom_col = np.argmax(weights, axis=1)
    dom = joints[np.arange(n), dom_col]
    dom = np.clip(dom, 0, n_joints - 1)

    vert_sim = joint_to_sim[dom]
    allzero = weights.sum(axis=1) <= 1e-9
    vert_sim[allzero] = root_idx
    vert_sim[vert_sim < 0] = root_idx
    return vert_sim


# ---------------------------------------------------------------------------
# Per-bone convex hulls
# ---------------------------------------------------------------------------

def reject_spike_outliers(
    pts: np.ndarray, *, mad_k: float = 5.0, min_keep: int = 8
) -> np.ndarray:
    """Drop the stray vertices that make a per-bone hull spike.

    A convex hull stretches to enclose its farthest point, so a few
    mis-weighted / transition / accessory vertices sitting far from a bone's
    main vertex cluster produce the long spikes seen in-game. This keeps only
    points within ``median + mad_k * MAD`` of the cluster's median position —
    a scale-robust threshold that nukes genuine spikes while preserving even
    long bones (whose natural spread is captured by the MAD). Never trims
    below ``min_keep`` points (keeps the nearest ones if it would).
    """
    if len(pts) <= min_keep:
        return pts
    centroid = np.median(pts, axis=0)
    d = np.linalg.norm(pts - centroid, axis=1)
    med = float(np.median(d))
    mad = float(np.median(np.abs(d - med)))
    if mad <= 1e-9:
        return pts  # tight cluster, nothing to reject
    thresh = med + mad_k * mad
    mask = d <= thresh
    if int(np.count_nonzero(mask)) < min_keep:
        keep = np.argsort(d)[:min_keep]
        mask = np.zeros(len(pts), dtype=bool)
        mask[keep] = True
    return pts[mask]


def build_per_bone_hulls(
    render_mesh: trimesh.Trimesh,
    vert_sim: np.ndarray,
    sim_bones: List[Tuple[int, str]],
    *,
    min_verts: int = 8,
    reject_outliers: bool = True,
) -> Tuple[List[Tuple[int, trimesh.Trimesh]], List[Tuple[int, str]]]:
    """Convex-hull each simulated bone's assigned vertices.

    Returns ``(hulls, skipped)`` where ``hulls`` is ``[(joint_idx, hull)]`` and
    ``skipped`` is ``[(joint_idx, reason)]`` for bones with too few vertices or
    a degenerate (zero-volume) hull. ``reject_outliers`` strips spike-causing
    stray vertices before hulling (see ``reject_spike_outliers``).
    """
    verts = np.asarray(render_mesh.vertices, dtype=np.float64)
    hulls: List[Tuple[int, trimesh.Trimesh]] = []
    skipped: List[Tuple[int, str]] = []

    backend = hull_backend_error()
    if backend is not None:
        # Every part would fail identically; report it once, not per bone.
        return hulls, [(sim_bones[0][0], backend)] if sim_bones else []

    for joint_idx, _name in sim_bones:
        mask = vert_sim == joint_idx
        count = int(np.count_nonzero(mask))
        if count < min_verts:
            skipped.append((joint_idx, f"only {count} vertices (< {min_verts})"))
            continue
        pts = verts[mask]
        if reject_outliers:
            pts = reject_spike_outliers(pts, min_keep=min_verts)
        try:
            hull = trimesh.Trimesh(vertices=pts, process=False).convex_hull
        except Exception as e:  # qhull failure on coplanar/collinear input
            skipped.append((joint_idx, f"hull failed: {e}"))
            continue
        if hull is None or len(hull.faces) == 0 or float(hull.volume) <= 1e-6:
            skipped.append((joint_idx, "degenerate hull"))
            continue
        hulls.append((joint_idx, hull))
    return hulls, skipped


# ---------------------------------------------------------------------------
# QC: $collisionjoints block
# ---------------------------------------------------------------------------

def build_collisionjoints_block(
    phys_smd_name: str,
    simulated_bone_names: List[str],
    root_bone_name: str,
    *,
    mass: float,
    inertia: float = presets.DEFAULT_INERTIA,
    damping: float = presets.DEFAULT_DAMPING,
    rotdamping: float = presets.DEFAULT_ROTDAMPING,
    constraint_source: str = "symmetric",
    constraint_rows: Optional[List[str]] = None,
) -> List[str]:
    """Build the ``$collisionjoints { ... }`` QC lines for a ragdoll.

    Globals live inside the block; every simulated bone except the root gets
    its ``$jointconstrain`` rows.

    ``constraint_source``:
      - ``"symmetric"`` — conservative +-deg on x/y/z (`presets.constraints_for`).
        Safe on ANY skeleton because it assumes nothing about which axis is the
        hinge, but it gives elbows and knees three degrees of freedom, so they
        bend sideways and hyperextend.
      - ``"valvebiped_donor"`` — Valve's anatomical rows, which make the elbow
        and knee true hinges (x/y pinned to 0). ONLY valid when the bones carry
        true ValveBiped orientations, i.e. a pose-conformed retarget.
    """
    lines: List[str] = []
    lines.append(f'$collisionjoints "{phys_smd_name}" {{')
    lines.append(f'    $mass {mass:.2f}')
    lines.append(f'    $inertia {inertia:.2f}')
    lines.append(f'    $damping {damping:.2f}')
    lines.append(f'    $rotdamping {rotdamping:.2f}')
    lines.append(f'    $rootbone "{root_bone_name}"')
    # Parity with the PHYS-driven path — these are what make a ragdoll settle
    # like a body rather than a puppet, and their absence here is why the
    # playermodel ragdoll felt worse than the standalone one:
    #   $noselfcollisions - parts stop jamming/jittering against each other
    #   $jointmassbias    - distal weighting so limbs swing with momentum
    #                       instead of every part massing by hull volume alone
    lines.append('    $noselfcollisions')
    for name in simulated_bone_names:
        # keyed by ValveBiped name, so map native names through as well
        bias = presets.DISTAL_MASS_BIAS.get(valvebiped_name(name))
        if bias:
            lines.append(f'    $jointmassbias "{name}" {bias:.2f}')
    lines.append('')
    if constraint_rows is not None:
        # Caller supplied the model's own converted limits; nothing generic.
        lines.extend(constraint_rows)
        lines.append('    $animatedfriction 1.0 1000.0 0.8 0.5 0.0')
        lines.append('}')
        return lines
    for name in simulated_bone_names:
        if name == root_bone_name:
            continue
        donor = (presets.donor_constraints_for(name)
                 if constraint_source == "valvebiped_donor" else None)
        if donor is not None:
            for axis, lo, hi, fric in donor:
                # studiomdl multiplies friction by 0.2 on the way into the
                # .phy, so emit /0.2 to land on the donor's value.
                lines.append(f'    $jointconstrain "{name}" {axis} limit '
                             f'{lo:.2f} {hi:.2f} {fric / 0.2:.2f}')
            continue
        for axis, mode, mn, mx, fr in presets.constraints_for(name):
            lines.append(
                f'    $jointconstrain "{name}" {axis} {mode} '
                f'{mn:.2f} {mx:.2f} {fr:.2f}'
            )
    # Friction ramp for the animation -> physics handoff; without it the
    # ragdoll drops into simulation with no damping and flails on spawn.
    lines.append('    $animatedfriction 1.0 1000.0 0.8 0.5 0.0')
    lines.append('}')
    return lines


# ---------------------------------------------------------------------------
# Real Source 2 hulls from the _physics.glb (preferred over weight-derived)
# ---------------------------------------------------------------------------

def build_hulls_from_physics_mesh(
    physics_mesh: trimesh.Trimesh,
    bone_ref_positions: Dict[int, np.ndarray],
    *,
    on_log: LogFn = print,
) -> List[Tuple[int, trimesh.Trimesh]]:
    """Recover real per-bone convex hulls from VRF's merged ``_physics.glb``.

    VRF bakes every Rubikon hull into model space and merges them into one mesh
    (grouped by collision-attribute/surface, not bone) with unshared vertices.
    So: weld coincident verts → split into connected components (each component
    is one real hull) → assign each component to the nearest bone → convex-hull
    the union assigned to each bone.

    ``bone_ref_positions`` maps ``joint_idx -> reference position`` and MUST be
    in the same coordinate space as ``physics_mesh`` vertices. We use the render
    mesh's own per-bone vertex centroids for this (not skeleton FK): the render
    verts are in identical raw space to the physics hull verts, whereas FK bone
    positions sit in a rotated frame (S2V bakes a Y-up→Z-up swap into the root
    bone), which would mis-assign every hull and collapse the collision onto the
    central bones. Returns ``[(joint_idx, hull)]``; empty if unusable.
    """
    F = np.asarray(physics_mesh.faces)
    if F is None or len(F) == 0:
        on_log("  physics glb has no faces; falling back to weight-derived hulls")
        return []
    if not bone_ref_positions:
        return []

    m = trimesh.Trimesh(
        vertices=np.asarray(physics_mesh.vertices, dtype=np.float64),
        faces=F, process=False,
    )
    # VRF emits 3 unshared verts per triangle; weld so each hull becomes one
    # connected component instead of a cloud of loose triangles.
    try:
        m.merge_vertices()
    except Exception:
        pass
    try:
        comps = list(m.split(only_watertight=False))
    except Exception:
        comps = []
    if not comps:
        comps = [m]

    ref_idx = list(bone_ref_positions.keys())
    ref_pos = np.array([bone_ref_positions[i] for i in ref_idx], dtype=np.float64)

    groups: dict = {}
    for comp in comps:
        if comp is None or len(comp.vertices) == 0:
            continue
        centroid = np.asarray(comp.vertices, dtype=np.float64).mean(axis=0)
        nearest = ref_idx[int(np.argmin(np.linalg.norm(ref_pos - centroid, axis=1)))]
        groups.setdefault(nearest, []).append(comp)

    hulls: List[Tuple[int, trimesh.Trimesh]] = []
    for joint_idx, cs in groups.items():
        pts = np.vstack([np.asarray(c.vertices, dtype=np.float64) for c in cs])
        if len(pts) < 4:
            continue
        try:
            hull = trimesh.Trimesh(vertices=pts, process=False).convex_hull
        except Exception:
            continue
        if hull is None or len(hull.faces) == 0 or float(hull.volume) <= 1e-6:
            continue
        hulls.append((joint_idx, hull))

    on_log(f"  physics glb: {len(comps)} hull component(s) -> {len(hulls)} bone hull(s)")
    return hulls


# ---------------------------------------------------------------------------
# PHYS-block-driven build (preferred: exact per-bone shapes + real joints)
# ---------------------------------------------------------------------------

def load_phys_dump(path: Path, on_log: LogFn = print) -> Optional["PhysData"]:
    """Parse a `<model>_phys.txt` sidecar (written by VRF Batch Export).
    Returns None (with a log line) on any failure — callers fall back to the
    weight-derived pipeline."""
    try:
        data = parse_phys_dump(Path(path).read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        on_log(f"  physics dump unreadable ({e}); falling back")
        return None
    if not data.parts:
        on_log("  physics dump has no parts; falling back")
        return None
    return data


def phys_root_part(phys: "PhysData") -> int:
    """Index of the root part: the one that is never a child (m_nBody2) in
    the joint tree; parts[0] when there are no joints (animated props)."""
    children = {j.body2 for j in phys.joints}
    for i in range(len(phys.parts)):
        if i not in children:
            return i
    return 0


def bones_above_phys_root(skin: "GltfSkin", phys: "PhysData",
                          rename: Callable[[str], str]) -> List[int]:
    """Skeleton joint indices sitting ABOVE the physics root bone (e.g.
    HLA's `zombie_root_motion`).

    These MUST be pruned before compiling a `$collisionjoints` model:
    studiomdl merges the root solid onto the skeleton root, and GMod then
    initializes that solid at the world origin instead of with the body
    (verified with a live dedicated-server probe — the ragdoll crumples on
    the first simulated tick).
    """
    root_bone = rename(phys.parts[phys_root_part(phys)].bone)
    by_name = {j.name: i for i, j in enumerate(skin.joints)}
    idx = by_name.get(root_bone)
    if idx is None:
        return []
    out: List[int] = []
    cur = skin.joints[idx].parent_joint_idx
    while cur is not None:
        out.append(cur)
        cur = skin.joints[cur].parent_joint_idx
    return out


def prune_skin_joints(
    skin: "GltfSkin",
    prune: List[int],
    vertex_joints: Optional[np.ndarray],
    clips: Optional[list] = None,
    on_log: LogFn = print,
) -> Optional[np.ndarray]:
    """Remove the given joints from the skin IN PLACE, folding each pruned
    joint's rest transform into its children so world binds are unchanged,
    remapping ``vertex_joints`` (returned) and each clip's channel keys.

    Weights on pruned joints move to the nearest kept ancestor (pruned roots
    are expected to be unweighted helpers; this is just a safety net).
    Animation channels of pruned joints are dropped.
    """
    if not prune:
        return vertex_joints
    prune_set = set(int(i) for i in prune)

    # Fold pruned-ancestor chains into each kept child so world binds stay
    # unchanged. The FULL chain is composed root-most first — a pairwise
    # parent-by-parent fold misses grandparents when a whole chain is
    # pruned together (e.g. the grunt's root_motion -> ground_root ->
    # pelvis_translations stack, which carries 120-degree rests).
    for j in skin.joints:
        p = j.parent_joint_idx
        if p is None or p not in prune_set:
            continue
        chain: List[int] = []
        cur: Optional[int] = p
        while cur is not None and cur in prune_set:
            chain.append(cur)
            cur = skin.joints[cur].parent_joint_idx
        W_r = np.eye(3)
        W_t = np.zeros(3)
        for ai in reversed(chain):              # root-most first
            a = skin.joints[ai]
            at = np.asarray(a.rest_translation, dtype=np.float64)
            W_t = W_r @ at + W_t
            W_r = W_r @ _quat_to_mat33(a.rest_rotation)
        j.rest_translation = W_r @ np.asarray(j.rest_translation,
                                              dtype=np.float64) + W_t
        j.rest_rotation = _mat33_to_quat(W_r @ _quat_to_mat33(j.rest_rotation))
        j.parent_joint_idx = cur

    keep = [i for i in range(len(skin.joints)) if i not in prune_set]
    old2new = {o: n for n, o in enumerate(keep)}

    # ancestors map for weight reassignment — snapshot the OLD parent
    # indices: the joint objects are reparented/renumbered in place below,
    # so walking skin.joints there would follow the wrong tree
    old_parent = [j.parent_joint_idx for j in skin.joints]

    def kept_ancestor(i: int) -> int:
        cur: Optional[int] = i
        while cur is not None and cur in prune_set:
            cur = old_parent[cur]
        return cur if cur is not None else keep[0]

    new_joints = [skin.joints[i] for i in keep]
    for j in new_joints:
        p = j.parent_joint_idx
        while p is not None and p in prune_set:
            p = skin.joints[p].parent_joint_idx
        j.parent_joint_idx = old2new[p] if p is not None else None
    skin.joints[:] = new_joints
    roots = [i for i, j in enumerate(skin.joints) if j.parent_joint_idx is None]
    skin.root_joint_idx = roots[0] if roots else 0

    if vertex_joints is not None:
        vj = np.asarray(vertex_joints)
        remap = np.arange(int(vj.max()) + 1 if vj.size else 1, dtype=vj.dtype)
        for o in range(len(remap)):
            t = kept_ancestor(o) if o in prune_set else o
            remap[o] = old2new.get(t, 0)
        vertex_joints = remap[vj]

    if clips:
        for clip in clips:
            ch = getattr(clip, "channels", None)
            if not isinstance(ch, dict):
                continue
            new_ch = {}
            for (ji, path), v in ch.items():
                if ji in prune_set:
                    continue
                new_ch[(old2new[ji], path)] = v
            ch.clear()
            ch.update(new_ch)

    on_log(f"  pruned {len(prune_set)} helper bone(s) above the physics root")
    return vertex_joints


def _quat_to_mat33(q) -> np.ndarray:
    x, y, z, w = np.asarray(q, dtype=np.float64)
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def hull_backend_error() -> Optional[str]:
    """Why convex hulls are unavailable, or None when they work.

    trimesh delegates ``convex_hull`` to ``scipy.spatial`` (qhull). When scipy
    is missing — an isolated build venv, or a frozen build that excluded it —
    every hull raises the same ImportError, which used to surface as one
    "hull failed" line per bone followed by an unexplained "no per-bone hulls
    could be built". Detect it once so the real cause is named.
    """
    try:
        import scipy.spatial  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - any import failure disables hulls
        return (f"convex hulls need scipy (trimesh uses scipy.spatial/qhull) "
                f"- {exc.__class__.__name__}: {exc}")
    return None


def _mat33_to_quat(m: np.ndarray) -> np.ndarray:
    t = np.trace(m)
    if t > 0:
        s = math.sqrt(t + 1.0) * 2
        return np.array([(m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s,
                         (m[1, 0] - m[0, 1]) / s, 0.25 * s], dtype=np.float32)
    i = int(np.argmax([m[0, 0], m[1, 1], m[2, 2]]))
    if i == 0:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        return np.array([0.25 * s, (m[0, 1] + m[1, 0]) / s,
                         (m[0, 2] + m[2, 0]) / s, (m[2, 1] - m[1, 2]) / s], dtype=np.float32)
    if i == 1:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        return np.array([(m[0, 1] + m[1, 0]) / s, 0.25 * s,
                         (m[1, 2] + m[2, 1]) / s, (m[0, 2] - m[2, 0]) / s], dtype=np.float32)
    s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
    return np.array([(m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s,
                     0.25 * s, (m[1, 0] - m[0, 1]) / s], dtype=np.float32)


def conformed_s2_constraint_rows(
    phys: "PhysData",
    skin: "GltfSkin",
    rename: Callable[[str], str],
    anat_rotations: Dict[int, np.ndarray],
    *,
    widen: float = 1.35,
    on_log: LogFn = print,
) -> List[str]:
    """``$jointconstrain`` rows from the REAL Source 2 joint limits, re-expressed
    in a CONFORMED skeleton's bone frames.

    ``joint_constraint_rows_v2`` emits rows about the child bone's LOCAL axes,
    read from the joint's bone-local ``frame2_quat``. After a conform the bones
    carry donor orientations, so those rows would constrain the wrong axes.

    Carrying the axis across is NOT a matter of preserving its coordinates: the
    Source 2 and Source 1 model spaces differ by a global yaw (the S2 rig is
    lateral-Y, ValveBiped lateral-X) on top of each bone's own axis convention,
    so an unchanged vector silently turns a knee hinge into a sideways one.
    ``anat_rotations[i]`` is the per-bone rotation the re-skin applied, mapping
    a direction in the source model space to the conformed one — the same
    transform the flesh went through. With the bone's source world rotation
    ``Ro`` and conformed ``Rn``, a joint frame ``F`` becomes
    ``Rn^T @ A @ Ro @ F``.
    """
    import dataclasses

    by_name = {j.name: i for i, j in enumerate(skin.joints)}
    world: Dict[int, np.ndarray] = {}

    def fk(i: int) -> np.ndarray:
        if i in world:
            return world[i]
        j = skin.joints[i]
        m = np.eye(4)
        m[:3, :3] = _quat_to_mat33(j.rest_rotation)
        m[:3, 3] = np.asarray(j.rest_translation, dtype=np.float64)
        p = j.parent_joint_idx
        world[i] = m if p is None else fk(p) @ m
        return world[i]

    rows: List[str] = []
    converted = 0
    for jt in phys.joints:
        parent, child = phys.parts[jt.body1], phys.parts[jt.body2]
        pn, cn = rename(parent.bone), rename(child.bone)
        pi, ci = by_name.get(pn), by_name.get(cn)
        if pi is None or ci is None:
            continue
        Rn_p = fk(pi)[:3, :3]
        Rn_c = fk(ci)[:3, :3]
        Ro_p = np.asarray(parent.bind_pose, dtype=np.float64)[:3, :3]
        Ro_c = np.asarray(child.bind_pose, dtype=np.float64)[:3, :3]
        A_p = anat_rotations.get(pi)
        A_c = anat_rotations.get(ci)
        if A_p is None or A_c is None:
            continue
        reframed = dataclasses.replace(
            jt,
            frame1_quat=_mat33_to_quat(
                Rn_p.T @ A_p @ Ro_p
                @ _quat_to_mat33(jt.frame1_quat)).astype(float),
            frame2_quat=_mat33_to_quat(
                Rn_c.T @ A_c @ Ro_c
                @ _quat_to_mat33(jt.frame2_quat)).astype(float),
        )
        for axis, lo, hi in joint_constraint_rows_v2(
                reframed, Rn_p, Rn_c, widen=widen):
            rows.append(f'    $jointconstrain "{cn}" {axis} limit '
                        f'{lo:.2f} {hi:.2f} 1.00')
        converted += 1
    on_log(f"  constraints: {converted} joint(s) converted from real Source 2 "
           "limits, re-expressed in the conformed bone frames")
    return rows


def phys_part_point_clouds(
    phys: "PhysData",
    skin: "GltfSkin",
    rename: Callable[[str], str],
) -> List[Tuple[int, np.ndarray]]:
    """Per-part model-space PHYS points, before they are hulled.

    Same transform ``build_hulls_from_phys_parts`` applies (bone-local shape
    points through the part's bind pose), but stopping short of the convex
    hull so the caller can put the points through a re-skin first. The
    playermodel path needs this: its mesh is conformed into the donor's bind,
    so authored Rubikon shapes left in Source 2 bind space would attach at the
    wrong offsets.
    """
    by_name = {j.name: i for i, j in enumerate(skin.joints)}
    out: List[Tuple[int, np.ndarray]] = []
    for part in phys.parts:
        ji = by_name.get(rename(part.bone))
        if ji is None:
            continue
        pts = np.vstack([shape_points(s) for s in part.shapes])
        h = np.hstack([pts, np.ones((len(pts), 1))])
        out.append((ji, (part.bind_pose @ h.T).T[:, :3]))
    return out


def hulls_from_point_clouds(
    clouds: List[Tuple[int, np.ndarray]],
    *,
    on_log: LogFn = print,
) -> List[Tuple[int, trimesh.Trimesh]]:
    """Convex-hull each ``(joint_index, points)`` cloud, dropping degenerates."""
    backend = hull_backend_error()
    if backend is not None:
        on_log(f"  {backend}")
        return []
    hulls: List[Tuple[int, trimesh.Trimesh]] = []
    for ji, pts in clouds:
        if len(pts) < 4:
            continue
        try:
            hull = trimesh.Trimesh(vertices=pts, process=False).convex_hull
        except Exception as e:  # noqa: BLE001 - qhull on degenerate input
            on_log(f"  hull failed for joint {ji}: {e}")
            continue
        if hull is None or len(hull.faces) == 0 or float(hull.volume) <= 1e-6:
            continue
        hulls.append((ji, hull))
    return hulls


def build_hulls_from_phys_parts(
    phys: "PhysData",
    skin: "GltfSkin",
    rename: Callable[[str], str],
    *,
    on_log: LogFn = print,
) -> Tuple[List[Tuple[int, trimesh.Trimesh]], List[str]]:
    """One convex hull per physics part, transformed bone-local -> model
    space by the part's bind pose (verified identical to the glTF/SMD
    space). Returns ``(hulls, missing_bone_names)``."""
    by_name = {j.name: i for i, j in enumerate(skin.joints)}
    hulls: List[Tuple[int, trimesh.Trimesh]] = []
    missing: List[str] = []
    for part in phys.parts:
        bone = rename(part.bone)
        ji = by_name.get(bone)
        if ji is None:
            missing.append(bone)
            continue
        pts = np.vstack([shape_points(s) for s in part.shapes])
        h = np.hstack([pts, np.ones((len(pts), 1))])
        model_pts = (part.bind_pose @ h.T).T[:, :3]
        try:
            hull = trimesh.Trimesh(vertices=model_pts, process=False).convex_hull
        except Exception as e:
            on_log(f"  hull failed for part '{bone}': {e}")
            continue
        if hull is None or len(hull.faces) == 0 or float(hull.volume) <= 1e-6:
            continue
        hulls.append((ji, hull))
    if missing:
        on_log(f"  physics parts with no matching bone: {', '.join(missing)}")
    return hulls, missing


def build_hitbox_lines(phys: "PhysData",
                       rename: Callable[[str], str]) -> List[str]:
    """``$hbox`` lines from the PHYS parts' bone-local shape AABBs.

    One hitbox per physics part, sized to the part's shapes in PART-LOCAL
    space (hitboxes are authored bone-local, so no bind transform). Hitgroup
    via the ValveBiped identity of the part's bone
    (`presets.HITGROUP_OF`), 0 (generic) for anything unmapped. From the
    verified zombie_classic ragdoll build.
    """
    lines: List[str] = []
    for part in phys.parts:
        if not part.shapes:
            continue
        pts = np.vstack([shape_points(s) for s in part.shapes])
        lo = pts.min(axis=0)
        hi = pts.max(axis=0)
        grp = presets.HITGROUP_OF.get(valvebiped_name(part.bone), 0)
        lines.append(f'$hbox {grp} "{rename(part.bone)}" '
                     f'{lo[0]:.2f} {lo[1]:.2f} {lo[2]:.2f} '
                     f'{hi[0]:.2f} {hi[1]:.2f} {hi[2]:.2f}')
    return lines


def hitbox_lines_from_hulls(
    skin,
    hulls: List[Tuple[int, "trimesh.Trimesh"]],
) -> List[str]:
    """``$hbox`` lines from already-built collision hulls in MODEL space.

    The PHYS path (`build_hitbox_lines`) reads part-local shape points straight
    out of the dump, but a conformed playermodel's hulls have been carried into
    the donor's bind space, so the bone-local AABB has to be taken against the
    CONFORMED bind: `inv(bone_world) @ hull_vertices`. Same hitgroup table, so
    a playermodel gets the hitboxes its ragdoll counterpart gets.
    """
    world: Dict[int, np.ndarray] = {}

    def _fk(i: int) -> np.ndarray:
        if i in world:
            return world[i]
        j = skin.joints[i]
        m = np.eye(4)
        m[:3, :3] = _quat_to_mat33(j.rest_rotation)
        m[:3, 3] = np.asarray(j.rest_translation, dtype=np.float64)
        p = j.parent_joint_idx
        world[i] = m if p is None else _fk(p) @ m
        return world[i]

    lines: List[str] = []
    for ji, hull in hulls:
        v = np.asarray(hull.vertices, dtype=np.float64)
        if not len(v):
            continue
        inv = np.linalg.inv(_fk(int(ji)))
        loc = v @ inv[:3, :3].T + inv[:3, 3]
        lo, hi = loc.min(axis=0), loc.max(axis=0)
        name = skin.joints[int(ji)].name
        grp = presets.HITGROUP_OF.get(valvebiped_name(name), 0)
        lines.append(f'$hbox {grp} "{name}" '
                     f'{lo[0]:.2f} {lo[1]:.2f} {lo[2]:.2f} '
                     f'{hi[0]:.2f} {hi[1]:.2f} {hi[2]:.2f}')
    return lines


def build_collisionjoints_from_phys(
    phys_smd_name: str,
    phys: "PhysData",
    rename: Callable[[str], str],
    *,
    mass: float,
    widen: float = 1.35,
    inertia: float = presets.DEFAULT_INERTIA,
    damping: float = presets.DEFAULT_DAMPING,
    rotdamping: float = presets.DEFAULT_ROTDAMPING,
    constraint_source: str = "s2",
    mass_bias: Optional[Dict[str, float]] = None,
    on_log: LogFn = print,
) -> List[str]:
    """`$collisionjoints` QC block from real Rubikon joints.

    Joint limits are converted with the verified frame-Z / bind-offset /
    clockwise semantics (see `utils/phys_kv3.py`). Models with NO joints
    (animated props like the HLA combine barriers) get no `$jointconstrain`
    lines at all — the compiled default is fixed-on-all-axes, which yields a
    rigid multi-solid prop that gets per-bone bone followers on prop_dynamic.
    QC friction 1.0 == the canonical 0.2 in the compiled .phy.

    ``constraint_source``:
      - ``"s2"`` — converted Source 2 joint limits (any skeleton; default).
      - ``"valvebiped_donor"`` — Valve's hand-tuned humanoid values
        (`presets.VALVEBIPED_DONOR_CONSTRAINTS`) for bones present in the
        table, S2-converted rows for the rest. ONLY valid when the bones
        carry true ValveBiped orientations (pose-conformed retarget) — on a
        rename-only native skeleton these numbers constrain the wrong axes.
    """
    root_bone = rename(phys.parts[phys_root_part(phys)].bone)
    lines: List[str] = []
    lines.append(f'$collisionjoints "{phys_smd_name}" {{')
    lines.append(f'    $mass {mass:.2f}')
    lines.append(f'    $inertia {inertia:.2f}')
    lines.append(f'    $damping {damping:.2f}')
    lines.append(f'    $rotdamping {rotdamping:.2f}')
    lines.append(f'    $rootbone "{root_bone}"')
    if phys.joints:
        lines.append('    $noselfcollisions')
    for bone_name, bias in (mass_bias or {}).items():
        lines.append(f'    $jointmassbias "{bone_name}" {bias:.2f}')
    lines.append('')
    donor_hits = 0
    for j in phys.joints:
        parent = phys.parts[j.body1]
        child = phys.parts[j.body2]
        child_name = rename(child.bone)
        donor = (presets.donor_constraints_for(child_name)
                 if constraint_source == "valvebiped_donor" else None)
        if donor is not None:
            donor_hits += 1
            for axis, lo, hi, fric in donor:
                # studiomdl writes friction*0.2 into the .phy — emit /0.2 so
                # the compiled value equals the donor's.
                lines.append(f'    $jointconstrain "{child_name}" {axis} limit '
                             f'{lo:.2f} {hi:.2f} {fric / 0.2:.2f}')
            continue
        rows = joint_constraint_rows_v2(
            j, parent.bind_pose[:3, :3], child.bind_pose[:3, :3], widen=widen)
        for axis, lo, hi in rows:
            lines.append(f'    $jointconstrain "{child_name}" {axis} limit '
                         f'{lo:.2f} {hi:.2f} 1.00')
    if constraint_source == "valvebiped_donor":
        on_log(f"  constraints: {donor_hits} joint(s) from the ValveBiped "
               f"donor table, {len(phys.joints) - donor_hits} converted from "
               "Source 2 data")
    lines.append('    $animatedfriction 1.0 1000.0 0.8 0.5 0.0')
    lines.append('}')
    return lines


def build_ragdoll_phys(
    *,
    phys: "PhysData",
    skin: "GltfSkin",
    coord: CoordinateMode,
    phys_smd_path: Path,
    phys_smd_name: str,
    total_mass: float,
    generate_smd: bool,
    widen: float = 1.35,
    constraint_source: str = "s2",
    mass_bias: Optional[Dict[str, float]] = None,
    on_log: LogFn = print,
) -> Tuple[Optional[List[str]], Optional[str]]:
    """PHYS-block-driven ragdoll build: exact per-bone hulls from the real
    Rubikon shapes + `$collisionjoints` with converted real joint limits.

    Works for ANY jointed model — humanoids, creatures, and animated props
    (empty `m_joints` compiles to fixed joints -> rigid multi-solid prop with
    bone followers on prop_dynamic). Requires native S2V units (the PHYS
    geometry is raw vmdl space; a scaled/axis-swapped import won't line up).
    Returns ``(qc_lines, None)`` or ``(None, error)``.
    """
    if coord.scale != 1.0 or coord.swap_axes:
        return None, ("PHYS-based physics needs native S2V units "
                      "(scale 1, no axis swap)")

    by_name = {j.name for j in skin.joints}

    def rename(n: str) -> str:
        # the shared skin may already be retargeted to ValveBiped names
        return n if n in by_name else valvebiped_name(n)

    hulls, _missing = build_hulls_from_phys_parts(phys, skin, rename, on_log=on_log)
    if not hulls:
        return None, "no physics parts matched the skeleton"
    if phys.joints and len(hulls) > 32:
        on_log(f"  WARNING: {len(hulls)} simulated parts exceeds the 32-part "
               "ragdoll limit — the ragdoll will not spawn in GMod")
    elif len(hulls) > 99:
        on_log(f"  WARNING: {len(hulls)} parts exceeds the 99-part limit "
               "for animated models")

    if generate_smd:
        ok, err = SmdRagdollPhysicsExporter.write(hulls, skin, phys_smd_path, coord)
        if not ok:
            return None, err or "physics SMD write failed"

    lines = build_collisionjoints_from_phys(
        phys_smd_name, phys, rename, mass=total_mass, widen=widen,
        constraint_source=constraint_source, mass_bias=mass_bias,
        on_log=on_log)
    kind = "ragdoll" if phys.joints else "jointed prop (no joints — rigid)"
    on_log(f"  {kind}: {len(hulls)} part(s), {len(phys.joints)} joint(s) "
           "from PHYS data")
    return lines, None


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def build_ragdoll(
    *,
    render_mesh: trimesh.Trimesh,
    vertex_joints: np.ndarray,
    vertex_weights: np.ndarray,
    skin: "GltfSkin",
    coord: CoordinateMode,
    phys_smd_path: Path,
    phys_smd_name: str,
    total_mass: float,
    generate_smd: bool,
    physics_mesh: Optional[trimesh.Trimesh] = None,
    prebuilt_hulls: Optional[List[Tuple[int, trimesh.Trimesh]]] = None,
    constraint_rows: Optional[List[str]] = None,
    min_hull_verts: int = 8,
    constraint_source: str = "symmetric",
    rotdamping: float = presets.DEFAULT_ROTDAMPING,
    out_real_hulls: Optional[List[Tuple[int, "trimesh.Trimesh"]]] = None,
    on_log: LogFn = print,
) -> Tuple[Optional[List[str]], Optional[str]]:
    """Build the per-bone hulls, write the ragdoll physics SMD, and return the
    ``$collisionjoints`` QC lines.

    Prefers the real Source 2 hulls from ``physics_mesh`` (the model's
    ``_physics.glb``) when supplied and usable, falling back to weight-derived
    hulls otherwise. Returns ``(qc_lines, None)`` on success or ``(None, error)``
    on failure. Assumes ``skin`` is already retargeted to ValveBiped names.
    """
    sim_bones = select_sim_bones(skin)
    if not sim_bones:
        return None, "no simulated bones matched the humanoid set"

    sim_indices = [i for i, _ in sim_bones]

    # Per-bone vertex assignment from skin weights — drives BOTH the weight
    # fallback hulls and the reference positions used to assign real physics
    # hulls (render verts share the physics hulls' raw coordinate space).
    vert_sim = assign_vertices_to_sim_bones(
        skin, sim_indices, vertex_joints, vertex_weights
    )
    rverts = np.asarray(render_mesh.vertices, dtype=np.float64)
    bone_ref: Dict[int, np.ndarray] = {}
    for ji in sim_indices:
        sel = vert_sim == ji
        if int(np.count_nonzero(sel)) > 0:
            bone_ref[ji] = rverts[sel].mean(axis=0)

    hulls: List[Tuple[int, trimesh.Trimesh]] = []
    # Real authored geometry (Rubikon shapes or the _physics.glb) vs blobs
    # fitted to skin weights. Only the former is precise enough to derive
    # hitboxes from - see the fill of out_real_hulls below.
    hulls_are_real = False
    if prebuilt_hulls:
        # Caller already has real PHYS hulls in this skin's space (the
        # playermodel path pushes the Rubikon shapes through the same conform
        # as the render mesh, so they land in donor bind space).
        hulls = [(ji, h) for ji, h in prebuilt_hulls if ji in set(sim_indices)]
        dropped = len(prebuilt_hulls) - len(hulls)
        on_log(f"  hull source: Source 2 PHYS shapes ({len(hulls)} part(s)"
               + (f", {dropped} not in the simulated set" if dropped else "")
               + ")")
        hulls_are_real = True
    if not hulls and physics_mesh is not None and bone_ref:
        real = build_hulls_from_physics_mesh(physics_mesh, bone_ref, on_log=on_log)
        # Guard against single-mesh / non-ragdoll collision (or a recovery that
        # collapses): if it covers too few bones, the weight hulls give better
        # per-bone coverage.
        if len(real) >= 4:
            hulls = real
            hulls_are_real = True
            on_log("  hull source: real Source 2 physics mesh")
        elif real:
            on_log(f"  physics glb yielded only {len(real)} bone hull(s) "
                   "(not a per-bone ragdoll?); using weight-derived hulls instead")

    if not hulls:
        weight_hulls, skipped = build_per_bone_hulls(
            render_mesh, vert_sim, sim_bones, min_verts=min_hull_verts
        )
        for joint_idx, reason in skipped:
            on_log(f"  skip ragdoll part '{skin.joints[joint_idx].name}': {reason}")
        hulls = weight_hulls
        if hulls:
            on_log("  hull source: weight-derived (render mesh)")

    if not hulls:
        return None, "no per-bone hulls could be built"
    if out_real_hulls is not None and hulls_are_real:
        out_real_hulls.extend(hulls)

    hull_indices = {i for i, _ in hulls}
    root_idx = sim_indices[0]
    root_name = skin.joints[root_idx].name
    if root_idx not in hull_indices:
        root_name = skin.joints[hulls[0][0]].name
        on_log(f"  pelvis had no hull; using '{root_name}' as ragdoll root")

    if generate_smd:
        ok, err = SmdRagdollPhysicsExporter.write(hulls, skin, phys_smd_path, coord)
        if not ok:
            return None, err or "physics SMD write failed"

    simulated_names = [skin.joints[i].name for i, _ in hulls]
    lines = build_collisionjoints_block(
        phys_smd_name, simulated_names, root_name, mass=total_mass,
        rotdamping=rotdamping, constraint_source=constraint_source,
        constraint_rows=constraint_rows,
    )
    if constraint_rows is None and constraint_source == "valvebiped_donor":
        hinged = sum(1 for n in simulated_names
                     if presets.donor_constraints_for(n) is not None)
        on_log(f"  constraints: {hinged} joint(s) from the ValveBiped donor "
               f"table (anatomical hinges), "
               f"{len(simulated_names) - hinged - 1} symmetric")
    on_log(f"  ragdoll: {len(hulls)} simulated part(s), root='{root_name}'")
    return lines, None
