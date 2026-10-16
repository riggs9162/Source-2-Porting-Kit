"""Rebuild a Source 2 skeleton as the donor's ValveBiped skeleton.

The one rule this module enforces: **every bone the animation library also
defines takes the library's bind verbatim - position and rotation.** Not a
re-aimed version of it, not the model's own placement with the donor's
orientation: the library's, exactly.

Why nothing else survives
-------------------------
`CalcVirtualAnimation` initialises every shared bone from `pSeqbone[]`, the
sequence-owning model's bind, on every frame it decodes, then lays the
animation's channels over it. Whatever we wrote is discarded. Two attempts to
work around that were measured in-engine and both failed:

* aiming each bone down its own segment ("make the elbow bend *this* elbow")
  put the combine grunt's bind a median 32.8 degrees off the donor, 113 at the
  clavicle, and the compiled playermodel wore its head sideways;
* keeping the model's own bone positions and correcting them with a
  `$proportions` autoplay delta did nothing whatsoever - a **5x exaggerated**
  delta produced byte-identical output.

A decompiled 1:1 reference port of this same model settles it: its
`$definebone` values are m_anm's to six decimals and it ships no `$proportions`
sequence at all. The character adopts the library's build. The mesh is then
carried onto that skeleton by :mod:`s2kit.pipelines.mesh_conform`.

Three classes of bone
---------------------
=============  ==================================================  ==================================
Canonical      mapped name is in ``VALVEBIPED_BONES`` AND the       renamed, kept, takes the donor's
               donor defines it                                     bind verbatim
Auxiliary      carries skin weight but is not canonical             renamed ``z_*``, rides the rigid
               (twists, pectorals, scapulae, metacarpals)           delta of its canonical ancestor
Discardable    no weight and not canonical (IK targets, root        pruned; its rest folds into every
               motion, ``_end_`` tips)                              kept descendant
=============  ==================================================  ==================================

Every canonical bone reparents to its nearest *canonical* ancestor, which makes
the surviving ValveBiped hierarchy exactly the donor's with no special cases -
a finger's metacarpal stops being in the way because the phalanx skips past it
to the hand, while the metacarpal itself survives as a weight-bearing leaf.

Pruned bones fold **root-first down the whole chain**. Folding pairwise is a
real bug with teeth: an HLA pelvis sits under
``root_motion -> ground_root -> pelvis_translations`` carrying 120 degrees of
rest between them, and only composing the innermost link leaves the rest of the
chain silently dropped.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from s2kit.pipelines.gltf_animation import GltfJoint, GltfSkin
from s2kit.pipelines.mdl_bones import MdlBone
from s2kit.pipelines.porting import LogFn, check_cancelled
from s2kit.pipelines.valvebiped import (SOURCE2_TO_VALVEBIPED, auxiliary_name,
                                        is_canonical, valvebiped_name)

__all__ = ["BoneClass", "RetargetResult", "RigAlignment", "classify",
           "retarget_skeleton", "donor_world_matrices", "skin_world_matrices",
           "measure_alignment", "joint_weight_mass", "remap_vertex_joints",
           "weighted_joint_indices"]

#: GMod's engine allows 256, but its own studiomdl mangles models past 128 on
#: the v48 path and older tool code carries 128-entry arrays.
MAX_COMPILED_BONES = 128

#: How far a canonical bone's compiled bind may sit from the donor's before the
#: retarget refuses to return. Zero in principle; the tolerance only absorbs
#: quaternion round-tripping.
MAX_DONOR_DIVERGENCE_DEG = 0.01

#: A bone may not be carried further from where the source rig put it than its
#: own length. Past that it has left the flesh it drives, and every rotation it
#: takes swings that flesh on a lever longer than the bone.
#:
#: Measured, not chosen: on an HLA grunt against m_anm the ratio splits the
#: skeleton cleanly in two with nothing in between. Every body bone is under
#: 0.75 - hand 0.21, calf 0.04 - while 22 bones exceed 1.0 and all 22 are
#: finger bones, where HL2's hand is simply a different shape from a gloved
#: Combine one. Snapping those to the donor put the flesh 3.25 units from
#: bones 1.2 units long.
MAX_BONE_DISPLACEMENT = 1.0

#: How far a held-back bone may point away from its own child. It is aimed
#: there by construction, so this only catches a chain the aim could not solve.
MAX_AIM_ERROR_DEG = 1.0

#: Below this the donor's own aim is kept untouched, so a bone whose child did
#: not move stays bit-for-bit donor-exact instead of being re-derived.
MIN_AIM_CORRECTION_DEG = 1e-4

#: The bone every ValveBiped rig is rooted at; the yaw fit is measured relative
#: to it so a difference in where the model stands cannot skew the angle.
_HUMANOID_ROOT = "ValveBiped.Bip01_Pelvis"


class BoneClass:
    """What the retargeter decided to do with a bone."""
    CANONICAL = "canonical"
    AUXILIARY = "auxiliary"
    DISCARDABLE = "discardable"


@dataclass(frozen=True)
class RigAlignment:
    """The single similarity transform that carries the source rig onto the donor's.

    One turn about Z, one uniform scale, one offset - and that is the *whole* of
    what the mesh gets. It is deliberately not per-bone: see
    :mod:`s2kit.pipelines.mesh_conform` for the measurements that settled it.
    """
    yaw: float = 0.0
    scale: float = 1.0
    translation: np.ndarray = field(default_factory=lambda: np.zeros(3))

    @property
    def rotation(self) -> np.ndarray:
        return yaw_matrix(self.yaw)

    def apply(self, points: np.ndarray) -> np.ndarray:
        """Transform an (N, 3) array of positions."""
        return (np.asarray(points, dtype=np.float64)
                @ (self.scale * self.rotation).T) + self.translation

    def frame(self, world: np.ndarray) -> np.ndarray:
        """Transform a 4x4 world bind, keeping its rotation block orthonormal.

        Scale belongs to positions only: a Source bone is rigid, and folding a
        scale factor into its rotation is how a skeleton acquires shear.
        """
        out = np.eye(4)
        out[:3, :3] = self.rotation @ world[:3, :3]
        out[:3, 3] = self.scale * (self.rotation @ world[:3, 3]) + self.translation
        return out


@dataclass
class RetargetResult:
    """What :func:`retarget_skeleton` did, for reporting and for the QC."""
    skin: GltfSkin
    #: old joint index -> new joint index, for every kept bone.
    index_map: Dict[int, int] = field(default_factory=dict)
    #: old joint index -> new joint index of the bone its weights fold into.
    fold_map: Dict[int, int] = field(default_factory=dict)
    classes: Dict[str, str] = field(default_factory=dict)
    canonical: List[str] = field(default_factory=list)
    auxiliary: List[str] = field(default_factory=list)
    discarded: List[str] = field(default_factory=list)
    #: World bind of the DONOR, keyed by ValveBiped name - the proportions
    #: delta is computed against this.
    donor_world: Dict[str, np.ndarray] = field(default_factory=dict)
    unmatched_donor_bones: List[str] = field(default_factory=list)
    #: Degrees about Z the source rig is turned through to face the donor's way.
    alignment_yaw: float = 0.0
    #: The full source -> donor similarity transform. The mesh gets exactly this
    #: and nothing else.
    alignment: RigAlignment = field(default_factory=RigAlignment)
    #: Source-rig world bind, keyed by OLD joint index - the "from" side of the
    #: mesh alignment.
    old_world: Dict[int, np.ndarray] = field(default_factory=dict)
    #: Retargeted world bind, keyed by NEW joint index - the "to" side.
    new_world: Dict[int, np.ndarray] = field(default_factory=dict)
    #: Names of shared bones whose bind is not the donor's - either they kept
    #: the source rig's position, or they were re-aimed because the child they
    #: point at moved.
    own_position: List[str] = field(default_factory=list)
    #: Bone name -> the child it was re-aimed at. Only bones the retarget
    #: actually turned appear here; everything else keeps the donor's aim.
    aim_target: Dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Quaternion / matrix helpers
# ---------------------------------------------------------------------------

def _q2m(q: Sequence[float]) -> np.ndarray:
    """Quaternion [x, y, z, w] -> 3x3 rotation matrix."""
    x, y, z, w = (float(v) for v in q)
    n = x * x + y * y + z * z + w * w
    if n < 1e-12:
        return np.eye(3)
    s = 2.0 / n
    xs, ys, zs = x * s, y * s, z * s
    return np.array([
        [1.0 - (y * ys + z * zs), x * ys - w * zs, x * zs + w * ys],
        [x * ys + w * zs, 1.0 - (x * xs + z * zs), y * zs - w * xs],
        [x * zs - w * ys, y * zs + w * xs, 1.0 - (x * xs + y * ys)],
    ], dtype=np.float64)


def _m2q(m: np.ndarray) -> np.ndarray:
    """3x3 rotation matrix -> quaternion [x, y, z, w], w kept non-negative.

    Branching on the largest diagonal term rather than always using the trace:
    the trace form loses precision as it approaches zero, which is exactly the
    180-degree-ish region ValveBiped's mirrored right-side frames sit in.
    """
    m = np.asarray(m, dtype=np.float64)
    trace = m[0, 0] + m[1, 1] + m[2, 2]
    if trace > 0.0:
        s = 0.5 / np.sqrt(trace + 1.0)
        q = np.array([(m[2, 1] - m[1, 2]) * s, (m[0, 2] - m[2, 0]) * s,
                      (m[1, 0] - m[0, 1]) * s, 0.25 / s])
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = 2.0 * np.sqrt(max(1e-12, 1.0 + m[0, 0] - m[1, 1] - m[2, 2]))
        q = np.array([0.25 * s, (m[0, 1] + m[1, 0]) / s,
                      (m[0, 2] + m[2, 0]) / s, (m[2, 1] - m[1, 2]) / s])
    elif m[1, 1] > m[2, 2]:
        s = 2.0 * np.sqrt(max(1e-12, 1.0 + m[1, 1] - m[0, 0] - m[2, 2]))
        q = np.array([(m[0, 1] + m[1, 0]) / s, 0.25 * s,
                      (m[1, 2] + m[2, 1]) / s, (m[0, 2] - m[2, 0]) / s])
    else:
        s = 2.0 * np.sqrt(max(1e-12, 1.0 + m[2, 2] - m[0, 0] - m[1, 1]))
        q = np.array([(m[0, 2] + m[2, 0]) / s, (m[1, 2] + m[2, 1]) / s,
                      0.25 * s, (m[1, 0] - m[0, 1]) / s])
    q = q / max(np.linalg.norm(q), 1e-12)
    return -q if q[3] < 0.0 else q


def _trs(translation: Sequence[float], quat: Sequence[float]) -> np.ndarray:
    """(translation, quaternion) -> 4x4."""
    m = np.eye(4)
    m[:3, :3] = _q2m(quat)
    m[:3, 3] = np.asarray(translation, dtype=np.float64)
    return m


def yaw_matrix(degrees: float) -> np.ndarray:
    """A 3x3 rotation of `degrees` about Z."""
    c, s = np.cos(np.radians(degrees)), np.sin(np.radians(degrees))
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]],
                    dtype=np.float64)


def measure_alignment(ours: Dict[str, np.ndarray],
                      theirs: Dict[str, np.ndarray],
                      root: str,
                      weights: Optional[Dict[str, float]] = None
                      ) -> "RigAlignment":
    """The turn, scale and offset that best carry the source rig onto the donor's.

    Source 2 character rigs and ValveBiped disagree about which way the world's
    X axis points, so a Source 2 skeleton dropped into Source 1 sits a quarter
    turn off, and the two builds rarely stand the same height. Both fall out of
    one least-squares fit over every bone the rigs share.

    The turn is constrained to yaw. A free 3x3 fit will happily tilt a character
    a degree or two to shave residual off a mismatched shoulder, and a tilted
    bind is far more visible than the residual it buys. Measured rather than
    assumed: the closed form is solved first, then checked against the four
    cardinal turns, and whichever genuinely fits best wins. A rig that already
    matches scores yaw 0, scale 1 and nothing happens.

    `weights` says how much flesh each bone carries, from
    :func:`joint_weight_mass`. It matters: the two builds differ
    *non-uniformly* - this donor's calf is 6 per cent longer than the Source 2
    one while its upper arm is shorter - so a single scale is a compromise, and
    which compromise you get depends on who votes. Counting every bone equally
    lets the fingers decide and lands 6 per cent out; weighting by flesh puts
    it within 0.6 per cent of what a hand port chose.
    """
    names = [name for name in ours if name in theirs and name != root]
    if len(names) < 3:
        return RigAlignment()

    ours_pts = np.array([ours[name][:3, 3] for name in names], dtype=np.float64)
    theirs_pts = np.array([theirs[name][:3, 3] for name in names], dtype=np.float64)

    share = np.array([max(0.0, float((weights or {}).get(name, 1.0)))
                      for name in names], dtype=np.float64)
    if share.sum() <= 0.0:
        share = np.ones(len(names), dtype=np.float64)
    share /= share.sum()

    ours_mid = (ours_pts * share[:, None]).sum(axis=0)
    theirs_mid = (theirs_pts * share[:, None]).sum(axis=0)
    o, t = ours_pts - ours_mid, theirs_pts - theirs_mid

    cross = float(np.sum(share * (t[:, 1] * o[:, 0] - t[:, 0] * o[:, 1])))
    dot = float(np.sum(share * (t[:, 0] * o[:, 0] + t[:, 1] * o[:, 1])))
    solved = float(np.degrees(np.arctan2(-cross, dot)))

    def error(degrees: float) -> float:
        return float(np.sum(share * np.linalg.norm(
            t - o @ yaw_matrix(degrees).T, axis=1)))

    # The closed form is sign-ambiguous under the convention used here, so it
    # is a candidate rather than the answer.
    yaw = min((0.0, 90.0, -90.0, 180.0, solved, -solved), key=error)

    turned = o @ yaw_matrix(yaw).T
    spread = float(np.sum(share[:, None] * turned * turned))
    scale = (float(np.sum(share[:, None] * turned * t) / spread)
             if spread > 1e-12 else 1.0)
    if not np.isfinite(scale) or scale <= 0.0:
        scale = 1.0
    return RigAlignment(yaw, scale,
                        theirs_mid - scale * (yaw_matrix(yaw) @ ours_mid))


def measure_alignment_yaw(ours: Dict[str, np.ndarray],
                          theirs: Dict[str, np.ndarray],
                          root: str) -> float:
    """Just the turn from :func:`measure_alignment`, in degrees."""
    return measure_alignment(ours, theirs, root).yaw


# ---------------------------------------------------------------------------
# Forward kinematics
# ---------------------------------------------------------------------------

def skin_world_matrices(skin: GltfSkin) -> List[np.ndarray]:
    """World bind matrix per joint, from the skin's rest TRS.

    Scale is deliberately dropped: a bind frame carrying non-unit scale cannot
    be expressed as a Source bone, and every S2V export we handle is unscaled.
    """
    out: List[np.ndarray] = [np.eye(4) for _ in skin.joints]
    for index, joint in enumerate(skin.joints):
        local = _trs(joint.rest_translation, joint.rest_rotation)
        parent = joint.parent_joint_idx
        out[index] = local if parent is None else out[parent] @ local
    return out


def donor_world_matrices(bones: Sequence[MdlBone]) -> Dict[str, np.ndarray]:
    """World bind matrix per donor bone, keyed by name."""
    worlds: List[np.ndarray] = [np.eye(4) for _ in bones]
    for index, bone in enumerate(bones):
        local = _trs(bone.pos, bone.quat)
        worlds[index] = local if bone.parent < 0 else worlds[bone.parent] @ local
    return {bone.name: worlds[i] for i, bone in enumerate(bones)}


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify(skin: GltfSkin, donor_names: Sequence[str],
             weighted: Sequence[int]) -> Dict[int, str]:
    """Decide each joint's :class:`BoneClass`.

    `weighted` is the set of joint indices any vertex actually references. A
    bone is only worth keeping for deformation if something is skinned to it.
    """
    donor = set(donor_names)
    carries_weight = set(int(i) for i in weighted)
    out: Dict[int, str] = {}
    for index, joint in enumerate(skin.joints):
        mapped = valvebiped_name(joint.name)
        if is_canonical(mapped) and mapped in donor:
            out[index] = BoneClass.CANONICAL
        elif index in carries_weight:
            out[index] = BoneClass.AUXILIARY
        else:
            out[index] = BoneClass.DISCARDABLE
    return out


def _nearest_kept(index: int, skin: GltfSkin, kept: set) -> Optional[int]:
    """Walk up the ORIGINAL hierarchy to the first ancestor being kept."""
    parent = skin.joints[index].parent_joint_idx
    while parent is not None:
        if parent in kept:
            return parent
        parent = skin.joints[parent].parent_joint_idx
    return None


def _nearest_canonical(index: int, skin: GltfSkin,
                       classes: Dict[int, str]) -> Optional[int]:
    parent = skin.joints[index].parent_joint_idx
    while parent is not None:
        if classes.get(parent) == BoneClass.CANONICAL:
            return parent
        parent = skin.joints[parent].parent_joint_idx
    return None


# ---------------------------------------------------------------------------
# The retarget
# ---------------------------------------------------------------------------

def retarget_skeleton(
    skin: GltfSkin,
    donor_bones: Sequence[MdlBone],
    weighted_joints: Sequence[int],
    *,
    weight_mass: Optional[Dict[int, float]] = None,
    max_bones: int = MAX_COMPILED_BONES,
    align_yaw: Optional[float] = None,
    on_log: Optional[LogFn] = None,
    cancel: Optional[threading.Event] = None,
) -> RetargetResult:
    """Rename, prune, reparent and re-orient `skin` onto ValveBiped axes.

    Returns a :class:`RetargetResult` whose `skin` is a NEW object - the input
    is left alone so a caller can still measure against it, which is exactly
    what the world-position invariant check does.
    """
    log = on_log or (lambda _m, _l="INFO": None)
    donor_world = donor_world_matrices(donor_bones)
    donor_by_name = {bone.name: bone for bone in donor_bones}
    old_world = skin_world_matrices(skin)

    classes = classify(skin, list(donor_by_name), weighted_joints)
    check_cancelled(cancel)

    kept = {i for i, c in classes.items() if c != BoneClass.DISCARDABLE}
    if not kept:
        raise ValueError("no bone survived classification - is this a humanoid rig?")

    new_parent = _reparent(skin, classes, kept)
    # The cap reads the REPARENTED topology, not the source one. A finger's
    # metacarpal is a parent in the Source 2 hierarchy but a leaf once its
    # phalanx has skipped past it to the hand, and only the second view says
    # whether folding it away costs anything.
    if len(kept) > max_bones:
        kept = _enforce_bone_cap(skin, classes, kept, new_parent,
                                 weighted_joints, max_bones, log)
        new_parent = _reparent(skin, classes, kept)

    check_cancelled(cancel)
    canonical_world = {valvebiped_name(skin.joints[i].name): old_world[i]
                       for i in kept if classes[i] == BoneClass.CANONICAL}
    alignment = measure_alignment(
        canonical_world, donor_world, _HUMANOID_ROOT,
        {valvebiped_name(skin.joints[i].name): (weight_mass or {}).get(i, 0.0)
         for i in kept if classes[i] == BoneClass.CANONICAL}
        if weight_mass else None)
    if align_yaw is not None:
        alignment = RigAlignment(float(align_yaw), alignment.scale,
                                 alignment.translation)
    if abs(alignment.yaw) > 1e-6:
        log(f"source rig faces {alignment.yaw:+.1f} deg from the donor; turning "
            "it to match (the mesh turns with it)", "INFO")
    if abs(alignment.scale - 1.0) > 1e-4:
        log(f"source rig stands {1.0 / alignment.scale:.3f}x the donor's build; "
            f"scaling it x{alignment.scale:.4f} so the donor's animations, foot "
            "locks and attachments land where they were authored to", "INFO")
    align_yaw = alignment.yaw

    held_back: List[str] = []
    aim_target: Dict[str, str] = {}
    new_world = _new_world_matrices(skin, classes, kept, old_world, donor_world,
                                    new_parent, alignment, log, held_back,
                                    aim_target)

    result = _rebuild_skin(skin, classes, kept, new_parent, new_world)
    result.donor_world = donor_world
    result.own_position = held_back
    result.aim_target = aim_target
    result.alignment = alignment
    result.alignment_yaw = float(alignment.yaw)
    result.old_world = {old: old_world[old] for old in result.index_map}
    result.new_world = {result.index_map[old]: new_world[old]
                        for old in result.index_map}
    _assert_donor_exact(result, donor_world, log)
    result.classes = {skin.joints[i].name: c for i, c in classes.items()}
    result.canonical = [skin.joints[i].name for i in sorted(kept)
                        if classes[i] == BoneClass.CANONICAL]
    result.auxiliary = [skin.joints[i].name for i in sorted(kept)
                        if classes[i] == BoneClass.AUXILIARY]
    result.discarded = [j.name for i, j in enumerate(skin.joints) if i not in kept]
    matched = {valvebiped_name(skin.joints[i].name) for i in kept
               if classes[i] == BoneClass.CANONICAL}
    result.unmatched_donor_bones = sorted(
        name for name in donor_by_name
        if is_canonical(name) and name not in matched)

    # Weights of everything pruned have to land somewhere, and it must be the
    # same somewhere the geometry folded into - one fold rule, or a mesh
    # fragments along whichever ancestor each vertex happened to route through.
    for index in range(len(skin.joints)):
        if index in kept:
            continue
        target = _nearest_kept(index, skin, kept)
        if target is not None:
            result.fold_map[index] = result.index_map[target]

    log(f"retarget: {len(result.canonical)} canonical, "
        f"{len(result.auxiliary)} auxiliary, {len(result.discarded)} discarded",
        "INFO")
    if result.unmatched_donor_bones:
        log(f"donor bones with no Source 2 counterpart: "
            f"{', '.join(result.unmatched_donor_bones)}", "WARNING")
    return result


def weighted_joint_indices(arrays: Sequence[dict], threshold: float = 1e-4
                           ) -> List[int]:
    """Joint indices any vertex actually leans on, across every primitive."""
    seen: set = set()
    for prim in arrays:
        joints = np.asarray(prim["vj"])
        weights = np.asarray(prim["vw"], dtype=np.float64)
        seen.update(int(v) for v in np.unique(joints[weights > threshold]))
    return sorted(seen)


def joint_weight_mass(arrays: Sequence[dict], threshold: float = 1e-4
                      ) -> Dict[int, float]:
    """Total skin weight resting on each joint - how much flesh a bone carries.

    The alignment fit is weighted by this so the bones holding the model up
    decide the result. Left unweighted, thirty finger bones outvote the torso
    and the fitted scale comes out 6 per cent wrong.
    """
    mass: Dict[int, float] = {}
    for prim in arrays:
        joints = np.asarray(prim["vj"]).astype(np.int64)
        weights = np.asarray(prim["vw"], dtype=np.float64)
        for column in range(joints.shape[1]):
            real = weights[:, column] > threshold
            for joint, weight in zip(joints[real, column], weights[real, column]):
                mass[int(joint)] = mass.get(int(joint), 0.0) + float(weight)
    return mass


def remap_vertex_joints(result: RetargetResult, arrays: Sequence[dict],
                        *, on_log: Optional[LogFn] = None) -> int:
    """Point every vertex link at the retargeted skeleton, in place.

    Two things happen here, and the second is the whole reason the metacarpals
    are allowed to be auxiliary rather than special-cased:

    * links to pruned bones move to whatever that bone folded into, using the
      SAME fold map the geometry used - resolving it per-vertex instead is how
      one mesh ends up routed through several different ancestors and comes
      apart at the seams;
    * links that collide after folding are **summed**, not overwritten. A
      vertex weighted to both a finger metacarpal and the hand arrives with one
      link carrying the total, which is exactly the merge a hand rig needs.

    Returns how many vertices had links merged.
    """
    log = on_log or (lambda _m, _l="INFO": None)
    lut_size = max(max(result.index_map, default=-1),
                   max(result.fold_map, default=-1)) + 1
    lut = np.full(max(lut_size, 1), -1, dtype=np.int64)
    for old, new in result.index_map.items():
        lut[old] = new
    for old, new in result.fold_map.items():
        lut[old] = new

    merged_total = 0
    for prim in arrays:
        joints = np.asarray(prim["vj"]).astype(np.int64)
        weights = np.asarray(prim["vw"], dtype=np.float64).copy()
        if joints.size == 0:
            continue
        inside = (joints >= 0) & (joints < len(lut))
        targets = np.where(inside, lut[np.clip(joints, 0, len(lut) - 1)], -1)
        weights[targets < 0] = 0.0

        # Collapse duplicate targets within each vertex. Four links means six
        # pairs, so this stays a handful of vectorised passes.
        links = targets.shape[1]
        merged_rows = np.zeros(len(targets), dtype=bool)
        for a in range(links):
            for b in range(a + 1, links):
                same = (targets[:, a] == targets[:, b]) & (targets[:, a] >= 0)
                if not same.any():
                    continue
                weights[same, a] += weights[same, b]
                weights[same, b] = 0.0
                targets[same, b] = -1
                merged_rows |= same
        merged_total += int(merged_rows.sum())

        totals = weights.sum(axis=1)
        empty = totals <= 1e-9
        if empty.any():
            # Every link fell off the skeleton. Pin to the root rather than
            # emit a zero-weight vertex studiomdl would reject.
            targets[empty, 0] = result.skin.root_joint_idx
            weights[empty, 0] = 1.0
            totals = weights.sum(axis=1)
        weights /= totals[:, None]

        targets[targets < 0] = 0
        prim["vj"] = targets.astype(np.uint16)
        prim["vw"] = weights.astype(np.float32)

    if merged_total:
        log(f"weights: merged colliding links on {merged_total} vertex/vertices "
            "(folded bones summing into their kept ancestor)", "INFO")
    return merged_total


def _assert_donor_exact(result: RetargetResult,
                        donor_world: Dict[str, np.ndarray],
                        log: LogFn) -> None:
    """Every shared bone matches the donor's bind - unless it was held back.

    This is the check an earlier construction would have failed before it ever
    reached a compile. The failure it guards is silent everywhere upstream: the
    SMDs are well formed, the QC is valid, studiomdl is happy, and it only
    shows once a library animation plays.

    A held-back bone is exempt by design - holding it back is a decision
    :func:`_new_world_matrices` logs, not a drift - so it is checked against
    the property it was given instead: that it aims at its own child. That is
    the thing whose absence splayed the fingers, so it is worth asserting
    rather than assuming.
    """
    worlds = skin_world_matrices(result.skin)
    by_name = {joint.name: i for i, joint in enumerate(result.skin.joints)}

    worst_deg, worst_pos, where_deg, where_pos = 0.0, 0.0, "", ""
    worst_aim, where_aim = 0.0, ""
    for index, joint in enumerate(result.skin.joints):
        donor_here = donor_world.get(joint.name)
        if donor_here is None:
            continue
        if joint.name in result.own_position:
            target = result.aim_target.get(joint.name)
            if target is not None and target in by_name:
                child = by_name[target]
                axis = worlds[index][:3, :3] @ np.array([1.0, 0.0, 0.0])
                offset = worlds[child][:3, 3] - worlds[index][:3, 3]
                length = float(np.linalg.norm(offset))
                if length > 1e-6:
                    degrees = float(np.degrees(np.arccos(
                        np.clip(float(axis @ offset / length), -1.0, 1.0))))
                    if degrees > worst_aim:
                        worst_aim, where_aim = degrees, joint.name
            continue

        delta = worlds[index][:3, :3] @ donor_here[:3, :3].T
        degrees = float(np.degrees(np.arccos(
            np.clip((np.trace(delta) - 1.0) / 2.0, -1.0, 1.0))))
        if degrees > worst_deg:
            worst_deg, where_deg = degrees, joint.name
        drift = float(np.linalg.norm(worlds[index][:3, 3] - donor_here[:3, 3]))
        if drift > worst_pos:
            worst_pos, where_pos = drift, joint.name

    if worst_deg > MAX_DONOR_DIVERGENCE_DEG:
        raise ValueError(
            f"'{where_deg}' bind sits {worst_deg:.3f} deg off the donor's. "
            "Shared bones must take the animation library's rotation exactly, "
            "or its channels turn this rig somewhere else.")
    if worst_pos > 1e-4:
        raise ValueError(
            f"'{where_pos}' bind sits {worst_pos:.4f} units off the donor's "
            "without having been held back deliberately. A shared bone either "
            "matches the library or is recorded in `own_position`.")
    if worst_aim > MAX_AIM_ERROR_DEG:
        raise ValueError(
            f"'{where_aim}' was re-aimed but still points {worst_aim:.1f} deg "
            "away from the child it was aimed at. A bone whose child moved must "
            "follow it, or the flesh it drives splays.")

    held = len(result.own_position)
    log(f"bind is donor-exact (worst {worst_deg:.2e} deg, {worst_pos:.2e} units)"
        + (f"; {held} bone(s) keep the source's position, aimed down their own "
           f"segment to within {worst_aim:.2f} deg" if held else ""), "INFO")


def _reparent(skin: GltfSkin, classes: Dict[int, str],
              kept: set) -> Dict[int, Optional[int]]:
    """Hang every kept bone off the right survivor.

    Canonical bones climb to their nearest CANONICAL ancestor, which is what
    makes the surviving ValveBiped hierarchy match the donor's exactly with no
    per-bone special cases. Everything else just hangs off whatever survived
    above it.
    """
    new_parent: Dict[int, Optional[int]] = {}
    for index in sorted(kept):
        if classes[index] == BoneClass.CANONICAL:
            new_parent[index] = _nearest_canonical(index, skin, classes)
        else:
            new_parent[index] = _nearest_kept(index, skin, kept)
    return new_parent


def _enforce_bone_cap(skin: GltfSkin, classes: Dict[int, str], kept: set,
                      new_parent: Dict[int, Optional[int]],
                      weighted_joints: Sequence[int], max_bones: int,
                      log: LogFn) -> set:
    """Fold the least-used auxiliary leaves away until the rig fits.

    Canonical bones are never dropped - losing one costs an animation channel
    outright, while losing an auxiliary bone only costs deformation detail,
    which folds into its parent. Leaves are re-derived after each drop, since
    removing one can turn its parent into a leaf too.
    """
    usage: Dict[int, int] = {}
    for index in weighted_joints:
        usage[int(index)] = usage.get(int(index), 0) + 1

    parents = {new_parent.get(i) for i in kept}
    dropped: List[int] = []
    while len(kept) > max_bones:
        leaves = [i for i in kept
                  if classes[i] == BoneClass.AUXILIARY and i not in parents]
        if not leaves:
            log(f"cannot get under {max_bones} bones by folding auxiliary "
                f"leaves alone - {len(kept)} remain; studiomdl may mangle the "
                "compile", "WARNING")
            break
        leaves.sort(key=lambda i: (usage.get(i, 0), skin.joints[i].name))
        victim = leaves[0]
        kept.discard(victim)
        classes[victim] = BoneClass.DISCARDABLE
        dropped.append(victim)
        parents = {_nearest_kept(i, skin, kept)
                   if classes[i] != BoneClass.CANONICAL
                   else _nearest_canonical(i, skin, classes) for i in kept}

    if dropped:
        log(f"bone cap: folded {len(dropped)} auxiliary leaf/leaves away to "
            f"fit {max_bones} ({', '.join(skin.joints[i].name for i in dropped)})",
            "INFO")
    return kept


def _new_world_matrices(skin: GltfSkin, classes: Dict[int, str], kept: set,
                        old_world: List[np.ndarray],
                        donor_world: Dict[str, np.ndarray],
                        new_parent: Dict[int, Optional[int]],
                        alignment: RigAlignment,
                        on_log: Optional[LogFn] = None,
                        held_back: Optional[List[str]] = None,
                        aim_target: Optional[Dict[str, str]] = None
                        ) -> Dict[int, np.ndarray]:
    """New world bind per kept bone: the donor's, wherever it fits.

    A working `$includemodel` model wants its bind to be the library's - the
    runtime rebuilds shared bones from `pSeqbone[]` - and for the body that is
    free, because the two builds agree to well within a bone length.

    The fingers are where it stops being free. HL2's hand is a different shape
    from a gloved Combine one, so donor-exact finger bones land 3.25 units from
    flesh whose bones are 1.2 units long, and every finger then swings on a
    lever three times its own length. Those bones keep the source's position.

    Keeping the source's position is not enough on its own, and the half
    measure is worse than either whole one. Take the donor's *rotation* with
    the source's position and no bone aims at its own child any more - the
    knuckles came out 63 to 76 degrees off their own segment, which renders as
    a hand splayed flat like a webbed foot. Take the source's rotation instead
    and the right hand arrives mirrored, because Source 2 aims right-side bones
    down -X where ValveBiped aims every bone down +X.

    So a held-back bone gets the donor's rotation **swung onto its own
    segment**: the minimal turn carrying the donor's down-bone axis onto the
    direction this bone actually has to point. It then aims at its child, sits
    inside its flesh, and keeps ValveBiped's convention on both sides. A bone
    with no child left to aim at inherits its parent's swing.

    The decision runs root-first and is inherited, so a finger is never half
    one rig and half the other.
    """
    log = on_log or (lambda _m, _l="INFO": None)
    donor_of = {i: donor_world.get(valvebiped_name(skin.joints[i].name))
                if classes[i] == BoneClass.CANONICAL else None for i in kept}
    children: Dict[int, List[int]] = {}
    for index in kept:
        parent = new_parent.get(index)
        if parent is not None:
            children.setdefault(parent, []).append(index)

    order = sorted(kept, key=lambda i: _depth_via(i, new_parent))

    # Pass one: where each bone sits. The donor's, unless taking it would carry
    # the bone further than its own length from where the source rig put it.
    place: Dict[int, np.ndarray] = {}
    own: set = set()
    displaced: List[str] = []
    for index in order:
        aligned = alignment.frame(old_world[index])
        donor_here = donor_of[index]
        if donor_here is None:
            place[index] = aligned[:3, 3]
            own.add(index)
            continue
        parent = new_parent.get(index)
        segment = (float(np.linalg.norm(
            aligned[:3, 3] - alignment.frame(old_world[parent])[:3, 3]))
            if parent is not None else 0.0)
        drift = float(np.linalg.norm(donor_here[:3, 3] - aligned[:3, 3]))
        too_far = segment > 1e-6 and drift > segment * MAX_BONE_DISPLACEMENT
        if too_far or parent in own:
            place[index] = aligned[:3, 3]
            own.add(index)
            if too_far:
                displaced.append(skin.joints[index].name)
        else:
            place[index] = donor_here[:3, 3].copy()

    # Pass two: where each bone points. Always at its own child - a bone whose
    # child moved has to follow it, or it aims into empty space and the flesh
    # it drives splays. Where nothing moved the swing is the identity and the
    # bone stays donor-exact, which is the overwhelmingly common case.
    out: Dict[int, np.ndarray] = {}
    swings: Dict[int, np.ndarray] = {}
    aimed: List[int] = []
    for index in order:
        donor_here = donor_of[index]
        if donor_here is None:
            out[index] = alignment.frame(old_world[index])
            continue
        child = _aim_child(index, children, skin, classes, donor_world)
        swing = None
        if child is not None:
            want = place[child] - place[index]
            have = (donor_world[valvebiped_name(skin.joints[child].name)][:3, 3]
                    - donor_here[:3, 3])
            if _angle_between(want, have) > MIN_AIM_CORRECTION_DEG:
                swing = _swing(donor_here[:3, :3] @ np.array([1.0, 0.0, 0.0]), want)
                aimed.append(index)
                if aim_target is not None:
                    aim_target[valvebiped_name(skin.joints[index].name)] =                         valvebiped_name(skin.joints[child].name)
        if swing is None and index in own:
            swing = swings.get(new_parent.get(index))
        if swing is None:
            swing = np.eye(3)
        world = np.eye(4)
        world[:3, :3] = swing @ donor_here[:3, :3]
        world[:3, 3] = place[index]
        out[index] = world
        swings[index] = swing

    if held_back is not None:
        held_back.extend(valvebiped_name(skin.joints[i].name)
                         for i in sorted(set(own) | set(aimed))
                         if donor_of[i] is not None)
    if displaced:
        log(f"{len(displaced)} bone(s) sit further from the donor than their own "
            f"length, so they keep the source rig's position: "
            f"{_summarise(displaced)}", "INFO")
    if aimed:
        log(f"{len(aimed)} bone(s) re-aimed down their own segment because the "
            f"child they point at moved: "
            f"{_summarise([skin.joints[i].name for i in aimed])}", "INFO")
    return out


def _angle_between(a: np.ndarray, b: np.ndarray) -> float:
    """Degrees between two directions; 0 if either is degenerate."""
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return float(np.degrees(np.arccos(
        np.clip(float(np.dot(a, b)) / (na * nb), -1.0, 1.0))))


def _aim_child(index: int, children: Dict[int, List[int]], skin: GltfSkin,
               classes: Dict[int, str],
               donor_world: Dict[str, np.ndarray]) -> Optional[int]:
    """The child that defines this bone's axis: the one the DONOR aims at.

    A hand has five canonical children and no intrinsic way to pick one, so the
    donor decides - whichever child its own bone points most directly at. That
    is deterministic and needs no name matching.
    """
    here = donor_world.get(valvebiped_name(skin.joints[index].name))
    options = [c for c in children.get(index, [])
               if classes[c] == BoneClass.CANONICAL
               and valvebiped_name(skin.joints[c].name) in donor_world]
    if here is None or not options:
        return None
    axis = here[:3, :3] @ np.array([1.0, 0.0, 0.0])

    def alignment_with_axis(child: int) -> float:
        there = donor_world[valvebiped_name(skin.joints[child].name)]
        offset = there[:3, 3] - here[:3, 3]
        length = float(np.linalg.norm(offset))
        return float(axis @ offset / length) if length > 1e-9 else -2.0

    best = max(options, key=alignment_with_axis)
    return best if alignment_with_axis(best) > -1.5 else None


def _swing(from_dir: np.ndarray, to_dir: np.ndarray) -> np.ndarray:
    """Minimal rotation carrying one direction onto another."""
    a, b = np.asarray(from_dir, dtype=np.float64), np.asarray(to_dir, dtype=np.float64)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-12 or nb < 1e-12:
        return np.eye(3)
    a, b = a / na, b / nb
    axis = np.cross(a, b)
    sine = float(np.linalg.norm(axis))
    cosine = float(np.dot(a, b))
    if sine < 1e-12:
        if cosine > 0.0:
            return np.eye(3)
        # Antiparallel: any perpendicular axis is a valid half turn.
        seed = np.array([1.0, 0.0, 0.0]) if abs(a[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
        axis = np.cross(a, seed)
        axis /= np.linalg.norm(axis)
        sine, cosine = 0.0, -1.0
    else:
        axis = axis / sine
    x, y, z = axis
    cross = np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]])
    return np.eye(3) + sine * cross + (1.0 - cosine) * (cross @ cross)


def _depth_via(index: int, parent_of: Dict[int, Optional[int]]) -> int:
    """Depth under a given parent map, guarding against a malformed cycle."""
    depth, seen = 0, {index}
    node = parent_of.get(index)
    while node is not None and node not in seen:
        seen.add(node)
        depth += 1
        node = parent_of.get(node)
    return depth


def _summarise(names: Sequence[str], limit: int = 6) -> str:
    """A readable bone list that does not run to twenty entries in a log line."""
    stems = sorted({n.replace("ValveBiped.Bip01_", "") for n in names})
    if len(stems) <= limit:
        return ", ".join(stems)
    return f"{', '.join(stems[:limit])} and {len(stems) - limit} more"


def _rebuild_skin(skin: GltfSkin, classes: Dict[int, str], kept: set,
                  new_parent: Dict[int, Optional[int]],
                  new_world: Dict[int, np.ndarray]) -> RetargetResult:
    """Emit a new GltfSkin with parent-local TRS rebuilt from `new_world`.

    Ordering is parents-before-children so the joint list is a valid SMD node
    block as written, and so each local can be solved against a parent whose
    world matrix is already final.
    """
    order: List[int] = []
    emitted: set = set()
    pending = sorted(kept)
    while pending:
        deferred: List[int] = []
        for index in pending:
            parent = new_parent.get(index)
            if parent is None or parent in emitted:
                emitted.add(index)
                order.append(index)
            else:
                deferred.append(index)
        if len(deferred) == len(pending):
            names = ", ".join(skin.joints[i].name for i in deferred[:5])
            raise ValueError(
                f"bone hierarchy has a cycle involving: {names}"
                + (" ..." if len(deferred) > 5 else ""))
        pending = deferred

    index_map = {old: new for new, old in enumerate(order)}
    joints: List[GltfJoint] = []
    for old_index in order:
        source = skin.joints[old_index]
        world = new_world[old_index]
        parent = new_parent.get(old_index)
        if parent is None:
            local = world
        else:
            local = np.linalg.inv(new_world[parent]) @ world
        name = (valvebiped_name(source.name)
                if classes[old_index] == BoneClass.CANONICAL
                else auxiliary_name(source.name))
        joints.append(GltfJoint(
            name=name,
            gltf_node_idx=source.gltf_node_idx,
            parent_joint_idx=None if parent is None else index_map[parent],
            inverse_bind_matrix=np.linalg.inv(world),
            rest_translation=np.asarray(local[:3, 3], dtype=np.float64),
            rest_rotation=_m2q(local[:3, :3]),
            rest_scale=np.ones(3, dtype=np.float64),
        ))

    roots = [i for i, j in enumerate(joints) if j.parent_joint_idx is None]
    if len(roots) > 1:
        names = ", ".join(joints[i].name for i in roots)
        raise ValueError(f"retarget produced {len(roots)} roots ({names}); "
                         "a Source skeleton needs exactly one")
    return RetargetResult(
        skin=GltfSkin(joints=joints, root_joint_idx=roots[0],
                      skin_idx=skin.skin_idx),
        index_map=index_map,
    )
