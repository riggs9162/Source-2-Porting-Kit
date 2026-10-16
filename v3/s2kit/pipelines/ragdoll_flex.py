"""Write native Source 1 DMX flex rigs on authored bodygroup submodels."""

from __future__ import annotations

import math
import re
import struct
from pathlib import Path

import numpy as np

from s2kit.vendor import valve_datamodel as dmx


def _mdl_string(data: bytes | bytearray, start: int) -> str:
    """Read one checked NUL-terminated UTF-8 studio-model string."""
    if not 0 <= start < len(data):
        raise ValueError("A compiled flex table references a string outside the model")
    end = data.find(b"\0", start)
    if end < 0:
        raise ValueError("A compiled flex string has no terminator")
    return bytes(data[start:end]).decode("utf-8")


def _compiled_operations(operations: list[dict], controllers: dict[str, int]) -> list[tuple[int, int]]:
    """Translate the supported source stack program to Source 1 studio opcodes."""
    opcodes = dict(CONST=1, FETCH1=2, ADD=4, SUB=5, MUL=6, DIV=7, MAX=13, MIN=14)
    stack = []
    for operation in operations:
        opcode = operation["opcode"]
        operand = int(operation["operand"]) & 0xffffffff
        if opcode == "CONST":
            stack.append([(1, operand)])
        elif opcode == "FETCH1":
            stack.append([(2, controllers[operation["controller"]])])
        elif opcode in ("NEG", "ABS"):
            if not stack:
                raise ValueError("A compiled source flex rule has an invalid stack")
            value = stack.pop()
            negative = [(1, 0), *value, (5, 0)]
            stack.append(negative if opcode == "NEG" else [*value, *negative, (13, 0)])
        elif opcode in opcodes:
            if len(stack) < 2:
                raise ValueError("A compiled source flex rule has an invalid stack")
            second, first = stack.pop(), stack.pop()
            stack.append([*first, *second, (opcodes[opcode], 0)])
        else:
            raise ValueError(f"Unsupported source flex opcode {opcode}")
    if len(stack) != 1:
        raise ValueError("A compiled source flex rule does not produce one value")
    return stack[0]


