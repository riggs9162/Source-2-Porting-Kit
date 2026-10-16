"""
Mesh loading and software rendering for the model viewer.

Flet has no 3D surface, so previews are rasterised here with numpy and handed
to the UI as PNG bytes. That turns out to be the right trade for asset
inspection: it needs no GPU context, works over the web renderer, and renders
the formats this kit actually moves around.

Loaders:
    .smd            native parser (also reports bones and materials)
    .obj            native fallback parser
    everything else trimesh, when the optional dependency is installed

Renderer: back-face-culled z-buffer triangle rasteriser with flat Lambert
shading, plus normal, depth and wireframe modes.
"""

from __future__ import annotations

import io
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

SHADING_MODES = ("shaded", "wireframe", "normals", "depth")

NATIVE_EXTENSIONS = {".smd", ".obj"}
TRIMESH_EXTENSIONS = {".glb", ".gltf", ".stl", ".ply", ".dae", ".obj", ".off", ".3mf"}


class MeshLoadError(RuntimeError):
    """Raised when a mesh cannot be loaded (with a user-facing reason)."""


@dataclass
class MeshData:
    """A triangle soup plus whatever the source format told us about it."""

    name: str
    vertices: Any                      # (N, 3) float32
    faces: Any                         # (M, 3) int32
    materials: list[str] = field(default_factory=list)
    bones: list[str] = field(default_factory=list)
    uvs: Optional[Any] = None
    source_faces: int = 0               # before any preview decimation
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def vertex_count(self) -> int:
        return int(self.vertices.shape[0]) if self.vertices is not None else 0

    @property
    def face_count(self) -> int:
        return int(self.faces.shape[0]) if self.faces is not None else 0

    def bounds(self) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        if self.vertex_count == 0:
            return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
        low = self.vertices.min(axis=0)
        high = self.vertices.max(axis=0)
        return tuple(float(v) for v in low), tuple(float(v) for v in high)

    def size(self) -> tuple[float, float, float]:
        low, high = self.bounds()
        return tuple(round(h - l, 3) for l, h in zip(low, high))

    def rows(self) -> list[tuple[str, str]]:
        """Label/value pairs for the model info panel."""
        width, depth, height = self.size()
        low, high = self.bounds()
        rows = [
            ("Vertices", f"{self.vertex_count:,}"),
            ("Triangles", f"{self.face_count:,}"),
        ]
        if self.source_faces and self.source_faces != self.face_count:
            rows.append(("Source triangles", f"{self.source_faces:,} (decimated "
                                             "for preview)"))
        rows.extend([
            ("Size (X/Y/Z)", f"{width} x {depth} x {height} units"),
            ("Bounds min", " ".join(f"{v:.2f}" for v in low)),
            ("Bounds max", " ".join(f"{v:.2f}" for v in high)),
        ])
        if self.materials:
            rows.append(("Materials", f"{len(self.materials)}: "
                                      + ", ".join(self.materials[:6])
                                      + (" ..." if len(self.materials) > 6 else "")))
        if self.bones:
            rows.append(("Bones", f"{len(self.bones)}: "
                                  + ", ".join(self.bones[:4])
                                  + (" ..." if len(self.bones) > 4 else "")))
        rows.extend((key, str(value)) for key, value in self.extra.items())
        return rows


def _numpy():
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - guarded by the bootstrapper
        raise MeshLoadError("numpy is required to load meshes") from exc
    return np


# ----------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------

def load_mesh(path: Any, *, max_faces: int = 60_000) -> MeshData:
    """Load any supported mesh, decimating to `max_faces` for preview use."""
    target = Path(path)
    if not target.is_file():
        raise MeshLoadError(f"File not found: {target}")

    suffix = target.suffix.lower()
    if suffix == ".smd":
        mesh = load_smd(target)
    elif suffix == ".obj":
        mesh = _load_trimesh(target) if _trimesh_available() else load_obj(target)
    elif suffix in TRIMESH_EXTENSIONS:
        mesh = _load_trimesh(target)
    elif suffix in (".mdl", ".vmdl", ".vmdl_c", ".dmx", ".fbx"):
        raise MeshLoadError(
            f"{suffix} is a compiled/proprietary format - decompile or export it "
            "to SMD, OBJ or GLB first"
        )
    else:
        raise MeshLoadError(f"No mesh loader for {suffix or 'this file'}")

    return _decimate(mesh, max_faces)


