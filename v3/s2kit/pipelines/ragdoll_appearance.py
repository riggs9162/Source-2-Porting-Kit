"""Write authored bodygroup choices, material families and supported model flexes."""

from __future__ import annotations

import copy
import json
import math
import re
import struct
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from s2kit.pipelines.dmx_mesh import smd_vertex_line
from s2kit.pipelines.gltf_accessors import decode_accessor
from s2kit.pipelines.gltf_animation import SOURCE2_BASIS, derive_bind_pose_local
from s2kit.pipelines.model_fidelity import load_model_fidelity, resource_token
from s2kit.pipelines.porting import check_cancelled
from s2kit.pipelines.smd_animation_export import SmdAnimationExporter


@dataclass
class AppearanceResult:
    """Compiler sources plus a feature-by-feature account of the authored graph."""

    qc_lines: list[str] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)
    report: dict = field(default_factory=dict)
    addon_drivers: dict = field(default_factory=dict)
    flex_sources: list[tuple[Path, Path]] = field(default_factory=list)
    weight_sources: dict[tuple, set[int]] = field(default_factory=dict)


def _quoted(value: str) -> str:
    """Quote identifiers that can safely appear in compiler input."""
    if any(character in value for character in ('"', '\n', '\r', '\0')):
        raise ValueError(f"Invalid compiler identifier {value!r}")
    return '"' + value + '"'


def _filename(value: str) -> str:
    """Use portable source filenames without changing displayed source names."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", value).strip("_") or "body"


def target_material_token(value: str) -> str:
    """Escape authored material identities that Source 1 otherwise treats as missing geometry."""
    token = resource_token(value)
    return "__s2kit_reserved/" + token if token.casefold() == "debug/debugempty" else token


def _map_primitive_materials(primitives: list[dict]) -> None:
    """Retain the original resource identity alongside its compiler-safe material token."""
    for primitive in primitives:
        source = primitive.setdefault("source_material", primitive["material"])
        primitive["material"] = target_material_token(source)


def _mesh_name(value: str) -> str:
    """Remove only VRF's explicit model-resource prefix from an exported name."""
    value = value.replace("\\", "/")
    for separator in (".vmdl_c.", ".vmdl_c:", ".vmdl.", ".vmdl:"):
        if separator in value:
            return value.split(separator, 1)[1]
    return value


def extract_primitives(gltf: dict, buffer: bytes, skin, *, bone_name_map=None) -> list[dict]:
    """Read skinned primitives with exact mesh provenance and named morph deltas."""
    target_bones = {joint.name: index for index, joint in enumerate(skin.joints)}
    rename = bone_name_map or {}
    primitives = []
    for node_index, node in enumerate(gltf.get("nodes", [])):
        if "mesh" not in node or "skin" not in node:
            continue
        mesh_index = int(node["mesh"])
        mesh = gltf["meshes"][mesh_index]
        source_bones = gltf["skins"][node["skin"]]["joints"]
        mapping = {}
        for index, source_node in enumerate(source_bones):
            name = gltf["nodes"][source_node].get("name", "")
            mapped = rename.get(name, name)
            if mapped in target_bones:
                mapping[index] = target_bones[mapped]
        for primitive_index, primitive in enumerate(mesh.get("primitives", [])):
            attributes = primitive.get("attributes", {})
            if "JOINTS_0" not in attributes:
                continue
            vertices = decode_accessor(gltf, buffer, attributes["POSITION"]).astype(np.float64)
            if int(primitive.get("mode", 4)) != 4:
                raise ValueError("Source model contains a non-triangle primitive")
            joints = decode_accessor(gltf, buffer, attributes["JOINTS_0"]).astype(np.int64)
            weights = decode_accessor(gltf, buffer, attributes["WEIGHTS_0"]).astype(np.float64)
            if "JOINTS_1" in attributes:
                joints = np.hstack([joints, decode_accessor(gltf, buffer, attributes["JOINTS_1"]).astype(np.int64)])
                weights = np.hstack([weights, decode_accessor(gltf, buffer, attributes["WEIGHTS_1"]).astype(np.float64)])
            if not np.isfinite(vertices).all() or not np.isfinite(weights).all() or (weights < 0).any():
                raise ValueError("Model contains non-finite vertices or invalid weights")
            mapped_joints = np.zeros_like(joints)
            for index in np.unique(joints[weights > 0]):
                if int(index) not in mapping:
                    raise ValueError(f"Weighted source joint {index} has no target bone")
                mapped_joints[joints == index] = mapping[int(index)]
            material_index = primitive.get("material")
            material = gltf.get("materials", [])[material_index] if material_index is not None else {}
            material_reference = material.get("extras", {}).get("vmat", {}).get("Name") or material.get("name", "default")
            indices = (decode_accessor(gltf, buffer, primitive["indices"]).astype(np.int64)
                       if "indices" in primitive else np.arange(len(vertices)))
            if len(indices) % 3 or (len(indices) and (indices.min() < 0 or indices.max() >= len(vertices))):
                raise ValueError("Invalid source triangle indices")
            targets = {}
            names = mesh.get("extras", {}).get("targetNames", [])
            for target_index, target in enumerate(primitive.get("targets", [])):
                accessor = target.get("POSITION", target.get("NORMAL"))
                name = names[target_index] if target_index < len(names) else gltf["accessors"][accessor].get("name", "")
                if not name or name in targets:
                    raise ValueError("Morph targets require unique authored names")
                targets[name] = {
                    channel: decode_accessor(gltf, buffer, target[channel]).astype(np.float64)
                    for channel in ("POSITION", "NORMAL") if channel in target
                }
            primitives.append(dict(
                vertices=vertices,
                normals=decode_accessor(gltf, buffer, attributes["NORMAL"]).astype(np.float64)
                if "NORMAL" in attributes else np.tile([0.0, 0.0, 1.0], (len(vertices), 1)),
                uv=decode_accessor(gltf, buffer, attributes["TEXCOORD_0"]).astype(np.float64)
                if "TEXCOORD_0" in attributes else np.zeros((len(vertices), 2)),
                vj=mapped_joints, vw=weights, indices=indices,
                material=resource_token(material_reference), targets=targets,
                gltf_mesh_index=mesh_index, primitive_index=primitive_index,
                node_index=node_index, mesh_name=_mesh_name(mesh.get("name") or node.get("name", "")),
                weight_stream=("gltf", node["skin"], *(attributes.get(name, -1) for name in
                    ("POSITION", "JOINTS_0", "WEIGHTS_0", "JOINTS_1", "WEIGHTS_1"))),
            ))
    if not primitives:
        raise ValueError("The model has no skinned render primitives")
    return primitives


