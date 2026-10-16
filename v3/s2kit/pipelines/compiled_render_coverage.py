"""Require every generated material stream and LOD triangle to survive Source compilation."""

from __future__ import annotations

import hashlib
import re
import struct
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from s2kit.pipelines.compiled_lod_vertices import read_compiled_vertices
from s2kit.pipelines.model_fidelity import resource_token
from s2kit.vendor import valve_datamodel as dmx


class RenderCoverageError(ValueError):
    """Generated render sources cannot be reconciled with their compiled material streams."""


def _tokens(text: str) -> list[str]:
    """Tokenize generated QC without interpreting resource names as escape sequences."""
    text = "\n".join(line.split("//", 1)[0] for line in text.splitlines())
    return [value[1:-1] if value.startswith('"') else value
            for value in re.findall(r'"[^"\n]*"|[{}]|[^\s{}"]+', text)]


def _block(tokens: list[str], start: int) -> tuple[list[str], int]:
    """Read one complete brace-delimited QC block."""
    if start >= len(tokens) or tokens[start] != "{":
        raise RenderCoverageError("Expected a generated QC block")
    depth = 1
    for index in range(start + 1, len(tokens)):
        depth += (tokens[index] == "{") - (tokens[index] == "}")
        if depth == 0:
            return tokens[start + 1:index], index + 1
    raise RenderCoverageError("Generated QC has an unterminated block")


def read_render_layout(qc: Path) -> dict:
    """Read numbered source choices and explicit per-LOD model/material replacements."""
    tokens = _tokens(Path(qc).read_text(encoding="utf-8-sig"))
    groups, levels = [], [dict(models={}, materials={})]
    index = 0
    while index < len(tokens):
        command = tokens[index].casefold()
        if command in {"$body", "$model"}:
            groups.append(dict(name=tokens[index + 1], choices=[tokens[index + 2]]))
            index += 3
        elif command == "$bodygroup":
            name = tokens[index + 1]
            block, index = _block(tokens, index + 2)
            choices = []
            cursor = 0
            while cursor < len(block):
                kind = block[cursor].casefold()
                if kind == "studio":
                    choices.append(block[cursor + 1])
                    cursor += 2
                elif kind == "blank":
                    choices.append(None)
                    cursor += 1
                else:
                    raise RenderCoverageError(f"Unsupported generated bodygroup token {block[cursor]}")
            groups.append(dict(name=name, choices=choices))
        elif command in {"$lod", "$shadowlod"}:
            start = index + (1 if command == "$shadowlod" else 2)
            block, index = _block(tokens, start)
            level = dict(models={}, materials={})
            cursor = 0
            while cursor < len(block):
                kind = block[cursor].casefold()
                if kind in {"replacemodel", "replacematerial"}:
                    source, target = block[cursor + 1:cursor + 3]
                    table = level["models" if kind == "replacemodel" else "materials"]
                    source = source.replace("\\", "/").casefold() if kind == "replacemodel" else resource_token(source)
                    target = target.replace("\\", "/") if kind == "replacemodel" else resource_token(target)
                    if source in table and table[source] != target:
                        raise RenderCoverageError(f"Conflicting LOD replacement for {source}")
                    table[source] = target
                    cursor += 3
                elif kind == "removemodel":
                    source = block[cursor + 1].replace("\\", "/").casefold()
                    if source in level["models"] and level["models"][source] is not None:
                        raise RenderCoverageError(f"Conflicting LOD removal for {source}")
                    level["models"][source] = None
                    cursor += 2
                elif kind in {"nofacial", "use_shadowlod_materials"}:
                    cursor += 1
                else:
                    raise RenderCoverageError(f"Unsupported generated LOD token {block[cursor]}")
            levels.append(level)
        elif tokens[index] == "{":
            _, index = _block(tokens, index)
        else:
            index += 1
    return dict(groups=groups, levels=levels)


