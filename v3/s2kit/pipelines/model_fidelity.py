"""Retain authored Source 2 appearance graphs and their lossless block dumps."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from pathlib import Path, PurePosixPath

from s2kit.pipelines.model_metadata import parse_kv3_documents


SCHEMA = "s2kit.model-fidelity.v1"
_BLOCK = re.compile(r"-- Block:\s+(\w+)\s+Size:\s+(\d+)\s+bytes\s+\[Offset:\s*(\d+)\]")


def resource_token(value: str) -> str:
    """Normalize a Source material reference without collapsing directory identity."""
    value = value.replace("\\", "/").strip().lower()
    if value.startswith("materials/"):
        value = value[len("materials/"):]
    for suffix in (".vmat_c", ".vmat", ".vmt"):
        if value.endswith(suffix):
            value = value[:-len(suffix)]
            break
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or ":" in value or '"' in value:
        raise ValueError(f"Unsafe material resource path: {value!r}")
    return path.as_posix()


def _one_document(text: str, name: str) -> dict:
    """Require one root document for the DATA and CTRL block types."""
    documents = parse_kv3_documents(text)
    if len(documents) != 1:
        raise ValueError(f"Expected one {name} document, found {len(documents)}")
    return documents[0]


def parse_model_fidelity(data_text: str, ctrl_text: str, morph_text: str = "",
                         phys_text: str = "", *, resource: str = "", mdat_text: str = "") -> dict:
    """Resolve model masks and absolute CTRL block indices into a portable graph."""
    data = _one_document(data_text, "DATA")
    ctrl = _one_document(ctrl_text, "CTRL")
    block_table = [dict(index=index, type=match[0], size=int(match[1]), offset=int(match[2]))
                   for index, match in enumerate(_BLOCK.findall(data_text))]
    morphs = parse_kv3_documents(morph_text)
    morph_indices = [block["index"] for block in block_table if block["type"] == "MRPH"]
    if morphs and len(morph_indices) != len(morphs):
        raise ValueError("MRPH document count does not match the model resource block table")
    morph_lookup = {block_index: index for index, block_index in enumerate(morph_indices)}
    masks = data.get("m_refMeshGroupMasks", [])
    lod_masks = data.get("m_refLODGroupMasks", [])
    meshes = []
    for entry in ctrl.get("embedded_meshes", []):
        mesh_index = int(entry["mesh_index"])
        if not 0 <= mesh_index < len(masks) or mesh_index >= len(lod_masks):
            raise ValueError(f"CTRL mesh {mesh_index} is absent from authored mask tables")
        morph_block = int(entry.get("morph_block", -1))
        if morph_block >= 0 and morph_block not in morph_lookup:
            raise ValueError(f"Missing MRPH block {morph_block} for mesh {entry['name']}")
        meshes.append(dict(
            index=mesh_index, name=str(entry["name"]),
            data_block=int(entry["data_block"]), morph_block=morph_block,
            morph_index=morph_lookup.get(morph_block),
            group_mask=int(masks[mesh_index]), lod_mask=int(lod_masks[mesh_index]),
        ))
    keyvalues = parse_kv3_documents(data.get("m_modelInfo", {}).get("m_keyValueText", ""))
    embedded = keyvalues[0] if keyvalues else {}
    physics = parse_kv3_documents(phys_text)
    groups = [str(value) for value in data.get("m_meshGroups", [])]
    if len(groups) != len(set(groups)):
        raise ValueError("Source mesh-group names are not unique")
    from s2kit.pipelines.model_metadata import parse_model_metadata

    metadata = parse_model_metadata(data_text, mdat_text)
    return dict(
        schema=SCHEMA, resource=resource or str(data.get("m_name", "")),
        meshes=meshes, groups=groups,
        default_mask=int(data.get("m_nDefaultMeshGroupMask", -1)),
        hidden_bodygroups=data.get("m_BodyGroupsHiddenInTools", []),
        skins=data.get("m_materialGroups", []),
        lod_distances=data.get("m_lodGroupSwitchDistances", []),
        morphs=morphs, constraints=embedded.get("BoneConstraintList", []),
        skin_morphs=embedded.get("materialgroup_driven_morph_list", []),
        physics=physics, source_keyvalues=embedded,
        source_data=data, source_ctrl=ctrl, block_table=block_table,
        source_mdat=parse_kv3_documents(mdat_text),
        source_metadata=dict(attachments=[asdict(attachment) for attachment in metadata.attachments],
                             hitboxes=[asdict(hitbox) for hitbox in metadata.hitboxes]),
    )


def write_fidelity_sidecar(glb_path: Path, blocks: dict[str, str],
                           *, resource: str = "", animation_export: dict | None = None) -> Path:
    """Write exact dump text alongside typed authoring data for a single model."""
    glb_path = Path(glb_path)
    fidelity = parse_model_fidelity(
        blocks.get("DATA", ""), blocks.get("CTRL", ""),
        blocks.get("MRPH", ""), blocks.get("PHYS", ""), resource=resource,
        mdat_text=blocks.get("MDAT", ""),
    )
    target = glb_path.with_suffix(".fidelity.json")
    previous = {}
    if target.is_file():
        try:
            previous = load_model_fidelity(target)
        except (ValueError, OSError):
            previous = {}
    same_source = previous.get("resource") == fidelity["resource"]
    if same_source and previous.get("source_data") == fidelity["source_data"] and previous.get("source_ctrl") == fidelity["source_ctrl"]:
        for key in ("dmx_sources", "lod_export_warnings", "warnings"):
            if previous.get(key):
                fidelity[key] = previous[key]
    raw_directory = glb_path.parent / (glb_path.stem + "_fidelity")
    raw_directory.mkdir(parents=True, exist_ok=True)
    raw = {}
    for block, text in blocks.items():
        block_target = raw_directory / (block.lower() + ".txt")
        block_target.write_text(text, encoding="utf-8", newline="\n")
        raw[block] = block_target.relative_to(glb_path.parent).as_posix()
    fidelity["raw_blocks"] = raw
    fidelity["animation_export"] = (animation_export if animation_export is not None else
                                    previous.get("animation_export") if same_source else None)
    if fidelity["animation_export"] is None:
        fidelity["animation_export"] = dict(included=None, additive_composed=False)
    metadata_sidecar = glb_path.with_suffix(".metadata.json")
    if metadata_sidecar.is_file() and not fidelity["source_metadata"]["attachments"]:
        metadata = json.loads(metadata_sidecar.read_text(encoding="utf-8"))
        fidelity["source_metadata"]["attachments"] = metadata.get("attachments", [])
        fidelity["source_metadata"]["hitboxes"] = metadata.get("hitboxes", [])
    target.write_text(json.dumps(fidelity, indent=2) + "\n", encoding="utf-8")
    return target


def load_model_fidelity(source: dict | Path | str) -> dict:
    """Load an authored graph, accepting a GLB or its explicit sidecar path."""
    if isinstance(source, dict):
        result = source
    else:
        path = Path(source)
        if path.suffix.lower() in (".glb", ".gltf"):
            path = path.with_suffix(".fidelity.json")
        result = json.loads(path.read_text(encoding="utf-8"))
        result["_sidecar_directory"] = str(path.parent.resolve())
    if result.get("schema") != SCHEMA:
        raise ValueError("Unsupported or missing model fidelity sidecar schema")
    return result


def refresh_model_fidelity(vrf_exe: Path, input_path: Path, resource: str,
                            glb_path: Path, *, on_log=None, cancel=None,
                            include_lods: bool = False) -> Path:
    """Refresh authoring sidecars from an existing GLB without exporting geometry."""
    from s2kit.pipelines.vrf import _model_dump_sections, _run_block_dump

    log = on_log or (lambda _message: None)
    blocks = {}
    for block in ("DATA", "CTRL", "MRPH", "PHYS", "MDAT"):
        text = _run_block_dump(vrf_exe, input_path, resource, "vmdl_c", block, log, cancel)
        sections = _model_dump_sections(text or "", resource)
        if resource not in sections:
            raise ValueError(f"No {block} dump for requested model {resource}")
        blocks[block] = sections[resource]
    target = write_fidelity_sidecar(glb_path, blocks, resource=resource)
    if include_lods:
        preserve_model_lods(vrf_exe, input_path, resource, glb_path,
                            on_log=log, cancel=cancel)
    log(f"Preserved authored model graph: {target.name}")
    return target


def _modeldoc_nodes(node: dict):
    """Traverse reconstructed ModelDoc nodes in their authored order."""
    yield node
    for child in node.get("children", []):
        if isinstance(child, dict):
            yield from _modeldoc_nodes(child)


@contextmanager
def _lod_workspace(parent: Path, log):
    """Retain a failed native decompile for diagnosis and subsequent cache reuse."""
    temporary = Path(tempfile.mkdtemp(prefix=".fidelity_lods_", dir=parent))
    try:
        yield temporary
    except BaseException as exc:
        (temporary / "failure.txt").write_text(str(exc) + "\n", encoding="utf-8")
        log(f"Retained native decompile after failure: {temporary}")
        raise
    else:
        if (temporary / "failure.txt").is_file():
            log(f"Retained native decompile after recoverable export failure: {temporary}")
            return
        resolved = temporary.resolve()
        if not resolved.is_relative_to(parent.resolve()) or not resolved.name.startswith(".fidelity_lods_"):
            raise ValueError("Native decompile cleanup escaped its owned workspace")
        shutil.rmtree(resolved)


def _animation_export_failure(lines: list[str]) -> bool:
    """Recognize explicit animation-only extraction stacks, excluding mesh failures."""
    text = "\n".join(lines)
    animation = re.search(r"(?m)^\s*at\s+[^\r\n]*(?:\bToDmxAnim\s*\(|\bNmClipExtract\.)", text)
    geometry = re.search(r"(?m)^\s*at\s+[^\r\n]*(?:\bToDmxMesh\s*\(|\bDatamodelRenderMesh|\bVBIB\.)", text)
    return animation is not None and geometry is None


def _validate_native_lod_mesh(path: Path) -> None:
    """Fully deserialize selected mesh data and check all referenced vertex streams."""
    import numpy as np

    from s2kit.pipelines.ragdoll_appearance import _read_dmx_mesh
    from s2kit.pipelines.smd_animation_export import CoordinateMode
    from s2kit.vendor import valve_datamodel

    document = valve_datamodel.load(str(path))
    models = [element for element in document.elements if element.type == "DmeModel"]
    meshes = [element for element in document.elements if element.type == "DmeMesh"]
    if len(models) != 1 or not meshes:
        raise ValueError(f"Native LOD source does not contain one complete render model: {path}")
    joints = models[0]["jointList"]
    mapped = {index: index for index in range(len(joints))}
    primitives = [primitive for mesh in meshes
                  for primitive in _read_dmx_mesh(mesh, mapped, joints, CoordinateMode(), path)]
    if not primitives:
        raise ValueError(f"Native LOD source has no triangle geometry: {path}")
    for primitive in primitives:
        if (not all(np.isfinite(primitive[field]).all() for field in ("vertices", "normals", "uv", "vw"))
                or np.any(primitive["vw"] < 0) or np.any(primitive["vw"].sum(axis=1) <= 0)):
            raise ValueError(f"Native LOD source contains invalid vertex streams: {path}")
        if any(not np.isfinite(values).all() for target in primitive["targets"].values()
               for values in target.values()):
            raise ValueError(f"Native LOD source contains invalid morph streams: {path}")


def preserve_model_lods(vrf_exe: Path, input_path: Path, resource: str,
                         glb_path: Path, *, on_log=None, cancel=None,
                         modeldoc: Path | None = None) -> Path:
    """Retain every authored lower-LOD mesh, including multi-mesh bodygroup levels."""
    from s2kit.core import process
    from s2kit.pipelines.porting import check_cancelled

    vrf_exe = Path(vrf_exe).resolve()
    input_path = Path(input_path).resolve()
    glb_path = Path(glb_path).resolve()
    sidecar = glb_path.with_suffix(".fidelity.json")
    fidelity = load_model_fidelity(sidecar)
    required = [mesh for mesh in fidelity["meshes"]
                if mesh["lod_mask"] & ~1 and not mesh["lod_mask"] & 1]
    if len(fidelity.get("lod_distances", [])) < 2 or not required:
        return sidecar
    known = fidelity.get("dmx_sources", {})
    if all(str(mesh["index"]) in known and (glb_path.parent / known[str(mesh["index"])]).is_file()
           for mesh in required):
        return sidecar
    log = on_log or (lambda _message: None)
    destination = glb_path.parent / (glb_path.stem + "_fidelity") / "meshes"
    destination.mkdir(parents=True, exist_ok=True)
    log(f"Preserving {len(required)} authored lower-LOD mesh sources: {resource}")
    existing_modeldoc = Path(modeldoc) if modeldoc is not None else None
    context = nullcontext(existing_modeldoc.parent) if existing_modeldoc else _lod_workspace(glb_path.parent, log)
    with context as temporary:
        modeldoc = existing_modeldoc or Path(temporary) / "model.vmdl"
        recovered = None
        if existing_modeldoc is None:
            command = [str(vrf_exe), "-i", str(input_path), "-f", resource,
                       "-e", "vmdl_c", "-d", "-o", str(modeldoc), "--threads", "1"]
            diagnostics = []

            def capture(message):
                """Retain the native diagnostic stream without interrupting progress reporting."""
                diagnostics.append(message)
                log(message)

            code = process.run_streamed(command, capture, cancel=cancel, cwd=Path(temporary))
            check_cancelled(cancel)
            if code:
                (Path(temporary) / "native-export.log").write_text("\n".join(diagnostics) + "\n", encoding="utf-8")
            if not modeldoc.is_file() or (code and not _animation_export_failure(diagnostics)):
                raise ValueError(f"Native LOD decompile failed for {resource} (exit {code})")
            if code:
                recovered = dict(exit_code=code, source=resource,
                    reason="Animation extraction failed after native model geometry was written",
                    diagnostic_directory=str(Path(temporary).resolve()))
        documents = parse_kv3_documents(modeldoc.read_text(encoding="utf-8"))
        if len(documents) != 1 or "rootNode" not in documents[0]:
            raise ValueError("Native decompile did not produce a ModelDoc root")
        entries = [node for node in _modeldoc_nodes(documents[0]["rootNode"])
                   if node.get("_class") == "RenderMeshFile"]
        lod_groups = [node for node in _modeldoc_nodes(documents[0]["rootNode"])
                      if node.get("_class") == "LODGroup"]
        if [float(group["switch_threshold"]) for group in lod_groups] != fidelity["lod_distances"]:
            raise ValueError("Cached ModelDoc LOD thresholds do not match compiled source DATA")
        all_levels = (1 << len(lod_groups)) - 1
        shared = {mesh["name"] for mesh in fidelity["meshes"]
                  if mesh["lod_mask"] & all_levels == all_levels}
        for level, group in enumerate(lod_groups):
            declared = {reference["mesh_name"] for reference in group.get("mesh_references", [])}
            expected = {mesh["name"] for mesh in fidelity["meshes"] if mesh["lod_mask"] & (1 << level)}
            if declared | shared != expected:
                raise ValueError(f"Cached ModelDoc mesh membership differs from source DATA at LOD {level}: "
                                 f"missing={sorted(expected - declared - shared)}, "
                                 f"unexpected={sorted((declared | shared) - expected)}")
        names = {}
        for entry in entries:
            name = entry.get("name", "")
            if name in names:
                raise ValueError(f"Reconstructed ModelDoc has duplicate render mesh name {name}")
            names[name] = entry
        retained = {}
        for mesh in required:
            if mesh["name"] not in names:
                raise ValueError(f"Authored mesh {mesh['name']} is missing from the ModelDoc decompile")
            source_name = PurePosixPath(names[mesh["name"]]["filename"].replace("\\", "/")).name
            source = modeldoc.parent / source_name
            if source.suffix.lower() != ".dmx" or not source.is_file():
                raise ValueError(f"Native LOD mesh source was not exported: {source_name}")
            if recovered is not None:
                _validate_native_lod_mesh(source)
            target = destination / f"mesh{mesh['index']:03d}_{source_name}"
            shutil.copy2(source, target)
            retained[str(mesh["index"])] = target.relative_to(glb_path.parent).as_posix()
        shutil.copy2(modeldoc, destination.parent / "source.vmdl")
        from s2kit.pipelines.native_lods import NativeLodError, write_native_lod_sidecar

        try:
            write_native_lod_sidecar(modeldoc, glb_path, fidelity["lod_distances"])
        except NativeLodError:
            pass
        if recovered is not None:
            warning = (f"Native animation export failed for {resource}; all selected LOD meshes passed "
                       f"complete validation. Diagnostics retained in {temporary}")
            fidelity.setdefault("lod_export_warnings", []).append(recovered)
            fidelity.setdefault("warnings", []).append(warning)
            (Path(temporary) / "failure.txt").write_text(warning + "\n", encoding="utf-8")
            log("WARNING: " + warning)
    fidelity["dmx_sources"] = retained
    fidelity.pop("_sidecar_directory", None)
    sidecar.write_text(json.dumps(fidelity, indent=2) + "\n", encoding="utf-8")
    return sidecar


def write_appearance(*args, **kwargs):
    """Write authored bodygroups, skins, supported flexes and procedural sources."""
    from s2kit.pipelines.ragdoll_appearance import write_appearance as write

    return write(*args, **kwargs)
