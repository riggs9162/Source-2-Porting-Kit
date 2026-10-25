"""Preserve a Source 2 model's native skeleton, authored sequences and metadata."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import trimesh

from s2kit.pipelines.glb_skin import collect_skinned_prims, read_glb
from s2kit.pipelines.gltf_animation import (
    SOURCE2_BASIS, SOURCE2_BASIS_QUAT, SOURCE_UNITS_PER_METRE, SPACE_METRES,
    derive_bind_pose_local, parse_clips, parse_skin, position_space,
)
from s2kit.pipelines.gltf_smd import (
    ConversionOptions, ModelSetScanner, convert_model_set,
)
from s2kit.pipelines.porting import check_cancelled
from s2kit.pipelines.ragdoll_animations import sample_authored_clip
from s2kit.pipelines.smd_animation_export import (
    CoordinateMode, SmdAnimationExporter, SmdSkeletalExporter,
    derive_definebone_lines,
)
from s2kit.pipelines.studiomdl import compile_qc


@dataclass
class NativePortResult:
    """Sources and fidelity measurements produced by a native model port."""

    qc: Path
    files: list[Path] = field(default_factory=list)
    bones: int = 0
    sequences: int = 0
    attachments: int = 0
    hitboxes: int = 0
    warnings: list[str] = field(default_factory=list)
    compiled: bool = False
    ragdoll_qc: Path | None = None
    compatibility_bones: list[str] = field(default_factory=list)
    physics: dict = field(default_factory=dict)
    dynamic_prop: bool = False


def coordinate_mode(gltf: dict) -> CoordinateMode:
    """Convert the mesh and skeleton together from the exporter's measured space."""
    if position_space(gltf) == SPACE_METRES:
        return CoordinateMode(SOURCE_UNITS_PER_METRE, True, SOURCE2_BASIS_QUAT)
    if not position_space(gltf):
        raise ValueError("Native port requires a Source 2 Viewer export with known units")
    return CoordinateMode()


def validate_native_data(gltf: dict, skin, clips) -> None:
    """Reject unsupported data instead of silently changing a native skeleton."""
    if len(skin.joints) > 256:
        raise ValueError("Garry's Mod supports at most 256 bones")
    names = [j.name.lower() for j in skin.joints]
    if len(names) != len(set(names)):
        raise ValueError("Source 1 requires unique case-insensitive bone names")
    for joint in skin.joints:
        if not np.allclose(joint.rest_scale, 1.0, atol=1e-5):
            raise ValueError(f"SMD cannot preserve bind scale on {joint.name}")
    for clip in clips:
        for (_, channel), (_, values, interpolation) in clip.channels.items():
            if channel == "scale" and not np.allclose(values, 1.0, atol=1e-5):
                raise ValueError(f"SMD cannot preserve animated scale in {clip.name}")
            if interpolation not in ("LINEAR", "STEP"):
                raise ValueError(f"Unsupported interpolation {interpolation} in {clip.name}")
    for mesh in gltf.get("meshes", []):
        for primitive in mesh.get("primitives", []):
            if "JOINTS_1" in primitive.get("attributes", {}):
                raise ValueError("Extra skin influence sets require explicit weight reduction")


def _filename(name: str, index: int) -> str:
    """Give every exact sequence name a distinct portable filename."""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", name).strip("_") or "clip"
    return f"anim_{index:03d}_{safe}.smd"


def load_lod_sources(path: Path) -> list[tuple[float, Path]]:
    """Resolve an optional LOD manifest relative to its own directory."""
    entries = json.loads(path.read_text(encoding="utf-8"))
    return [(float(entry["distance"]), (path.parent / entry["source"]).resolve())
            for entry in entries]


def sequence_event_qc(events: list[dict], frame_count: int) -> list[str]:
    """Validate explicit Source 1 event translations before placing them in QC."""
    result = []
    for event in events:
        frame, number, options = event.get("frame"), event.get("event"), event.get("options", "")
        if type(frame) is not int or not 0 <= frame < frame_count:
            raise ValueError(f"Animation event frame is outside this clip: {frame}")
        if type(number) is not int or not 1 <= number <= 65535:
            raise ValueError(f"Invalid Source 1 animation event number: {number}")
        if not isinstance(options, str) or any(character in options for character in ('"', '\\', '\r', '\n', '\x00')):
            raise ValueError("Animation event options cannot be represented safely in QC")
        if len(options.encode("utf-8")) > 63:
            raise ValueError("Source 1 animation event options must fit in 63 UTF-8 bytes")
        result.append(f'{{ event {number} {frame} "{options}" }}')
    return result


