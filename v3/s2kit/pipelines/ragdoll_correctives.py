"""Resolve authored procedural constraints and precompute Source 2 pose-space interpolation."""

from __future__ import annotations

import math
from typing import Any

import numpy as np


class CorrectiveError(ValueError):
    """An authored procedural input cannot be represented or resolved precisely."""


def string_token(value: str) -> int:
    """Compute Source 2's lowercase MurmurHash2 resource and bone-name token."""
    data = value.lower().encode('utf-8')
    multiplier = 0x5BD1E995
    result = 0x31415926 ^ len(data)
    position = 0
    while position + 4 <= len(data):
        number = int.from_bytes(data[position:position + 4], 'little')
        number = number * multiplier & 0xFFFFFFFF
        number ^= number >> 24
        number = number * multiplier & 0xFFFFFFFF
        result = (result * multiplier & 0xFFFFFFFF) ^ number
        position += 4
    tail = data[position:]
    if tail:
        result ^= int.from_bytes(tail, 'little')
        result = result * multiplier & 0xFFFFFFFF
    result ^= result >> 13
    result = result * multiplier & 0xFFFFFFFF
    return (result ^ result >> 15) & 0xFFFFFFFF


def rbf_kernel(distance, kernel: int, falloff: float):
    """Evaluate the kernels used by VRF's Source 2 pose-space constraint solver."""
    radius = np.asarray(distance, dtype=np.float64)
    scaled = radius / falloff if falloff > 0 else radius
    if kernel == 1:
        return 1.0 / np.sqrt(1.0 + scaled * scaled)
    if kernel == 2:
        return np.exp(-scaled * scaled)
    if kernel == 3:
        return radius
    if kernel == 4:
        return radius ** 3
    if kernel == 5:
        return radius ** 5
    if kernel == 6:
        with np.errstate(invalid='ignore', divide='ignore'):
            return radius * radius * np.log(radius)
    return np.sqrt(1.0 + scaled * scaled)


def build_radial_basis(samples, kernel: int = 0, falloff: float = 1.0) -> dict:
    """Solve the full interpolation matrix without normalizing or clamping basis weights."""
    samples = np.asarray(samples, dtype=np.float64)
    if samples.ndim != 2 or samples.shape[1] != 3 or not len(samples) or not np.isfinite(samples).all():
        raise CorrectiveError('RBF samples must be finite three-dimensional positions')
    if len(np.unique(samples, axis=0)) != len(samples):
        raise CorrectiveError('Duplicate RBF samples disable the source constraint')
    if not math.isfinite(float(falloff)):
        raise CorrectiveError('Source RBF falloff is not finite')
    kernel = int(kernel) if 0 <= int(kernel) <= 6 else 0
    distances = np.linalg.norm(samples[:, None] - samples[None, :], axis=2)
    matrix = rbf_kernel(distances, kernel, float(falloff))
    if not np.isfinite(matrix).all():
        raise CorrectiveError('Source RBF kernel is undefined at zero sample distance')
    try:
        inverse = np.linalg.inv(matrix)
    except np.linalg.LinAlgError as error:
        raise CorrectiveError('Singular source RBF interpolation matrix') from error
    return {'samples': samples.tolist(), 'inverse': inverse.tolist(), 'kernel': kernel,
            'falloff': float(falloff), 'condition_number': float(np.linalg.cond(matrix))}


def evaluate_radial_basis(basis: dict, point) -> np.ndarray:
    """Evaluate interpolating sample weights for local numeric verification and animation baking."""
    distances = np.linalg.norm(np.asarray(basis['samples']) - np.asarray(point), axis=1)
    return np.asarray(basis['inverse']) @ rbf_kernel(distances, basis['kernel'], basis['falloff'])


def evaluate_morph(driver: dict, point) -> np.ndarray:
    """Evaluate the authored output table, retaining unclamped extrapolation when requested."""
    weights = evaluate_radial_basis(driver['basis'], point)
    result = weights @ np.asarray(driver['output_weights'])
    return np.clip(result, 0.0, 1.0) if driver['clamp'] else result


def evaluate_pose_bone(driver: dict, point) -> list[np.ndarray]:
    """Evaluate raw positional weights and the source log-space quaternion average."""
    from s2kit.pipelines.skeleton_retarget import _q2m

    weights = evaluate_radial_basis(driver['basis'], point)
    outputs = np.asarray(driver['output_transforms'], dtype=np.float64)
    total = float(weights.sum())
    inverse = 1.0 / total if total > 0 else 1.0
    result = []
    for slave in range(outputs.shape[1]):
        values = outputs[:, slave]
        quaternions = values[:, 3:7].copy()
        quaternions[np.einsum('ij,j->i', quaternions, quaternions[0]) < 0] *= -1
        logarithm = np.zeros(4)
        for quaternion, weight in zip(quaternions, weights):
            length = float(np.linalg.norm(quaternion[:3]))
            scale = math.atan2(length, float(quaternion[3])) / length if length >= 1e-5 else 0.0
            logarithm[:3] += quaternion[:3] * scale * weight * inverse
            logarithm[3] += 0.5 * math.log(float(quaternion @ quaternion)) * weight * inverse
        angle = float(np.linalg.norm(logarithm[:3]))
        magnitude = math.exp(float(logarithm[3]))
        scale = math.sin(angle) * magnitude / angle if angle >= 1e-5 else 0.0
        quaternion = np.r_[logarithm[:3] * scale, math.cos(angle) * magnitude]
        transform = np.eye(4)
        transform[:3, :3] = _q2m(quaternion)
        transform[:3, 3] = weights @ values[:, :3]
        result.append(transform)
    return result