def load_smd(path: Path) -> MeshData:
    """Parse a Studiomdl Data file: nodes, then the triangles block."""
    np = _numpy()
    bones: list[str] = []
    materials: list[str] = []
    positions: list[tuple[float, float, float]] = []
    uvs: list[tuple[float, float]] = []
    faces: list[tuple[int, int, int]] = []

    section = ""
    pending: list[int] = []
    frame_count = 0

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise MeshLoadError(f"Could not read {path.name}: {exc}") from exc

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()

        if lowered in ("nodes", "skeleton", "triangles", "vertexanimation"):
            section = lowered
            pending = []
            continue
        if lowered == "end":
            section = ""
            continue
        if lowered.startswith("version"):
            continue

        if section == "nodes":
            match = re.match(r'^\s*(-?\d+)\s+"([^"]*)"\s+(-?\d+)', line)
            if match:
                bones.append(match.group(2))
            continue

        if section == "skeleton":
            if lowered.startswith("time"):
                frame_count += 1
            continue

        if section == "triangles":
            parts = line.split()
            # A material name line has no leading numeric bone index.
            if not _is_number(parts[0]):
                material = line.strip()
                if material not in materials:
                    materials.append(material)
                continue
            if len(parts) < 9:
                continue
            try:
                x, y, z = (float(parts[1]), float(parts[2]), float(parts[3]))
                u, v = (float(parts[7]), float(parts[8]))
            except ValueError:
                continue
            positions.append((x, y, z))
            uvs.append((u, v))
            pending.append(len(positions) - 1)
            if len(pending) == 3:
                faces.append((pending[0], pending[1], pending[2]))
                pending = []

    if not faces:
        raise MeshLoadError(f"{path.name} contains no triangles")

    mesh = MeshData(
        name=path.name,
        vertices=np.asarray(positions, dtype="float32"),
        faces=np.asarray(faces, dtype="int32"),
        materials=materials,
        bones=bones,
        uvs=np.asarray(uvs, dtype="float32"),
        extra={"Loader": "native SMD"},
    )
    if frame_count:
        mesh.extra["Skeleton frames"] = frame_count
    return mesh


def load_obj(path: Path) -> MeshData:
    """Minimal OBJ reader, used when trimesh is not installed."""
    np = _numpy()
    positions: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    materials: list[str] = []

    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        raise MeshLoadError(f"Could not read {path.name}: {exc}") from exc

    with handle:
        for line in handle:
            if line.startswith("v "):
                parts = line.split()
                if len(parts) >= 4:
                    positions.append((float(parts[1]), float(parts[2]),
                                      float(parts[3])))
            elif line.startswith("f "):
                indices = []
                for token in line.split()[1:]:
                    raw = token.split("/")[0]
                    if not raw:
                        continue
                    index = int(raw)
                    indices.append(index - 1 if index > 0 else len(positions) + index)
                # Fan-triangulate any n-gon.
                for offset in range(1, len(indices) - 1):
                    faces.append((indices[0], indices[offset], indices[offset + 1]))
            elif line.startswith("usemtl "):
                material = line.split(maxsplit=1)[1].strip()
                if material not in materials:
                    materials.append(material)

    if not faces:
        raise MeshLoadError(f"{path.name} contains no faces")
    return MeshData(
        name=path.name,
        vertices=np.asarray(positions, dtype="float32"),
        faces=np.asarray(faces, dtype="int32"),
        materials=materials,
        extra={"Loader": "native OBJ"},
    )


def _trimesh_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("trimesh") is not None