def read_lod_material_replacement_ids(vtx: Path | bytes, materials: list[str]) -> list[dict[int, str]]:
    """Keep each VTX replacement attached to its numeric global MDL texture identity."""
    data = Path(vtx).read_bytes() if not isinstance(vtx, bytes) else vtx
    if len(data) < 36 or struct.unpack_from("<i", data)[0] != 7:
        raise RenderCoverageError("Expected a version-seven VTX material table")
    lod_count, start = struct.unpack_from("<2i", data, 20)
    if not 1 <= lod_count <= 8 or start < 36 or start + lod_count * 8 > len(data):
        raise RenderCoverageError("VTX material replacement list exceeds its file")
    output = []
    for lod in range(lod_count):
        header = start + lod * 8
        count, relative = struct.unpack_from("<2i", data, header)
        first = header + relative
        if count < 0 or first < 0 or first + count * 6 > len(data):
            raise RenderCoverageError("VTX material replacements exceed their file")
        replacements = {}
        for index in range(count):
            offset = first + index * 6
            material, relative_name = struct.unpack_from("<hi", data, offset)
            string_offset = offset + relative_name
            if not 0 <= material < len(materials) or not 0 <= string_offset < len(data):
                raise RenderCoverageError("VTX material replacement references an absent texture/name")
            end = data.find(b"\0", string_offset)
            if end < 0:
                raise RenderCoverageError("VTX material replacement name is unterminated")
            target = resource_token(data[string_offset:end].decode("utf-8", errors="strict"))
            if material in replacements:
                raise RenderCoverageError(f"Repeated VTX material replacement for texture ID {material}")
            replacements[material] = target
        output.append(replacements)
    return output


def _replacement_names(replacements: list[dict[int, str]], materials: list[str]) -> list[dict[str, str]]:
    """Project unambiguous replacement resource names for QC comparison and older callers."""
    output = []
    for level in replacements:
        names = {}
        for texture_id, target in level.items():
            source = resource_token(materials[texture_id])
            if source in names and names[source] != target:
                raise RenderCoverageError(f"Ambiguous duplicate VTX material name {source} has different replacement targets")
            names[source] = target
        output.append(names)
    return output


def read_lod_material_replacements(vtx: Path | bytes, materials: list[str]) -> list[dict[str, str]]:
    """Read unambiguous resource-name replacements; runtime resolution must retain numeric IDs."""
    return _replacement_names(read_lod_material_replacement_ids(vtx, materials), materials)


def resolve_lod_material(metadata: dict, replacements: list[dict[int, str]],
                         lod: int, skin: int, slot: int) -> dict:
    """Resolve one rendered skin slot through its actual texture ID and per-LOD VTX record."""
    families = metadata["skin_indices"]
    if not 0 <= skin < len(families) or not 0 <= slot < len(families[skin]) or not 0 <= lod < len(replacements):
        raise RenderCoverageError("Rendered material references an absent skin slot or LOD")
    texture_id = families[skin][slot]
    if not 0 <= texture_id < len(metadata["materials"]):
        raise RenderCoverageError("Rendered material references an absent texture ID")
    source = resource_token(metadata["materials"][texture_id])
    if resource_token(metadata["skins"][skin][slot]) != source:
        raise RenderCoverageError("Decoded skin names disagree with their numeric texture indices")
    return dict(texture_id=texture_id, source_material=source,
                runtime_material=replacements[lod].get(texture_id, source))


def _stream_counts(positions: dict[str, list | np.ndarray]) -> dict[str, dict[str, int]]:
    """Count authored triangles and provable zero-area triangles at Source float precision."""
    output = {}
    for material, values in positions.items():
        triangles = np.asarray(values, dtype=np.float32).astype(np.float64).reshape(-1, 3, 3)
        if not np.isfinite(triangles).all():
            raise RenderCoverageError("Generated source contains nonfinite triangle positions")
        areas = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
        output[material] = dict(triangles=len(triangles), zero_area_triangles=int(np.count_nonzero(np.all(areas == 0, axis=1))))
    return output


