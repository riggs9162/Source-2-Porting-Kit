"""Write authored bodygroup choices, material families and supported model flexes."""

from __future__ import annotations

import copy
import json
import math
import re
import struct
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


def _quoted(value: str) -> str:
    """Quote identifiers that can safely appear in compiler input."""
    if any(character in value for character in ('"', '\n', '\r', '\0')):
        raise ValueError(f"Invalid compiler identifier {value!r}")
    return '"' + value + '"'


def _filename(value: str) -> str:
    """Use portable source filenames without changing displayed source names."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", value).strip("_") or "body"


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


def _read_dmx_primitives(path: Path, target_skin, source_coord, rename: dict) -> list[dict]:
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
    return [primitive for mesh in meshes
            for primitive in _read_dmx_mesh(mesh, mapped, source_bones, source_coord, path)]


def _read_dmx_mesh(mesh, mapped: dict, source_bones, source_coord, path: Path) -> list[dict]:
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
                           targets=targets, source_dmx=str(path)))
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


def write_reference(path: Path, skin, primitives: list[dict], coord) -> None:
    """Write exact selected meshes using the model's complete source skeleton."""
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
                    links = [(int(joint), float(weight)) for joint, weight in
                             zip(primitive["vj"][index], primitive["vw"][index]) if weight > 0]
                    if not links:
                        raise ValueError("A bodygroup mesh has an unweighted vertex")
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
    destination = output / (stem + ".smd")
    writer(destination, skin, selected, coord)
    result.files.append(destination)
    if not any(primitive["targets"] for primitive in selected):
        return destination.name
    from s2kit.pipelines.ragdoll_flex import write_flex_dmx

    dmx = destination.with_suffix(".dmx")
    write_flex_dmx(dmx, destination, selected, coord, rig, morph_transform=morph_transform,
                   define_controls=not result.flex_sources)
    result.files.append(dmx)
    result.flex_sources.append((dmx, destination))
    return dmx.name


def _write_authored_lods(gltf, skin, coord, fidelity, lookup, source_specs, output,
                          writer, rig, result, rename, primitive_transform, morph_transform,
                          cancel) -> list[str]:
    """Compose each bodygroup's authored lower-level meshes without flattening groups."""
    from s2kit.pipelines.native_port import coordinate_mode

    distances = [float(value) for value in fidelity.get("lod_distances", [])]
    if len(distances) < 2:
        return []
    if not all(math.isfinite(value) and value >= 0 for value in distances) or any(
            second <= first for first, second in zip(distances, distances[1:])):
        raise ValueError("Authored LOD thresholds must be finite and increase")
    sidecar_directory = Path(fidelity.get("_sidecar_directory", "."))
    source_coord = coordinate_mode(gltf)
    mesh_by_index = {mesh["index"]: mesh for mesh in fidelity["meshes"]}
    mesh_lookup = dict(lookup)
    sources = fidelity.get("dmx_sources", {})
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
                continue
            if not source["source"]:
                if selected_indices:
                    raise ValueError("An authored LOD adds geometry to a blank base bodygroup choice")
                continue
            if not selected_indices:
                qc.append(f'    removemodel {_quoted(source["source"])}')
                replacements.append(dict(base=source["source"], replacement=None))
                continue
            for index in selected_indices:
                if index in mesh_lookup:
                    continue
                relative = sources.get(str(index))
                if not relative:
                    raise ValueError(f"Authored lower-LOD source is missing for {mesh_by_index[index]['name']}; preserve model LOD sources first")
                path = sidecar_directory / relative
                prims = _read_dmx_primitives(path, skin, source_coord, rename)
                if primitive_transform is not None:
                    transformed = primitive_transform(prims)
                    if transformed is not None:
                        prims = transformed
                mesh_lookup[index] = prims
            selected = [primitive for index in selected_indices for primitive in mesh_lookup[index]]
            stem = "appearance_" + _filename(source["name"]) + f"_lod{level}"
            destination = _write_choice_source(stem, output, selected, skin, coord, writer,
                                               rig, result, morph_transform)
            qc.append(f'    replacemodel {_quoted(source["source"])} {_quoted(destination)}')
            replacements.append(dict(base=source["source"], replacement=destination,
                                     source_meshes=selected_indices))
            for primitive in selected:
                result.report["material_map"][primitive["material"]] = primitive["material"]
        qc.append("}")
        result.report["lods"].append(dict(level=level, source_threshold=distance,
                                          target_distance=distance, replacements=replacements,
                                          adaptation="Source 2 screen coverage threshold reused as an adjustable Source 1 distance"))
    return qc


