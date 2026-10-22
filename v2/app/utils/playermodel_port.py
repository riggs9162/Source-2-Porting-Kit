"""One-shot Source 2 GLB -> GMod playermodel source generator.

Implements the verified conversion recipe (see the ``s2-valvebiped-conform``
skill and tools/valvebiped_conform.py): ValveBiped-only prune with weight
folding, donor-exact bind conform with the anatomical (swing + forward-
anchored twist) re-skin, single-case bone naming, per-mesh bodygroup SMDs,
and a full playermodel QC ($definebone in QAngle order, donor $poseparameter
mirror, $includemodel, ``rotate -90`` local sequences, weight-derived
``$collisionjoints`` for the death ragdoll). Optionally compiles the result
with StudioMDL CE.

Qt-free: reachable from the GUI tool, CLIs and tests alike.
"""

from __future__ import annotations

import re
import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from ..core import ragdoll_presets
from ..core.bone_maps import SOURCE2_TO_VALVEBIPED
from ..tools.gltf_animation import (GltfJoint, GltfSkin,
                                    derive_bind_pose_local, parse_skin)
from ..tools.smd_animation_export import (CoordinateMode, SmdAnimationExporter,
                                          derive_definebone_lines,
                                          _quat_to_pyr_degrees)
from ..tools.valvebiped_conform import _mat_to_quat, conform_to_donor
# _acc/_mat_to_euler_xyz/_q2m/_write_ref_smd re-exported for existing callers
from .glb_skin_io import (LogFn, _acc, _mat_to_euler_xyz, _q2m,  # noqa: F401
                          _write_ref_smd, bodygroup_lines,
                          collect_skinned_prims, group_bodygroups, read_glb,
                          relax_skin_weights)

# Relaxation passes applied to the folded skin weights. Gains plateau by ~5
# (grunt worst edge 4.75u -> 3.86u) while weights only travel this many edges,
# so a low count keeps finger and elbow definition crisp.
_WEIGHT_RELAX_PASSES = 3

# Total skin weight a donor-less bone must carry to be kept as a helper. Well
# above rounding noise, well below anything that actually shapes the mesh.
_HELPER_MIN_WEIGHT = 1.0
# Palm bones. Never kept as helpers — see the note in _prune_to_mapped.
_METACARPAL_RE = re.compile(r"(_meta_|_metacarpal|Finger\dm$)", re.I)
from ..core.bone_maps import valvebiped_name
from ..tools.ragdoll_physics import (conformed_s2_constraint_rows,
                                     hulls_from_point_clouds, load_phys_dump,
                                     phys_part_point_clouds)
from .jiggle_bones import (jigglebone_lines, reorient_for_jiggle,
                           select_chains)
from .mdl_bones import read_mdl_bones
from .ragdoll_port import build_ragdoll_collision, discover_sidecars
from .studiomdl_run import compile_qc


def _bone_worlds(skin: GltfSkin) -> Dict[int, np.ndarray]:
    """World matrix per joint from the skin's rest TRS, parents first."""
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


