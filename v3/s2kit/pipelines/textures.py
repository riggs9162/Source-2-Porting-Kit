"""
Texture decode, transfer functions and VTF encoding.

The pixel plumbing every material tool sits on. Nothing here knows about PBR,
materials or Flet - it loads an image into a float32 array, moves it between
sRGB and linear, resizes it, and writes it back out as a VTF.

Two things in here are load-bearing and easy to undo by accident:

* **RGB and alpha are always resized separately.** PIL premultiplies RGB by
  alpha when it interpolates a combined RGBA buffer, which zeroes the RGB
  wherever alpha is 0. Our bumpmaps carry the Phong mask in alpha and that mask
  is 0 across flat dielectric regions, so a naive resize decodes as solid black
  in exactly the areas that should be flat tangent normal.
* **The transfer functions run in row blocks.** ``np.where`` materialises both
  branches at full size; on a 4096x4096 RGB buffer that is five full-size
  float32 temporaries (~1 GiB) for a 192 MiB result. Blocking caps the
  temporaries at a few MiB and produces identical output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np

#: Row-block height for the chunked transfer functions. Large enough that
#: numpy's per-call overhead vanishes, small enough that a block's temporaries
#: stay in the low tens of MiB even at 4K width.
CHUNK_ROWS = 256

#: Extensions the material scanners consider to be source textures.
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tga", ".bmp", ".tif", ".tiff")


class TextureError(RuntimeError):
    """Raised when a texture cannot be decoded or encoded."""


# ----------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------

def scaled_size(height: int, width: int, max_edge: int) -> Optional[tuple[int, int]]:
    """The (height, width) that fits `max_edge`, or None when it already does.

    One definition of the downscale arithmetic, so a cap applied at load time
    lands on exactly the same dimensions as the same cap applied later.
    """
    if max_edge <= 0:
        return None
    long_edge = max(height, width)
    if long_edge <= max_edge:
        return None
    scale = max_edge / float(long_edge)
    return max(1, int(round(height * scale))), max(1, int(round(width * scale)))


def _open(path: Any):
    """Open an image and normalise 16-bit modes down to 8-bit, or return None."""
    from PIL import Image

    if not path:
        return None
    file_path = Path(path)
    if not file_path.exists():
        return None
    image = Image.open(file_path)
    if image.mode.startswith("I;16"):
        # 16-bit modes carry 0..65535, which breaks the /255 normalisation.
        image = image.point(lambda value: value / 256).convert("RGB")
    return image


def _downscale(image: Any, max_edge: int):
    """Shrink a PIL image so its longest edge is <= max_edge, aspect preserved.

    Done in 8-bit PIL space rather than on the decoded float32 array: a 4K RGBA
    float32 buffer is 256 MiB while the same image as PIL bytes is 64 MiB, and
    the downscaled result is a quarter of that again. Resizing on load is what
    keeps a parallel batch of 4K materials inside RAM.
    """
    from PIL import Image

    target = scaled_size(image.size[1], image.size[0], max_edge)
    if target is None:
        return image
    new_height, new_width = target

    # Palette and bilevel images cannot be resampled in their own mode - PIL
    # silently falls back to nearest-neighbour - so promote them first.
    if image.mode not in ("L", "RGB", "RGBA"):
        image = image.convert("RGBA")
    if image.mode == "RGBA":
        rgb = image.convert("RGB").resize((new_width, new_height), Image.LANCZOS)
        alpha = image.getchannel("A").resize((new_width, new_height), Image.LANCZOS)
        rgb.putalpha(alpha)
        return rgb
    return image.resize((new_width, new_height), Image.LANCZOS)


def load_rgba(path: Any, max_edge: int = 0) -> Optional[np.ndarray]:
    """Decode an image as float32 RGBA in [0, 1], or None when it is missing.

    `max_edge` caps the longest side while the data is still 8-bit.
    """
    from PIL import Image

    image = _open(path)
    if image is None:
        return None
    image = _downscale(image, max_edge)
    if image.mode != "RGBA":
        if image.mode == "RGB":
            image.putalpha(Image.new("L", image.size, 255))
        else:
            image = image.convert("RGBA")

    raw = np.array(image)
    if raw.dtype != np.uint8:
        raw = np.clip(raw, 0, 255).astype(np.uint8)
    return raw.astype(np.float32) / 255.0


def load_scalar(path: Any, max_edge: int = 0) -> Optional[np.ndarray]:
    """Decode a single-channel map (AO / roughness / metallic / mask) as 2-D float32.

    Every consumer of these maps reads channel 0 anyway, so expanding them to
    RGBA costs four times the memory for identical values - 256 MiB instead of
    64 MiB per map at 4K, which is what pushes a parallel batch of 4K materials
    into MemoryError.
    """
    image = _open(path)
    if image is None:
        return None

    # Palette images go through RGB so we sample the palette's red channel;
    # converting straight to L would luminance-average it instead.
    if image.mode in ("P", "PA"):
        image = image.convert("RGB")
    elif image.mode not in ("L", "RGB", "RGBA"):
        image = image.convert("L")
    if image.mode != "L":
        image = image.getchannel(0)
    # Extract the channel before resizing so the resize runs on one band.
    image = _downscale(image, max_edge)

    raw = np.asarray(image)
    if raw.dtype != np.uint8:
        raw = np.clip(raw, 0, 255).astype(np.uint8)
    return raw.astype(np.float32) / 255.0


def uniform_rgba(rgba: Sequence[float]) -> np.ndarray:
    """A 4x4 float32 RGBA image filled with `rgba`.

    Source 2 vmats declare `"TextureColor" "[1.0 1.0 1.0 0.0]"` when the source
    model used a solid colour instead of a map. Materialising the literal at the
    same layout `load_rgba` returns lets the rest of the pipeline treat it as an
    ordinary (tiny) texture that gets resized up like any other.
    """
    values = [float(np.clip(component, 0.0, 1.0)) for component in rgba]
    while len(values) < 4:
        values.append(1.0)
    return np.tile(np.array([[values[:4]]], dtype=np.float32), (4, 4, 1))


def uniform_scalar(value: float) -> np.ndarray:
    """A 4x4 float32 scalar map filled with `value` - the `g_fl*` equivalent."""
    return np.full((4, 4), float(np.clip(value, 0.0, 1.0)), dtype=np.float32)


def flat_normal() -> np.ndarray:
    """A 4x4 tangent-space "no bump" normal: (X=0, Y=0, Z=1) as (0.5, 0.5, 1.0)."""
    return uniform_rgba((0.5, 0.5, 1.0, 1.0))


# ----------------------------------------------------------------------
# Resizing
# ----------------------------------------------------------------------

def fit(data: Optional[np.ndarray], height: int, width: int) -> Optional[np.ndarray]:
    """Resize float32 image data to exactly (height, width), or pass it through.

    Handles 2-D scalar maps and 3-D 1/3/4-channel images. RGBA is split so the
    alpha channel cannot premultiply the colour - see the module docstring.
    """
    from PIL import Image

    if data is None or data.shape[:2] == (height, width):
        return data

    source = (np.clip(data, 0.0, 1.0) * 255.0).astype(np.uint8)
    size = (width, height)

    if data.ndim == 2:
        resized = np.asarray(
            Image.fromarray(source, mode="L").resize(size, Image.LANCZOS),
            dtype=np.float32) / 255.0
    elif data.shape[2] == 1:
        resized = np.asarray(
            Image.fromarray(source[:, :, 0], mode="L").resize(size, Image.LANCZOS),
            dtype=np.float32) / 255.0
        resized = resized[:, :, np.newaxis]
    elif data.shape[2] == 3:
        resized = np.asarray(
            Image.fromarray(source, mode="RGB").resize(size, Image.LANCZOS),
            dtype=np.float32) / 255.0
    elif data.shape[2] == 4:
        rgb = np.asarray(
            Image.fromarray(source[:, :, :3], mode="RGB").resize(size, Image.LANCZOS),
            dtype=np.float32) / 255.0
        alpha = np.asarray(
            Image.fromarray(source[:, :, 3], mode="L").resize(size, Image.LANCZOS),
            dtype=np.float32)[:, :, np.newaxis] / 255.0
        resized = np.concatenate([rgb, alpha], axis=2)
    else:
        raise TextureError(f"Unsupported channel count: {data.shape[2]}")

    return resized


def as_rgba(data: np.ndarray) -> np.ndarray:
    """Promote a 2-D or 1/3-channel float array to float32 RGBA in [0, 1]."""
    values = np.clip(np.asarray(data, dtype=np.float32), 0.0, 1.0)
    if values.ndim == 2:
        values = values[:, :, np.newaxis]
    channels = values.shape[2]
    if channels == 1:
        grey = values[:, :, 0]
        values = np.dstack([grey, grey, grey])
        channels = 3
    if channels == 3:
        opaque = np.ones(values.shape[:2], dtype=np.float32)
        values = np.dstack([values, opaque])
    return values[:, :, :4]


def clamp_float(data: Optional[np.ndarray],
                max_edge: int) -> Optional[np.ndarray]:
    """Downscale a float32 buffer to `max_edge`, or pass it through untouched."""
    if data is None or max_edge <= 0:
        return data
    target = scaled_size(data.shape[0], data.shape[1], max_edge)
    if target is None:
        return data
    return fit(data, target[0], target[1])


def clamp_u8(data: Optional[np.ndarray], max_edge: int) -> Optional[np.ndarray]:
    """Downscale a uint8 output buffer so its longest edge is <= max_edge.

    Bilinear rather than Lanczos: this runs on already-baked masks where ringing
    would push values outside [0, 255] and back into the encoder as overflow.
    Same RGB/alpha split as `fit`.
    """
    from PIL import Image

    if data is None or max_edge <= 0:
        return data
    height, width = data.shape[:2]
    target = scaled_size(height, width, max_edge)
    if target is None:
        return data
    new_height, new_width = target
    size = (new_width, new_height)
    resample = Image.Resampling.BILINEAR

    if data.ndim == 2:
        return np.asarray(Image.fromarray(data, mode="L").resize(size, resample),
                          dtype=data.dtype).copy()
    if data.ndim == 3 and data.shape[2] == 3:
        return np.asarray(Image.fromarray(data, mode="RGB").resize(size, resample),
                          dtype=data.dtype).copy()
    if data.ndim == 3 and data.shape[2] == 4:
        rgb = np.asarray(
            Image.fromarray(data[:, :, :3], mode="RGB").resize(size, resample),
            dtype=data.dtype)
        alpha = np.asarray(
            Image.fromarray(data[:, :, 3], mode="L").resize(size, resample),
            dtype=data.dtype)[:, :, np.newaxis]
        return np.concatenate([rgb, alpha], axis=2).copy()
    return data


# ----------------------------------------------------------------------
# Transfer functions
# ----------------------------------------------------------------------

def srgb_to_linear(srgb: np.ndarray,
                   out: Optional[np.ndarray] = None) -> np.ndarray:
    """sRGB -> linear. Pass the input as `out` to convert in place."""
    source = np.asarray(srgb, dtype=np.float32)
    result = np.empty_like(source) if out is None else out

    for low in range(0, source.shape[0], CHUNK_ROWS):
        block = source[low:low + CHUNK_ROWS]
        target = result[low:low + CHUNK_ROWS]
        is_high = block > 0.04045
        # The high branch is computed first because `target` may alias `block`
        # when the caller asked for an in-place conversion.
        high = block + 0.055
        high /= 1.055
        np.power(high, 2.4, out=high)
        np.divide(block, 12.92, out=target)
        np.copyto(target, high, where=is_high)
    return result


def linear_to_srgb(linear: np.ndarray,
                   out: Optional[np.ndarray] = None) -> np.ndarray:
    """linear -> sRGB. Pass the input as `out` to convert in place."""
    source = np.asarray(linear, dtype=np.float32)
    result = np.empty_like(source) if out is None else out

    for low in range(0, source.shape[0], CHUNK_ROWS):
        block = source[low:low + CHUNK_ROWS]
        target = result[low:low + CHUNK_ROWS]
        is_high = block > 0.0031308
        high = np.power(block, 1.0 / 2.4)
        high *= 1.055
        high -= 0.055
        np.multiply(block, 12.92, out=target)
        np.copyto(target, high, where=is_high)
    return result


def to_uint8(data: np.ndarray) -> np.ndarray:
    """Clip float [0, 1] data and convert to uint8, in row blocks."""
    result = np.empty(data.shape, dtype=np.uint8)
    for low in range(0, data.shape[0], CHUNK_ROWS):
        block = np.clip(data[low:low + CHUNK_ROWS], 0.0, 1.0)
        result[low:low + CHUNK_ROWS] = block * 255.0
    return result


def luminance(rgb: np.ndarray) -> np.ndarray:
    """Rec.709 luminance of a linear RGB image, clipped to [0, 1]."""
    channels = rgb[:, :, :3]
    return np.clip(
        0.2126 * channels[:, :, 0]
        + 0.7152 * channels[:, :, 1]
        + 0.0722 * channels[:, :, 2],
        0.0, 1.0).astype(np.float32)


def chroma(rgb: np.ndarray) -> np.ndarray:
    """Per-pixel colourfulness in [0, 1]: brightest channel minus darkest."""
    channels = rgb[:, :, :3]
    return np.clip(np.max(channels, axis=2) - np.min(channels, axis=2),
                   0.0, 1.0).astype(np.float32)


# ----------------------------------------------------------------------
# VTF
# ----------------------------------------------------------------------

#: Applied through sourcepp's own flag word.
VTF_FLAG_NORMAL = 0x00000080
#: Internal sentinel, stripped before the flags reach sourcepp.
VTF_FLAG_SRGB = 0x00000040
#: Says the texture's alpha channel means something. The material system reads
#: this to decide whether a texture has usable alpha at all: without it
#: `$translucent`, `$alphatest`, `$phong`'s mask in the bumpmap alpha and every
#: other alpha parameter have nothing to read, and the surface draws as if the
#: alpha were solid white. Nothing in the compile or the console says so.
VTF_FLAG_EIGHTBITALPHA = 0x00002000
VTF_FLAG_ONEBITALPHA = 0x00001000


def alpha_flag(pixels: np.ndarray, image_format: Any) -> int:
    """`VTF_FLAG_EIGHTBITALPHA` when these pixels have alpha worth reading.

    Content decides it rather than the format: a DXT5 whose alpha is solid
    white carries no information in it, and claiming otherwise invites the
    engine to read a channel that says nothing. sourcepp's own
    `compute_transparency_flags` sets the flag from the *input* format instead,
    which is RGBA8888 for every buffer here and so always true.
    """
    vtfpp = _vtfpp()
    try:
        if not vtfpp.ImageFormatDetails.transparent(image_format):
            return 0
    except Exception:
        return 0
    if pixels.ndim != 3 or pixels.shape[2] < 4:
        return 0
    return VTF_FLAG_EIGHTBITALPHA if int(pixels[:, :, 3].min()) < 255 else 0



@dataclass
class VtfInfo:
    """Header facts about a decoded VTF."""

    width: int
    height: int
    format: str = ""
    mips: int = 1
    frames: int = 1
    flags: list[str] = field(default_factory=list)


def _vtfpp():
    try:
        from sourcepp import vtfpp
    except ImportError as exc:                              # pragma: no cover
        raise TextureError(
            "VTF support needs the 'sourcepp' package: pip install sourcepp"
        ) from exc
    return vtfpp


def decode_vtf(path: Any) -> tuple[np.ndarray, VtfInfo]:
    """Decode a VTF's largest mip as HxWx4 uint8 RGBA, plus its header facts."""
    vtfpp = _vtfpp()
    file_path = Path(path)
    texture = vtfpp.VTF(str(file_path))
    width = int(texture.width)
    height = int(texture.height)
    pixels = texture.get_image_data_as_rgba8888(0, 0, 0, 0)
    expected = width * height * 4
    if len(pixels) != expected:
        raise TextureError(
            f"{file_path.name}: decoded {len(pixels)} bytes, expected "
            f"{width}x{height}x4 = {expected}")
    data = np.frombuffer(pixels, dtype=np.uint8).reshape((height, width, 4)).copy()
    info = VtfInfo(
        width=width,
        height=height,
        format=str(getattr(texture.format, "name", texture.format)),
        mips=int(getattr(texture, "mip_count", 1) or 1),
        frames=int(getattr(texture, "frame_count", 1) or 1),
    )
    return data, info


