"""Resolve compiled materials in isolated shader-aware texture export contexts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path, PurePosixPath
from typing import Callable

import numpy as np
from PIL import Image

from s2kit.core import process
from s2kit.pipelines.porting import PortCancelled, check_cancelled
from s2kit.pipelines.soundscape import KVBlock, SoundscapeError, parse_kv
from s2kit.pipelines.vmat import KEY_ALIASES, VmatEntry, VmatParser


def _digest(path: Path) -> str:
    """Hash complete cache content without retaining large images in memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _resource(value: str) -> PurePosixPath:
    """Require one exact relative compiled material resource."""
    path = PurePosixPath(value.replace("\\", "/"))
    if (path.is_absolute() or ".." in path.parts or ":" in str(path)
            or path.suffix.lower() not in {".vmat", ".vmat_c"}):
        raise ValueError(f"Unsafe compiled material resource: {value}")
    return path.with_suffix(".vmat_c")


def _fields(path: Path, *, strict: bool = True) -> tuple[dict[str, str], dict[str, str]]:
    """Retain authored semantic inputs and the complete original compiled texture table."""
    document = parse_kv(path.read_text(encoding="utf-8-sig"))
    if len(document.pairs) == 1 and isinstance(document.pairs[0][1], KVBlock):
        layer = document.pairs[0][1]
    elif not strict:
        layer = document
    else:
        raise ValueError(f"Expected exactly one decompiled material layer: {path}")
    compiled = layer.block("Compiled Textures")
    return layer.scalars(), compiled.scalars() if compiled is not None else {}


def _compiled_candidates(image: str, compiled: dict[str, str]) -> list[dict[str, str]]:
    """Link semantic outputs to compiled names only when the exporter retained their identity."""
    stem = PurePosixPath(image.replace("\\", "/")).stem.casefold()
    candidates = []
    for parameter, resource in compiled.items():
        original = PurePosixPath(resource.replace("\\", "/")).stem.casefold()
        match = re.fullmatch(r"(.+)_([a-z0-9]{3,4})_([0-9a-f]+)", original)
        if original == stem or (match and (stem == match[1] or match[3] in stem.split("_"))):
            candidates.append(dict(parameter=parameter, resource=resource))
    return candidates


def _default_sources(raw_vmat: Path, raw_root: Path) -> dict:
    """Locate only full compiled identities for builtin first-input maps omitted by VRF."""
    fields, compiled = _fields(raw_vmat)
    sources = {}
    for role, aliases in KEY_ALIASES.items():
        parameter = next((key for key in aliases if key in fields), "")
        image = fields.get(parameter, "")
        if not image.startswith("materials/default/") or role == "roughness":
            continue
        candidates = _compiled_candidates(image, compiled)
        if len(candidates) != 1 or not candidates[0]["resource"].startswith("materials/default/"):
            raise ValueError(f"Builtin default lacks one compiled texture identity: {raw_vmat}: {parameter}")
        resource = candidates[0]["resource"]
        matches = list(raw_root.rglob(PurePosixPath(resource).stem + ".png"))
        if len(matches) != 1:
            raise ValueError(f"Builtin default lacks one exact compiled image: {raw_vmat}: {resource}")
        sources[role] = dict(parameter=parameter, image=image, resource=resource,
                             path=str(matches[0].resolve()), sha256=_digest(matches[0]),
                             shader=fields.get("shader", ""))
    return sources


