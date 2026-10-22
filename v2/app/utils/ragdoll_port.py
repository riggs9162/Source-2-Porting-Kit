"""One-shot Source 2 GLB -> GMod ragdoll source generator.

Single-model generalization of the verified zombie_classic ragdoll build:
native skeleton by default, prune of helper bones above the PHYS root
(srcds-verified — leaving them pins the root solid at the world origin),
per-PHYS-part convex hulls with real Rubikon joint limits (v2 conversion,
widen 1.35), Valve zombie-tuned globals, distal ``$jointmassbias``,
``$hbox`` hitboxes from the part-local shape AABBs, ``$definebone`` in
QAngle order, and an idle sequence taken from one static frame of a real
posed clip (HLA never displays the raw rest pose).

Without a ``<stem>_phys.txt`` PHYS sidecar the pipeline falls back to
weight-derived hulls, which requires a ValveBiped-mappable skeleton (the
preset sim-bone list drives part selection).

Qt-free: reachable from the GUI tool, CLIs and tests alike.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from ..core import ragdoll_presets as presets
from ..core.bone_maps import valvebiped_name
from ..tools.gltf_animation import (derive_bind_pose_local, parse_clips,
                                    parse_skin, sample_clip)
from ..tools.ragdoll_physics import (_mat33_to_quat, bones_above_phys_root,
                                     build_hitbox_lines, build_ragdoll,
                                     build_ragdoll_phys, is_humanoid_skeleton,
                                     load_phys_dump, prune_skin_joints,
                                     retarget_skeleton_to_valvebiped)
from ..tools.smd_animation_export import (CoordinateMode, SmdAnimationExporter,
                                          derive_definebone_lines)
from ..tools.valvebiped_conform import conform_to_donor
from .glb_skin_io import (LogFn, _q2m, _write_ref_smd, bodygroup_lines,
                          collect_skinned_prims, concat_prims,
                          group_bodygroups, is_s2v_native_gltf,
                          load_glb_tri_mesh, read_glb)
from .studiomdl_run import compile_qc

CLIP_FPS = 30.0


def discover_sidecars(glb_path: Path) -> Tuple[Optional[Path], Optional[Path]]:
    """(physics_glb, phys_dump) next to the GLB, or None for missing files.

    ``<stem>_physics.glb`` / ``<stem>_phys.txt`` are the sidecar names the
    VRF Batch Export writes.
    """
    glb_path = Path(glb_path)
    physics = glb_path.with_name(f"{glb_path.stem}_physics.glb")
    dump = glb_path.with_name(f"{glb_path.stem}_phys.txt")
    return (physics if physics.is_file() else None,
            dump if dump.is_file() else None)


def _vmdl_vpk_path(glb_path: Path, gltf: Optional[dict]) -> Optional[str]:
    """The model's path inside its VPK (``models/.../<stem>.vmdl_c``).

    VRF's ``-f`` filter is a PATH PREFIX, so the dump needs the real vmdl
    path. Two sources, both derived from the export itself: the glTF's
    mesh/node names (VRF prefixes them with the full vmdl path), and the
    kit's ``modelsrc/<sub>/<stem>.glb`` layout which mirrors
    ``models/<sub>/<stem>.vmdl_c``.
    """
    stem = Path(glb_path).stem
    if gltf is not None:
        pat = re.compile(r"(models/[\w/.\-]*?" + re.escape(stem)
                         + r"\.vmdl_c)", re.IGNORECASE)
        for section in ("scenes", "nodes", "meshes"):
            for entry in gltf.get(section, []) or []:
                m = pat.search(str(entry.get("name", "")))
                if m:
                    return m.group(1)
    parts = [p.lower() for p in Path(glb_path).parts]
    if "modelsrc" in parts:
        i = parts.index("modelsrc")
        rel = Path(glb_path).parts[i + 1:-1]
        return "/".join(("models",) + rel + (f"{stem}.vmdl_c",))
    return None


def ensure_phys_sidecar(
    glb_path: Path,
    *,
    vrf_exe: Optional[Path] = None,
    source_vpk: Optional[Path] = None,
    gltf: Optional[dict] = None,
    on_log: Optional[LogFn] = None,
) -> Optional[Path]:
    """``<stem>_phys.txt`` next to the GLB — the existing sidecar, or one
    freshly dumped from the model's source VPK (or loose .vmdl_c) via VRF
    ``-b PHYS``. The dump is written beside the GLB so later runs find it
    without re-dumping. Returns None when it can't be produced.
    """
    log = on_log or (lambda _m, _l="INFO": None)
    glb_path = Path(glb_path)
    dest = glb_path.with_name(f"{glb_path.stem}_phys.txt")
    if dest.is_file():
        return dest
    if not vrf_exe or not source_vpk:
        return None
    vrf_exe, source_vpk = Path(vrf_exe), Path(source_vpk)
    if not vrf_exe.is_file() or not source_vpk.is_file():
        log("PHYS auto-dump skipped: VRF CLI or source VPK path invalid",
            "WARNING")
        return None
    from .vrf_runner import dump_phys_blocks
    stem = glb_path.stem
    filt = _vmdl_vpk_path(glb_path, gltf) or stem
    log(f"no PHYS sidecar - dumping from {source_vpk.name} "
        f"(filter '{filt}')...", "INFO")
    blocks = dump_phys_blocks(vrf_exe, source_vpk, filt,
                              on_log=lambda m: log(m, "INFO"))
    text = None
    for p, t in blocks.items():
        if p.lower().endswith(f"/{stem.lower()}.vmdl_c"):
            text = t
            break
    if text is None and len(blocks) == 1:
        text = next(iter(blocks.values()))
    if text is None:
        log(f"PHYS auto-dump found no '{stem}.vmdl_c' in {source_vpk.name} "
            f"({len(blocks)} model(s) matched the filter)", "WARNING")
        return None
    dest.write_text(text, encoding="utf-8")
    log(f"wrote PHYS sidecar: {dest.name}", "SUCCESS")
    return dest


def build_ragdoll_collision(
    *,
    skin,
    all_arrays: List[dict],
    out_dir: Path,
    name_base: str,
    phys_data=None,
    physics_glb: Optional[Path] = None,
    total_mass: float = presets.DEFAULT_TOTAL_MASS,
    constraint_source: str = "s2",
    prebuilt_hulls=None,
    constraint_rows: Optional[List[str]] = None,
    phys_smd_name: Optional[str] = None,
    on_log: Optional[LogFn] = None,
) -> Tuple[Optional[List[str]], Optional[List[str]], Optional[str]]:
    """``($collisionjoints lines, $hbox lines, error)`` + physics SMD on disk.

    PHYS-exact path when ``phys_data`` is given (real Rubikon shapes/limits,
    plus hitboxes); weight-derived hulls otherwise (optionally recovering the
    real hull split from ``physics_glb``; no hitboxes on that path).
    """
    log = on_log or (lambda _m, _l="INFO": None)
    _rlog = lambda m: log(m, "INFO")
    coord = CoordinateMode(scale=1.0, swap_axes=False)
    phys_smd_name = phys_smd_name or f"{name_base}_physics.smd"
    phys_smd_path = Path(out_dir) / phys_smd_name

    if phys_data is not None:
        # same rename rule build_ragdoll_phys applies internally: the skin
        # may already carry ValveBiped names (retarget/conform) while the
        # PHYS parts always name Source 2 bones
        present = {j.name for j in skin.joints}

        def rename(n: str) -> str:
            return n if n in present else valvebiped_name(n)

        mass_bias: Dict[str, float] = {}
        for part in phys_data.parts:
            bias = presets.DISTAL_MASS_BIAS.get(valvebiped_name(part.bone))
            if bias:
                mass_bias[rename(part.bone)] = bias
        lines, err = build_ragdoll_phys(
            phys=phys_data, skin=skin, coord=coord,
            phys_smd_path=phys_smd_path, phys_smd_name=phys_smd_name,
            total_mass=total_mass, generate_smd=True,
            constraint_source=constraint_source, mass_bias=mass_bias,
            on_log=_rlog)
        if lines is None:
            return None, None, err
        return lines, build_hitbox_lines(phys_data, rename), None

    mesh, vj, vw = concat_prims(all_arrays)
    physics_mesh = load_glb_tri_mesh(physics_glb) if physics_glb else None
    # A conformed rig carries true ValveBiped bone axes, so Valve's anatomical
    # rows apply: elbows and knees become real hinges instead of the symmetric
    # fallback's three-degrees-of-freedom, which lets them bend sideways.
    donor_axes = constraint_source == "valvebiped_donor"
    lines, err = build_ragdoll(
        render_mesh=mesh, vertex_joints=vj, vertex_weights=vw, skin=skin,
        coord=coord, phys_smd_path=phys_smd_path,
        phys_smd_name=phys_smd_name, total_mass=total_mass,
        generate_smd=True, physics_mesh=physics_mesh,
        prebuilt_hulls=prebuilt_hulls,
        constraint_rows=constraint_rows,
        constraint_source=constraint_source,
        rotdamping=(presets.DONOR_ROTDAMPING if donor_axes
                    else presets.DEFAULT_ROTDAMPING),
        on_log=_rlog)
    if lines is None:
        return None, None, err
    return lines, None, None


def _find_clip(clips, needle: str):
    """Shortest clip name containing the needle - "zombie_idle_01" beats
    scripted-scene one-offs like "rhc_ss_intro_..._pre_idle"."""
    hits = [c for c in clips if needle in (c.name or "").lower()]
    return min(hits, key=lambda c: (len(c.name or ""), c.name)) if hits else None


def _frame_worlds(skin, frame) -> Dict[str, np.ndarray]:
    """Name-keyed world matrices of one sampled frame (FK on this skin)."""
    world: Dict[int, np.ndarray] = {}

    def fk(i: int) -> np.ndarray:
        if i in world:
            return world[i]
        t, r, _s = frame[i]
        m = np.eye(4)
        m[:3, :3] = _q2m(np.asarray(r, dtype=np.float64))
        m[:3, 3] = np.asarray(t, dtype=np.float64)
        p = skin.joints[i].parent_joint_idx
        world[i] = m if p is None else fk(p) @ m
        return world[i]

    for i in range(len(skin.joints)):
        fk(i)
    return {skin.joints[i].name: world[i] for i in range(len(skin.joints))}


def _frame_from_worlds(skin, worlds: Dict[str, np.ndarray]) -> dict:
    """Re-base name-keyed world matrices onto this (pruned) hierarchy."""
    frame = {}
    for i, j in enumerate(skin.joints):
        W = worlds[j.name]
        p = j.parent_joint_idx
        L = W if p is None else np.linalg.inv(
            worlds[skin.joints[p].name]) @ W
        frame[i] = (L[:3, 3].copy(), _mat33_to_quat(L[:3, :3]), np.ones(3))
    return frame


def port_ragdoll(
    glb_path: Path,
    out_dir: Path,
    modelname: str,
    cdmaterials: str,
    *,
    surfaceprop: str = presets.DEFAULT_SURFACEPROP,
    skeleton_naming: str = "native",      # native | auto | valvebiped
    constraint_source: str = "s2",        # s2 | valvebiped_donor
    total_mass: Optional[float] = None,
    donor_mdl: Optional[Path] = None,
    physics_glb: Optional[Path] = None,   # None = auto-discover
    phys_dump: Optional[Path] = None,     # None = auto-discover
    vrf_exe: Optional[Path] = None,       # with source_vpk: auto-dump PHYS
    source_vpk: Optional[Path] = None,
    studiomdl: Optional[Path] = None,
    studiomdl_game: Optional[Path] = None,
    on_log: Optional[LogFn] = None,
) -> dict:
    """Generate (and optionally compile) a full ragdoll source set."""
    log = on_log or (lambda _m, _l="INFO": None)
    _rlog = lambda m: log(m, "INFO")
    glb_path, out_dir = Path(glb_path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    gltf, buf = read_glb(glb_path)
    if not is_s2v_native_gltf(gltf):
        log("GLB does not look like a Source 2 Viewer export - the pipeline "
            "assumes native vmdl units (inches, no axis swap); results may "
            "be scaled wrong", "WARNING")
    skin = parse_skin(gltf, buf)
    if skin is None:
        raise ValueError("GLB has no skin - a ragdoll needs a skinned mesh")
    mesh_sets, all_arrays = collect_skinned_prims(gltf, buf, log)
    clips = parse_clips(gltf, buf, skin)

    # sidecars ---------------------------------------------------------------
    auto_physics, auto_dump = discover_sidecars(glb_path)
    physics_glb = Path(physics_glb) if physics_glb else auto_physics
    phys_dump = Path(phys_dump) if phys_dump else auto_dump
    if phys_dump is None:
        phys_dump = ensure_phys_sidecar(glb_path, vrf_exe=vrf_exe,
                                        source_vpk=source_vpk, gltf=gltf,
                                        on_log=log)
    phys_data = load_phys_dump(phys_dump, on_log=_rlog) if phys_dump else None
    if phys_data is not None:
        log(f"PHYS sidecar: {len(phys_data.parts)} part(s), "
            f"{len(phys_data.joints)} joint(s) ({Path(phys_dump).name})",
            "INFO")
    else:
        log("no PHYS sidecar - weight-derived hulls (set a Source VPK to "
            "auto-dump it, or export via VRF Batch Export)", "WARNING")

    # skeleton naming --------------------------------------------------------
    humanoid = is_humanoid_skeleton([j.name for j in skin.joints])
    use_valvebiped = (skeleton_naming == "valvebiped"
                      or (skeleton_naming == "auto" and humanoid))
    if phys_data is None and not use_valvebiped:
        raise ValueError(
            "no PHYS sidecar and a native skeleton: weight-derived hulls "
            "need ValveBiped-mappable bones. Set the Source VPK so the "
            "<stem>_phys.txt sidecar can be auto-dumped, ship it next to "
            "the GLB yourself, or pick ValveBiped naming.")
    if use_valvebiped:
        if donor_mdl and Path(donor_mdl).is_file():
            try:
                conform_to_donor(skin, None, None, None, Path(donor_mdl),
                                 on_log=_rlog, extra_meshes=all_arrays)
            except Exception as exc:
                log(f"donor conform failed ({exc}); falling back to a "
                    "rename-only retarget", "WARNING")
                for line in retarget_skeleton_to_valvebiped(skin):
                    _rlog(line)
        else:
            for line in retarget_skeleton_to_valvebiped(skin):
                _rlog(line)
    if constraint_source == "valvebiped_donor" and not use_valvebiped:
        log("donor constraints need ValveBiped orientations - using "
            "converted Source 2 limits instead", "WARNING")
        constraint_source = "s2"

    # Pose sampling happens BEFORE the prune: the helper chain above the
    # pelvis carries real animation (the grunt's pelvis_translations moves
    # 41 units and rotates 120 degrees in every clip), so kept bones'
    # frames are converted to world space now and re-based onto the pruned
    # hierarchy afterwards.
    idle_clip = _find_clip(clips, "idle")
    rag_clip = _find_clip(clips, "ragdoll")
    idle_worlds = rag_worlds = None
    if idle_clip is not None:
        frames, _n = sample_clip(idle_clip, skin, CLIP_FPS)
        idle_worlds = _frame_worlds(skin, frames[0])
        log(f'idle pose from clip "{idle_clip.name}"', "INFO")
    if rag_clip is not None:
        frames, _n = sample_clip(rag_clip, skin, CLIP_FPS)
        rag_worlds = _frame_worlds(skin, frames[0])

    # prune helper bones (the srcds-verified zombie_root_motion rule):
    # ancestors of the PHYS root, plus every ROOT-LEVEL SUBTREE that carries
    # no skin weights and no physics part. HLA rigs hang curve/IK helper
    # rigs at the skeleton root as siblings of the pelvis (zombie_root_motion,
    # ankle IK targets, the grunt's feet_offset_ground_root tree) — bones
    # outside the solid tree make GMod pin the root solid at the world
    # origin and the ragdoll crumples on the first simulated tick.
    present = {j.name for j in skin.joints}
    rename = (lambda n: n if n in present else valvebiped_name(n))
    prune: List[int] = []
    if phys_data is not None:
        prune.extend(bones_above_phys_root(skin, phys_data, rename))
    part_bones = ({rename(p.bone) for p in phys_data.parts}
                  if phys_data is not None else set())
    weighted: set = set()
    for pr in all_arrays:
        used = pr["vj"][np.asarray(pr["vw"]) > 1e-6]
        weighted.update(int(v) for v in np.unique(used))
    children: Dict[Optional[int], List[int]] = {}
    for i, j in enumerate(skin.joints):
        children.setdefault(j.parent_joint_idx, []).append(i)
    prune_set = set(prune)

    def _effective_root(i: int) -> bool:
        # a bone whose every ancestor is already pruned becomes a root in
        # the compiled skeleton (prune_skin_joints reparents through them)
        p = skin.joints[i].parent_joint_idx
        while p is not None and p in prune_set:
            p = skin.joints[p].parent_joint_idx
        return p is None

    for root in (i for i in range(len(skin.joints))
                 if i not in prune_set and _effective_root(i)):
        subtree = [root]
        stack = list(children.get(root, []))
        while stack:
            n = stack.pop()
            subtree.append(n)
            stack.extend(children.get(n, []))
        keep = any(i in weighted or skin.joints[i].name in part_bones
                   for i in subtree)
        if not keep:
            prune.extend(i for i in subtree if i not in prune_set)
            prune_set.update(subtree)
    if prune:
        for i in prune:
            _rlog(f"pruning helper bone '{skin.joints[i].name}'")
        vj_all = np.vstack([pr["vj"] for pr in all_arrays])
        vj_all = prune_skin_joints(skin, prune, vj_all, on_log=_rlog)
        off = 0
        for pr in all_arrays:
            n = len(pr["vj"])
            pr["vj"] = vj_all[off:off + n]
            off += n

    # reference SMDs ---------------------------------------------------------
    name_base = glb_path.stem
    smd_of_set: List[Tuple[str, str, int]] = []
    total_verts = 0
    for mname, prims in mesh_sets:
        safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in mname)
        smd = f"ref_{safe}.smd"
        _write_ref_smd(out_dir / smd, skin, prims)
        nverts = sum(len(p["vertices"]) for p in prims)
        smd_of_set.append((safe, smd, nverts))
        total_verts += nverts
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

    # collision --------------------------------------------------------------
    mass = presets.DEFAULT_TOTAL_MASS if total_mass is None else float(total_mass)
    coll_lines, hbox_lines, err = build_ragdoll_collision(
        skin=skin, all_arrays=all_arrays, out_dir=out_dir,
        name_base=name_base, phys_data=phys_data, physics_glb=physics_glb,
        total_mass=mass, constraint_source=constraint_source, on_log=log)
    if coll_lines is None:
        raise ValueError(f"ragdoll collision build failed: {err}")

    # sequences: one static frame of a real posed clip beats the raw rest
    # pose (verified on the zombie - HLA never displays the rest pose, and a
    # static frame cannot fling non-simulated attachments around). Frames
    # were sampled pre-prune; re-base them onto the pruned hierarchy.
    coord = CoordinateMode(scale=1.0, swap_axes=False)
    if idle_worlds is not None:
        idle_frames = [_frame_from_worlds(skin, idle_worlds)]
    else:
        idle_frames = [derive_bind_pose_local(skin)]
        log("no idle clip in the GLB - idle pose = bind pose", "INFO")
    SmdAnimationExporter.write_animation(
        skin, idle_frames, out_dir / "a_idle.smd", coord)

    rag_frames = ([_frame_from_worlds(skin, rag_worlds)]
                  if rag_worlds is not None else idle_frames)
    SmdAnimationExporter.write_animation(
        skin, rag_frames, out_dir / "a_ragdoll.smd", coord)

    # QC ---------------------------------------------------------------------
    L: List[str] = [f'$modelname "{modelname}"',
                    f'$cdmaterials "{cdmaterials}"']
    if total_verts > 65536:
        # StudioMDL CE honors $maxverts; older builds reject the command, so
        # only emit it when the model actually exceeds MAXSTUDIOVERTS
        L.append(f'$maxverts {total_verts}')
    L.append(f'$surfaceprop "{surfaceprop}"')
    L.append("")
    L.extend(derive_definebone_lines(skin, coord))
    L.append("")
    L.extend(bodygroup_lines(groups))
    L.append("")
    if hbox_lines:
        L.append('$hboxset "default"')
        L.extend(hbox_lines)
        L.append("")
    # all-local sequences on the model's own skeleton: no rotate compensation
    # (parity with the verified zombie ragdoll QC)
    L.append('$sequence "idle" "a_idle.smd" fps 1 loop')
    L.append('$sequence "ragdoll" "a_ragdoll.smd" fps 30 '
             'activity "ACT_DIERAGDOLL" 1')
    L.append("")
    L.extend(coll_lines)
    qc_path = out_dir / f"{name_base}_ragdoll.qc"
    qc_path.write_text("\n".join(L) + "\n", encoding="ascii")
    parts = len(phys_data.parts) if phys_data is not None else 0
    log(f"QC written: {qc_path.name} ({len(skin.joints)} bones, "
        f"{len(groups)} bodygroup(s) from {len(smd_of_set)} mesh(es))",
        "SUCCESS")

    compiled = False
    if studiomdl:
        compiled = compile_qc(qc_path, Path(studiomdl),
                              Path(studiomdl_game), on_log=log)

    return dict(qc=str(qc_path), bones=len(skin.joints), parts=parts,
                compiled=compiled)
