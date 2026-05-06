"""Manual glTF JSON + binary-buffer parser for skin / animation / skin-vertex data.

The existing GLTF→SMD pipeline uses ``trimesh`` for geometry, but trimesh does
not expose skin or animation data. ``pygltflib`` is not in the project's
[requirements.txt](../../requirements.txt). This module parses the relevant
slices of the glTF spec directly, just enough to support animation export:

* skin joint hierarchy + inverseBindMatrices
* per-vertex JOINTS_0 / WEIGHTS_0 attributes
* animation channel samplers (LINEAR / STEP / CUBICSPLINE)

Geometry parsing (POSITION/NORMAL/TEXCOORD_0/face indices) stays in
[gltf_smd_batch_tool.py](gltf_smd_batch_tool.py) via trimesh.
"""
from __future__ import annotations

import base64
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

# glTF componentType constants
_COMPONENT = {
    5120: ('b', 1, np.int8),    # BYTE
    5121: ('B', 1, np.uint8),   # UNSIGNED_BYTE
    5122: ('h', 2, np.int16),   # SHORT
    5123: ('H', 2, np.uint16),  # UNSIGNED_SHORT
    5125: ('I', 4, np.uint32),  # UNSIGNED_INT
    5126: ('f', 4, np.float32), # FLOAT
}

# glTF type strings → component count
_TYPE_COUNT = {
    'SCALAR': 1,
    'VEC2': 2,
    'VEC3': 3,
    'VEC4': 4,
    'MAT2': 4,
    'MAT3': 9,
    'MAT4': 16,
}


@dataclass
class GltfJoint:
    """Single skin joint mapped to a node, with parent-joint linkage."""
    name: str
    gltf_node_idx: int
    parent_joint_idx: Optional[int]   # index in skin.joints list, not node idx
    inverse_bind_matrix: np.ndarray   # 4x4 column-major, as glTF stores it
    rest_translation: np.ndarray      # (3,) parent-local; from node.translation or zeros
    rest_rotation: np.ndarray         # (4,) [x,y,z,w]; from node.rotation or [0,0,0,1]
    rest_scale: np.ndarray            # (3,); from node.scale or [1,1,1]


@dataclass
class GltfSkin:
    joints: List[GltfJoint]
    root_joint_idx: int   # index of the joint whose parent_joint_idx is None
    skin_idx: int         # index in gltf_data['skins']

    @property
    def joint_names(self) -> List[str]:
        return [j.name for j in self.joints]


