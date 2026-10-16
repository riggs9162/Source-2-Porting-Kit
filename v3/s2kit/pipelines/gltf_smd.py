"""
glTF/GLB -> Source 1 SMD + QC.

The second stage of the port: takes what VRF Batch Export wrote into
`modelsrc/` and produces the SMD meshes, animation SMDs and QC file that
studiomdl compiles.

Ported from the v2 tool (`v2/app/tools/gltf_smd_batch_tool.py`). The scanner,
loader, mesh processor and QC writer are carried over unchanged - they encode
a lot of hard-won Source 2 Viewer knowledge (native-unit detection, the
orphaned root matrix, stub physics glTFs, duplicate `@`-prefixed clips). What
is new is :func:`convert_model_set`: the per-model orchestration that used to
live inside a QThread is now a plain function taking `on_log` and a cancel
event, so it runs in parallel from a tool page, from a CLI, or from a test.

Framework-free: no Flet, no Qt.
"""

from __future__ import annotations

import json
import os
import re
import struct
import threading
import time
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import trimesh
from trimesh.resolvers import FilePathResolver

from s2kit.pipelines.gltf_animation import (
    SOURCE2_BASIS, SOURCE2_BASIS_QUAT, SOURCE_UNITS_PER_METRE, SPACE_METRES,
    SPACE_SOURCE, GltfClip, GltfSkin, load_buffer_bytes, parse_clips, parse_skin,
    parse_skin_vertex_data, peek_animations, position_space, sample_clip,
)
from s2kit.pipelines.smd_animation_export import (
    CoordinateMode, SmdAnimationExporter, SmdSkeletalExporter,
    compute_root_bind_pyr, derive_definebone_lines, is_loop_clip,
    sanitize_clip_filename,
)
from s2kit.pipelines.physics import (
    DEFAULT_MAX_CONVEX_PIECES, MAX_MASS_KG, MIN_SOLID_MASS_KG,
    THICKEN_TARGET_IN, CollisionModel,
    automass, check_collision_model, collisionmodel_block, convex_piece_budget,
    convex_pieces, decompose_concave, resolve_surfaceprop, slab_open_shells,
    surfaceprop_density, thicken_thin_pieces, weld_vertices,
)
from s2kit.pipelines.smd_export import SmdExporter

LogFn = Callable[[str], None]

#: studiomdl floors every compiled solid at 1kg, so asking for less is a no-op.
MIN_MASS_KG = MIN_SOLID_MASS_KG


class ConversionCancelled(RuntimeError):
    """Raised inside a conversion when the caller set the cancel event."""


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
class SkinDef:
    """One row of a Source 1 `$texturegroup`: a named skin variant whose
    materials replace the default skin's materials column-for-column.
    Loaded from the `<model>.skins.json` sidecar VRF Batch Export writes
    when a Source 2 model has multiple `m_materialGroups`.
    """
    name: str
    materials: List[str]


@dataclass
class ModelSet:
    """Source glTF model set with optional physics companion."""
    name: str
    base_dir: Path
    render_path: Path
    physics_path: Optional[Path] = None
    warnings: List[str] = field(default_factory=list)
    skins: List[SkinDef] = field(default_factory=list)


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

                skins = ModelSetScanner._load_skin_sidecar(render_path)

                sets.append(ModelSet(
                    name=name,
                    base_dir=base_path,
                    render_path=render_path,
                    physics_path=physics_path,
                    warnings=warnings,
                    skins=skins,
                ))

        return sets

    @staticmethod
    def _load_skin_sidecar(render_path: Path) -> List[SkinDef]:
        """Load `<render_stem>.skins.json` if it exists. The sidecar is
        written by VRF Batch Export when a Source 2 model has multiple
        `m_materialGroups`. Silent no-op if missing or malformed — skins
        are an optional enrichment, not a hard dependency.
        """
        sidecar = render_path.with_suffix(".skins.json")
        if not sidecar.is_file():
            return []
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(payload, dict):
            return []
        out: List[SkinDef] = []
        for entry in payload.get("skins", []):
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            mats = entry.get("materials")
            if not isinstance(name, str) or not isinstance(mats, list):
                continue
            mat_list = [m for m in mats if isinstance(m, str)]
            if mat_list:
                out.append(SkinDef(name=name, materials=mat_list))
        return out


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

    # GLB binary container magic ("glTF") and JSON chunk type ("JSON").
    _GLB_MAGIC = 0x46546C67
    _GLB_CHUNK_JSON = 0x4E4F534A

    @staticmethod
    def _read_glb_json(path: Path) -> Optional[dict]:
        """Extract the JSON chunk from a binary .glb container.

        Mirrors the JSON that ``json.load`` returns for a .gltf file so the
        Source 2 Viewer detection and material/skin metadata paths work
        identically for both containers. Returns None on any malformed read —
        callers treat glTF JSON as best-effort enrichment, not a hard
        dependency. The embedded BIN chunk is not decoded here (animation
        export of GLB remains unsupported; see load_buffer_bytes).
        """
        try:
            with open(path, 'rb') as f:
                magic, _ver, _length = struct.unpack('<III', f.read(12))
                if magic != GltfMeshLoader._GLB_MAGIC:
                    return None
                chunk_len, chunk_type = struct.unpack('<II', f.read(8))
                if chunk_type != GltfMeshLoader._GLB_CHUNK_JSON:
                    return None
                return json.loads(f.read(chunk_len))
        except Exception:
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
            # Load glTF JSON to extract material names, UV transforms, and the
            # Source 2 Viewer signature (best-effort). Both containers must be
            # parsed: .glb embeds the same JSON as a binary chunk, and without
            # it the S2V native-units path never fires — which silently scales
            # skinned .glb exports (ragdolls) ~40× too large, since trimesh's
            # dump() doesn't bake the inches->meters root matrix for skinned
            # meshes the way it does for static ones.
            gltf_data = None
            if path.suffix.lower() == '.glb':
                gltf_data = GltfMeshLoader._read_glb_json(path)
            else:
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

            # A Source 2 Viewer export that still carries its conversion matrix
            # bypasses Scene.dump()'s transform baking, so we work with the raw
            # POSITION values (vmdl-space, inches). Everything else dumps as
            # usual: legitimate node placement transforms — a Blender export, or
            # the near-identity matrix Source 2 Viewer 20 leaves behind once it
            # has baked the conversion itself — still have to be applied.
            space = position_space(gltf_data) if gltf_data else ""
            is_s2v = space == SPACE_SOURCE

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

            # Mark which space the vertices ended up in so the worker knows what
            # to undo: nothing at all for raw vmdl-space inches, or Source 2
            # Viewer's own metre/Y-up conversion for a v20 render mesh.
            if is_s2v:
                mesh.metadata['source2_native_units'] = True
            elif space == SPACE_METRES:
                mesh.metadata['source2_metre_units'] = True

            # Surface parsed JSON + base dir + source path so the animation
            # pipeline can decode the buffer without reparsing the file —
            # external .bin via base_dir, or the embedded BIN chunk via the
            # .glb source path.
            if gltf_data is not None:
                mesh.metadata['gltf_data'] = gltf_data
                mesh.metadata['gltf_base_dir'] = path.parent
                mesh.metadata['gltf_source_path'] = path

            return (mesh, "")

        except Exception as e:
            error_msg = str(e)
            if verbose:
                error_msg += f"\n{traceback.format_exc()}"
            return (None, error_msg)


