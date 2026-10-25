"""Serial, resumable Source 2 model batches - props and ragdolls - and self-contained GMod addons."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from s2kit.pipelines.porting import PortCancelled, check_cancelled


@dataclass(frozen=True)
class RagdollAsset:
    """A selected source model with an isolated extraction and output identity."""

    key: str
    source: str
    resource: str
    relative: str
    material_root: str = ""

    @property
    def compiled_input(self) -> bool:
        return Path(self.source).suffix.lower() in {".vpk", ".vmdl_c"}


def resource_path(value: str) -> PurePosixPath:
    """Validate a relative game path before using it beneath an output root."""
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or ":" in str(path) or not path.name:
        raise ValueError(f"Unsafe source resource path: {value}")
    return path


def _asset(source: Path, resource: str, relative: str, material_root: str = "") -> RagdollAsset:
    """Create a stable model identity independent of queue order."""
    identity = str(source.resolve()).casefold() + ":" + resource.casefold()
    key = hashlib.sha256(identity.encode()).hexdigest()[:16]
    relative_path = resource_path(relative).with_suffix("")
    if relative_path.parts[0] == "models":
        relative_path = PurePosixPath(*relative_path.parts[1:])
    return RagdollAsset(key, str(source.resolve()), resource, relative_path.as_posix(), material_root)


def discover_assets(options) -> list[RagdollAsset]:
    """Resolve pasted VRF URLs, exact compiled models, or exported model sets."""
    from s2kit.pipelines.gltf_smd import ModelSetScanner
    from s2kit.pipelines.vrf import build_export_jobs

    entries = options.source_urls.splitlines() if options.source_urls.strip() else [options.source]
    default = Path(options.source) if options.source else None
    jobs, problems = build_export_jobs(entries, default_input=default)
    if problems:
        raise ValueError("\n".join(problems))
    assets = []
    for job in jobs:
        source = job.input_path.resolve()
        if not source.exists():
            raise ValueError(f"Source does not exist: {source}")
        if source.suffix.lower() == ".vpk":
            selected = job.vpk_filepaths or [p.strip() for p in options.vpk_filter.split(",") if p.strip()]
            if not selected:
                raise ValueError("Select exact VMDL resources inside the VPK")
            for name in selected:
                path = resource_path(name)
                if path.suffix.lower() != ".vmdl_c":
                    raise ValueError(f"Model input must name an exact .vmdl_c resource: {name}")
                assets.append(_asset(source, path.as_posix(), path.as_posix()))
        elif source.suffix.lower() == ".vmdl_c":
            assets.append(_asset(source, "", source.name))
        else:
            root = source if source.is_dir() else source.parent
            model_root = root / "modelsrc" if (root / "modelsrc").is_dir() else root
            material_root = Path(options.material_source) if options.material_source else root / "materialsrc"
            for model in ModelSetScanner(model_root, recursive=True).find_sets():
                if source.is_file() and model.render_path.resolve() != source:
                    continue
                relative = model.render_path.relative_to(model_root).as_posix()
                assets.append(_asset(model.render_path, "", relative, str(material_root.resolve())))
    unique = {asset.key: asset for asset in assets}
    if not unique:
        raise ValueError("No selected model sources were found")
    names = [asset.relative.casefold() for asset in unique.values()]
    if len(names) != len(set(names)):
        raise ValueError("Selected resources collide under the output namespace")
    return list(unique.values())


def validate_options(options) -> None:
    """Reject ambiguous output names and writes overlapping installed inputs."""
    if not re.fullmatch(r"[a-z0-9_]+(?:/[a-z0-9_]+)*", options.namespace):
        raise ValueError("Output namespace needs lowercase letters, digits, underscores and separators")
    project = Path(options.project).resolve()
    if not str(options.project).strip():
        raise ValueError("Choose a workflow project folder")
    if options.model_type not in {"auto", "ragdoll", "prop"}:
        raise ValueError("Model type must be automatic, ragdoll or prop")
    for asset in discover_assets(options):
        source = Path(asset.source)
        root = source.parent
        if project == root or project.is_relative_to(root) or root.is_relative_to(project):
            raise ValueError("Choose a project folder outside the input tree")
    if options.material_source:
        material = Path(options.material_source).resolve()
        if not material.is_dir():
            raise ValueError("Material source must be an existing folder")
        if project == material or project.is_relative_to(material) or material.is_relative_to(project):
            raise ValueError("Choose a project folder outside the material input tree")
    if options.install:
        install = Path(options.gmod_addons).resolve()
        if not options.gmod_addons or install.name.casefold() != "addons" or not install.is_dir():
            raise ValueError("Installation requires the existing GMod addons folder")
        if install == project or project.is_relative_to(install) or install.is_relative_to(project):
            raise ValueError("Keep editable project sources outside the GMod addons folder")


def input_signature(options, assets: list[RagdollAsset]) -> str:
    """Stamp selected archives and companions without walking an entire game."""
    digest = hashlib.sha256(json.dumps(asdict(options), sort_keys=True).encode())
    files = set()
    for asset in assets:
        source = Path(asset.source)
        files.add(source)
        if source.suffix.lower() == ".vpk":
            prefix = source.stem.removesuffix("_dir")
            files.update(source.parent.glob(prefix + "_[0-9]*.vpk"))
        elif not asset.compiled_input:
            files.update(source.parent.glob(source.stem + ".*"))
            files.update(source.parent.glob(source.stem + "_phys.txt"))
            if source.suffix.casefold() == ".gltf":
                document = json.loads(source.read_text(encoding="utf-8"))
                from urllib.parse import unquote

                for entry in [*document.get("buffers", []), *document.get("images", [])]:
                    uri = entry.get("uri", "")
                    if uri and not uri.startswith("data:"):
                        dependency = source.parent / unquote(uri)
                        if dependency.is_file():
                            files.add(dependency)
            fidelity_path = source.with_suffix(".fidelity.json")
            if fidelity_path.is_file():
                fidelity = json.loads(fidelity_path.read_text(encoding="utf-8"))
                for name in fidelity.get("raw_blocks", {}).values():
                    dependency = source.parent / name
                    if dependency.is_file():
                        files.add(dependency)
                for dependency in (source.parent / (source.stem + "_fidelity")).rglob("*"):
                    if dependency.is_file():
                        files.add(dependency)
                if fidelity.get("morphs"):
                    from s2kit.pipelines.morph_atlas import morph_atlas_path

                    resolved = dict(fidelity, _sidecar_directory=str(source.parent))
                    for morph in fidelity["morphs"]:
                        if morph.get("m_pTextureAtlas"):
                            dependency = morph_atlas_path(resolved, morph)
                            if dependency.is_file():
                                files.add(dependency)
            if asset.material_root and Path(asset.material_root).is_dir():
                files.update(path for path in Path(asset.material_root).rglob("*") if path.is_file())
    if options.material_source:
        files.update(path for path in Path(options.material_source).rglob("*") if path.is_file())
    executables = {Path(value).resolve() for value in (options.compiler, options.vrf_executable)
                   if value and Path(value).is_file()}
    if any(asset.compiled_input for asset in assets):
        from s2kit.pipelines import vrf

        try:
            executables.add(vrf.resolve_vrf_executable(options.vrf_executable or None).resolve())
        except vrf.VrfRunnerError:
            pass
    for executable in executables:
        files.add(executable)
        files.update(executable.parent.glob("*.dll"))
    if options.game:
        from s2kit.pipelines.studiomdl import resolve_game_arg

        game = resolve_game_arg(options.game)
        if game is not None and (game / "gameinfo.txt").is_file():
            files.add(game / "gameinfo.txt")
    for path in sorted(files):
        stat = path.stat()
        digest.update(f"{path}|{stat.st_size}|{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()


def _equivalent_options(saved: dict, options) -> bool:
    """Compare actual filesystem locations while retaining every non-path setting exactly."""
    from s2kit.pipelines.workflow import WorkflowOptions

    previous, current = asdict(WorkflowOptions(**saved)), asdict(options)
    paths = ("source", "project", "material_source", "vrf_executable", "compiler", "game",
             "gmod_addons", "animation_cache")
    for values in (previous, current):
        for key in paths:
            if values[key]:
                values[key] = os.path.normcase(str(Path(values[key]).resolve()))
    return previous == current


def _paths(options, asset: RagdollAsset) -> tuple[Path, Path, Path]:
    """Return model extraction, editable sources and isolated addon payload."""
    project = Path(options.project).resolve()
    return (project / "extracted" / asset.key,
            project / "modelsrc" / asset.relative,
            project / "staged" / asset.key / "addon")


def _render_source(options, asset: RagdollAsset) -> Path:
    """Find the selected render model without accidentally porting companions."""
    if not asset.compiled_input:
        return Path(asset.source)
    extraction, _, _ = _paths(options, asset)
    expected = extraction / "modelsrc" / Path(asset.resource).with_suffix(".glb") if asset.resource else None
    if expected and expected.is_file():
        return expected
    candidates = [p for p in extraction.rglob("*.glb") if not p.stem.endswith("_physics")]
    basename = Path(asset.resource or asset.source).stem
    selected = [p for p in candidates if p.stem == basename]
    if len(selected) != 1:
        raise ValueError(f"Expected one exported render model for {asset.relative}; found {len(selected)}")
    return selected[0]


def _write_json(path: Path, value) -> Path:
    """Persist a stage's portable, inspectable result."""
    from s2kit.pipelines.workflow import atomic_json

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, value)
    return path


