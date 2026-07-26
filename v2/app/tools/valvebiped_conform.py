"""Donor-exact ValveBiped skeleton conform for playermodel-grade ports.

Upgrades the rename-only ValveBiped retarget to the full conversion proven on
the HLA zombie_classic playermodel port (July 2026). Engine facts this
implements (all verified against bone_setup.cpp semantics, decoded v48 anim
data and a live GMod dedicated-server bone probe):

1. ``CalcVirtualAnimation`` initializes every bone an included sequence maps
   from the ANIM LIBRARY's bind pose (``pSeqbone``) — the target model's own
   bind locals for shared bones are discarded at runtime. The compiled bind
   must therefore EQUAL the donor's (pos AND quat, per shared bone); custom
   proportions survive only via the ``$proportions`` delta autoplay.
2. The runtime binary-searches the stricmp-sorted ``bonetablebyname`` when
   building the included-model bone map; mixed-case native names make that
   table invalid for case-sensitive searches, silently dropping bones. All
   unmapped bones are renamed ``z_`` + lowercase so every name sorts
   identically under both comparators.
3. Sequences of GMod anim libraries carry always-on pose-parameter-driven
   autolayers (z_anm: ``body_rot_z``, ``idle_layer_alt``); the model must
   declare the donor's ``$poseparameter`` list verbatim, in donor order.

The conform mutates the shared ``GltfSkin`` (names, parents, rest TRS) and
re-skins the render mesh via dual-quaternion blending, then hands back the
proportions/bind frames plus a ready-to-``$include`` QCI snippet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from ..core.bone_maps import SOURCE2_TO_VALVEBIPED
from ..utils.mdl_bones import read_mdl_bones, read_mdl_poseparams

LogFn = Callable[[str], None]

# Frame value tuple used by SmdAnimationExporter.write_animation.
_ONE = np.ones(3, dtype=np.float64)


@dataclass
class ConformResult:
    """Outputs the batch worker needs after a successful conform."""
    mapped: int
    renamed_native: int
    # {joint_idx: (t, r, s)} frames for SmdAnimationExporter.write_animation
    bind_frame: Dict[int, Tuple[np.ndarray, np.ndarray, np.ndarray]]
    prop_frame: Dict[int, Tuple[np.ndarray, np.ndarray, np.ndarray]]
    poseparam_lines: List[str] = field(default_factory=list)
    donor_basename: str = ""
    # {joint_idx: 3x3} the per-bone ANATOMICAL rotation the re-skin applied,
    # mapping a direction in the SOURCE model space to the conformed one.
    # The two spaces differ by a global yaw plus each bone's own axis
    # convention, so anything carried across (e.g. physics joint axes) must go
    # through this rather than assuming raw coordinates transfer.
    anat_rotations: Dict[int, np.ndarray] = field(default_factory=dict)


def _quat_to_mat(q: np.ndarray) -> np.ndarray:
    x, y, z, w = q
    n = float(np.sqrt(x * x + y * y + z * z + w * w)) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
        [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
        [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)],
    ], dtype=np.float64)


_UP = np.array([0.0, 0.0, 1.0])

# Remove the donor skeleton's backward mid-spine bow from the re-skinned mesh
# (see "SPINE STRAIGHTENING" in conform_to_donor). Set False to keep the
# donor's bow, i.e. to have the flesh follow the spine joints exactly.
_STRAIGHTEN_SPINE = True

# Bones whose flesh must be oriented by the character frame rather than by a
# child bone's direction — see the re-skin loop in conform_to_donor.
_TORSO_BONES = frozenset((
    "ValveBiped.Bip01_Pelvis", "ValveBiped.Bip01_Spine",
    "ValveBiped.Bip01_Spine1", "ValveBiped.Bip01_Spine2",
    "ValveBiped.Bip01_Spine4", "ValveBiped.Bip01_Neck1",
    "ValveBiped.Bip01_Head1",
))


def _mat_to_quat(m: np.ndarray) -> np.ndarray:
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


def _trs(t, q) -> np.ndarray:
    m = np.eye(4)
    m[:3, :3] = _quat_to_mat(np.asarray(q, dtype=np.float64))
    m[:3, 3] = np.asarray(t, dtype=np.float64)
    return m


def _quat_mul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return np.array([
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    ])


def native_rename(name: str) -> str:
    """Rename policy for bones outside the donor: ``z_`` + lowercase.

    ``z`` sorts after ``ValveBiped.*`` under BOTH strcmp and stricmp, and an
    all-lowercase family cannot flip order between them — keeping the
    compiled ``bonetablebyname`` valid for every lookup style (rule 2 above).
    """
    return "z_" + name.lower()


def conform_to_donor(
    skin,                              # GltfSkin (mutated in place)
    mesh,                              # trimesh.Trimesh (vertices re-skinned)
    vertex_joints: Optional[np.ndarray],
    vertex_weights: Optional[np.ndarray],
    donor_mdl: Path,
    on_log: Optional[LogFn] = None,
    extra_meshes: Optional[List[dict]] = None,
) -> ConformResult:
    """Conform ``skin`` to the donor's bind and re-skin ``mesh`` accordingly.

    Mapped bones (SOURCE2_TO_VALVEBIPED targets that exist in the donor)
    adopt the donor's hierarchy and EXACT bind locals; unmapped bones are
    renamed ``z_*`` and rigid-follow their parent's world delta. Vertices are
    transferred with dual-quaternion blending (linear blending collapses
    verts spanning high-delta joints like elbows into slivers). The HLA
    segment lengths are preserved separately in ``prop_frame`` for the
    ``$proportions`` delta autoplay.
    """
    log = on_log or (lambda _m: None)
    donor_bones = read_mdl_bones(donor_mdl)
    donor_idx = {b.name: i for i, b in enumerate(donor_bones)}
    donor_local: Dict[str, np.ndarray] = {}
    donor_world: Dict[str, np.ndarray] = {}
    donor_parent: Dict[str, Optional[str]] = {}
    for i, b in enumerate(donor_bones):
        lm = _trs(b.pos, b.quat)
        donor_local[b.name] = lm
        if b.parent < 0:
            donor_world[b.name] = lm
            donor_parent[b.name] = None
        else:
            pname = donor_bones[b.parent].name
            donor_world[b.name] = donor_world[pname] @ lm
            donor_parent[b.name] = pname

    joints = skin.joints
    n = len(joints)

    # -- names ---------------------------------------------------------------
    new_name: Dict[int, str] = {}
    mapped: Dict[int, str] = {}
    renamed_native = 0
    for i, j in enumerate(joints):
        target = SOURCE2_TO_VALVEBIPED.get(j.name)
        if target and target in donor_idx:
            new_name[i] = target
            mapped[i] = target
        else:
            new_name[i] = target or native_rename(j.name)
            renamed_native += 1
    if len(set(new_name.values())) != n:
        raise ValueError("bone rename collision after conform mapping")
    idx_of_new = {nm: i for i, nm in new_name.items()}

    # -- donor-order reparenting for mapped bones ----------------------------
    for i, target in mapped.items():
        dp = donor_parent.get(target)
        if dp is not None and dp in idx_of_new:
            joints[i].parent_joint_idx = idx_of_new[dp]

    # topo order over the (possibly re-linked) parent graph
    children: Dict[Optional[int], List[int]] = {}
    for i, j in enumerate(joints):
        children.setdefault(j.parent_joint_idx, []).append(i)
    order: List[int] = []
    stack = list(children.get(None, []))
    while stack:
        i = stack.pop()
        order.append(i)
        stack.extend(children.get(i, []))
    if len(order) != n:
        raise ValueError("skeleton is not a tree after donor reparenting")

    # -- old world (current HLA rest) ----------------------------------------
    old_world: Dict[int, np.ndarray] = {}
    for i in order:
        lm = _trs(joints[i].rest_translation, joints[i].rest_rotation)
        p = joints[i].parent_joint_idx
        old_world[i] = lm if p is None else old_world[p] @ lm

    # -- bind pass: donor-exact for mapped, rigid-follow for the rest --------
    bind_world: Dict[int, np.ndarray] = {}
    for i in order:
        p = joints[i].parent_joint_idx
        if i in mapped:
            bind_world[i] = donor_world[mapped[i]].copy()
        elif p is None:
            bind_world[i] = old_world[i].copy()
        else:
            bind_world[i] = (bind_world[p] @ np.linalg.inv(old_world[p])) @ old_world[i]

    # -- prop pass: donor orientation, HLA segment length --------------------
    prop_world: Dict[int, np.ndarray] = {}
    for i in order:
        p = joints[i].parent_joint_idx
        if i in mapped:
            target = mapped[i]
            R = donor_world[target][:3, :3]
            if p is None:
                t = old_world[i][:3, 3]
            else:
                dlp = donor_local[target][:3, 3]
                dl = float(np.linalg.norm(dlp))
                length = float(np.linalg.norm(
                    old_world[i][:3, 3] - old_world[p][:3, 3]))
                if dl > 1e-6:
                    off = dlp / dl * length
                else:
                    off = np.linalg.inv(prop_world[p][:3, :3]) @ (
                        old_world[i][:3, 3] - old_world[p][:3, 3])
                t = prop_world[p][:3, :3] @ off + prop_world[p][:3, 3]
            m = np.eye(4)
            m[:3, :3] = R
            m[:3, 3] = t
            prop_world[i] = m
        else:
            # unmapped: local identical to bind (delta zero) under prop parent
            if p is None:
                prop_world[i] = bind_world[i].copy()
            else:
                loc = np.linalg.inv(bind_world[p]) @ bind_world[i]
                prop_world[i] = prop_world[p] @ loc

    anat_rotations: Dict[int, np.ndarray] = {}

    # -- DQS re-skin ---------------------------------------------------------
    # This IS "rename, shift onto the donor skeleton, rotate": the per-bone
    # transform is a translation to the new joint head plus a rotation. What
    # matters is where that rotation comes from. It is ANATOMICAL — the
    # minimal (swing) rotation aligning the old child-segment direction to the
    # new one — NOT the full frame delta (bind @ inv(old)), because the frame
    # delta carries the two rigs' bone-axis conventions (ValveBiped right-side
    # frames sit ~180 deg from Source 2's) straight into the vertices and
    # swings them around their joint heads: thigh verts at chest height,
    # detached hands. Roll/axis-convention differences belong in the bind
    # relationship; they must never displace vertices.
    # Re-measured after the trace=-1 quaternion fix in playermodel_port, in
    # case that bug was what made the frame delta look bad — it was not. Frame
    # delta vs anatomical, worst edge: grunt 10.30u vs 3.96u, zombie 10.51u vs
    # 4.94u; tearing p99 0.863 vs 0.387 and 0.916 vs 0.523. Do not retry it.
    if ((mesh is not None and vertex_joints is not None
         and vertex_weights is not None) or extra_meshes):
        child_of: Dict[int, List[int]] = {}
        for i, j in enumerate(joints):
            if j.parent_joint_idx is not None:
                child_of.setdefault(j.parent_joint_idx, []).append(i)

        def _swing(a: np.ndarray, c: np.ndarray) -> np.ndarray:
            a = a / (np.linalg.norm(a) or 1.0)
            c = c / (np.linalg.norm(c) or 1.0)
            d = float(np.dot(a, c))
            if d > 1.0 - 1e-9:
                return np.eye(3)
            if d < -1.0 + 1e-9:
                p = np.cross(a, [1.0, 0.0, 0.0])
                if np.linalg.norm(p) < 1e-6:
                    p = np.cross(a, [0.0, 1.0, 0.0])
                p /= np.linalg.norm(p)
                return 2.0 * np.outer(p, p) - np.eye(3)
            v = np.cross(a, c)
            s = np.linalg.norm(v)
            K = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
            return np.eye(3) + K + K @ K * ((1 - d) / (s * s))

        def _axis_angle(axis: np.ndarray, ang: float) -> np.ndarray:
            axis = axis / (np.linalg.norm(axis) or 1.0)
            K = np.array([[0, -axis[2], axis[1]],
                          [axis[2], 0, -axis[0]],
                          [-axis[1], axis[0], 0]])
            return np.eye(3) + np.sin(ang) * K + (1 - np.cos(ang)) * (K @ K)

        # character-forward per rig, from toe direction (ball - ankle, z
        # flattened) - the unambiguous anatomical anchor both rigs share
        def _fwd(world: Dict[int, np.ndarray]) -> np.ndarray:
            vs = []
            for foot, toe in (("ValveBiped.Bip01_L_Foot", "ValveBiped.Bip01_L_Toe0"),
                              ("ValveBiped.Bip01_R_Foot", "ValveBiped.Bip01_R_Toe0")):
                fi, ti = idx_of_new.get(foot), idx_of_new.get(toe)
                if fi is None or ti is None:
                    continue
                v = world[ti][:3, 3] - world[fi][:3, 3]
                v[2] = 0.0
                if np.linalg.norm(v) > 1e-4:
                    vs.append(v / np.linalg.norm(v))
            if not vs:
                return np.array([1.0, 0.0, 0.0])
            v = np.mean(vs, axis=0)
            return v / (np.linalg.norm(v) or 1.0)

        fwd_old = _fwd(old_world)
        fwd_new = _fwd(bind_world)

        # SPINE STRAIGHTENING. HL2 animation skeletons bow backward through
        # the mid-spine — m_anm sits 2.2-2.5u behind its own pelvis->neck axis,
        # z_anm 2.9-3.2u — while Source 2 rigs are near-straight (+-0.9u).
        # Flesh follows bone, so conforming inherits that bow and the character
        # stands hunched; it shows on every model, which is why both the grunt
        # and the zombie have it. Restore each torso bone's ORIGINAL sagittal
        # offset from the axis, so the column keeps the source's shape while
        # the joints stay exactly where the donor put them (the bind is
        # untouched — only the flesh moves).
        # Anchored on the PELVIS alone. Normalising on a pelvis->neck (or
        # ->head) axis pins both of its endpoints to zero offset by
        # construction, so whichever joint sits at the far end can never be
        # corrected — that left the zombie's neck carrying 2.57u of bow.
        # Measuring each joint's forward offset from the pelvis corrects the
        # whole chain, including the overall lean (donor -8.1 deg vs source
        # -2.4 deg on the grunt).
        # FORWARD only along the chain. Correcting height as well was tried and
        # measured wrong against a hand-made port of the same character on the
        # same skeleton: it drags the whole chain down to the source's spine
        # length, leaving the neck flesh 3.35u and the head 2.84u below where
        # a human put them. The donor's extra spine length is wanted (that IS
        # the GMod build) - only its backward BOW is not.
        # The HEAD is the one exception: measured from the NECK rather than
        # the pelvis, because the donor's short neck (3.59u vs the source's
        # 5.53u) is what buries the head in the shoulders.
        # LATERAL is never touched - that is shoulder width, where the donor's
        # build is wanted. The clavicles were tried here too (they sit 4.14u
        # high) but the lowered shoulders were not wanted.
        straighten: Dict[int, np.ndarray] = {}
        _pi = idx_of_new.get("ValveBiped.Bip01_Pelvis")
        _ni = idx_of_new.get("ValveBiped.Bip01_Neck1")
        if _STRAIGHTEN_SPINE and _pi is not None:
            for i in range(n):
                if new_name[i] not in _TORSO_BONES or i == _pi:
                    continue
                so = old_world[i][:3, 3] - old_world[_pi][:3, 3]
                do = bind_world[i][:3, 3] - bind_world[_pi][:3, 3]
                d_fwd = float(np.dot(so, fwd_old)) - float(np.dot(do, fwd_new))
                vec = d_fwd * fwd_new
                if new_name[i] == "ValveBiped.Bip01_Head1" and _ni is not None:
                    sg = float(np.dot(
                        old_world[i][:3, 3] - old_world[_ni][:3, 3], _UP))
                    dg = float(np.dot(
                        bind_world[i][:3, 3] - bind_world[_ni][:3, 3], _UP))
                    vec = vec + (sg - dg) * _UP
                if float(np.linalg.norm(vec)) > 1e-4:
                    straighten[i] = vec
            if straighten:
                worst = max(np.linalg.norm(v) for v in straighten.values())
                log(f"spine straightening: {len(straighten)} bone(s), "
                    f"up to {worst:.2f}u of donor bow/lift removed")

        anat: List[np.ndarray] = [None] * n
        for i in order:
            op = old_world[i][:3, 3]
            npos = bind_world[i][:3, 3]
            par_i = joints[i].parent_joint_idx
            if (i not in mapped and par_i is not None
                    and anat[par_i] is not None):
                # A bone the donor has no counterpart for (twist, scapula,
                # pectoral, metacarpal...) has no anatomy of its own to align
                # to: its "segment" points at another helper, and deriving a
                # rotation from that is meaningless. It rides with the bone it
                # hangs off, exactly as its flesh should.
                m4 = np.eye(4)
                R = anat[par_i][:3, :3]
                m4[:3, :3] = R
                m4[:3, 3] = npos - R @ op + straighten.get(i, 0.0)
                anat[i] = m4
                anat_rotations[i] = R.copy()
                continue
            # A real bone takes its axis from real anatomy. Helper children
            # (twists, scapulae) lie along or beside the bone, so once they are
            # kept they would otherwise compete to define its orientation.
            cand = child_of.get(i, [])
            mapped_kids = [c for c in cand if c in mapped]
            if mapped_kids:
                cand = mapped_kids
            segs = []
            for c in cand:
                od = old_world[c][:3, 3] - op
                nd = bind_world[c][:3, 3] - npos
                lo = float(np.linalg.norm(od))
                ln = float(np.linalg.norm(nd))
                if lo > 1e-4 and ln > 1e-4:
                    segs.append((od / lo, nd / ln, min(lo, ln)))
            # A bone's flesh follows the chain it sits on, so its axis must
            # come from the child that CONTINUES that chain. Taking whichever
            # child was listed first made the combine grunt's upper spine
            # follow clavicle_L and its right hand follow the thumb - both
            # lateral stubs - which sheared the shoulder girdle apart; the
            # zombie's joint order happened to list neck_0 and the index
            # metacarpal first and looked fine. Pick the child whose direction
            # best continues the bone's own incoming segment.
            par = joints[i].parent_joint_idx
            if len(segs) > 1 and par is not None:
                inc = op - old_world[par][:3, 3]
                li = float(np.linalg.norm(inc))
                if li > 1e-6:
                    inc = inc / li
                    segs.sort(key=lambda s: -float(np.dot(s[0], inc)))
            # TORSO bones take their orientation from the CHARACTER FRAME, not
            # from a child bone. A limb bone's direction is anatomy - the arm
            # points where the arm points - but the spine chain's layout is a
            # rigging convention: ValveBiped's Pelvis->Spine leans 41 deg back
            # where a Source 2 rig's leans forward, and the head's only child
            # may be an attachment helper (the zombie's `mini_root`). Deriving
            # flesh rotation from those tipped the hips backwards and dropped
            # the head, on BOTH rigs.
            R = None
            if new_name[i] in _TORSO_BONES:
                F_old = np.column_stack(
                    [fwd_old, np.cross(_UP, fwd_old), _UP])
                F_new = np.column_stack(
                    [fwd_new, np.cross(_UP, fwd_new), _UP])
                R = F_new @ F_old.T
            elif segs:
                odn, axis, _w = segs[0]
                R = _swing(odn, axis)
                # Deterministic anatomical roll: twist about the new segment
                # axis so CHARACTER-FORWARD stays forward (kneecaps/palms/
                # creases face where the donor expects). This is also what
                # carries the global yaw between the two rigs onto bones whose
                # segment runs ALONG that yaw axis: a vertical spine segment
                # points the same way in both rigs, so its swing is ~0 and the
                # roll is the only thing that can supply the yaw the lateral
                # clavicle picks up automatically. Wrap-window heuristics fail
                # here - real inter-rig rolls cluster AT +-90 deg.
                # Near-vertical-forward segments (feet) anchor on character-UP.
                fo, fn_ = fwd_old, fwd_new
                if abs(float(np.dot(axis, fn_))) > 0.8:
                    fo = np.array([0.0, 0.0, 1.0])
                    fn_ = np.array([0.0, 0.0, 1.0])
                a0 = R @ (fo - odn * float(np.dot(fo, odn)))
                a0 -= axis * float(np.dot(a0, axis))
                a1 = fn_ - axis * float(np.dot(fn_, axis))
                if np.linalg.norm(a0) > 1e-6 and np.linalg.norm(a1) > 1e-6:
                    a0 /= np.linalg.norm(a0)
                    a1 /= np.linalg.norm(a1)
                    ang = float(np.arctan2(np.dot(np.cross(a0, a1), axis),
                                           np.dot(a0, a1)))
                    R = _axis_angle(axis, ang) @ R
            if R is None:
                p = joints[i].parent_joint_idx
                R = anat[p][:3, :3] if (p is not None and anat[p] is not None) \
                    else np.eye(3)
            m4 = np.eye(4)
            m4[:3, :3] = R
            m4[:3, 3] = npos - R @ op + straighten.get(i, 0.0)
            anat[i] = m4
            anat_rotations[i] = R.copy()

        # Place donor-less helper BONES with the same anatomical transform
        # their flesh gets. Their bind was provisionally rigid-followed from
        # the parent's frame delta, which is a DIFFERENT rotation to the
        # anatomical one — so the bone sat in one place and its verts moved to
        # another, displacing everything weighted to it. Mapped bones are
        # untouched, so the donor-exact bind is preserved.
        for i in order:
            p = joints[i].parent_joint_idx
            if i in mapped or p is None or anat[p] is None:
                continue
            bind_world[i] = anat[p] @ old_world[i]
            anat[i] = anat[p].copy()
            anat_rotations[i] = anat[p][:3, :3].copy()
            # prop_world tracks bind for unmapped bones (delta zero)
            prop_world[i] = prop_world[p] @ (
                np.linalg.inv(bind_world[p]) @ bind_world[i])

        dq: List[Tuple[np.ndarray, np.ndarray]] = []
        for i in range(n):
            delta = anat[i]
            r = _mat_to_quat(delta[:3, :3])
            r = r / np.linalg.norm(r)
            t3 = delta[:3, 3]
            d = 0.5 * _quat_mul(np.array([t3[0], t3[1], t3[2], 0.0]), r)
            dq.append((r, d))

        def _reskin(V: np.ndarray, N: Optional[np.ndarray],
                    vj: np.ndarray, vw: np.ndarray):
            """DQS-transform arrays in place (verts + optional normals)."""
            for v in range(len(V)):
                R4 = np.zeros(4)
                D4 = np.zeros(4)
                # anchor the hemisphere to the DOMINANT weight's quat —
                # anchoring to the first-listed weight lets adjacent verts
                # blend on opposite hemispheres and tear high-delta regions
                row_j = vj[v]
                row_w = vw[v]
                kmax = int(np.argmax(row_w))
                ref = dq[int(row_j[kmax])][0]
                R4t = D4t = None
                tot = 0.0
                for k in range(row_j.shape[0]):
                    w = float(row_w[k])
                    if w <= 1e-6:
                        continue
                    r, d = dq[int(row_j[k])]
                    if float(np.dot(ref, r)) < 0.0:
                        r, d = -r, -d
                    R4 += w * r
                    D4 += w * d
                    tot += w
                if tot <= 1e-6:
                    continue
                nr = float(np.linalg.norm(R4))
                if nr < 1e-9:
                    continue
                Rh = R4 / nr
                Dh = D4 / nr
                rot = _quat_to_mat(Rh)
                cx, cy, cz, cw = -Rh[0], -Rh[1], -Rh[2], Rh[3]
                dx, dy, dz, dw = Dh
                tvec = 2 * np.array([
                    dw * cx + dx * cw + dy * cz - dz * cy,
                    dw * cy - dx * cz + dy * cw + dz * cx,
                    dw * cz + dx * cy - dy * cx + dz * cw,
                ])
                V[v] = rot @ V[v] + tvec
                if N is not None:
                    nn = rot @ N[v]
                    ln = float(np.linalg.norm(nn))
                    if ln > 1e-9:
                        N[v] = nn / ln

        if mesh is not None:
            V = np.asarray(mesh.vertices, dtype=np.float64).copy()
            try:
                N = np.asarray(mesh.vertex_normals, dtype=np.float64).copy()
            except Exception:
                N = None
            _reskin(V, N, vertex_joints, vertex_weights)
            mesh.vertices = V
            if N is not None:
                try:
                    mesh.vertex_normals = N
                except Exception:
                    log("WARNING: could not write re-skinned normals back")

        # additional mesh arrays (bodygroup meshes) — dicts with keys
        # 'vertices' (N,3), optional 'normals' (N,3), 'vj' (N,K), 'vw' (N,K);
        # transformed IN PLACE
        for em in (extra_meshes or []):
            _reskin(np.asarray(em["vertices"]),
                    em.get("normals"), em["vj"], em["vw"])

    # -- write back names + rest TRS; build result frames --------------------
    # AFTER the re-skin: that block re-places donor-less helper bones onto the
    # anatomical transform their flesh uses, and the rest pose must reflect it.
    bind_frame: Dict[int, Tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    prop_frame: Dict[int, Tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for i in order:
        p = joints[i].parent_joint_idx
        bl = bind_world[i] if p is None else np.linalg.inv(bind_world[p]) @ bind_world[i]
        pl = prop_world[i] if p is None else np.linalg.inv(prop_world[p]) @ prop_world[i]
        joints[i].name = new_name[i]
        joints[i].rest_translation = bl[:3, 3].copy()
        joints[i].rest_rotation = _mat_to_quat(bl[:3, :3])
        joints[i].rest_scale = _ONE.copy()
        bind_frame[i] = (bl[:3, 3].copy(), _mat_to_quat(bl[:3, :3]), _ONE.copy())
        prop_frame[i] = (pl[:3, 3].copy(), _mat_to_quat(pl[:3, :3]), _ONE.copy())

    # -- donor pose parameters ----------------------------------------------
    pp_lines: List[str] = []
    for p in read_mdl_poseparams(donor_mdl):
        loop = f' loop {p.loop:.2f}' if p.loop else ''
        pp_lines.append(
            f'$poseparameter "{p.name}" {p.start:.6f} {p.end:.6f}{loop}')

    # Split the report: a bone the donor lacks still gets a proper ValveBiped
    # name when bone_maps knows one (the twist/helper bones) — only the rest
    # fall back to z_*, and conflating the two made the log claim z_ bones that
    # are not in the model.
    n_z = sum(1 for i, nm in new_name.items()
              if i not in mapped and nm.startswith("z_"))
    log(f"conformed {len(mapped)} bone(s) to donor bind "
        f"({donor_mdl.name}); {renamed_native - n_z} helper bone(s) named "
        f"from the ValveBiped map, {n_z} renamed z_*")
    if pp_lines:
        log(f"mirrored {len(pp_lines)} donor $poseparameter(s)")
    return ConformResult(
        mapped=len(mapped),
        renamed_native=renamed_native,
        bind_frame=bind_frame,
        prop_frame=prop_frame,
        poseparam_lines=pp_lines,
        donor_basename=donor_mdl.name,
        anat_rotations=anat_rotations,
    )


def playermodel_qci_lines(res: ConformResult, model_name: str) -> List[str]:
    """QCI snippet wiring the conform outputs into a playermodel QC.

    The proportions sequence must stay LAST in the final QC; $include this
    file after everything else (or paste its lines at the end).
    """
    return [
        "// Generated by Source 2 Porting Kit - playermodel conform sidecar.",
        "// $include this AFTER all other sequences (proportions must be last).",
        *res.poseparam_lines,
        "",
        f'$includemodel "{res.donor_basename}"',
        "",
        # studiomdl yaws the ROOT bone of sequence animations +90 (XSI-era
        # legacy) but does NOT yaw $definebone data. Local sequences MUST
        # carry rotate -90 or the root twists 90 degrees against the skeleton
        # whenever they play (while included anim-library sequences, already
        # convention-consistent, do not) - the classic invariant "pretzel".
        f'$sequence "reference" "{model_name}_bindpose.smd" fps 1 rotate -90',
        "",
        f'$animation a_proportions "{model_name}_proportions.smd" '
        f'rotate -90 subtract "reference" 0',
        '$sequence "proportions" "a_proportions" delta autoplay '
        'fadein 0.0 fadeout 0.0',
    ]
