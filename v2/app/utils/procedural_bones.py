"""Procedural (helper) bone rules for conformed ValveBiped playermodels.

Source 2 rigs drive their twist bones with constraints that live in the
``.vmdl`` and are BAKED OUT by the glTF export — the exported bones are plain
children. Kept as-is they inherit their parent rigidly and never twist, so
they carry skin weight without providing the deformation they exist for
(measurably: keeping them changed a conform's mesh metrics not at all).

Source 1 expresses the same idea as ``$proceduralbones`` pointing at a ``.vrd``
of QuatInterp helpers::

    <helper> <bone> <parent> <control parent> <control bone>
    <basepos> x y z
    <trigger> tol  trigX trigY trigZ  dX dY dZ  posX posY posZ

Each ``<trigger>`` says "when the control bone reaches this orientation
relative to its control parent, give the helper this delta rotation". Sampling
the control's roll at three points and handing the helper a FRACTION of it
reproduces a twist bone: the one nearest the joint takes half, the one nearest
the end takes all of it.

The fractions are a documented convention, not measured from the
source rig — the S2 constraint data does not survive the glTF export.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

# (helper, parent, control parent, control bone, fraction of the control's roll)
# Ordered distal-fraction-first so the tables read like the anatomy.
_ARM: List[Tuple[str, str, str, str, float]] = []
for s in ("L", "R"):
    b = f"ValveBiped.Bip01_{s}_"
    _ARM += [
        # forearm twists follow the HAND's roll: mid-forearm half, wrist all
        (f"{b}Ulna", f"{b}Forearm", f"{b}Forearm", f"{b}Hand", 0.5),
        (f"{b}Wrist", f"{b}Forearm", f"{b}Forearm", f"{b}Hand", 1.0),
        # upper-arm twists follow the UPPER ARM against the clavicle
        (f"{b}Shoulder", f"{b}UpperArm", f"{b}Clavicle", f"{b}UpperArm", 1.0),
        (f"{b}Bicep", f"{b}UpperArm", f"{b}Clavicle", f"{b}UpperArm", 0.5),
        # thigh twists follow the thigh against the pelvis
        (f"{b}Thigh_TWIST", f"{b}Thigh", "ValveBiped.Bip01_Pelvis",
         f"{b}Thigh", 0.5),
        (f"{b}Thigh_TWIST1", f"{b}Thigh", "ValveBiped.Bip01_Pelvis",
         f"{b}Thigh", 1.0),
    ]

# The neck twist follows the head against the neck, half of it, so a head turn
# spreads over both neck segments instead of creasing at the collar.
HELPER_RULES: List[Tuple[str, str, str, str, float]] = _ARM + [
    ("ValveBiped.Bip01_Neck2", "ValveBiped.Bip01_Neck1",
     "ValveBiped.Bip01_Neck1", "ValveBiped.Bip01_Head1", 0.5),
]

# Trigger layout, copied in STRUCTURE from a Crowbar-written .vrd for a working
# GMod playermodel: three samples about the control bone's roll axis, the first
# sitting exactly on its REST orientation with a zero delta.
#
# Centring these on euler (0,0,0) instead - i.e. assuming the control bone rests
# at identity - is catastrophic and was shipped once. Real ValveBiped rests are
# 90-118 deg away from identity (measured: R_Shoulder rests at
# (93.94, -0.46, -33.62), and the reference file's first R_Bicep trigger is
# 93.937244 -0.463599 -33.622047 - the same numbers). Off-centre triggers make
# studiomdl interpolate at the wrong parameter and fire a large rotation in
# EVERY pose, which tears the forearms and shoulder open while the model is just
# standing there.
_SWING = 110.0                 # degrees either side of rest
_TOL_REST = 90.0
_TOL_SWING = 110.0


def _vrd_name(bone: str) -> str:
    """studiomdl resolves ``.vrd`` bone names with the XSI lookup, which
    compares against the bone name WITHOUT its DCC namespace. Writing the full
    ``ValveBiped.Bip01_L_Ulna`` makes every rule fail with
    ``quatinterpbone "..." unused`` and silently compiles a model with zero
    procedural bones - verified against a Crowbar-written .vrd, which uses the
    bare names and registers on the very same model."""
    return bone.rsplit(".", 1)[-1]


def _euler_xyz(R: np.ndarray) -> Tuple[float, float, float]:
    """Rotation matrix -> the XYZ euler triple the .vrd format stores.

    Validated against the reference file: run over a ValveBiped rig this
    reproduces its published trigger angles.
    """
    sy = -float(R[2, 0])
    if abs(sy) < 0.99999:
        e = (np.arctan2(R[2, 1], R[2, 2]), np.arcsin(sy),
             np.arctan2(R[1, 0], R[0, 0]))
    else:
        e = (np.arctan2(-R[1, 2], R[1, 1]), np.arcsin(sy), 0.0)
    return tuple(float(np.degrees(a)) for a in e)


def _mat_to_quat(R: np.ndarray) -> np.ndarray:
    t = float(np.trace(R))
    if t > 0:
        s = np.sqrt(t + 1.0) * 2
        return np.array([(R[2, 1] - R[1, 2]) / s, (R[0, 2] - R[2, 0]) / s,
                         (R[1, 0] - R[0, 1]) / s, 0.25 * s])
    i = int(np.argmax(np.diag(R)))
    j, k = (i + 1) % 3, (i + 2) % 3
    s = np.sqrt(max(1e-12, 1.0 + R[i, i] - R[j, j] - R[k, k])) * 2
    q = np.empty(4)
    q[i] = 0.25 * s
    q[j] = (R[j, i] + R[i, j]) / s
    q[k] = (R[k, i] + R[i, k]) / s
    q[3] = (R[k, j] - R[j, k]) / s
    return q


def _quat_to_mat(q: np.ndarray) -> np.ndarray:
    x, y, z, w = q / (np.linalg.norm(q) or 1.0)
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])


def _roll(deg: float) -> np.ndarray:
    """Rotation about the bone's own axis (local X, down the bone)."""
    a = np.radians(deg)
    return np.array([[1.0, 0.0, 0.0], [0.0, np.cos(a), -np.sin(a)],
                     [0.0, np.sin(a), np.cos(a)]])