def _compiler_game(options, material_payload: Path | None = None) -> Path:
    """Compile into the workflow project while mounting configured tool resources."""
    from s2kit.pipelines.studiomdl import resolve_game_arg

    original = resolve_game_arg(options.game)
    if original is None:
        raise ValueError("Set the compiler game folder containing gameinfo.txt")
    game = Path(options.project).resolve() / "compiler-game"
    game.mkdir(parents=True, exist_ok=True)
    mount = original.as_posix()
    material_mount = f'            Game "{material_payload.as_posix()}"\n' if material_payload else ""
    text = ('"GameInfo"\n{\n    game "Source 2 Porting Kit"\n    FileSystem\n'
            '    {\n        SteamAppId 730\n        ToolsAppId 211\n        SearchPaths\n'
            '        {\n            Game "|gameinfo_path|."\n'
            f'{material_mount}            Game "{mount}"\n        }}\n    }}\n}}\n')
    target = game / "gameinfo.txt"
    if not target.is_file() or target.read_text(encoding="utf-8") != text:
        target.write_text(text, encoding="utf-8")
    return game


def _model_kind(options, asset: RagdollAsset) -> str:
    """Resolve whether a source becomes a ragdoll or a prop, detecting it when automatic."""
    if options.model_type != "auto":
        return options.model_type
    from s2kit.pipelines.prop_port import classify_model

    return classify_model(_render_source(options, asset))


def _built_kind(options, asset: RagdollAsset) -> str:
    """Read the kind a completed conversion recorded for its model."""
    descriptor = _paths(options, asset)[1] / "build.json"
    return json.loads(descriptor.read_text(encoding="utf-8")).get("kind", "ragdoll")


def _export(options, asset: RagdollAsset, destination: Path, animations: bool, log, cancel) -> None:
    """Run one isolated Source 2 Viewer export of a selected model."""
    from s2kit.pipelines import vrf

    executable = vrf.resolve_vrf_executable(explicit=options.vrf_executable or None)
    result = vrf.run_vrf_export(
        executable, Path(asset.source), destination, vpk_filepath=asset.resource or None,
        threads=1, include_animations=animations, compose_additive=animations,
        on_log=lambda message: log(message, "INFO"), cancel=cancel)
    check_cancelled(cancel)
    if result:
        raise ValueError(f"Source 2 Viewer exited with {result}")