class MeshProcessor:
    """Apply transformations and cleanup to meshes."""

    @staticmethod
    def apply_scale(mesh: trimesh.Trimesh, scale: float):
        """Apply uniform scale (bakes into vertices)."""
        mesh.apply_scale(scale)

    @staticmethod
    def apply_source2_axis_conversion(mesh: trimesh.Trimesh):
        """Undo Source 2 Viewer's own basis swap - see :data:`SOURCE2_BASIS`.

        Only for vertices it already baked into glTF convention. A file that
        still carries the conversion matrix needs no rotation at all, and a
        Blender export needs `apply_axis_conversion` instead.
        """
        mesh.apply_transform(SOURCE2_BASIS)

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
                weld_vertices(mesh, weld_distance)
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
    def measure_collision(mesh: trimesh.Trimesh,
                          weld_distance: float = 0.0) -> CollisionModel:
        """Measure a physics mesh the way studiomdl's decomposition will.

        Welding matters here: VRF's physics glTF has three unshared vertices
        per triangle, so without it every triangle is its own connected
        component and the model reads as thousands of degenerate pieces.
        """
        if mesh is None:
            return CollisionModel()
        return convex_pieces(mesh, weld_distance=max(weld_distance, 1e-4))

    @staticmethod
    def calculate_surface_mass(model: CollisionModel,
                               surfaceprop: Optional[str],
                               mass_modifier: float) -> Optional[float]:
        """`$mass` for a measured collision model, by studiomdl's `$automass`
        rule (see `pipelines.physics.automass`), scaled by the user's modifier.
        """
        if model is None or not model.pieces:
            return None
        mass = automass(model, surfaceprop) * (mass_modifier or 1.0)
        return float(min(max(mass, MIN_MASS_KG), MAX_MASS_KG))


def declares_collision(path: Optional[Any]) -> bool:
    """Whether a physics glTF actually carries geometry.

    Source 2 Viewer writes a stub - no nodes, no meshes, no accessors - for a
    model with no collision data, which is a statement rather than a failure:
    Half-Life Alyx does not make that model solid either. A file that cannot be
    read at all is a different thing and answers True, because the absence of
    collision then is ours rather than the source's.
    """
    if path is None:
        return False
    path = Path(path)
    if not path.is_file():
        return False
    if path.suffix.lower() == ".glb":
        data = GltfMeshLoader._read_glb_json(path)
    else:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
    if not isinstance(data, dict):
        return True
    return bool(data.get("meshes"))


def declared_surfaceprops(gltf_data: dict) -> Dict[str, int]:
    """The Source 2 surface properties a glTF declares, and how much each covers.

    Source 2 Viewer groups a model's collision by surface property, names each
    node `physics_group_<property>` and repeats the property in that node's
    `extras`. It is the model's own answer to a question the name heuristics can
    only guess at - `world.metal_combine`, rather than whatever words happen to
    be in the filename.

    The weight is the vertex count of the group's mesh, read out of the
    accessor headers rather than the buffer. A model whose collision is mostly
    metal with one pane of glass in it therefore resolves to metal: Source 1
    carries a single `$surfaceprop` per model, so something has to win, and
    coverage is a better rule than declaration order.
    """
    weights: Dict[str, int] = {}
    meshes = gltf_data.get("meshes") or []
    accessors = gltf_data.get("accessors") or []
    for node in gltf_data.get("nodes") or []:
        value = (node.get("extras") or {}).get("SurfaceProperty")
        if not isinstance(value, str) or not value.strip():
            continue
        vertices = 0
        index = node.get("mesh")
        if isinstance(index, int) and 0 <= index < len(meshes):
            for primitive in meshes[index].get("primitives") or []:
                position = (primitive.get("attributes") or {}).get("POSITION")
                if isinstance(position, int) and 0 <= position < len(accessors):
                    vertices += int(accessors[position].get("count") or 0)
        key = value.strip()
        # A group with no mesh still counts for something - it declared itself.
        weights[key] = weights.get(key, 0) + max(vertices, 1)
    return weights


