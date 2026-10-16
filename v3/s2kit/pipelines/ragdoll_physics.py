"""Preserve authored articulated physics with a separate pivot-aligned skeleton."""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import trimesh

from s2kit.pipelines.dmx_mesh import smd_vertex_line
from s2kit.pipelines.gltf_animation import GltfJoint, GltfSkin, derive_bind_pose_local
from s2kit.pipelines.model_metadata import parse_kv3_documents
from s2kit.pipelines.native_physics import _collision_normals, _qc_name
from s2kit.pipelines.physics import (
    CUBIC_INCH_IN_CUBIC_METRES, THICKEN_TARGET_IN, _hull_of_points,
    _thinnest_direction, convex_piece_budget, thicken_thin_pieces,
)
from s2kit.pipelines.skeleton_retarget import _m2q, _q2m
from s2kit.pipelines.smd_animation_export import CoordinateMode, SmdAnimationExporter


MAX_SHAPE_ERROR_IN = 0.05
MAX_SHAPE_VOLUME_ERROR = 0.01
MAX_BONES = 256
MAX_BODIES = 32

_SOURCE_SURFACES = {
    1977497166: ('default', 2000.0, 0.8, 0.25),
    3722016785: ('npc_combine_soldier', 2000.0, 0.5, 0.0),
    2084844629: ('npc_combine_soldier_gear', 2000.0, 0.8, 0.25),
    2254283372: ('npc_headcrab_flesh', 900.0, 0.4, 0.02),
    4210780020: ('npc_zombie_flesh', 900.0, 0.6, 0.0),
}


class RagdollPhysicsError(ValueError):
    """A source rig or physics aggregate cannot produce a valid ragdoll."""


def _matrix(position, rotation) -> np.ndarray:
    """Build one rigid homogeneous transform from a translation and XYZW quaternion."""
    matrix = np.eye(4)
    matrix[:3, :3] = _q2m(rotation)
    matrix[:3, 3] = position
    if not np.isfinite(matrix).all():
        raise RagdollPhysicsError('Non-finite rigid transform')
    return matrix


def _rigid(value, label: str) -> np.ndarray:
    """Normalize rounded PHYS rotations while rejecting scale and reflection."""
    array = np.asarray(value, dtype=np.float64)
    if array.size != 12 or not np.isfinite(array).all():
        raise RagdollPhysicsError(f'{label}: expected a finite 3x4 bind transform')
    matrix = np.eye(4)
    matrix[:3] = array.reshape(3, 4)
    rotation = matrix[:3, :3]
    if (not np.allclose(rotation.T @ rotation, np.eye(3), atol=2e-5)
            or np.linalg.det(rotation) < 0.99998):
        raise RagdollPhysicsError(f'{label}: bind transform contains scale or reflection')
    left, _, right = np.linalg.svd(rotation)
    matrix[:3, :3] = left @ right
    return matrix


def _frame(value) -> np.ndarray:
    """Read a PHYS joint anchor and quaternion without losing its local origin."""
    if len(value) != 7 or not np.isfinite(value).all() or np.linalg.norm(value[3:]) < 1e-8:
        raise RagdollPhysicsError('Invalid Source 2 joint frame')
    return _matrix(value[:3], value[3:])


def _source_world(skin: GltfSkin, frame: dict, coord: CoordinateMode) -> list[np.ndarray]:
    """Evaluate a source local pose and convert all model transforms into Source inches."""
    transform = np.eye(4)
    if coord.swap_axes:
        transform[:3, :3] = _q2m(coord.rotation)
    world: list[np.ndarray | None] = [None] * len(skin.joints)
    pending = set(range(len(skin.joints)))
    while pending:
        progressed = []
        for index in sorted(pending):
            parent = skin.joints[index].parent_joint_idx
            if parent is not None and world[parent] is None:
                continue
            position, rotation, scale = frame[index]
            if not np.allclose(scale, 1.0, atol=1e-5):
                raise RagdollPhysicsError('Ragdoll animation cannot preserve scaled bones')
            local = _matrix(np.asarray(position) * coord.scale, rotation)
            world[index] = transform @ local if parent is None else world[parent] @ local
            progressed.append(index)
        if not progressed:
            raise RagdollPhysicsError('Source skeleton has a cycle or missing parent')
        pending.difference_update(progressed)
    return world


def surface_properties_from_dump(text: str) -> dict[int, dict]:
    """Read resolved Source 2 material density and physical properties from VSURF DATA."""
    result = {}
    for document in parse_kv3_documents(text):
        for item in document.get('SurfacePropertiesList', []):
            if 'm_nameHash' in item:
                result[int(item['m_nameHash'])] = item
    return result


def _surface(hash_value: int, properties: dict[int, dict]) -> dict:
    """Resolve an exact VSURF entry, or the verified Alyx defaults for known hashes."""
    entry = properties.get(int(hash_value))
    if entry:
        return copy.deepcopy(entry)
    known = _SOURCE_SURFACES.get(int(hash_value))
    if known:
        name, density, friction, elasticity = known
        return {'surfacePropertyName': name, 'm_nameHash': int(hash_value),
                'physics': {'density': density, 'friction': friction, 'elasticity': elasticity}}
    return {'surfacePropertyName': 'default', 'm_nameHash': int(hash_value),
            'unresolved': True, 'physics': {'density': 2000.0, 'friction': 0.8, 'elasticity': 0.25}}


