"""
GLTF Batch SMD Tool - Convert Source 2 glTF/GLB to Source 1 SMD/QC

Complete refactored batch converter with:
- Clean architecture: ModelSetScanner, PreviewModel, GltfMeshLoader, MeshProcessor, SmdWriter, QcWriter, BatchRunner
- Live preview system showing resolved paths, mass, surfaceprop, overwrite status per model
- Advanced options: flip V, preserve folder structure, auto-rescan, dry run
- Intelligent surfaceprop detection based on file name/material heuristics
- Comprehensive mass calculation: auto (volume-based) with modifier or static per-model
- Replace existing outputs control with preview of what will be overwritten/skipped
"""

from __future__ import annotations

import os
import re
import time
import json
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple, Dict

import numpy as np
import trimesh
from trimesh.resolvers import FilePathResolver

try:
    import pymeshlab
    HAS_PYMESHLAB = True
except ImportError:
    HAS_PYMESHLAB = False

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFileDialog,
    QLineEdit, QGroupBox, QDoubleSpinBox, QCheckBox, QRadioButton,
    QProgressBar, QFormLayout, QWidget, QTableWidget, QTableWidgetItem,
    QHeaderView, QScrollArea, QSplitter, QTextEdit, QButtonGroup, QComboBox,
    QGridLayout, QSizePolicy, QAbstractItemView,
)
from PySide6.QtCore import Qt, QThread, Signal, QEvent
from PySide6.QtGui import QColor

from .base_tool import BaseTool
from .smd_export import SmdExporter
from .smd_animation_export import (
    CoordinateMode, SmdAnimationExporter, SmdSkeletalExporter,
    compute_root_bind_pyr, derive_definebone_lines, is_loop_clip,
    sanitize_clip_filename,
)
from .gltf_animation import (
    GltfClip, GltfSkin, load_buffer_bytes, parse_clips, parse_skin,
    parse_skin_vertex_data, peek_animations, sample_clip,
)
from ..utils.helpers import get_config_dir


# ============================================================================
# Animation result metadata
# ============================================================================

@dataclass
class AnimMeta:
    """Resolved info for one animation clip in the QC."""
    clip_name: str           # canonical sequence name
    smd_filename: str        # SMD filename relative to QC
    num_frames: int
    fps: float
    loop: bool


# ============================================================================
# Data Models
# ============================================================================

@dataclass
class ModelSet:
    """Source glTF model set with optional physics companion."""
    name: str
    base_dir: Path
    render_path: Path
    physics_path: Optional[Path] = None
    warnings: List[str] = field(default_factory=list)


@dataclass
class PreviewModel:
    """Resolved output preview for a single model."""
    name: str
    render_smd_path: Path
    physics_smd_path: Optional[Path]
    qc_path: Optional[Path]
    modelname: str
    cdmaterials: str
    concave: bool
    mass: float
    surfaceprop: str
    has_physics: bool
    will_overwrite: bool
    will_skip: bool
    warnings: List[str] = field(default_factory=list)
    load_status: str = "Unknown"  # Ok, Failed, Pending
    failure_reason: str = ""
    anim_summary: str = "static"  # "static" or "<n> clip(s)"


# ============================================================================
# Core Processing Classes
# ============================================================================

class ModelSetScanner:
    """Scan a folder for glTF/GLB model sets, optionally recursing into subfolders."""

    PHYSICS_PATTERNS = [
        re.compile(r'.*_physics\.(gltf|glb)$', re.IGNORECASE),
        re.compile(r'.*physics\.(gltf|glb)$', re.IGNORECASE),
    ]

    def __init__(self, root: Path, recursive: bool = True):
        self.root = Path(root)
        self.recursive = recursive

    @classmethod
    def is_physics_file(cls, filename: str) -> bool:
        """Check if filename indicates a physics mesh."""
        return any(p.match(filename) for p in cls.PHYSICS_PATTERNS)

    def _iter_dirs(self):
        """Yield (base, files) tuples — only the root when recursive=False."""
        if self.recursive:
            for base, _, files in os.walk(self.root):
                yield base, files
        else:
            try:
                files = [
                    e.name for e in os.scandir(self.root)
                    if e.is_file()
                ]
            except (FileNotFoundError, NotADirectoryError, PermissionError):
                files = []
            yield str(self.root), files

    def find_sets(self) -> List[ModelSet]:
        """Scan for model sets."""
        sets: List[ModelSet] = []
        seen_keys: set = set()

        for base, files in self._iter_dirs():
            base_path = Path(base)
            gltf_files = [f for f in files if f.lower().endswith(('.gltf', '.glb'))]
            
            if not gltf_files:
                continue

            # Separate render and physics files
            render_files = [f for f in gltf_files if not self.is_physics_file(f)]
            physics_files = {f: f for f in gltf_files if self.is_physics_file(f)}

            for render_file in render_files:
                # Extract base name
                name = Path(render_file).stem
                render_path = base_path / render_file
                
                # Look for matching physics file
                physics_path = None
                for phys_file in physics_files.keys():
                    phys_stem = Path(phys_file).stem
                    # Match patterns like: base_physics, basephysics
                    if phys_stem.lower().replace('_physics', '').replace('physics', '') == name.lower():
                        physics_path = base_path / phys_file
                        break

                # Create unique key to avoid duplicates
                key = (str(base_path), name)
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                warnings = []
                if not render_path.exists():
                    warnings.append("Render file missing")
                if physics_path and not physics_path.exists():
                    warnings.append("Physics file missing")

                sets.append(ModelSet(
                    name=name,
                    base_dir=base_path,
                    render_path=render_path,
                    physics_path=physics_path,
                    warnings=warnings
                ))

        return sets


def _is_source2_viewer_export(gltf_data: dict) -> bool:
    """Return True if this glTF was produced by Source 2 Viewer (valveresourceformat).

    S2V exports store mesh POSITION values in **vmdl/Source convention** (X-forward,
    Y-left, Z-up, inches) and attach a 0.0254-scale + cyclic-axis-swap matrix on a
    root node to convert into glTF spec convention (Y-up meters). Trimesh applies
    that matrix during scene-dump for static models but skips it for skinned models
    where the mesh nodes are scene-root siblings of the skeleton — producing two
    different orientations from the same exporter. The right answer is to ignore the
    matrix entirely and use the raw POSITION values, which are already in the SMD
    target convention.

    Detection: generator string identifies the typical case; the 0.0254-magnitude
    matrix on any node is a backup signal for stripped/edited files.
    """
    asset = gltf_data.get('asset') or {}
    generator = asset.get('generator', '') or ''
    if 'Source 2 Viewer' in generator:
        return True

    for n in gltf_data.get('nodes') or []:
        m = n.get('matrix')
        if not m:
            continue
        # Basis-vector lengths from a column-major 4x4
        for sx_components in ((m[0], m[1], m[2]), (m[4], m[5], m[6]), (m[8], m[9], m[10])):
            mag = (sx_components[0]**2 + sx_components[1]**2 + sx_components[2]**2) ** 0.5
            if 0.020 < mag < 0.030:
                return True
    return False


