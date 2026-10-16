"""Inspect compiled VPhysics solids and calibrate compiler ragdoll parameter semantics."""

from __future__ import annotations

import json
import math
import re
import struct
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import trimesh


class PhyAuditError(ValueError):
    """Compiled physics is malformed or differs from its intended source representation."""


def write_audit_manifest(path: Path, result) -> Path:
    """Persist the exact rig and collision inputs needed for later independent compiled audits."""
    path = Path(path)
    skeleton = [{'name': joint.name, 'parent': joint.parent_joint_idx,
                 'translation': joint.rest_translation.tolist(),
                 'rotation': joint.rest_rotation.tolist()}
                for joint in result.skin.joints]
    shapes = [{'part': part, 'vertices': mesh.vertices.tolist(), 'report': report}
              for part, mesh, report in result.shapes]
    data = {'version': 1, 'skeleton': skeleton, 'root_joint_idx': result.skin.root_joint_idx,
            'helper_by_part': result.helper_by_part, 'bodies': result.body_reports,
            'joints': result.joint_reports, 'shapes': shapes, 'report': result.report}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, separators=(',', ':')) + '\n', encoding='utf-8')
    return path


def load_audit_manifest(path: Path):
    """Load prepared collision geometry without exporting meshes or sampling animations again."""
    from s2kit.pipelines.gltf_animation import GltfJoint, GltfSkin

    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get('version') != 1:
        raise PhyAuditError('Unsupported ragdoll physics audit manifest version')
    joints = [GltfJoint(item['name'], index, item['parent'], np.eye(4),
                        np.asarray(item['translation'], dtype=np.float64),
                        np.asarray(item['rotation'], dtype=np.float64), np.ones(3))
              for index, item in enumerate(data['skeleton'])]
    shapes = [(item['part'], trimesh.Trimesh(vertices=item['vertices'], process=False), item['report'])
              for item in data['shapes']]
    return SimpleNamespace(skin=GltfSkin(joints, data['root_joint_idx'], 0),
                           helper_by_part={int(key): value for key, value in data['helper_by_part'].items()},
                           body_reports=data['bodies'], joint_reports=data['joints'],
                           shapes=shapes, report=data['report'])


def _blocks(text: str) -> list[tuple[str, dict]]:
    """Read the flat keyvalue blocks emitted in ordinary Source 1 PHY files."""
    result = []
    for match in re.finditer(r'([A-Za-z0-9_]+)\s*\{([^{}]*)\}', text, re.S):
        pairs = re.findall(r'"([^"\r\n]+)"\s*"([^"\r\n]*)"', match[2])
        entry = dict(pairs)
        entry['_pairs'] = pairs
        result.append((match[1].lower(), entry))
    return result


def _convex_clouds(blob: bytes) -> list[np.ndarray]:
    """Read compact-surface leaf vertices and convert IVP metres into Source-local inches.

    A VPHY surface owns a relative-offset binary ledge tree. Internal ledges
    describe acceleration bounds, while terminal ledges are the actual convex
    collision pieces. The serialized layout uses 28-byte tree nodes, 16-byte
    ledge headers and 16-byte edge triangles. Every offset is checked against
    its solid boundary before any vertices are interpreted.
    """
    if len(blob) < 76 or blob[:4] != b'VPHY':
        raise PhyAuditError('Unsupported collision solid header')
    surface = 28
    if blob[surface + 44:surface + 48] != b'IVPS':
        raise PhyAuditError('Only ordinary IVPS compact collision surfaces are supported')
    root = surface + struct.unpack_from('<I', blob, surface + 32)[0]
    output = []
    pending, seen = [root], set()
    while pending:
        node = pending.pop()
        if node in seen or not 0 <= node <= len(blob) - 28:
            raise PhyAuditError('Invalid or cyclic compact collision tree')
        seen.add(node)
        right, ledge = struct.unpack_from('<ii', blob, node)
        if right:
            pending.extend((node + 28, node + right))
            continue
        offset = node + ledge
        if not ledge or not 0 <= offset <= len(blob) - 16:
            raise PhyAuditError('Invalid terminal collision ledge')
        vertices_offset, _, flags, triangle_count, _ = struct.unpack_from('<iiIHH', blob, offset)
        if triangle_count < 4 or offset + 16 + triangle_count * 16 > len(blob):
            raise PhyAuditError('Invalid convex triangle count')
        indices = set()
        for triangle in range(triangle_count):
            edges = struct.unpack_from('<6H', blob, offset + 20 + triangle * 16)
            indices.update(edges[::2])
        start = offset + vertices_offset
        if start < 0 or start + (max(indices) + 1) * 16 > len(blob):
            raise PhyAuditError('Convex vertex data escapes its solid boundary')
        points = np.asarray([struct.unpack_from('<3f', blob, start + index * 16)
                             for index in sorted(indices)], dtype=np.float64)
        points = points[:, [0, 2, 1]] / 0.0254
        points[:, 2] *= -1.0
        if not np.isfinite(points).all():
            raise PhyAuditError('Non-finite compiled collision vertices')
        output.append(points)
    return output