@lru_cache(maxsize=5)
def _unit_sphere(subdivisions: int) -> trimesh.Trimesh:
    """Cache deterministic inscribed sphere geometry shared by capsule shapes."""
    return trimesh.creation.icosphere(subdivisions=subdivisions, radius=1.0)


def _primitive_mesh(kind: str, data: dict) -> tuple[trimesh.Trimesh, dict]:
    """Tessellate analytic spheres and capsules against an explicit geometric error budget."""
    source = data['m_Sphere' if kind == 'sphere' else 'm_Capsule']
    radius = float(source['m_flRadius'])
    centers = np.asarray(source['m_vCenter'], dtype=np.float64).reshape(-1, 3)
    if radius <= 0 or not math.isfinite(radius) or not np.isfinite(centers).all():
        raise RagdollPhysicsError('Source primitive has an invalid radius or center')
    if len(centers) != (1 if kind == 'sphere' else 2):
        raise RagdollPhysicsError('Source primitive has the wrong number of endpoints')
    length = 0.0 if kind == 'sphere' else float(np.linalg.norm(centers[1] - centers[0]))
    exact_volume = math.pi * radius * radius * length + 4.0 * math.pi * radius ** 3 / 3.0
    mesh = None
    for subdivisions in range(1, 5):
        sphere = _unit_sphere(subdivisions)
        distances = np.einsum('ij,ij->i', sphere.face_normals, sphere.vertices[sphere.faces[:, 0]])
        error = radius * (1.0 - float(distances.min()))
        if error > MAX_SHAPE_ERROR_IN:
            continue
        points = np.concatenate([sphere.vertices * radius + center for center in centers])
        mesh = _hull_of_points(points)
        if mesh is None:
            continue
        volume_error = abs(float(mesh.volume) / exact_volume - 1.0)
        if volume_error <= MAX_SHAPE_VOLUME_ERROR:
            break
    else:
        raise RagdollPhysicsError('Source primitive exceeds the tessellation fidelity budget')
    return mesh, {'kind': kind, 'source_radius_in': radius, 'source_length_in': length,
                  'source_volume_in3': exact_volume, 'tessellation_subdivisions': subdivisions,
                  'tessellation_error_in': error, 'tessellation_volume_error': volume_error}


def _prepare_shape(kind: str, data: dict) -> tuple[trimesh.Trimesh, dict]:
    """Keep each authored shape separate and report any compiler thickness repair."""
    if kind in ('sphere', 'capsule'):
        mesh, report = _primitive_mesh(kind, data)
    else:
        hull = data.get('m_Hull') or {}
        mesh = _hull_of_points(np.asarray(hull.get('m_Vertices'), dtype=np.float64))
        if mesh is None:
            raise RagdollPhysicsError('Source convex hull has no closed volume')
        report = {'kind': kind, 'source_volume_in3': float(hull.get('m_flVolume') or mesh.volume),
                  'tessellation_error_in': 0.0, 'tessellation_volume_error': 0.0}
    original_bounds = mesh.bounds.copy()
    original_volume = float(mesh.volume)
    repaired = thicken_thin_pieces(mesh)
    mesh = repaired.mesh
    if mesh is None or repaired.dropped or not mesh.is_watertight:
        raise RagdollPhysicsError('Compiler thickness repair lost source collision geometry')
    report.update(export_volume_in3=float(mesh.volume), source_mesh_volume_in3=original_volume,
                  thickened=bool(repaired.thickened), minimum_width_in=_thinnest_direction(mesh)[1],
                  thickness_bounds_growth_in=float(np.abs(mesh.bounds - original_bounds).max()),
                  collision_attribute_index=int(data.get('m_nCollisionAttributeIndex', 0)),
                  surface_property_index=int(data.get('m_nSurfacePropertyIndex', 0)),
                  export_vertices=len(mesh.vertices), export_triangles=len(mesh.faces))
    return mesh, report


def _nearest_physical_parent(index: int, skin: GltfSkin, selected: set[int]) -> int | None:
    """Find the closest source ancestor represented by a generated fallback body."""
    seen = set()
    parent = skin.joints[index].parent_joint_idx
    while parent is not None:
        if parent in seen:
            raise RagdollPhysicsError('Cyclic source skeleton')
        if parent in selected:
            return parent
        seen.add(parent)
        parent = skin.joints[parent].parent_joint_idx
    return None


