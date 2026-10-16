"""Isolate compiled LOD vertex pools while preserving every vertex attribute byte."""

from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


VERTEX_DTYPE = np.dtype({
    "names": ["weights", "bones", "bone_count", "position", "normal", "uv"],
    "formats": [("<f4", 3), ("u1", 3), "u1", ("<f4", 3), ("<f4", 3), ("<f4", 2)],
    "offsets": [0, 12, 15, 16, 28, 40],
    "itemsize": 48,
})


class CompiledVertexError(ValueError):
    """A compiled model cannot be safely read or rewritten without guessing its layout."""


@dataclass
class CompiledStrip:
    """One rendered VTX primitive range and its strip-group vertex indices."""

    offset: int
    flags: int
    first_index: int
    index_count: int
    first_vertex: int
    vertex_count: int
    indices: np.ndarray


@dataclass
class CompiledStripGroup:
    """One packed VTX strip group and its original mesh-local vertex references."""

    lod: int
    offset: int
    flags: int
    vertex_offsets: list[int]
    original_ids: list[int]
    indices: np.ndarray
    index_offset: int
    strips: list[CompiledStrip] = field(default_factory=list)


@dataclass
class CompiledMesh:
    """A material mesh with logical root-zero VVD offsets and VTX LOD references."""

    part: int
    choice: int
    mesh: int
    mdl_offset: int
    model_offset: int
    model_vertex_base: int
    mesh_vertex_base: int
    vertex_count: int
    material_slot: int
    lod_counts: tuple[int, ...]
    lod_used: dict[int, list[int]] = field(default_factory=dict)
    lod_vertices: dict[int, list[int]] = field(default_factory=dict)
    lod_triangles: dict[int, np.ndarray] = field(default_factory=dict)
    lod_triangle_vertex_offsets: dict[int, np.ndarray] = field(default_factory=dict)
    strip_groups: list[CompiledStripGroup] = field(default_factory=list)

    @property
    def key(self) -> tuple[int, int, int]:
        """Return the bodypart, model choice and material mesh identity."""
        return self.part, self.choice, self.mesh

    @property
    def vertex_base(self) -> int:
        """Return this mesh's first logical root-zero VVD vertex."""
        return self.model_vertex_base + self.mesh_vertex_base


@dataclass
class CompiledModel:
    """An MDL bodygroup choice and its contiguous root-zero vertex range."""

    part: int
    choice: int
    mdl_offset: int
    vertex_base: int
    vertex_count: int
    meshes: list[CompiledMesh] = field(default_factory=list)


@dataclass
class CompiledVertices:
    """Validated MDL/VVD/VTX bytes and root-zero structured vertex and tangent arrays."""

    mdl_path: Path
    vvd_path: Path
    vtx_path: Path
    mdl_bytes: bytes
    vvd_bytes: bytes
    vtx_bytes: bytes
    vertices: np.ndarray
    tangents: np.ndarray
    models: list[CompiledModel]
    meshes: list[CompiledMesh]
    lod_count: int
    checksum: int
    strip_group_size: int
    strip_size: int
    fixup_count: int


@dataclass
class LodVertexSplitResult:
    """Original provenance, isolated mesh-local IDs, output layout and measured costs."""

    original: CompiledVertices
    compiled: CompiledVertices
    mapping: dict[tuple[int, int, int, int, int], int]
    reverse: dict[tuple[int, int, int, int], list[tuple[int, int, int, int, int]]]
    report: dict

    @property
    def vertices(self) -> np.ndarray:
        """Return original logical root-zero vertex rows for source correspondence."""
        return self.original.vertices

    @property
    def meshes(self) -> list[CompiledMesh]:
        """Return the rewritten mesh offsets and LOD references."""
        return self.compiled.meshes

    @property
    def models(self) -> list[CompiledModel]:
        """Return the rewritten bodygroup model offsets."""
        return self.compiled.models