class GltfMeshLoader:
    """Load glTF/GLB and flatten to single Trimesh with baked transforms."""

    class _TolerantResolver(FilePathResolver):
        """File resolver that returns None for missing files instead of raising exceptions."""
        
        def __init__(self, path):
            super().__init__(path)
            
        def get(self, key):
            """Get file contents, return None if missing instead of raising."""
            try:
                return super().get(key)
            except (FileNotFoundError, IOError):
                # Silently ignore missing texture/image files
                return None

    @staticmethod
    def preflight_check(path: Path) -> Tuple[bool, List[str]]:
        """Check if GLTF dependencies exist before loading.
        
        Returns:
            (success, warnings_list)
            - success: False only if critical buffers (geometry) are missing
            - warnings_list: All missing files (textures are non-critical)
        """
        path = Path(path)
        
        # GLB files are self-contained
        if path.suffix.lower() == '.glb':
            return (True, [])
        
        # For GLTF, check referenced buffers
        try:
            with open(path, 'r', encoding='utf-8') as f:
                gltf_data = json.load(f)
        except Exception as e:
            return (False, [f"Failed to parse GLTF: {e}"])
        
        critical_missing = []
        warnings = []
        base_dir = path.parent
        
        # Check buffers (CRITICAL - contains geometry data)
        if 'buffers' in gltf_data:
            for idx, buffer in enumerate(gltf_data['buffers']):
                if 'uri' in buffer:
                    uri = buffer['uri']
                    # Skip data URIs
                    if uri.startswith('data:'):
                        continue
                    buffer_path = base_dir / uri
                    if not buffer_path.exists():
                        critical_missing.append(f"buffer[{idx}]: {uri}")
        
        # Check images (NON-CRITICAL - textures can be missing)
        if 'images' in gltf_data:
            for idx, image in enumerate(gltf_data['images']):
                if 'uri' in image:
                    uri = image['uri']
                    if uri.startswith('data:'):
                        continue
                    image_path = base_dir / uri
                    if not image_path.exists():
                        warnings.append(f"texture[{idx}]: {uri}")
        
        # Only fail if critical buffers are missing
        all_missing = critical_missing + warnings
        return (len(critical_missing) == 0, all_missing)

    @staticmethod
    def load_mesh(path: Path, verbose: bool = False) -> Tuple[Optional[trimesh.Trimesh], str]:
        """Load and flatten glTF/GLB to single mesh with proper UV handling.
        
        Returns:
            (mesh, error_message)
        """
        path = Path(path)
        
        try:
            # Load glTF JSON to extract material names and UV transforms (best-effort)
            gltf_data = None
            if path.suffix.lower() == '.gltf':
                try:
                    with open(path, 'r', encoding='utf-8') as f:
                        gltf_data = json.load(f)
                except Exception:
                    pass  # Non-critical, continue without material names

            # Use custom resolver that tolerates missing textures
            resolver = GltfMeshLoader._TolerantResolver(str(path.parent))

            loaded = trimesh.load(
                str(path),
                force='scene',
                process=False,
                resolver=resolver
            )

            # For Source 2 Viewer exports, bypass Scene.dump()'s transform baking so
            # we work with raw POSITION values (vmdl-space, inches). Otherwise dump
            # as usual so legitimate node placement transforms (Blender exports etc.)
            # are still applied.
            is_s2v = bool(gltf_data) and _is_source2_viewer_export(gltf_data)

            mesh = None
            if isinstance(loaded, trimesh.Scene):
                if is_s2v:
                    submeshes = list(loaded.geometry.values())
                else:
                    submeshes = loaded.dump(concatenate=False)
                if not submeshes:
                    return (None, "Mesh has no faces after loading")

                # Manual numpy concatenation. trimesh.util.concatenate would atlas-pack
                # the per-submesh TextureVisuals into a combined image and remap UVs into
                # sub-rectangles, silently destroying tiling and material UV ranges.
                material_names: List[str] = []
                material_index: Dict[str, int] = {}
                verts_list: List[np.ndarray] = []
                faces_list: List[np.ndarray] = []
                uvs_list: List[np.ndarray] = []
                face_materials_list: List[np.ndarray] = []
                vert_offset = 0

                for sm in submeshes:
                    if sm is None or not hasattr(sm, 'faces') or len(sm.faces) == 0:
                        continue

                    if hasattr(sm.visual, 'face_materials') and sm.visual.face_materials is not None and \
                       hasattr(sm.visual, 'materials') and sm.visual.materials is not None:
                        local_materials = []
                        for i, mat in enumerate(sm.visual.materials):
                            mat_name = getattr(mat, 'name', None) or f"material_{i}"
                            local_materials.append(mat_name)

                        local_face_materials = np.array(sm.visual.face_materials, dtype=int)
                        remapped = np.zeros_like(local_face_materials)
                        for local_idx, mat_name in enumerate(local_materials):
                            if mat_name not in material_index:
                                material_index[mat_name] = len(material_names)
                                material_names.append(mat_name)
                            remapped[local_face_materials == local_idx] = material_index[mat_name]
                        face_materials_list.append(remapped)
                    else:
                        mat_name = None
                        if hasattr(sm.visual, 'material') and sm.visual.material is not None:
                            mat_name = getattr(sm.visual.material, 'name', None)
                        if not mat_name and gltf_data and 'materials' in gltf_data and len(gltf_data['materials']) == 1:
                            mat_name = gltf_data['materials'][0].get('name')
                        if not mat_name:
                            mat_name = getattr(sm, 'name', None) or "material"

                        if mat_name not in material_index:
                            material_index[mat_name] = len(material_names)
                            material_names.append(mat_name)
                        face_materials_list.append(np.full(len(sm.faces), material_index[mat_name], dtype=int))

                    nv = len(sm.vertices)
                    verts_list.append(np.asarray(sm.vertices))
                    faces_list.append(np.asarray(sm.faces) + vert_offset)
                    try:
                        uv = sm.visual.uv if hasattr(sm.visual, 'uv') else None
                    except Exception:
                        uv = None
                    if uv is not None and len(uv) == nv:
                        uvs_list.append(np.asarray(uv, dtype=np.float64))
                    else:
                        uvs_list.append(np.zeros((nv, 2), dtype=np.float64))
                    vert_offset += nv

                if not verts_list:
                    return (None, "Mesh has no faces after loading")

                V = np.concatenate(verts_list)
                F = np.concatenate(faces_list)
                UV = np.concatenate(uvs_list)

                mesh = trimesh.Trimesh(vertices=V, faces=F, process=False)
                mesh.visual = trimesh.visual.TextureVisuals(uv=UV)

                if face_materials_list:
                    mesh.metadata['gltf_material_names'] = material_names
                    mesh.metadata['gltf_face_materials'] = np.concatenate(face_materials_list)

            elif isinstance(loaded, trimesh.Trimesh):
                mesh = loaded
            else:
                return (None, f"Unsupported mesh type: {type(loaded).__name__}")

            if mesh is None or not hasattr(mesh, 'faces') or len(mesh.faces) == 0:
                return (None, "Mesh has no faces after loading")

            # Preserve glTF texture coordinates exactly as loaded. Do not auto-normalize,
            # wrap, clamp, or otherwise "fix" UVs here; SMD export controls UV handling.

            if gltf_data and 'materials' in gltf_data and 'gltf_material_names' not in mesh.metadata:
                mesh.metadata['gltf_material_names'] = [
                    mat.get('name', f'material_{i}') for i, mat in enumerate(gltf_data['materials'])
                ]

            # Mark S2V exports so the worker skips both scale and axis_conversion:
            # raw POSITION values (used by the loader path above) are already in
            # vmdl-space inches, which is what SMD wants.
            if is_s2v:
                mesh.metadata['source2_native_units'] = True

            # Surface parsed JSON + base dir so the animation pipeline can
            # decode the .bin buffer without reparsing the file.
            if gltf_data is not None:
                mesh.metadata['gltf_data'] = gltf_data
                mesh.metadata['gltf_base_dir'] = path.parent

            return (mesh, "")

        except Exception as e:
            error_msg = str(e)
            if verbose:
                error_msg += f"\n{traceback.format_exc()}"
            return (None, error_msg)


class MeshProcessor:
    """Apply transformations and cleanup to meshes."""

    SURFACEPROP_DENSITIES = {
        'default': 1.0,
        'metal': 2.7,
        'wood': 0.65,
        'glass': 1.15,
        'concrete': 2.25,
        'plastic': 0.55,
        'foliage': 0.2,
        'dirt': 1.35,
        'tile': 1.8,
    }

    @staticmethod
    def apply_scale(mesh: trimesh.Trimesh, scale: float):
        """Apply uniform scale (bakes into vertices)."""
        mesh.apply_scale(scale)

    @staticmethod
    def apply_axis_conversion(mesh: trimesh.Trimesh):
        """Convert from Z-up (Blender/glTF) to Source engine coordinate system.
        
        Applies +90° rotation around X-axis to fix orientation.
        """
        try:
            # Rotate +90 degrees around X to fix orientation in Source engine
            rotation_matrix = trimesh.transformations.rotation_matrix(
                np.radians(90.0),
                [1, 0, 0]
            )
            mesh.apply_transform(rotation_matrix)
        except Exception:
            pass

    @staticmethod
    def sanitize(mesh: trimesh.Trimesh):
        """Remove invalid geometry. Keeps mesh.metadata['gltf_face_materials'] aligned."""
        face_materials = None
        if hasattr(mesh, 'metadata') and mesh.metadata:
            fm = mesh.metadata.get('gltf_face_materials')
            if fm is not None:
                face_materials = np.asarray(fm)

        try:
            mesh.remove_infinite_values()
            if face_materials is not None:
                mask = mesh.nondegenerate_faces()
                mesh.update_faces(mask)
                face_materials = face_materials[mask]
                mask = mesh.unique_faces()
                mesh.update_faces(mask)
                face_materials = face_materials[mask]
                mesh.metadata['gltf_face_materials'] = face_materials
            else:
                mesh.remove_degenerate_faces()
                mesh.remove_duplicate_faces()
            mesh.remove_unreferenced_vertices()
            mesh.fix_normals()
        except Exception:
            pass

    @staticmethod
    def process_physics(mesh: trimesh.Trimesh, weld_distance: float):
        """Apply physics-specific processing (AFTER scaling).
        
        Minimal processing to preserve topology for physics decomposition.
        """
        # Step 1: Merge vertices ONLY if weld distance is specified
        # This preserves the original mesh topology better
        if weld_distance > 0:
            try:
                mesh.merge_vertices(radius=weld_distance)
            except Exception:
                pass
        
        # Step 2: Basic cleanup - remove only truly broken geometry
        try:
            mesh.remove_infinite_values()
            mesh.remove_degenerate_faces()
        except Exception:
            pass
        
        # Step 3: Compute vertex normals WITHOUT destroying topology
        # Just ensure normals exist for SMD export
        try:
            # Clear cache to force recalculation
            if hasattr(mesh, '_cache'):
                mesh._cache.clear()
            
            # Access vertex normals to trigger calculation
            # Trimesh automatically computes smooth normals from face adjacency
            _ = mesh.vertex_normals
        except Exception:
            pass
        
        return mesh

    @staticmethod
    def calculate_mass(mesh: trimesh.Trimesh, density: float) -> Optional[float]:
        """Calculate mass from mesh volume."""
        if mesh is None:
            return None

        try:
            volume = float(mesh.volume)
        except Exception:
            volume = 0.0

        if volume <= 0.0:
            try:
                volume = float(mesh.convex_hull.volume)
            except Exception:
                volume = 0.0

        if volume <= 0.0:
            # Fallback to bounding box volume
            try:
                bounds = mesh.bounds
                extents = bounds[1] - bounds[0]
                volume = float(np.prod(extents))
            except Exception:
                volume = 0.0

        if volume <= 0.0:
            return None

        return max(0.001, volume * density)

    @classmethod
    def get_surfaceprop_density(cls, surfaceprop: Optional[str]) -> float:
        """Get mass density multiplier for a Source surfaceprop."""
        if not surfaceprop:
            return cls.SURFACEPROP_DENSITIES['default']

        return cls.SURFACEPROP_DENSITIES.get(surfaceprop.lower(), cls.SURFACEPROP_DENSITIES['default'])

    @classmethod
    def calculate_surface_mass(cls, mesh: trimesh.Trimesh, surfaceprop: Optional[str], mass_modifier: float) -> Optional[float]:
        """Calculate mesh mass using density tuned for the detected surface type."""
        density = cls.get_surfaceprop_density(surfaceprop)
        mass = cls.calculate_mass(mesh, density)
        if mass is None:
            return None

        return mass * mass_modifier

    @staticmethod
    def calculate_physics_properties(mesh: trimesh.Trimesh, mass: float) -> Dict[str, float]:
        """
        Calculate inertia, damping, and rotdamping based on mesh properties.
        
        Physics property guidelines:
        - Inertia: Higher = more sturdy/rigid, Lower = more wobbly
        - Damping: Linear velocity decay (0.0-1.0, higher = faster decay)
        - Rotdamping: Angular velocity decay (0.0-10.0, higher = faster decay)
        
        Returns dict with: inertia, damping, rotdamping
        """
        try:
            # Get mesh properties
            volume = abs(float(mesh.volume)) if hasattr(mesh, 'volume') else 0.0
            if volume <= 0:
                volume = abs(float(mesh.convex_hull.volume))
            
            # Calculate bounding box dimensions
            bounds = mesh.bounds
            extents = bounds[1] - bounds[0]
            max_extent = float(np.max(extents))
            min_extent = float(np.min(extents[extents > 0])) if np.any(extents > 0) else 1.0
            aspect_ratio = max_extent / min_extent if min_extent > 0 else 1.0
            
            # Inertia: Resistance to rotation changes
            # 0.3 = balanced - not too floppy, not too jittery
            inertia = 0.3
            
            # Damping: Linear velocity decay per second
            # Slight damping smooths out jitters and prevents bouncing
            # 0.05 = loses 5% velocity per second
            damping = 0.05
            
            # Rotdamping: Angular velocity decay per second  
            # Moderate rotdamping prevents endless spinning and smooths rotation
            # 0.2 = loses 20% angular velocity per second
            rotdamping = 0.2
            
            return {
                'inertia': round(inertia, 2),
                'damping': round(damping, 4),
                'rotdamping': round(rotdamping, 4)
            }
        
        except Exception:
            # Fallback to reasonable defaults
            return {
                'inertia': 1.0,
                'damping': 0.05,
                'rotdamping': 0.5
            }

    @staticmethod
    def force_smooth_normals_pymeshlab(mesh: trimesh.Trimesh) -> bool:
        """Use PyMeshLab to force smooth (per-vertex) normals. Returns True if successful."""
        if not HAS_PYMESHLAB:
            return False
        
        try:
            # Create a new MeshSet
            ms = pymeshlab.MeshSet()
            
            # Add the trimesh as a pymeshlab mesh
            # Convert trimesh to pymeshlab format
            m = pymeshlab.Mesh(
                vertex_matrix=mesh.vertices,
                face_matrix=mesh.faces
            )
            ms.add_mesh(m)
            
            # Clear any existing per-face normals and wedge normals
            # This forces the mesh to only have per-vertex normals
            ms.clear_per_face_normals()
            ms.clear_per_wedge_normals()
            
            # Compute smooth vertex normals
            # This averages face normals at each vertex (Blender's "Shade Smooth")
            ms.compute_normal_per_vertex()
            
            # Get the processed mesh back
            processed_mesh = ms.current_mesh()
            
            # Update the original trimesh with smooth normals
            # PyMeshLab stores normals in vertex_normal_matrix()
            smooth_normals = processed_mesh.vertex_normal_matrix()
            
            # Force these normals into trimesh's cache
            if not hasattr(mesh, '_cache'):
                mesh._cache = trimesh.caching.Cache()
            mesh._cache['vertex_normals'] = smooth_normals
            
            return True
            
        except Exception as e:
            return False