def gltf_surfaceprops(path: Optional[Any]) -> Dict[str, int]:
    """:func:`declared_surfaceprops` for a glTF on disk; empty when it has none."""
    if path is None:
        return {}
    path = Path(path)
    if not path.is_file():
        return {}
    if path.suffix.lower() == ".glb":
        data = GltfMeshLoader._read_glb_json(path)
    else:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
    return declared_surfaceprops(data) if isinstance(data, dict) else {}


def surfaceprop_override_for(name: str, rules: str) -> Optional[str]:
    """The `$surfaceprop` forced on `name`, or None when no rule applies.

    A bare value forces itself on every model. A `pattern=prop` entry forces one
    only where the model name matches - a case-insensitive substring, or a glob
    when the pattern carries `*` or `?` - so a folder can keep the automatic
    choice everywhere the guess is right and be corrected where it is not. The
    detector reads model names for keywords, and a name is a poor witness: a
    doorframe called `..._windowed_wrapped` lands on `glass`, which is a 0.1in
    shell material and changes the compiled `$mass` as well as the footsteps.

    Entries are separated by newlines or commas; the first matching pattern
    wins, and a bare value is the fallback rather than a competitor.
    """
    subject = name.lower()
    fallback: Optional[str] = None
    for line in (rules or "").splitlines():
        for entry in line.split(","):
            entry = entry.strip()
            if not entry:
                continue
            if "=" not in entry:
                # A bare value carrying a wildcard is a rule someone forgot to
                # finish, not a surfaceprop - no Source 1 material has a `*` in
                # its name, and writing one into the QC helps nobody.
                if fallback is None and not any(ch in entry for ch in "*?"):
                    fallback = entry
                continue
            pattern, _, value = entry.partition("=")
            pattern, value = pattern.strip().lower(), value.strip()
            if not pattern or not value:
                continue
            matched = (fnmatch(subject, pattern)
                       if any(ch in pattern for ch in "*?")
                       else pattern in subject)
            if matched:
                return value
    return fallback


class SurfacepropDetector:
    """Pick a Source 1 surfaceprop for a model.

    A real Source 2 surfaceprop (from the model's PHYS block) always beats a
    guess, so `detect` takes one and only falls back to name keywords when it
    is absent. The choice is load-bearing beyond footstep sounds: it sets the
    density *and* whether the mass is computed as a solid or as a hollow shell.

    Ordering is deliberate - the more specific materials are tested first, so
    "metal grate" lands on `metalgrate` (a 0.5in shell) rather than `metal`.
    """

    HEURISTICS = {
        'metalgrate': ['grate', 'grating', 'catwalk'],
        'metalvent': ['vent', 'duct', 'ventilation'],
        'metalpanel': ['panel', 'sheet', 'plating', 'siding'],
        'chainlink': ['chainlink', 'chain_link', 'fence', 'wiremesh'],
        'metal': ['metal', 'pipe', 'iron', 'steel', 'aluminum', 'aluminium',
                  'girder', 'beam', 'rebar'],
        'wood_crate': ['crate'],
        'wood_furniture': ['chair', 'table', 'desk', 'shelf', 'cabinet',
                           'furniture'],
        'wood_panel': ['plywood', 'board', 'plank'],
        'wood': ['wood', 'timber', 'log', 'barrel'],
        'glassbottle': ['bottle'],
        'glass': ['glass', 'window', 'mirror'],
        'porcelain': ['porcelain', 'sink', 'toilet', 'ceramic'],
        'tile': ['tile'],
        'concrete': ['concrete', 'cement', 'curb', 'sidewalk'],
        'brick': ['brick'],
        'rock': ['rock', 'stone', 'boulder'],
        'plaster': ['plaster', 'drywall', 'stucco'],
        'cardboard': ['cardboard', 'carton'],
        'paper': ['paper', 'poster', 'newspaper'],
        'plastic': ['plastic', 'polymer', 'vinyl'],
        'carpet': ['carpet', 'rug', 'mat'],
        'foliage': ['foliage', 'plant', 'leaf', 'tree', 'bush', 'xen', 'flora'],
        'dirt': ['dirt', 'soil', 'ground', 'mud'],
        'sand': ['sand'],
        'computer': ['computer', 'monitor', 'console', 'terminal', 'keyboard'],
    }

    @classmethod
    def detect(cls, name: str, material_name: Optional[str] = None,
               source2_prop: Optional[str] = None) -> str:
        """Source 1 surfaceprop for a model.

        `source2_prop` is the raw name out of the Source 2 physics data; when
        it maps to something real it wins outright.
        """
        if source2_prop:
            resolved = resolve_surfaceprop(source2_prop)
            if resolved != 'default':
                return resolved

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
        piece_count: int = 1,
        remove2d: bool = False,
        inertia: Optional[float] = None,
        damping: Optional[float] = None,
        rotdamping: Optional[float] = None,
        is_animated: bool = False,
        skin: Optional[GltfSkin] = None,
        coord: Optional[CoordinateMode] = None,
        animations: Optional[List[AnimMeta]] = None,
        material_skins: Optional[List[SkinDef]] = None,
        vert_count: Optional[int] = None,
    ):
        """Write QC file with optional skeletal-animation block."""
        lines = []
        lines.append(f'$modelname "{QcWriter._normalize_path(modelname)}"')

        if cdmaterials:
            lines.append(f'$cdmaterials "{QcWriter._normalize_path(cdmaterials)}"')

        # Raise MAXSTUDIOVERTS (65536) only when the model actually exceeds it:
        # StudioMDL CE honors $maxverts, while older studiomdl builds reject
        # the unknown command — models under the limit must stay compatible.
        if vert_count is not None and vert_count > 65536:
            lines.append(f'$maxverts {vert_count}')

        if surfaceprop:
            lines.append(f'$surfaceprop "{surfaceprop}"')

        if material_skins and len(material_skins) > 1:
            lines.append('')
            lines.append('$texturegroup "skinfamilies"')
            lines.append('{')
            for s in material_skins:
                row = ' '.join(f'"{m}"' for m in s.materials)
                lines.append(f'    {{ {row} }} // {s.name}')
            lines.append('}')

        if skin is not None and coord is not None:
            # Skinned model — drop $staticprop and declare every bone with
            # $definebone so studiomdl preserves the full hierarchy (it strips
            # bones that have no weighted vertices / animation otherwise).
            # This branch runs whether or not the model has animation clips.
            lines.append(f'$body "body" "{name}.smd"')
            lines.append('')
            lines.extend(derive_definebone_lines(skin, coord))
            lines.append('')
            root_name = skin.joints[skin.root_joint_idx].name
            lines.append(f'$root "{root_name}"')
            lines.append('')

            if animations:
                for am in animations:
                    lines.append(
                        f'$animation "a_{am.clip_name}" "{am.smd_filename}" fps {am.fps:g}'
                    )
                lines.append('')

                for am in animations:
                    opts = [f'fps {am.fps:g}']
                    if am.loop:
                        opts.append('loop')
                    opts_str = ' '.join(opts)
                    lines.append(
                        f'$sequence "{am.clip_name}" {{ "a_{am.clip_name}" {opts_str} }}'
                    )
                lines.append('')
            else:
                # No animation clips, but studiomdl still needs at least one
                # sequence. A 1-frame idle over the reference SMD's bind pose
                # gives the (definebone-pinned) skeleton a home without
                # inventing motion.
                lines.append(f'$sequence "idle" "{name}.smd" fps 1')
                lines.append('')
        else:
            lines.append('$staticprop')
            lines.append(f'$body "body" "{name}.smd"')
            lines.append(f'$sequence "idle" "{name}.smd" fps 1')

        if has_physics:
            lines.extend(collisionmodel_block(
                f"{name}_physics.smd",
                mass=mass,
                concave=concave,
                piece_count=piece_count,
                max_convex_pieces=convex_piece_budget(piece_count),
                remove2d=remove2d,
                inertia=inertia,
                damping=damping,
                rotdamping=rotdamping,
            ))

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


