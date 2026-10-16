"""Preserve prepared convex geometry through the installed native VPhysics serializer."""

from __future__ import annotations

import hashlib
import os
import struct
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np
import trimesh
from scipy.optimize import linear_sum_assignment
from scipy.spatial import cKDTree

from s2kit.pipelines.phy_audit import _convex_clouds, _skin_worlds, read_phy
from s2kit.pipelines.porting import check_cancelled


class GeometryFinalizationError(ValueError):
    """Exact native serialization is unavailable or failed independent geometry verification."""


def _digest(path: Path) -> str:
    """Hash complete tool inputs so helper caches and provenance identify actual executable content."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def serializer_source() -> Path:
    """Locate the packaged x86 bridge source used by the exact collision finalizer."""
    return Path(__file__).resolve().parent.parent / "native/convex_serializer.cs"


def _x86_binary(path: Path) -> bool:
    """Recognize complete x86 Windows PE files used by the bridge and compiler library."""
    if not path.is_file():
        return False
    binary = path.read_bytes()
    if len(binary) < 64 or binary[:2] != b"MZ":
        return False
    header = struct.unpack_from("<I", binary, 60)[0]
    return (header + 24 <= len(binary) and binary[header:header + 4] == b"PE\0\0"
            and struct.unpack_from("<H", binary, header + 4)[0] == 0x14C)


def _run(command: list[str], *, cwd: Path, cancel=None, timeout: float = 180) -> str:
    """Run an isolated helper with cancellation and a bounded lifetime while collecting small diagnostics."""
    with tempfile.TemporaryFile() as output:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=flags)
        started = time.monotonic()
        try:
            while process.poll() is None:
                check_cancelled(cancel)
                if time.monotonic() - started > timeout:
                    raise GeometryFinalizationError("Native collision serialization timed out; the existing PHY was preserved")
                time.sleep(0.05)
        except BaseException:
            process.kill()
            process.wait()
            raise
        output.seek(0)
        message = output.read().decode("utf-8", errors="replace").strip()
        if process.returncode:
            raise GeometryFinalizationError(f"Native collision helper exited with {process.returncode}: {message[-3000:]}")
        return message


def _helper(compiler: Path, cache: Path, cancel=None) -> tuple[Path, Path, dict]:
    """Build the content-addressed bridge with Windows .NET and select the compiler's x86 physics library."""
    if os.name != "nt":
        raise GeometryFinalizationError("Exact physics serialization currently requires Windows and an x86 Source compiler VPhysics DLL")
    source = serializer_source()
    if not source.is_file():
        raise GeometryFinalizationError(f"The packaged native helper source is missing: {source}; reinstall the porting kit")
    framework = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
    if not framework.is_file():
        raise GeometryFinalizationError("Exact physics serialization needs the Windows .NET Framework 4 C# compiler; install/enable .NET Framework 4.8")
    compiler = Path(compiler).resolve()
    names = ["cevphysics.dll", "vphysics.dll"] if compiler.name.casefold().startswith("ce") else ["vphysics.dll", "cevphysics.dll"]
    compatible = []
    for name in names:
        candidate = compiler.parent / name
        if _x86_binary(candidate):
            compatible.append(candidate)
    if not compatible:
        raise GeometryFinalizationError(f"No compatible x86 vphysics.dll/cevphysics.dll was found beside {compiler}; select the 32-bit StudioMDL CE installation")
    library = compatible[0]
    source_hash, framework_hash, library_hash = _digest(source), _digest(framework), _digest(library)
    identity = hashlib.sha256((source_hash + framework_hash).encode()).hexdigest()[:20]
    directory = Path(cache).resolve() / identity
    directory.mkdir(parents=True, exist_ok=True)
    executable = directory / "convex_serializer.exe"
    if not _x86_binary(executable):
        with tempfile.TemporaryDirectory(prefix="csharp-", dir=directory) as temporary:
            candidate = Path(temporary) / executable.name
            _run([str(framework), "/nologo", "/optimize+", "/platform:x86", "/out:" + str(candidate), str(source)],
                 cwd=directory, cancel=cancel, timeout=60)
            if not _x86_binary(candidate):
                raise GeometryFinalizationError("The C# compiler did not produce a complete x86 collision helper")
            os.replace(candidate, executable)
    return executable, library, dict(source=str(source), source_sha256=source_hash,
                                    helper=str(executable), helper_sha256=_digest(executable),
                                    csharp_compiler=str(framework), csharp_compiler_sha256=framework_hash,
                                    physics_library=str(library), physics_library_sha256=library_hash,
                                    compiler=str(compiler), compiler_sha256=_digest(compiler),
                                    interface="VPhysicsCollision007", pointer_bits=32,
                                    construction="ConvexFromConvexPolyhedron", serialization="ConvertConvexToCollide/CollideWrite")


