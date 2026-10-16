"""Audit compiled Source 1 model data against editable ragdoll sources and payloads."""

from __future__ import annotations

import re
import struct
from pathlib import Path

import numpy as np


class ModelAuditError(ValueError):
    """A compiled model contains a truncated or invalid serialized record."""


class _Binary:
    """Read bounded little-endian records using Valve SDK serialized offsets."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = self.path.read_bytes()

    def unpack(self, form: str, offset: int):
        size = struct.calcsize("<" + form)
        self.require(offset, size)
        return struct.unpack_from("<" + form, self.data, offset)

    def require(self, offset: int, size: int) -> None:
        if offset < 0 or size < 0 or offset + size > len(self.data):
            raise ModelAuditError(f"{self.path.name}: record outside file at {offset}+{size}")

    def string(self, offset: int) -> str:
        self.require(offset, 1)
        end = self.data.find(b"\0", offset)
        if end < 0:
            raise ModelAuditError(f"{self.path.name}: unterminated string at {offset}")
        return self.data[offset:end].decode("utf-8", errors="replace")

    def table(self, count: int, offset: int, stride: int) -> range:
        if count < 0 or count > 1_000_000:
            raise ModelAuditError(f"{self.path.name}: invalid record count {count}")
        self.require(offset, count * stride)
        return range(offset, offset + count * stride, stride)


def _rotation(angles) -> np.ndarray:
    """Convert Source QAngle pitch, yaw, roll degrees to an orthonormal matrix."""
    pitch, yaw, roll = np.radians(angles)
    sp, sy, sr = np.sin([pitch, yaw, roll])
    cp, cy, cr = np.cos([pitch, yaw, roll])
    return np.array([[cp * cy, sr * sp * cy - cr * sy, cr * sp * cy + sr * sy],
                     [cp * sy, sr * sp * sy + cr * cy, cr * sp * sy - sr * cy],
                     [-sp, sr * cp, cr * cp]])


def _quaternion(value) -> np.ndarray:
    """Convert serialized XYZW unit quaternions into matrices without sign ambiguity."""
    q = np.asarray(value, dtype=float)
    length = np.linalg.norm(q)
    if length < 1e-12 or not np.isfinite(q).all():
        raise ModelAuditError("Invalid compiled bone quaternion")
    x, y, z, w = q / length
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def _tokens(line: str) -> list[str]:
    """Tokenize generated QC and VMT names while retaining Windows path separators."""
    return [token[1:-1] if token.startswith('"') else token
            for token in re.findall(r'"[^"\n]*"|[{}]|[^\s{}"]+', line)]


def _qc_expectations(path: Path) -> dict:
    """Read the emitted QC declarations that have directly checkable binary equivalents."""
    result = dict(bones={}, bodygroups=[], attachments=[], hitboxes=[], jiggles=[], lods=0)
    group = None
    hitbox_set = "default"
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        values = _tokens(line.split("//", 1)[0])
        if not values:
            continue
        kind = values[0].casefold()
        if kind == "$definebone":
            result["bones"][values[1]] = dict(parent=values[2], position=list(map(float, values[3:6])),
                                               rotation=list(map(float, values[6:9])))
        elif kind in {"$body", "$model"}:
            result["bodygroups"].append(dict(name=values[1], choices=[False]))
        elif kind == "$bodygroup":
            group = dict(name=values[1], choices=[])
            result["bodygroups"].append(group)
        elif group is not None and kind in {"studio", "blank"}:
            group["choices"].append(kind == "blank")
        elif group is not None and kind == "}":
            group = None
        elif kind == "$attachment":
            rotation = list(map(float, values[values.index("rotate") + 1:values.index("rotate") + 4])) if "rotate" in values else [0, 0, 0]
            result["attachments"].append(dict(name=values[1], bone=values[2],
                                                position=list(map(float, values[3:6])), rotation=rotation))
        elif kind == "$hboxset":
            hitbox_set = values[1]
        elif kind == "$hbox":
            result["hitboxes"].append(dict(set=hitbox_set, group=int(values[1]), bone=values[2],
                                            minimum=list(map(float, values[3:6])), maximum=list(map(float, values[6:9]))))
        elif kind == "$jigglebone":
            result["jiggles"].append(values[1])
        elif kind in {"$lod", "$shadowlod"}:
            result["lods"] += 1
    return result


def read_compiled_flex_descriptions(mdl: Path) -> list[str]:
    """Read output flex names from studiohdr offsets 260/264 and relative four-byte descriptors."""
    source = _Binary(mdl)
    source.require(0, 268)
    if source.data[:4] != b"IDST" or source.unpack("i", 4)[0] not in {48, 49}:
        raise ModelAuditError("Flex descriptor reader requires a v48/v49 IDST model")
    count, index = source.unpack("ii", 260)
    return [source.string(offset + source.unpack("i", offset)[0])
            for offset in source.table(count, index, 4)]


def read_compiled_model(path: Path) -> dict:
    """Read v48/v49 studiohdr, bone, mesh, skin, flex and metadata records from an MDL."""
    source = _Binary(path)
    source.require(0, 308)
    if source.data[:4] != b"IDST":
        raise ModelAuditError(f"{source.path.name}: expected IDST model header")
    version, checksum = source.unpack("iI", 4)
    if version not in {48, 49}:
        raise ModelAuditError(f"Unsupported compiled MDL version {version}")
    declared_length, = source.unpack("i", 76)
    if declared_length != len(source.data):
        raise ModelAuditError(f"MDL length {declared_length} differs from file size {len(source.data)}")
    result = dict(version=version, checksum=checksum, bones=[], bodygroups=[], materials=[],
                  material_paths=[], skins=[], skin_indices=[], flex_descriptors=[], flex_controllers=[], flex_rules=[],
                  attachments=[], hitboxes=[], meshes=[])
    count, index = source.unpack("ii", 156)
    for offset in source.table(count, index, 216):
        name, parent = source.unpack("ii", offset)
        flags, proc_type, proc_index, physics_bone = source.unpack("iiii", offset + 160)
        bone = dict(name=source.string(offset + name), parent=parent,
                    position=list(source.unpack("3f", offset + 32)), quaternion=list(source.unpack("4f", offset + 44)),
                    flags=flags, procedural_type=proc_type, procedural_offset=proc_index, physics_bone=physics_bone)
        if not np.isfinite(bone["position"] + bone["quaternion"]).all():
            raise ModelAuditError(f"Bone {bone['name']} has non-finite bind values")
        if proc_type == 5:
            if not proc_index:
                raise ModelAuditError(f"Jiggle bone {bone['name']} has no procedural record")
            bone["jiggle_flags"], = source.unpack("i", offset + proc_index)
            bone["jiggle_parameters"] = list(source.unpack("34f", offset + proc_index + 4))
        result["bones"].append(bone)
    for bone in result["bones"]:
        parent = bone["parent"]
        if parent < -1 or parent >= len(result["bones"]):
            raise ModelAuditError(f"Bone {bone['name']} has invalid parent {parent}")
        bone["parent_name"] = result["bones"][parent]["name"] if parent >= 0 else ""
    count, index = source.unpack("ii", 204)
    for offset in source.table(count, index, 64):
        relative, = source.unpack("i", offset)
        result["materials"].append(source.string(offset + relative).replace("\\", "/"))
    count, index = source.unpack("ii", 212)
    for offset in source.table(count, index, 4):
        absolute, = source.unpack("i", offset)
        result["material_paths"].append(source.string(absolute).replace("\\", "/"))
    slots, families, index = source.unpack("iii", 220)
    for offset in source.table(families, index, max(1, slots * 2)):
        row = source.unpack(f"{slots}h", offset)
        if any(value < 0 or value >= len(result["materials"]) for value in row):
            raise ModelAuditError("Compiled skin family references an absent texture")
        result["skin_indices"].append(list(row))
        result["skins"].append([result["materials"][value] for value in row])
    count, index = source.unpack("ii", 232)
    for part_offset in source.table(count, index, 16):
        name, model_count, base, model_index = source.unpack("4i", part_offset)
        part = dict(name=source.string(part_offset + name), base=base, choices=[])
        for model_offset in source.table(model_count, part_offset + model_index, 148):
            source.require(model_offset, 148)
            model_name = source.data[model_offset:model_offset + 64].split(b"\0", 1)[0].decode("utf-8", errors="replace")
            mesh_count, mesh_index, vertex_count = source.unpack("3i", model_offset + 72)
            choice = dict(name=model_name, mesh_count=mesh_count, vertex_count=vertex_count, meshes=[])
            for mesh_offset in source.table(mesh_count, model_offset + mesh_index, 116):
                material, _, vertices, _, flex_count, flex_index = source.unpack("6i", mesh_offset)
                if material < 0 or material >= slots:
                    raise ModelAuditError(f"Mesh references invalid skin slot {material}")
                mesh = dict(material_slot=material, vertices=vertices, flexes=[])
                for flex_offset in source.table(flex_count, mesh_offset + flex_index, 60):
                    descriptor, = source.unpack("i", flex_offset)
                    vertex_deltas, delta_index, pair = source.unpack("3i", flex_offset + 20)
                    animation_type, = source.unpack("B", flex_offset + 32)
                    stride = 16 if animation_type == 0 else 18 if animation_type == 1 else 0
                    if not stride:
                        raise ModelAuditError(f"Unsupported compiled vertex animation type {animation_type}")
                    source.table(vertex_deltas, flex_offset + delta_index, stride)
                    flex = dict(descriptor=descriptor, vertex_deltas=vertex_deltas, pair=pair, animation_type=animation_type)
                    mesh["flexes"].append(flex)
                choice["meshes"].append(mesh)
                result["meshes"].append(mesh)
            part["choices"].append(choice)
        result["bodygroups"].append(part)
    count, index = source.unpack("ii", 240)
    for offset in source.table(count, index, 92):
        name, flags, bone = source.unpack("3i", offset)
        result["attachments"].append(dict(name=source.string(offset + name), flags=flags, bone=bone,
                                           matrix=np.array(source.unpack("12f", offset + 12)).reshape(3, 4).tolist()))
    count, index = source.unpack("ii", 172)
    for set_offset in source.table(count, index, 12):
        name, boxes, box_index = source.unpack("3i", set_offset)
        set_name = source.string(set_offset + name)
        for offset in source.table(boxes, set_offset + box_index, 68):
            bone, group = source.unpack("2i", offset)
            result["hitboxes"].append(dict(set=set_name, bone=bone, group=group,
                                            minimum=list(source.unpack("3f", offset + 8)), maximum=list(source.unpack("3f", offset + 20))))
    count, index = source.unpack("ii", 260)
    for offset in source.table(count, index, 4):
        name, = source.unpack("i", offset)
        result["flex_descriptors"].append(source.string(offset + name))
    count, index = source.unpack("ii", 268)
    for offset in source.table(count, index, 20):
        kind, name, _, low, high = source.unpack("iiiff", offset)
        result["flex_controllers"].append(dict(name=source.string(offset + name), type=source.string(offset + kind), minimum=low, maximum=high))
    count, index = source.unpack("ii", 276)
    for offset in source.table(count, index, 12):
        descriptor, operations, operations_index = source.unpack("3i", offset)
        source.table(operations, offset + operations_index, 8)
        result["flex_rules"].append(dict(descriptor=descriptor, operations=operations))
    descriptors = len(result["flex_descriptors"])
    for mesh in result["meshes"]:
        for flex in mesh["flexes"]:
            if flex["descriptor"] < 0 or flex["descriptor"] >= descriptors:
                raise ModelAuditError("Mesh flex references an absent descriptor")
            flex["name"] = result["flex_descriptors"][flex["descriptor"]]
    for rule in result["flex_rules"]:
        if rule["descriptor"] < 0 or rule["descriptor"] >= descriptors:
            raise ModelAuditError("Flex rule references an absent descriptor")
        rule["name"] = result["flex_descriptors"][rule["descriptor"]]
    return result


def _read_vtx(path: Path) -> dict:
    """Read packed OptimizedModel file, body, submodel and LOD headers from optimize.h."""
    source = _Binary(path)
    version, = source.unpack("i", 0)
    checksum, lod_count, _, part_count, part_index = source.unpack("I4i", 16)
    result = dict(version=version, checksum=checksum, lod_count=lod_count, bodygroups=[])
    if version != 7 or not 1 <= lod_count <= 8:
        raise ModelAuditError(f"Invalid VTX version/LOD count {version}/{lod_count}")
    for part_offset in source.table(part_count, part_index, 8):
        models, model_index = source.unpack("2i", part_offset)
        part = []
        for model_offset in source.table(models, part_offset + model_index, 8):
            count, index = source.unpack("2i", model_offset)
            lods = []
            for offset in source.table(count, model_offset + index, 12):
                meshes, mesh_index, distance = source.unpack("iif", offset)
                source.table(meshes, offset + mesh_index, 9)
                lods.append(dict(meshes=meshes, distance=distance))
            part.append(lods)
        result["bodygroups"].append(part)
    return result


def _audit_bones(compiled: dict, expected: dict, errors: list[str]) -> dict:
    """Check complete emitted bind definitions by name, parent, position and rotation."""
    bones = compiled["bones"]
    lookup = {bone["name"].casefold(): bone for bone in bones}
    if not 0 < len(bones) <= 256 or len(lookup) != len(bones):
        errors.append(f"Compiled bone count/names violate the 256-bone limit: {len(bones)}")
    maximum_position, maximum_rotation = 0.0, 0.0
    mismatches = []
    for name, source in expected["bones"].items():
        bone = lookup.get(name.casefold())
        if bone is None:
            errors.append(f"Missing defined bone {name}")
            continue
        if bone["parent_name"].casefold() != source["parent"].casefold():
            errors.append(f"Bone {name} parent {bone['parent_name']} differs from QC {source['parent']}")
        position = float(np.max(np.abs(np.asarray(bone["position"]) - source["position"])))
        rotation = float(np.max(np.abs(_quaternion(bone["quaternion"]) - _rotation(source["rotation"]))))
        maximum_position, maximum_rotation = max(maximum_position, position), max(maximum_rotation, rotation)
        if position > 0.0002 or rotation > 0.00002:
            mismatches.append(dict(name=name, position_error_inches=position, rotation_matrix_error=rotation))
    if mismatches:
        errors.append(f"{len(mismatches)} compiled bone bind transforms differ from $definebone")
    return dict(count=len(bones), expected=len(expected["bones"]), maximum_position_error_inches=maximum_position,
                maximum_rotation_matrix_error=maximum_rotation, mismatches=mismatches)


def _audit_bodygroups(compiled: dict, expected: dict, appearance: dict, errors: list[str]) -> dict:
    """Compare binary bodypart choice counts, blank alternatives and authored group reports."""
    lookup = {part["name"].casefold(): part for part in compiled["bodygroups"]}
    for group in expected["bodygroups"]:
        actual = lookup.get(group["name"].casefold())
        if actual is None or len(actual["choices"]) != len(group["choices"]):
            errors.append(f"Bodygroup {group['name']} choice count differs from QC")
            continue
        for index, blank in enumerate(group["choices"]):
            actual_blank = actual["choices"][index]["mesh_count"] == 0
            if actual_blank != blank:
                errors.append(f"Bodygroup {group['name']} choice {index} blank state differs from QC")
    for group in appearance.get("bodygroups", []):
        actual = lookup.get(group["name"].casefold())
        if actual is None or len(actual["choices"]) != len(group["choices"]):
            errors.append(f"Authored bodygroup {group['name']} choices were lost in compilation")
    return dict(count=len(lookup), groups=[dict(name=part["name"], choices=len(part["choices"]),
                                              blank_choices=[i for i, choice in enumerate(part["choices"]) if not choice["mesh_count"]])
                                         for part in compiled["bodygroups"]])


def _material_root(payload, mdl: Path) -> Path:
    """Resolve the concrete final material payload directory without searching unrelated games."""
    if isinstance(payload, dict):
        value = payload.get("material_root")
        if value:
            return Path(value)
        payload = payload.get("addon_root", payload.get("root", payload.get("path")))
    if payload:
        base = Path(payload)
        return base if base.name.casefold() == "materials" else base / "materials"
    for parent in mdl.parents:
        if parent.name.casefold() == "models":
            return parent.parent / "materials"
    return mdl.parent / "materials"


def _resource_path(root: Path, name: str, suffix: str) -> Path:
    """Resolve a relative Source resource token while rejecting paths outside its payload."""
    token = name.replace("\\", "/").strip("/")
    if token.casefold().endswith(suffix):
        token = token[:-len(suffix)]
    path = root / (token + suffix)
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ModelAuditError(f"Unsafe compiled resource path {name}") from exc
    return path


def _audit_materials(compiled: dict, appearance: dict, payload, mdl: Path, errors: list[str]) -> dict:
    """Validate compiled skin bindings and each shipped VMT and referenced VTF resource."""
    root = _material_root(payload, mdl)
    references, checked_textures = [], set()
    texture_parameters = {"$basetexture", "$basetexture2", "$bumpmap", "$bumpmap2", "$normalmap", "$detail", "$envmap",
                          "$envmapmask", "$phongexponenttexture", "$lightwarptexture", "$selfillummask",
                          "$blendmodulatetexture", "$dudvmap", "$flowmap", "$flow_noise_texture",
                          "$wrinkle", "$stretch", "$compress", "$corneatexture", "$iris", "$ambientoccltexture"}
    for token in compiled["materials"]:
        if "___error" in token.casefold() or token.casefold() in {"error", "debug/debugempty"}:
            errors.append(f"Compiled model contains error material {token}")
        candidates = [_resource_path(root, directory.rstrip("/") + "/" + token, ".vmt")
                      for directory in compiled["material_paths"] or [""]]
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if path is None:
            errors.append(f"Missing compiled material {token}: " + ", ".join(str(candidate) for candidate in candidates))
            continue
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        references.append(str(path))
        for match in re.finditer(r'"?(\$[A-Za-z_0-9]+)"?\s+(?:"([^"\r\n]*)"|([^\s{}]+))', text):
            parameter, texture = match[1].casefold(), (match[2] if match[2] is not None else match[3])
            if parameter not in texture_parameters or not texture:
                continue
            if texture.startswith("_rt_") or (parameter == "$envmap" and texture.casefold() == "env_cubemap"):
                continue
            resource = _resource_path(root, texture, ".vtf")
            checked_textures.add(str(resource))
            if not resource.is_file():
                errors.append(f"Missing texture {texture} referenced by {path.name}")
            else:
                with resource.open("rb") as stream:
                    if stream.read(4) != b"VTF\0":
                        errors.append(f"Invalid VTF header {resource}")
    authored = appearance.get("skins", [])
    if len(authored) > 1 and len(compiled["skins"]) != len(authored):
        errors.append(f"Compiled skin family count {len(compiled['skins'])} differs from source {len(authored)}")
    if authored and compiled["skins"]:
        aliases = {str(source).casefold(): str(target).casefold()
                   for source, target in appearance.get("material_map", {}).items()}
        original = [aliases.get(str(token).casefold(), str(token).casefold())
                    for token in authored[0].get("materials", [])]
        internal_slots = {str(item["compiler_material"]).casefold(): item
                          for item in appearance.get("material_slot_skins", [])}
        for base, item in internal_slots.items():
            if base not in {token.casefold() for token in compiled["skins"][0]}:
                errors.append(f"Missing internal material carrier skin slot {base}")
            if len(item["skin_materials"]) != len(compiled["skins"]):
                errors.append(f"Internal material carrier skin family count differs: {base}")
        for family_index, source in enumerate(authored[:len(compiled["skins"])]):
            replacements = dict(zip(original, [aliases.get(str(token).casefold(), str(token).casefold())
                                               for token in source.get("materials", [])]))
            for slot, base in enumerate(compiled["skins"][0]):
                target = compiled["skins"][family_index][slot].casefold()
                internal = internal_slots.get(base.casefold())
                wanted = (str(internal["skin_materials"][family_index]).casefold()
                          if internal and family_index < len(internal["skin_materials"])
                          else replacements.get(base.casefold(), base.casefold()))
                if target != wanted:
                    errors.append(f"Skin {family_index} slot {slot}: {target} differs from source {wanted}")
    return dict(materials=compiled["materials"], search_paths=compiled["material_paths"], material_root=str(root),
                family_count=len(compiled["skins"]), skin_bindings=compiled["skins"],
                skin_texture_indices=compiled.get("skin_indices", []), vmts=references,
                referenced_vtfs=sorted(checked_textures))


def _audit_metadata(compiled: dict, expected: dict, report: dict, errors: list[str]) -> dict:
    """Verify attachment transforms, hitbox bounds and actual STUDIO_PROC_JIGGLE records."""
    bones = compiled["bones"]
    for key in ("attachments", "hitboxes"):
        source = report.get(key, {})
        source_count = (source.get("count") if isinstance(source, dict) else
                        sum(item.get("status") != "unsupported" for item in source)
                        if isinstance(source, list) else None)
        if source_count is not None and len(compiled[key]) != source_count:
            errors.append(f"Compiled {key} count {len(compiled[key])} differs from source {source_count}")
    invalid_hitbox_bones, nonfinite_hitboxes, inverted_hitboxes = [], [], []
    for index, actual in enumerate(compiled["hitboxes"]):
        label = f"{index} ({actual['set']})"
        if not 0 <= actual["bone"] < len(bones):
            invalid_hitbox_bones.append(index)
            errors.append(f"Compiled hitbox {label} references invalid bone {actual['bone']}")
        minimum, maximum = np.asarray(actual["minimum"]), np.asarray(actual["maximum"])
        if not np.isfinite(minimum).all() or not np.isfinite(maximum).all():
            nonfinite_hitboxes.append(index)
            errors.append(f"Compiled hitbox {label} has non-finite bounds")
        elif np.any(minimum > maximum):
            inverted_hitboxes.append(index)
            errors.append(f"Compiled hitbox {label} has inverted extents")
    attachments = {item["name"].casefold(): item for item in compiled["attachments"]}
    for item in expected["attachments"]:
        actual = attachments.get(item["name"].casefold())
        if actual is None:
            errors.append(f"Missing attachment {item['name']}")
            continue
        bone = actual["bone"]
        if not 0 <= bone < len(bones) or bones[bone]["name"].casefold() != item["bone"].casefold():
            errors.append(f"Attachment {item['name']} bone differs from QC")
        matrix = np.asarray(actual["matrix"])
        if not np.allclose(matrix[:, 3], item["position"], atol=0.0002, rtol=0) or not np.allclose(matrix[:, :3], _rotation(item["rotation"]), atol=0.00002, rtol=0):
            errors.append(f"Attachment {item['name']} transform differs from QC")
    for item in expected["hitboxes"]:
        candidates = [actual for actual in compiled["hitboxes"] if actual["set"].casefold() == item["set"].casefold()
                      and actual["group"] == item["group"] and 0 <= actual["bone"] < len(bones)
                      and bones[actual["bone"]]["name"].casefold() == item["bone"].casefold()]
        if not any(np.allclose(actual["minimum"], item["minimum"], atol=0.0002, rtol=0)
                   and np.allclose(actual["maximum"], item["maximum"], atol=0.0002, rtol=0) for actual in candidates):
            errors.append(f"Hitbox {item['set']}/{item['bone']}/{item['group']} differs from QC")
    procedural = report.get("appearance", report).get("procedural", {})
    jiggles = {bone["name"].casefold(): bone for bone in bones if bone["procedural_type"] == 5 and bone["procedural_offset"]}
    wanted = set(expected["jiggles"]) | {item["target"] for item in procedural.get("jiggle_bones", [])}
    for name in wanted:
        if name.casefold() not in jiggles:
            errors.append(f"Jiggle bone {name} lacks a compiled type-5 procedural record")
    source_hitboxes = report.get("hitboxes", {})
    generated_hitboxes = isinstance(source_hitboxes, dict) and source_hitboxes.get("status") == "generated"
    return dict(attachments=len(compiled["attachments"]), expected_attachments=len(expected["attachments"]),
                hitboxes=len(compiled["hitboxes"]), expected_hitboxes=len(expected["hitboxes"]),
                source_hitbox_count=source_hitboxes.get("source_count", source_hitboxes.get("count"))
                if isinstance(source_hitboxes, dict) else None,
                generated_hitboxes=len(compiled["hitboxes"]) if generated_hitboxes else 0,
                hitbox_validation=dict(checked=len(compiled["hitboxes"]), invalid_bone_indices=invalid_hitbox_bones,
                                       nonfinite_bounds=nonfinite_hitboxes, inverted_extents=inverted_hitboxes),
                jiggle_bones=[dict(name=bone["name"], flags=bone["jiggle_flags"]) for bone in jiggles.values()])


def _audit_flexes(compiled: dict, appearance: dict, errors: list[str]) -> dict:
    """Require retained controllers, expression rules and nonempty compiled vertex deltas."""
    expected = appearance.get("flexes", {})
    controllers = {item["name"]: item for item in compiled["flex_controllers"]}
    for item in expected.get("controllers", []):
        name = item.get("m_szName", item.get("name", ""))
        if name not in controllers:
            errors.append(f"Missing flex controller {name}")
    descriptors, rules = set(compiled["flex_descriptors"]), {item["name"] for item in compiled["flex_rules"] if item["operations"]}
    deltas = {}
    for mesh in compiled["meshes"]:
        for item in mesh["flexes"]:
            deltas[item["name"]] = deltas.get(item["name"], 0) + item["vertex_deltas"]
    for name in expected.get("targets", []):
        if name not in descriptors or name not in rules:
            errors.append(f"Missing compiled flex descriptor/rule {name}")
    if expected.get("targets") and not sum(deltas.values()):
        errors.append("Authored flex rig has no compiled vertex deltas")
    return dict(controller_count=len(controllers), descriptors=len(descriptors), rules=len(compiled["flex_rules"]),
                delta_records=sum(deltas.values()), delta_records_by_target=deltas,
                targets_without_vertex_deltas=[name for name in expected.get("targets", []) if not deltas.get(name)])


def audit_compiled_model(mdl: Path, qc: Path, report: dict | None = None, material_payload=None) -> dict:
    """Return an acceptance report for compiled bones, appearance, materials and binary geometry tables."""
    mdl, qc, report = Path(mdl), Path(qc), report or {}
    errors = []
    result = dict(ok=False, mdl=str(mdl), qc=str(qc), errors=errors,
                  source_layout="Valve Source SDK 2013 studio.h/optimize.h v48-v49 serialized records")
    try:
        compiled = read_compiled_model(mdl)
        expected = _qc_expectations(qc)
        appearance = report.get("appearance", report)
        result["version"], result["checksum"] = compiled["version"], compiled["checksum"]
        result["bones"] = _audit_bones(compiled, expected, errors)
        if report.get("compiled_bones") is not None and len(compiled["bones"]) != report["compiled_bones"]:
            errors.append(f"Compiled bone count differs from reported {report['compiled_bones']}")
        result["bodygroups"] = _audit_bodygroups(compiled, expected, appearance, errors)
        result["materials"] = _audit_materials(compiled, appearance, material_payload, mdl, errors)
        result["metadata"] = _audit_metadata(compiled, expected, report, errors)
        result["flexes"] = _audit_flexes(compiled, appearance, errors)
        vtx_path = mdl.with_suffix(".dx90.vtx")
        vtx = _read_vtx(vtx_path)
        result["lods"] = vtx
        if vtx["checksum"] != compiled["checksum"]:
            errors.append("MDL/VTX checksums differ")
        if vtx["lod_count"] != expected["lods"] + 1:
            errors.append(f"Compiled VTX LOD count {vtx['lod_count']} differs from QC {expected['lods'] + 1}")
        if "lods" in appearance and vtx["lod_count"] != len(appearance["lods"]) + 1:
            errors.append(f"Compiled VTX LOD count differs from authored appearance {len(appearance['lods']) + 1}")
        if len(vtx["bodygroups"]) != len(compiled["bodygroups"]):
            errors.append("MDL/VTX bodygroup counts differ")
        for index, part in enumerate(vtx["bodygroups"][:len(compiled["bodygroups"])]):
            if len(part) != len(compiled["bodygroups"][index]["choices"]):
                errors.append(f"MDL/VTX bodygroup {index} choice counts differ")
            for choice in part:
                if len(choice) != vtx["lod_count"]:
                    errors.append(f"VTX bodygroup {index} has an incomplete submodel LOD table")
        vvd = _Binary(mdl.with_suffix(".vvd"))
        vvd.require(0, 64)
        if vvd.data[:4] != b"IDSV":
            errors.append("Invalid VVD header")
        vvd_checksum, vvd_lods = vvd.unpack("Ii", 8)
        if vvd_checksum != compiled["checksum"] or vvd_lods != vtx["lod_count"]:
            errors.append("MDL/VVD checksums or VVD/VTX LOD counts differ")
        result["vertices_per_lod"] = list(vvd.unpack("8i", 16))[:vvd_lods]
        from s2kit.pipelines.compiled_render_coverage import audit_render_coverage

        result["render_coverage"] = audit_render_coverage(mdl, qc, appearance, compiled)
        errors.extend(result["render_coverage"]["errors"])
    except (OSError, ModelAuditError, ValueError, IndexError, KeyError, struct.error) as exc:
        errors.append(f"Compiled model audit failed: {exc}")
    result["ok"] = not errors
    return result
