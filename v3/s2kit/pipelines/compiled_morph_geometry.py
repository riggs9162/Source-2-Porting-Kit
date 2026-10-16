"""Restore authored per-LOD morphs after compiler vertex unification and audit every rendered output."""

from __future__ import annotations

import struct
from dataclasses import replace
from itertools import permutations
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

from s2kit.pipelines.compiled_lod_vertices import read_compiled_vertices, duplicate_lod_vertices
from s2kit.pipelines.compiled_model_audit import _tokens, read_compiled_model
from s2kit.pipelines.compiled_render_coverage import read_lod_material_replacement_ids, resolve_lod_material
from s2kit.pipelines.model_fidelity import resource_token
from s2kit.pipelines.porting import check_cancelled
from s2kit.vendor import valve_datamodel as dmx


class MorphGeometryError(ValueError):
    """Authored deformation cannot be assigned to a rendered corner without ambiguity."""


_ROTATION = np.array([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])
_ANIMATION = np.dtype([("index", "<u2"), ("speed", "u1"), ("side", "u1"), ("changes", "<f2", 6)])


def _source_choices(qc: Path, appearance: dict) -> list[list[list[Path | None]]]:
    """Resolve numbered compiler bodygroup choices and their explicit authored replacements."""
    groups = []
    active = None
    for line in qc.read_text(encoding="utf-8-sig").splitlines():
        values = _tokens(line.split("//", 1)[0])
        if not values:
            continue
        command = values[0].lower()
        if command in ("$body", "$model"):
            groups.append([values[2]])
        elif command == "$bodygroup":
            active = []
            groups.append(active)
        elif active is not None and command in ("studio", "blank"):
            active.append(values[1] if command == "studio" else None)
        elif active is not None and command == "}":
            active = None
    levels = [{}]
    for lod in appearance.get("lods", []):
        levels.append({item["base"]: item["replacement"] for item in lod["replacements"]})
    return [[[qc.parent / replacement if replacement else None
              for replacement in (level.get(base, base) for level in levels)]
             for base in choices] for choices in groups]


def _effective(values: np.ndarray) -> np.ndarray:
    """Represent the engine's legacy half-float to 1/4096-inch fixed-point conversion."""
    half = np.asarray(values, dtype=np.float16).astype(np.float64)
    scaled = np.trunc(half * 4096)
    if not np.isfinite(half).all() or (scaled < -32768).any() or (scaled > 32767).any():
        raise MorphGeometryError("Authored morph exceeds the legacy Source 1 vertex animation range")
    return scaled.astype(np.int32)


def _delta_precision(changes: np.ndarray, effective: np.ndarray) -> dict:
    """Measure per-unit source deformation lost through the target's half and fixed-point encoding."""
    difference = np.abs(changes - effective.astype(np.float64) / 4096)
    return dict(maximum_position_component_error_inches=float(np.max(difference[..., :3], initial=0)),
                maximum_position_vector_error_inches=float(np.max(np.linalg.norm(difference[..., :3], axis=-1), initial=0)),
                maximum_normal_component_error=float(np.max(difference[..., 3:], initial=0)),
                maximum_normal_vector_error=float(np.max(np.linalg.norm(difference[..., 3:], axis=-1), initial=0)))