def source_pose_point(bone_world: np.ndarray, parent_world: np.ndarray, offset) -> np.ndarray:
    """Measure the reference attachment point in the original source bone parent's frame."""
    local = np.linalg.inv(parent_world) @ bone_world
    return local[:3, :3] @ np.asarray(offset) + local[:3, 3]


def _reference_name(value: dict, names: dict[int, str]) -> str:
    """Resolve explicit names before hashes, matching compiled Source 2 constraint precedence."""
    name = str(value.get('m_sName') or '')
    if name:
        return name
    return names.get(int(value.get('m_nBoneHash', -1)), '')


def _attachment_map(value) -> dict[str, dict]:
    """Accept named attachment maps or the portable model metadata attachment list."""
    if isinstance(value, dict):
        return value
    return {item['name']: item for item in value or []}


def _target(value: dict, frames: dict, attachments: dict, bones_by_hash: dict,
            attachments_by_hash: dict) -> dict:
    """Resolve one local bone or single-influence attachment target and its authored offset."""
    if value.get('m_bIsAttachment'):
        name = _reference_name(value, attachments_by_hash)
        attachment = attachments.get(name)
        if attachment is None:
            raise CorrectiveError(f'Missing attachment dependency {name or value.get("m_nBoneHash")}')
        if int(attachment.get('influences', 1)) != 1:
            raise CorrectiveError(f'Weighted attachment dependency {name} needs multi-influence evaluation')
        if attachment.get('ignore_rotation'):
            raise CorrectiveError(f'Attachment dependency {name} ignores bone rotation and requires a separate evaluator')
        bone = str(attachment.get('bone') or '')
        if bone not in frames:
            raise CorrectiveError(f'Attachment {name} depends on absent or model-root bone {bone!r}')
        position = attachment.get('position', attachment.get('offset', [0, 0, 0]))
        rotation = attachment.get('rotation', [0, 0, 0, 1])
    else:
        name = _reference_name(value, bones_by_hash)
        bone = name
        if bone not in frames:
            raise CorrectiveError(f'Missing source bone dependency {name or value.get("m_nBoneHash")}')
        position, rotation = [0, 0, 0], [0, 0, 0, 1]
    return {'bone': bone, 'attachment': name if value.get('m_bIsAttachment') else '',
            'attachment_position': list(position), 'attachment_rotation': list(rotation),
            'position_offset': list(value.get('m_vOffset') or [0, 0, 0]),
            'rotation_offset': list(value.get('m_qOffset') or [0, 0, 0, 1]),
            'weight': float(value.get('m_flWeight', 1))}


def _slaves(definition: dict, frames: dict, names: dict) -> list[dict]:
    """Resolve constrained bones against original source names without changing the target rig."""
    output = []
    for value in definition.get('m_slaves', []):
        name = _reference_name(value, names)
        if name not in frames:
            raise CorrectiveError(f'Missing constrained bone {name or value.get("m_nBoneHash")}')
        output.append({'bone': name, 'parent': frames[name].get('parent', ''),
                       'weight': float(value.get('m_flWeight', 1))})
    return output


def _pose_input(bone: str, attachment: dict, frames: dict) -> dict:
    """Resolve the source-local measuring bone and attachment offset for an RBF input."""
    if bone not in frames:
        raise CorrectiveError(f'Missing pose-space measuring bone {bone}')
    parent = frames[bone].get('parent', '')
    if not parent or parent not in frames:
        raise CorrectiveError(f'Pose-space measuring bone {bone} lacks its original source parent')
    if int(attachment.get('influences', 1)) != 1:
        raise CorrectiveError('Pose-space attachment must have one authored influence')
    return {'bone': bone, 'parent': parent,
            'offset': list(attachment.get('position', attachment.get('offset', [0, 0, 0])))}


