"""Source 2 PHYS block (VPhysXAggregateData) parser + Rubikon-to-Source-1
constraint conversion.

Parses the text KV3 that `Source2Viewer-CLI -b PHYS` prints for a .vmdl_c
(the porting kit saves it as `<model>_phys.txt` next to the exported glTF)
into per-bone collision parts (spheres / capsules / hulls, bone-local) and
Rubikon joints, and converts those joints into `$jointconstrain` rows with
the verified semantics:

- the joint frame's Z column is the primary axis (hinge axis for revolute,
  cone/twist axis for conical) — NOT X;
- Rubikon limits are measured from frame1/frame2 ALIGNMENT while Source 1
  measures from the bind pose, so the bind offset is subtracted out;
- Source 1 .phy angles use the "clockwise" convention: x/y intervals are
  sign-mirrored when written to the QC, z passes through;
- intervals are extended to contain 0 (the bind pose) because vphysics
  hard-snaps any violation in a single step at spawn;
- QC friction is divided by 5 at compile (write 1.0 to get the canonical
  0.2 in the .phy).

HLA-era dump format (hull verts as `m_Vertices` float3 rows). All geometry
is in raw vmdl space (X-fwd, Z-up, inches) — verified identical to the
space VRF's glTF uses for mesh POSITIONs and the render skeleton bind pose,
so `part.bind_pose @ verts` drops straight into the kit's SMD pipeline.
Qt-free by design (usable from tools, CLIs and tests).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# tokenizer-lite: we only need arrays of floats, strings and a few scalars.
# The dump is regular machine-generated text, so anchored regex scanning is
# reliable; a full KV3 parser is not required.
# ---------------------------------------------------------------------------


@dataclass
class Shape:
    kind: str                      # 'sphere' | 'capsule' | 'hull'
    surface_idx: int
    # sphere: center(3), radius; capsule: c0(3), c1(3), radius; hull: verts (N,3)
    center: Optional[np.ndarray] = None
    center2: Optional[np.ndarray] = None
    radius: float = 0.0
    verts: Optional[np.ndarray] = None
    volume: float = 0.0            # hull: m_flVolume (bone-local units^3)


@dataclass
class Part:
    index: int
    bone: str                      # skeleton bone this part binds to
    bind_pose: np.ndarray          # 4x4 bone->model matrix
    shapes: List[Shape] = field(default_factory=list)
    mass: float = 0.0


@dataclass
class Joint:
    jtype: int                     # 3=revolute/hinge, 4=conical, 6=weld (observed)
    body1: int                     # parent part index
    body2: int                     # child part index
    frame1_pos: np.ndarray
    frame1_quat: np.ndarray        # x,y,z,w — joint frame in body1 local space
    frame2_pos: np.ndarray
    frame2_quat: np.ndarray        # joint frame in body2 (child) local space
    swing_enabled: bool = False
    swing_min: float = 0.0         # radians
    swing_max: float = 0.0
    twist_enabled: bool = False
    twist_min: float = 0.0
    twist_max: float = 0.0
    max_torque: float = 0.0        # angular motor-as-friction


@dataclass
class PhysData:
    bone_names: List[str]
    parts: List[Part]
    joints: List[Joint]
    surface_hashes: List[int]


_F = r"[-+0-9.eE]+"


def _floats(text: str) -> np.ndarray:
    return np.array([float(x) for x in re.findall(_F, text)], dtype=np.float64)


def _blocks(text: str, open_at: int) -> Tuple[str, int]:
    """Return the balanced {...} block starting at the first '{' at/after
    open_at, and the index just past its closing brace."""
    i = text.index("{", open_at)
    depth = 0
    for j in range(i, len(text)):
        c = text[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1], j + 1
    raise ValueError("unbalanced block")


def _array_block(text: str, key: str) -> str:
    """Return the balanced [...] contents for `key = [ ... ]` (top-level-ish)."""
    m = re.search(rf"{key}\s*=\s*\[", text)
    if not m:
        return ""
    i = m.end() - 1
    depth = 0
    for j in range(i, len(text)):
        c = text[j]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[i + 1:j]
    raise ValueError(f"unbalanced array for {key}")


def parse_phys_dump(text: str) -> PhysData:
    bone_names = re.findall(r'"([^"]*)"', _array_block(text, "m_boneNames"))
    bone_parents = [int(x) for x in re.findall(r"\d+", _array_block(text, "m_boneParents"))]
    surface_hashes = [int(x) for x in re.findall(r"\d+", _array_block(text, "m_surfacePropertyHashes"))]

    # bind poses: sequence of 3x4 row-major matrices
    bind_raw = _floats(_array_block(text, "m_bindPose"))
    n_parts_bp = len(bind_raw) // 12
    binds = []
    for i in range(n_parts_bp):
        m34 = bind_raw[i * 12:(i + 1) * 12].reshape(3, 4)
        m = np.eye(4)
        m[:3, :4] = m34
        binds.append(m)

    # ---- parts ----
    parts: List[Part] = []
    parts_arr = _array_block(text, "m_parts")
    pos = 0
    pidx = 0
    while True:
        m = re.search(r"m_flMass\s*=\s*(" + _F + r")", parts_arr[pos:])
        if not m:
            break
        part_start = pos + m.start()
        # part block spans until the next 'm_flMass' or end
        m2 = re.search(r"m_flMass\s*=\s*", parts_arr[part_start + 10:])
        part_end = part_start + 10 + m2.start() if m2 else len(parts_arr)
        chunk = parts_arr[part_start:part_end]

        bone_idx = bone_parents[pidx] if pidx < len(bone_parents) else pidx
        part = Part(
            index=pidx,
            bone=bone_names[bone_idx] if bone_idx < len(bone_names) else f"part{pidx}",
            bind_pose=binds[pidx] if pidx < len(binds) else np.eye(4),
            mass=float(m.group(1)),
        )

        # spheres
        for sm in re.finditer(r"m_Sphere\s*=\s*\{\s*m_vCenter\s*=\s*\[ (" + _F + r"), (" + _F + r"), (" + _F + r") \]\s*m_flRadius\s*=\s*(" + _F + r")", chunk):
            part.shapes.append(Shape(
                kind="sphere", surface_idx=0,
                center=np.array([float(sm.group(i)) for i in (1, 2, 3)]),
                radius=float(sm.group(4)),
            ))
        # capsules
        for cm in re.finditer(
            r"m_Capsule\s*=\s*\{\s*m_vCenter\s*=\s*\[\s*"
            r"\[ (" + _F + r"), (" + _F + r"), (" + _F + r") \],\s*"
            r"\[ (" + _F + r"), (" + _F + r"), (" + _F + r") \],\s*\]\s*"
            r"m_flRadius\s*=\s*(" + _F + r")", chunk):
            part.shapes.append(Shape(
                kind="capsule", surface_idx=0,
                center=np.array([float(cm.group(i)) for i in (1, 2, 3)]),
                center2=np.array([float(cm.group(i)) for i in (4, 5, 6)]),
                radius=float(cm.group(7)),
            ))
        # hulls: m_Vertices = [ [x,y,z], ... ]  (HLA era); volume from m_flVolume
        for hm in re.finditer(r"m_Hull\s*=\s*\{", chunk):
            hull_block, _ = _blocks(chunk, hm.start())
            vtxt = _array_block(hull_block, "m_Vertices")
            verts = _floats(vtxt).reshape(-1, 3) if vtxt else None
            volm = re.search(r"m_flVolume\s*=\s*(" + _F + r")", hull_block)
            part.shapes.append(Shape(
                kind="hull", surface_idx=0,
                verts=verts,
                volume=float(volm.group(1)) if volm else 0.0,
            ))

        parts.append(part)
        pidx += 1
        pos = part_end

    # ---- joints ----
    joints: List[Joint] = []
    jarr = _array_block(text, "m_joints")
    for jm in re.finditer(
        r"m_nType\s*=\s*(\d+)\s*"
        r"m_nBody1\s*=\s*(\d+)\s*"
        r"m_nBody2\s*=\s*(\d+)\s*"
        r"m_nFlags\s*=\s*\d+\s*"
        r"m_Frame1\s*=\s*\[\s*(" + _F + r"), (" + _F + r"), (" + _F + r"), (" + _F + r"),\s*(" + _F + r"), (" + _F + r"), (" + _F + r"),?\s*\]\s*"
        r"m_Frame2\s*=\s*\[\s*(" + _F + r"), (" + _F + r"), (" + _F + r"), (" + _F + r"),\s*(" + _F + r"), (" + _F + r"), (" + _F + r"),?\s*\]"
        r"(.*?)(?=\{\s*m_nType\s*=\s*|\Z)",
        jarr, re.S,
    ):
        rest = jm.group(18)

        def _flag(name: str) -> bool:
            m = re.search(rf"{name}\s*=\s*(\w+)", rest)
            return bool(m and m.group(1) == "true")

        def _range(name: str) -> Tuple[float, float]:
            m = re.search(rf"{name}\s*=\s*\{{\s*m_flMin\s*=\s*({_F})\s*m_flMax\s*=\s*({_F})", rest)
            return (float(m.group(1)), float(m.group(2))) if m else (0.0, 0.0)

        tq = re.search(r"m_flMaxTorque\s*=\s*(" + _F + r")", rest)
        smin, smax = _range("m_SwingLimit")
        tmin, tmax = _range("m_TwistLimit")
        joints.append(Joint(
            jtype=int(jm.group(1)),
            body1=int(jm.group(2)),
            body2=int(jm.group(3)),
            frame1_pos=np.array([float(jm.group(i)) for i in (4, 5, 6)]),
            frame1_quat=np.array([float(jm.group(i)) for i in (7, 8, 9, 10)]),
            frame2_pos=np.array([float(jm.group(i)) for i in (11, 12, 13)]),
            frame2_quat=np.array([float(jm.group(i)) for i in (14, 15, 16, 17)]),
            swing_enabled=_flag("m_bEnableSwingLimit"),
            swing_min=smin, swing_max=smax,
            twist_enabled=_flag("m_bEnableTwistLimit"),
            twist_min=tmin, twist_max=tmax,
            max_torque=float(tq.group(1)) if tq else 0.0,
        ))

    return PhysData(bone_names=bone_names, parts=parts, joints=joints,
                    surface_hashes=surface_hashes)


# ---------------------------------------------------------------------------
# shape -> point cloud (for convex hull SMD export), in PART-LOCAL space
# ---------------------------------------------------------------------------

def shape_points(shape: Shape, *, segments: int = 12, rings: int = 3) -> np.ndarray:
    if shape.kind == "hull" and shape.verts is not None:
        return shape.verts
    if shape.kind == "sphere":
        return _sphere_points(shape.center, shape.radius, segments)
    if shape.kind == "capsule":
        return _capsule_points(shape.center, shape.center2, shape.radius, segments, rings)
    return np.zeros((0, 3))


def _sphere_points(c: np.ndarray, r: float, segments: int) -> np.ndarray:
    pts = []
    for i in range(segments):
        th = math.pi * (i + 0.5) / segments
        for j in range(segments):
            ph = 2 * math.pi * j / segments
            pts.append(c + r * np.array([
                math.sin(th) * math.cos(ph),
                math.sin(th) * math.sin(ph),
                math.cos(th),
            ]))
    return np.array(pts)


def _capsule_points(c0: np.ndarray, c1: np.ndarray, r: float,
                    segments: int, rings: int) -> np.ndarray:
    """Cylinder rings along the axis + hemisphere caps."""
    axis = c1 - c0
    ln = np.linalg.norm(axis)
    if ln < 1e-9:
        return _sphere_points(c0, r, segments)
    axis = axis / ln
    # orthonormal basis
    up = np.array([0.0, 0.0, 1.0]) if abs(axis[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    u = np.cross(axis, up); u /= np.linalg.norm(u)
    v = np.cross(axis, u)
    pts = []
    for t in np.linspace(0.0, 1.0, max(2, rings)):
        center = c0 + (c1 - c0) * t
        for j in range(segments):
            ph = 2 * math.pi * j / segments
            pts.append(center + r * (math.cos(ph) * u + math.sin(ph) * v))
    # hemisphere caps (a few latitude rings each)
    for (cc, sgn) in ((c0, -1.0), (c1, 1.0)):
        for i in range(1, max(2, rings) + 1):
            th = (math.pi / 2) * i / (max(2, rings) + 1)
            rr = r * math.cos(th)
            h = r * math.sin(th)
            for j in range(segments):
                ph = 2 * math.pi * j / segments
                pts.append(cc + sgn * h * axis + rr * (math.cos(ph) * u + math.sin(ph) * v))
        pts.append(cc + sgn * r * axis)
    return np.array(pts)


# ---------------------------------------------------------------------------
# Rubikon joint -> $jointconstrain rows (v2 — verified semantics)
#
# Verified (nillerusr/source-engine + HLA PHYS numerical analysis):
# - Rubikon's primary axis (hinge axis, cone/twist axis) is the joint FRAME's
#   Z column. For conical joints frame Z ~ the bone axis; for revolute joints
#   frame Z is PERPENDICULAR to the bone (anatomical flexion).
# - Rubikon limits are measured from FRAME1/FRAME2 ALIGNMENT, not from the
#   bind pose. HLA's compiler symmetrizes one-sided authored ranges by
#   rotating m_Frame1 by the midpoint (elbow 0..90 deg -> +-45 with bind at
#   the interval end). Source 1 measures from BIND, so the bind offset (the
#   frame-relative rotation at bind) must be subtracted out.
# - Source 1 .phy angles are the "clockwise" convention: vphysics negate-
#   swaps every axis and the HL->IVP handedness map flips Z a second time,
#   so to realize a CCW interval [a,b]: x/y axes write [-b,-a], z writes [a,b].
# - QC $jointconstrain friction is divided by 5 at compile: write 1.0 to get
#   HL2's canonical 0.2 in the .phy.
# ---------------------------------------------------------------------------

def _quat_to_mat(q: np.ndarray) -> np.ndarray:
    x, y, z, w = q / np.linalg.norm(q)
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ])


def _twist_swing_decompose(D: np.ndarray) -> Tuple[float, np.ndarray]:
    """Decompose rotation matrix D into (twist angle about +Z, swing rotation
    applied after twist). Returns (twist_rad, swing_matrix)."""
    # twist quat about z: project rotation quat onto (z, w)
    q = _mat_to_quat_ts(D)
    tw = np.array([0.0, 0.0, q[2], q[3]])
    n = np.linalg.norm(tw)
    if n < 1e-9:
        twist = 0.0
        Rt = np.eye(3)
    else:
        tw = tw / n
        twist = 2.0 * math.atan2(tw[2], tw[3])
        if twist > math.pi:
            twist -= 2 * math.pi
        if twist < -math.pi:
            twist += 2 * math.pi
        Rt = _quat_to_mat(tw)
    swing = D @ Rt.T
    return twist, swing


def _mat_to_quat_ts(m: np.ndarray) -> np.ndarray:
    t = np.trace(m)
    if t > 0:
        s = math.sqrt(t + 1.0) * 2
        return np.array([(m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s,
                         (m[1, 0] - m[0, 1]) / s, 0.25 * s])
    i = int(np.argmax([m[0, 0], m[1, 1], m[2, 2]]))
    if i == 0:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        return np.array([0.25 * s, (m[0, 1] + m[1, 0]) / s,
                         (m[0, 2] + m[2, 0]) / s, (m[2, 1] - m[1, 2]) / s])
    if i == 1:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        return np.array([(m[0, 1] + m[1, 0]) / s, 0.25 * s,
                         (m[1, 2] + m[2, 1]) / s, (m[0, 2] - m[2, 0]) / s])
    s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
    return np.array([(m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s,
                     0.25 * s, (m[1, 0] - m[0, 1]) / s])


def joint_constraint_rows_v2(
    j: Joint,
    parent_bind_world: np.ndarray,     # 3x3 world rotation of PARENT bone at bind
    child_bind_world: np.ndarray,      # 3x3 world rotation of CHILD bone at bind
    *,
    widen: float = 1.0,
    min_span_deg: float = 2.0,
) -> List[Tuple[str, float, float]]:
    """Convert one Rubikon joint into CCW-correct, bind-referenced, per-axis
    `$jointconstrain` (axis, min_deg, max_deg) rows about the CHILD bone's
    local axes, including the clockwise x/y sign mapping for the QC.

    Weld (type 6) -> all axes fixed. Revolute (type 3): single axis = frame Z
    (perpendicular to the bone), others locked. Conical (type 4): twist about
    frame Z, symmetric cone on the perpendicular axes; disabled twist/swing
    means free for conical.
    """
    F1 = _quat_to_mat(j.frame1_quat)
    F2 = _quat_to_mat(j.frame2_quat)

    if j.jtype == 6:
        return [(a, 0.0, 0.0) for a in ("x", "y", "z")]

    # bind offset of frame2 relative to frame1, in joint-frame coordinates
    W1 = parent_bind_world @ F1
    W2 = child_bind_world @ F2
    D = W1.T @ W2
    twist0, swing0 = _twist_swing_decompose(D)

    # frame Z in child-local space = the primary axis
    a_child = F2[:, 2]

    # allowed TWIST interval measured from bind (CCW about a_child)
    if j.jtype == 3:
        if j.twist_enabled:
            lo_t = (j.twist_min - twist0) * widen
            hi_t = (j.twist_max - twist0) * widen
        else:
            lo_t, hi_t = -math.pi, math.pi
        cone = 0.0                     # revolute: swing locked
        cone_free = False
    else:                              # conical / ragdoll
        if j.twist_enabled:
            lo_t = (j.twist_min - twist0) * widen
            hi_t = (j.twist_max - twist0) * widen
        else:
            lo_t, hi_t = -2 * math.pi, 2 * math.pi
        if j.swing_enabled:
            cone = j.swing_max * widen
            cone_free = False
        else:
            cone = 0.0
            cone_free = True

    # swing bind offsets on the two perpendicular joint axes (usually ~0):
    # small-angle components of swing0 about joint X and Y
    sx = math.atan2(-swing0[1, 2], swing0[2, 2])   # about joint X
    sy = math.atan2(swing0[0, 2], swing0[2, 2])    # about joint Y

    deg = math.degrees
    # per-JOINT-axis CCW intervals, measured from bind
    joint_axis_limits = {
        2: (deg(lo_t), deg(hi_t)),                                   # Z: twist
        0: (-180.0, 180.0) if cone_free else (deg(-cone - sx), deg(cone - sx)),
        1: (-180.0, 180.0) if cone_free else (deg(-cone - sy), deg(cone - sy)),
    }
    if j.jtype == 3:
        joint_axis_limits[0] = (0.0, 0.0)
        joint_axis_limits[1] = (0.0, 0.0)

    # map each joint axis onto the nearest child bone axis
    names = ["x", "y", "z"]
    child_limits: Dict[int, Tuple[float, float]] = {}
    used = set()
    order = [2, 0, 1]                  # assign the primary axis first
    for ja in order:
        v = F2[:, ja]
        best, best_d = -1, -1.0
        for ax in range(3):
            if ax in used:
                continue
            d = abs(float(v[ax]))
            if d > best_d:
                best, best_d = ax, d
        used.add(best)
        lo, hi = joint_axis_limits[ja]
        if float(v[best]) < 0:         # axis points along -child_axis: mirror
            lo, hi = -hi, -lo
        child_limits[best] = (lo, hi)

    rows: List[Tuple[str, float, float]] = []
    for ax in range(3):
        lo, hi = child_limits[ax]
        lo, hi = min(lo, hi), max(lo, hi)
        # the interval MUST contain the bind pose (0): vphysics hard-snaps any
        # violation in one step at spawn. A bind outside the source range
        # happens when the target skeleton's bind pose differs from HLA's
        # (e.g. donor straight arms vs HLA bent) — extend the near edge to 0.
        if not (lo == 0.0 and hi == 0.0):
            lo, hi = min(lo, 0.0), max(hi, 0.0)
        # clamp to sane bounds; enforce a minimum span on non-fixed axes
        lo, hi = max(lo, -180.0), min(hi, 180.0)
        if not (lo == 0.0 and hi == 0.0) and hi - lo < min_span_deg:
            mid = (hi + lo) / 2
            lo, hi = mid - min_span_deg / 2, mid + min_span_deg / 2
        # clockwise QC convention: mirror x and y, z passes through
        if ax in (0, 1):
            lo, hi = -hi, -lo
        lo, hi = round(lo, 2), round(hi, 2)
        if lo == 0.0:
            lo = 0.0                   # normalize -0.0
        if hi == 0.0:
            hi = 0.0
        rows.append((names[ax], lo, hi))
    return rows


def joint_constraint_rows(j: Joint, *, widen: float = 1.0,
                          friction: float = 0.2,
                          min_span_deg: float = 4.0,
                          child_world_old: Optional[np.ndarray] = None,
                          child_world_new: Optional[np.ndarray] = None) -> List[Tuple[str, float, float]]:
    """Convert one Rubikon joint into per-axis (axis, min_deg, max_deg) rows
    for `$jointconstrain`, expressed about the CHILD bone's local axes.

    Convention: the joint frame's X axis (in child-local space, i.e. the
    first column of R(frame2_quat)) is the twist axis. Twist limits map to
    the child-local axis nearest that direction; swing (cone half-angle,
    m_flMax) applies symmetrically to the two remaining axes. Weld joints
    (type 6, or nothing enabled) return three 0..0 rows = fixed.

    When the target skeleton's child bone was REORIENTED (e.g. ValveBiped
    retarget), pass the child's old and new world rotation matrices; the
    twist direction is re-expressed in the new bone-local frame first.
    """
    R = _quat_to_mat(j.frame2_quat)
    twist_dir = R[:, 0]                      # joint X in child-local space
    if child_world_old is not None and child_world_new is not None:
        twist_world = child_world_old @ twist_dir
        twist_dir = child_world_new.T @ twist_world
    axes = np.eye(3)
    twist_axis = int(np.argmax(np.abs(twist_dir @ axes)))   # 0=x 1=y 2=z
    names = ["x", "y", "z"]
    sign = 1.0 if twist_dir[twist_axis] >= 0 else -1.0

    rows: List[Tuple[str, float, float]] = []
    deg = math.degrees

    if j.jtype == 6 or (not j.swing_enabled and not j.twist_enabled and j.jtype not in (3,)):
        return [(a, 0.0, 0.0) for a in names]

    for ax in range(3):
        if ax == twist_axis:
            if j.twist_enabled:
                lo, hi = deg(j.twist_min) * widen, deg(j.twist_max) * widen
                if sign < 0:
                    lo, hi = -hi, -lo
            elif j.jtype == 3:
                lo, hi = 0.0, 0.0        # hinge with no twist limit = locked? keep tiny
            else:
                lo, hi = -360.0, 360.0   # free twist (unusual)
        else:
            if j.jtype == 3:
                lo, hi = 0.0, 0.0        # revolute: no swing at all
            elif j.swing_enabled:
                half = deg(j.swing_max) * widen
                lo, hi = -half, half
            else:
                lo, hi = -360.0, 360.0
        # avoid degenerate sliver limits the solver jitters on
        if lo != 0.0 or hi != 0.0:
            if hi - lo < min_span_deg:
                mid = (hi + lo) / 2
                lo, hi = mid - min_span_deg / 2, mid + min_span_deg / 2
        rows.append((names[ax], round(lo, 2), round(hi, 2)))
    return rows
