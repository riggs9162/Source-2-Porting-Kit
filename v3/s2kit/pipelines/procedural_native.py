"""Compile exactly representable authored bone constraints into native procedural data."""

from __future__ import annotations

from pathlib import Path
import struct

import numpy as np
from trimesh.transformations import euler_from_matrix, quaternion_from_matrix

from s2kit.pipelines.skeleton_retarget import _q2m


def _matrix(position, quaternion) -> np.ndarray:
    """Build one source-local rigid frame without changing its authored axes."""
    value = np.eye(4)
    value[:3, :3] = _q2m(quaternion)
    value[:3, 3] = position
    return value


def _vrd_name(name: str) -> str:
    """Use the compiler's namespace-stripped procedural-bone identifier."""
    if not name or any(character.isspace() for character in name) or any(
        character in name for character in '\"\0\r\n'
    ):
        raise ValueError("Native procedural bone names must be single safe tokens")
    return name.removeprefix("ValveBiped.")


def _normalized(value, label: str) -> np.ndarray:
    """Reject degenerate axes rather than assigning a new authored orientation."""
    vector = np.asarray(value, dtype=float)
    length = np.linalg.norm(vector)
    if vector.shape != (3,) or not np.isfinite(vector).all() or length < 1e-8:
        raise ValueError(f"{label} is not a finite nonzero three-dimensional vector")
    return vector / length


def _constant_translation(gltf: dict, buffer: bytes, source_name: str) -> tuple[bool, float]:
    """Require every exported translation key and cubic tangent to retain the source bind origin."""
    from s2kit.pipelines.gltf_animation import read_accessor

    nodes = {index: node for index, node in enumerate(gltf.get("nodes", []))
             if node.get("name") == source_name}
    if len(nodes) != 1:
        raise ValueError("Native procedural animation verification needs one unambiguous source bone")
    maximum = 0.0
    for animation in gltf.get("animations", []):
        for channel in animation.get("channels", []):
            target = channel.get("target", {})
            if target.get("node") not in nodes or target.get("path") != "translation":
                continue
            node = nodes[target["node"]]
            if "matrix" in node:
                neutral = np.asarray(node["matrix"], dtype=float).reshape(4, 4).T[:3, 3]
            else:
                neutral = np.asarray(node.get("translation", [0, 0, 0]), dtype=float)
            sampler = animation["samplers"][channel["sampler"]]
            values = np.asarray(read_accessor(gltf, buffer, sampler["output"]), dtype=float)
            if values.ndim != 2 or values.shape[1] != 3 or not np.isfinite(values).all():
                raise ValueError("Native procedural translation keys are not finite three-dimensional values")
            if sampler.get("interpolation", "LINEAR") not in {"LINEAR", "STEP", "CUBICSPLINE"}:
                raise ValueError("Native procedural translation uses an unsupported interpolation mode")
            if sampler.get("interpolation", "LINEAR") == "CUBICSPLINE":
                if len(values) % 3:
                    raise ValueError("Native procedural translation has invalid cubic keys")
                tangents = values.reshape(-1, 3, 3)[:, [0, 2]]
                maximum = max(maximum, float(np.max(np.abs(tangents), initial=0)))
                values = values.reshape(-1, 3, 3)[:, 1]
            maximum = max(maximum, float(np.max(np.abs(values - neutral), initial=0)))
    return maximum <= 1e-7, maximum