def _source_mesh(path: Path, bone_names: dict[str, int]) -> dict[str, dict]:
    """Decode generated indexed DMX material streams in the compiler's model-space basis."""
    document = dmx.load(str(path))
    meshes = [element for element in document.elements if element.type == "DmeMesh"]
    if len(meshes) != 1:
        raise MorphGeometryError(f"Expected one generated morph mesh: {path}")
    mesh = meshes[0]
    state = mesh["bindState"]
    model = next(element for element in document.elements if element.type == "DmeModel")
    joint_count = int(state["jointCount"])
    weights = np.asarray(state["jointWeights"], dtype=np.float64).reshape(-1, joint_count)
    indices = np.asarray(state["jointIndices"], dtype=np.int64).reshape(-1, joint_count)
    bone_map = np.array([bone_names.get(joint.name, -1) for joint in model["jointList"]])
    mapped = bone_map[indices]
    if joint_count > 3 or np.any((mapped < 0) & (weights > 0)):
        raise MorphGeometryError("Generated morph source has unsupported skin links")
    mapped = np.pad(np.where(weights > 0, mapped, 65535), ((0, 0), (0, 3 - joint_count)), constant_values=65535)
    weights = np.pad(weights, ((0, 0), (0, 3 - joint_count)))
    order = np.argsort(mapped, axis=1)
    mapped = np.take_along_axis(mapped, order, axis=1)
    weights = np.take_along_axis(weights, order, axis=1)
    weights /= weights.sum(axis=1, keepdims=True)
    positions = np.asarray(state["positions"], dtype=np.float64) @ _ROTATION.T
    normals = np.asarray(state["normals"], dtype=np.float64) @ _ROTATION.T
    uv = np.asarray(state["textureCoordinates"], dtype=np.float64).copy()
    uv[:, 1] = 1 - uv[:, 1]
    targets = {}
    for delta in mesh["deltaStates"]:
        values = np.zeros((len(positions), 6), dtype=np.float64)
        for channel, start in (("positions", 0), ("normals", 3)):
            selection = np.asarray(delta[channel + "Indices"], dtype=np.int64)
            changes = np.asarray(delta[channel], dtype=np.float64).reshape(-1, 3) @ _ROTATION.T
            if len(selection) != len(changes) or (len(selection) and (selection.min() < 0 or selection.max() >= len(positions))):
                raise MorphGeometryError("Invalid generated DMX morph indices")
            values[selection, start:start + 3] = changes
        targets[delta.name] = values
    output = {}
    for face in mesh["faceSets"]:
        name = resource_token(face["material"]["mtlName"])
        corners = np.asarray([index for index in face["faces"] if index >= 0], dtype=np.int64)
        if len(corners) % 3:
            raise MorphGeometryError("Generated morph source contains non-triangle faces")
        selected = np.unique(corners)
        names = list(targets)
        changes = np.stack([targets[name][selected] for name in names], axis=1)
        remap = np.full(len(positions), -1, dtype=np.int64)
        remap[selected] = np.arange(len(selected))
        output[name] = dict(position=positions[selected], normal=normals[selected], uv=uv[selected],
                            bones=mapped[selected], weights=weights[selected],
                            targets=names, changes=changes, effective=_effective(changes),
                            tree=cKDTree(positions[selected]), triangles=remap[corners.reshape(-1, 3)])
    return output


def _corner_candidates(vertex_id: int, candidates: dict, triangles: np.ndarray, source: dict) -> np.ndarray:
    """Intersect all incident triangle correspondences when vertex attributes alone are ambiguous."""
    adjacency = source.get("triangle_adjacency")
    if adjacency is None:
        adjacency = {}
        for face_index, face in enumerate(source["triangles"]):
            for index in face:
                adjacency.setdefault(int(index), set()).add(face_index)
        source["triangle_adjacency"] = adjacency
    possible = set(map(int, candidates[vertex_id]))
    incident = triangles[np.any(triangles == vertex_id, axis=1)]
    for triangle in incident:
        source_faces = None
        for index in triangle:
            faces = set().union(*(adjacency.get(int(candidate), set()) for candidate in candidates[int(index)]))
            source_faces = faces if source_faces is None else source_faces & faces
        supported = set()
        corner = list(triangle).index(vertex_id)
        for face_index in source_faces or ():
            face = source["triangles"][face_index]
            for order in permutations(range(3)):
                if all(face[order[index]] in candidates[int(triangle[index])] for index in range(3)):
                    supported.add(int(face[order[corner]]))
        if not supported:
            raise MorphGeometryError(f"Rendered vertex {vertex_id} has no complete authored triangle provenance")
        possible &= supported
    if not len(incident) or not possible:
        raise MorphGeometryError(f"Rendered vertex {vertex_id} merges incompatible authored triangle corners")
    return np.asarray(sorted(possible), dtype=np.int64)


