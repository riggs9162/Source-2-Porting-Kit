"""Source 2 GLB -> a ValveBiped NPC or playermodel, in one pass.

The port is deliberately small, because the skeleton work all happens in
:mod:`s2kit.pipelines.skeleton_retarget`. What is left here is bookkeeping: read
the GLB, retarget, point the vertex links at the new bone indices, turn the mesh
with the rig, write the SMDs, and assemble a QC.

NPC and playermodel are the *same* conversion. They differ only in which
animation library the QC includes and which donor `.mdl` the rotations were
derived from, so both come out of :func:`port_model` with a different `donor`.

The model adopts the library's build
------------------------------------
`CalcVirtualAnimation` initialises every bone an included library also defines
from that library's bind - `pSeqbone[]` - and discards ours, on every frame it
decodes. There is no keeping the source model's own proportions through that:
a `$proportions` autoplay delta was tried here and measured, in-engine, to have
no effect whatsoever (a 5x exaggerated delta changed nothing). A decompiled 1:1
reference port of this very model settles the intent - its `$definebone` values
are m_anm's to six decimals and it ships no `$proportions` sequence at all.

So the bind IS the donor's: the character adopts HL2's build, which is what
every working port of this kind accepts.

The mesh does **not** follow it per bone. It gets one global similarity
transform and nothing else - see :mod:`s2kit.pipelines.mesh_conform` for the
measurements that settled that, because the opposite is the intuitive answer
and it tears the model.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from s2kit.pipelines import (mesh_conform, procedural_bones,
                             skeleton_retarget as retarget)
from s2kit.pipelines.gltf_animation import (SPACE_METRES, derive_bind_pose_local,
                                            parse_skin, position_space)
from s2kit.pipelines.glb_skin import (_write_ref_smd, bodygroup_lines,
                                      collect_skinned_prims, group_bodygroups,
                                      is_s2v_native_gltf, read_glb)
from s2kit.pipelines.mdl_bones import (mdl_version, read_mdl_bones,
                                       read_mdl_poseparams)
from s2kit.pipelines.porting import LogFn, check_cancelled
from s2kit.pipelines.smd_animation_export import (CoordinateMode,
                                                  SmdAnimationExporter,
                                                  derive_definebone_lines)
from s2kit.pipelines.studiomdl import compile_qc

__all__ = ["PortResult", "port_model"]

#: studiomdl yaws the root of every SMD-loaded sequence +90 degrees (XSI
#: legacy) but leaves `$definebone` alone. NPCs hide it because all their
#: sequences shift together; a playermodel mixes locally-authored sequences
#: with library ones and the clash scrambles the rig. Every local sequence and
#: animation therefore compensates.
_LOCAL_SEQ_ROTATE = "rotate -90"

#: The model's bind, which is now the donor's. The one locally-authored pose.
_RAGDOLL_SMD = "ragdoll.smd"


@dataclass
class PortResult:
    qc: Path
    files: List[Path] = field(default_factory=list)
    bones: int = 0
    canonical: int = 0
    auxiliary: int = 0
    discarded: int = 0
    bodygroups: List[str] = field(default_factory=list)
    merged_vertices: int = 0
    alignment_yaw: float = 0.0
    unmatched_donor_bones: List[str] = field(default_factory=list)
    poseparameters: List[str] = field(default_factory=list)
    compiled: bool = False


def port_model(
    glb_path: Path,
    out_dir: Path,
    donor_mdl: Path,
    *,
    modelname: str,
    cdmaterials: str = "models/",
    surfaceprop: str = "flesh",
    align_yaw: Optional[float] = None,
    scale: float = 1.0,
    max_bones: int = retarget.MAX_COMPILED_BONES,
    studiomdl: Optional[Path] = None,
    game: Optional[Path] = None,
    on_log: Optional[LogFn] = None,
    cancel: Optional[threading.Event] = None,
) -> PortResult:
    """Port one skinned GLB onto the donor's ValveBiped axes and write sources."""
    log = on_log or (lambda _m, _l="INFO": None)
    glb_path, out_dir, donor_mdl = Path(glb_path), Path(out_dir), Path(donor_mdl)
    out_dir.mkdir(parents=True, exist_ok=True)

    donor_bones, poseparams = _read_donor(donor_mdl, log)

    gltf, buf = read_glb(glb_path)
    skin = parse_skin(gltf, buf)
    if skin is None or not skin.joints:
        raise ValueError("the GLB has no skin - a character port needs a "
                         "skinned mesh")
    mesh_sets, all_arrays = collect_skinned_prims(gltf, buf, log)
    check_cancelled(cancel)

    if position_space(gltf) == SPACE_METRES:
        # Every stage below - the retarget against the donor's ValveBiped axes,
        # the alignment, the reference SMDs - read the raw arrays as Source units.
        # Converting the mesh alone would leave the skeleton behind, so refuse
        # rather than port a character 39.37x too small.
        raise ValueError(
            "this GLB's vertices are baked into glTF metres (Source 2 Viewer 20 "
            "does that to render meshes), and the character port needs Source "
            "units - re-export it with Source 2 Viewer 19.2, or take it through "
            "the glTF Batch SMD tool, which converts both spaces")

    native = is_s2v_native_gltf(gltf)
    coord = CoordinateMode(scale=1.0 if native else scale, swap_axes=not native)
    log(f"{len(skin.joints)} source bone(s); units are "
        f"{'Source-native' if native else f'glTF, scaled x{scale:g}'}", "INFO")

    weighted = retarget.weighted_joint_indices(all_arrays)
    result = retarget.retarget_skeleton(
        skin, donor_bones, weighted,
        weight_mass=retarget.joint_weight_mass(all_arrays),
        max_bones=max_bones, align_yaw=align_yaw, on_log=log, cancel=cancel)
    merged = retarget.remap_vertex_joints(result, all_arrays, on_log=log)
    before = {id(prim): np.asarray(prim["vertices"], dtype=np.float64).copy()
              for prim in all_arrays}
    mesh_conform.align_mesh(result.alignment, all_arrays, on_log=log,
                            cancel=cancel)
    _report_strain(result, all_arrays, before, log)
    check_cancelled(cancel)

    written: List[Path] = []
    sets = _write_reference_smds(result, mesh_sets, out_dir, written, cancel)
    groups = group_bodygroups(sets)
    _write_pose_smds(result, out_dir, coord, written, log)
    vrd = _write_vrd(result, out_dir, modelname, written, log)

    qc_path = _write_qc(result, out_dir, written, groups=groups,
                        modelname=modelname, cdmaterials=cdmaterials,
                        surfaceprop=surfaceprop, coord=coord,
                        donor_mdl=donor_mdl, donor_bones=donor_bones,
                        arrays=all_arrays, vrd=vrd, log=log)

    outcome = PortResult(
        qc=qc_path, files=written, bones=len(result.skin.joints),
        canonical=len(result.canonical), auxiliary=len(result.auxiliary),
        discarded=len(result.discarded),
        bodygroups=[name for name, _smds, _blank in groups],
        merged_vertices=merged, alignment_yaw=result.alignment_yaw,
        unmatched_donor_bones=result.unmatched_donor_bones,
        poseparameters=[p.name for p in poseparams],
    )
    if studiomdl:
        outcome.compiled = compile_qc(qc_path, Path(studiomdl), Path(game or ""),
                                      on_log=log, cancel=cancel)
    return outcome