def _load_trimesh(path: Path) -> MeshData:
    np = _numpy()
    try:
        import trimesh
    except ImportError as exc:
        raise MeshLoadError(
            f"Loading {path.suffix.upper()} needs the optional 'trimesh' package"
        ) from exc

    try:
        loaded = trimesh.load(str(path), force="mesh", process=False)
    except Exception as exc:
        raise MeshLoadError(f"trimesh could not load the file: {exc}") from exc

    if not hasattr(loaded, "faces") or len(getattr(loaded, "faces", [])) == 0:
        raise MeshLoadError(f"{path.name} has no triangles trimesh could read")

    materials: list[str] = []
    visual = getattr(loaded, "visual", None)
    material = getattr(visual, "material", None)
    if material is not None:
        materials.append(str(getattr(material, "name", "") or "material"))

    extra: dict[str, Any] = {"Loader": "trimesh"}
    try:
        extra["Watertight"] = "yes" if loaded.is_watertight else "no"
    except Exception:
        pass

    return MeshData(
        name=path.name,
        vertices=np.asarray(loaded.vertices, dtype="float32"),
        faces=np.asarray(loaded.faces, dtype="int32"),
        materials=[m for m in materials if m],
        extra=extra,
    )


def _decimate(mesh: MeshData, max_faces: int) -> MeshData:
    """Uniformly drop triangles when a mesh exceeds the preview budget."""
    if max_faces <= 0 or mesh.face_count <= max_faces:
        mesh.source_faces = mesh.face_count
        return mesh
    np = _numpy()
    stride = math.ceil(mesh.face_count / max_faces)
    kept = np.arange(0, mesh.face_count, stride)
    mesh.source_faces = mesh.face_count
    mesh.faces = mesh.faces[kept]
    return mesh


# ----------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------

@dataclass
class Camera:
    """Orbit camera state driven by the viewer's sliders."""

    yaw: float = 35.0        # degrees around the up axis
    pitch: float = 20.0      # degrees above the horizon
    zoom: float = 1.0        # 1.0 fits the bounding sphere
    up_axis: str = "z"       # SMD/Source models are Z-up, GLB is usually Y-up


def render(mesh: MeshData, *, size: int = 512, camera: Optional[Camera] = None,
           shading: str = "shaded", wireframe: bool = False,
           background: tuple[int, int, int] = (22, 24, 28),
           base_colour: tuple[int, int, int] = (176, 190, 205),
           cull_backfaces: bool = False) -> bytes:
    """Rasterise a mesh to PNG bytes.

    Flat shading, one colour per triangle: for asset inspection the facet
    structure is information, not an artefact.

    Lighting is two-sided (each face is lit by its viewer-facing normal) and
    back-face culling is off by default. Ported assets routinely arrive with
    inconsistent winding, and a preview that renders half a model black is
    worse than one that ignores winding entirely - the z-buffer still resolves
    occlusion correctly either way.
    """
    np = _numpy()
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise MeshLoadError("Pillow is required to render meshes") from exc

    camera = camera or Camera()
    width = height = max(64, int(size))

    if mesh.face_count == 0:
        return _blank(Image, width, height, background)

    vertices = mesh.vertices.astype("float64")
    centre = (vertices.min(axis=0) + vertices.max(axis=0)) / 2.0
    radius = float(np.linalg.norm(vertices - centre, axis=1).max()) or 1.0

    view = _view_matrix(np, camera)
    camera_space = (vertices - centre) @ view.T

    # Perspective fit: pull back far enough for the bounding sphere, then let
    # zoom scale that distance.
    fov = math.radians(36.0)
    distance = (radius / math.sin(fov / 2.0)) * 1.12 / max(0.15, camera.zoom)
    depth = camera_space[:, 2] + distance
    depth = np.where(depth < 1e-4, 1e-4, depth)
    focal = (width / 2.0) / math.tan(fov / 2.0)

    screen_x = camera_space[:, 0] * focal / depth + width / 2.0
    screen_y = -camera_space[:, 1] * focal / depth + height / 2.0
    projected = np.stack([screen_x, screen_y, depth], axis=1)

    faces = mesh.faces
    tri = camera_space[faces]
    normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    valid = lengths > 1e-12
    normals[valid] /= lengths[valid][:, None]

    # Camera space runs +Z away from the eye, so a triangle facing us has a
    # negative Z normal component.
    front = normals[:, 2] < 0
    facing = front if cull_backfaces and front.any() else np.ones(
        faces.shape[0], dtype=bool)

    # Two-sided lighting: point every normal back at the eye before shading so
    # a mesh with mixed winding still reads as a solid surface.
    normals = normals * np.where(normals[:, 2] > 0, -1.0, 1.0)[:, None]

    colours = _face_colours(np, mesh, normals, tri, shading, base_colour, distance,
                            radius)

    canvas = np.empty((height, width, 3), dtype="float32")
    canvas[:] = background
    zbuffer = np.full((height, width), np.inf, dtype="float64")

    order = np.nonzero(facing)[0]
    # Wireframes are drawn without a depth test, so only the front-facing
    # triangles go in - otherwise every mesh looks like an x-ray.
    edges = faces[np.nonzero(front)[0]] if front.any() else faces

    if shading == "wireframe":
        image = Image.fromarray(canvas.astype("uint8"), "RGB")
        _draw_wireframe(ImageDraw.Draw(image), projected, edges, (140, 200, 220))
        return _encode(Image, image)

    _rasterise(np, projected, faces[order], colours[order], canvas, zbuffer)

    image = Image.fromarray(np.clip(canvas, 0, 255).astype("uint8"), "RGB")
    if wireframe:
        _draw_wireframe(ImageDraw.Draw(image), projected, edges, (20, 24, 28),
                        width_px=1)
    return _encode(Image, image)