def add_model_origin_bone(path: Path, index: int, name: str) -> None:
    """Append an identity root to every SMD frame without moving authored bones."""
    text = path.read_text(encoding="utf-8")
    prefix, nodes = text.split("nodes\n", 1)
    node_lines, remainder = nodes.split("end\n", 1)
    if any(int(line.split()[0]) == index for line in node_lines.splitlines() if line.strip()):
        raise ValueError(f"Compatibility bone index {index} already exists in {path}")
    before, skeleton = remainder.split("skeleton\n", 1)
    frames, tail = skeleton.split("end\n", 1)
    output = []
    in_frame = False
    for line in frames.splitlines():
        if line.startswith("time "):
            if in_frame:
                output.append(f"{index} 0 0 0 0 0 0")
            in_frame = True
        output.append(line)
    if not in_frame:
        raise ValueError(f"SMD has no animation frames: {path}")
    output.append(f"{index} 0 0 0 0 0 0")
    path.write_text(prefix + "nodes\n" + node_lines + f'{index} "{name}" -1\nend\n' +
                    before + "skeleton\n" + "\n".join(output) + "\nend\n" + tail,
                    encoding="utf-8", newline="\n")


def _kinematic_root(document: dict, skin) -> str:
    """Choose the authored collision bone containing the most other physics bones."""
    names = document["m_boneNames"]
    parents = document.get("m_boneParents") or []
    if not parents:
        raise ValueError("Physics part-to-bone mapping is missing")
    if any(type(index) is not int or not 0 <= index < len(names) for index in parents):
        raise ValueError("Physics part-to-bone mapping contains an invalid name index")
    selected = {names[index] for index in parents}
    indices = {joint.name: index for index, joint in enumerate(skin.joints)}
    missing = selected - set(indices)
    if missing:
        raise ValueError(f"Physics bones are absent from the render skeleton: {sorted(missing)}")
    scores = dict.fromkeys(selected, 0)
    for name in selected:
        index = indices[name]
        while index is not None:
            joint = skin.joints[index]
            if joint.name in scores:
                scores[joint.name] += 1
            index = joint.parent_joint_idx
    return max(scores, key=lambda name: (scores[name], -indices[name]))


def write_native_reference(path, gltf, buffer, skin, coord) -> int:
    """Write original primitive normals, UVs and weights without mesh-library smoothing."""
    from s2kit.pipelines.dmx_mesh import smd_vertex_line

    ok, error = SmdAnimationExporter.write_animation(
        skin, [derive_bind_pose_local(skin)], path, coord)
    if not ok:
        raise ValueError(error)
    _, primitives = collect_skinned_prims(gltf, buffer, lambda *args: None)
    triangles = 0
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write("triangles\n")
        for primitive in primitives:
            vertices = primitive["vertices"] * coord.scale
            normals = primitive["normals"]
            if normals is None:
                raise ValueError("Native export requires authored vertex normals")
            if coord.swap_axes:
                vertices = vertices @ SOURCE2_BASIS[:3, :3].T
                normals = normals @ SOURCE2_BASIS[:3, :3].T
            for face in primitive["indices"].reshape(-1, 3):
                output.write(primitive["material"] + "\n")
                for vertex in face:
                    texcoord = primitive["uv"][vertex]
                    values = [*vertices[vertex], *normals[vertex], texcoord[0], 1.0 - texcoord[1]]
                    links = [(int(joint), float(weight)) for joint, weight in
                             zip(primitive["vj"][vertex], primitive["vw"][vertex]) if weight > 0]
                    if not links:
                        raise ValueError("Native mesh contains an unweighted vertex")
                    output.write(smd_vertex_line(values, links))
                triangles += 1
        output.write("end\n")
    return triangles