def _authored_mesh_lookup(fidelity: dict, primitives: list[dict]) -> dict[int, list[dict]]:
    """Join glTF mesh identities to CTRL entries, refusing ambiguous name joins."""
    exported = {}
    for primitive in primitives:
        exported.setdefault(primitive["mesh_name"], []).append(primitive)
    names = {}
    lookup = {}
    for mesh in fidelity["meshes"]:
        if not mesh["lod_mask"] & 1:
            continue
        name = mesh["name"]
        if name in names:
            raise ValueError(f"Ambiguous authored LOD0 mesh identity {name}")
        names[name] = mesh["index"]
        if name not in exported:
            raise ValueError(f"Authored LOD0 mesh {name!r} was not exported")
        lookup[mesh["index"]] = exported[name]
    if set(exported) != set(names):
        raise ValueError(f"glTF contains meshes absent from authored LOD0: {sorted(set(exported) - set(names))}")
    return lookup


def _read_dmx_primitives(path: Path, target_skin, source_coord, rename: dict, *,
                         atlas_morphs=None, morph_report=None) -> list[dict]:
    """Read authored lower-LOD DMX streams into the source glTF's coordinate space."""
    from s2kit.vendor import valve_datamodel

    document = valve_datamodel.load(str(path))
    models = [element for element in document.elements if element.type == "DmeModel"]
    meshes = [element for element in document.elements if element.type == "DmeMesh"]
    if len(models) != 1 or not meshes:
        raise ValueError(f"Authored LOD source requires one model with render meshes: {path}")
    source_bones = models[0]["jointList"]
    target_lookup = {joint.name: index for index, joint in enumerate(target_skin.joints)}
    mapped = {index: target_lookup[rename.get(element.name, element.name)]
              for index, element in enumerate(source_bones)
              if rename.get(element.name, element.name) in target_lookup}
    slices = [(0, 0)] * len(meshes)
    if atlas_morphs is not None:
        from s2kit.pipelines.morph_atlas import morph_buffer_slices

        targets, mdat, atlas_path = atlas_morphs
        count = len(next(iter(next(iter(targets.values())).values())))
        slices = morph_buffer_slices(meshes, mdat, count)
        if morph_report is not None:
            morph_report.update(atlas=str(atlas_path), native_source=str(path),
                                target_names=list(targets), buffer_slices=slices,
                                source="Retained MRPH and lossless RGBA8 atlas")
    return [primitive for mesh, (offset, count) in zip(meshes, slices)
            for primitive in _read_dmx_mesh(mesh, mapped, source_bones, source_coord, path,
                atlas_targets=None if atlas_morphs is None else {
                    name: {channel: values[offset:offset + count, :3] for channel, values in channels.items()}
                    for name, channels in atlas_morphs[0].items()})]


def _read_dmx_mesh(mesh, mapped: dict, source_bones, source_coord, path: Path, *,
                   atlas_targets=None) -> list[dict]:
    """Decode one vertex-buffer mesh within a reconstructed Source 2 DMX model."""
    state = mesh["currentState"]
    position_key = "position$0" if "position$0" in state else "positions"
    normal_key = "normal$0" if "normal$0" in state else "normals"
    uv_key = "texcoord$0" if "texcoord$0" in state else "textureCoordinates"
    weights_key = "blendweights$0" if "blendweights$0" in state else "jointWeights"
    joints_key = "blendindices$0" if "blendindices$0" in state else "jointIndices"
    positions = np.asarray(state[position_key], dtype=np.float64)
    normals = np.asarray(state[normal_key], dtype=np.float64)
    uv = np.asarray(state[uv_key], dtype=np.float64)
    count = int(state["jointCount"])
    weights = np.asarray(state[weights_key], dtype=np.float64).reshape(-1, count)
    joints = np.asarray(state[joints_key], dtype=np.int64).reshape(-1, count)
    mapped_joints = np.zeros_like(joints)
    for index in np.unique(joints[weights > 0]):
        if int(index) not in mapped:
            raise ValueError(f"LOD weighted joint {source_bones[int(index)].name} has no target bone")
        mapped_joints[joints == index] = mapped[int(index)]
    source_targets = {}
    for delta in mesh.get("deltaStates", []):
        channels = {}
        for target, key, length in (("POSITION", position_key, len(positions)),
                                    ("NORMAL", normal_key, len(normals))):
            if key in delta and key + "Indices" in delta:
                values = np.zeros((length, 3), dtype=np.float64)
                selection = np.asarray(delta[key + "Indices"], dtype=np.int64).reshape(-1)
                changes = np.asarray(delta[key], dtype=np.float64).reshape(-1, 3)
                if len(selection) != len(changes) or np.any(selection < 0) or np.any(selection >= length):
                    raise ValueError("Authored LOD has invalid sparse morph indices")
                values[selection] = changes
                channels[target] = values
        source_targets[delta.name] = channels
    for name, channels in (atlas_targets or {}).items():
        for channel, values in channels.items():
            length = len(positions) if channel == "POSITION" else len(normals)
            if len(values) != length:
                raise ValueError("Morph atlas vertex count differs from native DMX channel")
            source_targets.setdefault(name, {}).setdefault(channel, np.asarray(values, dtype=np.float64))
    output = []
    for face_set in mesh["faceSets"]:
        faces = []
        face = []
        for index in face_set["faces"]:
            if index >= 0:
                face.append(index)
            else:
                if len(face) != 3:
                    raise ValueError("Authored LOD contains a non-triangle face")
                faces.extend(face)
                face = []
        if face:
            raise ValueError("Unterminated authored LOD face")
        if not faces:
            continue
        pindex = np.asarray([state[position_key + "Indices"][index] for index in faces], dtype=np.int64)
        nindex = np.asarray([state[normal_key + "Indices"][index] for index in faces], dtype=np.int64)
        uindex = np.asarray([state[uv_key + "Indices"][index] for index in faces], dtype=np.int64)
        vertices = positions[pindex].copy()
        normal_values = normals[nindex].copy()
        targets = {}
        for name, channels in source_targets.items():
            targets[name] = {channel: values[pindex if channel == "POSITION" else nindex].copy()
                             for channel, values in channels.items()}
        if source_coord.swap_axes:
            rotation = SOURCE2_BASIS[:3, :3]
            vertices = vertices @ rotation
            normal_values = normal_values @ rotation
            for target in targets.values():
                for channel in target:
                    target[channel] = target[channel] @ rotation
        vertices /= source_coord.scale
        for target in targets.values():
            if "POSITION" in target:
                target["POSITION"] /= source_coord.scale
        output.append(dict(vertices=vertices, normals=normal_values, uv=uv[uindex].copy(),
                           vj=mapped_joints[pindex].copy(), vw=weights[pindex].copy(),
                           indices=np.arange(len(faces)), material=resource_token(face_set["material"]["mtlName"]),
                           targets=targets, source_dmx=str(path), weight_vertex_indices=pindex,
                           weight_stream=("dmx", str(path), str(mesh.id), str(state.id))))
    return output