def read_vtf_size(path: Any) -> tuple[int, int]:
    """Width and height straight from the VTF header, without decoding pixels."""
    import struct

    file_path = Path(path)
    with file_path.open("rb") as handle:
        header = handle.read(20)
    if len(header) < 20 or header[0:4] != b"VTF\x00":
        raise TextureError(f"{file_path.name}: not a VTF file")
    width, height = struct.unpack_from("<HH", header, 16)
    if width <= 0 or height <= 0:
        raise TextureError(f"{file_path.name}: bad header dimensions "
                           f"{width}x{height}")
    return int(width), int(height)


def optimise_dxt(texture: Any) -> Any:
    """Fit DXT alpha storage using fOptimizer's alpha rules without changing decoded pixels.

    Fully zero alpha may store a shader mask, so it is preserved. All mips,
    frames, cubemap faces and volume slices participate in the decision.
    Uncompressed data textures are never converted to DXT.
    """
    vtfpp = _vtfpp()
    if texture.format.name not in ("DXT5", "DXT3", "DXT1_ONE_BIT_ALPHA"):
        return texture
    binary = False
    for mip in range(texture.mip_count):
        for frame in range(texture.frame_count):
            for face in range(texture.face_count):
                for depth in range(texture.depth_for_mip(mip)):
                    data = texture.get_image_data_as_rgba8888(mip, frame, face, depth)
                    alpha = np.frombuffer(data, dtype=np.uint8)[3::4]
                    if not alpha.size or np.all(alpha == 0):
                        return texture
                    if np.any((alpha > 0) & (alpha < 255)):
                        return texture
                    binary |= bool(np.any(alpha == 0))
    target_format = (vtfpp.ImageFormat.DXT1_ONE_BIT_ALPHA if binary
                     else vtfpp.ImageFormat.DXT1)
    if target_format == texture.format:
        return texture
    candidate = vtfpp.VTF(texture.bake())
    candidate.set_format(target_format)
    for mip in range(texture.mip_count):
        for frame in range(texture.frame_count):
            for face in range(texture.face_count):
                for depth in range(texture.depth_for_mip(mip)):
                    if (texture.get_image_data_as_rgba8888(mip, frame, face, depth)
                            != candidate.get_image_data_as_rgba8888(mip, frame, face, depth)):
                        return texture
    candidate.flags = ((int(candidate.flags) & ~(VTF_FLAG_ONEBITALPHA | VTF_FLAG_EIGHTBITALPHA))
                       | (VTF_FLAG_ONEBITALPHA if binary else 0))
    return candidate