class SurfacepropDetector:
    """Detect surfaceprop from file name and material hints."""

    HEURISTICS = {
        'metal': ['metal', 'pipe', 'grate', 'iron', 'steel', 'aluminum', 'aluminium'],
        'wood': ['wood', 'plank', 'crate', 'barrel', 'timber'],
        'glass': ['glass', 'window'],
        'concrete': ['concrete', 'brick', 'rock', 'stone', 'cement'],
        'plastic': ['plastic', 'polymer'],
        'foliage': ['foliage', 'plant', 'leaf', 'tree', 'bush', 'xen', 'flora'],
        'dirt': ['dirt', 'soil', 'ground'],
        'tile': ['tile', 'ceramic'],
    }

    @classmethod
    def detect(cls, name: str, material_name: Optional[str] = None) -> str:
        """Detect surfaceprop from name and material."""
        text = name.lower()
        if material_name:
            text += ' ' + material_name.lower()

        for surfaceprop, keywords in cls.HEURISTICS.items():
            if any(kw in text for kw in keywords):
                return surfaceprop

        return 'default'


class QcWriter:
    """Write Source 1 QC files."""

    @staticmethod
    def _normalize_path(p: str) -> str:
        return p.replace("\\", "/")

    @staticmethod
    def write_qc(
        out_path: Path,
        name: str,
        modelname: str,
        cdmaterials: str,
        surfaceprop: str,
        has_physics: bool,
        concave: bool,
        mass: Optional[float],
        volume: Optional[float] = None,
        inertia: Optional[float] = None,
        damping: Optional[float] = None,
        rotdamping: Optional[float] = None,
        is_animated: bool = False,
        skin: Optional[GltfSkin] = None,
        coord: Optional[CoordinateMode] = None,
        animations: Optional[List[AnimMeta]] = None,
    ):
        """Write QC file with optional skeletal-animation block."""
        lines = []
        lines.append(f'$modelname "{QcWriter._normalize_path(modelname)}"')

        if cdmaterials:
            lines.append(f'$cdmaterials "{QcWriter._normalize_path(cdmaterials)}"')

        if surfaceprop:
            lines.append(f'$surfaceprop "{surfaceprop}"')

        if is_animated and skin is not None and coord is not None:
            # Skinned model with animations — drop $staticprop, declare bones
            # explicitly so studiomdl preserves the full hierarchy, and emit
            # an $animation/$sequence pair per clip.
            lines.append(f'$body "body" "{name}.smd"')
            lines.append('')
            lines.extend(derive_definebone_lines(skin, coord))
            lines.append('')
            root_name = skin.joints[skin.root_joint_idx].name
            lines.append(f'$root "{root_name}"')
            lines.append('')

            for am in (animations or []):
                lines.append(
                    f'$animation "a_{am.clip_name}" "{am.smd_filename}" fps {am.fps:g}'
                )
            lines.append('')

            for am in (animations or []):
                opts = [f'fps {am.fps:g}']
                if am.loop:
                    opts.append('loop')
                opts_str = ' '.join(opts)
                lines.append(
                    f'$sequence "{am.clip_name}" {{ "a_{am.clip_name}" {opts_str} }}'
                )
            lines.append('')
        else:
            lines.append('$staticprop')
            lines.append(f'$body "body" "{name}.smd"')
            lines.append(f'$sequence "idle" "{name}.smd" fps 1')

        if has_physics:
            lines.append(f'$collisionmodel "{name}_physics.smd" {{')
            lines.append('    $remove2d')
            if concave:
                lines.append('    $concave')
            lines.append('    $maxconvexpieces 10000')
            if mass is not None:
                lines.append(f'    $mass {mass:.3f}')
            
            # Add physics properties based on mesh characteristics
            if inertia is not None:
                lines.append(f'    $inertia {inertia:.2f}')
            if damping is not None:
                lines.append(f'    $damping {damping:.4f}')
            if rotdamping is not None:
                lines.append(f'    $rotdamping {rotdamping:.4f}')
            
            lines.append('}')

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


# ============================================================================
# Batch Runner
# ============================================================================