def _view_matrix(np: Any, camera: Camera) -> Any:
    """Rotation matrix taking world space into camera space (+Z toward eye)."""
    yaw = math.radians(camera.yaw)
    pitch = math.radians(max(-89.0, min(89.0, camera.pitch)))

    # Work in a Y-up camera basis; remap the model's up axis into it first.
    if camera.up_axis == "z":
        remap = np.array([[1, 0, 0],
                          [0, 0, 1],
                          [0, -1, 0]], dtype="float64")
    else:
        remap = np.eye(3)

    cos_yaw, sin_yaw = math.cos(yaw), math.sin(yaw)
    rot_yaw = np.array([[cos_yaw, 0, -sin_yaw],
                        [0, 1, 0],
                        [sin_yaw, 0, cos_yaw]], dtype="float64")
    cos_pitch, sin_pitch = math.cos(pitch), math.sin(pitch)
    rot_pitch = np.array([[1, 0, 0],
                          [0, cos_pitch, sin_pitch],
                          [0, -sin_pitch, cos_pitch]], dtype="float64")
    return rot_pitch @ rot_yaw @ remap


def _face_colours(np: Any, mesh: MeshData, normals: Any, tri: Any, shading: str,
                  base: tuple[int, int, int], distance: float,
                  radius: float) -> Any:
    """Flat colour per triangle for the requested shading mode."""
    if shading == "normals":
        return np.clip((normals * 0.5 + 0.5) * 255.0, 0, 255)

    if shading == "depth":
        centres = tri.mean(axis=1)[:, 2] + distance
        span = max(radius, 1e-6) * 2.0
        near = centres.min()
        shade = 1.0 - np.clip((centres - near) / span, 0.0, 1.0)
        grey = (40.0 + shade * 215.0)[:, None]
        return np.repeat(grey, 3, axis=1)

    # Lambert with a headlight plus a cool fill from above-left, which is
    # enough to read silhouette and surface direction. Both point back toward
    # the eye (-Z) because camera space runs +Z into the screen.
    key = np.array([0.35, 0.55, -0.75])
    key /= np.linalg.norm(key)
    fill = np.array([-0.5, 0.2, -0.4])
    fill /= np.linalg.norm(fill)
    lambert = np.clip(normals @ key, 0.0, 1.0)
    rim = np.clip(normals @ fill, 0.0, 1.0)
    intensity = 0.28 + 0.62 * lambert + 0.18 * rim
    colour = np.asarray(base, dtype="float64")[None, :] * intensity[:, None]
    # Tint the fill slightly blue so flat faces do not read as pure grey.
    colour[:, 2] += rim * 22.0
    return np.clip(colour, 0, 255)


