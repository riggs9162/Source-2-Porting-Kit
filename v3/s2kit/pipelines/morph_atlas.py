"""Decode retained Source 2 morph atlases and validate their native vertex-buffer layout."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import numpy as np
from PIL import Image

from s2kit.pipelines.model_fidelity import resource_token
from s2kit.pipelines.model_metadata import parse_kv3_documents


_BUNDLES = {
    "MORPH_BUNDLE_TYPE_POSITION_SPEED": "POSITION",
    "MORPH_BUNDLE_TYPE_NORMAL_WRINKLE": "NORMAL",
}


def decode_morph_atlas(data: dict, pixels: np.ndarray) -> dict[str, dict[str, np.ndarray]]:
    """Reconstruct object-space deltas with VRF's float32 UNORM8 and clipped-rectangle semantics."""
    if data.get("m_nLookupType") != "LOOKUP_TYPE_VERTEX_ID":
        raise ValueError("Morph atlas requires vertex-ID lookup")
    if data.get("m_nEncodingType") != "ENCODING_TYPE_OBJECT_SPACE":
        raise ValueError("Morph atlas requires object-space encoding")
    width, height = int(data["m_nWidth"]), int(data["m_nHeight"])
    pixels = np.asarray(pixels)
    if width <= 0 or height <= 0 or pixels.dtype != np.uint8 or pixels.ndim != 3 or pixels.shape[2] != 4:
        raise ValueError("Invalid morph grid or lossless RGBA8 atlas")
    texture_height, texture_width = pixels.shape[:2]
    flat_pixels = pixels.reshape(-1, 4)
    bundle_types = data["m_bundleTypes"]
    output = {}
    for morph in data.get("m_morphDatas", []):
        name = morph.get("m_name", "")
        if not name:
            continue
        if name in output:
            raise ValueError(f"Duplicate morph atlas target {name!r}")
        channels = {channel: np.zeros((width * height, 4), dtype=np.float32)
                    for bundle, channel in _BUNDLES.items() if bundle in bundle_types}
        for rect in morph.get("m_morphRectDatas", []):
            rectangle_width = int(np.rint(np.float32(rect["m_flUWidthSrc"]) * np.float32(texture_width)))
            rectangle_height = int(np.rint(np.float32(rect["m_flVHeightSrc"]) * np.float32(texture_height)))
            bundles = rect.get("m_bundleDatas", [])
            if len(bundles) > len(bundle_types) or rectangle_width < 0 or rectangle_height < 0:
                raise ValueError("Invalid morph atlas rectangle or bundle count")
            rows, columns = np.mgrid[:rectangle_height, :rectangle_width]
            destination_rows = rows + int(rect["m_nYTopDst"])
            destination_columns = columns + int(rect["m_nXLeftDst"])
            destination_valid = ((destination_rows >= 0) & (destination_rows < height) &
                                 (destination_columns >= 0) & (destination_columns < width))
            for bundle_type, bundle in zip(bundle_types, bundles):
                channel = _BUNDLES.get(bundle_type)
                if channel is None:
                    continue
                source_u = int(np.rint(np.float32(bundle["m_flULeftSrc"]) * np.float32(texture_width)))
                source_v = int(np.rint(np.float32(bundle["m_flVTopSrc"]) * np.float32(texture_height)))
                source_indices = (rows + source_v) * texture_width + columns + source_u
                valid = destination_valid & (source_indices >= 0) & (source_indices < len(flat_pixels))
                ranges = np.asarray(bundle["m_ranges"], dtype=np.float32)
                offsets = np.asarray(bundle["m_offsets"], dtype=np.float32)
                if ranges.shape != (4,) or offsets.shape != (4,) or not np.isfinite([ranges, offsets]).all():
                    raise ValueError("Invalid morph atlas quantization parameters")
                values = flat_pixels[source_indices[valid]].astype(np.float32) / np.float32(255)
                values = values * ranges + offsets
                destinations = destination_rows[valid] * width + destination_columns[valid]
                channels[channel][destinations] = values
        output[name] = channels
    return output