def authored_bodygroups(fidelity: dict) -> tuple[list[int], list[dict]]:
    """Partition source mesh masks into automatic geometry and numbered choices."""
    groups = {}
    automatic_mask = 0
    for bit, name in enumerate(fidelity["groups"]):
        match = re.fullmatch(r"(.+)_@(\d+)", name)
        if match:
            slot, choice = match[1], int(match[2])
            groups.setdefault(slot, {})[choice] = bit
        elif name.lower() == "autodefault":
            automatic_mask |= 1 << bit
        else:
            groups[name] = {0: bit}
    automatic = []
    bodygroups = []
    for slot, choices in groups.items():
        if sorted(choices) != list(range(max(choices) + 1)):
            raise ValueError(f"Authored bodygroup {slot} has non-contiguous choices")
        selected = [choice for choice, bit in choices.items() if fidelity["default_mask"] & (1 << bit)]
        if len(selected) != 1:
            raise ValueError(f"Authored bodygroup {slot} has no unique default")
        bodygroups.append(dict(name=slot, default=selected[0], choices=[
            dict(value=choice, bit=bit, meshes=[]) for choice, bit in sorted(choices.items())
        ]))
    for mesh in fidelity["meshes"]:
        mask = mesh["group_mask"]
        if not fidelity["groups"] or mask == -1 or mask & automatic_mask:
            automatic.append(mesh["index"])
            continue
        memberships = []
        for group in bodygroups:
            for choice in group["choices"]:
                if mask & (1 << choice["bit"]):
                    choice["meshes"].append(mesh["index"])
                    memberships.append(group["name"])
        if len(set(memberships)) > 1:
            raise ValueError(f"Mesh {mesh['name']} has non-separable membership across bodygroups")
        if not memberships:
            raise ValueError(f"Mesh {mesh['name']} has no authored bodygroup membership")
    combinations = math.prod(len(group["choices"]) for group in bodygroups)
    if len(bodygroups) + bool(automatic) >= 32 or combinations >= 2 ** 31:
        raise ValueError("Authored bodygroup layout exceeds Source 1's bodypart encoding limits")
    return automatic, bodygroups


def _geometry_vectors(values: np.ndarray, coord, *, direction: bool = False) -> np.ndarray:
    """Apply the native Source 2 Viewer coordinate conversion to vector streams."""
    result = values.copy()
    if not direction:
        result *= coord.scale
    if coord.swap_axes:
        result = result @ SOURCE2_BASIS[:3, :3].T
    return result


def _restore_base_morph_channels(fidelity: dict, lookup: dict, coord) -> list[dict]:
    """Restore omitted base normal deltas only after atlas positions independently match exported morphs."""
    from s2kit.pipelines.morph_atlas import load_mesh_morphs, morph_buffer_layout

    reports = []
    for mesh in fidelity["meshes"]:
        if mesh["index"] not in lookup:
            continue
        atlas_morphs = load_mesh_morphs(fidelity, mesh)
        if atlas_morphs is None:
            continue
        targets, mdat, path = atlas_morphs
        streams = {}
        for primitive in lookup[mesh["index"]]:
            streams.setdefault(primitive["weight_stream"], []).append(primitive)
        entries = morph_buffer_layout(mdat)
        if len(streams) != len(entries):
            raise ValueError("Base glTF morph buffers differ from original MDAT draw calls")
        maximum_error = 0.0
        recovered = set()
        for primitives, entry in zip(streams.values(), entries):
            count, offset = len(primitives[0]["vertices"]), entry["offset"]
            if count != entry["extent"] or sorted(primitive["material"] for primitive in primitives) != sorted(entry["materials"]):
                raise ValueError("Base glTF morph vertex extent or material membership differs from MDAT")
            for primitive in primitives:
                for name, channels in targets.items():
                    if any(offset + count > len(values) for values in channels.values()):
                        raise ValueError("Base glTF morph vertices exceed their authored atlas grid")
                    existing = primitive["targets"].get(name, {})
                    for channel, values in channels.items():
                        decoded = values[offset:offset + count, :3].astype(np.float64)
                        if channel in existing:
                            actual = _geometry_vectors(existing[channel], coord, direction=channel == "NORMAL")
                            error = float(np.max(np.abs(actual - decoded), initial=0))
                            maximum_error = max(maximum_error, error)
                            if error > 1e-5:
                                raise ValueError(f"Base morph atlas does not match glTF {name} {channel}: {error:.9g}")
                            continue
                        if coord.swap_axes:
                            decoded = decoded @ SOURCE2_BASIS[:3, :3]
                        if channel == "POSITION":
                            decoded /= coord.scale
                        primitive["targets"].setdefault(name, {})[channel] = decoded
                        recovered.add(channel)
        reports.append(dict(mesh=mesh["index"], atlas=str(path), target_names=list(targets),
                            recovered_channels=sorted(recovered), position_comparison_maximum=maximum_error,
                            buffer_slices=[(entry["offset"], entry["extent"]) for entry in entries]))
    return reports


def source1_skin_links(links: Iterable[tuple[int, float]]) -> list[tuple[int, float]]:
    """Keep the strongest three merged influences, with authored slot order breaking ties."""
    merged = {}
    for joint, weight in links:
        joint, weight = int(joint), float(weight)
        if not math.isfinite(weight) or weight < 0:
            raise ValueError("A source vertex contains an invalid bone weight")
        if weight:
            merged[joint] = merged.get(joint, 0.0) + weight
    selected = sorted(merged.items(), key=lambda item: -item[1])[:3]
    total = sum(weight for _, weight in selected)
    if not selected or total <= 0:
        raise ValueError("A bodygroup mesh has an unweighted vertex")
    return [(joint, weight / total) for joint, weight in selected]