def _rasterise(np: Any, projected: Any, faces: Any, colours: Any, canvas: Any,
               zbuffer: Any) -> None:
    """Z-buffered fill, batched by triangle size.

    A per-triangle barycentric grid costs ~40us of fixed numpy overhead however
    small the triangle is, and on a dense mesh almost every triangle covers a
    handful of pixels - 60k faces took 2.4 seconds, nearly all of it overhead.

    So triangles are bucketed by bounding-box size and each bucket is evaluated
    for all of its triangles at once over a shared span x span candidate grid.
    Only the few genuinely large triangles fall through to the scanline loop.
    """
    height, width = zbuffer.shape
    triangles = projected[faces]                       # (M, 3, 3)
    xs, ys = triangles[..., 0], triangles[..., 1]
    low_x, high_x = xs.min(axis=1), xs.max(axis=1)
    low_y, high_y = ys.min(axis=1), ys.max(axis=1)

    onscreen = ((high_x >= 0) & (low_x <= width - 1)
                & (high_y >= 0) & (low_y <= height - 1))
    extent = np.maximum(high_x - low_x, high_y - low_y)

    remaining = onscreen.copy()
    for span in _BUCKET_SPANS:
        # `span` candidate pixels per axis covers a bbox up to span-1 wide
        # once the fractional offset is taken into account.
        bucket = remaining & (extent <= span - 1)
        if bucket.any():
            _fill_batch(np, triangles, colours, np.nonzero(bucket)[0], canvas,
                        zbuffer, span)
            remaining &= ~bucket

    for index in np.nonzero(remaining)[0]:
        i0, i1, i2 = faces[index]
        p0, p1, p2 = projected[i0], projected[i1], projected[i2]

        min_x = max(int(math.floor(low_x[index])), 0)
        max_x = min(int(math.ceil(high_x[index])), width - 1)
        min_y = max(int(math.floor(low_y[index])), 0)
        max_y = min(int(math.ceil(high_y[index])), height - 1)
        if min_x > max_x or min_y > max_y:
            continue

        denominator = ((p1[1] - p2[1]) * (p0[0] - p2[0])
                       + (p2[0] - p1[0]) * (p0[1] - p2[1]))
        if abs(denominator) < 1e-12:
            continue

        xs = np.arange(min_x, max_x + 1, dtype="float64")
        ys = np.arange(min_y, max_y + 1, dtype="float64")
        grid_x, grid_y = np.meshgrid(xs, ys)

        w0 = ((p1[1] - p2[1]) * (grid_x - p2[0])
              + (p2[0] - p1[0]) * (grid_y - p2[1])) / denominator
        w1 = ((p2[1] - p0[1]) * (grid_x - p2[0])
              + (p0[0] - p2[0]) * (grid_y - p2[1])) / denominator
        w2 = 1.0 - w0 - w1

        inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
        if not inside.any():
            continue

        depth = w0 * p0[2] + w1 * p1[2] + w2 * p2[2]
        window = zbuffer[min_y:max_y + 1, min_x:max_x + 1]
        write = inside & (depth < window)
        if not write.any():
            continue
        window[write] = depth[write]
        canvas[min_y:max_y + 1, min_x:max_x + 1][write] = colours[index]


#: Candidate-grid sizes, smallest first. Anything larger goes scanline.
_BUCKET_SPANS = (2, 4, 8, 16)
#: Cap on candidate pixels evaluated in one numpy batch, to bound memory.
_BATCH_PIXELS = 4_000_000