def encode_vtf(pixels: np.ndarray, output_path: Any, *,
               image_format: str = "DXT5", flags: int = 0,
               invert_green: bool = False, mipmaps: bool = True) -> Path:
    """Write `pixels` (HxWx3 or HxWx4 uint8) to `output_path` as a VTF 7.4."""
    vtfpp = _vtfpp()
    target = Path(output_path)

    if pixels.ndim != 3 or pixels.shape[2] not in (3, 4):
        raise TextureError(f"Expected HxWx3 or HxWx4, got {pixels.shape}")
    height, width, channels = pixels.shape
    if channels == 3:
        opaque = np.empty((height, width, 4), dtype=np.uint8)
        opaque[:, :, :3] = pixels
        opaque[:, :, 3] = 255
        pixels = opaque
    if pixels.dtype != np.uint8:
        pixels = np.clip(pixels, 0, 255).astype(np.uint8)

    options = vtfpp.VTF.CreationOptions()
    options.version = 4
    options.output_format = getattr(vtfpp.ImageFormat, image_format)
    options.flags = ((flags & ~VTF_FLAG_SRGB)
                     | alpha_flag(pixels, options.output_format))
    options.compute_mips = bool(mipmaps)
    options.compute_thumbnail = False
    options.compute_reflectivity = False
    options.compute_transparency_flags = False
    options.invert_green_channel = invert_green
    # sourcepp defaults to POWER_OF_TWO_BIGGER + the default resize filter,
    # which runs an internal resize pass on every encode even when the input is
    # already power-of-two. That filter mangles normal-map vector data into flat
    # black regions. Every buffer we hand over is already at its final size, so
    # take sourcepp's resize out of the loop entirely.
    options.width_resize_method = vtfpp.ImageConversion.ResizeMethod.NONE
    options.height_resize_method = vtfpp.ImageConversion.ResizeMethod.NONE

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        texture = vtfpp.VTF.create(
            np.ascontiguousarray(pixels).tobytes(),
            vtfpp.ImageFormat.RGBA8888,
            width, height, options,
        )
        if not optimise_dxt(texture).bake_to_file(str(target)):
            raise TextureError(f"Encoder could not write {target}")
    except Exception as exc:
        raise TextureError(f"Encoding {target.name}: {exc}") from exc
    if not target.exists():
        raise TextureError(f"Encoder produced no file: {target}")
    return target