def _record_skin_weights(primitives: list[dict], result: AppearanceResult) -> None:
    """Measure target influence loss once per authored vertex stream without modifying it."""
    report = result.report.setdefault("skin_weights", dict(status="preserved", target_max_influences=3,
        vertex_count=0, affected_vertices=0, source_max_influences=0,
        discarded_weight_sum=0.0, max_discarded_weight=0.0, mean_discarded_weight=0.0,
        max_normalization_difference=0.0, normalization_difference_sum=0.0,
        mean_normalization_difference=0.0, source_weight_sum_max_deviation=0.0,
        tie_policy="First authored influence slot", source_weights_retained_in_extraction=True,
        geometry_sources="Base GLB plus authored lower-LOD DMX meshes",
        normalization_metric="Maximum surviving influence increase from its original normalized weight",
        by_geometry_source={},
        vertex_scope="Unique referenced vertices per source geometry and skin-weight stream"))
    for primitive in primitives:
        key = primitive.get("weight_stream", ("primitive", id(primitive)))
        seen = result.weight_sources.setdefault(key, set())
        source_indices = primitive.get("weight_vertex_indices")
        for index in np.unique(primitive["indices"]):
            source_index = int(source_indices[index]) if source_indices is not None else int(index)
            if source_index in seen:
                continue
            seen.add(source_index)
            joints, weights = primitive["vj"][index], primitive["vw"][index]
            merged = {}
            for joint, weight in zip(joints, weights):
                if weight > 0:
                    merged[int(joint)] = merged.get(int(joint), 0.0) + float(weight)
            total = sum(merged.values())
            reduced = source1_skin_links(merged.items())
            source_kind = "authored_lod_dmx" if key[0] == "dmx" else "base_glb"
            source = report["by_geometry_source"].setdefault(source_kind, dict(vertex_count=0,
                affected_vertices=0, max_discarded_weight=0.0, discarded_weight_sum=0.0,
                mean_discarded_weight=0.0, max_normalization_difference=0.0))
            source["vertex_count"] += 1
            report["vertex_count"] += 1
            report["source_max_influences"] = max(report["source_max_influences"], len(merged))
            report["source_weight_sum_max_deviation"] = max(report["source_weight_sum_max_deviation"], abs(total - 1))
            if len(merged) <= 3:
                continue
            discarded = max(0.0, 1.0 - sum(merged[joint] for joint, _ in reduced) / total)
            normalization = max(abs(weight - merged[joint] / total) for joint, weight in reduced)
            report["affected_vertices"] += 1
            source["affected_vertices"] += 1
            source["discarded_weight_sum"] += discarded
            source["max_discarded_weight"] = max(source["max_discarded_weight"], discarded)
            source["max_normalization_difference"] = max(source["max_normalization_difference"], normalization)
            source["mean_discarded_weight"] = source["discarded_weight_sum"] / source["affected_vertices"]
            report["discarded_weight_sum"] += discarded
            report["max_discarded_weight"] = max(report["max_discarded_weight"], discarded)
            report["normalization_difference_sum"] += normalization
            report["max_normalization_difference"] = max(report["max_normalization_difference"], normalization)
    affected = report["affected_vertices"]
    if affected:
        report["status"] = "adapted"
        report["adaptation"] = "Source 1 supports three bone influences; strongest three retained and renormalized"
        report["mean_discarded_weight"] = report["discarded_weight_sum"] / affected
        report["mean_normalization_difference"] = report["normalization_difference_sum"] / affected
        report["mean_population"] = "Affected vertices"


def write_reference(path: Path, skin, primitives: list[dict], coord) -> None:
    """Write selected meshes on the complete source skeleton with target-compatible links."""
    ok, error = SmdAnimationExporter.write_animation(skin, [derive_bind_pose_local(skin)], path, coord)
    if not ok:
        raise ValueError(error)
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write("triangles\n")
        for primitive in primitives:
            vertices = _geometry_vectors(primitive["vertices"], coord)
            normals = _geometry_vectors(primitive["normals"], coord, direction=True)
            for face in primitive["indices"].reshape(-1, 3):
                output.write(primitive["material"] + "\n")
                for index in face:
                    links = source1_skin_links(zip(primitive["vj"][index], primitive["vw"][index]))
                    uv = primitive["uv"][index]
                    output.write(smd_vertex_line([*vertices[index], *normals[index], uv[0], 1.0 - uv[1]], links))
        output.write("end\n")


def flex_expression(rule: dict, controllers: list[dict]) -> str:
    """Translate the supported compiled stack program without changing its arithmetic."""
    stack = []
    binary = {"ADD": "+", "SUB": "-", "MUL": "*", "DIV": "/"}
    for operation in rule.get("m_FlexOps", []):
        opcode = str(operation["m_OpCode"]).removeprefix("FLEX_OP_")
        operand = int(operation.get("m_Data", 0))
        if opcode == "CONST":
            value = struct.unpack("<f", struct.pack("<I", operand & 0xffffffff))[0]
            if not math.isfinite(value):
                raise ValueError("A flex expression contains a non-finite constant")
            stack.append(f"{value:.9g}")
        elif opcode == "FETCH1":
            if not 0 <= operand < len(controllers):
                raise ValueError("A flex expression references an absent controller")
            name = controllers[operand]["m_szName"]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError(f"Source 1 cannot preserve flex-controller identifier {name!r}")
            stack.append(name)
        elif opcode in (*binary, "MIN", "MAX"):
            if len(stack) < 2:
                raise ValueError("A flex expression has an invalid operand stack")
            second, first = stack.pop(), stack.pop()
            stack.append(f"({first}{binary[opcode]}{second})" if opcode in binary else
                         f"{opcode.lower()}({first},{second})")
        elif opcode in ("NEG", "ABS"):
            if not stack:
                raise ValueError("A flex expression has an invalid operand stack")
            value = stack.pop()
            stack.append(f"(-{value})" if opcode == "NEG" else f"abs({value})")
        else:
            raise ValueError(f"Unsupported source flex opcode {opcode}")
    if len(stack) != 1:
        raise ValueError("A flex expression does not produce one value")
    return stack[0]


def flex_rig(fidelity: dict) -> tuple[dict, list[dict]]:
    """Unify named morph controllers while retaining unsupported rule diagnostics."""
    controllers = {}
    expressions = {}
    rules = {}
    unsupported = []
    for morph_index, morph in enumerate(fidelity.get("morphs", [])):
        inputs = morph.get("m_FlexControllers", [])
        for controller in inputs:
            name = controller["m_szName"]
            if name in controllers and controllers[name] != controller:
                raise ValueError(f"Source morph sets disagree about controller {name}")
            controllers[name] = controller
        last_rules = {int(rule["m_nFlex"]): rule for rule in morph.get("m_FlexRules", [])}
        for descriptor_index, descriptor in enumerate(morph.get("m_FlexDesc", [])):
            name = descriptor["m_szFacs"]
            if descriptor_index not in last_rules:
                unsupported.append(dict(feature="flex_rule", name=name, reason="No authored rule", morph=morph_index))
                continue
            try:
                expression = flex_expression(last_rules[descriptor_index], inputs)
            except ValueError as exc:
                unsupported.append(dict(feature="flex_rule", name=name, reason=str(exc), morph=morph_index))
                continue
            if name in expressions and expressions[name] != expression:
                raise ValueError(f"Source morph sets disagree about rule {name}")
            expressions[name] = expression
            operations = []
            for operation in last_rules[descriptor_index]["m_FlexOps"]:
                opcode = str(operation["m_OpCode"]).removeprefix("FLEX_OP_")
                operand = int(operation.get("m_Data", 0))
                operations.append(dict(opcode=opcode, operand=operand,
                                       controller=inputs[operand]["m_szName"] if opcode == "FETCH1" else None))
            if name in rules and rules[name] != operations:
                raise ValueError(f"Source morph sets disagree about compiled rule {name}")
            rules[name] = operations
    if len(controllers) > 96:
        raise ValueError("Source flex rig exceeds Source 1's 96-controller limit")
    return dict(controllers=list(controllers.values()), expressions=expressions, rules=rules), unsupported