def _ao_fallback(model_data: dict, skin: GltfSkin, worlds: list[np.ndarray]) -> dict:
    """Generate explicitly adapted humanoid collision from authored AO proxy capsules."""
    text = (model_data.get('m_modelInfo') or {}).get('m_keyValueText', '')
    keys = parse_kv3_documents(text)
    capsules = keys[0].get('ao_proxy_capsule_list', []) if keys else []
    if not capsules:
        raise RagdollPhysicsError('Model has no source physics or authored AO proxy capsules')
    lookup = {joint.name: index for index, joint in enumerate(skin.joints)}
    names = list(dict.fromkeys(str(item['bonename']) for item in capsules))
    if any(name not in lookup for name in names):
        raise RagdollPhysicsError('AO proxy refers to a bone absent from the source skeleton')
    indices = [lookup[name] for name in names]
    selected = set(indices)
    parts = []
    for name in names:
        shapes = [{'m_nSurfacePropertyIndex': 0, 'm_nCollisionAttributeIndex': 0,
                   'm_Capsule': {'m_vCenter': [item['point0'], item['point1']],
                                 'm_flRadius': float(item['radius'])}}
                  for item in capsules if str(item['bonename']) == name]
        parts.append({'m_flMass': 0.0, 'm_flInertiaScale': 2.0,
                      'm_flLinearDamping': 0.01, 'm_flAngularDamping': 0.4,
                      'm_rnShape': {'m_capsules': shapes}})
    joints = []
    for part, source in enumerate(indices):
        parent = _nearest_physical_parent(source, skin, selected)
        if parent is None:
            continue
        relative = np.linalg.inv(worlds[parent]) @ worlds[source]
        lower = skin.joints[source].name.lower()
        hinge = 'arm_lower' in lower or 'leg_lower' in lower
        minimum, maximum = (-149.0, 4.0) if 'arm_lower' in lower else (-8.0, 126.0)
        joints.append({'m_nType': 3 if hinge else 4, 'm_nBody1': indices.index(parent),
                       'm_nBody2': part,
                       'm_Frame1': [*relative[:3, 3], *_m2q(relative[:3, :3])],
                       'm_Frame2': [0, 0, 0, 0, 0, 0, 1],
                       'm_bEnableCollision': False, 'm_bEnableSwingLimit': not hinge,
                       'm_SwingLimit': {'m_flMin': 0, 'm_flMax': math.radians(35)},
                       'm_bEnableTwistLimit': True,
                       'm_TwistLimit': {'m_flMin': math.radians(minimum if hinge else -25),
                                        'm_flMax': math.radians(maximum if hinge else 25)},
                       '_generated': True})
    return {'m_boneNames': names, 'm_boneParents': list(range(len(names))),
            'm_bindPose': [worlds[index][:3].reshape(-1).tolist() for index in indices],
            'm_parts': parts, 'm_joints': joints, 'm_surfacePropertyHashes': [3722016785],
            'm_collisionAttributes': [], '_generated_from_ao_proxy': True}


@dataclass
class RagdollPhysicsResult:
    """Pivot-aligned rig, source-preserving remapping and collision compilation inputs."""

    skin: GltfSkin
    index_map: dict[int, int]
    root_bone: str
    source_document: dict
    report: dict[str, Any]
    warnings: list[str]
    helper_by_part: dict[int, int]
    original_skin: GltfSkin = field(repr=False)
    original_coord: CoordinateMode = field(repr=False)
    helper_offsets: dict[int, np.ndarray] = field(repr=False)
    part_sources: list[int] = field(repr=False)
    shapes: list[tuple[int, trimesh.Trimesh, dict]] = field(repr=False)
    body_reports: list[dict] = field(repr=False)
    joint_reports: list[dict] = field(repr=False)
    friction_scale: float = 1.0
    rigid_helpers: dict[int, tuple[int, np.ndarray]] = field(default_factory=dict, repr=False)

    def remap_frame(self, frame: dict) -> dict:
        """Preserve original animated bone worlds while adding moving physical helper frames."""
        source = _source_world(self.original_skin, frame, self.original_coord)
        worlds = {self.index_map[index]: matrix for index, matrix in enumerate(source)}
        for part, helper in self.helper_by_part.items():
            worlds[helper] = source[self.part_sources[part]] @ self.helper_offsets[part]
        for helper, (parent, local) in self.rigid_helpers.items():
            worlds[helper] = worlds[parent] @ local
        result = {}
        for index, joint in enumerate(self.skin.joints):
            parent = joint.parent_joint_idx
            local = worlds[index] if parent is None else np.linalg.inv(worlds[parent]) @ worlds[index]
            result[index] = (local[:3, 3].copy(), _m2q(local[:3, :3]), np.ones(3))
        return result

    def remap_smd(self, path: Path) -> None:
        """Replace a Source-space native SMD bind header and remap intact triangle weights."""
        path = Path(path)
        text = path.read_text(encoding='utf-8')
        tail = text.split('triangles\n', 1)[1] if 'triangles\n' in text else None
        ok, message = SmdAnimationExporter.write_animation(
            self.skin, [derive_bind_pose_local(self.skin)], path, CoordinateMode())
        if not ok:
            raise RagdollPhysicsError(message)
        if tail is None:
            return
        lines = tail.splitlines()
        for start in range(0, len(lines) - 1, 4):
            if len(lines[start:start + 4]) != 4:
                raise RagdollPhysicsError(f'Incomplete triangle in {path}')
            for index in range(start + 1, start + 4):
                values = lines[index].split()
                links = [(int(values[0]), 1.0)]
                if len(values) > 9:
                    count = int(values[9])
                    links = [(int(values[10 + slot * 2]), float(values[11 + slot * 2]))
                             for slot in range(count)]
                mapped = [(self.index_map[joint], weight) for joint, weight in links]
                lines[index] = smd_vertex_line([float(value) for value in values[1:9]], mapped).rstrip('\n')
        with path.open('a', encoding='utf-8', newline='\n') as output:
            output.write('triangles\n' + '\n'.join(lines) + '\n')

    def write_collision(self, path: Path) -> list[str]:
        """Write separate rigid convex pieces and return the calibrated ragdoll QC directives."""
        path = Path(path)
        ok, message = SmdAnimationExporter.write_animation(
            self.skin, [derive_bind_pose_local(self.skin)], path, CoordinateMode())
        if not ok:
            raise RagdollPhysicsError(message)
        normals = _collision_normals(len(self.shapes))
        with path.open('a', encoding='utf-8', newline='\n') as output:
            output.write('triangles\n')
            for ordinal, (part, mesh, report) in enumerate(self.shapes):
                for face in mesh.faces:
                    output.write('physics\n')
                    for vertex in face:
                        output.write(smd_vertex_line([*mesh.vertices[vertex], *normals[ordinal], 0, 0],
                                                     [(self.helper_by_part[part], 1.0)]))
            output.write('end\n')
        return self.qc_lines(path.name)

    def qc_lines(self, filename: str) -> list[str]:
        """Emit per-body mass and material settings and source-frame joint adaptations."""
        lines = []
        for body in self.body_reports:
            lines.append(f'$jointsurfaceprop {_qc_name(body["helper"])} {_qc_name(body["target_surfaceprop"])}')
        lines.extend([f'$collisionjoints {_qc_name(filename)}', '{', '    $concaveperjoint',
                      f'    $maxconvexpieces {convex_piece_budget(len(self.shapes))}',
                      f'    $rootbone {_qc_name(self.root_bone)}',
                      f'    $mass {sum(body["target_mass_kg"] for body in self.body_reports):.9f}',
                      '    $inertia 1', '    $damping 0', '    $rotdamping 0',
                      '    $weldposition 0', '    $weldnormal 0.999'])
        for body in self.body_reports:
            helper = _qc_name(body['helper'])
            bias = body['target_mass_kg'] / body['export_volume_in3']
            lines.extend([f'    $jointmassbias {helper} {bias:.12g}',
                          f'    $jointinertia {helper} {body["target_inertia_scale"]:.9g}',
                          f'    $jointdamping {helper} {body["target_linear_damping"]:.9g}',
                          f'    $jointrotdamping {helper} {body["target_angular_damping"]:.9g}'])
        for joint in self.joint_reports:
            for axis, minimum, maximum in joint['target_axes']:
                friction = joint['target_friction'] / self.friction_scale
                lines.append(f'    $jointconstrain {_qc_name(joint["child_helper"])} '
                             f'{axis} limit {minimum:.9f} {maximum:.9f} {friction:.9f}')
        pairs = self.report['collision_pairs']
        if not pairs:
            lines.append('    $noselfcollisions')
        else:
            for first, second in pairs:
                lines.append(f'    $jointcollide {_qc_name(first)} {_qc_name(second)}')
        lines.append('}')
        return lines