def port_native_model(
    glb_path: Path, out_dir: Path, *, modelname: str,
    cdmaterials: str = "models/", surfaceprop: str = "metal",
    metadata_path: Path | None = None, ragdoll_bones: list[str] | None = None,
    dynamic_prop: bool = False,
    lod_sources: list[tuple[float, Path]] | None = None,
    ragdoll_mass: float = 25.0, ragdoll_limit: float = 25.0,
    studiomdl: Path | None = None, game: Path | None = None,
    on_log=None, cancel: threading.Event | None = None,
) -> NativePortResult:
    """Write a native model, optionally restricted to authored animation and collision.

    Dynamic props omit the generated ragdoll bind sequence and cannot request
    a ragdoll companion. Authored sequences and bone-driven collision remain.
    """
    from s2kit.pipelines.model_metadata import (
        attachment_qc_lines, hitbox_qc_lines, load_model_metadata,
        poseparameter_qc_lines, sequence_qc_options, weightlist_qc_lines,
        parse_kv3_documents,
    )

    if dynamic_prop and ragdoll_bones:
        raise ValueError("Dynamic prop mode cannot generate a ragdoll companion; clear ragdoll_bones")
    log = on_log or (lambda message, level="INFO": None)
    glb_path, out_dir = Path(glb_path), Path(out_dir)
    gltf, buffer = read_glb(glb_path)
    skin = parse_skin(gltf, buffer)
    if skin is None:
        raise ValueError("Native model port requires a skinned model")
    clips = parse_clips(gltf, buffer, skin)
    validate_native_data(gltf, skin, clips)
    coord = coordinate_mode(gltf)
    metadata = load_model_metadata(metadata_path or glb_path)
    events_path = glb_path.with_suffix(".events.json")
    event_sidecar = json.loads(events_path.read_text(encoding="utf-8")) if events_path.is_file() else {}
    event_sequences = event_sidecar.get("sequences", {})
    unknown_events = set(event_sequences) - {clip.name for clip in clips}
    if unknown_events:
        raise ValueError(f"Event translations refer to absent sequences: {sorted(unknown_events)}")
    if not metadata.animations and clips:
        raise ValueError("Authored animation metadata is missing; export again with VRF Batch Export")
    if any(animation.delta for animation in metadata.animations.values()):
        raise ValueError("Additive Source 2 clips need explicit composition before native SMD export")
    origin_bone = ""
    if any(attachment.root_transform for attachment in metadata.attachments):
        if len(skin.joints) >= 256:
            raise ValueError("Model-space attachments need one compatibility bone within GMod's 256-bone limit")
        origin_bone = "__s2_model_origin"
        while origin_bone.lower() in {joint.name.lower() for joint in skin.joints}:
            origin_bone += "_"
    phys_path = glb_path.with_name(glb_path.stem + "_phys.txt")
    bound_physics = None
    if phys_path.is_file():
        for document in parse_kv3_documents(phys_path.read_text(encoding="utf-8-sig")):
            if document.get("m_joints"):
                raise ValueError("Source 2 physics joints require explicit constraint conversion")
            if document.get("m_boneNames"):
                if bound_physics is not None:
                    raise ValueError("Multiple bone-bound physics aggregates need explicit composition")
                bound_physics = document
    native_root = skin.joints[skin.root_joint_idx].name
    compiler_root = _kinematic_root(bound_physics, skin) if bound_physics is not None else native_root
    if lod_sources is None and glb_path.with_suffix(".lods.json").is_file():
        lod_sources = load_lod_sources(glb_path.with_suffix(".lods.json"))
    model_set = next((s for s in ModelSetScanner(glb_path.parent, False).find_sets()
                      if s.render_path == glb_path), None)
    if model_set is None:
        raise ValueError(f"Could not discover model set for {glb_path}")
    options = ConversionOptions(
        export_animations=False, preserve_folders=False, cdmaterials=cdmaterials,
        static_surfaceprop=surfaceprop, auto_surfaceprop=False,
        collision_from_render=False, decompose_collision=False,
        export_physics=bound_physics is None,
    )
    converted = convert_model_set(model_set, options, input_root=glb_path.parent,
                                  output_root=out_dir, on_log=lambda m: log(m), cancel=cancel)
    if not converted.ok:
        raise ValueError(converted.reason)
    triangles = write_native_reference(out_dir / f"{glb_path.stem}.smd",
                                       gltf, buffer, skin, coord)
    if origin_bone:
        add_model_origin_bone(out_dir / f"{glb_path.stem}.smd", len(skin.joints), origin_bone)
    log(f"Native reference: {triangles} triangles with authored vertex normals")
    qc = out_dir / f"{glb_path.stem}.qc"
    generated = qc.read_text(encoding="utf-8")
    collision_at = generated.find("$collisionmodel")
    collision = generated[collision_at:] if collision_at >= 0 else ""
    result = NativePortResult(qc=qc, files=list(converted.files), bones=len(skin.joints),
                              dynamic_prop=dynamic_prop)
    result.warnings.extend(metadata.warnings)
    result.warnings = [warning for warning in result.warnings
                       if not (warning.endswith(": Source 2 animation events require manual translation")
                               and event_sequences.get(warning.split(":", 1)[0]))]
    result.warnings.extend(event_sidecar.get("warnings", []))
    if origin_bone:
        result.compatibility_bones.append(origin_bone)
        result.warnings = [warning for warning in result.warnings
                           if "model-space attachment requires a stationary identity root bone" not in warning]
        log(f"Added stationary compatibility bone {origin_bone} for model-space attachments")
    if bound_physics is not None:
        from s2kit.pipelines.native_physics import write_kinematic_physics

        physics_smd = out_dir / f"{glb_path.stem}_physics.smd"
        physics_result = write_kinematic_physics(
            phys_path, physics_smd, out_dir / f"{glb_path.stem}.smd",
            skin.joint_names, compiler_root)
        collision = "\n".join(physics_result.qc_lines) + "\n"
        result.physics = physics_result.report
        result.warnings.extend(physics_result.warnings)
        result.files.append(physics_smd)
        log(f"Native kinematic collision: {result.physics['part_count']} bodies, {result.physics['hull_count']} hulls")
    overlay_path = glb_path.with_suffix(".overlays.json")
    surfaces = json.loads(overlay_path.read_text(encoding="utf-8")).get("surfaces", []) if overlay_path.is_file() else []
    from s2kit.pipelines.bone_overlays import overlay_skin_material, write_bone_overlays

    overlay_files = write_bone_overlays(out_dir / f"{glb_path.stem}.smd", surfaces, out_dir)
    result.files.extend(overlay_files)
    weightlists = weightlist_qc_lines(metadata)
    if origin_bone:
        weightlists = [line if line != '}' else f'    "{origin_bone}" 1\n}}'
                       for line in weightlists]
    lines = [f'$modelname "{modelname}"', f'$cdmaterials "{cdmaterials}"',
             f'$surfaceprop "{surfaceprop}"', '$contents "solid"',
             f'$body "body" "{glb_path.stem}.smd"',
             *derive_definebone_lines(skin, coord, 90.0),
             f'$root "{compiler_root}"',
             *attachment_qc_lines(metadata, model_space_bone=origin_bone), *hitbox_qc_lines(metadata),
             *weightlists, *poseparameter_qc_lines(metadata)]
    for surface, path in zip(surfaces, overlay_files):
        if surface.get("bodygroup", False):
            choices = [f'    studio "{path.name}"', "    blank"]
            if surface.get("default_off", False):
                choices.reverse()
            lines.extend([f'$bodygroup "{path.stem}"', "{", *choices, "}"])
        else:
            lines.append(f'$body "{path.stem}" "{path.name}"')
    if origin_bone:
        lines.append(f'$definebone "{origin_bone}" "" 0 0 0 0 90 0 0 0 0 0 0 0')
    maxverts = re.search(r"(?m)^\$maxverts\s+\d+", generated)
    if maxverts:
        lines.insert(1, maxverts.group(0))
    if len(model_set.skins) > 1:
        lines.extend(['$texturegroup "skinfamilies"', "{"])
        lines.extend("    { " + " ".join(f'"{material}"' for material in [*entry.materials,
                         *[overlay_skin_material(surface, index) for surface in surfaces]]) + " }"
                     for index, entry in enumerate(model_set.skins))
        lines.append("}")
    from s2kit.pipelines.dmx_mesh import write_lod_references

    lod_lines, lod_files, partitioned = write_lod_references(
        out_dir / f"{glb_path.stem}.smd", lod_sources or [], skin.joint_names, log)
    if partitioned:
        lines = [line for line in lines if not line.startswith("$body ")]
    lines.extend(lod_lines)
    result.files.extend(lod_files)
    result.attachments = len(metadata.attachments)
    result.hitboxes = len(metadata.hitboxes)
    bind_path = out_dir / "bind.smd"
    ok, error = SmdAnimationExporter.write_animation(
        skin, [derive_bind_pose_local(skin)], bind_path, coord)
    if not ok:
        raise ValueError(error)
    if origin_bone:
        add_model_origin_bone(bind_path, len(skin.joints), origin_bone)
    result.files.append(bind_path)
    lines.append('$animation "native_bind" "bind.smd" fps 30')
    sequence_audit = []
    for index, clip in enumerate(clips):
        check_cancelled(cancel)
        authored = metadata.animations.get(clip.name)
        if authored is None:
            raise ValueError(f"Missing authored metadata for sequence {clip.name}")
        frames, count, sampling = sample_authored_clip(clip, skin, authored)
        path = out_dir / _filename(clip.name, index)
        ok, error = SmdAnimationExporter.write_animation(skin, frames, path, coord)
        if not ok:
            raise ValueError(f"{clip.name}: {error}")
        if origin_bone:
            add_model_origin_bone(path, len(skin.joints), origin_bone)
        result.files.append(path)
        flags = sequence_qc_options(authored, include_delta=False)
        translated_events = sequence_event_qc(event_sequences.get(clip.name, []), count)
        animation_name = f"native_clip_{index:03d}"
        lines.append(f'$animation "{animation_name}" "{path.name}" fps {authored.fps:g}')
        lines.append(f'$sequence "{clip.name}" {{ "{animation_name}" ' +
                     " ".join([*flags, *translated_events]) + " }")
        sequence_audit.append(dict(name=clip.name, frames=count, fps=authored.fps,
                                   loop=authored.loop, delta=authored.delta,
                                   sampling=sampling,
                                   translated_events=len(translated_events)))
        log(f"Sequence {index + 1}/{len(clips)}: {clip.name}, {count} frames at {authored.fps:g} fps")
    if dynamic_prop and not clips:
        lines.append('$sequence "idle" "bind.smd" fps 1')
    if not dynamic_prop and not any(clip.name.lower() == "ragdoll" for clip in clips):
        lines.append('$sequence "ragdoll" "bind.smd" fps 30 activity "ACT_DIERAGDOLL" 1')
    qc.write_text("\n".join(lines) + "\n" + collision, encoding="utf-8")
    result.sequences = len(clips)
    if ragdoll_bones:
        ragdoll_smd = out_dir / "ragdoll_physics.smd"
        hulls = _generated_hulls(gltf, buffer, skin, coord, ragdoll_bones)
        _write_hulls(ragdoll_smd, skin, coord, hulls)
        ragdoll_lines = [f'$root "{native_root}"' if line.startswith('$root ') else line
                         for line in lines]
        ragdoll_lines[0] = f'$modelname "{Path(modelname).with_suffix("").as_posix()}_ragdoll.mdl"'
        ragdoll_lines.extend(_ragdoll_block(skin, hulls, ragdoll_smd.name,
                                           ragdoll_mass, ragdoll_limit))
        result.ragdoll_qc = out_dir / f"{glb_path.stem}_ragdoll.qc"
        result.ragdoll_qc.write_text("\n".join(ragdoll_lines) + "\n", encoding="utf-8")
        result.files.extend([ragdoll_smd, result.ragdoll_qc])
        result.warnings.append("The ragdoll companion uses generated convex hulls and joint limits; it is not authored Source 2 physics.")
    report = out_dir / "native_port_report.json"
    report.write_text(json.dumps(dict(source=str(glb_path), modelname=modelname,
                                      dynamic_prop=dynamic_prop,
                                      compiler_basis_yaw_degrees=90.0,
                                      overlays=surfaces,
                                      bones=skin.joint_names, sequences=sequence_audit,
                                      compatibility_bones=result.compatibility_bones,
                                      physics=result.physics,
                                      attachments=result.attachments, hitboxes=result.hitboxes,
                                      warnings=result.warnings), indent=2), encoding="utf-8")
    result.files.append(report)
    if studiomdl:
        result.compiled = compile_qc(qc, Path(studiomdl), Path(game or ""), on_log=log, cancel=cancel)
        if result.compiled and result.ragdoll_qc:
            result.compiled = compile_qc(result.ragdoll_qc, Path(studiomdl), Path(game or ""), on_log=log, cancel=cancel)
        if not result.compiled:
            raise ValueError("Native model compile failed; see the compiler log")
    return result