def install_source_flex_rig(model_path: Path, rig: dict) -> dict:
    """Restore authored controllers, ranges, postfix rules and UI in a compiled MDL.

    Valve's Source 1 DMX importer understands morph geometry and combination
    controls but lacks Source 2 DmeFlexRules. The importer therefore creates a
    temporary direct rig. This replaces only its controller, rule and UI tables;
    morph description indices, geometry offsets and companion checksums remain
    unchanged. Tables are appended and all pointers remain relative to their
    owning structure, as defined by Valve's public studio.h format.
    """
    controllers = rig.get("controllers", [])
    rules = rig.get("rules", {})
    if not controllers and not rules:
        return dict(installed=False, controllers=0, rules=0)
    if len(controllers) > 96:
        raise ValueError("Source flex rig exceeds Source 1's 96-controller limit")
    path = Path(model_path)
    data = bytearray(path.read_bytes())
    if len(data) < 408 or data[:4] != b"IDST" or struct.unpack_from("<i", data, 4)[0] not in (48, 49):
        raise ValueError("Source flex installation requires a Source 1 MDL version 48 or 49")
    if struct.unpack_from("<i", data, 76)[0] != len(data):
        raise ValueError("Compiled MDL length does not match its header")
    descriptor_count, descriptor_offset = struct.unpack_from("<ii", data, 260)
    if descriptor_count < 0 or descriptor_offset < 0 or descriptor_offset + descriptor_count * 4 > len(data):
        raise ValueError("Compiled morph-description table is outside the model")
    descriptors = {}
    for index in range(descriptor_count):
        record = descriptor_offset + index * 4
        name = _mdl_string(data, record + struct.unpack_from("<i", data, record)[0])
        if name in descriptors:
            raise ValueError(f"Compiled model has duplicate morph description {name}")
        descriptors[name] = index
    missing = set(rules) - descriptors.keys()
    if missing:
        raise ValueError("Compiled model omitted authored morphs: " + ", ".join(sorted(missing)))
    names = [controller["m_szName"] for controller in controllers]
    if len(names) != len(set(names)):
        raise ValueError("Authored flex-controller names must be unique")
    controller_indices = {name: index for index, name in enumerate(names)}
    programs = [(descriptors[name], _compiled_operations(operations, controller_indices))
                for name, operations in rules.items()]
    data.extend(b"\0" * (-len(data) % 4))
    controller_offset = len(data)
    data.extend(b"\0" * (20 * len(controllers)))
    rule_offset = len(data)
    data.extend(b"\0" * (12 * len(programs)))
    ui_offset = len(data)
    data.extend(b"\0" * (20 * len(controllers)))
    string_offsets = {}

    def add_string(value: str) -> int:
        """Intern one model string in the appended flex payload."""
        if value not in string_offsets:
            string_offsets[value] = len(data)
            data.extend(value.encode("utf-8") + b"\0")
        return string_offsets[value]

    for index, controller in enumerate(controllers):
        record = controller_offset + index * 20
        name = controller["m_szName"]
        type_offset = add_string(str(controller.get("m_szType", "default")))
        name_offset = add_string(name)
        low, high = float(controller.get("min", 0)), float(controller.get("max", 1))
        if not math.isfinite(low) or not math.isfinite(high) or low > high:
            raise ValueError(f"Invalid source flex-controller range for {name}")
        struct.pack_into("<iiiff", data, record, type_offset - record, name_offset - record,
                         -1, low, high)
        ui_record = ui_offset + index * 20
        struct.pack_into("<iiiiBBBB", data, ui_record, name_offset - ui_record,
                         record - ui_record, 0, 0, 0, 0, 0, 0)
    data.extend(b"\0" * (-len(data) % 4))
    for index, (descriptor, operations) in enumerate(programs):
        record = rule_offset + index * 12
        struct.pack_into("<iii", data, record, descriptor, len(operations), len(data) - record)
        for opcode, operand in operations:
            data.extend(struct.pack("<iI", opcode, operand))
    struct.pack_into("<ii", data, 268, len(controllers), controller_offset)
    struct.pack_into("<ii", data, 276, len(programs), rule_offset)
    struct.pack_into("<ii", data, 384, len(controllers), ui_offset)
    struct.pack_into("<i", data, 76, len(data))
    temporary = path.with_name(path.name + ".flex.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
    return dict(installed=True, controllers=len(controllers), rules=len(programs),
                morph_descriptions=descriptor_count, controller_names=names,
                preserved_checksum=struct.unpack_from("<i", data, 8)[0])


def _read_reference(path: Path) -> tuple[list[dict], list[dict], list[str]]:
    """Read a generated reference SMD without welding or changing corner order."""
    text = path.read_text(encoding="utf-8")
    node_text = text.split("nodes\n", 1)[1].split("\nend", 1)[0]
    bones = []
    for line in node_text.splitlines():
        match = re.fullmatch(r'\s*(\d+)\s+"([^"]+)"\s+(-?\d+)\s*', line)
        if match is None or int(match[1]) != len(bones):
            raise ValueError("Reference SMD requires ordered bone indices")
        bones.append(dict(name=match[2], parent=int(match[3])))
    pose_text = text.split("skeleton\n", 1)[1].split("\nend", 1)[0]
    for line in pose_text.splitlines():
        if line.startswith("time ") or not line.strip():
            continue
        values = line.split()
        index = int(values[0])
        bones[index]["position"] = [float(value) for value in values[1:4]]
        bones[index]["rotation"] = [float(value) for value in values[4:7]]
    body = text.split("triangles\n", 1)[1].split("\nend", 1)[0].splitlines()
    corners, materials = [], []
    for offset in range(0, len(body), 4):
        if offset + 4 > len(body):
            raise ValueError("Truncated reference SMD triangle")
        materials.append(body[offset])
        for line in body[offset + 1:offset + 4]:
            values = line.split()
            count = int(values[9]) if len(values) > 9 else 0
            links = [(int(values[10 + index * 2]), float(values[11 + index * 2]))
                     for index in range(count)] if count else [(int(values[0]), 1.0)]
            corners.append(dict(position=[float(value) for value in values[1:4]],
                                normal=[float(value) for value in values[4:7]],
                                uv=[float(value) for value in values[7:9]], links=links))
    return bones, corners, materials


def _quaternion(rotation) -> list[float]:
    """Convert SMD XYZ radians into DMX XYZW orientation."""
    x, y, z = (value * 0.5 for value in rotation)
    sx, sy, sz, cx, cy, cz = math.sin(x), math.sin(y), math.sin(z), math.cos(x), math.cos(y), math.cos(z)
    return [sx * cy * cz - cx * sy * sz, cx * sy * cz + sx * cy * sz,
            cx * cy * sz - sx * sy * cz, cx * cy * cz + sx * sy * sz]


def _transform(document, name: str, position=None, orientation=None):
    """Create a DMX local transform with explicit identity defaults."""
    element = document.add_element(name, "DmeTransform")
    element["position"] = dmx.Vector3(position or [0.0, 0.0, 0.0])
    element["orientation"] = dmx.Quaternion(orientation or [0.0, 0.0, 0.0, 1.0])
    return element


def _set_skeleton(document, model, mesh_dag, bones: list[dict], corners: list[dict], vertex_data) -> None:
    """Write Source 1 joint lists and indexed weights from the current SMD bind."""
    elements = []
    transforms = []
    for bone in bones:
        element = document.add_element(bone["name"], "DmeJoint")
        quaternion = _quaternion(bone["rotation"])
        element["transform"] = _transform(document, bone["name"], bone["position"], quaternion)
        element["children"] = dmx.make_array([], dmx.Element)
        elements.append(element)
        transforms.append(_transform(document, bone["name"] + "_bind", bone["position"], quaternion))
    roots = []
    for index, bone in enumerate(bones):
        if bone["parent"] < 0:
            roots.append(elements[index])
        else:
            elements[bone["parent"]]["children"].append(elements[index])
    roots.append(mesh_dag)
    transforms.append(_transform(document, "mesh_bind"))
    model["children"] = dmx.make_array(roots, dmx.Element)
    model["jointList"] = dmx.make_array([*elements, mesh_dag], dmx.Element)
    model["jointTransforms"] = dmx.make_array([element["transform"] for element in elements] + [mesh_dag["transform"]], dmx.Element)
    bind = document.add_element("bind", "DmeTransformList")
    bind["transforms"] = dmx.make_array(transforms, dmx.Element)
    model["baseStates"] = dmx.make_array([bind], dmx.Element)
    count = max(len(corner["links"]) for corner in corners)
    weights, indices = [], []
    for corner in corners:
        total = sum(weight for _, weight in corner["links"])
        links = [(joint, weight / total) for joint, weight in corner["links"]]
        links += [(0, 0.0)] * (count - len(links))
        indices.extend(joint for joint, _ in links)
        weights.extend(weight for _, weight in links)
    vertex_data["jointCount"] = count
    vertex_data["jointWeights"] = dmx.make_array(weights, float)
    vertex_data["jointIndices"] = dmx.make_array(indices, int)


def _delta_stream(document, target, name: str, values: np.ndarray) -> None:
    """Write only nonzero delta entries while preserving original corner indices."""
    indices = np.flatnonzero(np.any(np.abs(values) > 1e-12, axis=1))
    target[name] = dmx.make_array(values[indices].tolist(), dmx.Vector3)
    target[name + "Indices"] = dmx.make_array(indices.tolist(), int)


def write_flex_dmx(path: Path, reference: Path, primitives: list[dict], coord, rig: dict,
                    *, morph_transform=None, define_controls: bool = True) -> None:
    """Write shared morph descriptions without Source 2-only DMX expression elements.

    Only the first source defines temporary direct controllers. Every subsequent
    bodygroup and LOD references the same morph descriptions through an operator
    with no controller definitions. Install the authored rig after compiling.
    """
    from s2kit.pipelines.ragdoll_appearance import _geometry_vectors

    bones, corners, materials = _read_reference(reference)
    document = dmx.DataModel("model", 18)
    root = document.add_element("root", "DmElement")
    document.root = root
    model = document.add_element(path.stem, "DmeModel")
    model["transform"] = _transform(document, "model")
    root["model"], root["skeleton"] = model, model
    mesh = document.add_element(path.stem, "DmeMesh")
    mesh["visible"] = True
    dag = document.add_element(path.stem, "DmeDag")
    dag["shape"] = mesh
    dag["transform"] = _transform(document, "mesh")
    vertex_data = document.add_element("bind", "DmeVertexData")
    vertex_data["vertexFormat"] = dmx.make_array(["positions", "normals", "textureCoordinates", "jointWeights", "jointIndices"], str)
    vertex_data["flipVCoordinates"] = True
    for key, source, element_type in (("positions", "position", dmx.Vector3),
                                      ("normals", "normal", dmx.Vector3),
                                      ("textureCoordinates", "uv", dmx.Vector2)):
        vertex_data[key] = dmx.make_array([corner[source] for corner in corners], element_type)
        vertex_data[key + "Indices"] = dmx.make_array(list(range(len(corners))), int)
    mesh["bindState"] = vertex_data
    mesh["currentState"] = vertex_data
    mesh["baseStates"] = dmx.make_array([vertex_data], dmx.Element)
    _set_skeleton(document, model, dag, bones, corners, vertex_data)
    face_sets = []
    for material in dict.fromkeys(materials):
        face_set = document.add_element(material, "DmeFaceSet")
        material_element = document.add_element(material, "DmeMaterial")
        material_element["mtlName"] = material
        face_set["material"] = material_element
        face_set["faces"] = dmx.make_array([
            value for triangle, current in enumerate(materials) if current == material
            for value in (triangle * 3, triangle * 3 + 1, triangle * 3 + 2, -1)
        ], int)
        face_sets.append(face_set)
    mesh["faceSets"] = dmx.make_array(face_sets, dmx.Element)
    names = list(dict.fromkeys([*(rig["expressions"] if define_controls else []),
                              *(name for primitive in primitives for name in primitive["targets"])]))
    if define_controls and len(names) > 96:
        raise ValueError("Morph import exceeds Source 1's 96-controller limit")
    delta_states = []
    for name in names:
        positions, normals = [], []
        for primitive in primitives:
            target = primitive["targets"].get(name, {})
            count = len(primitive["vertices"])
            delta_positions = target.get("POSITION", np.zeros((count, 3)))
            delta_normals = target.get("NORMAL", np.zeros((count, 3)))
            if morph_transform is None:
                transformed_positions = _geometry_vectors(delta_positions, coord)
                transformed_normals = _geometry_vectors(delta_normals, coord, direction=True)
            else:
                transform = np.asarray(morph_transform, dtype=np.float64)[:3, :3]
                scales = np.linalg.norm(transform, axis=0)
                if not np.allclose(scales, scales[0]) or scales[0] <= 0:
                    raise ValueError("Flex conversion requires a rigid or uniformly scaled mesh transform")
                transformed_positions = delta_positions @ transform.T
                transformed_normals = delta_normals @ (transform / scales[0]).T
            positions.append(transformed_positions[primitive["indices"]])
            normals.append(transformed_normals[primitive["indices"]])
        delta = document.add_element(name, "DmeVertexDeltaData")
        delta["vertexFormat"] = dmx.make_array(["positions", "normals"], str)
        _delta_stream(document, delta, "positions", np.vstack(positions))
        _delta_stream(document, delta, "normals", np.vstack(normals))
        delta_states.append(delta)
    mesh["deltaStates"] = dmx.make_array(delta_states, dmx.Element)
    mesh["deltaStateWeights"] = dmx.make_array([[0.0, 0.0] for _ in delta_states], dmx.Vector2)
    mesh["deltaStateWeightsLagged"] = dmx.make_array([[0.0, 0.0] for _ in delta_states], dmx.Vector2)
    combination = document.add_element("combinationOperator", "DmeCombinationOperator")
    controls = []
    for name in names if define_controls else []:
        control = document.add_element(name, "DmeCombinationInputControl")
        control["rawControlNames"] = dmx.make_array([name], str)
        control["stereo"] = False
        control["eyelid"] = False
        control["flexMin"] = 0.0
        control["flexMax"] = 1.0
        control["wrinkleScales"] = dmx.make_array([0.0], float)
        controls.append(control)
    combination["controls"] = dmx.make_array(controls, dmx.Element)
    combination["controlValues"] = dmx.make_array([[0.0, 0.0, 0.5] for _ in controls], dmx.Vector3)
    combination["controlValuesLagged"] = dmx.make_array([[0.0, 0.0, 0.5] for _ in controls], dmx.Vector3)
    combination["usesLaggedValues"] = False
    combination["dominators"] = dmx.make_array([], dmx.Element)
    combination["targets"] = dmx.make_array([mesh], dmx.Element)
    root["combinationOperator"] = combination
    document.write(str(path), "binary", 5)


def sync_flex_skeleton(path: Path, reference: Path) -> None:
    """Rebuild DMX skeleton and vertex links after physics/PM remaps its paired SMD."""
    document = dmx.load(str(path))
    bones, corners, _ = _read_reference(reference)
    model = document.root["model"]
    dag = next(element for element in document.elements if element.type == "DmeDag")
    vertex_data = dag["shape"]["currentState"]
    if len(vertex_data["positions"]) != len(corners):
        raise ValueError("Remapped SMD changed flex-DMX corner count")
    _set_skeleton(document, model, dag, bones, corners, vertex_data)
    document.write(str(path), "binary", 5)