# ---------------------------------------------------------------------------

def _read_donor(donor_mdl: Path, log: LogFn):
    """Donor bone table + pose parameters, with the failure modes named."""
    if not donor_mdl.is_file():
        raise ValueError(f"donor .mdl not found: {donor_mdl}")
    version = mdl_version(donor_mdl)
    if version is None:
        raise ValueError(f"not a Source 1 .mdl (no IDST magic): {donor_mdl}")
    bones = read_mdl_bones(donor_mdl)
    if not bones:
        raise ValueError(f"donor .mdl has no bone table: {donor_mdl}")
    poseparams = read_mdl_poseparams(donor_mdl)
    log(f"donor {donor_mdl.name}: v{version}, {len(bones)} bone(s), "
        f"{len(poseparams)} pose parameter(s)", "INFO")
    return bones, poseparams


def _report_strain(result: retarget.RetargetResult, arrays: Sequence[dict],
                   before: Dict[int, np.ndarray], log: LogFn) -> None:
    """Assert the mesh really did move rigidly, and name any bone where it did not.

    Under the global alignment this is zero by construction, which is the
    point: it is the regression guard that catches any future stage which
    starts moving vertices per bone. It needs no reference port, so it holds
    for any model against any donor.
    """
    stats = mesh_conform.measure_strain([before[id(p)] for p in arrays], arrays,
                                        result.skin)
    if not stats.get("edges"):
        return
    if stats["over_10"] <= 1e-6:
        log(f"mesh moved rigidly: {stats['edges']} edges, no measurable stretch",
            "INFO")
        return
    log(f"mesh stretched where it should not have: median {stats['median']:.3f}, "
        f"{stats['over_10']:.1%} of edges over 10%, "
        f"{stats['over_50']:.1%} over 50%", "WARNING")
    if stats["worst_bones"]:
        worst = ", ".join(f"{n.replace('ValveBiped.Bip01_', '')} {m:.2f}"
                          for n, m, _c in stats["worst_bones"][:4])
        log(f"  worst bones (median strain): {worst}", "WARNING")