# ============================================================================
# Batch Runner
# ============================================================================


# ============================================================================
# Conversion (was BatchRunner, a QThread)
# ============================================================================

PHYSICS_MATERIAL = "physics_group_prop.wood_crate_material"


@dataclass
class ConversionOptions:
    """Everything one batch run needs. Mirrors the tool's parameter schema."""

    # geometry
    scale: float = 40.0
    weld_distance: float = 0.0001
    axis_conversion: bool = True
    force_manual_scale: bool = False
    flip_v: bool = False
    uv_mode: str = "preserve"

    # what to write
    generate_smd: bool = True
    generate_qc: bool = True
    export_physics: bool = True
    export_animations: bool = True
    animation_fps: float = 30.0
    auto_loop_detect: bool = True

    # QC
    modelname_prefix: str = ""
    cdmaterials: str = "models/"
    concave: bool = True
    auto_mass: bool = True
    mass_modifier: float = 1.0
    static_mass: float = 10.0
    auto_surfaceprop: bool = True
    static_surfaceprop: str = "metal"
    #: `$remove2d` drops any convex piece under half an inch thick. Valve never
    #: uses it on a static prop, and on a ported model it silently deletes the
    #: collision for thin panels, signs and grates - so it is opt-in, and the
    #: default instead reports which pieces would have gone.
    remove_2d_collision: bool = False

    #: Forces `$surfaceprop`, over both the detector and the fixed value.
    #: Empty leaves the choice alone; see `surfaceprop_override_for`.
    surfaceprop_override: str = ""

    #: Build collision from the render mesh for a model the source gives none.
    #: Source 2 leaves those models non-solid - they are decoration there - but
    #: a prop dropped into a Hammer map usually has to hold a player up, and
    #: clipping a floor by hand for every one of them is worse than a hull that
    #: follows it. Off gives the source's answer instead: no `$collisionmodel`.
    collision_from_render: bool = True

    #: Cut concave collision into pieces studiomdl's per-element hulls can
    #: hold. Source 2 collision is often one concave triangle soup, which
    #: Source 1 has no shape for - see `physics.decompose_concave`.
    decompose_collision: bool = True
    #: Piece budget for that. studiomdl's own default is 40, and staying inside
    #: it is what keeps `$maxconvexpieces` and the compiler's cost warning off.
    max_convex_pieces: int = DEFAULT_MAX_CONVEX_PIECES

    # physics guards
    skip_physics_for_large: bool = True
    large_model_threshold: float = 1024.0

    # layout + safety
    preserve_folders: bool = True
    replace_existing: bool = True
    dry_run: bool = False
    verbose: bool = False


@dataclass
class ConversionResult:
    """What happened to one model set."""

    name: str
    status: str = "converted"      # converted | skipped | failed | dry-run
    reason: str = ""
    output_dir: Optional[Path] = None
    files: List[Path] = field(default_factory=list)
    bones: int = 0
    clips: int = 0
    triangles: int = 0
    vertices: int = 0
    mass: Optional[float] = None
    surfaceprop: str = ""
    has_physics: bool = False
    physics_fallback: bool = False
    skins: int = 0
    elapsed: float = 0.0
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("converted", "dry-run")


def resolve_output_dir(model_set: "ModelSet", input_root: Path, output_root: Path,
                       preserve_folders: bool) -> Path:
    """Where this set's SMDs land: mirrored subtree, or flat under the root."""
    if not preserve_folders:
        return output_root
    try:
        return output_root / model_set.base_dir.relative_to(input_root)
    except ValueError:
        return output_root