def _polyhedron(points: np.ndarray) -> tuple[bytes, trimesh.Trimesh]:
    """Encode a closed convex triangulation in the SDK CPolyhedron array layout with clockwise outward faces."""
    hull = trimesh.convex.convex_hull(np.asarray(points, dtype=np.float64))
    if not hull.is_watertight or not hull.is_convex or not np.isfinite(hull.vertices).all() or hull.volume <= 0:
        raise GeometryFinalizationError("Prepared physics shape is not a finite closed positive-volume convex hull")
    edges, references, polygons = {}, [], []
    for face, normal in zip(hull.faces[:, ::-1], hull.face_normals):
        start = len(references)
        for first, second in zip(face, np.roll(face, -1)):
            edge = tuple(sorted((int(first), int(second))))
            index = edges.setdefault(edge, len(edges))
            references.append((index, int(second != edge[0])))
        polygons.append((start, 3, *normal))
    counts = (len(hull.vertices), len(edges), len(references), len(polygons))
    if any(count > 65535 for count in counts):
        raise GeometryFinalizationError(f"Prepared convex exceeds SDK 16-bit polyhedron counts: {counts}")
    output = struct.pack("<4H", *counts) + np.asarray(hull.vertices, dtype="<f4").tobytes()
    output += np.asarray(list(edges), dtype="<u2").tobytes()
    output += b"".join(struct.pack("<HBB", index, endpoint, 0) for index, endpoint in references)
    output += b"".join(struct.pack("<HH3f", *polygon) for polygon in polygons)
    return output, hull


def _solid_meshes(blob: bytes) -> list[trimesh.Trimesh]:
    """Decode every terminal serialized ledge including face connectivity after bounded cloud validation."""
    _convex_clouds(blob)
    root = 28 + struct.unpack_from("<I", blob, 60)[0]
    pending, meshes = [root], []
    while pending:
        node = pending.pop()
        right, ledge = struct.unpack_from("<ii", blob, node)
        if right:
            pending.extend((node + 28, node + right))
            continue
        offset = node + ledge
        relative, _, _, count, _ = struct.unpack_from("<iiIHH", blob, offset)
        triangles = np.asarray([struct.unpack_from("<6H", blob, offset + 20 + index * 16)[::2] for index in range(count)])
        indices = np.unique(triangles)
        vertices = np.asarray([struct.unpack_from("<3f", blob, offset + relative + int(index) * 16) for index in indices])
        vertices = vertices[:, [0, 2, 1]] / 0.0254
        vertices[:, 2] *= -1
        faces = np.searchsorted(indices, triangles)
        meshes.append(trimesh.Trimesh(vertices=vertices, faces=faces, process=False))
    return meshes


def _shape_error(expected: trimesh.Trimesh, actual: trimesh.Trimesh) -> tuple[float, np.ndarray]:
    """Bound convex surface Hausdorff distance by the symmetric nearest-vertex distances of its hulls."""
    distance, mapping = cKDTree(expected.vertices).query(actual.vertices)
    reverse = cKDTree(actual.vertices).query(expected.vertices)[0]
    return float(max(distance.max(), reverse.max())), mapping


