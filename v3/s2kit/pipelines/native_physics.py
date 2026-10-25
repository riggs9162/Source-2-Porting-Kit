"""Export bone-bound Source 2 convex hulls for animated Source 1 bone followers.

PHYS bind poses already map local hull vertices into Source-inch model space.
The collision SMD therefore reuses the native reference skeleton without any
glTF axis or unit conversion. No explicit joint constraints are invented for
kinematic source parts. StudioMDL retains zero-limit defaults where it finds a
physical parent; prop_dynamic bone followers provide the intended motion.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import trimesh

from s2kit.pipelines.dmx_mesh import smd_vertex_line
from s2kit.pipelines.model_metadata import parse_kv3_documents
from s2kit.pipelines.physics import (
    MIN_PIECE_THICKNESS_IN, THICKEN_TARGET_IN, _hull_of_points,
    _thinnest_direction, convex_piece_budget, thicken_thin_pieces,
)


class NativePhysicsError(ValueError):
    """The source cannot be represented by this kinematic convex-hull exporter."""


@dataclass
class NativePhysicsResult:
    """Generated collision QC and an audit of source parts and repaired hulls."""

    qc_lines: list[str]
    report: dict[str, Any]
    warnings: list[str]


def _qc_name(value: str) -> str:
    """Validate a name before embedding it in a quoted QC token."""
    if not value or any(char in value for char in ('"', '\\', '\n', '\r')):
        raise NativePhysicsError(f"Unsafe or empty collision name: {value!r}")
    return '"' + value + '"'


def _reference_header(reference_path: Path, bone_names: list[str]) -> tuple[str, dict[int, int]]:
    """Check bone indices and retain the authored hierarchy for collision constraints."""
    text = reference_path.read_text(encoding='utf-8')
    header = text.split('triangles\n', 1)[0]
    match = re.search(r'(?ms)^nodes\n(.*?)^end\n', header)
    if match is None or '\nskeleton\n' not in header:
        raise NativePhysicsError('Reference SMD lacks its native skeleton header')
    entries = re.findall(r'^\s*(\d+)\s+"([^"\n]+)"\s+(-?\d+)\s*$', match[1], re.M)
    nodes = {int(index): name for index, name, parent in entries}
    parents = {int(index): int(parent) for index, name, parent in entries}
    if len(set(name.casefold() for name in bone_names)) != len(bone_names):
        raise NativePhysicsError('Collision bone names must be unique without regard to case')
    for index, name in enumerate(bone_names):
        if nodes.get(index) != name:
            raise NativePhysicsError(f'Reference bone {index} does not match {name!r}')
    return header, parents


def _physical_ancestor(joint: int, parents: dict[int, int], physical_bones: set[int]) -> int | None:
    """Find a selected collision ancestor without reparenting independent skeleton roots."""
    visited = {joint}
    parent = parents[joint]
    while parent >= 0:
        if parent in visited or parent not in parents:
            raise NativePhysicsError('Reference skeleton has a cyclic or missing parent')
        if parent in physical_bones:
            return parent
        visited.add(parent)
        parent = parents[parent]
    return None


def _source_parts(document: dict[str, Any], bone_names: list[str], root_bone: str, origin_bone: str = ""):
    """Resolve the PHYS part-to-bone indirection and reject unsupported shapes.

    A part named with an empty bone follows the model origin and binds to `origin_bone`.
    """
    if document.get('m_joints') or document.get('m_constraints2'):
        raise NativePhysicsError('Jointed Source 2 physics needs explicit joint-limit conversion')
    if document.get('m_pFeModel'):
        raise NativePhysicsError('Soft-body Source 2 physics is unsupported')
    parts = document.get('m_parts') or []
    names = document.get('m_boneNames') or []
    parents = document.get('m_boneParents') or []
    poses = document.get('m_bindPose') or []
    if not 1 <= len(parts) <= 32:
        raise NativePhysicsError('Kinematic physics requires between 1 and 32 named parts')
    if len(parents) != len(parts) or len(poses) != len(parts):
        raise NativePhysicsError('PHYS part, bone-index and bind-pose counts differ')
    lookup = {name: index for index, name in enumerate(bone_names)}
    resolved = []
    used = set()
    for index, part in enumerate(parts):
        bone_index = int(parents[index])
        if bone_index < 0 or bone_index >= len(names):
            raise NativePhysicsError(f'PHYS part {index} has an invalid bone-name index')
        bone = str(names[bone_index]) or origin_bone
        _qc_name(bone)
        if bone not in lookup or bone.casefold() in used:
            raise NativePhysicsError(f'PHYS bone {bone!r} is missing from the skeleton or duplicated')
        used.add(bone.casefold())
        transform = np.asarray(poses[index], dtype=np.float64)
        if transform.size != 12 or not np.isfinite(transform).all():
            raise NativePhysicsError(f'PHYS bone {bone!r} has an invalid 3x4 bind pose')
        transform = transform.reshape(3, 4)
        rotation = transform[:, :3]
        if (not np.allclose(rotation.T @ rotation, np.eye(3), atol=2e-5)
                or not math.isclose(float(np.linalg.det(rotation)), 1.0, abs_tol=2e-5)):
            raise NativePhysicsError(f'PHYS bone {bone!r} bind pose contains scaling or reflection')
        shape = part.get('m_rnShape') or {}
        for kind in ('m_spheres', 'm_capsules', 'm_meshes'):
            if shape.get(kind):
                raise NativePhysicsError(f'PHYS bone {bone!r} contains unsupported {kind}')
        hulls = shape.get('m_hulls') or []
        if not hulls:
            raise NativePhysicsError(f'PHYS bone {bone!r} has no convex hull geometry')
        resolved.append((index, bone, lookup[bone], transform, hulls))
    if root_bone.casefold() not in used:
        raise NativePhysicsError(f'Physics root {root_bone!r} must have authored collision geometry')
    return resolved


def _collision_normals(count: int) -> np.ndarray:
    """Give each hull a distinct smoothing group without moving its vertices.

    StudioMDL welds collision vertices when positions match and the normal dot
    product exceeds 0.999. A constant unit normal connects every face within a
    hull; separated normals prevent touching source hulls from becoming one.
    These normals describe connectivity only, because collision is not drawn.
    """
    index = np.arange(count, dtype=np.float64)
    z = 1.0 - 2.0 * (index + 0.5) / count
    angle = index * (math.pi * (3.0 - math.sqrt(5.0)))
    radius = np.sqrt(1.0 - z * z)
    normals = np.column_stack((radius * np.cos(angle), radius * np.sin(angle), z))
    for start in range(0, count, 256):
        dots = normals[start:start + 256] @ normals.T
        rows = np.arange(len(dots))
        dots[rows, start + rows] = -1
        if np.any(dots >= 0.998):
            raise NativePhysicsError('Too many authored hulls for isolated collision smoothing groups')
    return normals


def _prepare_hull(data: dict[str, Any], label: str) -> tuple[trimesh.Trimesh, dict[str, Any]]:
    """Triangulate one authored cloud, repair only that hull, and audit volume."""
    shape = data.get('m_Hull') or {}
    vertices = np.asarray(shape.get('m_Vertices'), dtype=np.float64)
    if vertices.ndim != 2 or vertices.shape[1] != 3 or not np.isfinite(vertices).all():
        raise NativePhysicsError(f'{label}: invalid convex hull vertex cloud')
    hull = _hull_of_points(vertices)
    if hull is None or not hull.is_watertight:
        raise NativePhysicsError(f'{label}: convex hull has no closed three-dimensional volume')
    original_volume = float(hull.volume)
    repair = thicken_thin_pieces(hull)
    fixed = repair.mesh
    if repair.dropped or fixed is None or not fixed.is_watertight:
        raise NativePhysicsError(f'{label}: thickness repair lost convex hull geometry')
    minimum_width = _thinnest_direction(fixed)[1]
    if minimum_width <= MIN_PIECE_THICKNESS_IN:
        raise NativePhysicsError(f'{label}: hull is still below the compiler thickness limit')
    authored_volume = shape.get('m_flVolume')
    if authored_volume is not None:
        authored_volume = float(authored_volume)
        if not math.isfinite(authored_volume) or authored_volume <= 0:
            raise NativePhysicsError(f'{label}: invalid authored collision volume')
    report = {
        'source_vertices': len(vertices), 'export_vertices': len(fixed.vertices),
        'triangles': len(fixed.faces), 'authored_volume_in3': authored_volume,
        'source_cloud_volume_in3': original_volume,
        'export_local_volume_in3': float(fixed.volume),
        'minimum_width_before_in': repair.thinnest,
        'minimum_width_after_in': minimum_width,
        'thickened': bool(repair.thickened),
        'collision_attribute_index': data.get('m_nCollisionAttributeIndex'),
        'surface_property_index': data.get('m_nSurfacePropertyIndex'),
    }
    return fixed, report


def write_kinematic_physics(phys_path: Path, smd_path: Path, reference_path: Path,
                            bone_names: list[str], root_bone: str,
                            origin_bone: str = "") -> NativePhysicsResult:
    """Write rigidly weighted compound hulls without inventing source joints.

    Each source part maps through m_boneParents into m_boneNames; bind poses are
    indexed by part. All hulls keep their own connected component. The report
    distinguishes source volume, thickness repairs, and physical ancestry.
    StudioMDL can lose physical ancestors while skipping nonphysical bones,
    so explicit constraints would reject otherwise valid bone-follower parts.
    Its zero-limit defaults apply where a compiled parent survives. This is a
    kinematic prop, not a usable ragdoll. The caller supplies a Source-inch
    native reference and retains its bones. Parts bound to the model origin
    follow `origin_bone`, normally the skeleton root.
    """
    phys_path, smd_path, reference_path = map(Path, (phys_path, smd_path, reference_path))
    header, parents = _reference_header(reference_path, bone_names)
    documents = parse_kv3_documents(phys_path.read_text(encoding='utf-8-sig'))
    if len(documents) != 1:
        raise NativePhysicsError('Expected one Source 2 PHYS aggregate')
    document = documents[0]
    parts = _source_parts(document, bone_names, root_bone, origin_bone)
    physical_bones = {joint for _, _, joint, _, _ in parts}
    ancestors = {joint: _physical_ancestor(joint, parents, physical_bones)
                 for joint in physical_bones}
    independent_bodies = [bone for _, bone, joint, _, _ in parts if ancestors[joint] is None]
    hull_count = sum(len(hulls) for _, _, _, _, hulls in parts)
    normals = _collision_normals(hull_count)
    prepared, reports = [], []
    ordinal = 0
    warnings = ['Source 2 kinematic parts require prop_dynamic bone followers for animated collision; this collision model is not a ragdoll.']
    warnings.append('Collision bodies without an authored physical ancestor have no constraints: '
                    + ', '.join(independent_bodies))
    warnings.append('No explicit joint constraints are emitted; compiler zero-limit defaults apply only where physical parents survive.')
    for part_index, bone, joint, transform, hulls in parts:
        for hull_index, data in enumerate(hulls):
            mesh, report = _prepare_hull(data, f'{bone} hull {hull_index}')
            positions = np.asarray(mesh.vertices) @ transform[:, :3].T + transform[:, 3]
            model_mesh = trimesh.Trimesh(vertices=positions, faces=mesh.faces, process=False)
            report.update(part=part_index, bone=bone, bone_index=joint, hull=hull_index,
                          export_model_volume_in3=float(model_mesh.volume),
                          bounds_model_in=model_mesh.bounds.tolist())
            reports.append(report)
            prepared.append((joint, positions, mesh.faces, normals[ordinal]))
            ordinal += 1
    thickened = sum(report['thickened'] for report in reports)
    if thickened:
        warnings.append(f'{thickened} authored hulls were individually thickened to at least {THICKEN_TARGET_IN:g} inches for VPhysics; dimensions are recorded per hull.')
    for report in reports:
        authored = report['authored_volume_in3']
        if authored and not math.isclose(authored, report['source_cloud_volume_in3'], rel_tol=1e-3, abs_tol=1e-4):
            warnings.append(f"{report['bone']} hull {report['hull']}: vertex-cloud volume differs from authored PHYS volume")
    smd_path.parent.mkdir(parents=True, exist_ok=True)
    with smd_path.open('w', encoding='utf-8', newline='\n') as output:
        output.write(header + 'triangles\n')
        for joint, positions, faces, normal in prepared:
            for face in faces:
                output.write('physics\n')
                for vertex in face:
                    output.write(smd_vertex_line([*positions[vertex], *normal, 0, 0], [(joint, 1.0)]))
        output.write('end\n')
    budget = convex_piece_budget(hull_count)
    qc = [f'$collisionjoints {_qc_name(smd_path.name)}', '{',
          '    $concaveperjoint', f'    $maxconvexpieces {budget}', '    $automass',
          f'    $rootbone {_qc_name(root_bone)}', '    $noselfcollisions',
          '    $weldposition 0', '    $weldnormal 0.999']
    qc.append('}')
    report = {
        'mode': 'kinematic_bone_followers', 'source': str(phys_path),
        'part_count': len(parts), 'hull_count': hull_count,
        'part_bones': [bone for _, bone, _, _, _ in parts],
        'root_bone': root_bone, 'source_joint_count': 0,
        'fallback_constraints': 'compiler_zero_limits_where_physical_parent_survives',
        'independent_bodies': independent_bodies, 'constrained_bodies': [],
        'source_parented_bodies': [bone for _, bone, joint, _, _ in parts if ancestors[joint] is not None],
        'thickened_hulls': thickened,
        'max_convex_pieces': budget, 'triangles': sum(item['triangles'] for item in reports),
        'source_cloud_volume_in3': sum(item['source_cloud_volume_in3'] for item in reports),
        'export_volume_in3': sum(item['export_model_volume_in3'] for item in reports),
        'surface_property_hashes': document.get('m_surfacePropertyHashes', []),
        'hulls': reports,
    }
    return NativePhysicsResult(qc, report, warnings)