def _extract(options, asset, log, cancel) -> list[Path]:
    """Export one asset independently so an unrelated resource cannot block it.

    Props always keep their authored clips. Ragdolls keep them only when
    requested, so an automatic export without clips is repeated with them once
    the model turns out to be an animated prop.
    """
    from s2kit.pipelines.prop_port import is_skinned

    destination, _, _ = _paths(options, asset)
    if destination.exists():
        destination.rename(destination.with_name(destination.name + f"-previous-{time.time_ns()}"))
    animations = options.include_source_animations or options.model_type == "prop"
    _export(options, asset, destination, animations, log, cancel)
    if not animations and _model_kind(options, asset) == "prop" and is_skinned(_render_source(options, asset)):
        log("Animated prop detected; exporting again with its authored clips", "INFO")
        shutil.rmtree(destination)
        _export(options, asset, destination, True, log, cancel)
    return sorted(p for p in destination.rglob("*") if p.is_file())


def _convert(options, asset, log, cancel, *, playermodel=False) -> list[Path]:
    """Write prop, native ragdoll or player sources and retain their complete conversion audit."""
    from s2kit.pipelines.ragdoll_port import port_ragdoll

    source = _render_source(options, asset)
    _, destination, _ = _paths(options, asset)
    modelname = f"{options.namespace}/{asset.relative}" + ("_pm.mdl" if playermodel else ".mdl")
    kind = "ragdoll" if playermodel else _model_kind(options, asset)
    calibration_path = Path(options.project) / "calibration" / "compiler_calibration.json"
    calibration = json.loads(calibration_path.read_text(encoding="utf-8")) if calibration_path.is_file() else {}
    friction_scale = float(calibration.get("friction_compile_scale", 1.0))
    if playermodel:
        from s2kit.pipelines.ragdoll_playermodel import port_ragdoll_playermodel

        destination = destination / "playermodel"
        result = port_ragdoll_playermodel(
            source, destination, modelname=modelname,
            cdmaterials=f"models/{options.namespace}/",
            animation_cache=Path(options.animation_cache) if options.animation_cache else None,
            friction_scale=friction_scale,
            include_source_animations=options.include_source_animations,
            on_log=log, cancel=cancel)
    elif kind == "prop":
        from s2kit.pipelines.prop_port import port_prop

        result = port_prop(source, destination, modelname=modelname,
                           cdmaterials=f"models/{options.namespace}/", on_log=log, cancel=cancel)
    else:
        result = port_ragdoll(
            source, destination, modelname=modelname,
            cdmaterials=f"models/{options.namespace}/",
            include_source_animations=options.include_source_animations,
            friction_scale=friction_scale,
            on_log=log, cancel=cancel)
    files = list(result.files)
    qcs = []
    if getattr(result, "animation_qc", None):
        qcs.append(str(result.animation_qc))
    qcs.append(str(result.qc))
    descriptor = _write_json(destination / "build.json", {
        "modelname": modelname, "qcs": qcs, "source": str(source), "kind": kind,
        "playermodel": playermodel, "report": getattr(result, "report", {}),
        "warnings": result.warnings,
    })
    return [*files, descriptor]


def _material_sources(options, asset) -> Path:
    """Resolve the extracted or explicitly supplied native material tree."""
    if options.material_source:
        return Path(options.material_source)
    if asset.compiled_input:
        return _paths(options, asset)[0] / "materialsrc"
    return Path(asset.material_root)