def _read_document(value: dict | Path | None) -> dict:
    """Read one source physics aggregate from its lossless exported sidecar."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return copy.deepcopy(value)
    documents = parse_kv3_documents(Path(value).read_text(encoding='utf-8-sig'))
    if len(documents) != 1:
        raise RagdollPhysicsError('Expected exactly one PHYS aggregate')
    return documents[0]


def _part_hierarchy(document: dict) -> tuple[dict[int, dict], int, list[int]]:
    """Require a single rooted articulated tree without discarding source constraints."""
    count = len(document['m_parts'])
    incoming = {}
    if document.get('m_constraints2'):
        raise RagdollPhysicsError('Alternate Source 2 constraints need explicit adaptation')
    for joint in document.get('m_joints', []):
        parent, child = int(joint['m_nBody1']), int(joint['m_nBody2'])
        if not 0 <= parent < count or not 0 <= child < count or child == parent or child in incoming:
            raise RagdollPhysicsError('PHYS joints are not a valid body tree')
        if int(joint['m_nType']) not in (3, 4, 6):
            raise RagdollPhysicsError(f'Unsupported PHYS joint type {joint["m_nType"]}')
        incoming[child] = joint
    roots = set(range(count)) - incoming.keys()
    if len(roots) != 1:
        raise RagdollPhysicsError('Ragdoll needs one physical root and a connected joint tree')
    root = next(iter(roots))
    order = [root]
    while len(order) < count:
        pending = [part for part in range(count) if part not in order
                   and int(incoming[part]['m_nBody1']) in order]
        if not pending:
            raise RagdollPhysicsError('Cyclic source physics hierarchy')
        order.extend(pending)
    return incoming, root, order


def _compiler_gimbal_loss(rotation: np.ndarray) -> float:
    """Measure Source MatrixAngles' roll-zero canonicalization below its 0.001 horizontal threshold."""
    horizontal = math.hypot(float(rotation[0, 0]), float(rotation[1, 0]))
    if horizontal > 0.001:
        return 0.0
    pitch = math.atan2(float(-rotation[2, 0]), horizontal)
    yaw = math.atan2(float(-rotation[0, 1]), float(rotation[1, 1]))
    cp, sp, cy, sy = math.cos(pitch), math.sin(pitch), math.cos(yaw), math.sin(yaw)
    canonical = np.array([[cp * cy, -sy, sp * cy], [cp * sy, cy, sp * sy], [-sp, 0, cp]])
    return float(np.max(np.abs(canonical - rotation)))