def read_phy(path: Path, *, geometry: bool = True) -> dict[str, Any]:
    """Read compiled bodies, constraints, collision rules and separate convex clouds."""
    data = Path(path).read_bytes()
    if len(data) < 16:
        raise PhyAuditError('Truncated PHY header')
    size, identifier, solid_count, checksum = struct.unpack_from('<4I', data)
    if size < 16 or size > len(data) or solid_count > 256:
        raise PhyAuditError('Invalid PHY header')
    position, clouds = size, []
    for index in range(solid_count):
        if position + 4 > len(data):
            raise PhyAuditError('Missing serialized PHY solid')
        length = struct.unpack_from('<I', data, position)[0]
        end = position + 4 + length
        if length < 76 or end > len(data):
            raise PhyAuditError('Truncated serialized PHY solid')
        clouds.append(_convex_clouds(data[position + 4:end]) if geometry else [])
        position = end
    text = data[position:].split(b'\x00', 1)[0].decode('ascii', errors='strict')
    blocks = _blocks(text)
    solids = [value for name, value in blocks if name == 'solid']
    constraints = [value for name, value in blocks if name == 'ragdollconstraint']
    if len(solids) != solid_count:
        raise PhyAuditError('PHY solid descriptions disagree with the binary header')
    for index, solid in enumerate(solids):
        solid['convexes'] = clouds[index]
        solid['convex_count'] = len(clouds[index]) if geometry else None
    return {'checksum': checksum, 'solid_count': solid_count, 'solids': solids,
            'constraints': constraints, 'collision_rules': [value for name, value in blocks
                                                            if name == 'collisionrules'],
            'text': text}


def _skin_worlds(skin) -> list[np.ndarray]:
    """Evaluate Source-space bind matrices in parent-first rig order."""
    from s2kit.pipelines.skeleton_retarget import _q2m

    worlds = []
    for joint in skin.joints:
        local = np.eye(4)
        local[:3, :3] = _q2m(joint.rest_rotation)
        local[:3, 3] = joint.rest_translation
        worlds.append(local if joint.parent_joint_idx is None else worlds[joint.parent_joint_idx] @ local)
    return worlds


