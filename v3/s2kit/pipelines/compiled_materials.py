"""Expand exact QC material bindings over duplicate Source compiler texture identities."""

from __future__ import annotations

import hashlib
import os
import struct
from pathlib import Path

from s2kit.pipelines.compiled_model_audit import read_compiled_model
from s2kit.pipelines.compiled_render_coverage import (
    read_lod_material_replacement_ids,
    read_render_layout,
)
from s2kit.pipelines.model_fidelity import resource_token
from s2kit.pipelines.porting import check_cancelled
from s2kit.pipelines.studiomdl import VTX_SUFFIXES, check_compiled_model


def _expanded_tables(materials: list[str], expected: list[dict], actual: list[dict]) -> tuple[list[dict], int]:
    """Require an existing exact binding before extending it to equivalent texture IDs."""
    if len(expected) != len(actual):
        raise ValueError("Compiled and generated LOD material table counts differ")
    identities = {}
    for index, material in enumerate(materials):
        identities.setdefault(resource_token(material), []).append(index)
    output, added = [], 0
    for lod, (level, current) in enumerate(zip(expected, actual)):
        bindings = {}
        for source, target in level["materials"].items():
            indices = identities.get(source, [])
            if not indices:
                raise ValueError(f"LOD {lod} material replacement source is absent: {source}")
            for index in indices:
                bindings[index] = target
        for index, target in current.items():
            if bindings.get(index) != target:
                raise ValueError(f"LOD {lod} texture ID {index} has an unexpected material replacement")
        for index, target in bindings.items():
            if index in current:
                continue
            equivalent = identities[resource_token(materials[index])]
            if len(equivalent) < 2 or not any(current.get(other) == target for other in equivalent):
                raise ValueError(f"LOD {lod} texture ID {index} lacks a certified duplicate material binding")
            added += 1
        output.append(bindings)
    return output, added


def _append_tables(original: bytes, tables: list[dict]) -> bytes:
    """Append packed v7 replacement records without relocating any rendered geometry."""
    data = bytearray(original)
    data.extend(b"\0" * (-len(data) % 4))
    start = len(data)
    data.extend(b"\0" * (8 * len(tables)))
    for lod, table in enumerate(tables):
        header = start + 8 * lod
        entries = len(data)
        struct.pack_into("<2i", data, header, len(table), entries - header)
        data.extend(b"\0" * (6 * len(table)))
        for offset, (index, target) in enumerate(sorted(table.items())):
            if not 0 <= index <= 32767 or "\0" in target:
                raise ValueError("LOD material replacement cannot fit the target binary format")
            record = entries + offset * 6
            encoded = target.encode("utf-8") + b"\0"
            struct.pack_into("<hi", data, record, index, len(data) - record)
            data.extend(encoded)
    if len(data) >= 2**31:
        raise ValueError("Expanded VTX material tables exceed signed 32-bit offsets")
    struct.pack_into("<i", data, 24, start)
    return bytes(data)


def _replace_files(prepared: dict[Path, tuple[bytes, bytes]], backup_dir: Path, cancel) -> None:
    """Keep exact originals and rollback the complete VTX cohort if replacement fails."""
    backups = {}
    temporary = {}
    replaced = []
    backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        for path, (original, rewritten) in prepared.items():
            check_cancelled(cancel)
            if path.read_bytes() != original:
                raise ValueError(f"Compiled VTX changed during material finalization: {path}")
            digest = hashlib.sha256(original).hexdigest()[:16]
            backup = backup_dir / (path.name + "." + digest + ".original")
            if backup.exists() and backup.read_bytes() != original:
                raise ValueError("Compiled material backup differs from its certified original")
            if not backup.exists():
                backup.write_bytes(original)
            backups[path] = backup
            staging = path.with_name(path.name + ".s2kit-materials.tmp")
            staging.write_bytes(rewritten)
            temporary[path] = staging
        check_cancelled(cancel)
        for path, staging in temporary.items():
            os.replace(staging, path)
            replaced.append(path)
            if path.read_bytes() != prepared[path][1]:
                raise ValueError(f"Written VTX material bindings differ from validated bytes: {path}")
    except BaseException:
        for path in reversed(replaced):
            recovery = path.with_name(path.name + ".s2kit-materials.rollback")
            recovery.write_bytes(backups[path].read_bytes())
            os.replace(recovery, path)
        raise
    finally:
        for staging in temporary.values():
            staging.unlink(missing_ok=True)


def finalize_lod_material_replacements(mdl: Path, qc: Path, *, backup_dir: Path | None = None,
                                       cancel=None, on_log=None) -> dict:
    """Restore identical-resource duplicate VTX bindings and reject unrelated compiler defects."""
    path = Path(mdl)
    components = check_compiled_model(path)
    if not components.ok:
        raise ValueError("Material finalization requires matching compiled components: " + "; ".join(components.problems()))
    metadata = read_compiled_model(path)
    expected = read_render_layout(Path(qc))["levels"]
    prepared, files = {}, []
    for suffix in VTX_SUFFIXES:
        check_cancelled(cancel)
        vtx = path.with_name(path.stem + suffix)
        if not vtx.is_file():
            continue
        original = vtx.read_bytes()
        actual = read_lod_material_replacement_ids(original, metadata["materials"])
        tables, added = _expanded_tables(metadata["materials"], expected, actual)
        rewritten = _append_tables(original, tables) if added else original
        if read_lod_material_replacement_ids(rewritten, metadata["materials"]) != tables:
            raise ValueError("Expanded VTX material bindings failed numeric round-trip validation")
        if rewritten[:24] != original[:24] or rewritten[28:len(original)] != original[28:]:
            raise ValueError("Material finalization changed original VTX geometry bytes")
        if struct.unpack_from("<I", rewritten, 16)[0] != metadata["checksum"]:
            raise ValueError("Material finalization changed the compiled checksum")
        if added:
            prepared[vtx] = original, rewritten
        files.append(dict(path=str(vtx), added_bindings=added, added_bytes=len(rewritten) - len(original),
                          original_sha256=hashlib.sha256(original).hexdigest(),
                          sha256=hashlib.sha256(rewritten).hexdigest()))
    directory = Path(backup_dir) if backup_dir is not None else path.parent / ".s2kit-material-originals"
    if prepared:
        _replace_files(prepared, directory, cancel)
        if on_log:
            on_log(f"Expanded {sum(record['added_bindings'] for record in files)} exact duplicate-ID LOD material bindings", "INFO")
    return dict(ok=True, errors=[], status="adapted" if prepared else "preserved", files=files,
                added_bindings=sum(record["added_bindings"] for record in files),
                backup_directory=str(directory) if prepared else None,
                geometry_bytes_unchanged=True, checksums_unchanged=True)