def _combined_skin(skin, source_worlds, part_sources, bind, incoming, root, order):
    """Insert physical helpers without changing the world bind of any original bone."""
    names = set(name.casefold() for name in skin.joint_names)
    helper_names = {}
    offsets = {}
    helper_worlds = {}
    for part in order:
        name = f'__s2_phys_{part:02d}'
        while name.casefold() in names:
            name += '_'
        names.add(name.casefold())
        helper_names[part] = name
        frame = _frame(incoming[part]['m_Frame2']) if part in incoming else np.eye(4)
        helper_worlds[part] = bind[part] @ frame
        offsets[part] = np.linalg.inv(source_worlds[part_sources[part]]) @ helper_worlds[part]
    originals_by_source = {source: part for part, source in enumerate(part_sources)}
    source_parents = {
        index: (('helper', originals_by_source[index]) if index in originals_by_source else
                ('source', joint.parent_joint_idx) if joint.parent_joint_idx is not None else
                ('helper', root)) for index, joint in enumerate(skin.joints)
    }
    bind_worlds = {('source', index): world for index, world in enumerate(source_worlds)}
    bind_worlds.update({('helper', part): world for part, world in helper_worlds.items()})
    axis_names, axis_worlds = {}, {}
    axis_turn = np.array([[1, 0, 0, 0], [0, 0, -1, 0], [0, 1, 0, 0], [0, 0, 0, 1]], dtype=float)
    for index, parent_key in source_parents.items():
        local = np.linalg.inv(bind_worlds[parent_key]) @ source_worlds[index]
        if _compiler_gimbal_loss(local[:3, :3]) <= 0.000001:
            continue
        name = f'__s2_bind_{index:03d}'
        while name.casefold() in names:
            name += '_'
        names.add(name.casefold())
        axis_names[index] = name
        axis_worlds[index] = bind_worlds[parent_key] @ axis_turn
    if len(skin.joints) + len(helper_names) + len(axis_names) > MAX_BONES:
        raise RagdollPhysicsError('Physical and bind-axis helpers exceed the 256-bone limit')
    pending = ([('helper', part) for part in order] + [('axis', index) for index in axis_names]
               + [('source', index) for index in range(len(skin.joints))])
    new_indices = {}
    worlds = []
    joints = []
    while pending:
        remaining = []
        for kind, index in pending:
            if kind == 'helper':
                parent_key = ('helper', int(incoming[index]['m_nBody1'])) if index in incoming else None
                name, world, node = helper_names[index], helper_worlds[index], -1
            elif kind == 'axis':
                parent_key = source_parents[index]
                name, world, node = axis_names[index], axis_worlds[index], -1
            else:
                original = skin.joints[index]
                parent_key = ('axis', index) if index in axis_names else source_parents[index]
                name, world, node = original.name, source_worlds[index], original.gltf_node_idx
            if parent_key is not None and parent_key not in new_indices:
                remaining.append((kind, index))
                continue
            parent = new_indices.get(parent_key)
            local = world if parent is None else np.linalg.inv(worlds[parent]) @ world
            new_indices[(kind, index)] = len(joints)
            joints.append(GltfJoint(name, node, parent, np.linalg.inv(world), local[:3, 3].copy(),
                                    _m2q(local[:3, :3]), np.ones(3)))
            worlds.append(world)
        if len(remaining) == len(pending):
            raise RagdollPhysicsError('Cannot construct an acyclic physical helper skeleton')
        pending = remaining
    combined = GltfSkin(joints, new_indices[('helper', root)], skin.skin_idx)
    rigid_helpers = {new_indices[('axis', index)]: (new_indices[source_parents[index]], axis_turn.copy())
                     for index in axis_names}
    return (combined, {index: new_indices[('source', index)] for index in range(len(skin.joints))},
            {part: new_indices[('helper', part)] for part in order}, offsets, rigid_helpers)


def _joint_report(joint: dict, bind, helpers, bodies) -> dict:
    """Express source limits about exact helper pivots and retain motor adaptation evidence."""
    parent, child = int(joint['m_nBody1']), int(joint['m_nBody2'])
    first, second = bind[parent] @ _frame(joint['m_Frame1']), bind[child] @ _frame(joint['m_Frame2'])
    difference = first[:3, :3].T @ second[:3, :3]
    bind_twist = math.atan2(difference[1, 0], difference[0, 0])
    bind_swing = math.atan2(float(np.linalg.norm(difference[:2, 2])), float(difference[2, 2]))
    kind = int(joint['m_nType'])
    twist = joint.get('m_TwistLimit') or {}
    swing = joint.get('m_SwingLimit') or {}
    if kind == 6:
        intervals = [(0.0, 0.0)] * 3
    else:
        if joint.get('m_bEnableTwistLimit'):
            twist_interval = (math.degrees(float(twist['m_flMin']) - bind_twist),
                              math.degrees(float(twist['m_flMax']) - bind_twist))
        else:
            twist_interval = (-360.0, 360.0)
        cone = math.degrees(float(swing.get('m_flMax', 0))) if joint.get('m_bEnableSwingLimit') else 180.0
        swing_intervals = [(0.0, 0.0)] * 2 if kind == 3 else [(-cone, cone)] * 2
        intervals = [*swing_intervals, twist_interval]
    axes = []
    for axis, (minimum, maximum) in zip('xyz', intervals):
        if axis in 'xy':
            minimum, maximum = -maximum, -minimum
        axes.append((axis, minimum, maximum))
    angular_motor = bool(joint.get('m_bEnableAngularMotor'))
    torque = float(joint.get('m_flMaxTorque', 0)) if angular_motor else 0.0
    friction = torque * 0.0254 ** 2 / bodies[child]['target_mass_kg']
    if joint.get('_generated'):
        friction = 0.2
    return {'source_type': kind, 'source_parent': parent, 'source_child': child,
            'parent_helper': helpers[parent], 'child_helper': helpers[child],
            'source_frames': [joint['m_Frame1'], joint['m_Frame2']],
            'bind_twist_deg': math.degrees(bind_twist), 'bind_swing_deg': math.degrees(bind_swing),
            'source_pivot_mismatch_in': float(np.linalg.norm(first[:3, 3] - second[:3, 3])),
            'target_axes': axes, 'target_friction': friction,
            'source_angular_motor_enabled': angular_motor,
            'source_angular_target_velocity': joint.get('m_vAngularTargetVelocity', [0, 0, 0]),
            'source_max_torque': torque,
            'angular_motor_adaptation': 'zero_velocity_torque_surrogate_inch2_to_metre2_per_child_mass',
            'source_linear_motor_enabled': bool(joint.get('m_bEnableLinearMotor')),
            'source_max_force': joint.get('m_flMaxForce', 0),
            'source_joint': copy.deepcopy(joint),
            'limit_adaptation': 'coupled_cone_to_independent_axis_envelope' if kind == 4 else 'bind_referenced',
            'generated': bool(joint.get('_generated'))}