def read_source_material_streams(path: Path) -> dict[str, dict[str, int]]:
    """Read actual generated SMD or indexed DMX compiler input material/triangle streams."""
    path = Path(path)
    positions = defaultdict(list)
    if path.suffix.casefold() == ".smd":
        lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        try:
            index = lines.index("triangles") + 1
        except ValueError as exc:
            raise RenderCoverageError(f"Generated SMD has no triangle section: {path}") from exc
        while index < len(lines) and lines[index] != "end":
            material = resource_token(lines[index])
            if index + 3 >= len(lines):
                raise RenderCoverageError(f"Generated SMD has an incomplete triangle: {path}")
            triangle = []
            for line in lines[index + 1:index + 4]:
                fields = line.split()
                if len(fields) < 9:
                    raise RenderCoverageError(f"Generated SMD has an incomplete vertex: {path}")
                triangle.append([float(value) for value in fields[1:4]])
            positions[material].append(triangle)
            index += 4
        if index >= len(lines):
            raise RenderCoverageError(f"Generated SMD has no triangle terminator: {path}")
    elif path.suffix.casefold() == ".dmx":
        document = dmx.load(str(path))
        meshes = [element for element in document.elements if element.type == "DmeMesh"]
        if len(meshes) != 1:
            raise RenderCoverageError(f"Expected one generated DMX mesh: {path}")
        mesh = meshes[0]
        state = mesh["bindState"]
        vertices = np.asarray(state["positions"], dtype=np.float64)
        indices = np.asarray(state.get("positionsIndices", range(len(vertices))), dtype=np.int64)
        if indices.size and (indices.min() < 0 or indices.max() >= len(vertices)):
            raise RenderCoverageError(f"DMX position index exceeds its array: {path}")
        for face_set in mesh["faceSets"]:
            material = resource_token(face_set["material"]["mtlName"])
            polygon = []
            for index in face_set["faces"]:
                if index == -1:
                    if len(polygon) != 3:
                        raise RenderCoverageError(f"Generated DMX contains a non-triangle face: {path}")
                    positions[material].append(vertices[indices[polygon]])
                    polygon = []
                elif not 0 <= index < len(indices):
                    raise RenderCoverageError(f"DMX face index exceeds its array: {path}")
                else:
                    polygon.append(index)
            if polygon:
                raise RenderCoverageError(f"Generated DMX face has no terminator: {path}")
    else:
        raise RenderCoverageError(f"Unsupported generated render source: {path}")
    return _stream_counts(positions)


def _authored_material(material: str, appearance: dict) -> str:
    """Resolve emitted material aliases back to their original VMAT resource identity."""
    aliases = {resource_token(target): resource_token(source)
               for source, target in appearance.get("material_map", {}).items() if resource_token(source) != resource_token(target)}
    aliases.update({resource_token(target): resource_token(source) for target, source in appearance.get("material_aliases", {}).items()})
    current, visited = resource_token(material), set()
    while current in aliases and aliases[current] != current:
        if current in visited:
            raise RenderCoverageError(f"Cyclic material alias {material}")
        visited.add(current)
        current = aliases[current]
    return current