def _fill_batch(np: Any, triangles: Any, colours: Any, indices: Any,
                canvas: Any, zbuffer: Any, span: int) -> None:
    """Rasterise many similarly-sized triangles at once.

    Every triangle in the batch gets the same `span` x `span` candidate grid
    anchored at its own bounding-box corner, so one set of numpy operations
    covers the whole bucket.
    """
    if indices.size == 0:
        return
    per_triangle = span * span
    chunk = max(1, _BATCH_PIXELS // per_triangle)
    for start in range(0, indices.size, chunk):
        _fill_chunk(np, triangles, colours, indices[start:start + chunk],
                    canvas, zbuffer, span)


def _fill_chunk(np: Any, triangles: Any, colours: Any, indices: Any,
                canvas: Any, zbuffer: Any, span: int) -> None:
    height, width = zbuffer.shape
    tri = triangles[indices]
    p0, p1, p2 = tri[:, 0], tri[:, 1], tri[:, 2]

    origin_x = np.floor(tri[..., 0].min(axis=1)).astype(np.intp)
    origin_y = np.floor(tri[..., 1].min(axis=1)).astype(np.intp)
    offsets = np.arange(span, dtype=np.intp)
    grid_x = origin_x[:, None, None] + offsets[None, None, :]     # (n, 1, span)
    grid_y = origin_y[:, None, None] + offsets[None, :, None]     # (n, span, 1)

    def column(values):
        return values[:, None, None]

    denominator = ((p1[:, 1] - p2[:, 1]) * (p0[:, 0] - p2[:, 0])
                   + (p2[:, 0] - p1[:, 0]) * (p0[:, 1] - p2[:, 1]))
    degenerate = np.abs(denominator) < 1e-12
    denominator = np.where(degenerate, 1.0, denominator)

    dx = grid_x - column(p2[:, 0])
    dy = grid_y - column(p2[:, 1])
    w0 = (column(p1[:, 1] - p2[:, 1]) * dx
          + column(p2[:, 0] - p1[:, 0]) * dy) / column(denominator)
    w1 = (column(p2[:, 1] - p0[:, 1]) * dx
          + column(p0[:, 0] - p2[:, 0]) * dy) / column(denominator)
    w2 = 1.0 - w0 - w1

    inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0) & ~column(degenerate)
    inside &= (grid_x >= 0) & (grid_x < width) & (grid_y >= 0) & (grid_y < height)
    if not inside.any():
        return

    depth = (w0 * column(p0[:, 2]) + w1 * column(p1[:, 2])
             + w2 * column(p2[:, 2]))
    triangle_of, row, col = np.nonzero(inside)
    flat = (np.broadcast_to(grid_y, inside.shape)[triangle_of, row, col] * width
            + np.broadcast_to(grid_x, inside.shape)[triangle_of, row, col])
    _write_pixels(np, flat, depth[triangle_of, row, col],
                  colours[indices[triangle_of]], canvas, zbuffer)


def _write_pixels(np: Any, flat: Any, depth: Any, colour: Any, canvas: Any,
                  zbuffer: Any) -> None:
    """Depth-test a bag of candidate pixels and write the winners."""
    if flat.size == 0:
        return
    # Nearest wins per pixel: sort by depth, then keep the first hit per pixel.
    order = np.argsort(depth, kind="stable")
    flat, depth, colour = flat[order], depth[order], colour[order]
    pixel_order = np.argsort(flat, kind="stable")
    flat, depth, colour = (flat[pixel_order], depth[pixel_order],
                           colour[pixel_order])
    first = np.ones(flat.shape, dtype=bool)
    first[1:] = flat[1:] != flat[:-1]
    flat, depth, colour = flat[first], depth[first], colour[first]

    # The buckets and the scanline pass share one z-buffer and all depth-test
    # against it, so the order they run in does not change the result.
    zflat = zbuffer.reshape(-1)
    visible = depth < zflat[flat]
    if not visible.any():
        return
    flat = flat[visible]
    zflat[flat] = depth[visible]
    canvas.reshape(-1, 3)[flat] = colour[visible]


def _draw_wireframe(draw: Any, projected: Any, faces: Any,
                    colour: tuple[int, int, int], width_px: int = 1) -> None:
    """Overlay triangle edges. Capped so a dense mesh cannot stall the UI."""
    limit = 40_000
    for index in range(min(faces.shape[0], limit)):
        i0, i1, i2 = faces[index]
        points = [(float(projected[i][0]), float(projected[i][1]))
                  for i in (i0, i1, i2, i0)]
        draw.line(points, fill=colour, width=width_px)


def _blank(Image: Any, width: int, height: int,
           background: tuple[int, int, int]) -> bytes:
    return _encode(Image, Image.new("RGB", (width, height), background))


def _encode(Image: Any, image: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _is_number(token: str) -> bool:
    try:
        float(token)
        return True
    except ValueError:
        return False