def _write_reference_smds(result: retarget.RetargetResult,
                          mesh_sets: Sequence[Tuple[str, List[dict]]],
                          out_dir: Path, written: List[Path],
                          cancel: Optional[threading.Event]
                          ) -> List[Tuple[str, str, int]]:
    """One reference SMD per mesh node; returns the bodygroup input tuples."""
    sets: List[Tuple[str, str, int]] = []
    for name, prims in mesh_sets:
        check_cancelled(cancel)
        safe = _safe_name(name)
        smd_name = f"ref_{safe}.smd"
        path = out_dir / smd_name
        _write_ref_smd(path, result.skin, prims, cancel)
        written.append(path)
        sets.append((safe, smd_name, sum(len(p["vertices"]) for p in prims)))
    return sets


def _write_pose_smds(result: retarget.RetargetResult, out_dir: Path,
                     coord: CoordinateMode, written: List[Path],
                     log: LogFn) -> None:
    """The one local pose the model needs: its bind, which is now the donor's."""
    bind = derive_bind_pose_local(result.skin)
    ok, err = SmdAnimationExporter.write_animation(
        result.skin, [bind], out_dir / _RAGDOLL_SMD, coord)
    if not ok:
        raise ValueError(f"bind pose SMD: {err}")
    written.append(out_dir / _RAGDOLL_SMD)


def _write_qc(result: retarget.RetargetResult, out_dir: Path,
              written: List[Path], *, groups, modelname: str,
              cdmaterials: str, surfaceprop: str,
              coord: CoordinateMode, donor_mdl: Path, donor_bones, arrays,
              vrd: Optional[str], log: LogFn) -> Path:
    """Assemble the QC in the shape a working reference port uses."""
    names = {joint.name for joint in result.skin.joints}
    lines: List[str] = [
        f'$modelname "{modelname}"',
        "",
        f'$cdmaterials "{cdmaterials}"',
        f'$surfaceprop "{surfaceprop}"',
        '$contents "solid"',
        "",
    ]
    lines.extend(bodygroup_lines(groups))
    lines.append("")

    lines.extend(derive_definebone_lines(result.skin, coord))
    lines.extend(_animation_attachment_bones(donor_bones, names))
    lines.append("")

    lines.extend(_attachment_lines(result, names, donor_bones))
    lines.extend(_hull_lines(result, arrays, names))
    lines.append("")

    lines.extend(_ik_lines(names))
    lines.append("")

    if vrd:
        # Without this the twist helpers are inert: they hold bind and follow
        # their parent rigidly, so the elbow, shoulder and thigh deformation
        # they exist for never happens.
        lines.append(f'$proceduralbones "{vrd}"')
        lines.append("")

    # One local sequence, playing the bind. No `reference`, no `$proportions`,
    # and no `$poseparameter` mirror - the reference port carries none of them
    # and takes the library's own.
    lines.append(f'$sequence "ragdoll" "{_RAGDOLL_SMD}" fps 30 '
                 f'{_LOCAL_SEQ_ROTATE} activity "ACT_DIERAGDOLL" 1')
    lines.append("")
    lines.append(f'$includemodel "{donor_mdl.stem}.mdl"')
    lines.append("")

    qc_path = out_dir / f"{_safe_name(Path(modelname).stem)}.qc"
    qc_path.write_text("\n".join(lines) + "\n", encoding="ascii")
    written.append(qc_path)
    log(f"QC written: {qc_path.name} ({len(result.skin.joints)} bones, "
        f"{len(groups)} bodygroup(s))", "SUCCESS")
    return qc_path