def _procedural_qc(fidelity: dict, bone_name_map: dict, target_names: set[str]) -> tuple[list[str], dict]:
    """Translate genuine source jiggle bones and enumerate cloth/rig adaptations."""
    lines = []
    result = dict(jiggle_bones=[], adapted_cloth=[], unsupported_constraints=[])
    for aggregate in fidelity.get("physics", []):
        finite_elements = aggregate.get("m_pFeModel") or {}
        names = finite_elements.get("m_CtrlName", [])
        for entry in finite_elements.get("m_JiggleBones", []):
            source_index = int(entry.get("m_nNode", entry.get("m_nNodeIndex", entry.get("m_nIndex", -1))))
            data = entry.get("m_JiggleBone", entry.get("m_jiggleBone", {}))
            if source_index < 0 or source_index >= len(names) or not data:
                result["adapted_cloth"].append(dict(feature="jiggle", reason="Unknown compiled jiggle representation", source=entry))
                continue
            original = names[source_index]
            bone = bone_name_map.get(original, original)
            if bone not in target_names:
                raise ValueError(f"Jiggle bone {original} has no target bone")
            flags = int(data.get("m_nFlags", 0))
            kind = "is_rigid" if flags & 2 else "is_flexible" if flags & 1 else ""
            if not kind:
                result["adapted_cloth"].append(dict(feature="jiggle", name=original, reason="Neither rigid nor flexible"))
                continue
            fields = {"length": "m_flLength", "tip_mass": "m_flTipMass",
                      "yaw_stiffness": "m_flYawStiffness", "yaw_damping": "m_flYawDamping",
                      "pitch_stiffness": "m_flPitchStiffness", "pitch_damping": "m_flPitchDamping",
                      "along_stiffness": "m_flAlongStiffness", "along_damping": "m_flAlongDamping"}
            lines.extend([f"$jigglebone {_quoted(bone)}", "{", f"    {kind}", "    {"])
            for target, source in fields.items():
                if source in data:
                    lines.append(f"        {target} {float(data[source]):.9g}")
            for flag, field_name, source in ((16, "angle_constraint", "m_flAngleLimit"),):
                if flags & flag:
                    lines.append(f"        {field_name} {math.degrees(float(data[source])):.9g}")
            for flag, label, prefix in ((4, "yaw", "Yaw"), (8, "pitch", "Pitch")):
                if flags & flag:
                    low = math.degrees(float(data.get("m_flMin" + prefix, 0)))
                    high = math.degrees(float(data.get("m_flMax" + prefix, 0)))
                    lines.append(f"        {label}_constraint {low:.9g} {high:.9g}")
                    for parameter in ("Friction", "Bounce"):
                        key = "m_fl" + prefix + parameter
                        if key in data:
                            lines.append(f"        {label}_{parameter.lower()} {float(data[key]):.9g}")
            if not flags & 32:
                lines.append("        allow_length_flex")
            lines.append("    }")
            if flags & 64:
                lines.extend(["    has_base_spring", "    {"])
                for target, source in (("base_mass", "Mass"), ("stiffness", "Stiffness"), ("damping", "Damping")):
                    lines.append(f"        {target} {float(data['m_flBase' + source]):.9g}")
                for label in ("Left", "Up", "Forward"):
                    low, high = float(data["m_flBaseMin" + label]), float(data["m_flBaseMax" + label])
                    lines.append(f"        {label.lower()}_constraint {low:.9g} {high:.9g}")
                    lines.append(f"        {label.lower()}_friction {float(data['m_flBase' + label + 'Friction']):.9g}")
                lines.append("    }")
            lines.append("}")
            result["jiggle_bones"].append(dict(name=original, target=bone, flags=flags,
                                               adapted_flags=flags & (0x80 | 0x300)))
        if finite_elements and (finite_elements.get("m_Rods") or finite_elements.get("m_Tris") or finite_elements.get("m_Quads")):
            result["adapted_cloth"].append(dict(
                feature="finite_element_cloth", nodes=int(finite_elements.get("m_nNodeCount", 0)),
                rods=len(finite_elements.get("m_Rods", [])), triangles=len(finite_elements.get("m_Tris", [])),
                adaptation="Authored anchor/bind follows are retained; coupled Source 2 cloth solver is unavailable in Source 1",
            ))
    for index, constraint in enumerate(fidelity.get("constraints", [])):
        result["unsupported_constraints"].append(dict(index=index, type=constraint.get("_class", ""),
                                                     reason="Retained in source graph; requires runtime or baked procedural evaluation"))
    return lines, result


def _string_token(name: str) -> int:
    """Hash Source 2 bone symbols with the documented case-insensitive Murmur2 seed."""
    data = name.lower().encode("utf-8")
    multiplier = 0x5bd1e995
    result = 0x31415926 ^ len(data)
    cursor = 0
    while cursor + 4 <= len(data):
        value = int.from_bytes(data[cursor:cursor + 4], "little")
        value = value * multiplier & 0xffffffff
        value ^= value >> 24
        result = ((result * multiplier) ^ (value * multiplier)) & 0xffffffff
        cursor += 4
    tail = data[cursor:]
    if tail:
        result ^= int.from_bytes(tail, "little")
        result = result * multiplier & 0xffffffff
    result ^= result >> 13
    result = result * multiplier & 0xffffffff
    return (result ^ (result >> 15)) & 0xffffffff


def _bind_worlds(skin, coord) -> list[np.ndarray]:
    """Express each source/target bind frame in the coordinate space written to SMD."""
    from s2kit.pipelines.glb_skin import _q2m
    from s2kit.pipelines.smd_animation_export import _apply_coord_to_root_trs

    worlds = []
    for joint in skin.joints:
        parent = joint.parent_joint_idx
        position = np.asarray(joint.rest_translation, dtype=np.float64) * coord.scale
        rotation = np.asarray(joint.rest_rotation, dtype=np.float64)
        if parent is None:
            position, rotation = _apply_coord_to_root_trs(np.asarray(joint.rest_translation), rotation, coord)
        local = np.eye(4)
        local[:3, :3] = _q2m(rotation)
        local[:3, 3] = position
        if parent is not None and parent >= len(worlds):
            raise ValueError("Procedural source bind requires parent-first bones")
        worlds.append(local if parent is None else worlds[parent] @ local)
    return worlds


def _source_rotation_matrix(value=None) -> np.ndarray:
    """Normalize a caller-supplied rigid source-model alignment to a homogeneous matrix."""
    if value is None:
        return np.eye(4)
    value = np.asarray(value, dtype=np.float64)
    if value.shape == (3, 3):
        result = np.eye(4)
        result[:3, :3] = value
    elif value.shape == (4, 4):
        result = value.copy()
    else:
        raise ValueError("Source model alignment requires a 3x3 or 4x4 rotation")
    if (not np.isfinite(result).all() or not np.allclose(result[3], [0, 0, 0, 1])
            or not np.allclose(result[:3, :3].T @ result[:3, :3], np.eye(3), atol=1e-7)
            or not np.isclose(np.linalg.det(result[:3, :3]), 1, atol=1e-7)):
        raise ValueError("Source model alignment must be a finite rigid transform")
    return result