def resolve_modelname(name: str, out_dir: Path, output_root: Path,
                      prefix: str = "") -> str:
    """`$modelname` for the QC: an explicit prefix, else the output subtree."""
    prefix = (prefix or "").strip().replace("\\", "/")
    if prefix:
        if not prefix.endswith("/"):
            prefix += "/"
        return f"{prefix}{name}.mdl"
    try:
        relative = out_dir.relative_to(output_root)
    except ValueError:
        return f"{name}.mdl"
    text = str(relative).replace("\\", "/")
    return f"{name}.mdl" if text == "." else f"{text}/{name}.mdl"


def existing_outputs(model_set: "ModelSet", out_dir: Path,
                     options: ConversionOptions) -> List[Path]:
    """Outputs already on disk that a run would overwrite."""
    found: List[Path] = []
    render = out_dir / f"{model_set.name}.smd"
    if render.exists():
        found.append(render)
    if model_set.physics_path is not None and options.export_physics:
        physics = out_dir / f"{model_set.name}_physics.smd"
        if physics.exists():
            found.append(physics)
    if options.generate_qc:
        qc = out_dir / f"{model_set.name}.qc"
        if qc.exists():
            found.append(qc)
    return found


def smooth_normals_by_position(mesh: trimesh.Trimesh,
                               tolerance: float = 1e-5) -> Optional[np.ndarray]:
    """Per-vertex normals averaged across every face touching that *position*.

    Source reads vertex normals to decide how to decompose a collision mesh
    into convex pieces, and physics glTFs arrive pre-split with hard edges, so
    the normals have to be smoothed across the split seams - not merely across
    connected faces, because the pieces are disconnected.

    Vectorised rewrite of the v2 loop (a Python dict of spatial buckets plus a
    per-vertex face list); same result in one numpy pass. Each
    (position group, face) pair contributes exactly once, matching the old
    set-based accumulation.
    """
    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces)
    if len(vertices) == 0 or len(faces) == 0:
        return None
    try:
        face_normals = np.asarray(mesh.face_normals, dtype=np.float64)
    except Exception:
        return None

    # Bucket vertices by rounded position, then map every vertex to its group.
    keys = np.round(vertices / tolerance).astype(np.int64)
    _unique, inverse = np.unique(keys, axis=0, return_inverse=True)
    inverse = np.asarray(inverse).ravel()
    group_count = int(inverse.max()) + 1

    # One row per (group, face) incidence, deduped so a face touching a group
    # through two vertices is still counted once.
    incident = np.stack(
        [inverse[faces.ravel()], np.repeat(np.arange(len(faces)), 3)], axis=1
    )
    incident = np.unique(incident, axis=0)

    accumulated = np.zeros((group_count, 3), dtype=np.float64)
    np.add.at(accumulated, incident[:, 0], face_normals[incident[:, 1]])

    lengths = np.linalg.norm(accumulated, axis=1, keepdims=True)
    normals = np.divide(accumulated, lengths, out=np.zeros_like(accumulated),
                        where=lengths > 1e-12)
    return normals[inverse]


def _check(cancel: Optional[threading.Event]) -> None:
    if cancel is not None and cancel.is_set():
        raise ConversionCancelled()


def _failed(result: "ConversionResult", reason: str,
            started: float) -> "ConversionResult":
    result.status = "failed"
    result.reason = reason
    result.elapsed = time.time() - started
    return result


