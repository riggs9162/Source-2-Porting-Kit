"""Preserve authored native LOD meshes alongside a VRF glTF model export.

VRF's glTF exporter emits the primary mesh only. A separate native ModelDoc
decompile supplies lower-resolution DMX meshes and their switch distances.
Only single-mesh LOD groups can currently feed the native model porter.
"""

from __future__ import annotations

import json
import math
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping

from s2kit.core import process
from s2kit.pipelines.model_metadata import parse_kv3_documents


class NativeLodError(ValueError):
    """Authored LOD groups cannot be preserved by the native model porter."""


@dataclass(frozen=True)
class NativeLod:
    """One authored lower-resolution mesh and its switch distance."""

    distance: float
    source: str


def _safe_resource_path(value: str) -> PurePosixPath:
    """Reject absolute, parent-relative or drive-qualified resource paths."""
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or ":" in str(path) or not path.name:
        raise NativeLodError(f"Unsafe native LOD resource path: {value!r}")
    return path


def _modeldoc_nodes(node: dict):
    """Visit ModelDoc child nodes in authoring order."""
    yield node
    for child in node.get("children", []):
        if isinstance(child, dict):
            yield from _modeldoc_nodes(child)


def parse_modeldoc_lods(text: str) -> list[NativeLod]:
    """Read ordered single-mesh ModelDoc LOD groups without guessing names.

    The first group is the primary glTF mesh. Distances must be finite and
    strictly increasing, and every referenced mesh must have a DMX source.
    Multi-mesh groups require a future native mesh-composition implementation.
    """
    documents = parse_kv3_documents(text)
    if len(documents) != 1 or not isinstance(documents[0].get("rootNode"), dict):
        raise NativeLodError("Native decompile did not produce one ModelDoc root")
    nodes = list(_modeldoc_nodes(documents[0]["rootNode"]))
    meshes = {}
    for node in nodes:
        if node.get("_class") == "RenderMeshFile":
            name = node.get("name")
            if not isinstance(name, str) or name in meshes:
                raise NativeLodError("Render mesh names are missing or duplicated")
            meshes[name] = node.get("filename", "")
    groups = [node for node in nodes if node.get("_class") == "LODGroup"]
    if not groups:
        return []
    lods = []
    previous = -1.0
    for group in groups:
        distance = float(group.get("switch_threshold", float("nan")))
        if not math.isfinite(distance) or distance < 0 or distance <= previous:
            raise NativeLodError("LOD switch distances must be finite and increasing")
        references = group.get("mesh_references", [])
        if len(references) != 1 or not isinstance(references[0], dict):
            raise NativeLodError("Multiple or empty meshes in a LOD group are unsupported")
        mesh_name = references[0].get("mesh_name")
        if mesh_name not in meshes:
            raise NativeLodError(f"LOD mesh {mesh_name!r} has no RenderMeshFile")
        source = _safe_resource_path(meshes[mesh_name])
        if source.suffix.lower() != ".dmx":
            raise NativeLodError(f"LOD mesh {mesh_name!r} is not a DMX source")
        lods.append(NativeLod(distance, source.as_posix()))
        previous = distance
    if lods[0].distance != 0:
        raise NativeLodError("The primary LOD group must have switch distance zero")
    return lods[1:]


def _authored_distances(data: str) -> list[float]:
    """Read LOD switch distances from the compiled model DATA block."""
    documents = parse_kv3_documents(data)
    if len(documents) != 1:
        raise NativeLodError("Expected one compiled model DATA document")
    return [float(value) for value in documents[0].get("m_lodGroupSwitchDistances", [])]


def write_native_lod_sidecar(modeldoc: Path, model_path: Path,
                             expected_distances: list[float]) -> int:
    """Copy exported lower LODs beside a model and write its portable manifest.

    Exact-file VRF exports flatten companion DMXs beside their ModelDoc output.
    All sources are checked before writing, including flattened-name collisions.
    The archived ModelDoc keeps the authored reference paths for inspection.
    """
    lods = parse_modeldoc_lods(modeldoc.read_text(encoding="utf-8"))
    if [lod.distance for lod in lods] != expected_distances[1:]:
        raise NativeLodError("ModelDoc LOD distances differ from compiled model DATA")
    files = []
    names = set()
    for lod in lods:
        name = _safe_resource_path(lod.source).name
        if name.lower() in names:
            raise NativeLodError(f"Native LOD filenames collide after export: {name}")
        source = modeldoc.parent / name
        if not source.is_file():
            raise NativeLodError(f"Native LOD companion was not exported: {name}")
        names.add(name.lower())
        files.append(source)
    destination = model_path.parent / (model_path.stem + "_native_lods")
    destination.mkdir(parents=True, exist_ok=True)
    entries = []
    for lod, source in zip(lods, files):
        target = destination / source.name
        shutil.copy2(source, target)
        entries.append({"distance": lod.distance,
                        "source": target.relative_to(model_path.parent).as_posix()})
    shutil.copy2(modeldoc, destination / "source.vmdl")
    model_path.with_suffix(".lods.json").write_text(
        json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    return len(entries)


def export_native_lod_sidecars(vrf_exe: Path, input_path: Path, staging_dir: Path,
                               model_data: Mapping[str, str],
                               on_log: Callable[[str], None] = print,
                               cancel: threading.Event | None = None) -> int:
    """Decompile models with authored LODs, retaining only required DMX files.

    Each model gets an isolated exact-file export to avoid VRF output-path
    ambiguity. Unsupported groups or failed decompilation produce explicit
    warnings; cancellation propagates to the owning export job.
    """
    written = 0
    for model_name, data in model_data.items():
        try:
            distances = _authored_distances(data)
            if len(distances) < 2:
                continue
            relative = _safe_resource_path(model_name)
            if relative.suffix.lower() != ".vmdl_c":
                raise NativeLodError("Native LOD export needs a compiled VMDL resource")
            model_path = staging_dir / relative.with_suffix(".glb")
            if not model_path.is_file() and not model_path.with_suffix(".gltf").is_file():
                raise NativeLodError("No matching glTF model was exported")
            on_log(f"Preserving {len(distances) - 1} authored LOD mesh(es): {model_name}")
            with tempfile.TemporaryDirectory(prefix=".native_lods_", dir=staging_dir) as temp:
                modeldoc = Path(temp) / "model.vmdl"
                command = [str(vrf_exe), "-i", str(input_path), "-e", "vmdl_c",
                           "-f", relative.as_posix(), "-d", "-o", str(modeldoc),
                           "--threads", "1"]
                result = process.run_streamed(command, on_log, cancel=cancel)
                if result != 0 or not modeldoc.is_file():
                    raise NativeLodError(f"VRF native decompile failed (exit {result})")
                written += write_native_lod_sidecar(modeldoc, model_path, distances)
        except (OSError, ValueError, KeyError, TypeError, process.ProcessError) as exc:
            on_log(f"WARNING: {model_name}: authored LODs were not preserved: {exc}")
    if written:
        on_log(f"Preserved {written} authored lower LOD mesh(es) with switch distances")
    return written