def source_twist_drivers(gltf: dict, buffer: bytes, target_skin, target_coord,
                         fidelity: dict, bone_name_map: dict, *,
                         source_model_rotation=None) -> tuple[list[dict], dict]:
    """Resolve original CTiltTwist equations and native-frame corrections for GMod."""
    from s2kit.pipelines.gltf_animation import parse_skin
    from s2kit.pipelines.native_port import coordinate_mode
    from trimesh.transformations import quaternion_from_matrix

    source_skin = parse_skin(gltf, buffer)
    source_worlds = _bind_worlds(source_skin, coordinate_mode(gltf))
    alignment = _source_rotation_matrix(source_model_rotation)
    source_worlds = [alignment @ world for world in source_worlds]
    target_worlds = _bind_worlds(target_skin, target_coord)
    source_by_name = {joint.name: index for index, joint in enumerate(source_skin.joints)}
    target_by_name = {joint.name: index for index, joint in enumerate(target_skin.joints)}
    source_by_hash = {_string_token(name): index for name, index in source_by_name.items()}
    corrections = {}
    for name, index in source_by_name.items():
        target_name = bone_name_map.get(name, name)
        if target_name not in target_by_name:
            continue
        correction = np.linalg.inv(target_worlds[target_by_name[target_name]]) @ source_worlds[index]
        parent = source_skin.joints[index].parent_joint_idx
        corrections[name] = dict(target=target_name, target_to_source=correction.tolist(),
                                  source_to_target=np.linalg.inv(correction).tolist(),
                                  source_bind_world=source_worlds[index].tolist(),
                                  parent=source_skin.joints[parent].name if parent is not None else "")
    records = []
    for index, definition in enumerate(fidelity.get("constraints", [])):
        if definition.get("_class") != "CTiltTwistConstraint":
            continue
        slaves, targets = definition.get("m_slaves", []), definition.get("m_targets", [])
        if len(slaves) != 1 or len(targets) != 1 or targets[0].get("m_bIsAttachment", False):
            continue
        slave_index = source_by_hash.get(int(slaves[0]["m_nBoneHash"]))
        target_index = source_by_hash.get(int(targets[0]["m_nBoneHash"]))
        if slave_index is None or target_index is None:
            raise ValueError("Source twist bone hash cannot be resolved against the original skeleton")
        slave, target = source_skin.joints[slave_index], source_skin.joints[target_index]
        slave_parent = source_skin.joints[slave.parent_joint_idx].name if slave.parent_joint_idx is not None else ""
        target_parent = source_skin.joints[target.parent_joint_idx].name if target.parent_joint_idx is not None else ""
        required = [slave.name, target.name, *([slave_parent] if slave_parent else []), *([target_parent] if target_parent else [])]
        if any(name not in corrections for name in required):
            raise ValueError("Source twist depends on a bone absent from the target skeleton")
        slave_local = source_worlds[slave_index] if slave.parent_joint_idx is None else np.linalg.inv(
            source_worlds[slave.parent_joint_idx]) @ source_worlds[slave_index]
        target_local = source_worlds[target_index] if target.parent_joint_idx is None else np.linalg.inv(
            source_worlds[target.parent_joint_idx]) @ source_worlds[target_index]
        records.append(dict(
            source_index=index, type="CTiltTwistConstraint", slave=slave.name,
            slave_parent=slave_parent, orig_target_bone=target.name,
            orig_parent_bone=target_parent,
            offset_quaternion=targets[0]["m_qOffset"],
            target_axis=int(definition.get("m_nTargetAxis", 0)),
            slave_axis=int(definition.get("m_nSlaveAxis", 0)),
            share=float(slaves[0].get("m_flWeight", 1.0)),
            original_bind_local=dict(position=slave_local[:3, 3].tolist(),
                                     quaternion=quaternion_from_matrix(slave_local)[[1, 2, 3, 0]].tolist()),
            original_target_bind_local=dict(position=target_local[:3, 3].tolist(),
                                            quaternion=quaternion_from_matrix(target_local)[[1, 2, 3, 0]].tolist()),
        ))
    return records, corrections


def _write_choice_source(stem: str, output: Path, selected: list[dict], skin, coord,
                          writer, rig: dict, result: AppearanceResult, morph_transform) -> str:
    """Write one compiled submodel with its paired flex-DMX representation when needed."""
    _record_skin_weights(selected, result)
    destination = output / (stem + ".smd")
    writer(destination, skin, selected, coord)
    result.files.append(destination)
    if not any(primitive["targets"] for primitive in selected):
        return destination.name
    from s2kit.pipelines.ragdoll_flex import write_flex_dmx

    dmx = destination.with_suffix(".dmx")
    compacted = write_flex_dmx(dmx, destination, selected, coord, rig, morph_transform=morph_transform,
                               define_controls=not result.flex_sources)
    result.report.setdefault("flex_geometry", []).append(dict(source=dmx.name, **compacted))
    result.files.append(dmx)
    result.flex_sources.append((dmx, destination))
    return dmx.name


def _load_authored_lod_meshes(gltf, skin, fidelity, lookup, rename, primitive_transform,
                              result, cancel, selected_indices: set[int]) -> dict:
    """Decode retained authored LOD meshes before assigning stable compiler material slots."""
    from s2kit.pipelines.native_port import coordinate_mode
    from s2kit.pipelines.morph_atlas import load_mesh_morphs

    source_coord = coordinate_mode(gltf)
    sidecar_directory = Path(fidelity.get("_sidecar_directory", "."))
    sources = fidelity.get("dmx_sources", {})
    loaded = dict(lookup)
    for mesh in fidelity["meshes"]:
        check_cancelled(cancel)
        index = mesh["index"]
        if index in loaded or index not in selected_indices or not mesh["lod_mask"]:
            continue
        relative = sources.get(str(index))
        if not relative:
            raise ValueError(f"Authored lower-LOD source is missing for {mesh['name']}; preserve model LOD sources first")
        path = sidecar_directory / relative
        atlas_morphs = load_mesh_morphs(fidelity, mesh)
        morph_report = {}
        primitives = _read_dmx_primitives(path, skin, source_coord, rename,
            atlas_morphs=atlas_morphs, morph_report=morph_report)
        if morph_report:
            result.report.setdefault("lod_morphs", []).append(dict(mesh=index, **morph_report))
        _map_primitive_materials(primitives)
        if primitive_transform is not None:
            transformed = primitive_transform(primitives)
            if transformed is not None:
                primitives = transformed
        loaded[index] = primitives
    return loaded


def _record_lod_material_sources(result, specification: dict, level: int, source: str | None) -> None:
    """Attach the emitted compiler file to independently auditable per-material topology records."""
    for record in result.report["lod_material_slots"]:
        if record["specification"] == specification["name"] and record["lod"] == level:
            record["source"] = source