def _write_defaults(directory: Path, defaults: dict) -> dict:
    """Select documented channels only after verifying builtin neutral or monochrome waveform pixels."""
    proof = {}
    for role, source in defaults.items():
        path = Path(source["path"])
        if _digest(path) != source["sha256"]:
            raise ValueError(f"Compiled builtin default changed while exporting: {path}")
        with Image.open(path) as bitmap:
            pixels = np.array(bitmap.convert("RGBA"))
        rgb, alpha = pixels[:, :, :3], pixels[:, :, 3]
        shader = source["shader"]
        basename = PurePosixPath(source["image"]).name
        if (role == "normal" and basename == "default_normal.tga"
                and shader in {"vr_complex.vfx", "vr_simple.vfx"}
                and np.all(rgb == rgb[0, 0]) and np.all(np.abs(rgb[0, 0, :2].astype(int) - 127) <= 1)
                and rgb[0, 0, 2] == 255 and np.all(alpha == 128)):
            channels, selected = "RGB", Image.fromarray(rgb)
        elif (role == "ao" and basename == "default_ao.tga"
              and shader in {"vr_complex.vfx", "vr_simple.vfx"} and np.all(pixels == 255)):
            channels, selected = "R", Image.fromarray(pixels[:, :, 0])
        elif (role == "selfillum" and basename == "default_selfillum.tga"
              and shader == "vr_complex.vfx" and np.all(rgb == 0) and np.all(alpha == 255)):
            channels, selected = "R", Image.fromarray(pixels[:, :, 0])
        elif (role == "selfillum_flow_waveform" and basename == "default_wave.tga"
              and shader == "vr_xen_foliage.vfx" and np.all(alpha == 255)
              and np.all(rgb[:, :, 0] == rgb[:, :, 1]) and np.all(rgb[:, :, 1] == rgb[:, :, 2])):
            channels, selected = "RGB", Image.fromarray(rgb)
        else:
            raise ValueError(f"Unsupported builtin default channel semantics: {source['resource']}: {role}")
        relative = PurePosixPath(source["image"])
        if relative.is_absolute() or ".." in relative.parts or ":" in str(relative):
            raise ValueError(f"Unsafe builtin default image: {relative}")
        target = directory / Path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        selected.save(target, format="PNG")
        proof[role] = dict(**source, channel_selection=channels,
                           rgba_min=pixels.min(axis=(0, 1)).tolist(),
                           rgba_max=pixels.max(axis=(0, 1)).tolist(),
                           semantic_image=relative.as_posix(), semantic_sha256=_digest(target),
                           method="validated_builtin_default_channels")
    return proof


def material_provenance(entry: VmatEntry, *, resource: str = "") -> dict:
    """Describe exact decoded inputs without inventing channel equivalence for unsupported shaders."""
    metadata_error = ""
    try:
        authored, compiled = _fields(entry.path, strict=bool(resource))
    except (ValueError, SoundscapeError) as exc:
        if resource:
            raise
        authored, compiled = {}, {}
        metadata_error = str(exc)
    maps = {}
    for role, path in sorted(entry.resolved.items()):
        parameter = next((key for key in KEY_ALIASES[role] if key in authored), "")
        image = authored.get(parameter, "")
        with Image.open(path) as bitmap:
            maps[role] = dict(
                authored_parameter=parameter, authored_image=image,
                path=str(path.resolve()), sha256=_digest(path),
                dimensions=list(bitmap.size), channels=list(bitmap.getbands()),
                resolution=entry.sources[role],
                compiled_texture_candidates=_compiled_candidates(image or path.name, compiled))
    return dict(material_resource=resource, isolated_vmat=str(entry.path.resolve()),
                isolated_vmat_sha256=_digest(entry.path),
                authored_textures={key: value for key, value in authored.items() if key.startswith("Texture")},
                compiled_textures=compiled, semantic_maps=maps,
                constants=entry.constants,
                metadata_status="unavailable" if metadata_error else "parsed",
                metadata_error=metadata_error,
                channel_conversion="authored semantic maps")


def _command(source: Path, resource: PurePosixPath, executable: Path, output: Path) -> list[str]:
    """Export only the selected VMAT so sibling materials cannot overwrite its decoded maps."""
    command = [str(executable), "-i", str(source), "-o", str(output), "-d", "--threads", "1", "-e", "vmat_c"]
    if source.suffix.lower() == ".vpk":
        return [*command, "-f", resource.as_posix()]
    matches = {parent / Path(resource) for parent in source.parents if (parent / Path(resource)).is_file()}
    if len(matches) != 1:
        raise ValueError(f"Cannot resolve one original compiled material beside {source}: {resource}")
    command[2] = str(next(iter(matches)))
    return command


def _cache_files(directory: Path, resource: PurePosixPath, raw_hash: str,
                 archive_input: bool) -> tuple[VmatEntry, list[Path]]:
    """Reject prefix-filter siblings, missing images and mismatched original material bytes."""
    vmats = list(directory.rglob("*.vmat"))
    if len(vmats) != 1 or vmats[0].stem != resource.stem:
        raise ValueError(f"Isolated material export selected unexpected VMATs: {resource}")
    if archive_input and vmats[0].relative_to(directory).as_posix() != resource.with_suffix(".vmat").as_posix():
        raise ValueError(f"Isolated archive export selected another resource path: {resource}")
    if _digest(vmats[0]) != raw_hash:
        raise ValueError(f"Isolated VMAT bytes differ from the selected extracted material: {resource}")
    entry = VmatParser(texture_root=directory).parse(vmats[0], root=directory)
    for path in entry.resolved.values():
        if not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError(f"Isolated material resolved a texture outside its own context: {path}")
    authored, _ = _fields(vmats[0])
    for role, aliases in KEY_ALIASES.items():
        value = next((authored[key] for key in aliases if key in authored), "")
        if (value and not value.startswith(("[", "{"))
                and (role not in entry.resolved or entry.sources.get(role) != "vmat")):
            raise ValueError(f"Isolated material lacks authored {role} map: {resource}: {value}")
    files = sorted(path for path in directory.rglob("*") if path.is_file() and path != directory / "manifest.json")
    if any(not path.resolve().is_relative_to(directory.resolve()) for path in files):
        raise ValueError(f"Isolated material cache contains files outside its owned context: {directory}")
    return entry, files