def convert_model_set(model_set: "ModelSet", options: ConversionOptions, *,
                      input_root: Path, output_root: Path,
                      on_log: LogFn = print,
                      cancel: Optional[threading.Event] = None
                      ) -> ConversionResult:
    """Convert one glTF model set to SMD(s) + QC.

    Asset problems come back as a failed :class:`ConversionResult` rather than
    an exception, so a batch keeps going; only cancellation propagates.
    """
    started = time.time()
    name = model_set.name
    result = ConversionResult(name=name, surfaceprop=options.static_surfaceprop,
                              skins=len(model_set.skins))
    out_dir = resolve_output_dir(model_set, input_root, output_root,
                                 options.preserve_folders)
    result.output_dir = out_dir
    result.has_physics = model_set.physics_path is not None

    def log(message: str) -> None:
        on_log(f"{name}: {message}")

    def verbose(message: str) -> None:
        if options.verbose:
            log(message)

    _check(cancel)

    if not options.replace_existing:
        clashes = existing_outputs(model_set, out_dir, options)
        if clashes:
            result.status = "skipped"
            result.reason = f"{len(clashes)} output(s) already exist"
            result.elapsed = time.time() - started
            return result

    if options.dry_run:
        result.status = "dry-run"
        result.reason = f"would write to {out_dir}"
        result.elapsed = time.time() - started
        return result

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _failed(result, f"could not create {out_dir}: {exc}", started)

    # -- load the render mesh ------------------------------------------
    ok, missing = GltfMeshLoader.preflight_check(model_set.render_path)
    if not ok:
        return _failed(result, f"missing dependencies: {', '.join(missing)}",
                       started)

    render_mesh, load_error = GltfMeshLoader.load_mesh(model_set.render_path,
                                                       options.verbose)
    if render_mesh is None:
        return _failed(result,
                       f"could not load {model_set.render_path.name}: {load_error}",
                       started)
    _check(cancel)

    # Source 2 Viewer's conversion turns inches into metres AND swaps the vmdl
    # Z-up basis to glTF Y-up. Where it lives decides what is left to undo: on a
    # node the geometry never saw it and needs neither (`native_units`), baked
    # into the vertices it needs both, in its own basis rather than the +90
    # degrees about X a Blender export wants (`metre_units`).
    # force_manual_scale overrides only the scale.
    native_units = bool(render_mesh.metadata.get("source2_native_units"))
    metre_units = bool(render_mesh.metadata.get("source2_metre_units"))
    if native_units:
        render_scale = options.scale if options.force_manual_scale else 1.0
        log(f"Source-units glTF detected, using scale={render_scale:g}"
            + (" (manual override)" if options.force_manual_scale else ""))
    elif metre_units:
        render_scale = (options.scale if options.force_manual_scale
                        else SOURCE_UNITS_PER_METRE)
        log(f"Source 2 Viewer metre glTF detected, using scale={render_scale:g}"
            + (" (manual override)" if options.force_manual_scale else ""))
    else:
        render_scale = options.scale

    # -- skin + clips ---------------------------------------------------
    skin: Optional[GltfSkin] = None
    clips: List[GltfClip] = []
    vertex_joints = vertex_weights = None
    gltf_data = render_mesh.metadata.get("gltf_data")
    gltf_base_dir = render_mesh.metadata.get("gltf_base_dir")
    gltf_src_path = render_mesh.metadata.get("gltf_source_path")
    if gltf_data is not None and gltf_base_dir is not None and gltf_data.get("skins"):
        buffer = load_buffer_bytes(gltf_data, gltf_base_dir, gltf_src_path)
        if buffer is not None:
            skin = parse_skin(gltf_data, buffer)
            if skin is not None:
                vertex_joints, vertex_weights = parse_skin_vertex_data(gltf_data,
                                                                       buffer)
                if options.export_animations and gltf_data.get("animations"):
                    clips = parse_clips(gltf_data, buffer, skin)

    has_skin = bool(skin and vertex_joints is not None and vertex_weights is not None)
    is_animated = bool(has_skin and clips)
    result.bones = len(skin.joints) if has_skin and skin else 0

    # -- process --------------------------------------------------------
    MeshProcessor.apply_scale(render_mesh, render_scale)
    apply_swap = options.axis_conversion and not native_units
    if apply_swap:
        if metre_units:
            MeshProcessor.apply_source2_axis_conversion(render_mesh)
        else:
            MeshProcessor.apply_axis_conversion(render_mesh)
    if not has_skin:
        # sanitize() drops unreferenced vertices, which reorders the array and
        # would desynchronise JOINTS_0/WEIGHTS_0 on a skinned mesh.
        MeshProcessor.sanitize(render_mesh)

    result.triangles = int(len(render_mesh.faces))
    result.vertices = int(len(render_mesh.vertices))
    coord = CoordinateMode(scale=render_scale, swap_axes=apply_swap,
                           swap_quat=SOURCE2_BASIS_QUAT if metre_units else None)
    animations: List[AnimMeta] = []
    _check(cancel)

    # -- render SMD ------------------------------------------------------
    render_smd = out_dir / f"{name}.smd"
    if options.generate_smd:
        if has_skin:
            success, warning = SmdSkeletalExporter.write_skinned(
                render_mesh, skin, vertex_joints, vertex_weights,
                render_smd, name, coord, flip_v=options.flip_v,
                uv_mode=options.uv_mode,
            )
            if not success:
                return _failed(result, f"skinned SMD failed: {warning}", started)
            if warning:
                result.warnings.append(warning)
                verbose(warning)
            result.files.append(render_smd)
            log(f"skinned reference SMD, {result.bones} bones"
                + ("" if is_animated else ", no clips"))
            animations = _write_animations(model_set, skin, clips, out_dir,
                                           coord, options, result, log, cancel)
        else:
            success, warning = SmdExporter.write_static(
                render_mesh, render_smd, name, options.flip_v, False,
                uv_mode=options.uv_mode)
            if not success:
                return _failed(result, "static SMD failed", started)
            if warning:
                result.warnings.append(warning)
                verbose(warning)
            result.files.append(render_smd)
    result.clips = len(animations)

    # -- physics ---------------------------------------------------------
    physics_mesh = _build_physics_mesh(model_set, render_mesh, options, result,
                                       log, verbose, cancel)
    if physics_mesh is not None and options.generate_smd:
        physics_smd = out_dir / f"{name}_physics.smd"
        # A skinned model's root bone carries a non-identity bind; the physics
        # root must mirror it or studiomdl's inv(phys_root)*main_root rotates
        # the collision hull off-axis.
        root_bind = compute_root_bind_pyr(skin, coord) if (has_skin and skin) else None
        success, warning = SmdExporter.write_static(
            physics_mesh, physics_smd, PHYSICS_MATERIAL, options.flip_v, False,
            smooth_normals_by_position(physics_mesh),
            uv_mode=options.uv_mode, root_bind=root_bind,
        )
        if success:
            result.files.append(physics_smd)
            if warning:
                verbose(f"(physics) {warning}")
        else:
            physics_mesh = None
            result.warnings.append("physics SMD failed")
            log("physics SMD failed")
    _check(cancel)

    # -- QC ---------------------------------------------------------------
    forced = surfaceprop_override_for(name, options.surfaceprop_override)
    if forced:
        surfaceprop = forced
        verbose(f"$surfaceprop forced to '{surfaceprop}'")
        if resolve_surfaceprop(surfaceprop) == "default"                 and surfaceprop.lower() != "default":
            message = (f"'{surfaceprop}' is not a surfaceprop this kit knows, "
                       f"so $mass falls back to the default density")
            result.warnings.append(message)
            log(message)
    elif options.auto_surfaceprop:
        declared = gltf_surfaceprops(model_set.physics_path)
        source_prop = max(declared, key=declared.get) if declared else None
        surfaceprop = SurfacepropDetector.detect(name, source2_prop=source_prop)
        if source_prop and resolve_surfaceprop(source_prop) != "default":
            verbose(f"$surfaceprop '{surfaceprop}' from the source's "
                    f"'{source_prop}'")
            if len(declared) > 1:
                message = (f"collision declares {len(declared)} surface "
                           f"properties ({', '.join(declared)}); Source 1 takes "
                           f"one per model, so '{source_prop}' won on coverage")
                result.warnings.append(message)
                log(message)
        elif source_prop and source_prop.lower() != "default":
            # Worth seeing: it is how the alias table finds out what it is
            # missing, and the fallback below is a guess by comparison.
            verbose(f"the source's '{source_prop}' has no Source 1 equivalent; "
                    f"reading the name instead")
    else:
        surfaceprop = options.static_surfaceprop
    result.surfaceprop = surfaceprop

    # Measure the collision mesh the way studiomdl will decompose it - the
    # piece count drives $concave/$maxconvexpieces, the volume/area drive the
    # mass, and the same measurement predicts the compiler's warnings.
    collision = (MeshProcessor.measure_collision(physics_mesh,
                                                 options.weld_distance)
                 if physics_mesh is not None else CollisionModel())

    mass: Optional[float] = None
    if physics_mesh is not None and options.auto_mass:
        mass = MeshProcessor.calculate_surface_mass(collision, surfaceprop,
                                                    options.mass_modifier)
    elif not options.auto_mass:
        mass = options.static_mass
    result.mass = mass

    if physics_mesh is not None:
        density, thickness = surfaceprop_density(surfaceprop)
        verbose(f"collision: {collision.piece_count} convex piece(s), "
                f"volume {collision.volume:.1f}in3, area {collision.area:.1f}in2, "
                f"surfaceprop '{surfaceprop}' "
                f"({density:g}kg/m3"
                + (f", {thickness:g}in shell)" if thickness else ", solid)")
                + (f" -> $mass {mass:.2f}" if mass else ""))
        for warning in check_collision_model(collision, mass or 0.0):
            result.warnings.append(warning)
            log(f"collision: {warning}")

    if options.generate_qc:
        qc_path = out_dir / f"{name}.qc"
        QcWriter.write_qc(
            qc_path, name,
            resolve_modelname(name, out_dir, output_root, options.modelname_prefix),
            options.cdmaterials, surfaceprop, physics_mesh is not None,
            options.concave, mass,
            volume=(collision.volume if physics_mesh is not None else None),
            piece_count=max(collision.piece_count, 1),
            remove2d=options.remove_2d_collision,
            is_animated=is_animated,
            skin=skin if has_skin else None,
            coord=coord if has_skin else None,
            animations=animations if has_skin else None,
            material_skins=model_set.skins or None,
            vert_count=result.vertices,
        )
        result.files.append(qc_path)

    result.elapsed = time.time() - started
    return result


