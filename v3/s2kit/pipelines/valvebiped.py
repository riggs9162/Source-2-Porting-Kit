"""The Source 2 -> ValveBiped bone-name map and the canonical ValveBiped set.

Reference data, not policy. The map says what a Source 2 bone is *called* in
Source 1; :data:`VALVEBIPED_BONES` says which of those names an animation
library and every ValveBiped-aware addon actually look for. The retargeter in
:mod:`s2kit.pipelines.skeleton_retarget` reads both and decides from them what
to keep, what to fold and what to rename.

Two entries carry hard-won rationale and are commented where they sit: the
finger metacarpals (which deliberately do NOT become `Finger{N}`) and the
headcrab root.
"""

from __future__ import annotations

from typing import Dict

#: Source 2 humanoid bone name -> ValveBiped (Source 1) bone name.
SOURCE2_TO_VALVEBIPED: Dict[str, str] = {
    # Root
    "pelvis":        "ValveBiped.Bip01_Pelvis",

    # Spine
    "spine_0":       "ValveBiped.Bip01_Spine",
    "spine_1":       "ValveBiped.Bip01_Spine1",
    "spine_2":       "ValveBiped.Bip01_Spine2",
    "spine_3":       "ValveBiped.Bip01_Spine4",  # optional

    # Neck & Head
    "neck_0":        "ValveBiped.Bip01_Neck1",
    "head":          "ValveBiped.Bip01_Head1",

    # Left Arm
    "clavicle_L":            "ValveBiped.Bip01_L_Clavicle",
    "arm_upper_L":           "ValveBiped.Bip01_L_UpperArm",
    "arm_lower_L":           "ValveBiped.Bip01_L_Forearm",
    "hand_L":                "ValveBiped.Bip01_L_Hand",

    # Left Fingers
    "finger_thumb_0_L":      "ValveBiped.Bip01_L_Finger0",
    "finger_thumb_1_L":      "ValveBiped.Bip01_L_Finger01",
    "finger_thumb_2_L":      "ValveBiped.Bip01_L_Finger02",

    # ValveBiped's FingerN/N1/N2 are the three PHALANGES; the metacarpals live
    # inside the palm and have no ValveBiped counterpart. Mapping meta->FingerN
    # shifts every finger one bone toward the palm and drops the fingertip
    # entirely (finger_*_2 goes unmapped) - it puts a 0.69u metacarpal stub onto
    # a 3.91u phalanx, a 5.6x length mismatch. The metacarpals are named below
    # but deliberately land OUTSIDE the canonical set, so they survive as
    # weight-bearing leaves while their phalanges reparent to the hand.
    "finger_index_0_L":      "ValveBiped.Bip01_L_Finger1",
    "finger_index_1_L":      "ValveBiped.Bip01_L_Finger11",
    "finger_index_2_L":      "ValveBiped.Bip01_L_Finger12",

    "finger_middle_0_L":     "ValveBiped.Bip01_L_Finger2",
    "finger_middle_1_L":     "ValveBiped.Bip01_L_Finger21",
    "finger_middle_2_L":     "ValveBiped.Bip01_L_Finger22",

    "finger_ring_0_L":       "ValveBiped.Bip01_L_Finger3",
    "finger_ring_1_L":       "ValveBiped.Bip01_L_Finger31",
    "finger_ring_2_L":       "ValveBiped.Bip01_L_Finger32",

    "finger_pinky_0_L":      "ValveBiped.Bip01_L_Finger4",
    "finger_pinky_1_L":      "ValveBiped.Bip01_L_Finger41",
    "finger_pinky_2_L":      "ValveBiped.Bip01_L_Finger42",

    # Right Arm
    "clavicle_R":            "ValveBiped.Bip01_R_Clavicle",
    "arm_upper_R":           "ValveBiped.Bip01_R_UpperArm",
    "arm_lower_R":           "ValveBiped.Bip01_R_Forearm",
    "hand_R":                "ValveBiped.Bip01_R_Hand",

    # Right Fingers
    "finger_thumb_0_R":      "ValveBiped.Bip01_R_Finger0",
    "finger_thumb_1_R":      "ValveBiped.Bip01_R_Finger01",
    "finger_thumb_2_R":      "ValveBiped.Bip01_R_Finger02",

    "finger_index_0_R":      "ValveBiped.Bip01_R_Finger1",
    "finger_index_1_R":      "ValveBiped.Bip01_R_Finger11",
    "finger_index_2_R":      "ValveBiped.Bip01_R_Finger12",

    "finger_middle_0_R":     "ValveBiped.Bip01_R_Finger2",
    "finger_middle_1_R":     "ValveBiped.Bip01_R_Finger21",
    "finger_middle_2_R":     "ValveBiped.Bip01_R_Finger22",

    "finger_ring_0_R":       "ValveBiped.Bip01_R_Finger3",
    "finger_ring_1_R":       "ValveBiped.Bip01_R_Finger31",
    "finger_ring_2_R":       "ValveBiped.Bip01_R_Finger32",

    "finger_pinky_0_R":      "ValveBiped.Bip01_R_Finger4",
    "finger_pinky_1_R":      "ValveBiped.Bip01_R_Finger41",
    "finger_pinky_2_R":      "ValveBiped.Bip01_R_Finger42",

    # Helper/deform bones. HL2 animation libraries do NOT contain these, so they
    # are never canonical - but naming them here gives the kept helpers proper
    # identities instead of raw Source 2 names. They carry real skin weight (on
    # the metrocop the arm and leg twists dominate ~6000 verts), so folding them
    # away collapses the twist deformation into the parent bone.
    #
    # The names follow a hand-authored reference rig, because
    # :mod:`s2kit.pipelines.procedural_bones` keys its twist rules off them -
    # `Pect`, `Hip`, `Quad`, `Scap`, `Tibia`, `Soleus` rather than the longer
    # forms. A rule whose helper it cannot find is skipped, so a spelling that
    # drifts here silently costs the rig its twist deformation.
    "arm_upper_L_TWIST":     "ValveBiped.Bip01_L_Shoulder",
    "arm_upper_L_TWIST1":    "ValveBiped.Bip01_L_Bicep",
    "arm_lower_L_TWIST":     "ValveBiped.Bip01_L_Ulna",
    "arm_lower_L_TWIST1":    "ValveBiped.Bip01_L_Wrist",
    "arm_upper_R_TWIST":     "ValveBiped.Bip01_R_Shoulder",
    "arm_upper_R_TWIST1":    "ValveBiped.Bip01_R_Bicep",
    "arm_lower_R_TWIST":     "ValveBiped.Bip01_R_Ulna",
    "arm_lower_R_TWIST1":    "ValveBiped.Bip01_R_Wrist",
    "scap_0_L":              "ValveBiped.Bip01_L_Trapezius",
    "scap_0_R":              "ValveBiped.Bip01_R_Trapezius",
    "pect_0_L":              "ValveBiped.Bip01_L_Pect",
    "pect_0_R":              "ValveBiped.Bip01_R_Pect",
    "neck_0_TWIST":          "ValveBiped.Bip01_Neck2",

    # Finger metacarpals. ValveBiped's own convention for these is Finger{N}m
    # hanging off the hand. Index=1, middle=2, ring=3, pinky=4.
    "finger_index_meta_L":   "ValveBiped.Bip01_L_Finger1m",
    "finger_middle_meta_L":  "ValveBiped.Bip01_L_Finger2m",
    "finger_ring_meta_L":    "ValveBiped.Bip01_L_Finger3m",
    "finger_pinky_meta_L":   "ValveBiped.Bip01_L_Finger4m",
    "finger_index_meta_R":   "ValveBiped.Bip01_R_Finger1m",
    "finger_middle_meta_R":  "ValveBiped.Bip01_R_Finger2m",
    "finger_ring_meta_R":    "ValveBiped.Bip01_R_Finger3m",
    "finger_pinky_meta_R":   "ValveBiped.Bip01_R_Finger4m",

    "leg_upper_L_TWIST":     "ValveBiped.Bip01_L_Hip",
    "leg_upper_L_TWIST1":    "ValveBiped.Bip01_L_Quad",
    "leg_upper_R_TWIST":     "ValveBiped.Bip01_R_Hip",
    "leg_upper_R_TWIST1":    "ValveBiped.Bip01_R_Quad",

    # Lower-leg twists, when a rig has them. Same two-stage shape as
    # the forearm: the far one takes most of the ankle's roll.
    "leg_lower_L_TWIST":     "ValveBiped.Bip01_L_Tibia",
    "leg_lower_L_TWIST1":    "ValveBiped.Bip01_L_Soleus",
    "leg_lower_R_TWIST":     "ValveBiped.Bip01_R_Tibia",
    "leg_lower_R_TWIST1":    "ValveBiped.Bip01_R_Soleus",

    # Scapula. `scap_0_*` already maps to Trapezius above; the tip bone
    # is the scapula proper.
    "scap_1_L":              "ValveBiped.Bip01_L_Scap",
    "scap_1_R":              "ValveBiped.Bip01_R_Scap",

    # Left Leg
    "leg_upper_L":           "ValveBiped.Bip01_L_Thigh",
    "leg_lower_L":           "ValveBiped.Bip01_L_Calf",
    "ankle_L":               "ValveBiped.Bip01_L_Foot",
    "ball_L":                "ValveBiped.Bip01_L_Toe0",

    # Right Leg
    "leg_upper_R":           "ValveBiped.Bip01_R_Thigh",
    "leg_lower_R":           "ValveBiped.Bip01_R_Calf",
    "ankle_R":               "ValveBiped.Bip01_R_Foot",
    "ball_R":                "ValveBiped.Bip01_R_Toe0",

    # Headcrab root (HLA zombie). z_anm animates HC_Body_Bone parented to
    # Spine4, so mapping the crab root to it gives an authentic crab wiggle
    # instead of a subtree that dead-ends during fold resolution.
    "mini_root":             "ValveBiped.HC_Body_Bone",
}