def write_appearance(gltf: dict, buffer: bytes, skin, coord, out_dir: Path, *,
                     fidelity: dict | Path, modelname: str = "", bone_name_map=None,
                     reference_writer=None, morph_transform=None, primitive_transform=None,
                     source_model_rotation=None, cancel=None) -> AppearanceResult:
    """Write exact source-mask bodygroups and material identities without name inference."""
    fidelity = load_model_fidelity(fidelity)
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    rename = bone_name_map or {}
    primitives = extract_primitives(gltf, buffer, skin, bone_name_map=rename)
    if primitive_transform is not None:
        transformed = primitive_transform(primitives)
        if transformed is not None:
            primitives = transformed
    lookup = _authored_mesh_lookup(fidelity, primitives)
    automatic, groups = authored_bodygroups(fidelity)
    result = AppearanceResult()
    result.report = dict(bodygroups=copy.deepcopy(groups), automatic_meshes=automatic,
                         source_meshes=len(lookup), material_map={}, skins=[], lods=[],
                         unsupported=[], flexes={}, modelname=modelname,
                         warnings=list(fidelity.get("warnings", [])))
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
    written_names = set()
    for source in source_specs:
        check_cancelled(cancel)
        selected = [primitive for index in source["meshes"] for primitive in lookup.get(index, [])]
        if not selected:
            source["source"] = None
            continue
        stem = "appearance_" + _filename(source["name"])
        if stem.casefold() in written_names:
            raise ValueError(f"Appearance source filenames collide for {source['name']}")
        written_names.add(stem.casefold())
        source["source"] = _write_choice_source(stem, output, selected, skin, coord,
                                                 writer, rig, result, morph_transform)
    for source in source_specs:
        if source["group"] is None and source["source"]:
            result.qc_lines.append(f'$body "automatic" {_quoted(source["source"])}')
    for group in groups:
        result.qc_lines.extend([f'$bodygroup {_quoted(group["name"])}', "{"])
        for choice in group["choices"]:
            source = next(item for item in source_specs if item["group"] == group["name"] and item["choice"] == choice["value"])
            result.qc_lines.append(f'    studio {_quoted(source["source"])}' if source["source"] else "    blank")
        result.qc_lines.append("}")
    materials = {primitive["material"] for primitive in primitives}
    skins = fidelity.get("skins", [])
    for family in skins:
        tokens = [resource_token(material) for material in family["m_materials"]]
        materials.update(tokens)
        result.report["skins"].append(dict(name=family["m_name"], materials=tokens))
    if len(skins) > 1:
        sizes = {len(family["m_materials"]) for family in skins}
        if len(sizes) != 1:
            raise ValueError("Source material families have incompatible slot counts")
        result.qc_lines.extend(['$texturegroup "skinfamilies"', "{"])
        for family in result.report["skins"]:
            result.qc_lines.append("    { " + " ".join(_quoted(token) for token in family["materials"]) + " }")
        result.qc_lines.append("}")
    result.report["material_map"] = {material: material for material in sorted(materials)}
    result.qc_lines.extend(_write_authored_lods(
        gltf, skin, coord, fidelity, lookup, source_specs, output, writer, rig, result,
        rename, primitive_transform, morph_transform, cancel,
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
    procedural_qc, procedural_report = _procedural_qc(fidelity, rename, set(skin.joint_names))
    translated = {record["source_index"] for record in twists}
    translated.update(record["source_index"] for record in prepared["runtime_constraints"])
    translated.update(record["source_index"] for record in prepared["inert_runtime_constraints"])
    procedural_report["runtime_twists"] = twists
    procedural_report["runtime_constraint_summary"] = prepared["runtime_constraint_summary"]
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
    report = output / "appearance_report.json"
    report.write_text(json.dumps(result.report, indent=2) + "\n", encoding="utf-8")
    result.files.append(report)
    return result


def sync_appearance_skeleton(result: AppearanceResult) -> None:
    """Apply remapped reference-SMD skeletons to corresponding flex-bearing DMX files."""
    from s2kit.pipelines.ragdoll_flex import sync_flex_skeleton

    for dmx, reference in result.flex_sources:
        sync_flex_skeleton(dmx, reference)