def build_vrd(worlds: Dict[str, np.ndarray]) -> Tuple[str, List[str]]:
    """Return ``(vrd_text, helper_names)`` for whichever helpers exist.

    ``worlds`` maps bone name -> 4x4 model-space bind matrix.

    A ``<trigger>``'s delta angles are the helper's ABSOLUTE local rotation at
    that trigger, not an offset from its bind: the engine REPLACES the bone's
    local rotation with the interpolated value. So the rest sample must carry
    the helper's own bind local rotation, and the swings are composed onto it.
    Writing a plain 0/50/-50 there only works if the helper happens to rest at
    identity relative to its parent - which Valve's rigs do and ours do not
    (our L_Ulna sits ~178 deg from its forearm), so it silently flipped the
    forearms and shoulders in every pose.
    """
    out: List[str] = ["// Procedural helper bones generated by "
                      "Source 2 Porting Kit.",
                      "// Twist bones take a fraction of their control bone's "
                      "roll; without these",
                      "// rules a kept twist bone follows its parent rigidly "
                      "and does nothing.",
                      ""]
    used: List[str] = []
    for helper, parent, cparent, control, frac in HELPER_RULES:
        if not {helper, parent, cparent, control} <= worlds.keys():
            continue
        rest = _euler_xyz(np.linalg.inv(worlds[cparent])[:3, :3]
                          @ worlds[control][:3, :3])
        bind = (np.linalg.inv(worlds[parent])[:3, :3]
                @ worlds[helper][:3, :3])
        used.append(helper)
        out.append(f"<helper> {_vrd_name(helper)} {_vrd_name(parent)} "
                   f"{_vrd_name(cparent)} {_vrd_name(control)}")
        out.append("<basepos> 0 0 0")
        for tol, off in ((_TOL_REST, 0.0), (_TOL_SWING, _SWING),
                         (_TOL_SWING, -_SWING)):
            d = _euler_xyz(bind @ _roll(off * frac))
            out.append(
                f"<trigger> {tol:.6f} "
                f"{rest[0] + off:.6f} {rest[1]:.6f} {rest[2]:.6f} "
                f"{d[0]:.6f} {d[1]:.6f} {d[2]:.6f} "
                f"0.000000 0.000000 0.000000"
            )
        out.append("")
    return "\n".join(out) + "\n", used


def write_vrd(path: Path,
              worlds: Dict[str, np.ndarray]) -> Optional[List[str]]:
    """Write the ``.vrd`` next to the QC. Returns the helper names written, or
    None when the rig has no helpers worth driving (no file is created)."""
    text, used = build_vrd(worlds)
    if not used:
        return None
    Path(path).write_text(text, encoding="ascii")
    return used