def prepare_ragdoll_physics(
    phys: dict | Path | None, skin: GltfSkin, coord: CoordinateMode, *,
    model_data: dict | None = None, surface_properties: dict[int, dict] | None = None,
    friction_scale: float = 1.0,
) -> RagdollPhysicsResult:
    """Prepare an authored ragdoll or clearly identified AO-proxy humanoid fallback.

    The returned skin and remapped animation frames are already in Source-inch
    model space and must be written with an identity CoordinateMode. Original
    rendering bones retain their names and world bind transforms. The source
    document is retained verbatim alongside evaluated values and adaptations.
    """
    if not math.isfinite(friction_scale) or friction_scale <= 0:
        raise RagdollPhysicsError('Compiler friction calibration must be positive')
    if len(set(name.casefold() for name in skin.joint_names)) != len(skin.joints):
        raise RagdollPhysicsError('Ragdoll source bone names must be unique')
    for name in skin.joint_names:
        _qc_name(name)
    source_worlds = _source_world(skin, derive_bind_pose_local(skin), coord)
    document = _read_document(phys)
    generated = not bool(document.get('m_parts'))
    if generated:
        document = _ao_fallback(model_data or {}, skin, source_worlds)
    parts = document['m_parts']
    if not 1 <= len(parts) <= MAX_BODIES:
        raise RagdollPhysicsError('Garry\'s Mod supports between 1 and 32 ragdoll bodies')
    if len(skin.joints) + len(parts) > MAX_BONES:
        raise RagdollPhysicsError('Source bones plus physical helpers exceed Garry\'s Mod\'s 256 bones')
    incoming, root, order = _part_hierarchy(document)
    lookup = {joint.name: index for index, joint in enumerate(skin.joints)}
    names, parents, poses = (document.get(key) or [] for key in ('m_boneNames', 'm_boneParents', 'm_bindPose'))
    if len(parents) != len(parts) or len(poses) != len(parts):
        raise RagdollPhysicsError('PHYS part, bind pose and bone indirection counts differ')
    try:
        part_names = [names[int(index)] for index in parents]
        part_sources = [lookup[name] for name in part_names]
    except (KeyError, IndexError, ValueError) as error:
        raise RagdollPhysicsError('PHYS part refers to an absent rendering bone') from error
    if len(set(part_sources)) != len(parts):
        raise RagdollPhysicsError('Multiple physics bodies bound to the same source bone')
    bind = [_rigid(pose, f'part {part}') for part, pose in enumerate(poses)]
    for part, index in enumerate(part_sources):
        if not np.allclose(bind[part], source_worlds[index], atol=0.005, rtol=0):
            raise RagdollPhysicsError(f'PHYS/render bind disagreement on {part_names[part]}')
    combined, index_map, helper_map, offsets, rigid_helpers = _combined_skin(
        skin, source_worlds, part_sources, bind, incoming, root, order)
    hashes = document.get('m_surfacePropertyHashes') or []
    properties = surface_properties or {}
    shapes = []
    bodies = []
    warnings = []
    for part_index, part in enumerate(parts):
        shape = part.get('m_rnShape') or {}
        if shape.get('m_meshes'):
            raise RagdollPhysicsError('Dynamic triangle-soup physics requires an explicit convex adaptation')
        body_shapes = []
        for field_name, kind in [('m_spheres', 'sphere'), ('m_capsules', 'capsule'), ('m_hulls', 'hull')]:
            for data in shape.get(field_name, []):
                mesh, report = _prepare_shape(kind, data)
                surface_index = report['surface_property_index']
                if not 0 <= surface_index < len(hashes):
                    raise RagdollPhysicsError('Shape has an invalid source surface property index')
                surface = _surface(int(hashes[surface_index]), properties)
                density = float(surface['physics']['density'])
                if not math.isfinite(density) or density <= 0:
                    raise RagdollPhysicsError('Source surface density must be finite and positive')
                report.update(part=part_index, source_surface=surface)
                positions = mesh.vertices @ bind[part_index][:3, :3].T + bind[part_index][:3, 3]
                model_mesh = trimesh.Trimesh(vertices=positions, faces=mesh.faces, process=False)
                report['bounds_model_in'] = model_mesh.bounds.tolist()
                shapes.append((part_index, model_mesh, report))
                body_shapes.append(report)
        if not body_shapes:
            raise RagdollPhysicsError(f'PHYS body {part_index} has no supported shapes')
        source_mass = float(part.get('m_flMass', 0))
        parameters = [source_mass, *(float(part.get(key, default)) for key, default in
                                     [('m_flInertiaScale', 1), ('m_flLinearDamping', 0),
                                      ('m_flAngularDamping', 0)])]
        if not all(math.isfinite(value) and value >= 0 for value in parameters):
            raise RagdollPhysicsError('Invalid source body mass, inertia or damping')
        automatic = sum(item['source_volume_in3'] * float(item['source_surface']['physics']['density'])
                        * CUBIC_INCH_IN_CUBIC_METRES for item in body_shapes)
        wanted_mass = source_mass if source_mass > 0 else automatic
        target_mass = max(1.0, wanted_mass)
        dominant = max(body_shapes, key=lambda item: item['source_volume_in3'])['source_surface']
        bodies.append({'part': part_index, 'source_bone': part_names[part_index],
                       'helper': combined.joints[helper_map[part_index]].name,
                       'source_mass_kg': source_mass, 'evaluated_source_mass_kg': wanted_mass,
                       'mass_mode': 'explicit' if source_mass > 0 else 'source_density',
                       'target_mass_kg': target_mass, 'mass_floor_delta_kg': target_mass - wanted_mass,
                       'export_volume_in3': sum(item['export_volume_in3'] for item in body_shapes),
                       'source_surfaceprop': dominant['surfacePropertyName'],
                       'target_surfaceprop': 'flesh' if 'npc_' in dominant['surfacePropertyName'] else 'default',
                       'mixed_shape_surfaces': len({item['surface_property_index'] for item in body_shapes}) > 1,
                       'target_inertia_scale': float(part.get('m_flInertiaScale', 1)),
                       'target_linear_damping': float(part.get('m_flLinearDamping', 0)),
                       'target_angular_damping': float(part.get('m_flAngularDamping', 0)),
                       'mass_center_override_unsupported': bool(part.get('m_bOverrideMassCenter')),
                       'source_part': copy.deepcopy(part), 'shape_count': len(body_shapes)})
    helpers = {part: combined.joints[index].name for part, index in helper_map.items()}
    joints = [_joint_report(joint, bind, helpers, bodies) for joint in document.get('m_joints', [])]
    disabled = {tuple(sorted((int(joint['m_nBody1']), int(joint['m_nBody2']))))
                for joint in document.get('m_joints', []) if not joint.get('m_bEnableCollision', False)}
    pairs = [[helpers[first], helpers[second]] for first in range(len(parts))
             for second in range(first + 1, len(parts)) if (first, second) not in disabled]
    if generated:
        warnings.append('No source PHYS collision exists: generated humanoid physics uses authored AO proxies and adapted anatomical limits.')
    if any(item['source_type'] == 4 for item in joints):
        warnings.append('Source coupled swing cones are represented by independent Source 1 angular-limit envelopes.')
    if any(item['source_linear_motor_enabled'] for item in joints):
        warnings.append('Source linear joint motors have no ordinary Source 1 ragdoll QC equivalent.')
    if any(item['source_angular_motor_enabled'] for item in joints):
        warnings.append('Angular motors use a unit-converted zero-velocity friction surrogate; solver equivalence is unverified.')
    if any(item['thickened'] for _, _, item in shapes):
        warnings.append(f'Thin authored collision pieces are individually thickened to {THICKEN_TARGET_IN:g} inches; per-shape growth is recorded.')
    if any(body['mixed_shape_surfaces'] for body in bodies):
        warnings.append('Source bodies with mixed shape surface properties use one dominant Source 1 body surface property.')
    if any(body['mass_floor_delta_kg'] > 0 for body in bodies):
        warnings.append('Source 1 enforces a 1 kg minimum mass per simulated body; mass increases are recorded.')
    if any(body['mass_center_override_unsupported'] for body in bodies):
        warnings.append('Authored per-body mass center overrides have no ordinary jointed QC equivalent and are retained as unsupported metadata.')
    if any(item['source_surface'].get('unresolved') for _, _, item in shapes):
        warnings.append('An unresolved source surface uses the explicit default density of 2000 kg/m3; automatic mass fidelity is unverified for that surface.')
    if document.get('m_pFeModel'):
        warnings.append('Source soft-body simulation is not part of rigid ragdoll physics; its authored data is retained for appearance conversion.')
    warnings.append('Source surface density determines automatic masses; stock Source 1 surface response and Rubikon solver behavior differ.')
    report = {'mode': 'generated_from_ao_proxy' if generated else 'authored_helper_ragdoll',
              'source_body_count': len(parts), 'helper_bone_count': len(parts),
              'bind_axis_helper_count': len(rigid_helpers),
              'bind_axis_helpers': [dict(name=combined.joints[index].name,
                                         parent=combined.joints[parent].name,
                                         local_transform=local.tolist(),
                                         purpose='Avoid Source 1 near-gimbal bind canonicalization without moving source bones')
                                    for index, (parent, local) in rigid_helpers.items()],
              'original_bone_count': len(skin.joints), 'combined_bone_count': len(combined.joints),
              'root_bone': helpers[root], 'source_root_part': root,
              'shape_count': len(shapes), 'source_joint_count': len(joints),
              'bodies': bodies, 'joints': joints, 'shapes': [report for _, _, report in shapes],
              'collision_pairs': pairs, 'source_collision_attributes': document.get('m_collisionAttributes', []),
              'unsupported_collision_attribute_filters': any(
                  item.get(key) for item in document.get('m_collisionAttributes', [])
                  for key in ('m_InteractAs', 'm_InteractWith', 'm_InteractExclude')),
              'friction_compile_scale': friction_scale,
              'maximum_tessellation_error_in': MAX_SHAPE_ERROR_IN,
              'maximum_tessellation_volume_error': MAX_SHAPE_VOLUME_ERROR,
              'source_total_mass_kg': sum(body['evaluated_source_mass_kg'] for body in bodies),
              'target_total_mass_kg': sum(body['target_mass_kg'] for body in bodies),
              'source_softbody_present': bool(document.get('m_pFeModel')),
              'warnings': warnings}
    return RagdollPhysicsResult(combined, index_map, helpers[root], document, report, warnings,
                                helper_map, skin, coord, offsets, part_sources, shapes, bodies,
                                joints, friction_scale, rigid_helpers=rigid_helpers)