def _write_animations(model_set: "ModelSet", skin: "GltfSkin",
                      clips: List["GltfClip"], out_dir: Path,
                      coord: "CoordinateMode", options: ConversionOptions,
                      result: ConversionResult, log: LogFn,
                      cancel: Optional[threading.Event]) -> List[AnimMeta]:
    """One SMD per clip, deduped by sanitised name."""
    animations: List[AnimMeta] = []
    seen: set = set()
    for clip in clips:
        _check(cancel)
        safe = sanitize_clip_filename(clip.name)
        if safe in seen:
            # S2V exports some clips twice, once under an "@"-prefixed alias
            # that sanitises to the same filename. Writing both would emit
            # duplicate $animation entries, which studiomdl rejects.
            log(f"skipping duplicate clip {clip.name!r} (resolves to {safe!r})")
            continue
        seen.add(safe)

        anim_smd = out_dir / f"{model_set.name}_anim_{safe}.smd"
        frames, frame_count = sample_clip(clip, skin, fps=options.animation_fps)
        ok, error = SmdAnimationExporter.write_animation(skin, frames, anim_smd,
                                                         coord)
        if not ok:
            result.warnings.append(f"animation {clip.name!r}: {error}")
            log(f"animation {clip.name!r} failed: {error}")
            continue
        result.files.append(anim_smd)
        animations.append(AnimMeta(
            clip_name=safe,
            smd_filename=anim_smd.name,
            num_frames=frame_count,
            fps=options.animation_fps,
            loop=is_loop_clip(clip.name) if options.auto_loop_detect else False,
        ))
    if animations:
        log(f"wrote {len(animations)} animation SMD(s)")
    return animations


#: Above this the greedy search costs more than the collision is worth - a
#: render mesh standing in for missing physics data runs to tens of thousands
#: of faces, and every candidate cut clips all of them.
MAX_DECOMPOSE_FACES = 6000


def _repair_collision(mesh: trimesh.Trimesh, options: ConversionOptions,
                      result: ConversionResult, log: LogFn,
                      verbose: LogFn) -> trimesh.Trimesh:
    """Make every element of a collision mesh something studiomdl can build.

    Two things stop it, both of them coming from the same place - a Source 2
    `m_meshes` shape is a *surface*, and Source 1 only has solids:

    * an open frame, whose hull fills the opening it describes and which no
      convex decomposition reopens (`physics.slab_open_shells`);
    * a piece too thin to make a solid of, one of which makes studiomdl hull
      the entire model into a single piece (`physics.thicken_thin_pieces`).
    """
    weld = max(options.weld_distance, 1e-4)
    shells = slab_open_shells(mesh, weld_distance=weld)
    if shells.changed:
        message = (f"rebuilt {shells.replaced} hollow open surface(s) as "
                   f"{shells.slabs} slab(s) - hulling one fills the opening it "
                   f"describes, and no cut reopens it")
        result.warnings.append(message)
        log(f"collision: {message}")
        mesh = shells.mesh

    repair = thicken_thin_pieces(mesh, weld_distance=weld)
    if not repair.changed:
        return mesh
    parts = []
    if repair.thickened:
        parts.append(f"thickened {repair.thickened} collision piece(s) to "
                     f"{THICKEN_TARGET_IN:g}in "
                     f"(thinnest was {repair.thinnest:.3f}in)")
    if repair.dropped:
        parts.append(f"dropped {repair.dropped} with no shape to thicken")
    message = ("; ".join(parts)
               + " - one piece under half an inch makes studiomdl hull the "
                 "whole model into a single convex piece")
    result.warnings.append(message)
    log(f"collision: {message}")
    return repair.mesh