def _write_authored_lods(gltf, skin, coord, fidelity, lookup, source_specs, output,
                          writer, rig, result, rename, primitive_transform, morph_transform,
                          cancel, material_plan) -> list[str]:
    """Compose each bodygroup's authored lower-level meshes without flattening groups."""
    distances = [float(value) for value in fidelity.get("lod_distances", [])]
    if len(distances) < 2:
        return []
    if not all(math.isfinite(value) and value >= 0 for value in distances) or any(
            second <= first for first, second in zip(distances, distances[1:])):
        raise ValueError("Authored LOD thresholds must be finite and increase")
    mesh_by_index = {mesh["index"]: mesh for mesh in fidelity["meshes"]}
    qc = []
    for level, distance in enumerate(distances[1:], start=1):
        check_cancelled(cancel)
        replacements = []
        qc.extend([f"$lod {distance:.9g}", "{"])
        for source in source_specs:
            selected_indices = [index for index in source["meshes"]
                                if mesh_by_index[index]["lod_mask"] & (1 << level)]
            base_indices = [index for index in source["meshes"]
                            if mesh_by_index[index]["lod_mask"] & 1]
            if selected_indices == base_indices:
                _record_lod_material_sources(result, source, level, source["source"])
                continue
            if not source["source"]:
                if selected_indices:
                    raise ValueError("An authored LOD adds geometry to a blank base bodygroup choice")
                continue
            if not selected_indices:
                qc.append(f'    removemodel {_quoted(source["source"])}')
                replacements.append(dict(base=source["source"], replacement=None))
                continue
            selected = material_plan["streams"][(source["name"], level)]
            stem = "appearance_" + _filename(source["name"]) + f"_lod{level}"
            destination = _write_choice_source(stem, output, selected, skin, coord, writer,
                                               rig, result, morph_transform)
            qc.append(f'    replacemodel {_quoted(source["source"])} {_quoted(destination)}')
            _record_lod_material_sources(result, source, level, destination)
            replacements.append(dict(base=source["source"], replacement=destination,
                                     source_meshes=selected_indices))
        for first, second in sorted(material_plan["replacements"][level].items()):
            qc.append(f"    replacematerial {_quoted(first)} {_quoted(second)}")
        qc.append("}")
        result.report["lods"].append(dict(level=level, source_threshold=distance,
                                          target_distance=distance, replacements=replacements,
                                          adaptation="Source 2 screen coverage threshold reused as an adjustable Source 1 distance"))
    return qc


