"""SMD export adapter built on srctools.smd.

Input contract: mesh data has already been processed by MeshProcessor
(scaled, axis-converted to Source's coordinate system, sanitized).
Per-face materials are read from mesh.metadata['gltf_face_materials']
+ mesh.metadata['gltf_material_names']. This module performs no
further coordinate, axis, or UV conversion.

Future opportunity: DMX export via srctools.dmx (out of scope here).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import trimesh

from srctools.math import Vec
from srctools.smd import Mesh, Triangle, Vertex


def _resolve_uvs(mesh: trimesh.Trimesh, uv_mode: str, force_uv_zero: bool) -> Optional[np.ndarray]:
    if force_uv_zero:
        return None
    try:
        raw_uvs = mesh.visual.uv
    except Exception:
        return None
    if raw_uvs is None or len(raw_uvs) != len(mesh.vertices):
        return None

    if uv_mode == 'wrap':
        return np.mod(raw_uvs, 1.0)
    if uv_mode == 'clamp':
        return np.clip(raw_uvs, 0.0, 1.0)
    if uv_mode == 'normalize':
        uv_min = np.min(raw_uvs, axis=0)
        uv_range = np.max(raw_uvs, axis=0) - uv_min
        if uv_range[0] > 1e-6 and uv_range[1] > 1e-6:
            return (raw_uvs - uv_min) / uv_range
        return raw_uvs
    return raw_uvs  # 'preserve'


def _resolve_face_materials(mesh: trimesh.Trimesh) -> Tuple[Optional[np.ndarray], Optional[list]]:
    if not hasattr(mesh, 'metadata') or not mesh.metadata:
        return (None, None)
    names = mesh.metadata.get('gltf_material_names')
    face_mat = mesh.metadata.get('gltf_face_materials')
    if face_mat is None or names is None:
        return (None, None)
    return (np.asarray(face_mat), list(names))


def _pick_material(face_idx: int, face_materials: Optional[np.ndarray],
                   material_names: Optional[list], fallback: str) -> str:
    if face_materials is None or material_names is None:
        return fallback
    if face_idx >= len(face_materials):
        return fallback
    mat_idx = int(face_materials[face_idx])
    if 0 <= mat_idx < len(material_names):
        return material_names[mat_idx]
    return fallback


class SmdExporter:
    """Static-prop SMD writer (single root bone) backed by srctools.smd."""

    @staticmethod
    def write_static(
        mesh: trimesh.Trimesh,
        out_path: Path,
        material_name: str,
        flip_v: bool = True,
        force_uv_zero: bool = False,
        override_normals: Optional[np.ndarray] = None,
        uv_mode: str = 'preserve',
    ) -> Tuple[bool, str]:
        """Write static SMD with single root bone. Drop-in for the deleted SmdWriter.

        Args:
            uv_mode: 'preserve' | 'wrap' | 'clamp' | 'normalize'.

        Returns:
            (success, warning_message). warning_message empty on success.
        """
        if mesh is None or not hasattr(mesh, 'faces') or len(mesh.faces) == 0:
            return (False, "Mesh has no faces")

        vertices = mesh.vertices
        faces = mesh.faces

        try:
            if len(vertices) == 0:
                return (False, "Mesh has no vertices")
            max_index = len(vertices) - 1
            valid_mask = np.all((faces >= 0) & (faces <= max_index), axis=1)
            face_index_map = np.nonzero(valid_mask)[0]
            faces = faces[valid_mask]
            if len(faces) == 0:
                return (False, "No valid faces after index validation")
        except Exception:
            return (False, "Face validation failed")

        if override_normals is not None:
            normals = override_normals
        else:
            try:
                normals = mesh.vertex_normals
                if len(normals) != len(vertices):
                    normals = None
            except Exception:
                normals = None

        finite_verts = np.isfinite(vertices).all(axis=1)
        if normals is not None:
            finite_normals = np.isfinite(normals).all(axis=1)
            finite_faces = np.all(finite_verts[faces], axis=1) & np.all(finite_normals[faces], axis=1)
        else:
            finite_faces = np.all(finite_verts[faces], axis=1)

        faces = faces[finite_faces]
        face_index_map = face_index_map[finite_faces]
        if len(faces) == 0:
            return (False, "No valid faces after filtering")

        uvs = _resolve_uvs(mesh, uv_mode, force_uv_zero)
        face_materials, material_names = _resolve_face_materials(mesh)

        smd = Mesh.blank("root")
        root_bone = smd.root_bone()
        links = [(root_bone, 1.0)]

        default_normal = (0.0, 0.0, 1.0)

        for local_idx, face in enumerate(faces):
            # Map back to the pre-filter face index so face_materials lookups stay aligned.
            original_face_idx = int(face_index_map[local_idx])
            mat = _pick_material(original_face_idx, face_materials, material_names, material_name)

            verts = []
            for vidx in face:
                v = vertices[vidx]
                if normals is not None:
                    n = normals[vidx]
                else:
                    n = default_normal

                if uvs is not None:
                    uv = uvs[vidx]
                    u = float(uv[0])
                    tv = float(uv[1])
                else:
                    u = 0.0
                    tv = 0.0
                if flip_v:
                    tv = 1.0 - tv

                verts.append(Vertex(
                    Vec(float(v[0]), float(v[1]), float(v[2])),
                    Vec(float(n[0]), float(n[1]), float(n[2])),
                    u, tv,
                    list(links),
                ))

            smd.triangles.append(Triangle(mat, verts[0], verts[1], verts[2]))

        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "wb") as f:
                smd.export(f)
        except Exception as e:
            return (False, f"SMD write failed: {e}")

        return (True, "")
