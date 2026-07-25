"""Humanoid ragdoll presets for Source 1 back-porting.

Defines which ValveBiped bones get simulated as ragdoll parts and the
per-bone ``$jointconstrain`` limits that go in the ``$collisionjoints``
block. Qt-free; consumed by `tools/ragdoll_physics.py`.

Constraint philosophy (Phase 1): limits are CONSERVATIVE and SYMMETRIC
(±deg on each of x/y/z). Source's per-bone local axes depend on the
exported skeleton's bind orientation, so we cannot reliably know which
single axis is the "hinge" for a given model. Symmetric limits keyed to
each joint's role produce a stable, naturally-flopping ragdoll that never
explodes; exact anatomical hinges (e.g. a knee that only bends one way)
are a tuning pass — do it in HLMV's Physics tab → Generate QC, or refine
this table once a target skeleton is fixed.

Names must match the ValveBiped values in `core/bone_maps.py`.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# --- canonical ValveBiped bone names ---------------------------------------
PELVIS = "ValveBiped.Bip01_Pelvis"
SPINE = "ValveBiped.Bip01_Spine"
SPINE1 = "ValveBiped.Bip01_Spine1"
SPINE2 = "ValveBiped.Bip01_Spine2"
SPINE4 = "ValveBiped.Bip01_Spine4"
NECK1 = "ValveBiped.Bip01_Neck1"
HEAD1 = "ValveBiped.Bip01_Head1"
L_UPPERARM = "ValveBiped.Bip01_L_UpperArm"
L_FOREARM = "ValveBiped.Bip01_L_Forearm"
L_HAND = "ValveBiped.Bip01_L_Hand"
R_UPPERARM = "ValveBiped.Bip01_R_UpperArm"
R_FOREARM = "ValveBiped.Bip01_R_Forearm"
R_HAND = "ValveBiped.Bip01_R_Hand"
L_THIGH = "ValveBiped.Bip01_L_Thigh"
L_CALF = "ValveBiped.Bip01_L_Calf"
L_FOOT = "ValveBiped.Bip01_L_Foot"
R_THIGH = "ValveBiped.Bip01_R_Thigh"
R_CALF = "ValveBiped.Bip01_R_Calf"
R_FOOT = "ValveBiped.Bip01_R_Foot"

# Root of the ragdoll (the one bone with no $jointconstrain).
HUMANOID_ROOT_BONE = PELVIS

# Bones we simulate, in a stable order. Whichever of these are actually
# present in the target skeleton get a hull + constraint; absent ones are
# skipped. Well under the 32-part ragdoll limit. Deliberately excludes
# clavicles, fingers, toes and twist/helper bones — those fold into their
# nearest simulated ancestor's hull (see ragdoll_physics.assign_vertices).
HUMANOID_SIM_BONES: List[str] = [
    PELVIS,
    SPINE, SPINE1, SPINE2, SPINE4,
    NECK1, HEAD1,
    L_UPPERARM, L_FOREARM, L_HAND,
    R_UPPERARM, R_FOREARM, R_HAND,
    L_THIGH, L_CALF, L_FOOT,
    R_THIGH, R_CALF, R_FOOT,
]

# Per-bone symmetric angular freedom (degrees), keyed by ValveBiped name.
_SYMMETRIC_LIMIT_DEG: Dict[str, float] = {
    SPINE: 20.0, SPINE1: 18.0, SPINE2: 18.0, SPINE4: 15.0,
    NECK1: 22.0, HEAD1: 25.0,
    L_UPPERARM: 70.0, R_UPPERARM: 70.0,
    L_FOREARM: 60.0, R_FOREARM: 60.0,
    L_HAND: 35.0, R_HAND: 35.0,
    L_THIGH: 65.0, R_THIGH: 65.0,
    L_CALF: 70.0, R_CALF: 70.0,
    L_FOOT: 30.0, R_FOOT: 30.0,
}

# Fallback freedom for a simulated bone with no explicit entry above.
_DEFAULT_LIMIT_DEG = 25.0

# One $jointconstrain row: (axis, mode, min_deg, max_deg, friction).
JointConstraint = Tuple[str, str, float, float, float]


def constraints_for(bone_name: str) -> List[JointConstraint]:
    """Return the per-axis ``$jointconstrain`` rows for a simulated bone.

    The root bone (pelvis) returns ``[]`` — it is unconstrained. Every other
    bone gets three symmetric ``limit`` rows (x/y/z) sized to its role.
    """
    if bone_name == HUMANOID_ROOT_BONE:
        return []
    deg = _SYMMETRIC_LIMIT_DEG.get(bone_name, _DEFAULT_LIMIT_DEG)
    return [
        ("x", "limit", -deg, deg, 0.0),
        ("y", "limit", -deg, deg, 0.0),
        ("z", "limit", -deg, deg, 0.0),
    ]


# Global physics defaults for the $collisionjoints block. Valve's
# zombie-tuned globals (stock player/zombie_classic.phy) — the values the
# verified zombie_classic ragdoll port shipped with: high rotdamping so a
# freshly-spawned ragdoll settles instead of windmilling.
DEFAULT_INERTIA = 10.0
DEFAULT_DAMPING = 0.10
DEFAULT_ROTDAMPING = 3.00
# Typical Source humanoid ragdoll total mass (lbs-ish engine units) used when
# the caller has no better volume-derived figure.
DEFAULT_TOTAL_MASS = 90.0
# Default surfaceprop for a humanoid ragdoll.
DEFAULT_SURFACEPROP = "flesh"

# HL2-style distal mass bias ($jointmassbias): forearms/calves twice the
# default share, hands/feet/ankles four times, head twice — keeps extremities
# from fluttering. Keyed by ValveBiped name; look a part's bone up through
# `bone_maps.valvebiped_name` so native-skeleton ports hit the table too.
# From the verified zombie_classic ragdoll build.
DISTAL_MASS_BIAS: Dict[str, float] = {
    L_FOREARM: 2.0, R_FOREARM: 2.0,
    L_CALF: 2.0, R_CALF: 2.0,
    L_HAND: 4.0, R_HAND: 4.0,
    L_FOOT: 4.0, R_FOOT: 4.0,
    HEAD1: 2.0,
}

# HL2 hitgroups for $hbox lines (1 head, 2 chest, 3 stomach, 4/5 L/R arm,
# 6/7 L/R leg; 0 generic). Same ValveBiped-keyed lookup rule as
# DISTAL_MASS_BIAS. Mirrors the verified zombie_classic hitbox set, extended
# with the obvious spine/neck entries.
HITGROUP_OF: Dict[str, int] = {
    HEAD1: 1, NECK1: 1,
    SPINE2: 2, SPINE4: 2,
    PELVIS: 3, SPINE: 3, SPINE1: 3,
    L_UPPERARM: 4, L_FOREARM: 4, L_HAND: 4,
    R_UPPERARM: 5, R_FOREARM: 5, R_HAND: 5,
    L_THIGH: 6, L_CALF: 6, L_FOOT: 6,
    R_THIGH: 7, R_CALF: 7, R_FOOT: 7,
}


# --- donor constraints: Valve's hand-tuned humanoid values ------------------
# Extracted from GMod's stock player/zombie_classic.phy ragdollconstraint
# blocks (values also representative of the HL2 citizen/soldier family).
# Rows are (axis, min_deg, max_deg, phy_friction) about the CHILD bone's
# local axes, measured from the bind pose. VALID ONLY for skeletons whose
# bones carry true ValveBiped orientations (e.g. a pose-conformed retarget)
# — on a rename-only native skeleton the axes point elsewhere and these
# numbers would constrain the wrong motions.
# NOTE: studiomdl multiplies $jointconstrain friction by 0.2 when writing
# the .phy, so QC emission must divide phy_friction by 0.2.
DonorRow = Tuple[str, float, float, float]

VALVEBIPED_DONOR_CONSTRAINTS: Dict[str, List[DonorRow]] = {
    "ValveBiped.Bip01_Spine2":     [("x", -44, 44, .2), ("y", -39, 39, .2), ("z", -32, 50, .2)],
    "ValveBiped.Bip01_R_Clavicle": [("x", -30, 30, .2), ("y", -46, 46, .2), ("z", -26, 48, .2)],
    "ValveBiped.Bip01_L_Clavicle": [("x", -30, 30, .2), ("y", -46, 46, .2), ("z", -26, 48, .2)],
    "ValveBiped.Bip01_R_UpperArm": [("x", -39, 39, .2), ("y", -79, 95, .2), ("z", -93, 28, .2)],
    "ValveBiped.Bip01_L_UpperArm": [("x", -30, 30, .2), ("y", -95, 84, .2), ("z", -86, 26, .2)],
    "ValveBiped.Bip01_R_Forearm":  [("x", 0, 0, .2), ("y", 0, 0, .2), ("z", -149, 4, .2)],
    "ValveBiped.Bip01_L_Forearm":  [("x", 0, 0, .2), ("y", 0, 0, .2), ("z", -149, 4, .2)],
    "ValveBiped.Bip01_R_Hand":     [("x", -55, 55, .2), ("y", -55, 55, .2), ("z", -55, 55, .2)],
    "ValveBiped.Bip01_L_Hand":     [("x", -55, 55, .2), ("y", -55, 55, .2), ("z", -55, 55, .2)],
    "ValveBiped.Bip01_R_Thigh":    [("x", -12, 12, .2), ("y", -8, 75, .2), ("z", -97, 32, .2)],
    "ValveBiped.Bip01_L_Thigh":    [("x", -12, 12, .2), ("y", -73, 6, .2), ("z", -93, 30, .2)],
    "ValveBiped.Bip01_R_Calf":     [("x", 0, 0, .2), ("y", 0, 0, .2), ("z", -12, 126, .2)],
    "ValveBiped.Bip01_L_Calf":     [("x", 0, 0, .2), ("y", 0, 0, .2), ("z", -8, 126, .2)],
    "ValveBiped.Bip01_R_Foot":     [("x", 0, 0, 0.0), ("y", -25, 6, .2), ("z", -15, 35, .2)],
    "ValveBiped.Bip01_L_Foot":     [("x", 0, 0, 0.0), ("y", -19, 19, .2), ("z", -15, 35, .2)],
}


def donor_constraints_for(bone_name: str) -> Optional[List[DonorRow]]:
    """Valve's hand-tuned rows for a ValveBiped bone, or None if the bone has
    no donor entry (root, spine_0/neck/head and all non-humanoid bones)."""
    return VALVEBIPED_DONOR_CONSTRAINTS.get(bone_name)
