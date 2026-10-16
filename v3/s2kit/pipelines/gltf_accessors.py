"""Decode bounded glTF accessors, including sparse morphs and normalized weights."""

from __future__ import annotations

import numpy as np


_DTYPES = {
    5120: np.dtype("i1"),
    5121: np.dtype("u1"),
    5122: np.dtype("<i2"),
    5123: np.dtype("<u2"),
    5125: np.dtype("<u4"),
    5126: np.dtype("<f4"),
}

_SHAPES = {
    "SCALAR": (1, 1),
    "VEC2": (1, 2),
    "VEC3": (1, 3),
    "VEC4": (1, 4),
    "MAT2": (2, 2),
    "MAT3": (3, 3),
    "MAT4": (4, 4),
}


def _stream(document: dict, buffer: bytes, view_index: int, offset: int,
            count: int, dtype: np.dtype, columns: int, rows: int,
            *, tightly_packed: bool = False) -> np.ndarray:
    """Read one accessor stream within its declared buffer view."""
    views = document.get("bufferViews", [])
    if not 0 <= view_index < len(views):
        raise ValueError(f"Invalid glTF buffer view {view_index}")
    view = views[view_index]
    if int(view.get("buffer", 0)) != 0:
        raise ValueError("This model reader requires glTF buffer zero")
    column_size = rows * dtype.itemsize
    column_stride = (column_size + 3) // 4 * 4 if columns > 1 else column_size
    item_size = columns * column_stride
    stride = item_size if tightly_packed else int(view.get("byteStride", item_size))
    view_start = int(view.get("byteOffset", 0))
    view_length = int(view.get("byteLength", -1))
    required = 0 if count == 0 else (count - 1) * stride + item_size
    if min(offset, count, view_start, view_length) < 0 or stride < item_size:
        raise ValueError("Invalid glTF accessor offset, count or stride")
    if offset + required > view_length or view_start + view_length > len(buffer):
        raise ValueError("glTF accessor extends beyond its buffer view")
    if count == 0:
        return np.empty((0, columns * rows), dtype=dtype)
    data = np.ndarray(
        shape=(count, columns, rows), dtype=dtype, buffer=buffer,
        offset=view_start + offset, strides=(stride, column_stride, dtype.itemsize),
    )
    return data.reshape(count, columns * rows).copy()


def decode_accessor(document: dict, buffer: bytes, index: int) -> np.ndarray:
    """Return SCALAR as a vector and other glTF accessor types as row arrays."""
    accessors = document.get("accessors", [])
    if not 0 <= index < len(accessors):
        raise ValueError(f"Invalid glTF accessor {index}")
    accessor = accessors[index]
    component_type = int(accessor["componentType"])
    if component_type not in _DTYPES or accessor["type"] not in _SHAPES:
        raise ValueError(f"Unsupported glTF accessor representation at {index}")
    dtype = _DTYPES[component_type]
    columns, rows = _SHAPES[accessor["type"]]
    count = int(accessor["count"])
    if count < 0:
        raise ValueError("Negative glTF accessor count")
    if "bufferView" in accessor:
        data = _stream(document, buffer, int(accessor["bufferView"]),
                       int(accessor.get("byteOffset", 0)), count, dtype, columns, rows)
    else:
        if int(accessor.get("byteOffset", 0)) != 0:
            raise ValueError("A glTF accessor without a buffer view cannot have an offset")
        data = np.zeros((count, columns * rows), dtype=dtype)
    sparse = accessor.get("sparse")
    if sparse:
        sparse_count = int(sparse["count"])
        if not 0 <= sparse_count <= count:
            raise ValueError("Invalid sparse glTF accessor count")
        indices = sparse["indices"]
        index_type = int(indices["componentType"])
        if index_type not in (5121, 5123, 5125):
            raise ValueError("Sparse glTF indices must be unsigned integers")
        selection = _stream(document, buffer, int(indices["bufferView"]),
                            int(indices.get("byteOffset", 0)), sparse_count,
                            _DTYPES[index_type], 1, 1, tightly_packed=True).reshape(-1)
        if sparse_count and (selection[-1] >= count or np.any(selection[1:] <= selection[:-1])):
            raise ValueError("Sparse glTF indices must increase within the accessor")
        values = sparse["values"]
        overrides = _stream(document, buffer, int(values["bufferView"]),
                            int(values.get("byteOffset", 0)), sparse_count,
                            dtype, columns, rows, tightly_packed=True)
        data[selection] = overrides
    if accessor.get("normalized") and dtype.kind in "iu":
        limits = np.iinfo(dtype)
        data = data.astype(np.float64) / limits.max
        if dtype.kind == "i":
            data = np.maximum(data, -1.0)
    return data[:, 0] if accessor["type"] == "SCALAR" else data