def _prune_to_mapped(skin: GltfSkin, donor_names: set,
                     meshes: List[dict], log: LogFn,
                     keep_helpers: bool = False) -> GltfSkin:
    """ValveBiped-only prune: keep joints whose map target exists in the
    donor; fold every pruned joint's weights onto its nearest kept ancestor
    through the JOINT tree (the single fold rule). Mesh vj arrays are
    remapped in place (duplicate targets per vertex merge their weights).

    ``keep_helpers`` spares donor-less bones that carry real skin weight. It
    defaults OFF so that "drop non-ValveBiped bones" means exactly that: the
    written SMDs list only kept bones and the dropped ones' weight lands on
    the parent. Kept helpers are still the better-deforming rig (see below),
    so the option remains - it is just no longer silently on."""
    keep = [i for i, j in enumerate(skin.joints)
            if SOURCE2_TO_VALVEBIPED.get(j.name) in donor_names]
    if keep_helpers:
        # Bones the donor lacks but that actually drive geometry — twists,
        # scapulae, pectorals. Folding their weight onto a parent collapses the
        # deformation they exist to provide (on the metrocop the arm/leg twists
        # alone dominate ~6000 verts). Pure rig scaffolding (IK targets, root
        # motion, *_end tips) carries no weight and is still dropped.
        #
        # METACARPALS are excluded on purpose. ValveBiped has no palm bone — it
        # hangs Finger1..4 straight off Hand — so donor reparenting orphans a
        # kept metacarpal: it rides the hand at its SOURCE offset while the
        # fingers move onto the donor's, and drags the knuckle flesh between
        # them. Dropping them folds that flesh onto Hand, which is exactly where
        # ValveBiped puts the palm. Measured on three characters, hand-edge p90
        # 0.355->0.193, 0.439->0.205, 0.402->0.174 and the whole-body p90
        # improves too. A hand-made port of one of these characters
        # (bioVespa303's) ships a 55-bone pure-ValveBiped rig with no
        # metacarpals at all, for the same reason.
        # MOST of that gap was a bug, not the metacarpals. Dropping them also
        # makes each finger root a direct child of Hand in the SOURCE graph, so
        # the donor's reparenting becomes a no-op — which is what accidentally
        # kept this path clear of the `old_world` fault fixed in
        # valvebiped_conform (the prune path measures BIT-IDENTICAL before and
        # after that fix; the keep-helpers path improved 3x). Re-measured after
        # it, hand-edge p90 keep vs prune is 0.276 vs 0.201 (metrocop) and 0.283
        # vs 0.203 (hazmat) — still a real edge to pruning, now 1.4x rather than
        # 4.2x, and paid for with the twist/scapula deformation the helpers
        # carry. Which is why this branch is worth keeping as an option even
        # though it is no longer the default.
        weight = np.zeros(len(skin.joints))
        for em in meshes:
            vj, vw = em["vj"], em["vw"]
            for k in range(len(vj)):
                for s in range(vj.shape[1]):
                    w = float(vw[k][s])
                    if w > 0.0:
                        weight[int(vj[k][s])] += w
        extra = [i for i in range(len(skin.joints))
                 if i not in keep and weight[i] >= _HELPER_MIN_WEIGHT
                 and not _METACARPAL_RE.search(skin.joints[i].name)]
        if extra:
            keep = sorted(set(keep) | set(extra))
            log(f"keeping {len(extra)} weight-bearing helper bone(s) the "
                f"donor lacks", "INFO")
    keep_set = set(keep)
    fold: Dict[int, int] = {}
    for i, j in enumerate(skin.joints):
        cur = i
        while cur is not None and cur not in keep_set:
            cur = skin.joints[cur].parent_joint_idx
        fold[i] = cur if cur is not None else keep[0]

    # old worlds for local recomputation across pruned parents
    world: Dict[int, np.ndarray] = {}

    def fk(i: int) -> np.ndarray:
        if i in world:
            return world[i]
        j = skin.joints[i]
        m = np.eye(4)
        m[:3, :3] = _q2m(j.rest_rotation)
        m[:3, 3] = np.asarray(j.rest_translation, dtype=np.float64)
        world[i] = m if j.parent_joint_idx is None else fk(j.parent_joint_idx) @ m
        return world[i]

    for i in range(len(skin.joints)):
        fk(i)

    remap = {old: new for new, old in enumerate(keep)}
    joints: List[GltfJoint] = []
    for old in keep:
        j = skin.joints[old]
        p = j.parent_joint_idx
        while p is not None and p not in keep_set:
            p = skin.joints[p].parent_joint_idx
        lm = world[old] if p is None else np.linalg.inv(world[p]) @ world[old]
        # Branch-based extraction: w = sqrt(1+trace)/2 is only valid for
        # trace > 0, and folding a pruned chain routinely lands a joint on a
        # ~180 deg local rotation (trace -1), where it divides by ~0. The
        # combine grunt's ankles hit it exactly and came out 71 deg off, which
        # flipped the feet and, through the toe-derived character-forward
        # vector, twisted every other bone's roll.
        q = _mat_to_quat(lm[:3, :3])
        joints.append(GltfJoint(
            name=j.name, gltf_node_idx=j.gltf_node_idx,
            parent_joint_idx=None if p is None else remap[p],
            inverse_bind_matrix=j.inverse_bind_matrix,
            rest_translation=lm[:3, 3].copy(), rest_rotation=q,
            rest_scale=np.ones(3)))

    for em in meshes:
        vj = em["vj"]
        vw = em["vw"]
        for v in range(len(vj)):
            slots: Dict[int, float] = {}
            for k in range(vj.shape[1]):
                w = float(vw[v][k])
                if w <= 0.0:
                    continue
                tgt = remap[fold[int(vj[v][k])]]
                slots[tgt] = slots.get(tgt, 0.0) + w
            items = sorted(slots.items(), key=lambda kv: -kv[1])[:vj.shape[1]]
            tot = sum(w for _b, w in items) or 1.0
            vj[v] = 0
            vw[v] = 0.0
            for k, (b, w) in enumerate(items):
                vj[v][k] = b
                vw[v][k] = w / tot

    # Folding leaves blocky weight transitions; skinning adapts the mesh to the
    # donor's proportions only across those, so relax them before conforming.
    relax_skin_weights(meshes, len(joints), passes=_WEIGHT_RELAX_PASSES)

    root = next(i for i, j in enumerate(joints) if j.parent_joint_idx is None)
    log(f"valvebiped-only prune: {len(skin.joints)} -> {len(joints)} joints "
        f"(weights folded through the joint tree, relaxed "
        f"x{_WEIGHT_RELAX_PASSES})", "INFO")
    return GltfSkin(joints=joints, root_joint_idx=root, skin_idx=skin.skin_idx)