class BatchRunner(QThread):
    """Execute batch conversion with progress updates."""
    
    progress = Signal(int, int, str)  # current, total, message
    finished = Signal(bool, str, dict)  # success, message, stats

    def __init__(
        self,
        model_sets: List[ModelSet],
        input_root: Path,
        output_root: Path,
        scale: float,
        weld_distance: float,
        flip_v: bool,
        export_physics: bool,
        preserve_folders: bool,
        generate_smd: bool,
        generate_qc: bool,
        modelname_template: str,
        cdmaterials: str,
        concave: bool,
        auto_mass: bool,
        mass_modifier: float,
        static_mass: float,
        auto_surfaceprop: bool,
        static_surfaceprop: str,
        replace_existing: bool,
        dry_run: bool,
        verbose: bool = False,
        axis_conversion: bool = True,
        uv_mode: str = 'preserve',
        export_animations: bool = True,
        animation_fps: float = 30.0,
        auto_loop_detect: bool = True,
        skip_physics_for_large: bool = True,
        large_model_threshold: float = 1024.0,
    ):
        super().__init__()
        self.model_sets = model_sets
        self.input_root = input_root
        self.output_root = output_root
        self.scale = scale
        self.weld_distance = weld_distance
        self.flip_v = flip_v
        self.export_physics = export_physics
        self.preserve_folders = preserve_folders
        self.generate_smd = generate_smd
        self.generate_qc = generate_qc
        self.modelname_template = modelname_template
        self.cdmaterials = cdmaterials
        self.concave = concave
        self.auto_mass = auto_mass
        self.mass_modifier = mass_modifier
        self.static_mass = static_mass
        self.auto_surfaceprop = auto_surfaceprop
        self.static_surfaceprop = static_surfaceprop
        self.replace_existing = replace_existing
        self.dry_run = dry_run
        self.verbose = verbose
        self.axis_conversion = axis_conversion
        self.uv_mode = uv_mode
        self.export_animations = export_animations
        self.animation_fps = animation_fps
        self.auto_loop_detect = auto_loop_detect
        self.skip_physics_for_large = skip_physics_for_large
        self.large_model_threshold = large_model_threshold

    def _get_output_dir(self, model_set: ModelSet) -> Path:
        """Calculate output directory for model."""
        if self.preserve_folders:
            try:
                rel = model_set.base_dir.relative_to(self.input_root)
                return self.output_root / rel
            except ValueError:
                return self.output_root
        return self.output_root

    def _resolve_modelname(self, name: str, out_dir: Path) -> str:
        """Resolve $modelname from template (prefix + asset name)."""
        prefix = self.modelname_template.strip()
        
        if not prefix:
            # Default: use relative path from output root + asset name
            try:
                rel = out_dir.relative_to(self.output_root)
                if str(rel) != ".":
                    return f"{str(rel).replace(chr(92), '/')}/{name}.mdl"
                return f"{name}.mdl"
            except ValueError:
                return f"{name}.mdl"
        
        # Treat prefix as a path prefix, append asset name
        prefix = prefix.replace("\\", "/")
        if not prefix.endswith("/"):
            prefix += "/"
        
        modelname = f"{prefix}{name}.mdl"
        return modelname

    def _should_skip(self, out_dir: Path, name: str, has_physics: bool) -> bool:
        """Check if model should be skipped due to existing outputs."""
        if self.replace_existing:
            return False

        # Check render SMD
        if (out_dir / f"{name}.smd").exists():
            return True

        # Check physics SMD
        if has_physics and self.export_physics and (out_dir / f"{name}_physics.smd").exists():
            return True

        # Check QC
        if self.generate_qc and (out_dir / f"{name}.qc").exists():
            return True

        return False

    def run(self):
        """Execute batch conversion."""
        start_time = time.time()
        total = len(self.model_sets)
        converted = 0
        skipped = 0
        errors = []

        for idx, model_set in enumerate(self.model_sets, start=1):
            if self.isInterruptionRequested():
                stats = {'converted': converted, 'skipped': skipped, 'errors': len(errors), 'cancelled': True}
                self.finished.emit(False, f"Cancelled after {idx-1}/{total}", stats)
                return

            has_physics = model_set.physics_path is not None
            out_dir = self._get_output_dir(model_set)

            # Check skip
            if self._should_skip(out_dir, model_set.name, has_physics):
                self.progress.emit(idx, total, f"Skipped {model_set.name} (exists)")
                skipped += 1
                continue

            self.progress.emit(idx, total, f"Processing {model_set.name}...")

            if self.dry_run:
                converted += 1
                continue

            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                errors.append(f"{model_set.name}: Failed to create output dir")
                continue

            # Preflight check render mesh
            preflight_ok, missing = GltfMeshLoader.preflight_check(model_set.render_path)
            if not preflight_ok:
                error_detail = f"{model_set.name}: Missing dependencies - {', '.join(missing)}"
                errors.append(error_detail)
                self.progress.emit(idx, total, error_detail)
                continue

            # Load render mesh
            render_mesh, load_error = GltfMeshLoader.load_mesh(model_set.render_path, self.verbose)
            if render_mesh is None:
                error_detail = f"{model_set.name}: Failed to load render mesh\n  Path: {model_set.render_path}\n  Error: {load_error}"
                errors.append(error_detail)
                if self.verbose:
                    self.progress.emit(idx, total, error_detail)
                else:
                    self.progress.emit(idx, total, f"{model_set.name}: Failed to load render mesh")
                continue

            # Process render. Source 2 Viewer's root matrix simultaneously converts
            # inches->meters AND swaps vmdl's Z-up basis to glTF's Y-up basis. When the
            # matrix reaches the geometry (static models, source2_native_units=False) we
            # need both ×40 and the +90°X back-rotation. When it's orphaned (skinned
            # models, source2_native_units=True) the geometry is already in vmdl Z-up
            # inches and needs neither.
            native_units = bool(render_mesh.metadata.get('source2_native_units'))
            render_scale = 1.0 if native_units else self.scale
            if render_scale != self.scale:
                self.progress.emit(idx, total, f"{model_set.name}: Source-units glTF detected, using scale=1.0")

            # Detect skin + animations. The buffer is loaded lazily — only when
            # both export_animations is on and the file actually carries skin
            # data — to keep the static-prop path's I/O cost unchanged.
            skin: Optional[GltfSkin] = None
            clips: List[GltfClip] = []
            vertex_joints = None
            vertex_weights = None
            gltf_data = render_mesh.metadata.get('gltf_data')
            gltf_base_dir = render_mesh.metadata.get('gltf_base_dir')
            if (self.export_animations and gltf_data is not None
                    and gltf_base_dir is not None
                    and gltf_data.get('skins') and gltf_data.get('animations')):
                buf = load_buffer_bytes(gltf_data, gltf_base_dir)
                if buf is not None:
                    skin = parse_skin(gltf_data, buf)
                    if skin is not None:
                        clips = parse_clips(gltf_data, buf, skin)
                        vertex_joints, vertex_weights = parse_skin_vertex_data(gltf_data, buf)

            is_animated = bool(
                skin and clips and vertex_joints is not None and vertex_weights is not None
            )

            MeshProcessor.apply_scale(render_mesh, render_scale)
            apply_swap = self.axis_conversion and not native_units
            if apply_swap:
                MeshProcessor.apply_axis_conversion(render_mesh)
            # Skip sanitize() in the animated path — it calls
            # remove_unreferenced_vertices() which reorders the vertex array
            # and would silently desynchronise JOINTS_0 / WEIGHTS_0.
            if not is_animated:
                MeshProcessor.sanitize(render_mesh)

            coord = CoordinateMode(scale=render_scale, swap_axes=apply_swap)
            animations: List[AnimMeta] = []

            # Write render SMD
            render_smd = out_dir / f"{model_set.name}.smd"
            if self.generate_smd:
                if is_animated:
                    assert skin is not None and vertex_joints is not None and vertex_weights is not None
                    success, uv_warning = SmdSkeletalExporter.write_skinned(
                        render_mesh, skin, vertex_joints, vertex_weights,
                        render_smd, model_set.name, coord,
                        flip_v=self.flip_v, uv_mode=self.uv_mode,
                    )
                    if not success:
                        errors.append(f"{model_set.name}: Failed to write skinned SMD: {uv_warning}")
                        continue
                    elif uv_warning and self.verbose:
                        self.progress.emit(idx, total, f"{model_set.name}: {uv_warning}")
                    self.progress.emit(
                        idx, total,
                        f"{model_set.name}: skinned ref SMD ({len(skin.joints)} bones)"
                    )

                    # Per-clip animation SMDs.
                    # S2V sometimes exports each clip twice — once with its
                    # canonical name and once with an "@"-prefixed alias
                    # (e.g. "closed_idle" + "@closed_idle"). Both entries
                    # share their channel data, and our sanitiser strips
                    # non-alnum chars so the prefixed alias collapses to the
                    # same filename. Without dedupe we'd overwrite the SMD
                    # in place AND emit duplicate $animation/$sequence
                    # entries in the QC, which studiomdl rejects with
                    # "Duplicate animation name". Track sanitised names and
                    # skip subsequent collisions.
                    seen_clip_names: set = set()
                    for clip in clips:
                        safe = sanitize_clip_filename(clip.name)
                        if safe in seen_clip_names:
                            self.progress.emit(
                                idx, total,
                                f"{model_set.name}: skipping duplicate clip "
                                f"{clip.name!r} (resolves to {safe!r}, already written)"
                            )
                            continue
                        seen_clip_names.add(safe)

                        anim_smd = out_dir / f"{model_set.name}_anim_{safe}.smd"
                        frames, num_frames = sample_clip(clip, skin, fps=self.animation_fps)
                        ok, anim_err = SmdAnimationExporter.write_animation(
                            skin, frames, anim_smd, coord,
                        )
                        if not ok:
                            errors.append(
                                f"{model_set.name}: Failed to write anim {clip.name!r}: {anim_err}"
                            )
                            continue
                        loop = is_loop_clip(clip.name) if self.auto_loop_detect else False
                        animations.append(AnimMeta(
                            clip_name=safe,
                            smd_filename=anim_smd.name,
                            num_frames=num_frames,
                            fps=self.animation_fps,
                            loop=loop,
                        ))
                    self.progress.emit(
                        idx, total,
                        f"{model_set.name}: wrote {len(animations)} animation SMD(s)"
                    )
                else:
                    success, uv_warning = SmdExporter.write_static(render_mesh, render_smd, model_set.name, self.flip_v, False, uv_mode=self.uv_mode)
                    if not success:
                        errors.append(f"{model_set.name}: Failed to write render SMD")
                        continue
                    elif uv_warning and self.verbose:
                        self.progress.emit(idx, total, f"{model_set.name}: {uv_warning}")
            else:
                if self.verbose:
                    self.progress.emit(idx, total, f"{model_set.name}: Skipping SMD generation (disabled)")


            # Physics processing. If the source physics glTF is missing, has unmet
            # preflight deps, or loads empty (Source 2 Viewer emits stub physics glTFs
            # for vmdls that have no collision data), fall back to using the already-
            # processed render mesh so the prop still gets a real $collisionmodel
            # instead of studiomdl's default-sphere fallback.
            physics_mesh = None
            # vphysics chokes on enormous collision meshes — multi-thousand-inch
            # props (Citadel, skyboxes, drop-ships) can crash the engine on load.
            # Bypass the entire physics block when the render mesh exceeds the
            # configured threshold; the QC will then omit $collisionmodel and
            # studiomdl will leave the model with no collision (which is what
            # you want for huge static set-pieces anyway).
            render_too_large = False
            if self.export_physics and self.skip_physics_for_large:
                try:
                    render_extent = float(np.max(render_mesh.bounds[1] - render_mesh.bounds[0]))
                except Exception:
                    render_extent = 0.0
                if render_extent > self.large_model_threshold:
                    render_too_large = True
                    self.progress.emit(
                        idx, total,
                        f"{model_set.name}: skipping physics — bbox {render_extent:.0f}u "
                        f"exceeds {self.large_model_threshold:.0f}u threshold "
                        f"(vphysics may crash on collision this large)"
                    )
            if self.export_physics and not render_too_large:
                loaded_physics = None
                load_failure = None

                physics_path = model_set.physics_path
                if has_physics and physics_path is not None:
                    preflight_ok, missing = GltfMeshLoader.preflight_check(physics_path)
                    if not preflight_ok:
                        load_failure = f"missing dependencies - {', '.join(missing)}"
                    else:
                        loaded_physics, load_error = GltfMeshLoader.load_mesh(physics_path, self.verbose)
                        if loaded_physics is None:
                            load_failure = load_error or "empty physics glTF"

                if loaded_physics is not None:
                    physics_mesh = loaded_physics
                    physics_native = bool(physics_mesh.metadata.get('source2_native_units'))
                    physics_scale = 1.0 if physics_native else self.scale
                    MeshProcessor.apply_scale(physics_mesh, physics_scale)
                    if self.axis_conversion and not physics_native:
                        MeshProcessor.apply_axis_conversion(physics_mesh)
                else:
                    physics_mesh = render_mesh.copy()
                    # Per-face render materials must not leak into physics SMD; the
                    # exporter would otherwise emit triangles under render material
                    # names instead of the single physics material.
                    if hasattr(physics_mesh, 'metadata') and physics_mesh.metadata:
                        physics_mesh.metadata.pop('gltf_face_materials', None)
                        physics_mesh.metadata.pop('gltf_material_names', None)
                    reason = load_failure if has_physics else "no physics glTF in source"
                    self.progress.emit(idx, total, f"{model_set.name}: {reason}; using render mesh as physics fallback")

                if physics_mesh is not None:
                    # CRITICAL: Source uses vertex normals to detect convex decomposition
                        # Must have smooth shading (averaged vertex normals) for proper physics
                        # Physics meshes are often pre-split into convex pieces with hard edges
                        # We need to FORCE smooth normals across the entire mesh
                        smooth_normals = None
                        
                        try:
                            if self.verbose:
                                before_verts = len(physics_mesh.vertices)
                            
                            # Physics meshes are often disconnected convex pieces
                            # Normal merge won't work - we need spatial averaging
                            
                            # Step 1: Build a spatial index of all vertices
                            # Group vertices by position (within tolerance)
                            tolerance = 1e-5
                            vertex_groups = {}
                            
                            for i, vert in enumerate(physics_mesh.vertices):
                                # Round position to create spatial buckets
                                key = tuple(np.round(vert / tolerance).astype(int))
                                if key not in vertex_groups:
                                    vertex_groups[key] = []
                                vertex_groups[key].append(i)
                            
                            # Step 2: Calculate smooth normals by averaging face normals
                            # for all faces that touch each spatial location
                            face_normals = physics_mesh.face_normals
                            vertex_to_faces = [[] for _ in range(len(physics_mesh.vertices))]
                            
                            # Build vertex->face mapping
                            for face_idx, face in enumerate(physics_mesh.faces):
                                for vid in face:
                                    vertex_to_faces[vid].append(face_idx)
                            
                            # Calculate smooth normals by position
                            smooth_normals = np.zeros_like(physics_mesh.vertices)
                            
                            for group_verts in vertex_groups.values():
                                # Collect all face normals for this spatial location
                                contributing_faces = set()
                                for vid in group_verts:
                                    contributing_faces.update(vertex_to_faces[vid])
                                
                                # Average all contributing face normals
                                if contributing_faces:
                                    avg_normal = np.mean([face_normals[fid] for fid in contributing_faces], axis=0)
                                    # Normalize
                                    norm = np.linalg.norm(avg_normal)
                                    if norm > 0:
                                        avg_normal /= norm
                                    
                                    # Assign to all vertices at this position
                                    for vid in group_verts:
                                        smooth_normals[vid] = avg_normal
                            
                            if self.verbose:
                                unique_positions = len(vertex_groups)
                                self.progress.emit(idx, total, f"{model_set.name}: {unique_positions} unique positions from {before_verts} vertices")
                            
                            # Debug: Sample a few calculated normals
                            if self.verbose and len(smooth_normals) >= 10:
                                # Check first face vertices
                                first_face = physics_mesh.faces[0]
                                v0, v1, v2 = first_face
                                all_same = np.allclose(smooth_normals[v0], smooth_normals[v1]) and \
                                          np.allclose(smooth_normals[v1], smooth_normals[v2])
                                
                                if all_same:
                                    self.progress.emit(idx, total, f"{model_set.name}: ⚠ First face still has flat normals!")
                                else:
                                    self.progress.emit(idx, total, f"{model_set.name}: ✓ First face has smooth normals")
                                
                                # Count unique normals
                                unique_count = len(np.unique(smooth_normals.round(decimals=4), axis=0))
                                self.progress.emit(idx, total, f"{model_set.name}: {unique_count}/{len(smooth_normals)} unique normals")
                        
                        except Exception as e:
                            if self.verbose:
                                self.progress.emit(idx, total, f"{model_set.name}: ERROR: {str(e)}")
                                import traceback
                                self.progress.emit(idx, total, traceback.format_exc())
                            smooth_normals = None
                        
                        physics_smd = out_dir / f"{model_set.name}_physics.smd"
                        # Use proper material name for physics meshes
                        physics_material = "physics_group_prop.wood_crate_material"
                        # When the main skeleton's root bone has a non-identity
                        # bind (e.g. S2V skinned exports bake a Y-up swap into
                        # root.rest_rotation → ~(0, 90, 90) PYR), the physics
                        # SMD's "root" must mirror that bind so studiomdl's
                        # inv(phys_root)*main_root cancels at runtime instead of
                        # rotating the collision hull off-axis.
                        physics_root_bind = (
                            compute_root_bind_pyr(skin, coord) if is_animated and skin else None
                        )
                        # Pass smooth normals explicitly to ensure they're written to SMD
                        if self.generate_smd:
                            success, uv_warning = SmdExporter.write_static(
                                physics_mesh, physics_smd, physics_material,
                                self.flip_v, False, smooth_normals,
                                uv_mode=self.uv_mode, root_bind=physics_root_bind,
                            )
                            if not success:
                                physics_mesh = None
                                errors.append(f"{model_set.name}: Failed to write physics SMD")
                            elif uv_warning and self.verbose:
                                self.progress.emit(idx, total, f"{model_set.name} (physics): {uv_warning}")
                        else:
                            if self.verbose:
                                self.progress.emit(idx, total, f"{model_set.name}: Skipping physics SMD generation (disabled)")


            # Detect surfaceprop
            surfaceprop = self.static_surfaceprop
            if self.auto_surfaceprop:
                surfaceprop = SurfacepropDetector.detect(model_set.name)

            # Calculate mass
            mass = None
            if physics_mesh and self.auto_mass:
                mass = MeshProcessor.calculate_surface_mass(physics_mesh, surfaceprop, self.mass_modifier)
            elif not self.auto_mass:
                mass = self.static_mass

            # Calculate physics properties based on mesh if we have physics and mass
            phys_props = None
            if physics_mesh and mass:
                phys_props = MeshProcessor.calculate_physics_properties(physics_mesh, mass)

            # Write QC
            if self.generate_qc:
                qc_path = out_dir / f"{model_set.name}.qc"
                modelname = self._resolve_modelname(model_set.name, out_dir)
                QcWriter.write_qc(
                    qc_path,
                    model_set.name,
                    modelname,
                    self.cdmaterials,
                    surfaceprop,
                    physics_mesh is not None,
                    self.concave,
                    mass,
                    volume=abs(float(physics_mesh.volume)) if physics_mesh else None,
                    inertia=phys_props['inertia'] if phys_props else None,
                    damping=phys_props['damping'] if phys_props else None,
                    rotdamping=phys_props['rotdamping'] if phys_props else None,
                    is_animated=is_animated,
                    skin=skin if is_animated else None,
                    coord=coord if is_animated else None,
                    animations=animations if is_animated else None,
                )

            converted += 1

        elapsed = time.time() - start_time
        stats = {
            'converted': converted,
            'skipped': skipped,
            'errors': len(errors),
            'elapsed': elapsed,
            'error_list': errors
        }

        msg = f"Converted {converted}, Skipped {skipped}, Errors {len(errors)} ({elapsed:.1f}s)"
        self.finished.emit(True, msg, stats)