def _parent_local_aim(constraint: dict, frames: dict, skin) -> dict:
    """Reduce a full-weight aim at its own parent to an exact constant local transform."""
    slaves, targets = constraint["slaves"], constraint["targets"]
    aim_targets = targets[:-1] if len(targets) > 1 else targets
    if constraint["up_type"] != 3 or len(slaves) != 1 or len(aim_targets) != 1:
        raise ValueError("Native constant aim requires one slave, one aim target and parent-relative up")
    slave, target = slaves[0], aim_targets[0]
    if abs(slave["weight"] - 1) > 1e-7 or abs(target["weight"] - 1) > 1e-7:
        raise ValueError("Native constant aim does not reproduce partial constraint blending")
    if target["bone"] != slave["parent"]:
        raise ValueError("Aim target does not share the constrained bone's authored parent frame")
    joints = {joint.name: joint for joint in skin.joints}
    slave_frame, parent_frame = frames[slave["bone"]], frames[slave["parent"]]
    name, parent_name = slave_frame["target"], parent_frame["target"]
    joint, parent = joints[name], joints[parent_name]
    actual_parent = skin.joints[joint.parent_joint_idx].name if joint.parent_joint_idx is not None else ""
    if actual_parent != parent_name or parent.parent_joint_idx is None:
        raise ValueError("Native control hierarchy differs from the authored parent-relative frame")
    source_local = (np.linalg.inv(np.asarray(parent_frame["source_bind_world"]))
                    @ np.asarray(slave_frame["source_bind_world"]))
    attachment = (_matrix(target["attachment_position"], target["attachment_rotation"])
                  @ _matrix(target["position_offset"], target["rotation_offset"]))
    direction = _normalized(attachment[:3, 3] - source_local[:3, 3], "Authored aim direction")
    up = _normalized(constraint["up_vector"], "Authored up vector")
    up = _normalized(up - direction * np.dot(up, direction), "Projected authored up vector")
    side = _normalized(np.cross(direction, up), "Authored aim side vector")
    desired_source = source_local.copy()
    desired_source[:3, :3] = np.column_stack((side, direction, up)) @ _q2m(constraint["aim_offset"])
    local = (np.asarray(parent_frame["target_to_source"]) @ desired_source
             @ np.asarray(slave_frame["source_to_target"]))
    quaternion = quaternion_from_matrix(local)[[1, 2, 3, 0]]
    return dict(source_index=int(constraint["source_index"]), source_type=constraint["source_type"],
        source_slave=slave["bone"], bone=name, parent=parent_name, control=parent_name,
        control_parent=skin.joints[parent.parent_joint_idx].name,
        position=local[:3, 3].tolist(), quaternion=quaternion.tolist(),
        euler_xyz_degrees=np.degrees(euler_from_matrix(local, axes="sxyz")).tolist(),
        trigger_quaternion=[0.0, 0.0, 0.0, 1.0], tolerance_degrees=360.0,
        method="exact_parent_local_aim_quatinterp",
        derivation="Full-weight parent-up aim and target share the same moving parent frame")


def _exclusive_slave(data: dict, record: dict) -> bool:
    """Require native helpers to have a single authored writer across the complete source graph."""
    from s2kit.pipelines.ragdoll_correctives import string_token

    name = record["source_slave"]
    token = string_token(name)
    for index, definition in enumerate(data.get("source_constraints", [])):
        if index == record["source_index"]:
            continue
        for slave in definition.get("m_slaves", []):
            if slave.get("m_sName") == name or slave.get("m_nBoneHash") == token:
                return False
    return True


def authored_native_aims(data: dict, skin, *, gltf: dict | None = None,
                         buffer: bytes | None = None) -> tuple[list[dict], list[dict]]:
    """Select parent-local aims with fixed origins and exact singularity-free native equivalents."""
    selected, rejected = [], []
    for constraint in data.get("runtime_constraints", []):
        if constraint.get("kind") != "aim":
            continue
        index = int(constraint["source_index"])
        try:
            record = _parent_local_aim(constraint, data.get("source_frames", {}), skin)
            if not _exclusive_slave(data, record):
                raise ValueError("Another authored constraint also writes this native helper")
            if gltf is not None:
                if buffer is None:
                    raise ValueError("Native procedural animation verification needs the glTF buffer")
                constant, error = _constant_translation(gltf, buffer, record["source_slave"])
                if not constant:
                    raise ValueError("Authored translation animation changes the constrained bone's aim origin")
                record["maximum_authored_translation_key_error"] = error
            selected.append(record)
        except (KeyError, ValueError) as error:
            rejected.append(dict(source_index=index, reason=str(error)))
    return selected, rejected


