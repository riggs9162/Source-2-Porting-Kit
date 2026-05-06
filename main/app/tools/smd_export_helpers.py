"""Shared helpers for SMD exporters.

Both [smd_export.py](smd_export.py) (static prop) and
[smd_animation_export.py](smd_animation_export.py) (skinned + animation)
need the same UV resolution, per-face material resolution, and material
fallback logic. This module hosts them.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np
import trimesh


def resolve_uvs(mesh: trimesh.Trimesh, uv_mode: str, force_uv_zero: bool) -> Optional[np.ndarray]:
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


def resolve_face_materials(mesh: trimesh.Trimesh) -> Tuple[Optional[np.ndarray], Optional[List[str]]]:
    if not hasattr(mesh, 'metadata') or not mesh.metadata:
        return (None, None)
    names = mesh.metadata.get('gltf_material_names')
    face_mat = mesh.metadata.get('gltf_face_materials')
    if face_mat is None or names is None:
        return (None, None)
    return (np.asarray(face_mat), list(names))


def pick_material(face_idx: int, face_materials: Optional[np.ndarray],
                  material_names: Optional[List[str]], fallback: str) -> str:
    if face_materials is None or material_names is None:
        return fallback
    if face_idx >= len(face_materials):
        return fallback
    mat_idx = int(face_materials[face_idx])
    if 0 <= mat_idx < len(material_names):
        return material_names[mat_idx]
    return fallback