def _resolve_vertices(source: dict, vertices: np.ndarray, ids: list[int], cancel=None,
                        triangles=None, lod=0) -> tuple[np.ndarray, dict]:
    """Require equivalent target deformations among all surviving bounded attribute candidates."""
    chosen = []
    report = dict(vertices=len(ids), position_snap_maximum=0.0, uv_fallbacks=0,
                  multiple_equivalent_candidates=0, normal_disambiguations=0, skin_disambiguations=0,
                  topology_disambiguations=0)
    candidate_sets = {}
    ambiguous = []
    for ordinal, vertex_id in enumerate(ids):
        if ordinal % 1024 == 0:
            check_cancelled(cancel)
        vertex = vertices[vertex_id]
        candidates = np.asarray(source["tree"].query_ball_point(vertex["position"], .0501), dtype=np.int64)
        if not len(candidates):
            raise MorphGeometryError(f"Rendered vertex {vertex_id} has no source within the compiler LOD snap bound")
        if lod == 0:
            exact = np.linalg.norm(source["position"][candidates] - vertex["position"], axis=1) <= 1e-4
            exact &= np.max(np.abs(source["uv"][candidates] - vertex["uv"]), axis=1) <= 1e-6
            exact &= np.max(np.abs(source["normal"][candidates] - vertex["normal"]), axis=1) <= 1e-5
            candidates = candidates[exact]
        else:
            compatible = np.max(np.abs(source["uv"][candidates] - vertex["uv"]), axis=1) <= .0101
            compatible &= (source["normal"][candidates] @ vertex["normal"]) >= np.cos(np.deg2rad(10.01))
            candidates = candidates[compatible]
        if not len(candidates):
            raise MorphGeometryError(f"Rendered vertex {vertex_id} has no source within attribute unification bounds")
        original_candidates = candidates.copy()
        needs_topology = not np.all(source["effective"][candidates] == source["effective"][candidates[0]])
        matching_uv = candidates[np.max(np.abs(source["uv"][candidates] - vertex["uv"]), axis=1) <= 1e-6]
        if len(matching_uv):
            candidates = matching_uv
        else:
            report["uv_fallbacks"] += 1
        values = source["effective"][candidates]
        if not np.all(values == values[0]):
            bones = np.where(vertex["weights"] > 0, vertex["bones"].astype(int), 65535)
            order = np.argsort(bones)
            weights = vertex["weights"][order].astype(float)
            weights /= weights.sum()
            matching_skin = candidates[np.all(source["bones"][candidates] == bones[order], axis=1) &
                (np.max(np.abs(source["weights"][candidates] - weights), axis=1) <= 1e-5)]
            if len(matching_skin):
                candidates = matching_skin
                values = source["effective"][candidates]
                report["skin_disambiguations"] += 1
        if not np.all(values == values[0]):
            matching_normal = candidates[np.max(np.abs(source["normal"][candidates] - vertex["normal"]), axis=1) <= 1e-5]
            if len(matching_normal):
                candidates = matching_normal
                values = source["effective"][candidates]
                report["normal_disambiguations"] += 1
        if needs_topology:
            ambiguous.append((ordinal, vertex_id))
        candidate_sets[vertex_id] = set(map(int, original_candidates))
        selected = int(candidates[0])
        chosen.append(selected)
        report["multiple_equivalent_candidates"] += len(candidates) > 1
        report["position_snap_maximum"] = max(report["position_snap_maximum"],
            float(np.max(np.linalg.norm(source["position"][candidates] - vertex["position"], axis=1))))
    for ordinal, vertex_id in ambiguous:
        if triangles is None:
            raise MorphGeometryError(f"Ambiguous authored morph values for rendered vertex {vertex_id}; corner provenance is required")
        candidates = _corner_candidates(vertex_id, candidate_sets, triangles, source)
        values = source["effective"][candidates]
        if not np.all(values == values[0]):
            raise MorphGeometryError(f"Authored triangle corners disagree about rendered vertex {vertex_id}")
        chosen[ordinal] = int(candidates[0])
        report["topology_disambiguations"] += 1
    return np.asarray(chosen, dtype=np.int64), report