def _generated_hulls(gltf, buffer, skin, coord, selected: list[str]) -> dict[int, trimesh.Trimesh]:
    """Fit separate rigid convex bodies to the strongest weights of selected bones."""
    names = skin.joint_names
    missing = set(selected) - set(names)
    if missing:
        raise ValueError(f"Unknown ragdoll bones: {sorted(missing)}")
    chosen = {names.index(name) for name in selected}
    if skin.root_joint_idx not in chosen or not 2 <= len(chosen) <= 32:
        raise ValueError("Generated ragdolls need the skeleton root and 2 to 32 selected bones")
    mapping = []
    for index in range(len(names)):
        while index not in chosen:
            index = skin.joints[index].parent_joint_idx
            if index is None:
                raise ValueError("Every ragdoll bone must descend from the selected root")
        mapping.append(index)
    _, prims = collect_skinned_prims(gltf, buffer, lambda *args: None)
    points = {index: [] for index in chosen}
    for primitive in prims:
        used = np.unique(primitive["indices"])
        vertices = primitive["vertices"][used] * coord.scale
        if coord.swap_axes:
            vertices = vertices @ SOURCE2_BASIS[:3, :3].T
        weights = np.zeros((len(used), len(names)))
        for slot in range(primitive["vj"].shape[1]):
            mapped = np.asarray(mapping)[primitive["vj"][used, slot]]
            np.add.at(weights, (np.arange(len(used)), mapped), primitive["vw"][used, slot])
        dominant = weights.argmax(axis=1)
        for index in chosen:
            points[index].extend(vertices[dominant == index])
    hulls = {}
    for index, cloud in points.items():
        if len(cloud) < 4:
            raise ValueError(f"Too little geometry for ragdoll bone {names[index]}")
        hull = trimesh.convex.convex_hull(np.unique(np.round(cloud, 5), axis=0))
        if min(hull.extents) < 0.5 or hull.volume < 0.1:
            raise ValueError(f"Ragdoll bone {names[index]} has a degenerate hull; select its parent")
        hulls[index] = hull
    return hulls


