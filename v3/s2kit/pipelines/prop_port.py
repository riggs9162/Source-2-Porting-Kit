"""Assemble static and animated prop sources for the one-click model workflow."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from s2kit.pipelines.model_fidelity import addon_material_token, resource_token
from s2kit.pipelines.porting import check_cancelled


@dataclass
class PropPortResult:
    """Editable prop sources with the material identities the addon must ship."""

    qc: Path
    files: list[Path] = field(default_factory=list)
    report: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _physics_documents(glb_path: Path) -> list[dict]:
    """Read the PHYS sidecar written beside an export, or nothing when it is absent."""
    from s2kit.pipelines.model_metadata import parse_kv3_documents

    path = glb_path.with_name(glb_path.stem + "_phys.txt")
    if not path.is_file():
        return []
    return parse_kv3_documents(path.read_text(encoding="utf-8-sig"))


def _fidelity(glb_path: Path) -> dict:
    """Load the authored model graph when the export carries one."""
    from s2kit.pipelines.model_fidelity import load_model_fidelity

    if not glb_path.with_suffix(".fidelity.json").is_file():
        return {}
    return load_model_fidelity(glb_path)


def _has_ao_proxy(fidelity: dict) -> bool:
    """Whether the model authors the AO capsules the ragdoll converter builds a humanoid from."""
    from s2kit.pipelines.model_metadata import parse_kv3_documents

    info = fidelity.get("source_data", {}).get("m_modelInfo") or {}
    keys = parse_kv3_documents(info.get("m_keyValueText", "") or "")
    return bool(keys and keys[0].get("ao_proxy_capsule_list"))


def is_skinned(glb_path: Path) -> bool:
    """Whether an exported render model carries a skeleton."""
    from s2kit.pipelines.ragdoll_port import read_render_model

    document, _ = read_render_model(Path(glb_path))
    return bool(document.get("skins"))


def classify_model(glb_path: Path) -> str:
    """Return ``"ragdoll"`` for jointed or AO-proxy characters and ``"prop"`` otherwise.

    A skinned model is a ragdoll when its PHYS block joins bodies together, or
    when it has no PHYS bodies at all but authors the AO proxy capsules the
    ragdoll converter builds a humanoid from. Unskinned meshes and skinned
    models with rigid or bone-following collision are props.
    """
    glb_path = Path(glb_path)
    if not is_skinned(glb_path):
        return "prop"
    documents = _physics_documents(glb_path)
    if any(item.get("m_joints") or item.get("m_constraints2") for item in documents):
        return "ragdoll"
    if not any(item.get("m_parts") for item in documents) and _has_ao_proxy(_fidelity(glb_path)):
        return "ragdoll"
    return "prop"


def _authored_mass(glb_path: Path) -> float | None:
    """Total the PHYS part masses, or nothing when any part leaves its mass to the engine."""
    parts = [part for item in _physics_documents(glb_path) for part in item.get("m_parts", [])]
    masses = [float(part.get("m_flMass", 0.0)) for part in parts]
    if not masses or any(mass <= 0 for mass in masses):
        return None
    return sum(masses)


def _lod_materials(lod_sources: list[tuple[float, Path]]) -> list[str]:
    """Read the VMAT resources a set of authored LOD meshes reference."""
    from s2kit.vendor import valve_datamodel

    resources = []
    for _, source in lod_sources:
        data = valve_datamodel.load(str(source))
        for mesh in (element for element in data.elements if element.type == "DmeMesh"):
            resources.extend(str(face_set["material"]["mtlName"]) for face_set in mesh["faceSets"])
    return resources


def _material_resources(document: dict, fidelity: dict, lod_sources) -> dict[str, str]:
    """Map each compiler material name to the one VMAT resource it was exported from."""
    candidates = [material.get("extras", {}).get("vmat", {}).get("Name", "")
                  for material in document.get("materials", [])]
    candidates.extend(material for family in fidelity.get("skins", []) for material in family.get("m_materials", []))
    candidates.extend(_lod_materials(lod_sources))
    resources: dict[str, str] = {}
    for candidate in filter(None, candidates):
        token = resource_token(candidate)
        name = PurePosixPath(token).name.casefold()
        if resources.setdefault(name, token) != token:
            raise ValueError(f"Materials {resources[name]} and {token} share the compiler name {name}")
    return resources


def _render_materials(qc: Path, files: list[Path]) -> set[str]:
    """Collect every material name the compiled model will reference."""
    used = set()
    for path in files:
        if path.suffix.lower() != ".smd" or path.stem.endswith("_physics"):
            continue
        text = path.read_text(encoding="utf-8")
        if "triangles\n" not in text:
            continue
        records = text.split("triangles\n", 1)[1].splitlines()
        used.update(records[index].strip() for index in range(0, len(records) - 1, 4))
    groups = re.search(r'(?ms)^\$texturegroup\s+"[^"]*"\s*\{(.*?)^\}', qc.read_text(encoding="utf-8"))
    if groups:
        used.update(re.findall(r'"([^"]+)"', groups.group(1)))
    return {name for name in used if name}


def _search_path(cdmaterials: str, token: str) -> str:
    """The `$cdmaterials` folder that holds one material resource beneath the output root."""
    base = cdmaterials.rstrip("/") + "/"
    parent = PurePosixPath(token).parent
    return base if parent == PurePosixPath(".") else f"{base}{parent.as_posix()}/"


def _rewrite_qc(qc: Path, modelname: str, material_paths: list[str], extra_lines: list[str],
                drop_bodies: bool) -> None:
    """Pin the exact model name and material search paths, then append LOD declarations."""
    paths = " ".join(f'"{path}"' for path in material_paths)
    lines = []
    for line in qc.read_text(encoding="utf-8").splitlines():
        if line.startswith("$modelname "):
            line = f'$modelname "{modelname}"'
        elif line.startswith("$cdmaterials "):
            line = f"$cdmaterials {paths}"
        elif drop_bodies and line.startswith("$body "):
            continue
        lines.append(line)
    qc.write_text("\n".join([*lines, *extra_lines]) + "\n", encoding="utf-8")


def _port_static(model_set, out_dir: Path, modelname: str, cdmaterials: str,
                 lod_sources, log, cancel) -> tuple[Path, list[Path], dict, list[str], list[str], bool]:
    """Write a `$staticprop` with source collision, authored mass and authored LOD meshes."""
    from s2kit.pipelines.dmx_mesh import write_lod_references
    from s2kit.pipelines.gltf_smd import ConversionOptions, convert_model_set

    mass = _authored_mass(model_set.render_path)
    options = ConversionOptions(
        modelname_prefix=PurePosixPath(modelname).parent.as_posix(), cdmaterials=cdmaterials,
        export_animations=False, preserve_folders=False, replace_existing=True,
        auto_mass=mass is None, static_mass=mass or 10.0)
    converted = convert_model_set(model_set, options, input_root=model_set.base_dir,
                                  output_root=out_dir, on_log=lambda message: log(message), cancel=cancel)
    if not converted.ok:
        raise ValueError(converted.reason or f"Conversion returned {converted.status}")
    qc = out_dir / f"{model_set.name}.qc"
    lod_lines, lod_files, partitioned = write_lod_references(
        out_dir / f"{model_set.name}.smd", lod_sources, [], log)
    physics = dict(collision=converted.has_physics, surfaceprop=converted.surfaceprop, mass=converted.mass,
                   mass_source="authored" if mass is not None else "surfaceprop_density")
    return qc, [*converted.files, *lod_files], physics, list(converted.warnings), lod_lines, partitioned


def _port_animated(glb_path: Path, model_set, out_dir: Path, modelname: str, cdmaterials: str,
                   log, cancel) -> tuple[Path, list[Path], dict, list[str], list[str], bool]:
    """Write a dynamic prop with its native skeleton, authored clips and bone-following collision."""
    from s2kit.pipelines.gltf_smd import SurfacepropDetector, gltf_surfaceprops
    from s2kit.pipelines.native_port import port_native_model

    declared = gltf_surfaceprops(model_set.physics_path)
    surfaceprop = SurfacepropDetector.detect(
        model_set.name, source2_prop=max(declared, key=declared.get) if declared else None)
    result = port_native_model(glb_path, out_dir, modelname=modelname, cdmaterials=cdmaterials,
                               surfaceprop=surfaceprop, dynamic_prop=True,
                               on_log=log, cancel=cancel)
    physics = dict(collision=bool(result.physics) or "$collisionmodel" in result.qc.read_text(encoding="utf-8"),
                   surfaceprop=surfaceprop, bones=result.bones, sequences=result.sequences,
                   attachments=result.attachments, hitboxes=result.hitboxes,
                   kinematic=result.physics)
    return result.qc, list(result.files), physics, list(result.warnings), [], False


def port_prop(glb_path: Path, out_dir: Path, *, modelname: str, cdmaterials: str = "models/",
              on_log=None, cancel: threading.Event | None = None) -> PropPortResult:
    """Write a compilable prop that keeps the source materials, skins, LODs and collision.

    Unskinned models become a `$staticprop` whose LOD meshes come from the
    export's `.lods.json`. Skinned models keep their native skeleton and every
    exported clip as a dynamic prop. Material names stay the VMAT basenames, and
    `$cdmaterials` lists `cdmaterials` joined with each source folder, so the
    shipped VMTs mirror the source resource tree.
    """
    from s2kit.pipelines.gltf_smd import ModelSetScanner
    from s2kit.pipelines.native_port import load_lod_sources
    from s2kit.pipelines.ragdoll_port import read_render_model

    log = on_log or (lambda _message, _level="INFO": None)
    glb_path, out_dir = Path(glb_path), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    check_cancelled(cancel)
    document, _ = read_render_model(glb_path)
    model_set = next((item for item in ModelSetScanner(glb_path.parent, recursive=False).find_sets()
                      if item.render_path.resolve() == glb_path.resolve()), None)
    if model_set is None:
        raise ValueError(f"Could not discover the model set for {glb_path}")
    fidelity = _fidelity(glb_path)
    animated = bool(document.get("skins"))
    manifest = glb_path.with_suffix(".lods.json")
    lod_sources = load_lod_sources(manifest) if manifest.is_file() else []
    if animated:
        qc, files, physics, warnings, lod_lines, partitioned = _port_animated(
            glb_path, model_set, out_dir, modelname, cdmaterials, log, cancel)
    else:
        qc, files, physics, warnings, lod_lines, partitioned = _port_static(
            model_set, out_dir, modelname, cdmaterials, lod_sources, log, cancel)
    check_cancelled(cancel)
    resources = _material_resources(document, fidelity, lod_sources)
    used = _render_materials(qc, files)
    unresolved = sorted(name for name in used if name.casefold() not in resources)
    if unresolved:
        raise ValueError("Materials without an exported VMAT resource: " + ", ".join(unresolved))
    tokens = sorted({resources[name.casefold()] for name in used})
    targets = {token: addon_material_token(token) for token in tokens}
    if len(set(targets.values())) != len(targets):
        raise ValueError("Material resources collide once their models/ root is dropped: " + ", ".join(tokens))
    material_paths = sorted({_search_path(cdmaterials, target) for target in targets.values()})
    _rewrite_qc(qc, modelname, material_paths, lod_lines, partitioned)
    if fidelity.get("groups"):
        warnings.append("Source bodygroups are combined into one body; every mesh group choice renders together.")
    report = {
        "version": 1, "kind": "prop", "variant": "animated" if animated else "static",
        "source": str(glb_path), "modelname": modelname,
        "material_map": targets,
        "material_search_paths": material_paths,
        "skins": [dict(name=skin.name, materials=list(skin.materials)) for skin in model_set.skins],
        "lod_count": len(lod_sources), "physics": physics,
        "engine_validation": "not_run", "warnings": list(dict.fromkeys(warnings)),
    }
    report_path = out_dir / "prop-report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    files.append(report_path)
    for warning in report["warnings"]:
        log(warning, "WARNING")
    log(f"Wrote {report['variant']} prop with {len(tokens)} material(s) and {len(lod_sources)} LOD(s)", "SUCCESS")
    return PropPortResult(qc, files, report, report["warnings"])