def prepare_corrective_drivers(data: dict) -> dict[str, Any]:
    """Compile resolvable procedural records into a source-ordered self-contained runtime graph."""
    frames = data.get('source_frames', {})
    attachments = _attachment_map(data.get('source_attachments'))
    bones_by_hash = {string_token(name): name for name in frames}
    attachments_by_hash = {string_token(name): name for name in attachments}
    available = set(data.get('available_flexes') or [])
    twists = {int(item['source_index']): item for item in data.get('twist_constraints', [])}
    native = {int(item['source_index']): item for item in data.get('native_procedurals', [])}
    definitions = data.get('source_constraints', [])
    if len(native) != len(data.get('native_procedurals', [])):
        raise CorrectiveError('Native procedural ownership contains duplicate source indices')
    for index, record in native.items():
        if not 0 <= index < len(definitions) or definitions[index].get('_class') != record.get('source_type'):
            raise CorrectiveError('Native procedural ownership does not match its preserved source constraint')
    constraints, unsupported, inert = [], [], []
    for index, definition in enumerate(definitions):
        kind = definition.get('_class', '')
        try:
            if index in native:
                continue
            if index in twists:
                constraints.append(dict(twists[index], kind='twist'))
                continue
            if kind == 'CTiltTwistConstraint':
                raise CorrectiveError('Tilt/twist source references did not resolve against the target rig')
            record = {'source_index': index, 'source_type': kind}
            if kind == 'CBoneConstraintPoseSpaceMorph':
                name = definition.get('m_sAttachmentName', '')
                if name not in attachments:
                    raise CorrectiveError(f'Missing pose-space reference attachment {name}')
                record.update(_pose_input(definition.get('m_sBoneName', ''), attachments[name], frames))
                outputs = list(definition.get('m_outputMorph', []))
                if available and any(name not in available for name in outputs):
                    raise CorrectiveError('Pose-space output flex controller is absent from the compiled expression rig')
                inputs = definition.get('m_inputList') or []
                record.update(kind='pose_morph', outputs=outputs,
                              output_weights=[[float(item.get('m_outputWeightList', [])[slot])
                                               if slot < len(item.get('m_outputWeightList', [])) else 0.0
                                               for slot in range(len(outputs))] for item in inputs],
                              clamp=bool(definition.get('m_bClamp')),
                              basis=build_radial_basis([item['m_inputValue'] for item in inputs],
                                                       definition.get('m_eRbfType', 0), definition.get('m_flFalloff', 1)))
            elif kind == 'CBoneConstraintPoseSpaceBone':
                targets = definition.get('m_targets') or []
                if len(targets) != 1 or not targets[0].get('m_bIsAttachment'):
                    raise CorrectiveError('Pose-space bone constraint requires one local attachment target')
                target = _target(targets[0], frames, attachments, bones_by_hash, attachments_by_hash)
                inputs = definition.get('m_inputList') or []
                slaves = _slaves(definition, frames, bones_by_hash)
                if not slaves:
                    inert.append(record)
                    continue
                if any(len(item.get('m_outputTransformList', [])) < len(slaves) for item in inputs):
                    raise CorrectiveError('Pose-space output transform table is incomplete')
                if any(len(transform) != 7 for item in inputs for transform in item['m_outputTransformList']):
                    raise CorrectiveError('Pose-space output transform needs a position and XYZW quaternion')
                from s2kit.pipelines.skeleton_retarget import _q2m

                offset = (np.asarray(target['attachment_position'])
                          + _q2m(target['attachment_rotation']) @ np.asarray(target['position_offset']))
                record.update(_pose_input(target['bone'], {'position': offset.tolist()}, frames))
                record.update(kind='pose_bone', slaves=slaves,
                              output_transforms=[item['m_outputTransformList'] for item in inputs],
                              basis=build_radial_basis([item['m_inputValue'] for item in inputs],
                                                       definition.get('m_eRbfType', 0), definition.get('m_flFalloff', 1)))
            elif kind in ('CPointConstraint', 'COrientConstraint', 'CParentConstraint', 'CAimConstraint'):
                slaves = _slaves(definition, frames, bones_by_hash)
                if not slaves:
                    inert.append(record)
                    continue
                targets = [_target(value, frames, attachments, bones_by_hash, attachments_by_hash)
                           for value in definition.get('m_targets', [])]
                if not targets:
                    inert.append(record)
                    continue
                record.update(kind={'CPointConstraint': 'point', 'COrientConstraint': 'orient',
                                     'CParentConstraint': 'parent', 'CAimConstraint': 'aim'}[kind],
                              slaves=slaves, targets=targets,
                              up_vector=definition.get('m_vUpVector') or [0, 0, 1],
                              up_type=int(definition.get('m_nUpType', 1)),
                              aim_offset=definition.get('m_qAimOffset') or [0, 0, 0, 1])
            else:
                raise CorrectiveError(f'Procedural type {kind} has no implemented evaluator')
            constraints.append(record)
        except CorrectiveError as error:
            unsupported.append({'source_index': index, 'type': kind, 'reason': str(error)})
    return {'runtime_constraints': constraints, 'unsupported_runtime_constraints': unsupported,
            'inert_runtime_constraints': inert,
            'runtime_constraint_summary': {'resolved': len(constraints), 'inert': len(inert),
                                           'native': len(native),
                                           'unsupported': len(unsupported),
                                           'pose_morphs': sum(item['kind'] == 'pose_morph' for item in constraints),
                                           'pose_bones': sum(item['kind'] == 'pose_bone' for item in constraints)}}