#: The canonical ValveBiped skeleton - the 53 bone names GMod's animation set
#: and every ValveBiped-aware addon (JMod, WiltOS, ...) actually look for.
#: Source: https://gmodwiki.com/ValveBiped_Bones
#:
#: A Source 2 bone is only treated as *canonical* when its mapped name is in
#: here. Names outside it - Ulna, Wrist, Bicep, Shoulder, Trapezius, Pectoral,
#: Neck2, Thigh_TWIST, Finger1m - exist on some Valve models but are not part of
#: the contract, and inventing them makes a rig look ValveBiped-compliant to
#: addons that then fail to find what they expect.
VALVEBIPED_BONES: frozenset = frozenset(
    ["ValveBiped.Bip01_Pelvis", "ValveBiped.Bip01_Spine",
     "ValveBiped.Bip01_Spine1", "ValveBiped.Bip01_Spine2",
     "ValveBiped.Bip01_Spine4", "ValveBiped.Bip01_Neck1",
     "ValveBiped.Bip01_Head1"]
    + [f"ValveBiped.Bip01_{s}_{b}" for s in ("L", "R")
       for b in ("Clavicle", "UpperArm", "Forearm", "Hand",
                 "Thigh", "Calf", "Foot", "Toe0")]
    + [n for s in ("L", "R") for d in range(5)
       for n in (f"ValveBiped.Bip01_{s}_Finger{d}",
                 f"ValveBiped.Bip01_{s}_Finger{d}1",
                 f"ValveBiped.Bip01_{s}_Finger{d}2")]
)


