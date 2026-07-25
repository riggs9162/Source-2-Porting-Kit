"""Shared raw-GLB parsing + reference-SMD helpers for the porter pipelines.

Home of the accessor/euler/SMD primitives used by both
``utils/playermodel_port.py`` and ``utils/ragdoll_port.py`` — extracted here
so the playermodel path can call into the ragdoll collision builder without
an import cycle. Qt-free.
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import numpy as np
import trimesh

from ..tools.gltf_animation import GltfSkin, load_buffer_bytes

LogFn = Callable[[str, str], None]

_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}

_NPTYPES = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16,
            5125: np.uint32, 5126: np.float32}


def _acc(gltf: dict, buf: bytes, idx: int) -> np.ndarray:
    """Read a glTF accessor into an (count, ncomp) numpy array."""
    a = gltf["accessors"][idx]
    bv = gltf["bufferViews"][a["bufferView"]]
    off = bv.get("byteOffset", 0) + a.get("byteOffset", 0)
    n = a["count"]
    nc = _NCOMP[a["type"]]
    dt = np.dtype(_NPTYPES[a["componentType"]]).newbyteorder("<")
    item = dt.itemsize * nc
    stride = bv.get("byteStride", item)
    if stride == item:
        out = np.frombuffer(buf, dtype=dt, count=n * nc, offset=off)
        out = out.reshape(n, nc)
    else:
        # interleaved view: strided window then slice the leading components
        window = np.frombuffer(buf, dtype=np.uint8,
                               count=(n - 1) * stride + item, offset=off)
        out = np.lib.stride_tricks.as_strided(
            window[:].view(dt), shape=(n, nc), strides=(stride, dt.itemsize))
    return out.astype(np.float64 if dt.kind == "f" else np.int64)


def _mat_to_euler_xyz(R: np.ndarray) -> Tuple[float, float, float]:
    """SMD skeleton euler (radians) for R = Rz @ Ry @ Rx."""
    ry = math.asin(max(-1.0, min(1.0, -R[2, 0])))
    if abs(R[2, 0]) < 0.9999999:
        rx = math.atan2(R[2, 1], R[2, 2])
        rz = math.atan2(R[1, 0], R[0, 0])
    else:
        rx = math.atan2(-R[1, 2], R[1, 1])
        rz = 0.0
    return rx, ry, rz


def _q2m(q) -> np.ndarray:
    x, y, z, w = q
    n = float(np.sqrt(x * x + y * y + z * z + w * w)) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ])


def read_glb(path: Path) -> Tuple[dict, bytes]:
    """JSON chunk + resolved binary buffer of a .glb container."""
    raw = Path(path).read_bytes()
    jlen, _ = struct.unpack_from("<II", raw, 12)
    gltf = json.loads(raw[20:20 + jlen])
    # three-arg form required: the two-arg form returns None for
    # GLB-embedded buffers
    buf = load_buffer_bytes(gltf, Path(path).parent, glb_path=Path(path))
    if buf is None:
        raise ValueError(f"could not resolve the binary buffer of {path}")
    return gltf, buf


def is_s2v_native_gltf(gltf: dict) -> bool:
    """True if this glTF was produced by Source 2 Viewer (VRF).

    S2V stores POSITION values in vmdl/Source convention (X-forward, Z-up,
    inches) and attaches a 0.0254-scale + axis-swap matrix on a root node —
    the raw positions are already in the SMD target convention, so the
    porter pipelines use them as-is. (Mirrors the batch tool's
    ``_is_source2_viewer_export``, which lives in a Qt module.)
    """
    asset = gltf.get("asset") or {}
    if "Source 2 Viewer" in (asset.get("generator", "") or ""):
        return True
    for n in gltf.get("nodes") or []:
        m = n.get("matrix")
        if not m:
            continue
        # basis-vector lengths from a column-major 4x4
        for col in ((m[0], m[1], m[2]), (m[4], m[5], m[6]), (m[8], m[9], m[10])):
            mag = (col[0] ** 2 + col[1] ** 2 + col[2] ** 2) ** 0.5
            if 0.020 < mag < 0.030:
                return True
    return False


def relax_skin_weights(prims: List[dict], n_bones: int, passes: int = 3,
                       lam: float = 0.5) -> None:
    """Laplacian-relax the skin weights over the mesh graph, in place.

    Pruning to the donor's bone set folds many bones onto one ancestor, which
    leaves blocky weight transitions. Linear/dual-quaternion skinning only
    adapts a mesh to the donor's proportions ACROSS those transitions, so
    blocky weights concentrate every bit of that adaptation into a few narrow
    bands and it shows up as lumps. Spreading each vertex's weights toward its
    neighbours' distributes the same deformation smoothly.

    Vertices are welded by position first so the relaxation crosses UV/material
    seams (which split vertices but not the surface). Kept deliberately short —
    weights only travel `passes` edges, so joint definition survives.
    """
    if passes <= 0:
        return
    for p in prims:
        raw = p.get("indices")
        if raw is None:
            continue                    # no triangles: no graph to relax over
        V = np.asarray(p["vertices"], dtype=np.float64)
        idx = np.asarray(raw).reshape(-1, 3)
        vj, vw = p["vj"], p["vw"]
        nv = len(V)
        if nv == 0 or len(idx) == 0:
            continue
        _uniq, weld = np.unique(np.round(V, 4), axis=0, return_inverse=True)
        nu = _uniq.shape[0]

        W = np.zeros((nu, n_bones))
        for k in range(nv):
            for s in range(vj.shape[1]):
                w = float(vw[k][s])
                if w > 0.0:
                    W[weld[k], int(vj[k][s])] += w
        cnt = np.bincount(weld, minlength=nu).astype(float)
        cnt[cnt == 0] = 1.0
        W /= cnt[:, None]

        a = weld[idx]
        pairs = np.vstack([a[:, [0, 1]], a[:, [1, 2]], a[:, [2, 0]]])
        pairs = np.vstack([pairs, pairs[:, ::-1]])
        deg = np.bincount(pairs[:, 0], minlength=nu).astype(float)
        deg[deg == 0] = 1.0
        for _ in range(passes):
            acc = np.zeros_like(W)
            np.add.at(acc, pairs[:, 0], W[pairs[:, 1]])
            W = (1.0 - lam) * W + lam * (acc / deg[:, None])
            np.clip(W, 0.0, None, out=W)
            W /= np.maximum(W.sum(axis=1, keepdims=True), 1e-9)

        slots = vj.shape[1]
        for k in range(nv):
            row = W[weld[k]]
            top = np.argpartition(-row, min(slots, n_bones) - 1)[:slots]
            top = top[np.argsort(-row[top])]
            tot = float(row[top].sum()) or 1.0
            vj[k] = 0
            vw[k] = 0.0
            for s, b in enumerate(top):
                vj[k][s] = int(b)
                vw[k][s] = row[b] / tot


def _tokens(name: str) -> List[str]:
    """Lowercased ``_``/``-`` separated name parts, empties dropped."""
    return [t for t in name.lower().replace("-", "_").split("_") if t]


def _same_group(a: str, b: str) -> bool:
    """Do two mesh names look like variants of one interchangeable slot?

    True when one name is a token-prefix of the other (``tank`` /
    ``tank_destroyed``) or they first differ at a purely numeric token
    (``helmet_default_00`` / ``helmet_default_01``). Names that diverge on a
    word — ``combine_grunt_grenadeholster`` vs ``combine_helmet_default_00``
    — stay in separate slots.
    """
    ta, tb = _tokens(a), _tokens(b)
    k = 0
    while k < len(ta) and k < len(tb) and ta[k] == tb[k]:
        k += 1
    if k == len(ta) or k == len(tb):
        return True
    return ta[k].isdigit() and tb[k].isdigit()


def group_bodygroups(sets: List[Tuple[str, str, int]]
                     ) -> List[Tuple[str, List[str], bool]]:
    """Fold per-mesh SMDs into ``$bodygroup`` slots.

    A Source bodypart holding a single submodel is *always* drawn, so emitting
    one group per mesh makes every helmet variant and both the intact and
    destroyed backpack render simultaneously, stacked inside each other. S2
    character exports carry those alternates as sibling meshes, so variants of
    one slot are merged here and everything but the base body gains a
    ``blank`` — placed last, keeping body index 0 on the first real variant.

    Args:
        sets: ``(safe_name, smd_filename, vert_count)`` in mesh order.
    Returns:
        ``(group_name, [smd_filename, ...], add_blank)`` in mesh order.
    """
    parent = list(range(len(sets)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            if _same_group(sets[i][0], sets[j][0]):
                parent[find(i)] = find(j)

    order: List[int] = []
    members: dict = {}
    for i in range(len(sets)):
        r = find(i)
        if r not in members:
            members[r] = []
            order.append(r)
        members[r].append(i)

    # the heaviest slot is the character body itself — it must never blank out
    base = max(order, key=lambda r: sum(sets[i][2] for i in members[r]))
    groups: List[Tuple[str, List[str], bool]] = []
    for r in order:
        idxs = members[r]
        toks = _tokens(sets[idxs[0]][0])
        for i in idxs[1:]:
            other = _tokens(sets[i][0])
            toks = [t for k, t in enumerate(toks)
                    if k < len(other) and other[k] == t]
        name = "_".join(toks) or min((sets[i][0] for i in idxs), key=len)
        groups.append((name, [sets[i][1] for i in idxs], r != base))
    return groups


def bodygroup_lines(groups: List[Tuple[str, List[str], bool]]) -> List[str]:
    """Render ``group_bodygroups`` output as QC ``$bodygroup`` blocks."""
    out: List[str] = []
    for name, smds, add_blank in groups:
        body = "".join(f'    studio "{s}"\n' for s in smds)
        if add_blank:
            body += "    blank\n"
        out.append(f'$bodygroup "{name}"\n{{\n{body}}}')
    return out


def collect_skinned_prims(gltf: dict, buf: bytes,
                          log: LogFn) -> Tuple[List[Tuple[str, List[dict]]],
                                               List[dict]]:
    """Per-mesh-node skinned primitives as raw numpy arrays.

    Returns ``(mesh_sets, all_arrays)``: mesh_sets is ``[(name, prims)]``
    with the common name prefix stripped (VRF exports carry the full vmdl
    path in every mesh name), all_arrays is the flat prim list for in-place
    conform/re-skin. Verts stay raw — the conform moves them.
    """
    mesh_sets: List[Tuple[str, List[dict]]] = []
    all_arrays: List[dict] = []
    for ni, node in enumerate(gltf.get("nodes", [])):
        if "mesh" not in node or "skin" not in node:
            continue
        mesh = gltf["meshes"][node["mesh"]]
        name = mesh.get("name") or node.get("name") or f"mesh{ni}"
        prims = []
        for prim in mesh.get("primitives", []):
            A = prim["attributes"]
            if "JOINTS_0" not in A:
                continue
            V = _acc(gltf, buf, A["POSITION"]).astype(np.float64)
            N = (_acc(gltf, buf, A["NORMAL"]).astype(np.float64)
                 if "NORMAL" in A else None)
            UV = (_acc(gltf, buf, A["TEXCOORD_0"]).astype(np.float64)
                  if "TEXCOORD_0" in A else None)
            # glTF joints index the SKIN's joint list == parse_skin order
            J = _acc(gltf, buf, A["JOINTS_0"]).astype(np.int64)
            W = _acc(gltf, buf, A["WEIGHTS_0"]).astype(np.float64)
            if W.max() > 2.0:
                W = W / 255.0
            idx = _acc(gltf, buf, prim["indices"]).astype(np.int64)[:, 0]
            mi = prim.get("material")
            mat = (gltf.get("materials", [{}])[mi].get("name", "default")
                   if mi is not None else "default")
            pr = dict(vertices=V, normals=N, uv=UV, vj=J, vw=W,
                      indices=idx, material=mat)
            prims.append(pr)
            all_arrays.append(pr)
        if prims:
            mesh_sets.append((name, prims))
    if not mesh_sets:
        raise ValueError("no skinned meshes found in the GLB")
    if len(mesh_sets) > 1:
        names = [m for m, _ in mesh_sets]
        pre = names[0]
        for nm in names[1:]:
            while not nm.startswith(pre):
                pre = pre[:-1]
        if len(pre) > 3:
            mesh_sets = [(m[len(pre):] or m, p) for m, p in mesh_sets]
    log(f"{len(mesh_sets)} mesh set(s): {[m for m, _ in mesh_sets]}", "INFO")
    return mesh_sets, all_arrays


def concat_prims(all_arrays: List[dict]) -> Tuple[trimesh.Trimesh,
                                                  np.ndarray, np.ndarray]:
    """One concatenated render mesh with vj/vw rows aligned to its vertices.

    ``process=False`` keeps the vertex order untouched so row i of the
    returned vj/vw still describes vertex i.
    """
    verts, faces, vjs, vws = [], [], [], []
    off = 0
    for pr in all_arrays:
        V = pr["vertices"]
        verts.append(V)
        faces.append(pr["indices"].reshape(-1, 3) + off)
        vjs.append(pr["vj"])
        vws.append(pr["vw"])
        off += len(V)
    mesh = trimesh.Trimesh(vertices=np.vstack(verts),
                           faces=np.vstack(faces), process=False)
    return mesh, np.vstack(vjs), np.vstack(vws)


def load_glb_tri_mesh(path: Path) -> Optional[trimesh.Trimesh]:
    """Raw-POSITION triangle mesh of a GLB (for VRF ``_physics.glb`` files).

    Skips scene-transform baking on purpose: S2V physics exports carry raw
    vmdl-space inches in POSITION, which is exactly the space the porter
    pipelines work in.
    """
    try:
        gltf, buf = read_glb(path)
        verts, faces = [], []
        off = 0
        for mesh in gltf.get("meshes", []):
            for prim in mesh.get("primitives", []):
                A = prim.get("attributes", {})
                if "POSITION" not in A or "indices" not in prim:
                    continue
                V = _acc(gltf, buf, A["POSITION"]).astype(np.float64)
                idx = _acc(gltf, buf, prim["indices"]).astype(np.int64)[:, 0]
                verts.append(V)
                faces.append(idx.reshape(-1, 3) + off)
                off += len(V)
        if not verts:
            return None
        return trimesh.Trimesh(vertices=np.vstack(verts),
                               faces=np.vstack(faces), process=False)
    except Exception:
        return None


def _write_ref_smd(path: Path, skin: GltfSkin, prims: List[dict]):
    """Reference SMD: skin skeleton at rest + skinned triangles (<=3 links)."""
    lines = ["version 1", "nodes"]
    for i, j in enumerate(skin.joints):
        p = -1 if j.parent_joint_idx is None else j.parent_joint_idx
        lines.append(f'{i} "{j.name}" {p}')
    lines += ["end", "skeleton", "time 0"]
    for i, j in enumerate(skin.joints):
        rx, ry, rz = _mat_to_euler_xyz(_q2m(j.rest_rotation))
        t = j.rest_translation
        lines.append(f"{i} {t[0]:.6f} {t[1]:.6f} {t[2]:.6f} "
                     f"{rx:.6f} {ry:.6f} {rz:.6f}")
    lines += ["end", "triangles"]
    for pr in prims:
        V, N, UV, VJ, VW, idx, mat = (pr["vertices"], pr.get("normals"),
                                      pr.get("uv"), pr["vj"], pr["vw"],
                                      pr["indices"], pr["material"])
        for tri in range(0, len(idx), 3):
            lines.append(mat)
            for k in (0, 1, 2):
                v = int(idx[tri + k])
                links = [(int(VJ[v][s]), float(VW[v][s]))
                         for s in np.argsort(-VW[v])[:3] if VW[v][s] > 1e-4]
                if not links:
                    links = [(0, 1.0)]
                tot = sum(w for _b, w in links)
                links = [(b, w / tot) for b, w in links]
                n3 = N[v] if N is not None else (0.0, 0.0, 1.0)
                u2 = (UV[v][0], 1.0 - UV[v][1]) if UV is not None else (0.0, 0.0)
                ln = (f"{links[0][0]} {V[v][0]:.6f} {V[v][1]:.6f} {V[v][2]:.6f} "
                      f"{n3[0]:.6f} {n3[1]:.6f} {n3[2]:.6f} "
                      f"{u2[0]:.6f} {u2[1]:.6f} {len(links)}")
                for b, w in links:
                    ln += f" {b} {w:.6f}"
                lines.append(ln)
    lines.append("end")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