def write_native_procedurals(data: dict, skin, output: Path, *, gltf: dict | None = None,
                             buffer: bytes | None = None) -> dict:
    """Write exact authored VRD quaternion helpers without modifying runtime ownership."""
    selected, rejected = authored_native_aims(data, skin, gltf=gltf, buffer=buffer)
    if not selected:
        return dict(qc_lines=[], files=[], records=[], rejected=rejected)
    lines = []
    for record in selected:
        names = [record[key] for key in ("bone", "parent", "control_parent", "control")]
        lines.append("<helper> " + " ".join(_vrd_name(name) for name in names))
        lines.append("<basepos> " + " ".join(f"{number:.9f}" for number in record["position"]))
        values = [record["tolerance_degrees"], 0, 0, 0, *record["euler_xyz_degrees"], 0, 0, 0]
        lines.append("<trigger> " + " ".join(f"{number:.9f}" for number in values))
        lines.append("")
    path = Path(output) / "authored_procedurals.vrd"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="ascii")
    return dict(qc_lines=[f'$proceduralbones "{path.name}"'], files=[path], records=selected, rejected=rejected)


def audit_native_procedurals(model: Path, records: list[dict]) -> dict:
    """Require actual native quaternion procedures, exact controls, flags and one authored trigger."""
    if not records:
        return dict(ok=True, errors=[], bones=[])
    from s2kit.pipelines.compiled_model_audit import read_compiled_model

    metadata = read_compiled_model(model)
    data = Path(model).read_bytes()
    bone_offset = struct.unpack_from("<i", data, 160)[0]
    lookup = {bone["name"]: (index, bone) for index, bone in enumerate(metadata["bones"])}
    errors, audited = [], []
    for record in records:
        if record["bone"] not in lookup:
            errors.append(f'Authored native helper is absent: {record["bone"]}')
            continue
        index, bone = lookup[record["bone"]]
        if bone["procedural_type"] != 2 or not bone["procedural_offset"] or not bone["flags"] & 4:
            errors.append(f'Authored native quaternion procedure was lost: {record["bone"]}')
            continue
        if (not 0 <= bone["parent"] < len(metadata["bones"])
                or metadata["bones"][bone["parent"]]["name"] != record["parent"]):
            errors.append(f'Native helper parent differs: {record["bone"]}')
        offset = bone_offset + index * 216 + bone["procedural_offset"]
        if not 0 <= offset <= len(data) - 12:
            errors.append(f'Native helper procedure is truncated: {record["bone"]}')
            continue
        control, count, trigger_offset = struct.unpack_from("<3i", data, offset)
        if not 0 <= control < len(metadata["bones"]) or metadata["bones"][control]["name"] != record["control"]:
            errors.append(f'Native helper control differs: {record["bone"]}')
        elif (metadata["bones"][control]["parent"] < 0
              or metadata["bones"][metadata["bones"][control]["parent"]]["name"] != record["control_parent"]):
            errors.append(f'Native helper control-parent frame differs: {record["bone"]}')
        if count != 1:
            errors.append(f'Native helper has {count} triggers instead of one: {record["bone"]}')
            continue
        if not 0 <= offset + trigger_offset <= len(data) - 48:
            errors.append(f'Native helper trigger is truncated: {record["bone"]}')
            continue
        inverse_tolerance, *values = struct.unpack_from("<12f", data, offset + trigger_offset)
        trigger, position, quaternion = values[:4], values[4:7], values[7:11]
        if (not np.isfinite([inverse_tolerance, *values]).all()
                or abs(np.linalg.norm(trigger) - 1) > 1e-5
                or abs(np.linalg.norm(quaternion) - 1) > 1e-5):
            errors.append(f'Native helper trigger has invalid numeric values: {record["bone"]}')
            continue
        position_error = float(np.max(np.abs(np.asarray(position) - record["position"])))
        rotation_error = float(np.max(np.abs(_q2m(quaternion) - _q2m(record["quaternion"]))))
        trigger_error = float(np.max(np.abs(_q2m(trigger) - np.eye(3))))
        tolerance_error = abs(inverse_tolerance - 1 / np.radians(record["tolerance_degrees"]))
        if max(position_error, rotation_error, trigger_error, tolerance_error) > 1e-5:
            errors.append(f'Native helper encoded trigger differs: {record["bone"]}')
        audited.append(dict(bone=record["bone"], source_index=record["source_index"],
            procedural_type=bone["procedural_type"], control=control, triggers=count,
            maximum_position_component_error=position_error, maximum_rotation_component_error=rotation_error,
            maximum_trigger_component_error=trigger_error, inverse_tolerance_error=tolerance_error))
    return dict(ok=not errors, errors=errors, bones=audited)