def _provenance(compiled, qc: Path, appearance: dict, cancel=None) -> tuple[list[dict], dict]:
    """Resolve every actually rendered LOD vertex against its exact bodygroup and material source."""
    metadata = read_compiled_model(compiled.mdl_path)
    material_replacements = read_lod_material_replacement_ids(compiled.vtx_bytes, metadata["materials"])
    bone_names = {bone["name"]: index for index, bone in enumerate(metadata["bones"])}
    choices = _source_choices(qc, appearance)
    plans = []
    precision = _delta_precision(np.zeros((0, 6)), np.zeros((0, 6), dtype=np.int32))
    report = dict(mesh_lods=[], source_encoding="Legacy half-float with engine 1/4096 fixed conversion",
                  source_to_encoded_precision=precision,
                  precision_scope="All source morphs on VTX-used material vertices at every authored LOD; errors are per unit weight",
                  precision_weight_scaling="Signed and overshoot weights scale these deformation errors by the absolute descriptor weight",
                  raw_source_morphs_retained_in_extraction=True)
    cache = {}
    cached_model = None
    for mesh in compiled.meshes:
        check_cancelled(cancel)
        if cached_model != (mesh.part, mesh.choice):
            cache = {}
            cached_model = mesh.part, mesh.choice
        paths = choices[mesh.part][mesh.choice]
        if not any(path is not None and path.suffix.lower() == ".dmx" for path in paths):
            continue
        material = metadata["skins"][0][mesh.material_slot]
        vertex_rows = compiled.vertices[mesh.vertex_base:mesh.vertex_base + mesh.vertex_count]
        for lod, ids in mesh.lod_used.items():
            if not ids:
                continue
            path = paths[lod]
            if path is None or path.suffix.lower() != ".dmx":
                raise MorphGeometryError("Mixed SMD/DMX morph LOD sources require explicit corner provenance")
            if path not in cache:
                cache[path] = _source_mesh(path, bone_names)
            if material not in cache[path]:
                raise MorphGeometryError(f"Authored LOD source has no material {material}")
            source = cache[path][material]
            try:
                selected, detail = _resolve_vertices(source, vertex_rows, ids, cancel,
                                                      mesh.lod_triangles[lod], lod)
            except MorphGeometryError as error:
                raise MorphGeometryError(f"{path.name}, {material}, mesh {mesh.key}, LOD {lod}: {error}") from error
            bindings = [resolve_lod_material(metadata, material_replacements, lod, skin, mesh.material_slot)
                        for skin in range(len(metadata["skins"]))]
            detail.update(part=mesh.part, choice=mesh.choice, mesh=mesh.mesh, lod=lod, material=material,
                          compiler_material=material, runtime_materials=[binding["runtime_material"] for binding in bindings],
                          material_bindings=bindings)
            delta_precision = _delta_precision(source["changes"][selected], source["effective"][selected])
            detail["source_to_encoded_precision"] = delta_precision
            for metric, value in delta_precision.items():
                precision[metric] = max(precision[metric], value)
            report["mesh_lods"].append(detail)
            plans.append(dict(key=mesh.key, lod=lod, ids=ids, targets=source["targets"],
                              changes=source["changes"][selected], effective=source["effective"][selected]))
    return plans, report