def _materials(options, asset, log, cancel) -> list[Path]:
    """Convert full resource-relative material paths into an isolated addon."""
    from s2kit.pipelines import materials, semantic_materials, vmat
    from s2kit.pipelines.model_fidelity import resource_token

    root = _material_sources(options, asset)
    _, destination, payload = _paths(options, asset)
    descriptor = json.loads((destination / "build.json").read_text(encoding="utf-8"))
    report = descriptor.get("report", {})
    required = report.get("material_map", {})
    aliases = report.get("material_aliases", {})
    for alias, source in aliases.items():
        resource_path(alias)
        if resource_token(source) not in required:
            raise ValueError(f"Unmapped material source for compiler alias {alias}: {source}")
    paths = sorted(root.rglob("*.vmat")) if root.is_dir() else []
    selected = [(path.relative_to(root).with_suffix("").as_posix(), path) for path in paths]
    if required:
        keys = {resource_token(str(key)) for key in required}
        selected = []
        missing = set()
        for key in sorted(keys):
            candidates = [root / (key + ".vmat"), root / (
                key.replace("/materials/", "/").replace("/material/", "/") + ".vmat")]
            resolved = list(dict.fromkeys(path for path in candidates if path.is_file()))
            if len(resolved) > 1:
                raise ValueError(f"Ambiguous extracted material resource: {key}")
            if resolved:
                selected.append((key, resolved[0]))
            else:
                missing.add(key)
        if missing:
            raise ValueError("Missing referenced VMATs: " + ", ".join(sorted(missing)))
    if not selected:
        raise ValueError(f"No referenced VMAT materials were found under {root}")
    written = []
    audits = []
    compiled_materials = asset.compiled_input and not options.material_source
    source_stamp = input_signature(options, [asset]) if compiled_materials else ""
    implementation_stamp = _stage_signature("materials") if compiled_materials else ""
    for relative, path in selected:
        check_cancelled(cancel)
        if compiled_materials:
            from s2kit.pipelines import vrf

            entry, provenance, sources = semantic_materials.resolve_material(
                resource=relative + ".vmat", raw_vmat=path, raw_root=root,
                source=Path(asset.source), project=Path(options.project),
                vrf_executable=vrf.resolve_vrf_executable(options.vrf_executable or None),
                source_signature=source_stamp, implementation_signature=implementation_stamp,
                log=lambda message: log(message, "INFO"), cancel=cancel)
            written.extend(sources)
        else:
            entry = vmat.VmatParser(texture_root=root).parse(path, root=root)
            provenance = semantic_materials.material_provenance(entry)
            provenance["mode"] = "explicit_material_source" if options.material_source else "exported_vmat"
        if not entry.buildable:
            raise ValueError(f"No resolved base color for {path}")
        target_resource = resource_path(required.get(relative, relative))
        material_path = f"models/{options.namespace}/{target_resource.parent.as_posix()}"
        settings = materials.MaterialOptions(
            emit_normal=options.emit_normal, translucent=entry.translucent,
            alphatest=entry.alphatest, tintable=entry.tintable)
        result = materials.build_material(
            entry.to_source(), payload / "materials" / material_path,
            target_resource.name, material_path, settings, log=log, cancel=cancel)
        written.extend(result.written)
        audits.append({"source": relative, "target": target_resource.as_posix(),
                       "status": "adapted", "notes": [*entry.warnings, *result.notes],
                       "shader": entry.shader, "target_shader": settings.shader,
                       "source_provenance": provenance})
    original_vmts = {
        payload / "materials" / "models" / options.namespace / (entry["target"] + ".vmt")
        for entry in audits
    }
    alias_audits = []
    alias_sources = {}
    for alias, source in sorted(aliases.items()):
        check_cancelled(cancel)
        token = resource_token(alias)
        source_token = resource_token(source)
        if token in alias_sources and alias_sources[token] != source_token:
            raise ValueError(f"Conflicting compiler material alias: {token}")
        alias_sources[token] = source_token
        source_target = resource_path(required[source_token]).as_posix()
        original = payload / "materials" / "models" / options.namespace / (source_target + ".vmt")
        target = payload / "materials" / "models" / options.namespace / (token + ".vmt")
        content = original.read_bytes()
        if target in original_vmts and target.read_bytes() != content:
            raise ValueError(f"Compiler material alias replaces another source material: {token}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        written.append(target)
        alias_audits.append(dict(target=token, source=source_token, source_target=source_target,
                                 status="generated", vmt_bytes_preserved=True, additional_textures=0))
    written.append(_write_json(destination / "material-report.json", {"materials": audits, "aliases": alias_audits}))
    return written


def _compile(options, asset, log, cancel, *, playermodel=False) -> list[Path]:
    """Compile, checksum-check and stage exactly this model's runtime files."""
    from s2kit.pipelines import studiomdl

    _, destination, payload = _paths(options, asset)
    if playermodel:
        destination = destination / "playermodel"
    descriptor = json.loads((destination / "build.json").read_text(encoding="utf-8"))
    game = _compiler_game(options, payload)
    written = []
    physics_audits = []
    model_audits = []
    player_audit = None
    for name in descriptor["qcs"]:
        qc = Path(name)
        if not studiomdl.compile_qc(qc, Path(options.compiler), game, on_log=log, cancel=cancel):
            check_cancelled(cancel)
            raise ValueError(f"Compiler failed: {qc}")
        mdl = studiomdl.model_path_for(qc, game)
        geometry = bool(re.search(r"(?mi)^\s*\$(?:body|bodygroup|model)\b", qc.read_text(encoding="utf-8")))
        check = studiomdl.check_compiled_model(mdl, require_geometry=geometry)
        if not check.ok:
            raise ValueError("; ".join(check.problems()))
        outputs = [mdl, *check.siblings]
        morph_finalization = None
        material_finalization = None
        if geometry:
            from s2kit.pipelines.compiled_materials import finalize_lod_material_replacements
            from s2kit.pipelines.compiled_morph_geometry import repair_compiled_morph_geometry
            from s2kit.pipelines.ragdoll_flex import install_source_flex_rig

            appearance = descriptor.get("report", {}).get("appearance", {})
            install_source_flex_rig(mdl, appearance.get("flexes", {}))
            check_cancelled(cancel)
            morph_finalization = repair_compiled_morph_geometry(
                mdl, qc, appearance, backup_dir=destination / "compiler-backups" / qc.stem,
                cancel=cancel, on_log=log)
            check_cancelled(cancel)
            if not morph_finalization["ok"]:
                raise ValueError("Compiled morph restoration failed: " + "; ".join(morph_finalization["errors"]))
            material_finalization = finalize_lod_material_replacements(
                mdl, qc, backup_dir=destination / "compiler-backups" / qc.stem / "materials",
                cancel=cancel, on_log=log)
            check_cancelled(cancel)
            if not material_finalization["ok"]:
                raise ValueError("Compiled LOD material restoration failed: " + "; ".join(material_finalization["errors"]))
            check = studiomdl.check_compiled_model(mdl, require_geometry=True)
            if not check.ok:
                raise ValueError("; ".join(check.problems()))
        if geometry and descriptor.get("report", {}).get("physics_audit_manifest"):
            from s2kit.pipelines.phy_audit import (
                audit_compiled_phy, finalize_compiled_phy, load_audit_manifest,
            )
            from s2kit.pipelines.phy_geometry import rebuild_compiled_phy_geometry

            prepared = load_audit_manifest(Path(descriptor["report"]["physics_audit_manifest"]))
            phy = mdl.with_suffix(".phy")
            if not phy.is_file():
                raise ValueError(f"Compiled ragdoll has no physics file: {mdl}")
            geometry_finalization = rebuild_compiled_phy_geometry(
                phy, prepared, Path(options.compiler),
                Path(options.project) / "calibration" / "convex_serializer",
                on_log=log, cancel=cancel)
            finalization = finalize_compiled_phy(phy, prepared)
            audit = audit_compiled_phy(
                phy, prepared, maximum_bounds_error_in=1e-4,
                maximum_volume_error=1e-5, allow_geometry_adaptation=False)
            if not audit["ok"]:
                raise ValueError("Compiled physics audit failed: " + "; ".join(audit["errors"]))
            physics_audits.append({"model": str(mdl), "geometry_finalization": geometry_finalization,
                                   "mass_finalization": finalization, **audit})
            for adaptation in audit["adaptations"]:
                log(adaptation, "WARNING")
        if geometry:
            from s2kit.pipelines.compiled_model_audit import audit_compiled_model
            from s2kit.pipelines.compiled_morph_geometry import audit_compiled_morph_geometry
            from s2kit.pipelines.procedural_native import audit_native_procedurals
            from s2kit.pipelines.ragdoll_animation_audit import (
                audit_source_clip_timing, audit_source_flex_programs,
            )

            model_audit = audit_compiled_model(mdl, qc, descriptor.get("report", {}), payload)
            model_audit["source_animation_timing"] = audit_source_clip_timing(mdl, descriptor.get("report", {}))
            model_audit["source_flex_programs"] = audit_source_flex_programs(
                mdl, descriptor.get("report", {}).get("appearance", {}).get("flexes", {}))
            model_audit["morph_geometry_finalization"] = morph_finalization
            model_audit["lod_material_finalization"] = material_finalization
            model_audit["source_morph_geometry"] = audit_compiled_morph_geometry(
                mdl, qc, descriptor.get("report", {}).get("appearance", {}), cancel=cancel)
            model_audit["native_procedurals"] = audit_native_procedurals(
                mdl, descriptor.get("report", {}).get("appearance", {}).get("procedural", {}).get(
                    "native_procedurals", []))
            for detail in (model_audit["source_animation_timing"], model_audit["source_flex_programs"],
                           model_audit["source_morph_geometry"], model_audit["native_procedurals"]):
                model_audit["errors"].extend(detail["errors"])
            model_audit["ok"] = not model_audit["errors"]
            if not model_audit["ok"]:
                raise ValueError("Compiled model audit failed: " + "; ".join(model_audit["errors"]))
            model_audits.append(model_audit)
        ani = mdl.with_suffix(".ani")
        if ani.is_file():
            outputs.append(ani)
        for source in outputs:
            target = payload / source.relative_to(game)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            written.append(target)
    for source_name in descriptor.get("report", {}).get("addon_drivers", []):
        source = Path(source_name)
        if source.suffix.lower() != ".lua" or not source.is_file():
            raise ValueError(f"Missing generated addon driver: {source}")
        driver_identity = hashlib.sha256(descriptor["modelname"].casefold().encode()).hexdigest()[:16]
        target = payload / "lua" / "autorun" / f"s2kit_{driver_identity}_{source.name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        written.append(target)
        manifest = source.with_suffix(".payload.json")
        if manifest.is_file():
            for entry in json.loads(manifest.read_text(encoding="utf-8")):
                dependency = Path(entry["source"])
                relative = resource_path(entry["relative"])
                if not dependency.is_file() or relative.parts[:2] != ("lua", "s2kit"):
                    raise ValueError(f"Invalid generated model driver dependency: {dependency}")
                dependency_target = payload / relative
                dependency_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dependency, dependency_target)
                written.append(dependency_target)
    if playermodel:
        from s2kit.pipelines.player_animations import audit_compiled_player_model

        main = studiomdl.model_path_for(Path(descriptor["qcs"][-1]), game)
        library = studiomdl.model_path_for(Path(descriptor["qcs"][0]), game)
        player_audit = audit_compiled_player_model(main, library)
    written.append(_write_json(destination / "compiled-report.json", {
        "checksum_validation": "passed", "modelname": descriptor["modelname"],
        "engine_validation": "not_run", "files": [str(p) for p in written],
        "physics_audits": physics_audits,
        "model_audits": model_audits,
        "player_animation_audit": player_audit,
    }))
    return written


