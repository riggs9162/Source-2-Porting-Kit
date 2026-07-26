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

import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from ..core import ragdoll_presets
from ..core.bone_maps import SOURCE2_TO_VALVEBIPED
from ..tools.gltf_animation import GltfJoint, GltfSkin, parse_skin
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
from ..core.bone_maps import valvebiped_name
from ..tools.ragdoll_physics import (conformed_s2_constraint_rows,
                                     hulls_from_point_clouds, load_phys_dump,
                                     phys_part_point_clouds)
from .mdl_bones import read_mdl_bones
from .procedural_bones import write_vrd
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
                     keep_helpers: bool = True) -> GltfSkin:
    """ValveBiped-only prune: keep joints whose map target exists in the
    donor; fold every pruned joint's weights onto its nearest kept ancestor
    through the JOINT tree (the single fold rule). Mesh vj arrays are
    remapped in place (duplicate targets per vertex merge their weights)."""
    keep = [i for i, j in enumerate(skin.joints)
            if SOURCE2_TO_VALVEBIPED.get(j.name) in donor_names]
    if keep_helpers:
        # Bones the donor lacks but that actually drive geometry — twists,
        # scapulae, pectorals, finger metacarpals. Folding their weight onto a
        # parent collapses the deformation they exist to provide (on the
        # metrocop the arm/leg twists alone dominate ~6000 verts). Pure rig
        # scaffolding (IK targets, root motion, *_end tips) carries no weight
        # and is still dropped.
        weight = np.zeros(len(skin.joints))
        for em in meshes:
            vj, vw = em["vj"], em["vw"]
            for k in range(len(vj)):
                for s in range(vj.shape[1]):
                    w = float(vw[k][s])
                    if w > 0.0:
                        weight[int(vj[k][s])] += w
        extra = [i for i in range(len(skin.joints))
                 if i not in keep and weight[i] >= _HELPER_MIN_WEIGHT]
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
    valvebiped_only: bool = True,
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
        skin = _prune_to_mapped(skin, donor_names, all_arrays, log)

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
    if total_bones > 128:
        log(f"{total_bones} compiled bones (>128): expect trouble in GMod's "
            "own studiomdl and older tools — enable the ValveBiped-only "
            "prune or trim the source rig", "WARNING")

    res = conform_to_donor(skin, None, None, None, donor_mdl,
                           on_log=lambda m: log(m, "INFO"),
                           extra_meshes=all_arrays + phys_arrays)
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

    coord = CoordinateMode(scale=1.0, swap_axes=False)
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

    # Procedural helper bones. The kept twist bones are plain children here —
    # Source 2 drove them with constraints that the glTF export bakes away, so
    # without these rules they follow their parent rigidly and contribute no
    # twist at all (keeping them measured identical to dropping them).
    helpers = write_vrd(out_dir / f"{name_base}_pm.vrd", sorted(have))
    if helpers:
        L.append(f'$proceduralbones "{name_base}_pm.vrd"')
        L.append("")
        log(f"procedural helpers driven: {len(helpers)} "
            f"({', '.join(h.split('_', 1)[-1] for h in helpers[:4])}...)")

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
    L.append("")
    L.append(f'$includemodel "{res.donor_basename}"')
    L.append("")
    # studiomdl yaws sequence-anim roots +90 (XSI legacy) but not definebones
    # — every locally-authored sequence needs the compensation
    L.append('$sequence "reference" "pm_bindpose.smd" fps 1 rotate -90')
    L.append('$sequence "ragdoll" "pm_bindpose.smd" fps 30 rotate -90 '
             'activity "ACT_DIERAGDOLL" 1')
    L.append("")
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
        coll_lines, _hbox, cerr = build_ragdoll_collision(
            skin=skin, all_arrays=all_arrays, out_dir=out_dir,
            name_base=name_base, phys_data=None, physics_glb=None,
            prebuilt_hulls=phys_hulls or None, constraint_rows=phys_rows,
            total_mass=total_mass, constraint_source="valvebiped_donor",
            phys_smd_name="pm_physics.smd", on_log=log)
        if coll_lines:
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