def audit_render_coverage(mdl: Path, qc: Path, appearance: dict, metadata: dict | None = None) -> dict:
    """Audit every source stream, numbered choice, LOD and resolved skin material binding."""
    from s2kit.pipelines.compiled_model_audit import read_compiled_model

    mdl, qc = Path(mdl), Path(qc)
    result = dict(ok=False, errors=[], streams=[], source_hashes={}, lod_material_replacements=[],
                  lod_material_replacement_ids=[],
                  degeneracy_policy="Only source triangles with exactly zero area at Source float32 precision may be elided; nonempty streams cannot disappear")
    errors = result["errors"]
    try:
        metadata = metadata or read_compiled_model(mdl)
        compiled = read_compiled_vertices(mdl)
        layout = read_render_layout(qc)
        replacements = read_lod_material_replacement_ids(compiled.vtx_bytes, metadata["materials"])
        result["lod_material_replacement_ids"] = replacements
        replacement_names = _replacement_names(replacements, metadata["materials"])
        result["lod_material_replacements"] = replacement_names
        if len(layout["groups"]) != len(metadata["bodygroups"]) or len(layout["levels"]) != compiled.lod_count:
            raise RenderCoverageError("Generated source bodygroups/LODs differ from the compiled layout")
        for lod, (expected, actual) in enumerate(zip(layout["levels"], replacement_names)):
            if expected["materials"] != actual:
                errors.append(f"LOD {lod} material replacements differ from QC: expected {expected['materials']}, compiled {actual}")
        records = defaultdict(list)
        for record in appearance.get("lod_material_slots", []):
            records[record["part"], record["choice"], record["lod"], resource_token(record["compiler_material"])].append(record)
        used_records = set()
        cache = {}
        for part, group in enumerate(layout["groups"]):
            if len(group["choices"]) != len(metadata["bodygroups"][part]["choices"]):
                raise RenderCoverageError(f"Compiled bodygroup {part} has a different choice count")
            for choice, base in enumerate(group["choices"]):
                meshes = [mesh for mesh in compiled.meshes if (mesh.part, mesh.choice) == (part, choice)]
                for lod, level in enumerate(layout["levels"]):
                    filename = level["models"].get(base.replace("\\", "/").casefold(), base) if base else None
                    source = qc.parent / filename if filename else None
                    if source is not None and not source.suffix:
                        source = source.with_suffix(".smd")
                    if source is not None and source not in cache:
                        cache[source] = read_source_material_streams(source)
                        result["source_hashes"][str(source)] = hashlib.sha256(source.read_bytes()).hexdigest()
                    expected_streams = cache[source] if source else {}
                    actual_streams = Counter()
                    material_meshes = defaultdict(list)
                    for mesh in meshes:
                        count = len(mesh.lod_triangles[lod])
                        material = resource_token(metadata["skins"][0][mesh.material_slot])
                        actual_streams[material] += count
                        if count:
                            material_meshes[material].append(mesh)
                    for material in sorted(expected_streams.keys() | actual_streams.keys()):
                        expected = expected_streams.get(material, dict(triangles=0, zero_area_triangles=0))
                        actual = actual_streams[material]
                        if not expected["triangles"] and not actual:
                            continue
                        removed = expected["triangles"] - actual
                        row = dict(part=part, choice=choice, lod=lod, source=str(source) if source else None,
                                   compiler_material=material, source_triangles=expected["triangles"],
                                   source_zero_area_triangles=expected["zero_area_triangles"], compiled_triangles=actual,
                                   runtime_materials=[], authored_materials=[], material_bindings=[],
                                   removed_zero_area_triangles=min(max(removed, 0), expected["zero_area_triangles"]),
                                   unaccounted_lost_triangles=max(removed - expected["zero_area_triangles"], 0))
                        result["streams"].append(row)
                        if removed < 0 or removed > expected["zero_area_triangles"] or (expected["triangles"] > expected["zero_area_triangles"] and not actual):
                            errors.append(f"Bodypart {part} choice {choice} LOD {lod} material {material}: source {expected['triangles']} triangles ({expected['zero_area_triangles']} zero-area), compiled {actual}")
                        rows = records.get((part, choice, lod, material), [])
                        if records and material in expected_streams and not rows:
                            errors.append(f"Missing authored material-slot provenance for bodypart {part} choice {choice} LOD {lod} material {material}")
                        if rows:
                            used_records.add((part, choice, lod, material))
                            if sum(record["triangle_count"] for record in rows) != expected["triangles"]:
                                errors.append(f"Authored material-slot triangle count differs from generated source: {part}/{choice}/{lod}/{material}")
                            for record in rows:
                                declared = Path(record["source"])
                                if (declared if declared.is_absolute() else qc.parent / declared).resolve() != source.resolve():
                                    errors.append(f"Authored material-slot source path differs: {part}/{choice}/{lod}/{material}")
                        for skin in range(len(metadata["skins"])):
                            bindings = []
                            for mesh in material_meshes[material]:
                                binding = resolve_lod_material(metadata, replacements, lod, skin, mesh.material_slot)
                                binding.update(mesh=mesh.mesh, skin_slot=mesh.material_slot)
                                wanted = level["materials"].get(binding["source_material"], binding["source_material"])
                                if binding["runtime_material"] != wanted:
                                    errors.append(f"Numeric LOD material differs from QC: {part}/{choice}/{lod}/{material}/skin{skin}, texture ID {binding['texture_id']}: expected {wanted}, compiled {binding['runtime_material']}")
                                bindings.append(binding)
                            row["material_bindings"].append(bindings)
                            resolved = {binding["runtime_material"] for binding in bindings}
                            if len(resolved) > 1:
                                errors.append(f"One generated material stream resolves to multiple runtime materials: {part}/{choice}/{lod}/{material}/skin{skin}")
                            runtime = next(iter(resolved), None)
                            authored = _authored_material(runtime, appearance) if runtime else None
                            row["runtime_materials"].append(runtime)
                            row["authored_materials"].append(authored)
                            for record in rows:
                                wanted = record.get("runtime_materials", [record["runtime_material"]] * len(metadata["skins"]))
                                originals = record.get("authored_materials", [])
                                if len(wanted) != len(metadata["skins"]) or (runtime is not None and resource_token(wanted[skin]) != runtime):
                                    errors.append(f"Runtime LOD material differs from authored slot: {part}/{choice}/{lod}/{material}/skin{skin}")
                                if originals and (len(originals) != len(metadata["skins"]) or authored != resource_token(originals[skin])):
                                    errors.append(f"Original VMAT binding differs after skin/LOD resolution: {part}/{choice}/{lod}/{material}/skin{skin}")
        for key in records.keys() - used_records:
            if any(record["triangle_count"] for record in records[key]):
                errors.append(f"Nonempty authored material-slot stream is absent from generated sources: {key}")
    except (OSError, ValueError, IndexError, KeyError, TypeError, struct.error, UnicodeError) as exc:
        errors.append(f"Compiled render coverage audit failed: {exc}")
    result["ok"] = not errors
    result["source_triangles"] = sum(row["source_triangles"] for row in result["streams"])
    result["compiled_triangles"] = sum(row["compiled_triangles"] for row in result["streams"])
    return result