def _replace_directory(staging: Path, destination: Path, backup_parent: Path) -> Path | None:
    """Activate a complete directory while retaining and restoring its previous version on failure."""
    backup = None
    if destination.exists():
        backup = backup_parent / (destination.name + f"-previous-{time.time_ns()}")
        destination.rename(backup)
    try:
        staging.rename(destination)
    except BaseException:
        if backup is not None and backup.exists() and not destination.exists():
            backup.rename(destination)
        raise
    return backup


def _discard_staging(path: Path, parent: Path, prefix: str) -> None:
    """Remove only the uniquely named temporary tree created by the current transaction."""
    if not path.exists():
        return
    if path.resolve().parent != parent.resolve() or not path.name.startswith(prefix):
        raise ValueError(f"Refusing to remove unexpected staging directory: {path}")
    shutil.rmtree(path)


def _package_staged(options, assets, records, log, cancel, staging: Path) -> list[Path]:
    """Build a verified addon completely before replacing the previous packaged output."""
    from s2kit.pipelines.workflow import file_digest

    project = Path(options.project).resolve()
    addon = project / "addon"
    successful = [asset for asset in assets if all(
        records.get(f"{kind}:{asset.key}", {}).get("status") == "success"
        for kind in ("native", "materials", "compile"))]
    if not successful:
        raise ValueError("No validated model with complete materials is available to package")
    staging.mkdir()
    copied = {}
    registrations = []
    written = []
    for asset in successful:
        check_cancelled(cancel)
        _, sources, payload = _paths(options, asset)
        pm_ok = records.get(f"compile_pm:{asset.key}", {}).get("status") == "success"
        owned = {}
        for kind in (["materials", "compile", "compile_pm"] if pm_ok else ["materials", "compile"]):
            for name, expected_digest in records[f"{kind}:{asset.key}"].get("outputs", {}).items():
                source = Path(name)
                if source.is_relative_to(payload):
                    if source in owned and owned[source] != expected_digest:
                        raise ValueError(f"Conflicting validated output hashes: {source}")
                    owned[source] = expected_digest
        for source in sorted(owned):
            check_cancelled(cancel)
            relative = source.relative_to(payload)
            digest = file_digest(source)
            if digest != owned[source]:
                raise ValueError(f"Validated payload changed before packaging: {source}")
            key = relative.as_posix().casefold()
            if key in copied:
                if copied[key] != digest:
                    raise ValueError(f"Conflicting addon payload: {relative}")
                continue
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if file_digest(target) != digest:
                raise ValueError(f"Validated payload changed during packaging: {source}")
            copied[key] = digest
            written.append(target)
        if pm_ok:
            model_display = Path(asset.relative).stem.replace("_", " ").title()
            display = f"{model_display} [{options.namespace}]"
            modelname = f"models/{options.namespace}/{asset.relative}_pm.mdl"
            registrations.append(f'player_manager.AddValidModel({json.dumps(display)}, {json.dumps(modelname)})')
            registrations.append(f'list.Set("PlayerOptionsModel", {json.dumps(display)}, {json.dumps(modelname)})')
        log(f"Packaged {asset.relative}" + (" and playermodel" if pm_ok else ""), "INFO")
    namespace_identity = hashlib.sha256(options.namespace.encode()).hexdigest()[:16]
    registration = staging / "lua" / "autorun" / f"s2kit_{namespace_identity}_playermodels.lua"
    registration.parent.mkdir(parents=True, exist_ok=True)
    registration.write_text('if ( SERVER ) then\n    AddCSLuaFile()\nend\n\n' +
                            "\n".join(registrations) + "\n", encoding="utf-8")
    kinds = {_built_kind(options, asset) for asset in successful}
    contents = "ragdolls" if kinds == {"ragdoll"} else "props" if kinds == {"prop"} else "models"
    written.extend([registration, _write_json(staging / "addon.json", {
        "title": options.namespace.replace("/", " ") + " " + contents, "type": "model",
        "tags": ["realism"], "ignore": [],
    })])
    manifest_data = {
        "version": 1, "assets": [asdict(asset) for asset in successful],
        "files": {p.relative_to(staging).as_posix(): file_digest(p) for p in written},
    }
    check_cancelled(cancel)
    _replace_directory(staging, addon, project)
    manifest = _write_json(project / "addon-manifest.json", manifest_data)
    return [*[addon / path.relative_to(staging) for path in written], manifest]