def audit_compiled_phy(path: Path, result, *, mass_tolerance_kg: float = 0.02,
                       maximum_bounds_error_in: float = 0.06,
                       maximum_volume_error: float = 0.02,
                       allow_geometry_adaptation: bool = True) -> dict:
    """Compare actual compiled collision geometry and parameters with the prepared physics."""
    compiled = read_phy(path)
    expected = {body['helper']: body for body in result.body_reports}
    actual = {body['name']: body for body in compiled['solids']}
    errors, bodies, adaptations = [], [], []
    if set(expected) != set(actual):
        errors.append(f'Compiled physics bones differ: missing={sorted(set(expected) - set(actual))}, extra={sorted(set(actual) - set(expected))}')
    worlds = _skin_worlds(result.skin)
    inverse = {part: np.linalg.inv(worlds[index]) for part, index in result.helper_by_part.items()}
    for name in sorted(set(expected) & set(actual)):
        wanted, found = expected[name], actual[name]
        part = wanted['part']
        source_clouds = [mesh.vertices @ inverse[part][:3, :3].T + inverse[part][:3, 3]
                         for owner, mesh, report in result.shapes if owner == part]
        compiled_clouds = found['convexes']
        volume = sum(abs(float(trimesh.convex.convex_hull(points).volume)) for points in compiled_clouds)
        volume_error = abs(volume / wanted['export_volume_in3'] - 1.0)
        source_points, compiled_points = np.concatenate(source_clouds), np.concatenate(compiled_clouds)
        source_bounds = np.stack((source_points.min(axis=0), source_points.max(axis=0)))
        compiled_bounds = np.stack((compiled_points.min(axis=0), compiled_points.max(axis=0)))
        bounds_error = float(np.abs(source_bounds - compiled_bounds).max())
        mass_error = abs(float(found['mass']) - wanted['target_mass_kg'])
        if len(compiled_clouds) != wanted['shape_count']:
            errors.append(f'{name}: compiled {len(compiled_clouds)} convex pieces, expected {wanted["shape_count"]}')
        if mass_error > mass_tolerance_kg:
            errors.append(f'{name}: compiled mass differs by {mass_error:.6f} kg')
        if bounds_error > maximum_bounds_error_in or volume_error > maximum_volume_error:
            message = f'{name}: compiler convex rebuilding changed bounds by {bounds_error:.6f} in and volume by {volume_error:.3%}'
            if allow_geometry_adaptation:
                adaptations.append(message)
            else:
                errors.append(message)
        for field, key in [('inertia', 'target_inertia_scale'), ('damping', 'target_linear_damping'),
                           ('rotdamping', 'target_angular_damping')]:
            if not math.isclose(float(found.get(field, 0)), wanted[key], abs_tol=1e-5):
                errors.append(f'{name}: compiled {field} differs from target')
        bodies.append({'bone': name, 'compiled_convex_count': len(compiled_clouds),
                       'compiled_mass_kg': float(found['mass']), 'mass_error_kg': mass_error,
                       'compiled_volume_in3': volume, 'relative_volume_error': volume_error,
                       'maximum_bounds_error_in': bounds_error})
    indexed = {int(solid['index']): solid['name'] for solid in compiled['solids']}
    constraints = {(indexed[int(item['parent'])], indexed[int(item['child'])]): item
                   for item in compiled['constraints']}
    if len(constraints) != len(result.joint_reports):
        errors.append('Compiled joint count differs from the prepared physics tree')
    for wanted in result.joint_reports:
        pair = (wanted['parent_helper'], wanted['child_helper'])
        found = constraints.get(pair)
        if found is None:
            errors.append(f'Missing compiled constraint {pair}')
            continue
        for axis, minimum, maximum in wanted['target_axes']:
            for suffix, target in [('min', minimum), ('max', maximum), ('friction', wanted['target_friction'])]:
                if not math.isclose(float(found[axis + suffix]), target, abs_tol=2e-4):
                    errors.append(f'{pair[1]}: compiled {axis}{suffix} differs from target')
    expected_pairs = {tuple(sorted(pair)) for pair in result.report['collision_pairs']}
    actual_pairs = set()
    for rules in compiled['collision_rules']:
        for key, value in rules['_pairs']:
            if key.lower() == 'collisionpair':
                first, second = (int(index) for index in value.split(','))
                actual_pairs.add(tuple(sorted((indexed[first], indexed[second]))))
    if actual_pairs != expected_pairs:
        errors.append('Compiled collision pair whitelist differs from the prepared source filters')
    return {'ok': not errors, 'errors': errors, 'solid_count': compiled['solid_count'],
            'constraint_count': len(compiled['constraints']), 'bodies': bodies,
            'geometry_verified': True, 'geometry_within_tolerance': not adaptations,
            'adaptations': adaptations, 'checksum': compiled['checksum']}


def finalize_compiled_phy(path: Path, result) -> dict:
    """Restore requested per-body masses after compiler convex rebuilding redistributes them.

    Only solid mass keyvalues are rewritten. The compiler's collision binary,
    model checksum, constraint values and collision whitelist remain intact.
    The finalizer retains the compiler's original masses in its audit record.
    """
    path = Path(path)
    data = path.read_bytes()
    parsed = read_phy(path, geometry=False)
    desired = {body['helper']: body['target_mass_kg'] for body in result.body_reports}
    position = struct.unpack_from('<I', data)[0]
    for index in range(parsed['solid_count']):
        position += 4 + struct.unpack_from('<I', data, position)[0]
    tail = data[position:]
    separator = tail.find(b'\x00')
    text = tail[:separator if separator >= 0 else len(tail)].decode('ascii')
    changes = []

    def replace(match):
        block = match.group()
        name = re.search(r'"name"\s*"([^"\r\n]+)"', block)
        if name is None or name[1] not in desired:
            raise PhyAuditError('Cannot finalize an unexpected compiled physics bone')
        target = desired[name[1]]
        mass = re.search(r'"mass"\s*"([^"\r\n]+)"', block)
        if mass is None:
            raise PhyAuditError('Compiled body has no mass keyvalue')
        changes.append({'bone': name[1], 'compiler_mass_kg': float(mass[1]), 'final_mass_kg': target})
        return block[:mass.start(1)] + f'{target:.9f}' + block[mass.end(1):]

    rewritten = re.sub(r'\bsolid\s*\{[^{}]*\}', replace, text, flags=re.S)
    if len(changes) != len(desired):
        raise PhyAuditError('Cannot finalize masses with missing or additional compiled bodies')
    ending = tail[separator:] if separator >= 0 else b''
    path.write_bytes(data[:position] + rewritten.encode('ascii') + ending)
    report = {'method': 'compiled_phy_solid_mass_keyvalues', 'bodies': changes,
              'binary_collision_unchanged': True, 'checksum': parsed['checksum']}
    result.report['compiled_mass_finalization'] = report
    return report