@dataclass
class GltfClip:
    """One animation clip; channels keyed by (joint_idx_in_skin, path)."""
    name: str
    duration: float
    # (joint_idx, 'translation'|'rotation'|'scale') -> (times[k], values[k,N], interp)
    channels: Dict[Tuple[int, str], Tuple[np.ndarray, np.ndarray, str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Buffer + accessor reading
# ---------------------------------------------------------------------------

def load_buffer_bytes(gltf_data: dict, base_dir: Path) -> Optional[bytes]:
    """Load buffer 0's bytes. Returns None for GLB or unresolvable buffers.

    Supports external .bin files and data URIs. GLB-embedded buffers
    (the chunk after the JSON) are not supported by this first pass.
    """
    buffers = gltf_data.get('buffers') or []
    if not buffers:
        return None
    buf = buffers[0]
    uri = buf.get('uri')
    if not uri:
        # GLB-embedded buffer; out of scope for animation in this pass.
        return None
    if uri.startswith('data:'):
        # data:application/octet-stream;base64,XXXX
        comma = uri.find(',')
        if comma < 0:
            return None
        return base64.b64decode(uri[comma + 1:])
    bin_path = base_dir / uri
    if not bin_path.exists():
        return None
    return bin_path.read_bytes()


def read_accessor(gltf_data: dict, buffer: bytes, accessor_idx: int) -> np.ndarray:
    """Decode a glTF accessor into a numpy array.

    Returns a 2D array of shape (count, n_components) for vector/matrix types,
    or shape (count,) for SCALAR. Honors bufferView.byteStride for interleaved
    attributes. Sparse accessors are not supported (rare; raises ValueError).
    """
    acc = gltf_data['accessors'][accessor_idx]
    if 'sparse' in acc:
        raise ValueError(f"Sparse accessor {accessor_idx} not supported")

    component_type = acc['componentType']
    type_str = acc['type']
    count = acc['count']
    fmt_char, comp_size, np_dtype = _COMPONENT[component_type]
    n_components = _TYPE_COUNT[type_str]
    elem_size = comp_size * n_components

    bv = gltf_data['bufferViews'][acc['bufferView']]
    bv_offset = bv.get('byteOffset', 0)
    acc_offset = acc.get('byteOffset', 0)
    base = bv_offset + acc_offset
    stride = bv.get('byteStride', elem_size)

    if stride == elem_size:
        # Tightly packed; one slice + frombuffer is fastest.
        end = base + elem_size * count
        raw = np.frombuffer(buffer, dtype=np_dtype, count=count * n_components, offset=base)
        arr = raw.copy()  # writable view
    else:
        # Interleaved; pull each element separately.
        arr = np.empty(count * n_components, dtype=np_dtype)
        for i in range(count):
            off = base + i * stride
            chunk = np.frombuffer(buffer, dtype=np_dtype, count=n_components, offset=off)
            arr[i * n_components:(i + 1) * n_components] = chunk

    if type_str == 'SCALAR':
        out = arr.reshape(count)
    else:
        out = arr.reshape(count, n_components)

    # Honor `normalized` for integer accessors (e.g. unit-norm WEIGHTS_0 stored as ushort).
    if acc.get('normalized') and component_type in (5120, 5121, 5122, 5123):
        out = out.astype(np.float32)
        if component_type == 5120:
            out = np.maximum(out / 127.0, -1.0)
        elif component_type == 5121:
            out = out / 255.0
        elif component_type == 5122:
            out = np.maximum(out / 32767.0, -1.0)
        elif component_type == 5123:
            out = out / 65535.0

    return out


# ---------------------------------------------------------------------------
# Skin parsing
# ---------------------------------------------------------------------------

def _build_node_to_parent(gltf_data: dict) -> Dict[int, int]:
    """Map gltf_node_idx → its parent gltf_node_idx (only for nodes that have one)."""
    parent: Dict[int, int] = {}
    for n_idx, node in enumerate(gltf_data.get('nodes') or []):
        for child in node.get('children') or []:
            parent[child] = n_idx
    return parent


def parse_skin(gltf_data: dict, buffer: Optional[bytes], skin_idx: int = 0) -> Optional[GltfSkin]:
    """Parse skin[skin_idx] into a GltfSkin. Returns None if absent / malformed."""
    skins = gltf_data.get('skins') or []
    if skin_idx >= len(skins):
        return None
    skin = skins[skin_idx]

    joint_node_indices: List[int] = list(skin.get('joints') or [])
    if not joint_node_indices:
        return None

    nodes = gltf_data.get('nodes') or []
    node_to_parent = _build_node_to_parent(gltf_data)
    node_to_joint_idx: Dict[int, int] = {n: i for i, n in enumerate(joint_node_indices)}

    # Inverse-bind matrices (optional per spec; default identity).
    ibm_acc_idx = skin.get('inverseBindMatrices')
    if ibm_acc_idx is not None and buffer is not None:
        ibm_arr = read_accessor(gltf_data, buffer, ibm_acc_idx)
        # glTF stores MAT4 column-major; reshape (count, 16) -> (count, 4, 4) col-major.
        ibm_matrices = ibm_arr.reshape(len(joint_node_indices), 4, 4).transpose(0, 2, 1).copy()
    else:
        ibm_matrices = np.tile(np.eye(4, dtype=np.float32), (len(joint_node_indices), 1, 1))

    joints: List[GltfJoint] = []
    root_joint_idx: Optional[int] = None

    for joint_idx, node_idx in enumerate(joint_node_indices):
        node = nodes[node_idx] if node_idx < len(nodes) else {}
        name = node.get('name') or f'joint_{joint_idx}'

        parent_node = node_to_parent.get(node_idx)
        # The parent is "in skin" only if the parent node is itself a joint of this skin.
        parent_joint_idx = node_to_joint_idx.get(parent_node) if parent_node is not None else None

        if parent_joint_idx is None:
            if root_joint_idx is None:
                root_joint_idx = joint_idx
            # If multiple roots exist we keep the first; $root pins it later.

        rest_t = np.array(node.get('translation') or [0.0, 0.0, 0.0], dtype=np.float32)
        rest_r = np.array(node.get('rotation') or [0.0, 0.0, 0.0, 1.0], dtype=np.float32)
        rest_s = np.array(node.get('scale') or [1.0, 1.0, 1.0], dtype=np.float32)

        joints.append(GltfJoint(
            name=name,
            gltf_node_idx=node_idx,
            parent_joint_idx=parent_joint_idx,
            inverse_bind_matrix=ibm_matrices[joint_idx],
            rest_translation=rest_t,
            rest_rotation=rest_r,
            rest_scale=rest_s,
        ))

    if root_joint_idx is None:
        root_joint_idx = 0

    return GltfSkin(joints=joints, root_joint_idx=root_joint_idx, skin_idx=skin_idx)


# ---------------------------------------------------------------------------
# Skin vertex attributes (JOINTS_0 / WEIGHTS_0)
# ---------------------------------------------------------------------------

def parse_skin_vertex_data(gltf_data: dict, buffer: bytes, mesh_idx: int = 0
                            ) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
    """Concatenate JOINTS_0 / WEIGHTS_0 across all primitives of one mesh.

    Returns (joints[N,4] uint16, weights[N,4] float32). Either may be None
    if missing. Joint indices are local to the mesh's skin (i.e. they index
    into ``GltfSkin.joints`` directly, by glTF convention).
    """
    meshes = gltf_data.get('meshes') or []
    if mesh_idx >= len(meshes):
        return (None, None)

    joints_chunks: List[np.ndarray] = []
    weights_chunks: List[np.ndarray] = []

    for prim in meshes[mesh_idx].get('primitives') or []:
        attrs = prim.get('attributes') or {}
        j_idx = attrs.get('JOINTS_0')
        w_idx = attrs.get('WEIGHTS_0')
        if j_idx is None or w_idx is None:
            return (None, None)

        j_raw = read_accessor(gltf_data, buffer, j_idx).astype(np.uint16)
        w_raw = read_accessor(gltf_data, buffer, w_idx).astype(np.float32)
        if j_raw.ndim == 1:
            j_raw = j_raw.reshape(-1, 1)
        if w_raw.ndim == 1:
            w_raw = w_raw.reshape(-1, 1)
        if j_raw.shape[1] != 4 or w_raw.shape[1] != 4:
            return (None, None)

        joints_chunks.append(j_raw)
        weights_chunks.append(w_raw)

    if not joints_chunks:
        return (None, None)

    joints = np.concatenate(joints_chunks, axis=0)
    weights = np.concatenate(weights_chunks, axis=0)

    # Renormalise rows so each vertex's weights sum to 1.0; zero rows kept as-is
    # so the caller can handle the unweighted-vertex case explicitly.
    row_sums = weights.sum(axis=1, keepdims=True)
    nz = row_sums[:, 0] > 1e-6
    weights[nz] = weights[nz] / row_sums[nz]

    return (joints, weights)


# ---------------------------------------------------------------------------
# Animation parsing + sampling
# ---------------------------------------------------------------------------

def parse_clips(gltf_data: dict, buffer: bytes, skin: GltfSkin) -> List[GltfClip]:
    """Parse all glTF animations into GltfClip objects.

    Channels targeting nodes outside ``skin`` are silently dropped (Source's
    SMD has no concept of them).
    """
    node_to_joint_idx: Dict[int, int] = {j.gltf_node_idx: i for i, j in enumerate(skin.joints)}
    clips: List[GltfClip] = []

    for anim_idx, anim in enumerate(gltf_data.get('animations') or []):
        name = anim.get('name') or f'anim_{anim_idx}'
        clip = GltfClip(name=name, duration=0.0)

        samplers = anim.get('samplers') or []
        for ch in anim.get('channels') or []:
            target = ch.get('target') or {}
            node_idx = target.get('node')
            path = target.get('path')
            if node_idx is None or path not in ('translation', 'rotation', 'scale'):
                continue
            joint_idx = node_to_joint_idx.get(node_idx)
            if joint_idx is None:
                continue

            sampler_idx = ch.get('sampler')
            if sampler_idx is None or sampler_idx >= len(samplers):
                continue
            sampler = samplers[sampler_idx]
            interp = sampler.get('interpolation', 'LINEAR')

            in_acc = sampler.get('input')
            out_acc = sampler.get('output')
            if in_acc is None or out_acc is None:
                continue

            times = read_accessor(gltf_data, buffer, in_acc).astype(np.float32)
            values = read_accessor(gltf_data, buffer, out_acc).astype(np.float32)

            if times.ndim != 1 or len(times) == 0:
                continue
            clip.channels[(joint_idx, path)] = (times, values, interp)
            if times[-1] > clip.duration:
                clip.duration = float(times[-1])

        clips.append(clip)

    return clips


def _sample_at(times: np.ndarray, values: np.ndarray, interp: str, t: float) -> np.ndarray:
    """Sample one channel at time ``t`` honoring sampler interpolation.

    For CUBICSPLINE the ``values`` array is laid out as
    [in-tangent_k, value_k, out-tangent_k] for each keyframe (3× length).
    Falls back to LINEAR if interp is unknown.
    """
    n = len(times)
    if n == 0:
        return values[0] if interp != 'CUBICSPLINE' else values[1]
    if t <= times[0]:
        if interp == 'CUBICSPLINE':
            return values[1]   # value at first keyframe (skip in-tangent)
        return values[0]
    if t >= times[-1]:
        if interp == 'CUBICSPLINE':
            return values[(n - 1) * 3 + 1]
        return values[-1]

    # Locate interval [k, k+1] containing t.
    k = int(np.searchsorted(times, t, side='right')) - 1
    k = max(0, min(k, n - 2))
    t0 = times[k]
    t1 = times[k + 1]
    dt = t1 - t0
    u = 0.0 if dt <= 0 else (t - t0) / dt

    if interp == 'STEP':
        return values[k]

    if interp == 'CUBICSPLINE':
        # https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html#interpolation-cubic
        p0 = values[k * 3 + 1]
        m0 = values[k * 3 + 2] * dt    # out-tangent of k
        p1 = values[(k + 1) * 3 + 1]
        m1 = values[(k + 1) * 3] * dt  # in-tangent of k+1
        u2 = u * u
        u3 = u2 * u
        out = (2 * u3 - 3 * u2 + 1) * p0 + (u3 - 2 * u2 + u) * m0 \
              + (-2 * u3 + 3 * u2) * p1 + (u3 - u2) * m1
        return out

    # LINEAR (default). For rotation paths we'll re-do as slerp at the call site.
    return (1.0 - u) * values[k] + u * values[k + 1]


def _slerp(q0: np.ndarray, q1: np.ndarray, t: float) -> np.ndarray:
    """Spherical-linear interpolation of two unit quaternions [x,y,z,w]."""
    dot = float(np.dot(q0, q1))
    if dot < 0.0:
        q1 = -q1
        dot = -dot
    if dot > 0.9995:
        # Linear fallback when quaternions are very close.
        out = (1.0 - t) * q0 + t * q1
        return out / max(np.linalg.norm(out), 1e-12)
    theta_0 = np.arccos(dot)
    sin_theta_0 = np.sin(theta_0)
    theta = theta_0 * t
    s0 = np.sin(theta_0 - theta) / sin_theta_0
    s1 = np.sin(theta) / sin_theta_0
    return s0 * q0 + s1 * q1


def _sample_rotation_at(times: np.ndarray, values: np.ndarray, interp: str, t: float) -> np.ndarray:
    """Sample a rotation channel; uses slerp for LINEAR, otherwise the generic path."""
    n = len(times)
    if n == 0:
        return np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float32)
    if t <= times[0]:
        if interp == 'CUBICSPLINE':
            q = values[1]
        else:
            q = values[0]
        return q / max(np.linalg.norm(q), 1e-12)
    if t >= times[-1]:
        if interp == 'CUBICSPLINE':
            q = values[(n - 1) * 3 + 1]
        else:
            q = values[-1]
        return q / max(np.linalg.norm(q), 1e-12)

    if interp == 'LINEAR':
        k = int(np.searchsorted(times, t, side='right')) - 1
        k = max(0, min(k, n - 2))
        t0, t1 = times[k], times[k + 1]
        dt = t1 - t0
        u = 0.0 if dt <= 0 else float((t - t0) / dt)
        return _slerp(values[k], values[k + 1], u)

    # STEP / CUBICSPLINE → renormalise the result.
    q = _sample_at(times, values, interp, t)
    return q / max(np.linalg.norm(q), 1e-12)


def derive_bind_pose_local(skin: GltfSkin) -> Dict[int, Tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Bind pose, parent-local (T, R, S) per joint, from rest node TRS."""
    return {
        i: (j.rest_translation, j.rest_rotation, j.rest_scale)
        for i, j in enumerate(skin.joints)
    }


def sample_clip(clip: GltfClip, skin: GltfSkin, fps: float
                ) -> Tuple[List[Dict[int, Tuple[np.ndarray, np.ndarray, np.ndarray]]], int]:
    """Sample a clip at uniform fps. Returns (frames, num_frames).

    ``frames[t][joint_idx]`` is ``(translation[3], rotation[4], scale[3])`` in
    parent-local space. Joints with no channels for a path use the bind pose.
    """
    if clip.duration <= 0.0:
        num_frames = 1
    else:
        num_frames = max(1, int(round(clip.duration * fps)) + 1)

    bind = derive_bind_pose_local(skin)
    frames: List[Dict[int, Tuple[np.ndarray, np.ndarray, np.ndarray]]] = []

    for f in range(num_frames):
        t = (f / fps) if fps > 0 else 0.0
        if t > clip.duration:
            t = clip.duration
        frame: Dict[int, Tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        for joint_idx, (rest_t, rest_r, rest_s) in bind.items():
            tch = clip.channels.get((joint_idx, 'translation'))
            rch = clip.channels.get((joint_idx, 'rotation'))
            sch = clip.channels.get((joint_idx, 'scale'))

            tr = _sample_at(*tch, t=t) if tch else rest_t
            rot = _sample_rotation_at(*rch, t=t) if rch else rest_r
            sc = _sample_at(*sch, t=t) if sch else rest_s
            frame[joint_idx] = (np.asarray(tr, dtype=np.float32),
                                np.asarray(rot, dtype=np.float32),
                                np.asarray(sc, dtype=np.float32))
        frames.append(frame)

    return frames, num_frames


# ---------------------------------------------------------------------------
# Lightweight UI helper (no buffer load)
# ---------------------------------------------------------------------------

def peek_animations(path: Path) -> Tuple[int, int]:
    """Return (skin_count, animation_count) for a glTF without loading the buffer.

    Used by the UI preview column. GLB is not supported here; returns (0, 0)
    for .glb to keep things simple — animation export of GLB isn't supported
    in the current pass anyway.
    """
    p = Path(path)
    if p.suffix.lower() != '.gltf':
        return (0, 0)
    try:
        import json
        with open(p, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception:
        return (0, 0)
    return (len(data.get('skins') or []), len(data.get('animations') or []))