def resize_vtf(path: Any, width: int, height: int) -> None:
    """Re-encode an existing VTF at a new size, keeping its format and flags."""
    vtfpp = _vtfpp()
    from PIL import Image

    file_path = Path(path)
    texture = vtfpp.VTF(str(file_path))
    source, _info = decode_vtf(file_path)

    resized = Image.fromarray(source, mode="RGBA").resize(
        (width, height), Image.Resampling.LANCZOS)
    pixels = np.ascontiguousarray(np.asarray(resized, dtype=np.uint8))

    options = vtfpp.VTF.CreationOptions()
    options.version = 4
    options.output_format = texture.format
    options.flags = int(texture.flags)
    options.compute_mips = True
    options.compute_thumbnail = False
    options.compute_reflectivity = False
    options.compute_transparency_flags = False
    options.width_resize_method = vtfpp.ImageConversion.ResizeMethod.NONE
    options.height_resize_method = vtfpp.ImageConversion.ResizeMethod.NONE

    rebuilt = vtfpp.VTF.create(pixels.tobytes(), vtfpp.ImageFormat.RGBA8888,
                               width, height, options)
    if not optimise_dxt(rebuilt).bake_to_file(str(file_path)):
        raise TextureError(f"sourcepp failed to write {file_path.name}")


def save_png(pixels: np.ndarray, output_path: Any) -> Path:
    """Write a uint8 array (HxW greyscale, HxWx3 RGB or HxWx4 RGBA) as a PNG."""
    from PIL import Image

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if pixels.ndim == 2:
        mode = "L"
    elif pixels.shape[2] == 3:
        mode = "RGB"
    elif pixels.shape[2] == 4:
        mode = "RGBA"
    else:
        raise TextureError(f"Cannot save shape {pixels.shape} as PNG")
    Image.fromarray(np.ascontiguousarray(pixels), mode=mode).save(target)
    return target


def save_scalar_png(values: np.ndarray, output_path: Any) -> Path:
    """Write a float [0, 1] scalar map as an 8-bit greyscale PNG."""
    return save_png((np.clip(values, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8),
                    output_path)