def _read_flexes(compiled, mesh, names: list[str]) -> dict[str, np.ndarray]:
    """Read complete named legacy vertex deltas without controller or descriptor inference."""
    data = compiled.mdl_bytes
    count, relative = struct.unpack_from("<2i", data, mesh.mdl_offset + 16)
    output = {}
    for index in range(count):
        offset = mesh.mdl_offset + relative + index * 60
        descriptor, = struct.unpack_from("<i", data, offset)
        length, start, pair = struct.unpack_from("<3i", data, offset + 20)
        kind, = struct.unpack_from("<B", data, offset + 32)
        if (kind != 0 or pair or not 0 <= descriptor < len(names)
                or struct.unpack_from("<4f", data, offset + 4) != (0., 1., 10., 11.)):
            raise MorphGeometryError("Unsupported compiled stereo or nonlinear vertex animation record")
        records = np.frombuffer(data, dtype=_ANIMATION, count=length, offset=offset + start)
        if np.any(records["speed"] != 255) or np.any(records["side"] != 255):
            raise MorphGeometryError("Compiled morph uses unsupported delayed or stereo vertex weights")
        values = output.setdefault(names[descriptor], np.zeros((mesh.vertex_count, 6), dtype=np.int32))
        if (records["index"] >= mesh.vertex_count).any():
            raise MorphGeometryError("Compiled morph vertex index exceeds its material mesh")
        values[records["index"]] += _effective(records["changes"])
    return output


def _audit_plans(compiled, plans: list[dict], mapping=None, cancel=None) -> dict:
    """Compare every rendered descriptor component to its uniquely resolved authored value."""
    errors = []
    count = 0
    maximum = 0
    meshes = {mesh.key: mesh for mesh in compiled.meshes}
    names = read_compiled_model(compiled.mdl_path)["flex_descriptors"]
    cached = {}
    for plan in plans:
        check_cancelled(cancel)
        key = plan["key"]
        if key not in cached:
            cached = {key: _read_flexes(compiled, meshes[key], names)}
        ids = [mapping[(*key, plan["lod"], index)] for index in plan["ids"]] if mapping else plan["ids"]
        target_indices = {name: index for index, name in enumerate(plan["targets"])}
        for name in sorted(set(plan["targets"]) | cached[key].keys()):
            actual = cached[key].get(name)
            actual = actual[ids] if actual is not None else np.zeros((len(ids), 6), dtype=np.int32)
            expected = (plan["effective"][:, target_indices[name]] if name in target_indices
                        else np.zeros((len(ids), 6), dtype=np.int32))
            difference = np.abs(actual.astype(np.int64) - expected.astype(np.int64))
            error = int(np.max(difference, initial=0))
            maximum = max(maximum, error)
            count += difference.size
            if error:
                errors.append(dict(mesh=key, lod=plan["lod"], target=name,
                                   maximum_delta_error=error / 4096))
        for group in meshes[key].strip_groups:
            if group.lod != plan["lod"] or group.flags & 4:
                continue
            if any(np.any(values[group.original_ids]) for values in cached[key].values()):
                errors.append(dict(mesh=key, lod=plan["lod"],
                                   reason="Rendered morph strip group lacks the VTX delta-flex flag"))
    return dict(ok=not errors, feature_status="adapted", errors=errors, component_comparisons=count,
                maximum_effective_delta_error=maximum / 4096,
                weight_encoding="Single canonical direct-weight key (0,1,10,11), including signed and overshoot weights")


def audit_compiled_morph_geometry(mdl_path: Path, qc_path: Path, appearance: dict, *, cancel=None) -> dict:
    """Reject compiler morph loss even when controller, rule and descriptor counts match."""
    if not appearance.get("flex_geometry"):
        return dict(ok=True, applicable=False, errors=[])
    compiled = read_compiled_vertices(mdl_path)
    plans, provenance = _provenance(compiled, Path(qc_path), appearance, cancel)
    return dict(applicable=True, provenance=provenance, **_audit_plans(compiled, plans, cancel=cancel))