def calibrate_compiler(studiomdl: Path, game: Path, scratch_dir: Path, *, on_log=None, cancel=None) -> dict:
    """Compile an isolated known ragdoll to measure the installed compiler's friction scale."""
    from s2kit.pipelines.gltf_animation import GltfJoint, GltfSkin, derive_bind_pose_local
    from s2kit.pipelines.ragdoll_physics import prepare_ragdoll_physics
    from s2kit.pipelines.smd_animation_export import CoordinateMode, SmdAnimationExporter, derive_definebone_lines
    from s2kit.pipelines.studiomdl import compile_qc, model_path_for

    scratch_dir = Path(scratch_dir).resolve()
    scratch_dir.mkdir(parents=True, exist_ok=True)
    cache_path = scratch_dir / 'compiler_calibration.json'
    identity = {'compiler': str(Path(studiomdl).resolve()), 'compiler_size': Path(studiomdl).stat().st_size,
                'compiler_modified_ns': Path(studiomdl).stat().st_mtime_ns, 'probe_version': 2}
    if cache_path.is_file():
        cached = json.loads(cache_path.read_text(encoding='utf-8'))
        if all(cached.get(key) == value for key, value in identity.items()):
            return cached
    joints = [GltfJoint('root', 0, None, np.eye(4), np.zeros(3), np.array([0, 0, 0, 1]), np.ones(3)),
              GltfJoint('child', 1, 0, np.eye(4), np.array([6, 0, 0]), np.array([0, 0, 0, 1]), np.ones(3))]
    bind0 = np.eye(4)
    bind1 = np.eye(4)
    bind1[0, 3] = 6
    document = {'m_boneNames': ['root', 'child'], 'm_boneParents': [0, 1],
                'm_bindPose': [bind0[:3].reshape(-1).tolist(), bind1[:3].reshape(-1).tolist()],
                'm_surfacePropertyHashes': [1977497166],
                'm_parts': [{'m_flMass': mass, 'm_rnShape': {'m_spheres': [
                    {'m_Sphere': {'m_vCenter': [0, 0, 0], 'm_flRadius': 2}, 'm_nSurfacePropertyIndex': 0}]}}
                            for mass in (2, 3)],
                'm_joints': [{'m_nType': 3, 'm_nBody1': 0, 'm_nBody2': 1,
                              'm_Frame1': [6, 0, 0, 0, 0, 0, 1], 'm_Frame2': [0, 0, 0, 0, 0, 0, 1],
                              'm_bEnableTwistLimit': True, 'm_TwistLimit': {'m_flMin': -0.2, 'm_flMax': 0.3}}]}
    result = prepare_ragdoll_physics(document, GltfSkin(joints, 0, 0), CoordinateMode())
    result.joint_reports[0]['target_friction'] = 1.0
    qc_lines = result.write_collision(scratch_dir / 'collision.smd')
    SmdAnimationExporter.write_animation(result.skin, [derive_bind_pose_local(result.skin)],
                                         scratch_dir / 'bind.smd', CoordinateMode())
    model_name = 's2kit_calibration/ragdoll_parameter_probe.mdl'
    qc = scratch_dir / 'probe.qc'
    qc.write_text('\n'.join([f'$modelname "{model_name}"', '$body body "collision.smd"',
                             '$surfaceprop "default"', *derive_definebone_lines(result.skin, CoordinateMode(), 90),
                             f'$root "{result.root_bone}"', '$sequence idle "bind.smd"', *qc_lines]) + '\n',
                  encoding='utf-8')
    if not compile_qc(qc, Path(studiomdl), Path(game), on_log=on_log, cancel=cancel):
        raise PhyAuditError('Compiler calibration probe failed')
    model_path = model_path_for(qc, Path(game))
    if model_path is None:
        raise PhyAuditError('Could not locate compiled calibration model')
    actual = read_phy(model_path.with_suffix('.phy'))
    scale = float(actual['constraints'][0]['zfriction'])
    if not math.isfinite(scale) or scale <= 0:
        raise PhyAuditError('Compiler produced invalid calibration friction')
    compiler_masses = [float(item['mass']) for item in actual['solids']]
    finalize_compiled_phy(model_path.with_suffix('.phy'), result)
    result.joint_reports[0]['target_friction'] = scale
    calibration = {**identity, 'friction_compile_scale': scale, 'probe_phy': str(model_path.with_suffix('.phy')),
                   'solid_count': actual['solid_count'],
                   'compiled_masses_kg': compiler_masses,
                   'geometry_audit': audit_compiled_phy(model_path.with_suffix('.phy'), result)}
    cache_path.write_text(json.dumps(calibration, indent=2) + '\n', encoding='utf-8')
    return calibration