def _require(data: bytes, offset: int, size: int, label: str) -> None:
    """Validate a complete nonnegative range before unpacking or slicing it."""
    if offset < 0 or size < 0 or offset + size > len(data):
        raise CompiledVertexError(f"{label}: range {offset}+{size} exceeds {len(data)} bytes")


def _unpack(data: bytes, fmt: str, offset: int, label: str) -> tuple:
    """Read little-endian packed values after validating their complete range."""
    _require(data, offset, struct.calcsize("<" + fmt), label)
    return struct.unpack_from("<" + fmt, data, offset)


def _table(data: bytes, count: int, offset: int, size: int, label: str) -> range:
    """Validate a counted packed table and return its record offsets."""
    if count < 0 or count > len(data) // max(size, 1):
        raise CompiledVertexError(f"{label}: invalid count {count}")
    _require(data, offset, count * size, label)
    return range(offset, offset + count * size, size)


def _vvd_arrays(data: bytes, checksum: int) -> tuple[np.ndarray, np.ndarray, int, int]:
    """Reconstruct logical root-zero arrays using the file's ordered VVD fixups."""
    _require(data, 0, 64, "VVD header")
    magic, version, actual_checksum, lod_count = _unpack(data, "4sIII", 0, "VVD header")
    if magic != b"IDSV" or version != 4 or actual_checksum != checksum:
        raise CompiledVertexError("Expected a checksum-matched version-four IDSV vertex file")
    if not 1 <= lod_count <= 8:
        raise CompiledVertexError(f"VVD has invalid LOD count {lod_count}")
    counts = _unpack(data, "8I", 16, "VVD LOD counts")
    if not counts[0] or any(counts[index] > counts[index - 1] for index in range(1, 8)):
        raise CompiledVertexError("VVD LOD vertex counts are empty or increasing")
    fixup_count, fixup_offset, vertex_offset, tangent_offset = _unpack(data, "4I", 48, "VVD tables")
    _require(data, vertex_offset, counts[0] * 48, "VVD vertex rows")
    _require(data, tangent_offset, counts[0] * 16, "VVD tangent rows")
    if vertex_offset < 64 or tangent_offset < vertex_offset + counts[0] * 48:
        raise CompiledVertexError("VVD vertex and tangent arrays overlap")
    vertices = np.frombuffer(data, dtype=VERTEX_DTYPE, count=counts[0], offset=vertex_offset)
    tangents = np.frombuffer(data, dtype="<f4", count=counts[0] * 4, offset=tangent_offset).reshape(-1, 4)
    if fixup_count:
        indices = []
        per_lod = np.zeros(8, dtype=np.int64)
        for offset in _table(data, fixup_count, fixup_offset, 12, "VVD fixups"):
            lod, start, count = _unpack(data, "3I", offset, "VVD fixup")
            if lod >= lod_count or start + count > counts[0]:
                raise CompiledVertexError("VVD fixup references an absent LOD or vertex range")
            indices.extend(range(start, start + count))
            per_lod[:lod + 1] += count
        if len(indices) != counts[0] or len(set(indices)) != counts[0]:
            raise CompiledVertexError("VVD root-zero fixups do not partition the original vertex rows")
        if tuple(per_lod[:lod_count]) != counts[:lod_count]:
            raise CompiledVertexError("VVD fixup counts disagree with the header")
        vertices = vertices[indices]
        tangents = tangents[indices]
    if not np.isfinite(vertices["position"]).all() or not np.isfinite(vertices["normal"]).all():
        raise CompiledVertexError("VVD contains nonfinite positions or normals")
    if not np.isfinite(vertices["weights"]).all() or not np.isfinite(vertices["uv"]).all() or not np.isfinite(tangents).all():
        raise CompiledVertexError("VVD contains nonfinite weights, UVs or tangents")
    if (vertices["bone_count"] > 3).any() or (vertices["weights"] < 0).any():
        raise CompiledVertexError("VVD contains invalid Source-one skin weights")
    return vertices, tangents, lod_count, fixup_count


