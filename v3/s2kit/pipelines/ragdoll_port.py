"""Assemble faithful native ragdoll sources from authored appearance and physics."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from s2kit.pipelines.gltf_animation import (
    derive_bind_pose_local, load_buffer_bytes, parse_clips, parse_skin,
)
from s2kit.pipelines.glb_skin import read_glb
from s2kit.pipelines.model_metadata import (
    attachment_qc_lines, hitbox_qc_lines, load_model_metadata, sequence_qc_options,
    poseparameter_qc_lines, weightlist_qc_lines,
)
from s2kit.pipelines.native_port import add_model_origin_bone, coordinate_mode, validate_native_data
from s2kit.pipelines.porting import check_cancelled
from s2kit.pipelines.ragdoll_animations import sample_authored_clip
from s2kit.pipelines.smd_animation_export import (
    CoordinateMode, SmdAnimationExporter, derive_definebone_lines,
)


@dataclass
class RagdollPortResult:
    """Editable sources and measured feature decisions for a native ragdoll."""

    qc: Path
    files: list[Path] = field(default_factory=list)
    report: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def read_render_model(path: Path) -> tuple[dict, bytes]:
    """Read either supported glTF container without assuming its filename extension."""
    path = Path(path)
    if path.suffix.lower() == ".glb":
        return read_glb(path)
    document = json.loads(path.read_text(encoding="utf-8"))
    buffer = load_buffer_bytes(document, path.parent)
    if buffer is None:
        raise ValueError(f"Could not resolve the model buffer: {path}")
    return document, buffer


def _write_pose(path: Path, skin, frames) -> None:
    """Write complete animation samples in the prepared Source-inch skeleton."""
    ok, error = SmdAnimationExporter.write_animation(skin, frames, path, CoordinateMode())
    if not ok:
        raise ValueError(f"{path.name}: {error}")


def _clip_filename(index: int, name: str) -> str:
    """Give distinct source clips portable files while retaining their QC names."""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", name).strip("_")[:100] or "clip"
    return f"anim_{index:04d}_{safe}.smd"


def port_ragdoll(glb_path: Path, out_dir: Path, *, modelname: str,
                 cdmaterials: str = "models/", include_source_animations: bool = False,
                 friction_scale: float = 1.0, on_log=None,
                 cancel: threading.Event | None = None) -> RagdollPortResult:
    """Write an original-sized native ragdoll with individually audited adaptations."""
    from s2kit.pipelines.model_fidelity import load_model_fidelity, write_appearance
    from s2kit.pipelines.ragdoll_appearance import sync_appearance_skeleton
    from s2kit.pipelines.ragdoll_physics import prepare_ragdoll_physics
    from s2kit.pipelines.phy_audit import write_audit_manifest

    log = on_log or (lambda _message, _level="INFO": None)
    glb_path, out_dir = Path(glb_path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    document, buffer = read_render_model(glb_path)
    skin = parse_skin(document, buffer)
    if skin is None:
        raise ValueError("A ragdoll requires a skinned source model")
    coord = coordinate_mode(document)
    clips = parse_clips(document, buffer, skin) if include_source_animations else []
    validate_native_data(document, skin, clips)
    metadata = load_model_metadata(glb_path)
    fidelity = load_model_fidelity(glb_path)
    check_cancelled(cancel)
    physics_path = glb_path.with_name(glb_path.stem + "_phys.txt")
    physics = prepare_ragdoll_physics(
        physics_path if physics_path.is_file() else None, skin, coord,
        model_data=fidelity["source_data"], friction_scale=friction_scale)
    appearance = write_appearance(
        document, buffer, skin, coord, out_dir, fidelity=fidelity,
        modelname=modelname, cancel=cancel)
    from s2kit.pipelines.ragdoll_attachments import adapt_native_metadata, load_attachment_influences

    raw_attachments = (load_attachment_influences(fidelity, glb_path.parent)
                       if any(item.influences != 1 for item in metadata.attachments) else {})
    metadata, metadata_report = adapt_native_metadata(
        metadata, source_frames=appearance.addon_drivers.get("source_frames", {}),
        raw_attachments=raw_attachments)
    files = list(appearance.files)
    for path in files:
        if path.suffix.lower() == ".smd":
            physics.remap_smd(path)
    sync_appearance_skeleton(appearance)
    collision = out_dir / "collision.smd"
    collision_lines = physics.write_collision(collision)
    files.append(collision)
    combined = physics.skin
    bind = out_dir / "ragdoll.smd"
    _write_pose(bind, combined, [derive_bind_pose_local(combined)])
    files.append(bind)
    origin = ""
    if any(item.root_transform for item in metadata.attachments):
        if len(combined.joints) >= 256:
            raise ValueError("Model-space attachments need a compatibility bone within the 256-bone limit")
        origin = "__s2_model_origin"
        while origin.casefold() in {name.casefold() for name in combined.joint_names}:
            origin += "_"
        for path in files:
            if path.suffix.lower() == ".smd":
                add_model_origin_bone(path, len(combined.joints), origin)
    lines = [
        f'$modelname "{modelname}"', '$maxverts 65535',
        f'$cdmaterials "{cdmaterials}"', '$surfaceprop "flesh"', '$contents "solid"',
        *appearance.qc_lines, *derive_definebone_lines(combined, CoordinateMode(), 90.0),
        f'$root "{physics.root_bone}"',
        *attachment_qc_lines(metadata, model_space_bone=origin), *hitbox_qc_lines(metadata),
        *poseparameter_qc_lines(metadata), *weightlist_qc_lines(metadata),
        '$sequence "ragdoll" "ragdoll.smd" fps 30 activity "ACT_DIERAGDOLL" 1',
    ]
    if origin:
        lines.append(f'$definebone "{origin}" "" 0 0 0 0 90 0 0 0 0 0 0 0')
    warnings = [*physics.warnings, *appearance.report.get("warnings", [])]
    for key in ("attachments", "hitboxes"):
        for adaptation in metadata_report[key]["adaptations"]:
            warnings.append(f"{adaptation['name']}: {adaptation['reason']}")
    sequence_audit = []
    for index, clip in enumerate(clips):
        check_cancelled(cancel)
        authored = metadata.animation_for_clip(clip.name)
        if authored is None:
            raise ValueError(f"Missing authored metadata for animation {clip.name}")
        composed = fidelity.get("animation_export", {}).get("additive_composed", False)
        if authored.delta and not composed:
            warnings.append(f"{clip.name}: source additive channels require explicit composition; clip omitted")
            sequence_audit.append({"name": clip.name, "status": "unsupported", "reason": "additive_encoding"})
            continue
        frames, count, sampling = sample_authored_clip(clip, skin, authored)
        target = out_dir / _clip_filename(index, clip.name)
        remapped = [physics.remap_frame(frame) for frame in frames]
        _write_pose(target, combined, remapped)
        if origin:
            add_model_origin_bone(target, len(combined.joints), origin)
        files.append(target)
        name = clip.name if clip.name.casefold() != "ragdoll" else "source_ragdoll"
        if any(char in name for char in ('"', "\r", "\n", "\\")):
            raise ValueError(f"Unsafe source animation name: {name!r}")
        flags = sequence_qc_options(authored, include_delta=False)
        lines.append(f'$sequence "{name}" "{target.name}" ' + " ".join(flags))
        sequence_audit.append({"name": clip.name, "target_name": name,
                               "status": "adapted" if authored.delta else "preserved",
                               "additive_composed_over_bind": bool(authored.delta),
                               "frames": count, "fps": authored.fps, "loop": authored.loop,
                               "sampling": sampling})
        log(f"Animation {index + 1}/{len(clips)}: {clip.name}", "INFO")
    lines.extend(collision_lines)
    qc = out_dir / (glb_path.stem + ".qc")
    qc.write_text("\n".join(lines) + "\n", encoding="utf-8")
    files.append(qc)
    source_bind = physics.report.get("original_bone_count", len(skin.joints))
    report = {
        "version": 1, "source": str(glb_path), "modelname": modelname,
        "original_bones": source_bind, "compiled_bones": len(combined.joints) + bool(origin),
        "physics": physics.report, "appearance": appearance.report,
        "material_map": appearance.report.get("material_map", {}),
        "animations": {"requested": include_source_animations, "clips": sequence_audit},
        "attachments": metadata_report["attachments"],
        "hitboxes": metadata_report["hitboxes"],
        "engine_validation": "not_run", "warnings": list(dict.fromkeys(warnings)),
    }
    audit_manifest = write_audit_manifest(out_dir / "physics-audit-input.json", physics)
    files.append(audit_manifest)
    report["physics_audit_manifest"] = str(audit_manifest)
    if include_source_animations:
        from s2kit.pipelines.morph_animation import export_morph_animations

        exported = [item for item in sequence_audit if "target_name" in item]
        morphs = export_morph_animations(document, buffer,
            names={item["name"]: item["target_name"] for item in exported},
            durations={item["name"]: (item["frames"] - 1) / item["fps"] for item in exported},
            cancel=cancel)
        appearance.addon_drivers["morph_animations"] = morphs.clips
        report["animations"]["morphs"] = morphs.report
    if appearance.addon_drivers:
        from s2kit.pipelines.ragdoll_runtime import model_driver_files, write_model_driver

        driver = write_model_driver(out_dir, modelname, appearance.addon_drivers)
        report["addon_drivers"] = [str(driver)]
        files.extend(model_driver_files(driver))
    report_path = out_dir / "ragdoll-report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    files.append(report_path)
    for warning in report["warnings"]:
        log(warning, "WARNING")
    log(f"Wrote {physics.report['source_body_count']} physics bodies and {len(combined.joints)} bones", "SUCCESS")
    return RagdollPortResult(qc, files, report, report["warnings"])
