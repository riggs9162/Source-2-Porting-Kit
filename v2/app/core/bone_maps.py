"""Canonical Source 2 → Source 1 (ValveBiped) bone-name mapping.

Qt-free so it can back both the Bone Backport tool and the ragdoll
back-porting pipeline (`tools/ragdoll_physics.py`) without dragging in
PySide6. The ragdoll constraint preset in `core/ragdoll_presets.py` keys
off the ValveBiped names produced here, so both must agree on spelling.
"""

from __future__ import annotations

from typing import Dict

# Source 2 humanoid bone name -> ValveBiped (Source 1) bone name.
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
    # inside the palm and have no ValveBiped counterpart, so they stay
    # unmapped and fold into the hand. Mapping meta->FingerN shifted every
    # finger one bone toward the palm and dropped the fingertip entirely
    # (finger_*_2 went unmapped) - it put a 0.69u metacarpal stub onto a 3.91u
    # phalanx, a 5.6x length mismatch. Matches the community rename script.
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

    # Helper/deform bones. HL2 animation libraries do NOT contain these, so
    # they never become "mapped" (no donor-exact bind) — but naming them here
    # gives the kept helpers proper ValveBiped identities instead of z_*
    # fallbacks. Names follow the community Source 2 -> Source 1 rename script;
    # the *_TWIST leg/neck ones have no stock counterpart and keep a
    # descriptive name. These carry real skin weight (on the metrocop the arm
    # and leg twists dominate ~6000 verts), so dropping them collapses the
    # twist deformation into the parent bone.
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
    "pect_0_L":              "ValveBiped.Bip01_L_Pectoral",
    "pect_0_R":              "ValveBiped.Bip01_R_Pectoral",
    "neck_0_TWIST":          "ValveBiped.Bip01_Neck2",

    # Finger metacarpals. ValveBiped's own convention for these is Finger{N}m
    # hanging off the hand (seen in Valve/community HL2 rigs); the phalanges
    # stay Finger{N}/{N}1/{N}2. Index=1, middle=2, ring=3, pinky=4.
    "finger_index_meta_L":   "ValveBiped.Bip01_L_Finger1m",
    "finger_middle_meta_L":  "ValveBiped.Bip01_L_Finger2m",
    "finger_ring_meta_L":    "ValveBiped.Bip01_L_Finger3m",
    "finger_pinky_meta_L":   "ValveBiped.Bip01_L_Finger4m",
    "finger_index_meta_R":   "ValveBiped.Bip01_R_Finger1m",
    "finger_middle_meta_R":  "ValveBiped.Bip01_R_Finger2m",
    "finger_ring_meta_R":    "ValveBiped.Bip01_R_Finger3m",
    "finger_pinky_meta_R":   "ValveBiped.Bip01_R_Finger4m",
    "leg_upper_L_TWIST":     "ValveBiped.Bip01_L_Thigh_TWIST",
    "leg_upper_L_TWIST1":    "ValveBiped.Bip01_L_Thigh_TWIST1",
    "leg_upper_R_TWIST":     "ValveBiped.Bip01_R_Thigh_TWIST",
    "leg_upper_R_TWIST1":    "ValveBiped.Bip01_R_Thigh_TWIST1",

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

    # Headcrab root (HLA zombie). Without this the crab's whole subtree gets
    # pruned in ValveBiped-only builds and its vertices dead-end during fold
    # resolution (mini_root parents to the root-motion bone, not the head) —
    # the "floating crab legs" fragmentation. z_anm animates HC_Body_Bone
    # (parented to Spine4), so mapping it also gives an authentic crab wiggle.
    "mini_root":             "ValveBiped.HC_Body_Bone",
}


def valvebiped_name(source2_name: str) -> str:
    """Return the ValveBiped name for a Source 2 bone, or the input unchanged
    when there is no mapping (cloth/custom/already-Source-1 bones)."""
    return SOURCE2_TO_VALVEBIPED.get(source2_name, source2_name)