def adapt_physics_to_playermodel(
    native_result: RagdollPhysicsResult, target_skin: GltfSkin, bone_name_map: dict[str, str], *,
    model_rotation: np.ndarray | None = None, model_translation: np.ndarray | None = None,
) -> RagdollPhysicsResult:
    """Bind unchanged source collision shapes to a standard humanoid animation hierarchy.

    The target rig is in Source inches. Its bone origins and axes remain those
    required by the playermodel animation and IK pipeline. Projected constraint
    axes and displaced source anchors are measured per joint, while the native
    companion retains its dedicated physical helpers and source pivots.
    """
    rotation = np.eye(3) if model_rotation is None else np.asarray(model_rotation, dtype=np.float64)
    translation = np.zeros(3) if model_translation is None else np.asarray(model_translation, dtype=np.float64)
    if (rotation.shape != (3, 3) or not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-6)
            or np.linalg.det(rotation) < 0.999999):
        raise RagdollPhysicsError('Playermodel physics requires a rigid model-space transform')
    lookup = {joint.name: index for index, joint in enumerate(target_skin.joints)}
    source_worlds = _source_world(target_skin, derive_bind_pose_local(target_skin), CoordinateMode())
    mapped = {}
    bodies = copy.deepcopy(native_result.body_reports)
    name_map = {}
    for body in bodies:
        name = bone_name_map.get(body['source_bone'])
        if name not in lookup or lookup[name] in mapped.values():
            raise RagdollPhysicsError(f'Playermodel lacks a unique physics bone for {body["source_bone"]}')
        name_map[body['helper']] = name
        body['helper'] = name
        mapped[body['part']] = lookup[name]
    shapes = []
    for part, mesh, report in native_result.shapes:
        positions = mesh.vertices @ rotation.T + translation
        transformed = trimesh.Trimesh(vertices=positions, faces=mesh.faces, process=False)
        item = copy.deepcopy(report)
        item['bounds_model_in'] = transformed.bounds.tolist()
        shapes.append((part, transformed, item))
    joints = copy.deepcopy(native_result.joint_reports)
    poses = [_rigid(pose, f'part {index}') for index, pose in enumerate(native_result.source_document['m_bindPose'])]
    for item in joints:
        child = item['source_child']
        original_frame = poses[child] @ _frame(item['source_frames'][1])
        wanted_rotation = rotation @ original_frame[:3, :3]
        wanted_anchor = rotation @ original_frame[:3, 3] + translation
        target = source_worlds[mapped[child]]
        basis = target[:3, :3].T @ wanted_rotation
        available = set(range(3))
        assignments = {}
        for source_axis in (2, 0, 1):
            axis = max(available, key=lambda index: abs(float(basis[index, source_axis])))
            available.remove(axis)
            assignments[source_axis] = axis
        adapted = {}
        for source_axis, (_, low, high) in enumerate(item['target_axes']):
            if source_axis in (0, 1):
                low, high = -high, -low
            axis = assignments[source_axis]
            if basis[axis, source_axis] < 0:
                low, high = -high, -low
            if axis in (0, 1):
                low, high = -high, -low
            adapted[axis] = ('xyz'[axis], low, high)
        item['target_axes'] = [adapted[axis] for axis in range(3)]
        item['parent_helper'] = name_map[item['parent_helper']]
        item['child_helper'] = name_map[item['child_helper']]
        item['playermodel_anchor_error_in'] = float(np.linalg.norm(wanted_anchor - target[:3, 3]))
        item['playermodel_primary_axis_projection_error_deg'] = math.degrees(math.acos(
            min(1.0, abs(float(basis[assignments[2], 2])))))
        item['playermodel_source_joint_frame_in_target'] = basis.tolist()
        item['limit_adaptation'] += '_projected_to_playermodel_bone_axes'
    warnings = [*native_result.warnings,
                'Playermodel physics keeps canonical animation and IK chains; source joint pivots and axes are adapted and measured per joint.']
    report = copy.deepcopy(native_result.report)
    report.update(mode='playermodel_adapted_source_physics', helper_bone_count=0,
                  combined_bone_count=len(target_skin.joints), bodies=bodies, joints=joints,
                  shapes=[item for _, _, item in shapes], root_bone=name_map[native_result.root_bone],
                  collision_pairs=[[name_map[first], name_map[second]]
                                   for first, second in native_result.report['collision_pairs']],
                  warnings=warnings)
    return RagdollPhysicsResult(
        target_skin, {}, report['root_bone'], native_result.source_document, report, warnings,
        mapped, native_result.original_skin, native_result.original_coord, {},
        native_result.part_sources, shapes, bodies, joints, native_result.friction_scale)