def _package(options, assets, records, log, cancel) -> list[Path]:
    """Assemble successful models without exposing a cancelled or incomplete replacement."""
    project = Path(options.project).resolve()
    staging = project / f"addon-staging-{time.time_ns()}"
    try:
        return _package_staged(options, assets, records, log, cancel, staging)
    finally:
        _discard_staging(staging, project, "addon-staging-")


def _install(options, log, cancel) -> list[Path]:
    """Atomically install verified owned files while preserving unrelated files and previous versions."""
    from s2kit.pipelines.workflow import file_digest

    project = Path(options.project).resolve()
    manifest = json.loads((project / "addon-manifest.json").read_text(encoding="utf-8"))
    addons = Path(options.gmod_addons).resolve()
    destination = addons / ("s2kit_" + options.namespace.replace("/", "_"))
    if destination.resolve().parent != addons:
        raise ValueError(f"Installation addon resolves outside its configured folder: {destination}")
    previous_manifest = destination / "s2kit-installed.json"
    previous = json.loads(previous_manifest.read_text(encoding="utf-8")) if previous_manifest.is_file() else {}
    inputs = []
    for relative, digest in manifest["files"].items():
        check_cancelled(cancel)
        path = resource_path(relative)
        source = project / "addon" / path
        if file_digest(source) != digest:
            raise ValueError(f"Packaged file changed before installation: {relative}")
        target = destination / path
        if not target.resolve().is_relative_to(destination.resolve()):
            raise ValueError(f"Installation target escapes its addon: {target}")
        if target.exists():
            if not target.is_file() or target.resolve() != target.absolute():
                raise ValueError(f"Installation would replace an existing directory or linked file: {target}")
            actual = file_digest(target)
            if actual != digest and previous.get("files", {}).get(relative) != actual:
                raise ValueError(f"Installation would overwrite an unowned or modified file: {target}")
        inputs.append((relative, source, digest))
    staging = addons.parent / f".s2kit-install-staging-{time.time_ns()}"

    def copy_checked(source, target):
        check_cancelled(cancel)
        return shutil.copy2(source, target)

    try:
        if destination.exists():
            shutil.copytree(destination, staging, symlinks=True, copy_function=copy_checked)
        else:
            staging.mkdir()
        for relative, source, digest in inputs:
            check_cancelled(cancel)
            target = staging / resource_path(relative)
            if not target.resolve().is_relative_to(staging.resolve()):
                raise ValueError(f"Installation staging path escapes its addon: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if file_digest(target) != digest:
                raise ValueError(f"Installation verification failed: {target}")
        for relative, digest in previous.get("files", {}).items():
            check_cancelled(cancel)
            if relative in manifest["files"]:
                continue
            target = staging / resource_path(relative)
            if not target.resolve().is_relative_to(staging.resolve()):
                raise ValueError(f"Previous installation path escapes its addon: {relative}")
            if target.is_file() and not target.is_symlink() and file_digest(target) == digest:
                target.unlink()
        _write_json(staging / previous_manifest.name, {"version": 1, "files": manifest["files"]})
        check_cancelled(cancel)
        backup = _replace_directory(staging, destination, addons.parent)
        if backup is not None:
            log(f"Preserved previous addon under {backup}", "INFO")
    finally:
        _discard_staging(staging, addons.parent, ".s2kit-install-staging-")
    written = [destination / resource_path(relative) for relative in manifest["files"]]
    written.append(previous_manifest)
    log(f"Installed and verified {len(written)} files under {destination}", "SUCCESS")
    return written


def _implementation_bytes(function) -> bytes:
    """Fingerprint editable source or bundled function bytecode when source files are unavailable."""
    import inspect
    import marshal

    try:
        return inspect.getsource(function).encode()
    except (OSError, TypeError):
        return marshal.dumps(function.__code__)


def _stage_signature(kind: str) -> str:
    """Invalidate affected cached stages when their production converter changes."""
    names = {
        "extract": ["vrf", "model_fidelity", "model_metadata", "native_lods", "prop_port"],
        "native": ["prop_port", "gltf_smd", "native_physics", "dmx_mesh", "physics", "bone_overlays",
                   "smd_export", "smd_export_helpers", "ragdoll_port", "ragdoll_physics", "ragdoll_contacts", "ragdoll_appearance", "lod_materials", "model_fidelity",
                   "gltf_accessors", "morph_atlas", "ragdoll_flex", "ragdoll_runtime", "ragdoll_correctives", "procedural_native",
                   "ragdoll_attachments", "ragdoll_animations", "morph_animation", "model_metadata", "smd_animation_export",
                   "gltf_animation", "glb_skin", "native_port", "skeleton_retarget"],
        "player": ["ragdoll_playermodel", "player_animations", "ragdoll_appearance", "lod_materials", "morph_atlas", "procedural_bones",
                   "ragdoll_physics", "ragdoll_contacts", "ragdoll_flex", "ragdoll_runtime", "ragdoll_correctives", "procedural_native",
                   "ragdoll_animations", "morph_animation", "model_metadata", "smd_animation_export",
                   "gltf_animation", "glb_skin", "native_port", "skeleton_retarget"],
        "materials": ["materials", "semantic_materials", "soundscape", "vmat", "vmt", "pbr_maps", "textures"],
        "compile": ["studiomdl", "phy_audit", "phy_geometry", "ragdoll_flex", "compiled_model_audit", "compiled_render_coverage", "compiled_materials",
                    "compiled_morph_geometry", "compiled_lod_vertices", "ragdoll_animation_audit", "procedural_native"],
        "compile_pm": ["studiomdl", "phy_audit", "phy_geometry", "ragdoll_flex", "compiled_model_audit", "compiled_render_coverage", "compiled_materials",
                       "compiled_morph_geometry", "compiled_lod_vertices", "ragdoll_animation_audit", "player_animations", "procedural_native"],
        "calibrate": ["phy_audit"],
    }.get(kind, [])
    function = {"extract": _extract, "native": _convert, "player": _convert,
                "materials": _materials, "compile": _compile, "compile_pm": _compile,
                "package": _package, "install": _install, "calibrate": _compiler_game}[kind]
    digest = hashlib.sha256(_implementation_bytes(function))
    from s2kit.core import asset_defaults

    defaults_source = Path(__file__).resolve().parent.parent / "core" / "asset_defaults.py"
    defaults_bytes = (defaults_source.read_bytes() if defaults_source.is_file() else
                      json.dumps({name: value for name, value in vars(asset_defaults).items()
                                  if name.isupper()}, sort_keys=True).encode())
    digest.update(defaults_bytes)
    helpers = [_paths, _write_json, resource_path, _implementation_bytes]
    helpers.extend({
        "extract": [_export, _model_kind, _render_source],
        "native": [_render_source, _model_kind], "player": [_render_source],
        "materials": [_material_sources], "compile": [_compiler_game], "compile_pm": [_compiler_game],
        "package": [_package_staged, _replace_directory, _discard_staging, _built_kind],
        "install": [_replace_directory, _discard_staging],
    }.get(kind, []))
    for helper in helpers:
        digest.update(_implementation_bytes(helper))
    for name in names:
        path = Path(__file__).with_name(name + ".py")
        if path.is_file():
            digest.update(path.read_bytes())
        else:
            import importlib.util
            import marshal

            qualified = f"{__package__}.{name}"
            spec = importlib.util.find_spec(qualified)
            loader = spec.loader if spec else None
            if loader is not None and hasattr(loader, "get_code"):
                code = loader.get_code(qualified)
                if code is not None:
                    digest.update(marshal.dumps(code))
    if kind in {"compile", "compile_pm"}:
        native_source = Path(__file__).resolve().parent.parent / "native" / "convex_serializer.cs"
        digest.update(native_source.read_bytes() if native_source.is_file() else b"missing convex serializer")
    return digest.hexdigest()


def _dependency_signature(records: dict, dependencies: list[str]) -> str:
    """Bind cached stages to durable prerequisite results across cancellation and resume."""
    values = {key: {field: records[key].get(field) for field in
                    ("status", "converter_signature", "outputs")}
              for key in dependencies}
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def run_ragdoll_workflow(options, *, inspect_only: bool = False,
                         log: Callable = lambda *_args: None,
                         progress: Callable = lambda *_args: None,
                         cancel: threading.Event | None = None) -> dict:
    """Execute independent model stages with durable per-stage output hashes."""
    from s2kit.pipelines.workflow import atomic_json, file_digest, project_lock, write_report

    options.validate()
    cancel = cancel or threading.Event()
    assets = discover_assets(options)
    project = Path(options.project).resolve()
    signature = input_signature(options, assets)
    with project_lock(project):
        checkpoint = project / "workflow-queue.json"
        state = {"version": 2, "options": asdict(options), "signature": signature,
                 "assets": [asdict(asset) for asset in assets], "tasks": {}, "findings": [],
                 "status": "planned"}
        if checkpoint.is_file() and not inspect_only:
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
            saved_options = saved.get("options", {})
            if saved.get("version") != 2 or not _equivalent_options(saved_options, options):
                raise ValueError("This project uses different settings; choose a separate project folder")
            same_inputs = saved.get("signature") == signature
            if not same_inputs and saved_options != asdict(options):
                from s2kit.pipelines.workflow import WorkflowOptions

                same_inputs = saved.get("signature") == input_signature(WorkflowOptions(**saved_options), assets)
            if same_inputs:
                state = saved
                state.update(options=asdict(options), signature=signature)
        queue = [("calibrate", "calibrate", None, [])]
        for asset in assets:
            prerequisite = []
            if asset.compiled_input:
                queue.append((f"extract:{asset.key}", "extract", asset, []))
                prerequisite = [f"extract:{asset.key}"]
            queue.append((f"native:{asset.key}", "native", asset, [*prerequisite, "calibrate"]))
            queue.append((f"materials:{asset.key}", "materials", asset, [f"native:{asset.key}"]))
            queue.append((f"compile:{asset.key}", "compile", asset, [f"native:{asset.key}", f"materials:{asset.key}"]))
            if (options.create_playermodels and options.model_type != "prop"
                    and "headcrab" not in asset.relative.casefold()):
                queue.append((f"player:{asset.key}", "player", asset, [f"native:{asset.key}"]))
                queue.append((f"compile_pm:{asset.key}", "compile_pm", asset, [f"player:{asset.key}", f"materials:{asset.key}"]))
        queue.append(("package", "package", None, []))
        if options.install:
            queue.append(("install", "install", None, ["package"]))
        state["findings"] = [
            {"severity": "info", "asset": asset.relative,
             "message": "Source metadata and physics are validated after extraction" if asset.compiled_input
             else "Native sources selected; compiled and engine behavior will be checked separately"}
            for asset in assets]
        for key, kind, asset, deps in queue:
            state["tasks"].setdefault(key, {"id": key, "kind": kind,
                "source": asset.source if asset else "", "dependencies": deps,
                "status": "planned" if inspect_only else "pending", "outputs": {}, "messages": []})
        if inspect_only:
            write_report(project, state)
            return state
        state["status"] = "running"
        converter_signatures = {}
        try:
            for index, (key, kind, asset, deps) in enumerate(queue):
                check_cancelled(cancel)
                record = state["tasks"][key]
                if kind not in converter_signatures:
                    converter_signatures[kind] = _stage_signature(kind)
                code_signature = converter_signatures[kind]
                cache_dependencies = ([name for name in state["tasks"] if name not in {"package", "install"}]
                                      if kind == "package" else deps)
                dependency_signature = _dependency_signature(state["tasks"], cache_dependencies)
                reusable = record["status"] == "success" and bool(record["outputs"])
                reusable = reusable and record.get("converter_signature") == code_signature
                reusable = reusable and (not cache_dependencies or
                    record.get("dependency_signature") == dependency_signature)
                reusable = reusable and all(Path(p).is_file() and file_digest(Path(p)) == digest
                                            for p, digest in record["outputs"].items())
                if kind == "package" and any(item.get("rerun") for name, item in state["tasks"].items()
                                             if name not in {"package", "install"}):
                    reusable = False
                if reusable and not any(state["tasks"][dep].get("rerun") for dep in deps):
                    record["rerun"] = False
                    progress(index + 1, len(queue))
                    continue
                if (kind in {"player", "compile_pm"} and state["tasks"][f"native:{asset.key}"]["status"] == "success"
                        and _built_kind(options, asset) == "prop"):
                    record.update(status="skipped", error="", rerun=False, outputs={},
                                  messages=[["INFO", "Playermodels are built only from ragdoll characters"]])
                    progress(index + 1, len(queue))
                    continue
                record.update(status="running", messages=[], error="", rerun=True)
                if any(state["tasks"][dep]["status"] != "success" for dep in deps):
                    record.update(status="blocked", error="A prerequisite failed")
                    continue
                atomic_json(checkpoint, state)

                def task_log(message, level="INFO"):
                    record["messages"].append([level, str(message)])
                    record["messages"] = record["messages"][-1000:]
                    log(f"{asset.relative if asset else key}: {message}", level)

                try:
                    if kind == "calibrate":
                        from s2kit.pipelines.phy_audit import calibrate_compiler

                        calibrate_compiler(Path(options.compiler), _compiler_game(options),
                                           project / "calibration", on_log=task_log, cancel=cancel)
                        outputs = [project / "calibration" / "compiler_calibration.json"]
                    elif kind == "extract":
                        outputs = _extract(options, asset, task_log, cancel)
                    elif kind == "native":
                        outputs = _convert(options, asset, task_log, cancel)
                    elif kind == "player":
                        outputs = _convert(options, asset, task_log, cancel, playermodel=True)
                    elif kind == "materials":
                        outputs = _materials(options, asset, task_log, cancel)
                    elif kind in {"compile", "compile_pm"}:
                        outputs = _compile(options, asset, task_log, cancel, playermodel=kind == "compile_pm")
                    elif kind == "package":
                        outputs = _package(options, assets, state["tasks"], task_log, cancel)
                    else:
                        outputs = _install(options, task_log, cancel)
                    check_cancelled(cancel)
                    if not outputs or any(not Path(p).is_file() for p in outputs):
                        raise ValueError("Stage did not produce its expected files")
                    record.update(status="success", converter_signature=code_signature,
                                  dependency_signature=dependency_signature,
                                  outputs={str(Path(p).resolve()): file_digest(Path(p))
                                                             for p in outputs})
                except Exception as exc:
                    if cancel.is_set() or isinstance(exc, PortCancelled):
                        record.update(status="interrupted", error="Cancelled; resume reruns this stage")
                        raise PortCancelled() from exc
                    record.update(status="failed", error=str(exc))
                    task_log(str(exc), "ERROR")
                state["updated_at"] = time.time()
                atomic_json(checkpoint, state)
                write_report(project, state)
                progress(index + 1, len(queue))
            state["status"] = "success" if all(record["status"] in {"success", "skipped"}
                for record in state["tasks"].values()) else "incomplete"
        except PortCancelled:
            state["status"] = "interrupted"
        finally:
            state["updated_at"] = time.time()
            atomic_json(checkpoint, state)
            write_report(project, state)
        return state