def _write_hulls(path, skin, coord, hulls) -> None:
    """Write one rigid weight per collision vertex, preserving the render skeleton."""
    meshes, joints = [], []
    for index, hull in hulls.items():
        meshes.append(hull)
        row = np.zeros((len(hull.vertices), 4), dtype=np.int32)
        row[:, 0] = index
        joints.append(row)
    mesh = trimesh.util.concatenate(meshes)
    weights = np.zeros((len(mesh.vertices), 4))
    weights[:, 0] = 1.0
    ok, error = SmdSkeletalExporter.write_skinned(
        mesh, skin, np.vstack(joints), weights, path, "physics", coord,
        override_normals=np.vstack([h.vertex_normals for h in meshes]))
    if not ok:
        raise ValueError(error)


def _ragdoll_block(skin, hulls, filename, mass, limit) -> list[str]:
    """Conservative generated articulation with fixed limits and disabled self collision."""
    lines = [f'$collisionjoints "{filename}"', "{", f"    $mass {mass:g}",
             "    $inertia 2", "    $damping 0.05", "    $rotdamping 1.5",
             f'    $rootbone "{skin.joints[skin.root_joint_idx].name}"',
             "    $noselfcollisions"]
    for index in hulls:
        if index == skin.root_joint_idx:
            continue
        for axis in "xyz":
            lines.append(f'    $jointconstrain "{skin.joints[index].name}" {axis} limit {-limit:g} {limit:g} 1')
    return [*lines, "}"]
