"""
Texture decoding for the image viewer.

Loads anything Pillow understands plus VTF (via `sourcepp.vtfpp`, per-mip and
per-frame), then renders a preview to PNG bytes with the background, channel
isolation and scaling the user picked. Returns metadata alongside so the
viewer can show a real texture info panel rather than just a picture.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

CHANNELS = ("rgb", "rgba", "r", "g", "b", "a", "luminance")


@dataclass
class TextureInfo:
    """Decoded texture plus everything worth telling the user about it."""

    width: int
    height: int
    mode: str
    format: str = ""
    mip_count: int = 1
    frame_count: int = 1
    face_count: int = 1
    has_alpha: bool = False
    flags: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def rows(self) -> list[tuple[str, str]]:
        rows = [
            ("Dimensions", f"{self.width} x {self.height}"),
            ("Pixel mode", self.mode),
        ]
        if self.format:
            rows.append(("Format", self.format))
        if self.mip_count > 1:
            rows.append(("Mips", str(self.mip_count)))
        if self.frame_count > 1:
            rows.append(("Frames", str(self.frame_count)))
        if self.face_count > 1:
            rows.append(("Faces", str(self.face_count)))
        rows.append(("Alpha", "yes" if self.has_alpha else "no"))
        if self.flags:
            rows.append(("Flags", ", ".join(self.flags)))
        rows.extend((key, str(value)) for key, value in self.extra.items())
        return rows


class TextureLoadError(RuntimeError):
    """Raised when a texture cannot be decoded (with a user-facing reason)."""


def _pillow():
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - guarded by the bootstrapper
        raise TextureLoadError("Pillow is not installed") from exc
    return Image


def load(path: Any, *, mip: int = 0, frame: int = 0, face: int = 0):
    """Decode a texture to an RGBA Pillow image. Returns `(image, info)`."""
    target = Path(path)
    if not target.is_file():
        raise TextureLoadError(f"File not found: {target}")
    if target.suffix.lower() == ".vtf":
        return _load_vtf(target, mip=mip, frame=frame, face=face)
    return _load_pillow(target)


def _load_pillow(path: Path):
    Image = _pillow()
    try:
        image = Image.open(path)
        image.load()
    except Exception as exc:
        raise TextureLoadError(f"{type(exc).__name__}: {exc}") from exc
    info = TextureInfo(
        width=image.width,
        height=image.height,
        mode=image.mode,
        format=(image.format or path.suffix.lstrip(".")).upper(),
        frame_count=getattr(image, "n_frames", 1),
        has_alpha=image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info,
    )
    if image.mode != "RGBA":
        image = image.convert("RGBA")
    return image, info


def _load_vtf(path: Path, *, mip: int, frame: int, face: int):
    try:
        from sourcepp import vtfpp
    except ImportError as exc:
        raise TextureLoadError(
            "Reading VTF needs the optional 'sourcepp' package"
        ) from exc

    Image = _pillow()
    try:
        vtf = vtfpp.VTF(path.read_bytes())
    except Exception as exc:
        raise TextureLoadError(f"Not a readable VTF: {exc}") from exc

    mip = max(0, min(mip, vtf.mip_count - 1))
    frame = max(0, min(frame, vtf.frame_count - 1))
    face = max(0, min(face, vtf.face_count - 1))

    try:
        rgba = vtf.get_image_data_as_rgba8888(mip, frame, face, 0)
    except Exception as exc:
        raise TextureLoadError(f"Could not decode VTF image data: {exc}") from exc

    width = vtf.width_for_mip(mip) if hasattr(vtf, "width_for_mip") else vtf.width
    height = vtf.height_for_mip(mip) if hasattr(vtf, "height_for_mip") else vtf.height
    expected = width * height * 4
    if len(rgba) < expected:
        raise TextureLoadError(
            f"VTF mip {mip} returned {len(rgba)} bytes, expected {expected}"
        )
    image = Image.frombytes("RGBA", (width, height), bytes(rgba[:expected]))

    info = TextureInfo(
        width=width,
        height=height,
        mode="RGBA",
        format=str(vtf.format).replace("ImageFormat.", ""),
        mip_count=vtf.mip_count,
        frame_count=vtf.frame_count,
        face_count=vtf.face_count,
        has_alpha=_has_alpha(image),
        flags=_vtf_flags(vtf),
        extra={
            "VTF version": f"7.{getattr(vtf, 'version', '?')}",
            "Base size": f"{vtf.width} x {vtf.height}",
            "sRGB": "yes" if getattr(vtf, "is_srgb", False) else "no",
            "Bumpmap scale": getattr(vtf, "bumpmap_scale", "-"),
        },
    )
    return image, info


def _vtf_flags(vtf: Any) -> list[str]:
    """Decode the VTF flags bitfield into readable names."""
    try:
        raw = int(vtf.flags)
        names = []
        for member in type(vtf).Flags:
            value = int(member.value)
            if value and raw & value == value:
                names.append(member.name.replace("V0_", "").replace("_", " ").lower())
        return names
    except Exception:
        return []


def _has_alpha(image: Any) -> bool:
    try:
        alpha = image.getchannel("A")
        low, high = alpha.getextrema()
        return low < 255
    except Exception:
        return image.mode in ("RGBA", "LA")


# ----------------------------------------------------------------------
# Preview rendering
# ----------------------------------------------------------------------

def export_texture(path: Any, *, max_size: int = 0, channel: str = "rgba") -> tuple[bytes, TextureInfo]:
    """Encode a texture without preview backgrounds or upscaling, preserving RGBA channels."""
    Image = _pillow()
    image, info = load(path)
    if max_size > 0 and max(image.size) > max_size:
        scale = max_size / max(image.size)
        target = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
        image = Image.merge("RGBA", tuple(plane.resize(target, Image.NEAREST) for plane in image.split()))
    if channel in ("r", "g", "b", "a"):
        image = image.getchannel(channel.upper()).convert("RGB")
    elif channel == "luminance":
        image = image.convert("L").convert("RGB")
    elif channel == "rgb":
        image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue(), info


def render_preview(path: Any, *, max_size: int = 1024, channel: str = "rgba",
                   background: str = "checker", mip: int = 0, frame: int = 0,
                   face: int = 0, smooth: bool = False,
                   zoom: float = 0.0) -> tuple[bytes, TextureInfo]:
    """Decode a texture and encode a display-ready PNG.

    Args:
        max_size: longest edge of the produced preview.
        channel: one of :data:`CHANNELS`; isolates a single channel as grey.
        background: `checker`, `dark`, `light` or `magenta` behind the alpha.
        smooth: bilinear scaling instead of nearest-neighbour texels.
        zoom: explicit scale factor; 0 means fit to `max_size`.
    """
    Image = _pillow()
    image, info = load(path, mip=mip, frame=frame, face=face)

    # Downscale first. Channel splitting and the alpha checkerboard both cost
    # time proportional to pixel count, and a 4K texture shown at 1024 does not
    # need any of that work done at 4K - it took over a second before this.
    if zoom <= 0 and max(image.size) > max_size:
        ratio = max_size / max(image.size)
        image = image.resize(
            (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
            Image.BILINEAR if smooth else Image.NEAREST,
        )

    if channel in ("r", "g", "b", "a"):
        index = "rgba".index(channel)
        grey = image.split()[index]
        image = Image.merge("RGBA", (grey, grey, grey,
                                     Image.new("L", image.size, 255)))
    elif channel == "luminance":
        grey = image.convert("L")
        image = Image.merge("RGBA", (grey, grey, grey,
                                     Image.new("L", image.size, 255)))
    elif channel == "rgb":
        red, green, blue, _ = image.split()
        image = Image.merge("RGBA", (red, green, blue,
                                     Image.new("L", image.size, 255)))

    composite = _flatten(image, background)

    if zoom > 0:
        target = (max(1, int(image.width * zoom)), max(1, int(image.height * zoom)))
    else:
        scale = min(max_size / max(image.width, 1), max_size / max(image.height, 1))
        scale = min(scale, 1.0) if max(image.size) > max_size else scale
        target = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))

    if target != composite.size:
        resample = Image.BILINEAR if smooth else Image.NEAREST
        composite = composite.resize(target, resample)

    buffer = io.BytesIO()
    composite.convert("RGB").save(buffer, format="PNG", optimize=False)
    return buffer.getvalue(), info


def _flatten(image: Any, background: str):
    """Composite an RGBA image over the chosen preview background."""
    Image = _pillow()
    width, height = image.size
    if background == "checker":
        backdrop = _checkerboard(width, height)
    elif background == "light":
        backdrop = Image.new("RGBA", (width, height), (235, 235, 235, 255))
    elif background == "magenta":
        backdrop = Image.new("RGBA", (width, height), (255, 0, 255, 255))
    else:
        backdrop = Image.new("RGBA", (width, height), (24, 24, 28, 255))
    backdrop.alpha_composite(image)
    return backdrop


def _checkerboard(width: int, height: int, cell: int = 8):
    """The classic alpha checker, drawn at the image's own resolution."""
    Image = _pillow()
    try:
        import numpy as np
    except ImportError:
        return Image.new("RGBA", (width, height), (32, 32, 36, 255))

    ys, xs = np.mgrid[0:height, 0:width]
    mask = ((xs // cell) + (ys // cell)) % 2 == 0
    board = np.where(mask[..., None], np.array([70, 70, 76], dtype=np.uint8),
                     np.array([48, 48, 52], dtype=np.uint8))
    rgba = np.dstack([board, np.full((height, width, 1), 255, dtype=np.uint8)])
    return Image.fromarray(rgba, "RGBA")


def channel_thumbnails(path: Any, size: int = 128) -> dict[str, bytes]:
    """One small PNG per channel - the split view in the texture viewer."""
    Image = _pillow()
    image, _info = load(path)
    image.thumbnail((size, size), Image.NEAREST)
    result: dict[str, bytes] = {}
    for index, name in enumerate("RGBA"):
        grey = image.split()[index]
        buffer = io.BytesIO()
        grey.convert("RGB").save(buffer, format="PNG")
        result[name] = buffer.getvalue()
    return result


def histogram(path: Any, width: int = 256, height: int = 96) -> Optional[bytes]:
    """A small RGB histogram PNG, or None when numpy/Pillow are unavailable."""
    Image = _pillow()
    try:
        import numpy as np
    except ImportError:
        return None
    image, _info = load(path)
    pixels = np.asarray(image.convert("RGB"))
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    canvas[:] = (18, 18, 22)
    for channel in range(3):
        counts = np.bincount(pixels[..., channel].ravel(), minlength=256)
        # Log scale: texture histograms are dominated by flat regions.
        scaled = np.log1p(counts.astype(np.float64))
        peak = scaled.max() or 1.0
        columns = np.interp(np.linspace(0, 255, width), np.arange(256), scaled)
        heights = (columns / peak * (height - 2)).astype(int)
        for x, bar in enumerate(heights):
            if bar <= 0:
                continue
            canvas[height - bar:height, x, channel] = 235
    buffer = io.BytesIO()
    Image.fromarray(canvas, "RGB").save(buffer, format="PNG")
    return buffer.getvalue()