def _geometry_report(name: str, expected: list[trimesh.Trimesh], blob: bytes,
                     maximum_error: float, maximum_volume_error: float, native_bounds=None) -> dict:
    """Match and independently verify every serialized source shape, surface bound, volume and triangulation."""
    actual = _solid_meshes(blob)
    source_points = np.concatenate([mesh.vertices for mesh in expected])
    source_bounds = np.stack((source_points.min(axis=0), source_points.max(axis=0)))
    native_bounds_error = None
    if native_bounds is not None:
        native_bounds = np.asarray(native_bounds).reshape(2, 3)
        native_bounds_error = float(np.abs(native_bounds - source_bounds).max())
        if not np.isfinite(native_bounds).all() or native_bounds_error > maximum_error:
            raise GeometryFinalizationError(f"{name}: native collision support AABB differs by {native_bounds_error:.9g} in; invalid convex edge traversal")
    if len(expected) != len(actual):
        raise GeometryFinalizationError(f"{name}: native serialization changed convex separation {len(expected)}→{len(actual)}")
    costs = np.asarray([[_shape_error(source, target)[0] for target in actual] for source in expected])
    source_indices, target_indices = linear_sum_assignment(costs)
    shapes = []
    for source_index, target_index in zip(source_indices, target_indices):
        source, target = expected[source_index], actual[target_index]
        error, mapping = _shape_error(source, target)
        target_volume = abs(float(trimesh.convex.convex_hull(target.vertices).volume))
        volume_error = abs(target_volume / source.volume - 1)
        if error > maximum_error or volume_error > maximum_volume_error:
            raise GeometryFinalizationError(f"{name} shape {source_index}: decoded native geometry exceeds tolerance ({error:.9g} in, {volume_error:.9g} relative volume)")
        wanted_faces = {tuple(sorted(face)) for face in source.faces}
        found_faces = {tuple(sorted(mapping[face])) for face in target.faces}
        if len(source.vertices) != len(target.vertices) or len(source.faces) != len(target.faces) or wanted_faces != found_faces:
            raise GeometryFinalizationError(f"{name} shape {source_index}: native serialization changed the prepared convex triangulation")
        shapes.append(dict(source_shape=int(source_index), serialized_shape=int(target_index),
                           source_vertices=len(source.vertices), serialized_vertices=len(target.vertices),
                           source_triangles=len(source.faces), serialized_triangles=len(target.faces),
                           triangle_topology_exact=wanted_faces == found_faces,
                           maximum_surface_error_bound_in=error, source_volume_in3=float(source.volume),
                           serialized_volume_in3=target_volume, relative_volume_error=volume_error))
    return dict(bone=name, source_shape_count=len(expected), serialized_shape_count=len(actual), shapes=shapes,
                native_support_bounds_in=native_bounds.tolist() if native_bounds is not None else None,
                maximum_native_support_bounds_error_in=native_bounds_error,
                maximum_surface_error_bound_in=max(item["maximum_surface_error_bound_in"] for item in shapes),
                maximum_relative_shape_volume_error=max(item["relative_volume_error"] for item in shapes))