def port_playermodel(
    glb_path: Path,
    donor_mdl: Path,
    out_dir: Path,
    modelname: str,
    cdmaterials: str,
    surfaceprop: str = "flesh",
    valvebiped_only: bool = False,
    keep_helpers: bool = False,
    own_proportions: bool = False,
    jiggle_bones: str = "",
    jiggle_preset: str = "gear",
    ragdoll_collision: bool = True,
    total_mass: float = ragdoll_presets.DEFAULT_TOTAL_MASS,
    studiomdl: Optional[Path] = None,
    studiomdl_game: Optional[Path] = None,
    on_log: Optional[LogFn] = None,
) -> dict:
    """Generate (and optionally compile) a full playermodel source set."""
    log = on_log or (lambda _m, _l="INFO": None)
    glb_path, donor_mdl, out_dir = Path(glb_path), Path(donor_mdl), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    gltf, buf = read_glb(glb_path)
    skin = parse_skin(gltf, buf)
    if skin is None:
        raise ValueError("GLB has no skin - a playermodel needs a skinned mesh")

    donor_bones = read_mdl_bones(donor_mdl)
    donor_names = {b.name for b in donor_bones}

    # per-mesh-node primitives (verts stay raw; conform moves them)
    mesh_sets, all_arrays = collect_skinned_prims(gltf, buf, log)

    if valvebiped_only:
        skin = _prune_to_mapped(skin, donor_names, all_arrays, log,
                                keep_helpers=keep_helpers)
    else:
        # Keeping every bone must not silently cost the weight relaxation the
        # prune happens to run at the end of its fold.
        relax_skin_weights(all_arrays, len(skin.joints),
                           passes=_WEIGHT_RELAX_PASSES)
        log(f"keeping all {len(skin.joints)} source bones "
            f"(weights relaxed x{_WEIGHT_RELAX_PASSES})", "INFO")

    # Real Source 2 collision, if the model ships a PHYS sidecar. The authored
    # Rubikon shapes live in the ORIGINAL bind space, so they cannot simply be
    # dropped in — they ride through conform_to_donor as extra vertex arrays
    # (one bone each, weight 1) and come out in donor bind space with the mesh.
    phys_clouds: List[Tuple[int, np.ndarray]] = []
    phys_arrays: List[dict] = []
    phys_obj = None
    if ragdoll_collision:
        _physics_glb, phys_dump = discover_sidecars(glb_path)
        if phys_dump is not None:
            try:
                phys_obj = load_phys_dump(phys_dump,
                                          on_log=lambda m: log(m, "INFO"))
                present = {j.name for j in skin.joints}

                def _rename(n: str) -> str:
                    return n if n in present else valvebiped_name(n)

                phys_clouds = phys_part_point_clouds(phys_obj, skin, _rename)
                for ji, pts in phys_clouds:
                    vj = np.zeros((len(pts), 4), dtype=np.int64)
                    vw = np.zeros((len(pts), 4))
                    vj[:, 0] = ji
                    vw[:, 0] = 1.0
                    phys_arrays.append(dict(vertices=pts.copy(), vj=vj, vw=vw))
                log(f"PHYS sidecar: {len(phys_clouds)} collision part(s) "
                    f"from {phys_dump.name}", "INFO")
            except Exception as exc:  # noqa: BLE001 - fall back to weight hulls
                log(f"PHYS sidecar unusable ({exc}); using weight-derived "
                    "collision hulls", "WARNING")
                phys_clouds, phys_arrays, phys_obj = [], [], None

    total_bones = len(skin.joints) + 2      # + Anim_Attachment helpers
    if total_bones > 256:
        log(f"{total_bones} compiled bones: GMod's limit is 256 - "
            "turn on the ValveBiped-only prune or trim the source "
            "rig", "WARNING")

    res = conform_to_donor(skin, None, None, None, donor_mdl,
                           own_proportions=own_proportions,
                           on_log=lambda m: log(m, "INFO"),
                           extra_meshes=all_arrays + phys_arrays)
    # The compiled bind has to stay ON the donor's, per rule 1: the runtime
    # discards a shared bone's own bind and rebuilds it from the anim library's.
    # Drift here is invisible to every rest-pose check - it only shows once a
    # donor clip plays - so measure it explicitly rather than trust the flags.
    # Gated on MAX, not on p50. The p50 gate this replaces was blind to exactly
    # the failure it exists to catch: one bone 39.331u out of 55 logged as
    # "p50 0.000u" at INFO with the explanatory clause suppressed, and that one
    # bone was the pelvis - the root of the whole skeleton. A single bone past
    # 0.1u is already a broken model, because the runtime rebuilds it from the
    # donor and everything below it inherits the error.
    donor_pos = {b.name: np.asarray(b.pos, dtype=np.float64) for b in donor_bones}
    drift = [(float(np.linalg.norm(np.asarray(j.rest_translation,
                                              dtype=np.float64)
                                   - donor_pos[j.name])), j.name)
             for j in skin.joints if j.name in donor_pos]
    if drift and max(d for d, _n in drift) > 0.1:
        worst, worst_bone = max(drift)
        past = sum(1 for d, _n in drift if d > 0.1)
        log(f"bind drift from {donor_mdl.name}: max {worst:.3f}u on "
            f"'{worst_bone}', {past}/{len(drift)} shared bone(s) past 0.1u "
            "- donor animations rebuild these bones from their own bind, so "
            "this misposes the mesh in game", "WARNING")
    # ...and the drift check alone cannot see a PARENTAGE fault: a bone whose
    # world bind is donor-exact still has the wrong LOCAL if its parent is not
    # the donor's, which is what the drift above measures. The donor's root bone
    # is the one case where that is not implied, so assert it directly.
    donor_root = next((b.name for b in donor_bones if b.parent < 0), None)
    for j in skin.joints:
        if j.name == donor_root and j.parent_joint_idx is not None:
            log(f"'{donor_root}' is the donor's ROOT but compiled under "
                f"'{skin.joints[j.parent_joint_idx].name}' - every donor "
                "sequence will compose its model-space pelvis through that "
                "chain and displace the whole skeleton", "WARNING")

    # phys_arrays were transformed in place alongside the render mesh, so the
    # collision points are now in the same donor bind space as the verts.
    phys_hulls = hulls_from_point_clouds(
        [(ji, np.asarray(a["vertices"]))
         for (ji, _p), a in zip(phys_clouds, phys_arrays)],
        on_log=lambda m: log(m, "INFO")) if phys_arrays else []
    # This model's OWN joint limits, re-expressed in the conformed bone frames,
    # in preference to Valve's generic humanoid table.
    phys_rows: Optional[List[str]] = None
    if phys_hulls and phys_obj is not None:
        present = {j.name for j in skin.joints}
        phys_rows = conformed_s2_constraint_rows(
            phys_obj, skin,
            lambda n: n if n in present else valvebiped_name(n),
            res.anat_rotations, on_log=lambda m: log(m, "INFO"))

    # Jiggle chains -----------------------------------------------------------
    # Done after the conform so it acts on the final bind, and before anything
    # is written so the ref SMDs, the bindpose and the $definebones all agree.
    jiggle_names: List[str] = []
    if jiggle_bones:
        pats = [s for s in re.split(r"[,;\s]+", jiggle_bones) if s]
        targets = select_chains(skin, pats)
        if not targets:
            log(f"no bones matched the jiggle patterns {pats}", "WARNING")
        else:
            jiggle_names = reorient_for_jiggle(
                skin, targets, on_log=lambda m: log(m, "INFO"))
            res.bind_frame = derive_bind_pose_local(skin)

    # SMDs -------------------------------------------------------------------
    name_base = glb_path.stem
    smd_of_set: List[Tuple[str, str, int]] = []
    for mname, prims in mesh_sets:
        safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in mname)
        smd = f"pm_{safe}.smd"
        _write_ref_smd(out_dir / smd, skin, prims)
        nverts = sum(len(p["vertices"]) for p in prims)
        smd_of_set.append((safe, smd, nverts))
        log(f"wrote {smd} ({nverts} verts)", "INFO")
    groups = group_bodygroups(smd_of_set)
    for gname, smds, add_blank in groups:
        if len(smds) > 1 or add_blank:
            log(f"bodygroup '{gname}': {len(smds)} variant(s)"
                f"{' + blank' if add_blank else ''}", "INFO")
    # GMod's studiomdl refuses 32 or more bodygroups outright
    # (EXCEPTION_ACCESS_VIOLATION); StudioMDL CE compiles them, and the model
    # then crashes the engine and HLMV the moment it is drawn, because the
    # running product in mstudiobodyparts_t::base overflows its signed int well
    # before the last slot. The HLA zombie hit this with 43 slots. Loud, because
    # a silent pass here produces a file that only fails at view time.
    if len(groups) >= 32:
        log(f"{len(groups)} bodygroups: 32 or more crashes GMod and HLMV "
            f"(the engine's bodypart index overflows). Merge variant meshes in "
            f"the source, or drop bodygroups, to get under 32.", "ERROR")

    coord = CoordinateMode(scale=1.0, swap_axes=False)
    # THREE distinct poses are needed when own_proportions is on, and getting
    # any two of them confused breaks it:
    #   $definebone      -> the model's OWN pose (what the mesh is skinned to)
    #   pm_bindpose.smd  -> the DONOR pose, because the autoplay delta is added
    #                       on top of whatever sequence plays, including this
    #                       one. Donor + (own - donor) = own, which is what the
    #                       mesh expects. Putting the own pose here instead
    #                       applies the difference TWICE and visibly exaggerates
    #                       the build.
    #   pm_ownpose.smd   -> the OWN pose, purely as the delta's source
    SmdAnimationExporter.write_animation(
        skin, [res.bind_frame], out_dir / "pm_bindpose.smd", coord)

    # QC ---------------------------------------------------------------------
    L: List[str] = [f'$modelname "{modelname}"',
                    f'$cdmaterials "{cdmaterials}"',
                    f'$surfaceprop "{surfaceprop}"', ""]
    L.extend(derive_definebone_lines(skin, coord))
    donor_by_name = {b.name: b for b in donor_bones}
    for extra, par in (("ValveBiped.Anim_Attachment_RH", "ValveBiped.Bip01_R_Hand"),
                       ("ValveBiped.Anim_Attachment_LH", "ValveBiped.Bip01_L_Hand")):
        b = donor_by_name.get(extra)
        if b is None or par not in {j.name for j in skin.joints}:
            continue
        pit, yaw, rol = _quat_to_pyr_degrees(np.asarray(b.quat, dtype=np.float64))
        L.append(f'$definebone "{extra}" "{par}" '
                 f'{b.pos[0]:.6f} {b.pos[1]:.6f} {b.pos[2]:.6f} '
                 f'{pit:.6f} {yaw:.6f} {rol:.6f} 0 0 0 0 0 0')
        L.append(f'$attachment "anim_attachment_{extra[-2:]}" "{extra}" '
                 '0 0 0 rotate -90 -90 0')
    L.append("")
    L.extend(bodygroup_lines(groups))
    L.append("")
    L.extend(res.poseparam_lines)
    L.append("")
    have = {j.name for j in skin.joints}
    for chain, bone, knee in (("rhand", "ValveBiped.Bip01_R_Hand", "0.707 0.707 0"),
                              ("lhand", "ValveBiped.Bip01_L_Hand", "0.707 0.707 0"),
                              ("rfoot", "ValveBiped.Bip01_R_Foot", "0.707 -0.707 0"),
                              ("lfoot", "ValveBiped.Bip01_L_Foot", "0.707 -0.707 0")):
        if bone in have:
            L.append(f'$ikchain "{chain}" "{bone}" knee {knee}')
    # Foot locking: without it the feet slide during move blends. Only
    # meaningful once the foot chains above exist.
    for chain in ("rfoot", "lfoot"):
        if f'$ikchain "{chain}"' in "\n".join(L):
            L.append(f'$ikautoplaylock "{chain}" 1 0.1')
    L.append("")

    # $proceduralbones is NOT emitted. Twist helpers stay inert children.
    #
    # Driving them was tried and reverted: it broke the model in game twice,
    # and even after fixing BOTH real format bugs found along the way - the
    # triggers must be centred on the control bone's rest orientation, and the
    # rest trigger must carry the helper's own bind local rotation rather than
    # identity - dropping $proceduralbones was still what made the model come
    # out right. What was never validated is the rule CONTENT: the 0.5/1.0 roll
    # fractions, the +-110 deg sample spacing and the assumption that a helper
    # twists about its local X are all invented here, not measured from the
    # source rig (Source 2 keeps that in the .vmdl and the glTF export bakes it
    # away). Do not re-enable without a way to check the DRIVEN pose; verifying
    # the rest pose passes while the driven pose is still wrong.
    #
    # build_vrd() is kept, with both format findings encoded and tested, for
    # whoever picks this up with real constraint data.

    # -- player fit-out ------------------------------------------------------
    # Emitted so the model behaves as a playermodel rather than a bare mesh:
    # the eyes attachment is what head-turn and NPC aiming read, the hull is
    # the standard 32x32x72 GMod player box, and $bonemerge lets bonemerged
    # props (weapons, accessories) attach. Values are derived from this
    # model's own bind, not copied.
    idx_of = {j.name: i for i, j in enumerate(skin.joints)}
    worlds = _bone_worlds(skin)
    head_i = idx_of.get("ValveBiped.Bip01_Head1")
    if head_i is not None:
        # same head-local offset HL2 humans use; our bind IS the donor's, so
        # it lands in the same place on the skull
        eye_w = (worlds[head_i] @ np.array([4.5, -5.0, 0.0, 1.0]))[:3]
        L.append(f'$attachment "eyes" "ValveBiped.Bip01_Head1" '
                 f'4.5 -5 0 rotate 0 -90 -90')
        L.append(f'$eyeposition 0 0 {eye_w[2]:.2f}')
    allv = np.vstack([np.asarray(a["vertices"], dtype=np.float64)
                      for a in all_arrays])
    mid = (allv.min(axis=0) + allv.max(axis=0)) * 0.5
    L.append(f'$illumposition {mid[0]:.3f} {mid[1]:.3f} {mid[2]:.3f}')
    L.append('$contents "solid"')
    L.append('$bbox -15 -15 0 15 15 72')
    L.append('$cbox 0 0 0 0 0 0')
    for bm in ("ValveBiped.Bip01_Pelvis", "ValveBiped.Bip01_Spine",
               "ValveBiped.Bip01_Spine1", "ValveBiped.Bip01_Spine2",
               "ValveBiped.Bip01_Spine4", "ValveBiped.Bip01_R_Clavicle",
               "ValveBiped.Bip01_R_UpperArm", "ValveBiped.Bip01_R_Forearm",
               "ValveBiped.Bip01_R_Hand"):
        if bm in have:
            L.append(f'$bonemerge "{bm}"')
    if jiggle_names:
        L.extend(jigglebone_lines(jiggle_names, jiggle_preset))
        L.append("")
        log(f"jigglebones: {len(jiggle_names)} bone(s), '{jiggle_preset}' "
            f"preset", "INFO")

    L.append("")
    L.append(f'$includemodel "{res.donor_basename}"')
    L.append("")
    # studiomdl yaws sequence-anim roots +90 (XSI legacy) but not definebones
    # — every locally-authored sequence needs the compensation
    L.append('$sequence "reference" "pm_bindpose.smd" fps 1 rotate -90')
    L.append('$sequence "ragdoll" "pm_bindpose.smd" fps 30 rotate -90 '
             'activity "ACT_DIERAGDOLL" 1')
    if own_proportions:
        # The mesh is bound to the model's OWN proportions, so the runtime -
        # which rebuilds every shared bone from the DONOR's bind - has to be
        # corrected back onto it. delta = own - donor, applied always.
        SmdAnimationExporter.write_animation(
            skin, [res.prop_frame], out_dir / "pm_ownpose.smd", coord)
        L.append('$animation "a_prop_base" "pm_bindpose.smd" fps 30 '
                 'rotate -90')
        L.append('$animation "a_proportions" "pm_ownpose.smd" fps 30 '
                 'rotate -90 subtract "a_prop_base" 0')
        L.append('$sequence "proportions" "a_proportions" autoplay delta '
                 'fadein 0.2 fadeout 0.2')
        log("own proportions kept; $proportions delta emitted", "INFO")

    L.append("")

    # The delta above is emitted ONLY under own_proportions, and only because
    # that flag also binds the mesh to the model's own segment lengths. The two
    # must move together: a delta over a mesh still bound at DONOR lengths
    # double-corrects, moving bones up to 3.9u while the flesh was solved for
    # the donor's - simulated against a decoded m_anm sequence, worst triangle
    # stretch went 1.72x (animation alone) -> 13.97x with 264 triangles past
    # 1.5x, in EVERY pose because it is autoplay.
    # own_proportions defaults OFF, and turning it on was tried and REVERTED.
    # It looks better by every static measure - with it on all 14 ValveBiped
    # segment lengths reproduce the source exactly and the bind-pose silhouette
    # IoU rises 0.792/0.764 -> 0.818/0.809 - but it breaks the model in game,
    # because those measures never exercise an animation. It moves the compiled
    # bind off the donor's, and rule 1 above says the runtime discards a shared
    # bone's own bind and rebuilds it from the anim library's. Measured against
    # a hand-made port that is known good in game, |compiled bind - donor bind|
    # per shared bone: reference p50 0.007u / max 2.34u with 23/53 bones past
    # 0.1u, versus own_proportions p50 0.546u / max 3.96u with 43/55 past it.
    # The whole skeleton drifts, so every donor clip poses the mesh against a
    # bind it was never skinned to. The delta is supposed to correct that and
    # demonstrably does not carry it in GMod. Keep the bind on the donor; the
    # cost is that the character adopts HL2's build, which is exactly what the
    # hand-made port accepts too.
    collision = False
    if ragdoll_collision:
        # Weight-derived hulls from the CONFORMED mesh only: after
        # conform_to_donor the verts and bind are donor-exact, so PHYS-dump
        # geometry (original S2 bind space) would attach hulls at wrong
        # offsets, while build_ragdoll's ValveBiped preset constraints are
        # exactly the valid set for the conformed rig.
        # The rig is conformed to the donor, so its bones carry true ValveBiped
        # orientations and Valve's anatomical constraints apply — elbows and
        # knees become hinges rather than free in all three axes.
        coll_lines, hbox_lines, cerr = build_ragdoll_collision(
            skin=skin, all_arrays=all_arrays, out_dir=out_dir,
            name_base=name_base, phys_data=None, physics_glb=None,
            prebuilt_hulls=phys_hulls or None, constraint_rows=phys_rows,
            total_mass=total_mass, constraint_source="valvebiped_donor",
            phys_smd_name="pm_physics.smd", on_log=log)
        if coll_lines:
            if hbox_lines:
                L.append('$hboxset "default"')
                L.extend(hbox_lines)
                L.append("")
            L.extend(coll_lines)
            collision = True
        else:
            log(f"ragdoll collision skipped: {cerr}", "WARNING")
            L.append(f"// no $collisionjoints ({cerr})")
    else:
        L.append("// no $collisionjoints (ragdoll collision disabled)")
    qc_path = out_dir / f"{name_base}_pm.qc"
    qc_path.write_text("\n".join(L) + "\n", encoding="ascii")
    log(f"QC written: {qc_path.name} ({len(skin.joints)} bones, "
        f"{len(groups)} bodygroup(s) from {len(smd_of_set)} mesh(es))",
        "SUCCESS")

    compiled = False
    if studiomdl:
        compiled = compile_qc(qc_path, Path(studiomdl),
                              Path(studiomdl_game), on_log=log)

    return dict(qc=str(qc_path), bones=len(skin.joints),
                bodygroups=[g for g, _s, _b in groups], collision=collision,
                compiled=compiled)