def write_appearance(gltf: dict, buffer: bytes, skin, coord, out_dir: Path, *,
                     fidelity: dict | Path, modelname: str = "", bone_name_map=None,
                     reference_writer=None, morph_transform=None, primitive_transform=None,
                     source_model_rotation=None, include_source_animations: bool = False,
                     procedural_skin=None, cancel=None) -> AppearanceResult:
    """Write exact source-mask bodygroups and material identities without name inference."""
    fidelity = load_model_fidelity(fidelity)
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    rename = bone_name_map or {}
    primitives = extract_primitives(gltf, buffer, skin, bone_name_map=rename)
    from s2kit.pipelines.native_port import coordinate_mode

    base_morphs = _restore_base_morph_channels(
        fidelity, _authored_mesh_lookup(fidelity, primitives), coordinate_mode(gltf))
    if primitive_transform is not None:
        transformed = primitive_transform(primitives)
        if transformed is not None:
            primitives = transformed
    _map_primitive_materials(primitives)
    lookup = _authored_mesh_lookup(fidelity, primitives)
    automatic, groups = authored_bodygroups(fidelity)
    result = AppearanceResult()
    result.report = dict(bodygroups=copy.deepcopy(groups), automatic_meshes=automatic,
                         source_meshes=len(lookup), material_map={}, skins=[], lods=[],
                         unsupported=[], flexes={}, modelname=modelname,
                         warnings=list(fidelity.get("warnings", [])))
    result.report["base_morphs"] = base_morphs
    writer = reference_writer or write_reference
    rig, unsupported = flex_rig(fidelity)
    result.report["unsupported"].extend(unsupported)
    result.report["flexes"] = dict(controllers=rig["controllers"], targets=list(rig["expressions"]),
                                   expressions=rig["expressions"], rules=rig["rules"],
                                   installation="Source 1 controller and rule tables restored after compile")
    source_specs = []
    if automatic:
        source_specs.append(dict(name="automatic", group=None, choice=None, meshes=automatic))
    for group in groups:
        for choice in group["choices"]:
            source_specs.append(dict(name=f"{group['name']}_{choice['value']}", group=group["name"],
                                     choice=choice["value"], meshes=choice["meshes"]))
    part_indices = {None: 0} if automatic else {}
    part_indices.update({group["name"]: index + bool(automatic) for index, group in enumerate(groups)})
    for source in source_specs:
        source["part"] = part_indices[source["group"]]
        source["compiled_choice"] = source["choice"] if source["choice"] is not None else 0
    lookup = _load_authored_lod_meshes(gltf, skin, fidelity, lookup, rename, primitive_transform,
                                      result, cancel, {index for source in source_specs for index in source["meshes"]})
    from s2kit.pipelines.lod_materials import dmx_skin_material_qc, plan_lod_materials

    material_plan = plan_lod_materials(source_specs,
        {mesh["index"]: mesh for mesh in fidelity["meshes"]}, lookup, fidelity, target_material_token)
    result.report["lod_material_slots"] = material_plan["records"]
    result.report["material_aliases"] = material_plan["aliases"]
    result.report["material_slot_skins"] = material_plan["skin_slots"]
    written_names = set()
    for source in source_specs:
        check_cancelled(cancel)
        selected = material_plan["streams"][(source["name"], 0)]
        if not selected:
            source["source"] = None
            continue
        stem = "appearance_" + _filename(source["name"])
        if stem.casefold() in written_names:
            raise ValueError(f"Appearance source filenames collide for {source['name']}")
        written_names.add(stem.casefold())
        source["source"] = _write_choice_source(stem, output, selected, skin, coord,
                                                 writer, rig, result, morph_transform)
        _record_lod_material_sources(result, source, 0, source["source"])
    for source in source_specs:
        if source["group"] is None and source["source"]:
            result.qc_lines.append(f'$body "automatic" {_quoted(source["source"])}')
    for group in groups:
        result.qc_lines.extend([f'$bodygroup {_quoted(group["name"])}', "{"])
        for choice in group["choices"]:
            source = next(item for item in source_specs if item["group"] == group["name"] and item["choice"] == choice["value"])
            result.qc_lines.append(f'    studio {_quoted(source["source"])}' if source["source"] else "    blank")
        result.qc_lines.append("}")
    materials = {primitive["source_material"] for primitive in primitives}
    skins = fidelity.get("skins", [])
    for family in skins:
        tokens = [resource_token(material) for material in family["m_materials"]]
        materials.update(tokens)
        result.report["skins"].append(dict(name=family["m_name"], materials=tokens,
                                           target_materials=[target_material_token(token) for token in tokens]))
    if len(skins) > 1:
        sizes = {len(family["m_materials"]) for family in skins}
        if len(sizes) != 1:
            raise ValueError("Source material families have incompatible slot counts")
        skin_qc, skin_references = dmx_skin_material_qc(source_specs, material_plan, len(skins))
        result.report["material_skin_references"] = skin_references
        if skin_qc:
            result.qc_lines.extend(skin_qc)
        else:
            result.qc_lines.extend(['$texturegroup "skinfamilies"', "{"])
            for skin_index, family in enumerate(result.report["skins"]):
                tokens = [*family["target_materials"],
                          *(slot["skin_materials"][skin_index] for slot in material_plan["skin_slots"])]
                result.qc_lines.append("    { " + " ".join(_quoted(token) for token in tokens) + " }")
            result.qc_lines.append("}")
    result.report["material_map"] = {material: target_material_token(material) for material in sorted(materials)}
    for selected in material_plan["streams"].values():
        for primitive in selected:
            source = primitive["source_material"]
            result.report["material_map"][source] = target_material_token(source)
    for source in material_plan["aliases"].values():
        result.report["material_map"][source] = target_material_token(source)
    result.qc_lines.extend(_write_authored_lods(
        gltf, skin, coord, fidelity, lookup, source_specs, output, writer, rig, result,
        rename, primitive_transform, morph_transform, cancel, material_plan,
    ))
    result.addon_drivers = dict(
        bodygroup_defaults={group["name"]: group["default"] for group in groups},
        skin_names=[family["m_name"] for family in skins],
        skin_morphs=fidelity.get("skin_morphs", []),
        source_constraints=fidelity.get("constraints", []),
        source_attachments={attachment["name"]: attachment
                            for attachment in fidelity.get("source_metadata", {}).get("attachments", [])},
        available_flexes=[controller["m_szName"] for controller in rig["controllers"]],
        source_model_rotation=_source_rotation_matrix(source_model_rotation).tolist(),
    )
    twists, corrections = source_twist_drivers(gltf, buffer, skin, coord, fidelity, rename,
                                               source_model_rotation=source_model_rotation)
    result.addon_drivers["twist_constraints"] = twists
    result.addon_drivers["source_frames"] = corrections
    from s2kit.pipelines.ragdoll_correctives import prepare_corrective_drivers

    prepared = prepare_corrective_drivers(result.addon_drivers)
    result.addon_drivers.update(prepared)
    from s2kit.pipelines.procedural_native import write_native_procedurals

    native_procedurals = write_native_procedurals(
        result.addon_drivers, procedural_skin or skin, output,
        gltf=gltf if include_source_animations else None,
        buffer=buffer if include_source_animations else None,
    )
    result.addon_drivers["native_procedurals"] = native_procedurals["records"]
    if native_procedurals["records"]:
        prepared = prepare_corrective_drivers(result.addon_drivers)
        result.addon_drivers.update(prepared)
        result.qc_lines.extend(native_procedurals["qc_lines"])
        result.files.extend(native_procedurals["files"])
    corrective_controllers = {name for constraint in prepared["runtime_constraints"]
                              if constraint["kind"] == "pose_morph" for name in constraint["outputs"]}
    corrective_targets = {}
    for target, operations in rig["rules"].items():
        if (len(operations) == 1 and operations[0]["opcode"] == "FETCH1"
                and operations[0]["controller"] in corrective_controllers):
            corrective_targets.setdefault(operations[0]["controller"], []).append(target)
    result.addon_drivers["corrective_flex_targets"] = corrective_targets
    procedural_qc, procedural_report = _procedural_qc(fidelity, rename, set(skin.joint_names))
    translated = {record["source_index"] for record in twists}
    translated.update(record["source_index"] for record in prepared["runtime_constraints"])
    translated.update(record["source_index"] for record in prepared["inert_runtime_constraints"])
    translated.update(record["source_index"] for record in native_procedurals["records"])
    procedural_report["native_procedurals"] = native_procedurals["records"]
    procedural_report["native_procedural_fallbacks"] = native_procedurals["rejected"]
    procedural_report["runtime_twists"] = twists
    procedural_report["runtime_constraint_summary"] = prepared["runtime_constraint_summary"]
    procedural_report["morph_output_precedence"] = dict(
        policy="Source bone constraints override decoded animation controller values",
        direct_targets=corrective_targets,
        combined_animated_controllers=sorted(corrective_controllers - corrective_targets.keys()))
    procedural_report["unsupported_constraints"] = [record for record in procedural_report["unsupported_constraints"]
                                                     if record["index"] not in translated]
    for record in procedural_report["unsupported_constraints"]:
        precise = next((item for item in prepared["unsupported_runtime_constraints"]
                        if item["source_index"] == record["index"]), None)
        if precise is not None:
            record["reason"] = precise["reason"]
    result.qc_lines.extend(procedural_qc)
    result.report["procedural"] = procedural_report
    result.report["sources"] = source_specs
    if len(set(result.report["material_map"].values())) != len(result.report["material_map"]):
        raise ValueError("Compiler-safe material aliases collide with an authored source resource")
    result.report["material_adaptations"] = [dict(source=source, target=target,
        reason="Source 1 discards geometry with this reserved token; original texture and slot preserved")
        for source, target in result.report["material_map"].items() if source != target]
    result.report["lod_material_adaptations"] = [dict(target=target, source=source,
        reason="Choice-local stable LOD0 carrier; identical original VMT bytes and explicit per-LOD/skin replacements")
        for target, source in result.report["material_aliases"].items()]
    if result.report["lod_material_adaptations"]:
        result.report["warnings"].append("Lower-LOD-only authored materials use stable choice-local compiler slots "
            "with identical base VMT aliases and explicit per-LOD/skin material replacements; all source triangles retained.")
    for adaptation in result.report["material_adaptations"]:
        result.report["warnings"].append(f"Authored material {adaptation['source']} uses safe target alias "
                                         f"{adaptation['target']} to preserve its geometry and textures.")
    if result.report.get("skin_weights", {}).get("affected_vertices"):
        weights = result.report["skin_weights"]
        result.report["warnings"].append(f"Source 1 retained the strongest three bone influences on "
            f"{weights['affected_vertices']} vertices; maximum discarded source weight "
            f"{weights['max_discarded_weight']:.6%}, mean {weights['mean_discarded_weight']:.6%}.")
    report = output / "appearance_report.json"
    report.write_text(json.dumps(result.report, indent=2) + "\n", encoding="utf-8")
    result.files.append(report)
    return result


def sync_appearance_skeleton(result: AppearanceResult) -> None:
    """Apply remapped reference-SMD skeletons to corresponding flex-bearing DMX files."""
    from s2kit.pipelines.ragdoll_flex import sync_flex_skeleton

    for dmx, reference in result.flex_sources:
        sync_flex_skeleton(dmx, reference)