def _mdl_models(data: bytes, vertex_count: int) -> tuple[list[CompiledModel], list[CompiledMesh], list[int]]:
    """Read packed v48/v49 bodygroups, model ranges and 116-byte material meshes."""
    _require(data, 0, 240, "MDL header")
    if data[:4] != b"IDST" or _unpack(data, "I", 4, "MDL version")[0] not in {48, 49}:
        raise CompiledVertexError("Compiled vertex rewriting requires a v48/v49 IDST model")
    if _unpack(data, "I", 76, "MDL length")[0] != len(data):
        raise CompiledVertexError("MDL declared length differs from its byte count")
    part_count, part_index = _unpack(data, "2I", 232, "MDL bodyparts")
    models, meshes, choices = [], [], []
    cursor = 0
    for part, part_offset in enumerate(_table(data, part_count, part_index, 16, "MDL bodyparts")):
        model_count = _unpack(data, "I", part_offset + 4, "MDL choice count")[0]
        model_index = _unpack(data, "i", part_offset + 12, "MDL model index")[0]
        choices.append(model_count)
        for choice, offset in enumerate(_table(data, model_count, part_offset + model_index, 148, "MDL models")):
            mesh_count, mesh_index, count, vertex_index, tangent_index = _unpack(data, "5i", offset + 72, "MDL model")
            if count < 0 or vertex_index < 0 or vertex_index % 48 or tangent_index < 0 or tangent_index % 16:
                raise CompiledVertexError("MDL model has invalid vertex or tangent offsets")
            base = vertex_index // 48
            if count and (base != cursor or tangent_index // 16 != base):
                raise CompiledVertexError("MDL nonempty model vertex ranges are not contiguous root-zero rows")
            model = CompiledModel(part, choice, offset, base, count)
            mesh_cursor = 0
            for index, mesh_offset in enumerate(_table(data, mesh_count, offset + mesh_index, 116, "MDL meshes")):
                material, back, mesh_vertices, mesh_base = _unpack(data, "4i", mesh_offset, "MDL mesh")
                if back != offset - mesh_offset or mesh_vertices < 0 or mesh_base != mesh_cursor:
                    raise CompiledVertexError("MDL mesh backreference or contiguous vertex range is invalid")
                lod_counts = _unpack(data, "8I", mesh_offset + 52, "MDL mesh LOD counts")
                if lod_counts[0] != mesh_vertices or any(value > mesh_vertices for value in lod_counts) or any(
                        lod_counts[index] > lod_counts[index - 1] for index in range(1, 8)):
                    raise CompiledVertexError("MDL mesh LOD counts exceed the mesh vertex range")
                mesh = CompiledMesh(part, choice, index, mesh_offset, offset, base, mesh_base,
                                    mesh_vertices, material, lod_counts)
                model.meshes.append(mesh)
                meshes.append(mesh)
                mesh_cursor += mesh_vertices
            if mesh_cursor != count or base + count > vertex_count:
                raise CompiledVertexError("MDL model and mesh vertex totals disagree with the VVD")
            if count:
                cursor += count
            models.append(model)
    if cursor != vertex_count:
        raise CompiledVertexError("MDL mesh ranges do not account for every VVD vertex")
    return models, meshes, choices


def _read_vtx(data: bytes, models: list[CompiledModel], choices: list[int], checksum: int,
              lod_count: int, group_size: int, strip_size: int) -> None:
    """Read packed VTX references using an explicitly selected strip-group layout."""
    _require(data, 0, 36, "VTX header")
    version = _unpack(data, "I", 0, "VTX version")[0]
    actual_checksum, actual_lods, _, part_count, part_index = _unpack(data, "5I", 16, "VTX header")
    if version != 7 or actual_checksum != checksum or actual_lods != lod_count or part_count != len(choices):
        raise CompiledVertexError("VTX version, checksum, LOD or bodypart count differs from MDL/VVD")
    by_model = {(model.part, model.choice): model for model in models}
    for part, part_offset in enumerate(_table(data, part_count, part_index, 8, "VTX bodyparts")):
        count, relative = _unpack(data, "2i", part_offset, "VTX bodypart")
        if count != choices[part]:
            raise CompiledVertexError("VTX model choices differ from the MDL bodygroups")
        for choice, model_offset in enumerate(_table(data, count, part_offset + relative, 8, "VTX models")):
            model = by_model[part, choice]
            count, relative = _unpack(data, "2i", model_offset, "VTX model")
            if count != lod_count:
                raise CompiledVertexError("VTX model has a different LOD count")
            for lod, lod_offset in enumerate(_table(data, count, model_offset + relative, 12, "VTX LODs")):
                mesh_count, relative = _unpack(data, "2i", lod_offset, "VTX LOD")
                if mesh_count != len(model.meshes):
                    raise CompiledVertexError("VTX LOD mesh count differs from the MDL")
                for mesh, mesh_offset in zip(model.meshes, _table(data, mesh_count, lod_offset + relative, 9, "VTX meshes")):
                    group_count, relative = _unpack(data, "2i", mesh_offset, "VTX mesh")
                    all_ids, used_ids, triangles, corner_offsets = set(), set(), [], []
                    for group_offset in _table(data, group_count, mesh_offset + relative, group_size, "VTX strip groups"):
                        vertices, vertex_index, indices, index_index, strips, strip_index, flags = _unpack(data, "6iB", group_offset, "VTX strip group")
                        vertex_offsets = list(_table(data, vertices, group_offset + vertex_index, 9, "VTX vertices"))
                        original_ids = [_unpack(data, "H", offset + 4, "VTX mesh vertex")[0] for offset in vertex_offsets]
                        if any(index >= mesh.lod_counts[lod] for index in original_ids):
                            raise CompiledVertexError("VTX vertex references an absent MDL mesh vertex")
                        _require(data, group_offset + index_index, indices * 2, "VTX indices")
                        index_array = np.frombuffer(data, dtype="<u2", count=indices, offset=group_offset + index_index)
                        if indices and int(index_array.max()) >= vertices:
                            raise CompiledVertexError("VTX index references an absent strip-group vertex")
                        strip_records = []
                        for offset in _table(data, strips, group_offset + strip_index, strip_size, "VTX strips"):
                            strip_indices, first_index, strip_vertices, first_vertex = _unpack(data, "4i", offset, "VTX strip")
                            if min(strip_indices, first_index, strip_vertices, first_vertex) < 0 or first_index + strip_indices > indices or first_vertex + strip_vertices > vertices:
                                raise CompiledVertexError("VTX strip range exceeds its group")
                            changes, change_index = _unpack(data, "2i", offset + 19, "VTX bone state changes")
                            _table(data, changes, offset + change_index, 8, "VTX bone state changes")
                            strip_flags = data[offset + 18]
                            selected = index_array[first_index:first_index + strip_indices]
                            if strip_flags == 1:
                                if len(selected) % 3:
                                    raise CompiledVertexError("VTX triangle-list strip has an incomplete triangle")
                                primitive_triangles = selected.reshape(-1, 3)
                            elif strip_flags == 2:
                                primitive_triangles = []
                                for index in range(max(0, len(selected) - 2)):
                                    triangle = [int(value) for value in selected[index:index + 3]]
                                    if index % 2:
                                        triangle[0], triangle[1] = triangle[1], triangle[0]
                                    if len(set(triangle)) == 3:
                                        primitive_triangles.append(triangle)
                                primitive_triangles = np.asarray(primitive_triangles, dtype=np.int64).reshape(-1, 3)
                            else:
                                raise CompiledVertexError(f"Unsupported VTX primitive flags {strip_flags}")
                            for triangle in primitive_triangles:
                                triangles.append([original_ids[int(index)] for index in triangle])
                                corner_offsets.append([vertex_offsets[int(index)] for index in triangle])
                            strip_records.append(CompiledStrip(offset, strip_flags, first_index, strip_indices,
                                                               first_vertex, strip_vertices, selected))
                        if group_size == 33:
                            topology_count, topology_index = _unpack(data, "2i", group_offset + 25, "VTX topology")
                            _table(data, topology_count, group_offset + topology_index, 2, "VTX topology indices")
                        group = CompiledStripGroup(lod, group_offset, flags, vertex_offsets,
                                                   original_ids, index_array, group_offset + index_index,
                                                   strip_records)
                        mesh.strip_groups.append(group)
                        all_ids.update(original_ids)
                        used_ids.update(value for triangle in triangles for value in triangle)
                    mesh.lod_vertices[lod] = sorted(all_ids)
                    mesh.lod_used[lod] = sorted(used_ids)
                    mesh.lod_triangles[lod] = np.asarray(triangles, dtype=np.int64).reshape(-1, 3)
                    mesh.lod_triangle_vertex_offsets[lod] = np.asarray(corner_offsets, dtype=np.int64).reshape(-1, 3)


def _read_bytes(mdl_path: Path, mdl: bytes, vvd: bytes, vtx: bytes, *, strip_group_size: int = 33,
                strip_size: int = 35) -> CompiledVertices:
    """Validate a coherent compiler-owned byte triplet without opening output files."""
    checksum = _unpack(mdl, "I", 8, "MDL checksum")[0]
    vertices, tangents, lod_count, fixup_count = _vvd_arrays(vvd, checksum)
    models, meshes, choices = _mdl_models(mdl, len(vertices))
    counts = _unpack(vvd, "8I", 16, "VVD LOD counts")
    if any(sum(mesh.lod_counts[lod] for mesh in meshes) != counts[lod] for lod in range(8)):
        raise CompiledVertexError("MDL mesh LOD totals disagree with the VVD header")
    _read_vtx(vtx, models, choices, checksum, lod_count, strip_group_size, strip_size)
    return CompiledVertices(mdl_path, mdl_path.with_suffix(".vvd"), mdl_path.with_suffix(".dx90.vtx"),
                            mdl, vvd, vtx, vertices, tangents, models, meshes, lod_count,
                            checksum, strip_group_size, strip_size, fixup_count)


def read_compiled_vertices(mdl_path: Path, *, strip_group_size: int = 33,
                           strip_size: int = 35) -> CompiledVertices:
    """Read v48/v49 MDL, version-four VVD and CE's 33/35-byte extended DX90 VTX layout."""
    if (strip_group_size, strip_size) not in {(33, 35), (25, 27)}:
        raise CompiledVertexError("Unsupported packed VTX strip-group/strip record sizes")
    mdl_path = Path(mdl_path)
    return _read_bytes(mdl_path, mdl_path.read_bytes(), mdl_path.with_suffix(".vvd").read_bytes(),
                       mdl_path.with_suffix(".dx90.vtx").read_bytes(),
                       strip_group_size=strip_group_size, strip_size=strip_size)


def _replace_files(original: CompiledVertices, rewritten: CompiledVertices, backup_dir: Path | None) -> list[str]:
    """Preserve exact originals and replace a fully validated triplet with rollback on failure."""
    pairs = [(original.mdl_path, original.mdl_bytes, rewritten.mdl_bytes),
             (original.vvd_path, original.vvd_bytes, rewritten.vvd_bytes),
             (original.vtx_path, original.vtx_bytes, rewritten.vtx_bytes)]
    identity = hashlib.sha256(b"".join(old for _, old, _ in pairs)).hexdigest()[:20]
    backup_dir = Path(backup_dir) if backup_dir else original.mdl_path.parent / ".s2kit-lod-originals"
    backup_dir = backup_dir / identity
    backup_dir.mkdir(parents=True, exist_ok=True)
    temporaries = []
    replaced = []
    try:
        for path, old, new in pairs:
            if path.read_bytes() != old:
                raise CompiledVertexError(f"Compiled component changed before replacement: {path}")
            backup = backup_dir / path.name
            if backup.exists() and backup.read_bytes() != old:
                raise CompiledVertexError(f"Original byte backup conflicts with {backup}")
            if not backup.exists():
                backup.write_bytes(old)
            temporary = path.with_name(path.name + ".s2kit-lod-tmp")
            temporary.write_bytes(new)
            temporaries.append((path, temporary, old))
        for path, temporary, old in temporaries:
            os.replace(temporary, path)
            replaced.append((path, old))
    except Exception:
        for path, old in reversed(replaced):
            path.write_bytes(old)
        raise
    finally:
        for _, temporary, _ in temporaries:
            if temporary.exists():
                temporary.unlink()
    return [str(backup_dir / path.name) for path, _, _ in pairs]


def write_compiled_vertices(original: CompiledVertices, rewritten: CompiledVertices, *,
                            backup_dir: Path | None = None) -> list[str]:
    """Validate a prepared triplet and replace it once, retaining exact originals and rollback bytes."""
    if (original.mdl_path, original.vvd_path, original.vtx_path) != (
            rewritten.mdl_path, rewritten.vvd_path, rewritten.vtx_path):
        raise CompiledVertexError("Original and rewritten compiled paths differ")
    checked = _read_bytes(rewritten.mdl_path, rewritten.mdl_bytes, rewritten.vvd_bytes,
                          rewritten.vtx_bytes, strip_group_size=rewritten.strip_group_size,
                          strip_size=rewritten.strip_size)
    if checked.checksum != original.checksum or checked.lod_count != original.lod_count:
        raise CompiledVertexError("Prepared vertex output changed checksum or LOD identity")
    return _replace_files(original, checked, backup_dir)


def duplicate_lod_vertices(mdl_path: Path, *, flexed_meshes: set[tuple[int, int, int]] | None = None,
                           backup_dir: Path | None = None, write: bool = True,
                           strip_group_size: int = 33, strip_size: int = 35) -> LodVertexSplitResult:
    """Give each lower LOD independent exact vertex copies and retain the union at every root LOD.

    Original LOD-zero mesh IDs remain valid. Each selected mesh receives byte-identical
    copies for lower-LOD VTX references. Source flex restoration must replace its old
    mesh flex records before the rewritten model is used in the engine.
    """
    original = read_compiled_vertices(mdl_path, strip_group_size=strip_group_size, strip_size=strip_size)
    selected = {mesh.key for mesh in original.meshes} if flexed_meshes is None else set(flexed_meshes)
    absent = selected - {mesh.key for mesh in original.meshes}
    if absent:
        raise CompiledVertexError(f"Selected flex meshes are absent: {sorted(absent)}")
    mdl, vtx = bytearray(original.mdl_bytes), bytearray(original.vtx_bytes)
    vertex_rows, tangent_rows = [], []
    mapping, reverse, mesh_reports = {}, {}, []
    global_cursor = 0
    for model in original.models:
        model_base = global_cursor
        mesh_cursor = 0
        for mesh in model.meshes:
            old = np.arange(mesh.vertex_count, dtype=np.int64)
            local_maps = {0: {int(index): int(index) for index in old}}
            source_rows = list(old)
            for lod in range(1, original.lod_count):
                if mesh.key in selected:
                    ids = mesh.lod_vertices[lod]
                    local_maps[lod] = {index: len(source_rows) + ordinal for ordinal, index in enumerate(ids)}
                    source_rows.extend(ids)
                else:
                    local_maps[lod] = {index: index for index in mesh.lod_vertices[lod]}
            if len(source_rows) > 65536:
                raise CompiledVertexError(f"Mesh {mesh.key} requires {len(source_rows)} vertices, beyond unsigned 16-bit VTX IDs")
            rows = mesh.vertex_base + np.asarray(source_rows, dtype=np.int64)
            vertex_rows.append(original.vertices[rows].tobytes())
            tangent_rows.append(original.tangents[rows].tobytes())
            struct.pack_into("<2i", mdl, mesh.mdl_offset + 8, len(rows), mesh_cursor)
            struct.pack_into("<8i", mdl, mesh.mdl_offset + 52, *([len(rows)] * 8))
            for lod, ids in mesh.lod_vertices.items():
                for old_id in ids:
                    new_id = local_maps[lod][old_id]
                    key = (*mesh.key, lod, old_id)
                    mapping[key] = new_id
                    reverse.setdefault((*mesh.key, new_id), []).append(key)
            for group in mesh.strip_groups:
                if mesh.key in selected:
                    vtx[group.offset + 24] = group.flags | 4
                for offset, old_id in zip(group.vertex_offsets, group.original_ids):
                    struct.pack_into("<H", vtx, offset + 4, local_maps[group.lod][old_id])
            mesh_reports.append(dict(mesh=list(mesh.key), original_vertices=mesh.vertex_count,
                                     vertices=len(rows), added_vertices=len(rows) - mesh.vertex_count,
                                     split=mesh.key in selected, global_vertex_base=global_cursor,
                                     model_vertex_base=model_base, mesh_vertex_base=mesh_cursor,
                                     lod_used={str(lod): len(ids) for lod, ids in mesh.lod_used.items()}))
            global_cursor += len(rows)
            mesh_cursor += len(rows)
        struct.pack_into("<3i", mdl, model.mdl_offset + 80, mesh_cursor, model_base * 48, model_base * 16)
    vertex_data, tangent_data = b"".join(vertex_rows), b"".join(tangent_rows)
    if global_cursor > 0x7FFFFFFF // 64:
        raise CompiledVertexError("Rewritten VVD exceeds signed Source-one file offsets")
    header = bytearray(original.vvd_bytes[:64])
    struct.pack_into("<8I", header, 16, *([global_cursor] * 8))
    struct.pack_into("<4I", header, 48, 0, 64, 64, 64 + len(vertex_data))
    vvd = bytes(header) + vertex_data + tangent_data
    compiled = _read_bytes(original.mdl_path, bytes(mdl), vvd, bytes(vtx),
                           strip_group_size=strip_group_size, strip_size=strip_size)
    checked = 0
    for old_mesh, new_mesh in zip(original.meshes, compiled.meshes):
        for old_group, new_group in zip(old_mesh.strip_groups, new_mesh.strip_groups):
            if old_group.lod != new_group.lod or not np.array_equal(old_group.indices, new_group.indices):
                raise CompiledVertexError("VTX strip topology changed during vertex isolation")
            old_rows = old_mesh.vertex_base + np.asarray(old_group.original_ids, dtype=np.int64)
            new_rows = new_mesh.vertex_base + np.asarray(new_group.original_ids, dtype=np.int64)
            if original.vertices[old_rows].tobytes() != compiled.vertices[new_rows].tobytes():
                raise CompiledVertexError("VTX-referenced vertex attributes changed during isolation")
            if original.tangents[old_rows].tobytes() != compiled.tangents[new_rows].tobytes():
                raise CompiledVertexError("VTX-referenced tangent attributes changed during isolation")
            checked += len(old_rows)
    report = dict(status="isolated", method="independent_lod_vertex_pools_with_constant_root_lod_union",
                  original_vertices=len(original.vertices), vertices=global_cursor,
                  added_vertices=global_cursor - len(original.vertices),
                  added_vertex_buffer_bytes=(global_cursor - len(original.vertices)) * 64,
                  lods=original.lod_count, original_fixups=original.fixup_count, fixups=0,
                  checked_strip_vertex_records=checked, vertex_attributes_byte_identical=True,
                  tangent_attributes_byte_identical=True, triangle_indices_byte_identical=True,
                  meshes=mesh_reports, originals=[])
    if write:
        report["originals"] = write_compiled_vertices(original, compiled, backup_dir=backup_dir)
    return LodVertexSplitResult(original, compiled, mapping, reverse, report)