def rebuild_compiled_phy_geometry(phy_path: Path, physics_result, compiler_path: Path, cache_dir: Path, *,
                                  on_log=None, cancel=None, maximum_surface_error_in: float = 1e-4,
                                  maximum_relative_volume_error: float = 1e-5) -> dict:
    """Atomically replace validated geometry using native serialization while preserving checksum/header/keyvalues.

    Errors preserve the original file and identify missing runtime/compiler dependencies.
    The caller owns fallback policy and runs mass finalization and parameter audits afterward.
    Surface tolerances apply to prepared source convexes; analytic tessellation and thickness
    repairs remain separately recorded in the source physics report.
    """
    if (not np.isfinite(maximum_surface_error_in) or maximum_surface_error_in <= 0
            or not np.isfinite(maximum_relative_volume_error) or maximum_relative_volume_error <= 0):
        raise GeometryFinalizationError("Native geometry tolerances must be finite and positive")
    phy_path = Path(phy_path)
    original = phy_path.read_bytes()
    compiled = read_phy(phy_path, geometry=False)
    if [int(solid["index"]) for solid in compiled["solids"]] != list(range(compiled["solid_count"])):
        raise GeometryFinalizationError("Compiled PHY solid keyvalues must appear in contiguous binary index order")
    worlds = _skin_worlds(physics_result.skin)
    body_by_name = {item["helper"]: item for item in physics_result.body_reports}
    prepared, payload = [], bytearray(struct.pack("<4sii", b"S2CV", 1, compiled["solid_count"]))
    for solid in compiled["solids"]:
        check_cancelled(cancel)
        name = solid["name"]
        if name not in body_by_name:
            raise GeometryFinalizationError(f"Compiled physics body is absent from the source audit manifest: {name}")
        part = body_by_name[name]["part"]
        inverse = np.linalg.inv(worlds[physics_result.helper_by_part[part]])
        meshes = []
        shapes = [(mesh, report) for owner, mesh, report in physics_result.shapes if owner == part]
        if not shapes:
            raise GeometryFinalizationError(f"Prepared physics body has no source convex shapes: {name}")
        payload.extend(struct.pack("<i", len(shapes)))
        for mesh, report in shapes:
            points = mesh.vertices @ inverse[:3, :3].T + inverse[:3, 3]
            encoded, hull = _polyhedron(points)
            payload.extend(encoded)
            meshes.append(hull)
        prepared.append((name, meshes))
    executable, library, provenance = _helper(Path(compiler_path), Path(cache_dir), cancel)
    with tempfile.TemporaryDirectory(prefix="native-geometry-", dir=Path(cache_dir).resolve()) as temporary:
        work = Path(temporary)
        input_path, output_path = work / "source.convexes", work / "native.surfaces"
        input_path.write_bytes(payload)
        _run([str(executable), str(library), str(input_path), str(output_path)], cwd=work, cancel=cancel)
        data = output_path.read_bytes()
        if len(data) < 12 or struct.unpack_from("<4sii", data) != (b"S2PV", 2, len(prepared)):
            raise GeometryFinalizationError("Native helper returned an invalid surface container")
        position, blobs, reports = 12, [], []
        for name, expected in prepared:
            if position + 4 > len(data):
                raise GeometryFinalizationError("Native helper omitted a physics body")
            size = struct.unpack_from("<I", data, position)[0]
            position += 4
            blob = data[position:position + size]
            if len(blob) != size:
                raise GeometryFinalizationError("Native helper truncated a physics body")
            position += size
            if position + 24 > len(data):
                raise GeometryFinalizationError("Native helper omitted collision support bounds")
            native_bounds = struct.unpack_from("<6f", data, position)
            position += 24
            reports.append(_geometry_report(name, expected, blob, maximum_surface_error_in,
                                            maximum_relative_volume_error, native_bounds))
            blobs.append(blob)
        if position != len(data):
            raise GeometryFinalizationError("Native helper returned unexpected trailing surfaces")
        header_size = struct.unpack_from("<I", original)[0]
        tail_offset = header_size
        for solid in compiled["solids"]:
            tail_offset += 4 + struct.unpack_from("<I", original, tail_offset)[0]
        replacement = original[:header_size] + b"".join(struct.pack("<I", len(blob)) + blob for blob in blobs) + original[tail_offset:]
        candidate = work / "verified.phy"
        candidate.write_bytes(replacement)
        verified = read_phy(candidate)
        if verified["checksum"] != compiled["checksum"] or verified["text"] != compiled["text"]:
            raise GeometryFinalizationError("Native replacement changed the original checksum or physics keyvalues")
        check_cancelled(cancel)
        if phy_path.read_bytes() != original:
            raise GeometryFinalizationError("Compiled PHY changed during geometry finalization; refusing to overwrite it")
        with tempfile.NamedTemporaryFile(prefix=phy_path.name + ".", suffix=".verified", dir=phy_path.parent, delete=False) as stream:
            pending = Path(stream.name)
            stream.write(replacement)
        try:
            os.replace(pending, phy_path)
        finally:
            pending.unlink(missing_ok=True)
    result = dict(status="preserved", method="native_exact_convex_polyhedra", provenance=provenance,
                  body_count=len(reports), bodies=reports, checksum_preserved=True, header_preserved=True,
                  keyvalues_preserved=True, original_phy_sha256=hashlib.sha256(original).hexdigest(),
                  finalized_phy_sha256=hashlib.sha256(replacement).hexdigest(),
                  maximum_surface_error_bound_in=max(item["maximum_surface_error_bound_in"] for item in reports),
                  maximum_native_support_bounds_error_in=max(item["maximum_native_support_bounds_error_in"] for item in reports),
                  maximum_relative_shape_volume_error=max(item["maximum_relative_shape_volume_error"] for item in reports),
                  tolerance_reference="Prepared convex surfaces; analytic tessellation and thickness repairs remain separate source adaptations",
                  tolerances=dict(surface_error_in=maximum_surface_error_in, relative_volume_error=maximum_relative_volume_error))
    if on_log:
        on_log(f"Native serialized {len(reports)} physics bodies; max surface bound {result['maximum_surface_error_bound_in']:.8g}in, max volume error {result['maximum_relative_shape_volume_error']:.8g}", "SUCCESS")
    return result
