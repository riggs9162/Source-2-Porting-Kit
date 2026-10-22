"""``$jigglebone`` support for ported Source 2 dangly chains.

Source 2 ships no jigglebone data — a character's ``m_pFeModel`` block carries a
cloth FEM solve (particles, distance rods, collision spheres) and its
``m_JiggleBones`` array is empty. So the parameters here are authored, matching
a hand-made GMod port of the same character.

The load-bearing part is the ORIENTATION. ``$jigglebone`` simulates a tip along
the bone's local **+Z**, while Source 2 bones run **+X** down the bone — every
dangly bone on the combine captain measured exactly 90 deg out. A jigglebone on
an unmodified ported bone swings sideways through the model, which no amount of
stiffness tuning fixes. The reference port solved it by re-authoring the bones
with ValveBiped-style orientations; ``reorient_for_jiggle`` does the same
automatically.

Re-orienting is safe for the mesh: it changes a bone's rest ROTATION while
keeping its world position, and every child's local transform is recomputed so
world positions are untouched. Skinning is relative to the bind, so the bind
pose renders identically — only what happens under simulation changes.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

# Valve's own numbers, from a working GMod port of the combine captain's
# equipment bags. Kept as the default because they are known to look right on
# this class of prop; nothing in the Source 2 data implies a better guess.
PRESETS: Dict[str, Dict[str, float]] = {
    "gear": dict(length=30.0, tip_mass=50.0, pitch_stiffness=50.0,
                 pitch_damping=7.0, yaw_stiffness=50.0, yaw_damping=7.0,
                 along_stiffness=100.0, along_damping=0.0,
                 angle_constraint=25.0),
    # longer and looser: antennae, aerials, thin whips
    "antenna": dict(length=40.0, tip_mass=20.0, pitch_stiffness=25.0,
                    pitch_damping=4.0, yaw_stiffness=25.0, yaw_damping=4.0,
                    along_stiffness=100.0, along_damping=0.0,
                    angle_constraint=45.0),
    # short and tight: pouches, buckles, armour tags
    "stiff": dict(length=15.0, tip_mass=80.0, pitch_stiffness=100.0,
                  pitch_damping=12.0, yaw_stiffness=100.0, yaw_damping=12.0,
                  along_stiffness=100.0, along_damping=0.0,
                  angle_constraint=12.0),
}

_UP = np.array([0.0, 0.0, 1.0])


def _q2m(q) -> np.ndarray:
    x, y, z, w = np.asarray(q, dtype=np.float64)
    n = float(np.sqrt(x * x + y * y + z * z + w * w)) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])


def _m2q(m: np.ndarray) -> np.ndarray:
    t = float(np.trace(m))
    if t > 0:
        s = np.sqrt(t + 1.0) * 2
        return np.array([(m[2, 1] - m[1, 2]) / s, (m[0, 2] - m[2, 0]) / s,
                         (m[1, 0] - m[0, 1]) / s, 0.25 * s])
    i = int(np.argmax(np.diag(m)))
    j, k = (i + 1) % 3, (i + 2) % 3
    s = np.sqrt(max(1e-12, 1.0 + m[i, i] - m[j, j] - m[k, k])) * 2
    q = np.empty(4)
    q[i] = 0.25 * s
    q[j] = (m[j, i] + m[i, j]) / s
    q[k] = (m[k, i] + m[i, k]) / s
    q[3] = (m[k, j] - m[j, k]) / s
    return q


def _worlds(skin) -> Dict[int, np.ndarray]:
    out: Dict[int, np.ndarray] = {}

    def fk(i: int) -> np.ndarray:
        if i in out:
            return out[i]
        j = skin.joints[i]
        m = np.eye(4)
        m[:3, :3] = _q2m(j.rest_rotation)
        m[:3, 3] = np.asarray(j.rest_translation, dtype=np.float64)
        p = j.parent_joint_idx
        out[i] = m if p is None else fk(p) @ m
        return out[i]

    for i in range(len(skin.joints)):
        fk(i)
    return out


def _frame_with_z_along(axis: np.ndarray) -> np.ndarray:
    """Orthonormal frame whose +Z is ``axis``, kept as upright as possible."""
    z = axis / (np.linalg.norm(axis) or 1.0)
    ref = _UP if abs(float(np.dot(z, _UP))) < 0.95 else np.array([1.0, 0.0, 0.0])
    x = np.cross(ref, z)
    x /= (np.linalg.norm(x) or 1.0)
    y = np.cross(z, x)
    return np.column_stack([x, y, z])


def select_chains(skin, patterns: Sequence[str]) -> List[int]:
    """Joint indices whose name matches any of ``patterns`` (substring, case
    insensitive). Empty patterns select nothing."""
    pats = [p.strip().lower() for p in patterns if p and p.strip()]
    if not pats:
        return []
    return [i for i, j in enumerate(skin.joints)
            if any(p in j.name.lower() for p in pats)]


def reorient_for_jiggle(skin, targets: Sequence[int],
                        on_log=None) -> List[str]:
    """Point each target bone's local +Z down its own length, in place.

    World positions of every bone are preserved: only rotations change, and
    children are re-expressed in their parent's new frame. Returns the names
    re-oriented, parents before children.
    """
    log = on_log or (lambda _m: None)
    if not targets:
        return []
    tset = set(targets)
    world = _worlds(skin)
    kids: Dict[int, List[int]] = {}
    for i, j in enumerate(skin.joints):
        if j.parent_joint_idx is not None:
            kids.setdefault(j.parent_joint_idx, []).append(i)

    # parents first, so a child re-orients against its parent's final frame
    order = sorted(tset, key=lambda i: _depth(skin, i))
    done: List[str] = []
    for i in order:
        here = world[i][:3, 3]
        ch = kids.get(i, [])
        if ch:
            axis = world[ch[0]][:3, 3] - here
        else:
            p = skin.joints[i].parent_joint_idx
            axis = here - world[p][:3, 3] if p is not None else _UP
        if float(np.linalg.norm(axis)) < 1e-6:
            continue
        new_w = np.eye(4)
        new_w[:3, :3] = _frame_with_z_along(axis)
        new_w[:3, 3] = here
        # children keep their world transforms
        child_w = {c: world[c].copy() for c in ch}
        world[i] = new_w
        for c, cw in child_w.items():
            loc = np.linalg.inv(new_w) @ cw
            skin.joints[c].rest_translation = loc[:3, 3].copy()
            skin.joints[c].rest_rotation = _m2q(loc[:3, :3])
        p = skin.joints[i].parent_joint_idx
        loc = new_w if p is None else np.linalg.inv(world[p]) @ new_w
        skin.joints[i].rest_translation = loc[:3, 3].copy()
        skin.joints[i].rest_rotation = _m2q(loc[:3, :3])
        done.append(skin.joints[i].name)
    if done:
        log(f"jiggle: re-oriented {len(done)} bone(s) so +Z runs down the bone")
    return done


def _depth(skin, i: int) -> int:
    d, p = 0, skin.joints[i].parent_joint_idx
    while p is not None:
        d += 1
        p = skin.joints[p].parent_joint_idx
    return d


def jigglebone_lines(names: Sequence[str],
                     preset: str = "gear") -> List[str]:
    """``$jigglebone`` blocks for ``names``, using a named parameter preset."""
    par = PRESETS.get(preset) or PRESETS["gear"]
    out: List[str] = []
    for n in names:
        out.append(f'$jigglebone "{n}"')
        out.append("{")
        out.append("\tis_flexible")
        out.append("\t{")
        out.append(f"\t\tlength {par['length']:g}")
        out.append(f"\t\ttip_mass {par['tip_mass']:g}")
        out.append(f"\t\tpitch_stiffness {par['pitch_stiffness']:g}")
        out.append(f"\t\tpitch_damping {par['pitch_damping']:g}")
        out.append(f"\t\tyaw_stiffness {par['yaw_stiffness']:g}")
        out.append(f"\t\tyaw_damping {par['yaw_damping']:g}")
        out.append("\t\tallow_length_flex")
        out.append(f"\t\talong_stiffness {par['along_stiffness']:g}")
        out.append(f"\t\talong_damping {par['along_damping']:g}")
        out.append(f"\t\tangle_constraint {par['angle_constraint']:g}")
        out.append("\t}")
        out.append("}")
    return out