def _mesh_mdat(fidelity: dict, mesh: dict) -> dict:
    """Resolve the mesh's exact MDAT block from retained typed or lossless raw documents."""
    documents = fidelity.get("source_mdat")
    if documents is None:
        relative = fidelity.get("raw_blocks", {}).get("MDAT")
        if not relative:
            raise ValueError("Morph mapping requires retained MDAT draw calls")
        path = Path(fidelity["_sidecar_directory"]) / relative
        documents = parse_kv3_documents(path.read_text(encoding="utf-8"))
        fidelity["source_mdat"] = documents
    blocks = [block["index"] for block in fidelity["block_table"] if block["type"] == "MDAT"]
    if len(blocks) != len(documents) or mesh["data_block"] not in blocks:
        raise ValueError("Morph MDAT document count or block identity differs from the resource")
    return documents[blocks.index(mesh["data_block"])]


def morph_atlas_path(fidelity: dict, data: dict) -> Path:
    """Resolve the retained atlas resource beneath the source model's export directory."""
    resource = PurePosixPath(str(data["m_pTextureAtlas"]).replace("\\", "/"))
    parent = PurePosixPath(fidelity["resource"]).parent
    if resource.is_absolute() or ".." in resource.parts:
        raise ValueError("Unsafe morph atlas resource path")
    relative = resource.relative_to(parent).with_suffix(".png")
    return Path(fidelity["_sidecar_directory"]) / relative


def load_mesh_morphs(fidelity: dict, mesh: dict) -> tuple[dict, dict, Path] | None:
    """Read a mesh's exact retained PNG atlas without reexporting its source resource."""
    morph_index = mesh.get("morph_index")
    if morph_index is None:
        return None
    data = fidelity["morphs"][int(morph_index)]
    if not any(morph.get("m_name") for morph in data.get("m_morphDatas", [])):
        return None
    path = morph_atlas_path(fidelity, data)
    with Image.open(path) as image:
        if image.mode != "RGBA":
            raise ValueError(f"Morph atlas must retain lossless RGBA channels: {path}")
        decoded = decode_morph_atlas(data, np.asarray(image))
    return decoded, _mesh_mdat(fidelity, mesh), path


def morph_buffer_layout(mdat: dict) -> list[dict]:
    """Derive first-use buffer offsets from the original ordered mesh draw calls."""
    buffers = {}
    offset = 0
    for scene_object in mdat.get("m_sceneObjects", []):
        for draw in scene_object.get("m_drawCalls", []):
            handles = tuple(int(buffer["m_hBuffer"]) for buffer in draw["m_vertexBuffers"])
            if not 1 <= len(handles) <= 2:
                raise ValueError("Unsupported morph vertex-buffer layout")
            if handles not in buffers:
                buffers[handles] = dict(offset=offset, extent=0, materials=[])
            entry = buffers[handles]
            entry["extent"] = max(entry["extent"], int(draw["m_nBaseVertex"]) + int(draw["m_nVertexCount"]))
            material = draw.get("m_material", draw.get("m_pMaterial"))
            if not isinstance(material, str):
                raise ValueError("Morph draw call has no unambiguous source material")
            entry["materials"].append(resource_token(material))
            offset += int(draw["m_nVertexCount"])
    return list(buffers.values())


def morph_buffer_slices(meshes: list, mdat: dict, vertex_count: int) -> list[tuple[int, int]]:
    """Match native DMX buffer creation order, material membership and draw-call extents to morph offsets."""
    entries = morph_buffer_layout(mdat)
    if len(meshes) == 1 and len(entries) > 1:
        entries = [dict(offset=0, extent=sum(entry["extent"] for entry in entries),
                        materials=[material for entry in entries for material in entry["materials"]])]
    if len(meshes) != len(entries):
        raise ValueError("Native DMX mesh count differs from morph draw-call buffers")
    slices = []
    for mesh, entry in zip(meshes, entries):
        state = mesh["currentState"]
        key = "position$0" if "position$0" in state else "positions"
        count = len(state[key])
        materials = [resource_token(face["material"]["mtlName"]) for face in mesh["faceSets"]]
        if count != entry["extent"] or sorted(materials) != sorted(entry["materials"]):
            raise ValueError("Native DMX vertex extent or material membership differs from morph draw calls")
        if entry["offset"] < 0 or entry["offset"] + count > vertex_count:
            raise ValueError("Native DMX vertices exceed their authored morph grid")
        slices.append((entry["offset"], count))
    return slices