def _verified_cache(directory: Path, identity: dict, resource: PurePosixPath) -> tuple[VmatEntry, list[Path]] | None:
    """Trust only completed isolated exports whose entire recorded byte inventory still matches."""
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("identity") != identity or manifest.get("complete") is not True:
            return None
        entry, files = _cache_files(directory, resource, identity["raw_vmat_sha256"], identity["archive_input"])
        actual = {path.relative_to(directory).as_posix(): _digest(path) for path in files}
        if actual != manifest["files"]:
            return None
        return entry, [*files, manifest_path]
    except (OSError, ValueError, KeyError, TypeError, SoundscapeError):
        return None


def _discard(directory: Path, root: Path) -> None:
    """Remove only a verified owned temporary/cache directory beneath the project cache root."""
    resolved = directory.resolve()
    if resolved == root.resolve() or not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"Refusing to remove unowned semantic material directory: {directory}")
    if directory.exists():
        shutil.rmtree(directory)


def resolve_material(*, resource: str, raw_vmat: Path, raw_root: Path, source: Path, project: Path,
                     vrf_executable: Path, source_signature: str, implementation_signature: str,
                     log: Callable[[str], None], cancel: threading.Event | None = None
                     ) -> tuple[VmatEntry, dict, list[Path]]:
    """Return a hash-verified exact VMAT export without touching the immutable raw model extraction."""
    check_cancelled(cancel)
    selected = _resource(resource)
    raw_hash = _digest(raw_vmat)
    defaults = _default_sources(raw_vmat, raw_root)
    identity = dict(version=1, material_resource=selected.as_posix(), source=str(source.resolve()),
                    archive_input=source.suffix.lower() == ".vpk",
                    source_signature=source_signature, raw_vmat_sha256=raw_hash,
                    exporter=str(vrf_executable.resolve()), exporter_sha256=_digest(vrf_executable),
                    implementation_signature=implementation_signature,
                    builtin_defaults={role: {key: value for key, value in item.items() if key != "path"}
                                      for role, item in defaults.items()})
    cache_root = Path(project).resolve() / "semantic-materials"
    cache_root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    destination = cache_root / key
    cached = _verified_cache(destination, identity, selected)
    if cached is None:
        temporary = Path(tempfile.mkdtemp(prefix=".export-", dir=cache_root))
        try:
            command = _command(Path(source), selected, Path(vrf_executable), temporary)
            code = process.run_streamed(command, log, cancel=cancel, cwd=temporary)
            check_cancelled(cancel)
            if code:
                raise ValueError(f"Isolated material export failed ({code}): {selected}")
            default_proof = _write_defaults(temporary, defaults)
            _, files = _cache_files(temporary, selected, raw_hash, identity["archive_input"])
            manifest = dict(identity=identity, complete=True, builtin_defaults=default_proof,
                            files={path.relative_to(temporary).as_posix(): _digest(path) for path in files})
            check_cancelled(cancel)
            (temporary / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            if destination.exists():
                _discard(destination, cache_root)
            os.replace(temporary, destination)
        except process.ProcessCancelled as exc:
            raise PortCancelled() from exc
        finally:
            _discard(temporary, cache_root)
        cached = _verified_cache(destination, identity, selected)
    if cached is None:
        raise ValueError(f"Isolated material cache did not verify after export: {selected}")
    check_cancelled(cancel)
    entry, files = cached
    provenance = material_provenance(entry, resource=selected.as_posix())
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    provenance.update(mode="isolated_compiled_vmat", raw_vmat=str(raw_vmat.resolve()),
                      raw_vmat_sha256=raw_hash, vmat_bytes_preserved=True,
                      channel_conversion="Source2Viewer shader-aware material unpacking",
                      builtin_defaults=manifest["builtin_defaults"],
                      manifest=str(destination / "manifest.json"),
                      manifest_sha256=_digest(destination / "manifest.json"))
    return entry, provenance, files