def valvebiped_name(source2_name: str) -> str:
    """The ValveBiped name for a Source 2 bone, or the input unchanged.

    Unmapped names pass through: cloth, custom and already-Source-1 bones keep
    whatever they were called.
    """
    return SOURCE2_TO_VALVEBIPED.get(source2_name, source2_name)


def is_canonical(name: str) -> bool:
    """True when `name` is one of the ValveBiped names an anim library defines."""
    return name in VALVEBIPED_BONES


def auxiliary_name(source2_name: str) -> str:
    """The name a kept-but-non-canonical bone compiles under.

    A helper the map gives a proper ValveBiped name - a twist, a scapula, a
    metacarpal - **keeps it**. Two reasons. `$proceduralbones` resolves its
    helper and control bones by name, so a twist renamed out of the ValveBiped
    family cannot be driven and studiomdl aborts the compile looking for it.
    And a working reference rig does exactly this: its helpers are
    `ValveBiped.Bip01_L_Ulna` and only its bespoke jiggle bones sit in a
    lowercase family of their own.

    Everything else - cloth, jaw helpers, accessory mounts - is folded into one
    all-lowercase `z_` family. studiomdl stricmp-sorts `bonetablebyname` while
    the runtime searches it case-sensitively, so a rig mixing `backTooth_R` and
    `jaw` in among `ValveBiped.*` produces a table that cannot be binary
    searched (75 of 123 bones went unfindable on the zombie playermodel).
    `ValveBiped.*` and `z_*` order the same way under both comparisons, and
    within each family the case pattern is uniform, so the two agree.
    """
    mapped = SOURCE2_TO_VALVEBIPED.get(source2_name, source2_name)
    if mapped.startswith("ValveBiped."):
        return mapped
    return "z_" + mapped.lower()