def _decompose_collision(mesh: trimesh.Trimesh, options: ConversionOptions,
                         result: ConversionResult, log: LogFn, verbose: LogFn,
                         cancel: Optional[threading.Event]) -> trimesh.Trimesh:
    """Cut a concave collision mesh into pieces studiomdl can represent.

    studiomdl hulls each connected element of the collision SMD, so a Source 2
    `m_meshes` soup - a doorframe, a grate, a cable run - compiles as a solid
    block with its openings filled in. Nothing in the compile reports that, so
    it has to be dealt with before the SMD is written.
    """
    if not options.decompose_collision or mesh is None:
        return mesh
    if len(mesh.faces) > MAX_DECOMPOSE_FACES:
        verbose(f"collision decomposition skipped: {len(mesh.faces)} faces")
        return mesh

    split = decompose_concave(mesh,
                              max_pieces=max(1, int(options.max_convex_pieces)),
                              weld_distance=max(options.weld_distance, 1e-4),
                              cancel=cancel)
    if not split.changed:
        return mesh

    log(f"collision: cut {split.pieces_before} concave piece(s) into "
        f"{split.pieces_after}, {split.recovered * 100:.0f}% of the hulled-over "
        f"volume given back")
    if split.unsplit:
        message = (f"{split.unsplit} collision piece(s) still concave at the "
                   f"{options.max_convex_pieces}-piece budget")
        result.warnings.append(message)
        log(f"collision: {message}")
    return split.mesh


def _build_physics_mesh(model_set: "ModelSet", render_mesh: trimesh.Trimesh,
                        options: ConversionOptions, result: ConversionResult,
                        log: LogFn, verbose: LogFn,
                        cancel: Optional[threading.Event] = None
                        ) -> Optional[trimesh.Trimesh]:
    """The collision mesh: the physics glTF, or the render mesh as a fallback.

    None means no `$collisionmodel` at all - physics disabled, or the model is
    too large to give vphysics a hull safely.
    """
    if not options.export_physics:
        return None

    if options.skip_physics_for_large:
        try:
            extent = float(np.max(render_mesh.bounds[1] - render_mesh.bounds[0]))
        except Exception:
            extent = 0.0
        if extent > options.large_model_threshold:
            # vphysics can crash the engine on collision meshes this big
            # (Citadel, skyboxes, drop-ships), and huge static set-pieces are
            # better off with no collision anyway.
            message = (f"skipping physics - bbox {extent:.0f}u exceeds the "
                       f"{options.large_model_threshold:.0f}u threshold")
            result.warnings.append(message)
            log(message)
            return None

    loaded = None
    failure = ""
    if model_set.physics_path is not None:
        ok, missing = GltfMeshLoader.preflight_check(model_set.physics_path)
        if not ok:
            failure = f"missing dependencies: {', '.join(missing)}"
        else:
            loaded, error = GltfMeshLoader.load_mesh(model_set.physics_path,
                                                     options.verbose)
            if loaded is None:
                failure = error or "empty physics glTF"

    if loaded is not None:
        native = bool(loaded.metadata.get("source2_native_units"))
        metres = bool(loaded.metadata.get("source2_metre_units"))
        if native and not options.force_manual_scale:
            physics_scale = 1.0
        elif metres and not options.force_manual_scale:
            physics_scale = SOURCE_UNITS_PER_METRE
        else:
            physics_scale = options.scale
        MeshProcessor.apply_scale(loaded, physics_scale)
        if options.axis_conversion and not native:
            if metres:
                MeshProcessor.apply_source2_axis_conversion(loaded)
            else:
                MeshProcessor.apply_axis_conversion(loaded)
        verbose(f"physics mesh: {len(loaded.faces)} faces")
        # Thickening comes last: a cut can leave a sliver of its own, so the
        # pass that guarantees every piece is a solid has to see the pieces
        # that will actually be written.
        loaded = _decompose_collision(loaded, options, result, log, verbose,
                                      cancel)
        return _repair_collision(loaded, options, result, log, verbose)

    # A stub physics glTF is not a failure to read one - it is Source 2 Viewer
    # writing out that the model has no collision, which is how Half-Life Alyx
    # ships its decoration. `collision_from_render` decides what to do about
    # that: invent collision from the render mesh, which is what a prop placed
    # in a Hammer map generally needs, or leave the prop non-solid the way the
    # game it came from has it.
    if not (declares_collision(model_set.physics_path)
            or options.collision_from_render):
        message = ("no collision in the source, so none written - the prop "
                   "is not solid, which is what it is in Source 2 too")
        result.warnings.append(message)
        log(message)
        return None

    fallback = render_mesh.copy()
    if getattr(fallback, "metadata", None):
        # Render materials must not leak into the physics SMD.
        fallback.metadata.pop("gltf_face_materials", None)
        fallback.metadata.pop("gltf_material_names", None)
    reason = failure or "no collision in the source"
    result.physics_fallback = True
    result.warnings.append(f"collision built from the render mesh ({reason})")
    log(f"{reason}; building collision from the render mesh instead")
    # Invented collision earns the same passes a real one gets. Without the
    # decomposition a render mesh is hulled per connected component, and a
    # shell's hull is the box around it - which is the whole complaint about
    # inventing collision in the first place.
    fallback = _decompose_collision(fallback, options, result, log, verbose,
                                    cancel)
    return _repair_collision(fallback, options, result, log, verbose)