def repair_compiled_morph_geometry(mdl_path: Path, qc_path: Path, appearance: dict, *,
                                    backup_dir: Path | None = None, cancel=None, on_log=None) -> dict:
    """Split only proven LOD pools and install exact authored deltas before atomically publishing files."""
    if not appearance.get("flex_geometry"):
        return dict(ok=True, applicable=False, errors=[])
    from s2kit.pipelines.compiled_lod_vertices import write_compiled_vertices

    original = read_compiled_vertices(mdl_path)
    if struct.unpack_from("<I", original.mdl_bytes, 152)[0] & 0x200000:
        raise MorphGeometryError("Custom fixed-point morph encoding needs an explicit supported conversion")
    plans, provenance = _provenance(original, Path(qc_path), appearance, cancel)
    existing = _audit_plans(original, plans, cancel=cancel)
    if existing["ok"]:
        return dict(applicable=True, status="already_faithful", provenance=provenance, backups=[], **existing)
    split = duplicate_lod_vertices(mdl_path, flexed_meshes={plan["key"] for plan in plans}, write=False)
    data = bytearray(split.compiled.mdl_bytes)
    names = read_compiled_model(mdl_path)["flex_descriptors"]
    descriptors = {name: index for index, name in enumerate(names)}
    channels = {}
    for plan in plans:
        mesh_channels = channels.setdefault(plan["key"], {})
        ids = np.asarray([split.mapping[(*plan["key"], plan["lod"], index)] for index in plan["ids"]])
        for target_index, name in enumerate(plan["targets"]):
            if name not in descriptors:
                raise MorphGeometryError(f"Authored morph descriptor is missing: {name}")
            values = plan["changes"][:, target_index]
            active = np.any(plan["effective"][:, target_index] != 0, axis=1)
            mesh_channels.setdefault(name, []).append((ids[active], values[active]))
    for mesh in split.compiled.meshes:
        check_cancelled(cancel)
        if mesh.key not in channels:
            continue
        records = []
        for name, streams in channels[mesh.key].items():
            check_cancelled(cancel)
            ids = np.concatenate([stream[0] for stream in streams])
            values = np.concatenate([stream[1] for stream in streams])
            if not len(ids):
                continue
            if len(ids) > 10000:
                raise MorphGeometryError("Repaired morph exceeds the supported Source renderer flex-vertex limit")
            if len(np.unique(ids)) != len(ids) or ids.max() > 65535:
                raise MorphGeometryError("Repaired morph vertex IDs overlap or exceed the serialized index limit")
            order = np.argsort(ids)
            animation = np.zeros(len(ids), dtype=_ANIMATION)
            animation["index"] = ids[order]
            animation["speed"] = animation["side"] = 255
            animation["changes"] = values[order]
            records.append((descriptors[name], animation.tobytes(), len(ids)))
        start = len(data)
        data.extend(bytes(60 * len(records)))
        for index, (descriptor, payload, count) in enumerate(records):
            offset = start + index * 60
            struct.pack_into("<i4f3iB", data, offset, descriptor, 0., 1., 10., 11., count,
                             len(data) - offset, 0, 0)
            data.extend(payload)
        struct.pack_into("<2i", data, mesh.mdl_offset + 16, len(records), start - mesh.mdl_offset)
    struct.pack_into("<i", data, 76, len(data))
    rewritten = replace(split.compiled, mdl_bytes=bytes(data))
    audit = _audit_plans(rewritten, plans, split.mapping, cancel)
    if not audit["ok"]:
        raise MorphGeometryError(f"Rebuilt morph records failed exact output verification: {audit['errors'][:3]}")
    check_cancelled(cancel)
    backups = write_compiled_vertices(original, rewritten, backup_dir=backup_dir)
    if on_log:
        on_log(f"Restored authored morph geometry across {len(plans)} material LOD meshes", "INFO")
    return dict(applicable=True, provenance=provenance, vertex_split=split.report,
                backups=backups, **audit)