def _write_vrd(result: retarget.RetargetResult, out_dir: Path, modelname: str,
               written: List[Path], log: LogFn) -> Optional[str]:
    """Write the twist-helper driver file, or nothing if the rig has no twists."""
    text = procedural_bones.build_vrd(result.skin, on_log=log)
    if "<helper>" not in text:
        return None
    name = f"{_safe_name(Path(modelname).stem)}_helper.vrd"
    path = out_dir / name
    path.write_text(text, encoding="ascii")
    written.append(path)
    return name


def _animation_attachment_bones(donor_bones, names: set) -> List[str]:
    """`$definebone` the library's hand attachment bones onto our hands.

    They carry no skin weight, so nothing brings them across on its own, but
    HL2's held-weapon animations drive them and a playermodel without them
    holds its weapons in the wrong place.
    """
    from s2kit.pipelines.smd_animation_export import _quat_to_pyr_degrees

    by_name = {bone.name: bone for bone in donor_bones}
    lines: List[str] = []
    for extra, parent in (("ValveBiped.Anim_Attachment_RH",
                           "ValveBiped.Bip01_R_Hand"),
                          ("ValveBiped.Anim_Attachment_LH",
                           "ValveBiped.Bip01_L_Hand")):
        bone = by_name.get(extra)
        if bone is None or parent not in names:
            continue
        pit, yaw, rol = _quat_to_pyr_degrees(np.asarray(bone.quat,
                                                        dtype=np.float64))
        lines.append(f'$definebone "{extra}" "{parent}" '
                     f'{bone.pos[0]:.6f} {bone.pos[1]:.6f} {bone.pos[2]:.6f} '
                     f'{pit:.6f} {yaw:.6f} {rol:.6f} 0 0 0 0 0 0')
    return lines


def _attachment_lines(result: retarget.RetargetResult, names: set,
                      donor_bones) -> List[str]:
    """Eyes, plus the two hand attachments HL2's weapon animations expect."""
    lines: List[str] = []
    head = "ValveBiped.Bip01_Head1"
    if head in names:
        lines.append(f'$attachment "eyes" "{head}" 4.5 -5 0 rotate 0 -90 -90')
    have = {bone.name for bone in donor_bones}
    for side in ("RH", "LH"):
        bone = f"ValveBiped.Anim_Attachment_{side}"
        if bone in have and f"ValveBiped.Bip01_{side[0]}_Hand" in names:
            lines.append(f'$attachment "anim_attachment_{side}" "{bone}" '
                         f'0 0 0 rotate -90 -90 0')
    return lines


def _hull_lines(result: retarget.RetargetResult, arrays,
                names: set) -> List[str]:
    lines: List[str] = []
    worlds = retarget.skin_world_matrices(result.skin)
    head = "ValveBiped.Bip01_Head1"
    if head in names:
        index = [j.name for j in result.skin.joints].index(head)
        lines.append(f'$eyeposition 0 0 {worlds[index][2, 3]:.2f}')
    every = np.vstack([np.asarray(prim["vertices"], dtype=np.float64)
                       for prim in arrays])
    middle = (every.min(axis=0) + every.max(axis=0)) * 0.5
    lines.append(f'$illumposition {middle[0]:.3f} {middle[1]:.3f} '
                 f'{middle[2]:.3f}')
    lines.append("$bbox -15 -15 0 15 15 72")
    lines.append("$cbox 0 0 0 0 0 0")
    return lines


def _ik_lines(names: set) -> List[str]:
    """The IK chains and foot locks every ValveBiped character carries.

    HL2's animations reference these by name; without them the library's ikrule
    data has nothing to resolve against and feet slide through the floor.
    """
    chains = (("rhand", "ValveBiped.Bip01_R_Hand", "0.707 0.707 0"),
              ("lhand", "ValveBiped.Bip01_L_Hand", "0.707 0.707 0"),
              ("rfoot", "ValveBiped.Bip01_R_Foot", "0.707 -0.707 0"),
              ("lfoot", "ValveBiped.Bip01_L_Foot", "0.707 -0.707 0"))
    lines = [f'$ikchain "{tag}" "{bone}" knee {knee}'
             for tag, bone, knee in chains if bone in names]
    if not lines:
        return lines
    lines.append("")
    for tag, bone, _knee in chains:
        if tag.endswith("foot") and bone in names:
            lines.append(f'$ikautoplaylock "{tag}" 1 0.1')
    return lines


def _safe_name(name: str) -> str:
    """A filename-safe, lowercase stem."""
    out = "".join(c if (c.isalnum() or c in "._-") else "_" for c in name)
    return out.strip("._-").lower() or "model"