# ============================================================================
# GUI Tool
# ============================================================================

class GltfSmdBatchTool(BaseTool):
    """GLTF to SMD batch converter with live preview."""

    def __init__(self):
        super().__init__("GLTF Batch SMD")
        self.model_sets: List[ModelSet] = []
        self.preview_models: List[PreviewModel] = []
        self.auto_rescan_enabled = False
        self.thread: Optional[BatchRunner] = None
        self.recent_runs: List[Dict[str, str]] = []
        self.recent_runs_file = get_config_dir() / "gltf_smd_recent_runs.json"
        self.load_recent_runs()
        self.setup_content()

    def setup_content(self):
        """Two-pane layout: settings on the left (scrollable, narrow), preview
        + run controls on the right (always visible). Mirrors the VMAT PBR
        tool so the two batch tools share a familiar shape."""
        root = QHBoxLayout()
        self.content_layout.addLayout(root)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_settings_pane())
        splitter.addWidget(self._build_preview_pane())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([460, 880])
        root.addWidget(splitter)

        # Initial-state plumbing that depends on widgets being constructed.
        self._on_export_animations_toggled(self.export_animations_check.isChecked())
        self._on_auto_mass_toggled(self.auto_mass_radio.isChecked())
        self._on_auto_surf_toggled(self.auto_surf_radio.isChecked())

    # ------------------------------------------------------------------
    # Pane builders
    # ------------------------------------------------------------------

    def _build_settings_pane(self) -> QWidget:
        """Left pane: scrollable column of settings groupboxes."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        container = QWidget()
        col = QVBoxLayout(container)
        col.setContentsMargins(0, 0, 6, 0)
        col.setSpacing(8)

        col.addWidget(self._build_folders_group())
        col.addWidget(self._build_processing_group())
        col.addWidget(self._build_output_group())
        col.addWidget(self._build_qc_group())
        col.addWidget(self._build_physics_group())
        col.addWidget(self._build_advanced_group())
        col.addStretch()

        scroll.setWidget(container)
        scroll.setMinimumWidth(420)
        return scroll

    def _build_folders_group(self) -> QGroupBox:
        """Recent runs + input/output folders, stacked."""
        group = QGroupBox("Folders & Recent Runs")
        form = QFormLayout()

        self.recent_combo = QComboBox()
        self.recent_combo.addItem("-- Select a recent run --")
        self.recent_combo.currentIndexChanged.connect(self.on_recent_run_selected)
        self.populate_recent_runs()
        form.addRow("Recent run:", self.recent_combo)

        self.input_edit = QLineEdit()
        input_btn = QPushButton("Browse...")
        input_btn.clicked.connect(self.browse_input)
        form.addRow("Input:", self._row_widget(self.input_edit, input_btn))

        self.output_edit = QLineEdit()
        output_btn = QPushButton("Browse...")
        output_btn.clicked.connect(self.browse_output)
        form.addRow("Output:", self._row_widget(self.output_edit, output_btn))

        # Inline warning for input==output — no longer floating after the
        # left column, since the right pane is the focal area now.
        self.input_output_warning = QLabel(
            "⚠️ Input and Output are the same. You may overwrite source exports."
        )
        self.input_output_warning.setStyleSheet("color: orange; font-weight: bold;")
        self.input_output_warning.setVisible(False)
        self.input_output_warning.setWordWrap(True)
        form.addRow("", self.input_output_warning)

        group.setLayout(form)
        return group

    def _build_processing_group(self) -> QGroupBox:
        group = QGroupBox("Processing")
        form = QFormLayout()

        self.scale_spin = QDoubleSpinBox()
        self.scale_spin.setRange(0.001, 10000.0)
        self.scale_spin.setValue(40.0)
        self.scale_spin.setDecimals(3)
        self.scale_spin.valueChanged.connect(self.update_preview)
        form.addRow("Scale:", self.scale_spin)

        self.weld_spin = QDoubleSpinBox()
        self.weld_spin.setRange(0.0, 1.0)
        self.weld_spin.setDecimals(6)
        self.weld_spin.setSingleStep(0.00001)
        self.weld_spin.setValue(0.0001)
        self.weld_spin.valueChanged.connect(self.update_preview)
        form.addRow("Weld distance:", self.weld_spin)

        group.setLayout(form)
        return group

    def _build_output_group(self) -> QGroupBox:
        """Generation toggles + animation knobs."""
        group = QGroupBox("Output")
        form = QFormLayout()

        # 2x2 grid for the boolean generate-X toggles.
        gen_grid = QGridLayout()
        gen_grid.setContentsMargins(0, 0, 0, 0)
        gen_grid.setHorizontalSpacing(12)
        self.generate_smd_check = QCheckBox("Generate SMD")
        self.generate_smd_check.setChecked(True)
        self.generate_smd_check.toggled.connect(self.update_preview)
        self.generate_qc_check = QCheckBox("Generate QC")
        self.generate_qc_check.setChecked(True)
        self.generate_qc_check.toggled.connect(self.update_preview)
        gen_grid.addWidget(self.generate_smd_check, 0, 0)
        gen_grid.addWidget(self.generate_qc_check, 0, 1)
        form.addRow("Generate:", self._wrap_layout(gen_grid))

        self.export_animations_check = QCheckBox("Export animations")
        self.export_animations_check.setChecked(True)
        self.export_animations_check.setToolTip(
            "When the glTF has skin + animation data, write a skinned reference "
            "SMD plus per-clip animation SMDs and emit $definebone / $animation / "
            "$sequence in the QC. Files without animations stay on the static path."
        )
        self.export_animations_check.toggled.connect(self.update_preview)
        self.export_animations_check.toggled.connect(self._on_export_animations_toggled)
        form.addRow("", self.export_animations_check)

        self.animation_fps_spin = QDoubleSpinBox()
        self.animation_fps_spin.setRange(1.0, 120.0)
        self.animation_fps_spin.setValue(30.0)
        self.animation_fps_spin.setDecimals(1)
        self.animation_fps_spin.setToolTip(
            "Sampling rate for animation SMDs. 30 matches Source's default."
        )
        form.addRow("  Animation FPS:", self.animation_fps_spin)

        self.auto_loop_check = QCheckBox("Auto-detect looping clips")
        self.auto_loop_check.setChecked(True)
        self.auto_loop_check.setToolTip(
            "Mark $sequences as 'loop' when the clip name contains idle / loop / "
            "walk / run / breathe / cycle."
        )
        form.addRow("", self.auto_loop_check)

        group.setLayout(form)
        return group

    def _build_qc_group(self) -> QGroupBox:
        """Modelname / cdmaterials / mass + surfaceprop radio groups.
        Mass and surfaceprop fields auto-disable when their radio is off."""
        group = QGroupBox("QC")
        form = QFormLayout()

        self.modelname_edit = QLineEdit()
        self.modelname_edit.setPlaceholderText("{name} or path/to/{name}")
        self.modelname_edit.textChanged.connect(self.update_preview)
        form.addRow("Model name:", self.modelname_edit)

        self.cdmaterials_edit = QLineEdit()
        self.cdmaterials_edit.setText("models/")
        self.cdmaterials_edit.textChanged.connect(self.update_preview)
        form.addRow("cdmaterials:", self.cdmaterials_edit)

        self.concave_check = QCheckBox("$concave")
        self.concave_check.setChecked(True)
        self.concave_check.toggled.connect(self.update_preview)
        form.addRow("", self.concave_check)

        # Mass: radio pair, each with its own knob. Side-by-side rows so the
        # "auto" path and "static" path don't get mistaken for a four-row
        # checklist.
        mass_group = QButtonGroup(self)
        self.auto_mass_radio = QRadioButton("Auto mass")
        self.auto_mass_radio.setChecked(True)
        self.auto_mass_radio.toggled.connect(self.update_preview)
        self.auto_mass_radio.toggled.connect(self._on_auto_mass_toggled)
        mass_group.addButton(self.auto_mass_radio)
        self.mass_modifier_spin = QDoubleSpinBox()
        self.mass_modifier_spin.setRange(0.001, 10000.0)
        self.mass_modifier_spin.setValue(1.0)
        self.mass_modifier_spin.setDecimals(3)
        self.mass_modifier_spin.valueChanged.connect(self.update_preview)
        form.addRow(self.auto_mass_radio, self.mass_modifier_spin)

        self.static_mass_radio = QRadioButton("Static mass")
        mass_group.addButton(self.static_mass_radio)
        self.static_mass_radio.toggled.connect(self.update_preview)
        self.static_mass_spin = QDoubleSpinBox()
        self.static_mass_spin.setRange(0.001, 10000.0)
        self.static_mass_spin.setValue(10.0)
        self.static_mass_spin.setDecimals(3)
        self.static_mass_spin.valueChanged.connect(self.update_preview)
        form.addRow(self.static_mass_radio, self.static_mass_spin)

        # Surfaceprop pair on the same compact pattern.
        surf_group = QButtonGroup(self)
        self.auto_surf_radio = QRadioButton("Auto surfaceprop")
        self.auto_surf_radio.setChecked(True)
        self.auto_surf_radio.toggled.connect(self.update_preview)
        self.auto_surf_radio.toggled.connect(self._on_auto_surf_toggled)
        surf_group.addButton(self.auto_surf_radio)
        form.addRow("", self.auto_surf_radio)

        self.static_surf_radio = QRadioButton("Static surfaceprop")
        surf_group.addButton(self.static_surf_radio)
        self.static_surf_radio.toggled.connect(self.update_preview)
        self.static_surf_edit = QLineEdit()
        self.static_surf_edit.setText("metal")
        self.static_surf_edit.textChanged.connect(self.update_preview)
        form.addRow(self.static_surf_radio, self.static_surf_edit)

        group.setLayout(form)
        return group

    def _build_physics_group(self) -> QGroupBox:
        """Export physics + the size-skip safety net for huge models."""
        group = QGroupBox("Physics")
        form = QFormLayout()

        self.export_physics_check = QCheckBox("Export physics")
        self.export_physics_check.setChecked(True)
        self.export_physics_check.toggled.connect(self.update_preview)
        form.addRow("", self.export_physics_check)

        self.skip_large_physics_check = QCheckBox("Skip physics for very large models")
        self.skip_large_physics_check.setChecked(True)
        self.skip_large_physics_check.setToolTip(
            "When the render mesh's bounding box exceeds the threshold below, "
            "skip physics generation entirely (no $collisionmodel block in the QC).\n"
            "Source's vphysics can crash on enormous collision meshes — Citadels, "
            "skybox props, dropship hulls, etc. — so a no-collision compile is "
            "safer than a half-broken one. Static set-pieces rarely need physics anyway."
        )
        form.addRow("", self.skip_large_physics_check)

        self.large_threshold_spin = QDoubleSpinBox()
        self.large_threshold_spin.setRange(64.0, 100000.0)
        self.large_threshold_spin.setDecimals(0)
        self.large_threshold_spin.setSingleStep(64.0)
        self.large_threshold_spin.setValue(1024.0)
        self.large_threshold_spin.setSuffix(" u")
        self.large_threshold_spin.setToolTip(
            "Maximum bounding-box extent (in Source units / inches) before physics "
            "is skipped. 1024u (~85ft) is a conservative default; Citadels and other "
            "set-pieces sit well above this."
        )
        self.large_threshold_spin.setEnabled(self.skip_large_physics_check.isChecked())
        self.skip_large_physics_check.toggled.connect(self.large_threshold_spin.setEnabled)
        form.addRow("  Skip threshold:", self.large_threshold_spin)

        group.setLayout(form)
        return group

    def _build_advanced_group(self) -> QGroupBox:
        """UV / axis / preserve-folders / dry-run / verbose."""
        group = QGroupBox("Advanced")
        form = QFormLayout()

        self.flip_v_check = QCheckBox("Flip V (1 - tv)")
        self.flip_v_check.setChecked(False)
        self.flip_v_check.setToolTip("Flip vertical UV coordinate (rarely needed for Source)")
        form.addRow("", self.flip_v_check)

        self.axis_conversion_check = QCheckBox("Z-up to Source axis (-90° X)")
        self.axis_conversion_check.setChecked(True)
        self.axis_conversion_check.setToolTip("Convert Z-up (Blender) to Source engine coordinates")
        form.addRow("", self.axis_conversion_check)

        self.uv_mode_combo = QComboBox()
        self.uv_mode_combo.addItems([
            'Preserve UVs', 'Wrap to 0-1 (tiling)', 'Clamp to 0-1', 'Normalize (fit to 0-1)'
        ])
        self.uv_mode_combo.setCurrentIndex(0)
        self.uv_mode_combo.setToolTip(
            "Preserve: Keep original UV coordinates as-is (recommended)\n"
            "Wrap: Apply modulo to wrap UVs into 0-1 range\n"
            "Clamp: Clamp UVs to 0-1 range (may distort tiling)\n"
            "Normalize: Scale UVs to fit 0-1 based on min/max"
        )
        form.addRow("UV mode:", self.uv_mode_combo)

        # Run-time toggles in a 2x2 grid — same layout idea as Output.
        run_grid = QGridLayout()
        run_grid.setContentsMargins(0, 0, 0, 0)
        run_grid.setHorizontalSpacing(12)
        self.preserve_folders_check = QCheckBox("Preserve folders")
        self.preserve_folders_check.setChecked(True)
        self.preserve_folders_check.toggled.connect(self.update_preview)
        self.replace_existing_check = QCheckBox("Replace existing outputs")
        self.replace_existing_check.setChecked(True)
        self.replace_existing_check.setToolTip(
            "Overwrite outputs that already exist in the destination folder."
        )
        self.replace_existing_check.toggled.connect(self.update_preview)
        self.dry_run_check = QCheckBox("Dry run (no writes)")
        self.verbose_check = QCheckBox("Verbose logging")
        self.verbose_check.setChecked(False)
        run_grid.addWidget(self.preserve_folders_check, 0, 0)
        run_grid.addWidget(self.replace_existing_check, 0, 1)
        run_grid.addWidget(self.dry_run_check, 1, 0)
        run_grid.addWidget(self.verbose_check, 1, 1)
        form.addRow("Run mode:", self._wrap_layout(run_grid))

        group.setLayout(form)
        return group

    # ------------------------------------------------------------------
    # Right pane (preview + selection + run)
    # ------------------------------------------------------------------

    def _build_preview_pane(self) -> QWidget:
        pane = QWidget()
        col = QVBoxLayout(pane)
        col.setContentsMargins(6, 0, 0, 0)
        col.setSpacing(6)

        # Combined scan + selection row so the table stays as tall as possible.
        action_row = QHBoxLayout()
        self.rescan_btn = QPushButton("Scan")
        self.rescan_btn.clicked.connect(self.scan_models)
        action_row.addWidget(self.rescan_btn)
        self.recursive_scan_check = QCheckBox("Recursive (include subfolders)")
        self.recursive_scan_check.setChecked(True)
        self.recursive_scan_check.setToolTip(
            "When on, scan all subfolders of the input. When off, scan only "
            "the input folder itself."
        )
        self.recursive_scan_check.toggled.connect(self.scan_models)
        action_row.addWidget(self.recursive_scan_check)
        self.auto_rescan_check = QCheckBox("Auto-rescan")
        self.auto_rescan_check.toggled.connect(self.toggle_auto_rescan)
        action_row.addWidget(self.auto_rescan_check)

        action_row.addSpacing(16)
        action_row.addWidget(QLabel("All:"))
        self.select_all_btn = QPushButton("Check")
        self.select_all_btn.clicked.connect(lambda: self._set_all_selected(True))
        self.select_none_btn = QPushButton("Uncheck")
        self.select_none_btn.clicked.connect(lambda: self._set_all_selected(False))
        self.select_invert_btn = QPushButton("Invert")
        self.select_invert_btn.clicked.connect(self._invert_selection)
        for b in (self.select_all_btn, self.select_none_btn, self.select_invert_btn):
            action_row.addWidget(b)

        action_row.addSpacing(12)
        action_row.addWidget(QLabel("Selected:"))
        sel_tooltip = (
            "Click a row, then Shift+Click (range) or Ctrl+Click (toggle) more "
            "rows like in Explorer.\n"
            "These buttons toggle the Include checkbox for the highlighted rows "
            "only. Pressing Space while the table has focus does the same."
        )
        self.check_selected_btn = QPushButton("Check")
        self.check_selected_btn.setToolTip(sel_tooltip)
        self.check_selected_btn.clicked.connect(lambda: self._set_selected_rows_checked(True))
        self.uncheck_selected_btn = QPushButton("Uncheck")
        self.uncheck_selected_btn.setToolTip(sel_tooltip)
        self.uncheck_selected_btn.clicked.connect(lambda: self._set_selected_rows_checked(False))
        self.toggle_selected_btn = QPushButton("Toggle")
        self.toggle_selected_btn.setToolTip(sel_tooltip)
        self.toggle_selected_btn.clicked.connect(self._toggle_selected_rows)
        for b in (self.check_selected_btn, self.uncheck_selected_btn, self.toggle_selected_btn):
            action_row.addWidget(b)

        action_row.addStretch()
        col.addLayout(action_row)

        # Preview table — fills remaining vertical space.
        self.preview_table = QTableWidget()
        self.preview_table.setColumnCount(10)
        self.preview_table.setHorizontalHeaderLabels([
            "Include", "Name", "Physics", "Anim", "Load", "Modelname",
            "Mass", "Surfaceprop", "Status", "Warnings",
        ])
        self.preview_table.horizontalHeader().setStretchLastSection(True)
        self.preview_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.preview_table.setSelectionBehavior(QTableWidget.SelectRows)
        # Explorer-style multi-select: click → single, Shift+Click → range,
        # Ctrl+Click → toggle individual.
        self.preview_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.preview_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # Spacebar bulk-toggle for highlighted rows.
        self.preview_table.installEventFilter(self)
        col.addWidget(self.preview_table, 1)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        col.addWidget(self.progress_bar)

        # Run controls live in the right pane next to the table.
        run_row = QHBoxLayout()
        run_row.addStretch()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_batch)
        self.run_btn = QPushButton("Run Batch")
        self.run_btn.clicked.connect(self.run_batch)
        run_row.addWidget(self.cancel_btn)
        run_row.addWidget(self.run_btn)
        col.addLayout(run_row)

        return pane

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _wrap_layout(layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w

    def _on_export_animations_toggled(self, on: bool):
        """Grey out animation knobs when animations are disabled — the spin
        and the loop checkbox are no-ops without animation export."""
        self.animation_fps_spin.setEnabled(on)
        self.auto_loop_check.setEnabled(on)

    def _on_auto_mass_toggled(self, on: bool):
        """When auto mass is on, the modifier spin is live and the static
        spin is greyed; flip when static is selected. Keeps users from
        accidentally tweaking the inactive knob."""
        self.mass_modifier_spin.setEnabled(on)
        self.static_mass_spin.setEnabled(not on)

    def _on_auto_surf_toggled(self, on: bool):
        self.static_surf_edit.setEnabled(not on)

    # ------------------------------------------------------------------
    # Selected-rows helpers + Space key filter (Explorer-style)
    # ------------------------------------------------------------------

    def _highlighted_rows(self) -> List[int]:
        sel_model = self.preview_table.selectionModel()
        if sel_model is None:
            return []
        return sorted({idx.row() for idx in sel_model.selectedIndexes()})

    def _set_selected_rows_checked(self, checked: bool):
        state = Qt.Checked if checked else Qt.Unchecked
        for row in self._highlighted_rows():
            item = self.preview_table.item(row, 0)
            if item is not None:
                item.setCheckState(state)

    def _toggle_selected_rows(self):
        rows = self._highlighted_rows()
        if not rows:
            return
        checked_count = sum(
            1 for row in rows
            if self.preview_table.item(row, 0) is not None
            and self.preview_table.item(row, 0).checkState() == Qt.Checked
        )
        # Majority-wins toggle: predictable result for mixed selections.
        new_state = Qt.Unchecked if checked_count * 2 >= len(rows) else Qt.Checked
        for row in rows:
            item = self.preview_table.item(row, 0)
            if item is not None:
                item.setCheckState(new_state)

    def eventFilter(self, obj, event):
        """Intercept Space on the preview table to bulk-toggle highlighted rows."""
        if obj is self.preview_table and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Space, Qt.Key_Select):
                self._toggle_selected_rows()
                return True
        return super().eventFilter(obj, event)

    def _row_widget(self, *widgets) -> QWidget:
        """Create horizontal row of widgets."""
        w = QWidget()
        layout = QHBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        for widget in widgets:
            layout.addWidget(widget)
        return w

    def browse_input(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Input Folder")
        if folder:
            self.input_edit.setText(folder)
            self.check_input_output_same()
            if self.auto_rescan_enabled:
                self.scan_models()

    def browse_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if folder:
            self.output_edit.setText(folder)
            self.check_input_output_same()
            if self.auto_rescan_enabled:
                self.update_preview()

    def check_input_output_same(self):
        """Check if input and output folders are the same and show warning."""
        input_path = self.input_edit.text().strip()
        output_path = self.output_edit.text().strip()
        if input_path and output_path:
            try:
                same = Path(input_path).resolve() == Path(output_path).resolve()
                self.input_output_warning.setVisible(same)
            except Exception:
                self.input_output_warning.setVisible(False)
        else:
            self.input_output_warning.setVisible(False)

    def toggle_auto_rescan(self, enabled: bool):
        self.auto_rescan_enabled = enabled

    def scan_models(self):
        """Scan input folder for model sets."""
        input_path = self.input_edit.text().strip()
        if not input_path:
            self.log("Input folder required", "WARNING")
            return

        input_root = Path(input_path)
        if not input_root.exists():
            self.log(f"Input folder not found: {input_path}", "ERROR")
            return

        recursive = self.recursive_scan_check.isChecked()
        self.log(f"Scanning ({'recursive' if recursive else 'top-level only'})...", "INFO")
        scanner = ModelSetScanner(input_root, recursive=recursive)
        self.model_sets = scanner.find_sets()
        self.log(f"Found {len(self.model_sets)} model sets", "SUCCESS")
        self.update_preview()

    def update_preview(self):
        """Update preview table with resolved values."""
        if not self.model_sets:
            self.preview_table.setRowCount(0)
            return

        output_path = self.output_edit.text().strip()
        if not output_path:
            self.preview_table.setRowCount(0)
            return

        output_root = Path(output_path)
        input_root = Path(self.input_edit.text().strip())

        self.preview_models = []
        
        for model_set in self.model_sets:
            # Calculate output dir
            if self.preserve_folders_check.isChecked():
                try:
                    rel = model_set.base_dir.relative_to(input_root)
                    out_dir = output_root / rel
                except ValueError:
                    out_dir = output_root
            else:
                out_dir = output_root

            # Resolve modelname (per-asset with prefix)
            prefix = self.modelname_edit.text().strip()
            if not prefix:
                try:
                    rel = out_dir.relative_to(output_root)
                    if str(rel) != ".":
                        modelname = f"{str(rel).replace(chr(92), '/')}/{model_set.name}.mdl"
                    else:
                        modelname = f"{model_set.name}.mdl"
                except ValueError:
                    modelname = f"{model_set.name}.mdl"
            else:
                prefix = prefix.replace("\\", "/")
                if not prefix.endswith("/"):
                    prefix += "/"
                modelname = f"{prefix}{model_set.name}.mdl"

            # Preflight check for load status
            load_status = "Pending"
            failure_reason = ""
            preflight_ok, missing = GltfMeshLoader.preflight_check(model_set.render_path)
            if not preflight_ok:
                load_status = "Failed"
                failure_reason = f"Missing: {', '.join(missing)}"
                model_set.warnings.append(failure_reason)
            else:
                load_status = "Ok"

            # Animation summary (cheap JSON-only peek; no buffer load)
            if self.export_animations_check.isChecked():
                skin_count, anim_count = peek_animations(model_set.render_path)
                if skin_count > 0 and anim_count > 0:
                    anim_summary = f"{anim_count} clip(s)"
                else:
                    anim_summary = "static"
            else:
                anim_summary = "static"

            # Surfaceprop
            if self.auto_surf_radio.isChecked():
                surfaceprop = SurfacepropDetector.detect(model_set.name)
            else:
                surfaceprop = self.static_surf_edit.text().strip()

            # Mass
            if self.auto_mass_radio.isChecked():
                density = MeshProcessor.get_surfaceprop_density(surfaceprop)
                mass = 10.0 * density * self.mass_modifier_spin.value()  # Preview estimate
            else:
                mass = self.static_mass_spin.value()

            # Check existing
            render_smd = out_dir / f"{model_set.name}.smd"
            physics_smd = out_dir / f"{model_set.name}_physics.smd" if model_set.physics_path else None
            qc_path = out_dir / f"{model_set.name}.qc" if self.generate_qc_check.isChecked() else None

            will_overwrite = False
            will_skip = False
            if not self.replace_existing_check.isChecked():
                exists = render_smd.exists()
                if model_set.physics_path and self.export_physics_check.isChecked():
                    exists = exists or physics_smd.exists()
                if qc_path:
                    exists = exists or qc_path.exists()
                if exists:
                    will_skip = True
            else:
                if render_smd.exists():
                    will_overwrite = True

            preview = PreviewModel(
                name=model_set.name,
                render_smd_path=render_smd,
                physics_smd_path=physics_smd,
                qc_path=qc_path,
                modelname=modelname,
                cdmaterials=self.cdmaterials_edit.text().strip(),
                concave=self.concave_check.isChecked(),
                mass=mass,
                surfaceprop=surfaceprop,
                has_physics=model_set.physics_path is not None,
                will_overwrite=will_overwrite,
                will_skip=will_skip,
                warnings=model_set.warnings,
                load_status=load_status,
                failure_reason=failure_reason,
                anim_summary=anim_summary,
            )
            self.preview_models.append(preview)

        # Populate table
        # Preserve existing user selections across re-previews so that toggling
        # an option (e.g. axis_conversion) doesn't clobber the user's manual
        # picks. Keyed by render_path which is the stable per-model identity.
        prior_selection = {}
        for row in range(self.preview_table.rowCount()):
            include_item = self.preview_table.item(row, 0)
            if include_item is None:
                continue
            key = include_item.data(Qt.UserRole)
            if key:
                prior_selection[key] = include_item.checkState() == Qt.Checked

        self.preview_table.setRowCount(len(self.preview_models))
        for row, pm in enumerate(self.preview_models):
            # Include checkbox — failed loads default to off since the runner
            # will skip them anyway and unchecked makes the bulk-select state
            # easier to reason about.
            ms = self.model_sets[row]
            include_item = QTableWidgetItem()
            include_item.setFlags(
                (include_item.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable
            )
            key = str(ms.render_path)
            if key in prior_selection:
                include_default = prior_selection[key]
            else:
                include_default = pm.load_status != "Failed"
            include_item.setCheckState(Qt.Checked if include_default else Qt.Unchecked)
            include_item.setData(Qt.UserRole, key)
            include_item.setToolTip("Tick to include this model in the batch run")
            self.preview_table.setItem(row, 0, include_item)

            # Name
            name_item = QTableWidgetItem(pm.name)
            name_item.setToolTip(str(pm.render_smd_path))
            self.preview_table.setItem(row, 1, name_item)

            # Physics
            phys_item = QTableWidgetItem("Yes" if pm.has_physics else "No")
            if pm.physics_smd_path:
                phys_item.setToolTip(str(pm.physics_smd_path))
            self.preview_table.setItem(row, 2, phys_item)

            # Anim
            anim_item = QTableWidgetItem(pm.anim_summary)
            if pm.anim_summary != "static":
                anim_item.setForeground(QColor("blue"))
            self.preview_table.setItem(row, 3, anim_item)

            # Load status
            load_item = QTableWidgetItem(pm.load_status)
            if pm.load_status == "Failed":
                load_item.setForeground(QColor("red"))
                load_item.setToolTip(pm.failure_reason)
            elif pm.load_status == "Ok":
                load_item.setForeground(QColor("green"))
            self.preview_table.setItem(row, 4, load_item)

            # Modelname
            self.preview_table.setItem(row, 5, QTableWidgetItem(pm.modelname))

            # Mass
            self.preview_table.setItem(row, 6, QTableWidgetItem(f"{pm.mass:.2f}"))

            # Surfaceprop
            self.preview_table.setItem(row, 7, QTableWidgetItem(pm.surfaceprop))

            # Status (Overwrite/Skip/New)
            status = "Overwrite" if pm.will_overwrite else ("Skip" if pm.will_skip else "New")
            status_item = QTableWidgetItem(status)
            if pm.will_skip:
                status_item.setForeground(QColor("orange"))
            elif pm.will_overwrite:
                status_item.setForeground(QColor("red"))
            self.preview_table.setItem(row, 8, status_item)

            # Warnings
            warnings_text = ""
            if pm.failure_reason:
                warnings_text = pm.failure_reason
            elif pm.warnings:
                warnings_text = ", ".join(pm.warnings)
            warnings_item = QTableWidgetItem(warnings_text)
            if warnings_text:
                warnings_item.setForeground(QColor("orange"))
            self.preview_table.setItem(row, 9, warnings_item)

        self.preview_table.resizeColumnsToContents()

    def _set_all_selected(self, checked: bool):
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self.preview_table.rowCount()):
            item = self.preview_table.item(row, 0)
            if item is not None:
                item.setCheckState(state)

    def _invert_selection(self):
        for row in range(self.preview_table.rowCount()):
            item = self.preview_table.item(row, 0)
            if item is None:
                continue
            item.setCheckState(
                Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked
            )

    def _selected_model_sets(self) -> List[ModelSet]:
        """Return ``model_sets`` filtered to rows whose Include box is ticked."""
        chosen: List[ModelSet] = []
        for row in range(self.preview_table.rowCount()):
            item = self.preview_table.item(row, 0)
            if item is None or item.checkState() != Qt.Checked:
                continue
            if 0 <= row < len(self.model_sets):
                chosen.append(self.model_sets[row])
        return chosen

    def run_batch(self):
        """Start batch conversion."""
        input_path = self.input_edit.text().strip()
        output_path = self.output_edit.text().strip()

        if not input_path or not output_path:
            self.log("Input and output folders required", "ERROR")
            return

        if not self.model_sets:
            self.log("No models to convert. Click Rescan.", "WARNING")
            return

        selected_sets = self._selected_model_sets()
        if not selected_sets:
            self.log("No models selected. Tick at least one Include checkbox.", "WARNING")
            return
        if len(selected_sets) < len(self.model_sets):
            self.log(
                f"Running on {len(selected_sets)} of {len(self.model_sets)} models "
                f"(selected via Include checkboxes)",
                "INFO",
            )

        # Add to recent runs
        self.add_recent_run(input_path, output_path)

        input_root = Path(input_path)
        output_root = Path(output_path)

        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, len(selected_sets))
        self.progress_bar.setValue(0)

        # Map UI combo selection to uv_mode parameter
        uv_mode_map = {
            0: 'preserve',
            1: 'wrap',
            2: 'clamp',
            3: 'normalize'
        }
        uv_mode = uv_mode_map.get(self.uv_mode_combo.currentIndex(), 'preserve')

        self.thread = BatchRunner(
            model_sets=selected_sets,
            input_root=input_root,
            output_root=output_root,
            scale=self.scale_spin.value(),
            weld_distance=self.weld_spin.value(),
            flip_v=self.flip_v_check.isChecked(),
            export_physics=self.export_physics_check.isChecked(),
            preserve_folders=self.preserve_folders_check.isChecked(),
            generate_smd=self.generate_smd_check.isChecked(),
            generate_qc=self.generate_qc_check.isChecked(),
            modelname_template=self.modelname_edit.text().strip(),
            cdmaterials=self.cdmaterials_edit.text().strip(),
            concave=self.concave_check.isChecked(),
            auto_mass=self.auto_mass_radio.isChecked(),
            mass_modifier=self.mass_modifier_spin.value(),
            static_mass=self.static_mass_spin.value(),
            auto_surfaceprop=self.auto_surf_radio.isChecked(),
            static_surfaceprop=self.static_surf_edit.text().strip(),
            replace_existing=self.replace_existing_check.isChecked(),
            dry_run=self.dry_run_check.isChecked(),
            verbose=self.verbose_check.isChecked(),
            axis_conversion=self.axis_conversion_check.isChecked(),
            uv_mode=uv_mode,
            export_animations=self.export_animations_check.isChecked(),
            animation_fps=self.animation_fps_spin.value(),
            auto_loop_detect=self.auto_loop_check.isChecked(),
            skip_physics_for_large=self.skip_large_physics_check.isChecked(),
            large_model_threshold=self.large_threshold_spin.value(),
        )
        self.thread.progress.connect(self.on_progress)
        self.thread.finished.connect(self.on_finished)
        self.thread.start()

    def on_progress(self, current: int, total: int, message: str):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(current)
        self.log(message, "INFO")

    def on_finished(self, success: bool, message: str, stats: dict):
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.progress_bar.setVisible(False)
        
        log_type = "SUCCESS" if success else "WARNING"
        self.log(message, log_type)
        
        # Log errors
        if 'error_list' in stats:
            for error in stats['error_list']:
                self.log(error, "ERROR")

    def cancel_batch(self):
        if self.thread and self.thread.isRunning():
            self.thread.requestInterruption()
            self.log("Cancelling...", "INFO")
    def load_recent_runs(self):
        """Load recent runs from JSON file."""
        try:
            if self.recent_runs_file.exists():
                with open(self.recent_runs_file, 'r') as f:
                    self.recent_runs = json.load(f)
                    # Keep only last 10 runs
                    self.recent_runs = self.recent_runs[-10:]
        except Exception as e:
            self.recent_runs = []
            print(f"Failed to load recent runs: {e}")

    def save_recent_runs(self):
        """Save recent runs to JSON file."""
        try:
            with open(self.recent_runs_file, 'w') as f:
                json.dump(self.recent_runs, f, indent=2)
        except Exception as e:
            print(f"Failed to save recent runs: {e}")

    def add_recent_run(self, input_path: str, output_path: str):
        """Add a run to recent runs history."""
        if not input_path or not output_path:
            return
        
        # Create entry with all settings
        entry = {
            'input': input_path,
            'output': output_path,
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            # Processing options
            'scale': self.scale_spin.value(),
            'weld_distance': self.weld_spin.value(),
            # Output options
            'generate_smd': self.generate_smd_check.isChecked(),
            'generate_qc': self.generate_qc_check.isChecked(),
            # QC options
            'modelname': self.modelname_edit.text(),
            'cdmaterials': self.cdmaterials_edit.text(),
            'concave': self.concave_check.isChecked(),
            'auto_mass': self.auto_mass_radio.isChecked(),
            'mass_modifier': self.mass_modifier_spin.value(),
            'static_mass': self.static_mass_spin.value(),
            'auto_surfaceprop': self.auto_surf_radio.isChecked(),
            'static_surfaceprop': self.static_surf_edit.text(),
            # Advanced options
            'flip_v': self.flip_v_check.isChecked(),
            'axis_conversion': self.axis_conversion_check.isChecked(),
            'export_physics': self.export_physics_check.isChecked(),
            'skip_physics_for_large': self.skip_large_physics_check.isChecked(),
            'large_model_threshold': self.large_threshold_spin.value(),
            'preserve_folders': self.preserve_folders_check.isChecked(),
            'replace_existing': self.replace_existing_check.isChecked(),
            # Animation options
            'export_animations': self.export_animations_check.isChecked(),
            'animation_fps': self.animation_fps_spin.value(),
            'auto_loop_detect': self.auto_loop_check.isChecked(),
            # Scan options
            'recursive_scan': self.recursive_scan_check.isChecked(),
        }
        
        # Remove duplicates (same input/output combo)
        self.recent_runs = [r for r in self.recent_runs 
                           if not (r.get('input') == input_path and r.get('output') == output_path)]
        
        # Add to end (most recent)
        self.recent_runs.append(entry)
        
        # Keep only last 10
        self.recent_runs = self.recent_runs[-10:]
        
        # Save and refresh UI
        self.save_recent_runs()
        self.populate_recent_runs()

    def populate_recent_runs(self):
        """Populate the recent runs dropdown."""
        self.recent_combo.blockSignals(True)
        self.recent_combo.clear()
        self.recent_combo.addItem("-- Select a recent run --")
        
        # Add in reverse order (most recent first)
        for run in reversed(self.recent_runs):
            input_name = Path(run['input']).name if run.get('input') else '?'
            output_name = Path(run['output']).name if run.get('output') else '?'
            timestamp = run.get('timestamp', '')
            label = f"{input_name} → {output_name} ({timestamp})"
            self.recent_combo.addItem(label, run)
        
        self.recent_combo.blockSignals(False)

    def on_recent_run_selected(self, index: int):
        """Handle recent run selection."""
        if index <= 0:  # Skip placeholder item
            return
        
        run = self.recent_combo.itemData(index)
        if run:
            # Restore paths
            input_path = run.get('input', '')
            output_path = run.get('output', '')
            
            if input_path:
                self.input_edit.setText(input_path)
            if output_path:
                self.output_edit.setText(output_path)
            
            # Restore processing options
            if 'scale' in run:
                self.scale_spin.setValue(run['scale'])
            if 'weld_distance' in run:
                self.weld_spin.setValue(run['weld_distance'])
            
            # Restore output options
            if 'generate_smd' in run:
                self.generate_smd_check.setChecked(run['generate_smd'])
            if 'generate_qc' in run:
                self.generate_qc_check.setChecked(run['generate_qc'])
            
            # Restore QC options
            if 'modelname' in run:
                self.modelname_edit.setText(run['modelname'])
            if 'cdmaterials' in run:
                self.cdmaterials_edit.setText(run['cdmaterials'])
            if 'concave' in run:
                self.concave_check.setChecked(run['concave'])
            if 'auto_mass' in run:
                self.auto_mass_radio.setChecked(run['auto_mass'])
                self.static_mass_radio.setChecked(not run['auto_mass'])
            if 'mass_modifier' in run:
                self.mass_modifier_spin.setValue(run['mass_modifier'])
            if 'static_mass' in run:
                self.static_mass_spin.setValue(run['static_mass'])
            if 'auto_surfaceprop' in run:
                self.auto_surf_radio.setChecked(run['auto_surfaceprop'])
                self.static_surf_radio.setChecked(not run['auto_surfaceprop'])
            if 'static_surfaceprop' in run:
                self.static_surf_edit.setText(run['static_surfaceprop'])
            
            # Restore advanced options
            if 'flip_v' in run:
                self.flip_v_check.setChecked(run['flip_v'])
            if 'axis_conversion' in run:
                self.axis_conversion_check.setChecked(run['axis_conversion'])
            if 'export_physics' in run:
                self.export_physics_check.setChecked(run['export_physics'])
            if 'skip_physics_for_large' in run:
                self.skip_large_physics_check.setChecked(bool(run['skip_physics_for_large']))
            if 'large_model_threshold' in run:
                self.large_threshold_spin.setValue(float(run['large_model_threshold']))
            if 'preserve_folders' in run:
                self.preserve_folders_check.setChecked(run['preserve_folders'])
            if 'replace_existing' in run:
                self.replace_existing_check.setChecked(run['replace_existing'])
            if 'export_animations' in run:
                self.export_animations_check.setChecked(run['export_animations'])
            if 'animation_fps' in run:
                self.animation_fps_spin.setValue(run['animation_fps'])
            if 'auto_loop_detect' in run:
                self.auto_loop_check.setChecked(run['auto_loop_detect'])
            if 'recursive_scan' in run:
                self.recursive_scan_check.setChecked(run['recursive_scan'])

            # Trigger rescan
            self.scan_models()
            self.log(f"Loaded recent run: {Path(input_path).name} → {Path(output_path).name}", "INFO")
