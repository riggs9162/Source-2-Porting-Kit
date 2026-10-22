"""
Hotspot texturing: the rectangle sets that let Hammer fit a texture to a face.

A hotspot sheet is one texture holding many rectangular trim/panel regions, plus
a list of where those regions are. Select faces, press Fit, and the editor snaps
each face's UVs onto whichever rectangle best matches its size and aspect. One
material, one draw call, no manual aligning.

**Two incompatible formats share the `.rect` extension**, which is the single
biggest trap here:

``source2``
    KV3 text, referenced from a `.vmat` as ``SubrectDefinition`` inside
    ``ToolAttributes``. Coordinates are **normalised fixed-point where 32768 is
    the full extent of the texture on each axis** - independent of pixel size.
    Per-rect ``allowRotation`` / ``allowTiling`` and an ``inset`` pair. This is
    what Half-Life: Alyx and CS2 ship.

``hammerpp``
    Plain KeyValues-ish text, referenced from a `.vmt` as ``%rectanglemap``
    (percent, not dollar - the standard Source prefix for editor-only keys).
    Coordinates are **pixels**. Per-rect ``rotate`` / ``reflect`` / ``alt``
    flags, which work in Strata Hammer and are ignored by Hammer++.

Both put the origin at the **top left**, +Y down.

Converting between them therefore needs the texture's pixel size, which neither
file records. That is why :class:`RectSheet` carries one.

Everything here is framework-free and works on plain integers, so the same code
backs the editor canvas, a batch convert and a test.
"""

from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

#: Source 2 stores rect coordinates as a fixed-point fraction of the texture,
#: where this value is the full extent on each axis. Verified against every
#: shipped HLA and CS2 `.rect`: a 2048px and a 4096px sheet both max out here.
SOURCE2_EXTENT = 32768

#: The KV3 header every shipped Source 2 `.rect` starts with, byte for byte.
KV3_HEADER = ('<!-- kv3 encoding:text:version{e21c7f3c-8a33-41c5-9977-a76d3a32aa0d} '
              'format:generic:version{7412167c-06e9-4698-aff2-e63eb59037e7} -->')

#: Formats :func:`read_sheet` can produce.
FORMATS = ("hammerpp", "source2")


class HotspotError(RuntimeError):
    """A rect file could not be read or written."""


# ----------------------------------------------------------------------
# Model
# ----------------------------------------------------------------------

@dataclass
class Rect:
    """One rectangle, in **pixels**, origin top-left.

    Stored as position plus size rather than min/max because that is what the
    editor drags around; the file formats' `min`/`max` are derived.

    The flags are the union of what the two formats express. Writing to a format
    that lacks one simply drops it, and :func:`describe_losses` reports that
    rather than letting it vanish quietly.
    """

    x: int = 0
    y: int = 0
    w: int = 1
    h: int = 1

    #: Strata `rotate` / Source 2 `allowRotation` - the region may be turned 90
    #: degrees to fit a face of the opposite aspect.
    rotate: bool = False
    #: Strata `reflect` - the region may be randomly mirrored horizontally.
    reflect: bool = False
    #: Strata `alt` - held out of the normal pool; only picked with Alt held.
    alt: bool = False
    #: Source 2 `allowTiling` - the region repeats rather than stretching.
    tiling: bool = False
    #: Source 2 `inset` in pixels, for a trim whose art has a bevel.
    inset_x: int = 0
    inset_y: int = 0
    #: Editor-only, never serialised.
    label: str = ""

    # -- derived ------------------------------------------------------

    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h

    @property
    def area(self) -> int:
        return max(0, self.w) * max(0, self.h)

    @property
    def aspect(self) -> float:
        """Width over height. 1.0 for a square, >1 for a wide trim."""
        return (self.w / self.h) if self.h else 0.0

    def normalised(self) -> "Rect":
        """A copy with non-negative width and height."""
        x, y, w, h = self.x, self.y, self.w, self.h
        if w < 0:
            x, w = x + w, -w
        if h < 0:
            y, h = y + h, -h
        return replace(self, x=x, y=y, w=w, h=h)

    def clamped(self, width: int, height: int) -> "Rect":
        """A copy clipped to a texture of `width` x `height`, never zero-area."""
        rect = self.normalised()
        x = max(0, min(rect.x, max(0, width - 1)))
        y = max(0, min(rect.y, max(0, height - 1)))
        w = max(1, min(rect.w, width - x))
        h = max(1, min(rect.h, height - y))
        return replace(rect, x=x, y=y, w=w, h=h)

    def snapped(self, grid: int, width: int = 0, height: int = 0) -> "Rect":
        """A copy with all four edges rounded to the nearest multiple of `grid`."""
        if grid <= 1:
            return self.normalised()
        rect = self.normalised()
        x = int(round(rect.x / grid)) * grid
        y = int(round(rect.y / grid)) * grid
        x2 = max(x + grid, int(round(rect.x2 / grid)) * grid)
        y2 = max(y + grid, int(round(rect.y2 / grid)) * grid)
        out = replace(rect, x=x, y=y, w=x2 - x, h=y2 - y)
        return out.clamped(width, height) if width and height else out

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px <= self.x2 and self.y <= py <= self.y2

    def key(self) -> tuple[int, int, int, int]:
        """Geometry only - used to spot exact duplicates."""
        return (self.x, self.y, self.w, self.h)


@dataclass
class RectSheet:
    """A set of rectangles plus the texture they describe."""

    rects: list[Rect] = field(default_factory=list)
    #: Texture size in pixels. Required to convert to or from Source 2.
    width: int = 0
    height: int = 0
    #: Where this came from: one of :data:`FORMATS`, or "" when built by hand.
    source_format: str = ""
    source_path: Optional[Path] = None
    #: Comment lines recovered from a Hammer++ file, so a round trip keeps them.
    comments: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.rects)

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)

    def coverage(self) -> float:
        """Fraction of the texture covered by at least one rect, 0..1.

        Computed on a per-pixel-row basis rather than by summing areas, so
        overlapping rects are not double-counted. Valve's own sheets overlap
        heavily on purpose, so a naive sum reads well over 100%.
        """
        if not (self.width and self.height and self.rects):
            return 0.0
        # Sweep the distinct horizontal edges rather than every pixel row: the
        # covered set only changes where a rect starts or ends, so a 2048-tall
        # sheet costs as many steps as it has distinct edges, not 2048. Same
        # exact answer, and this runs on every edit.
        edges = sorted({0, self.height}
                       | {r.y for r in self.rects if 0 < r.y < self.height}
                       | {r.y2 for r in self.rects if 0 < r.y2 < self.height})
        covered = 0
        for top, bottom in zip(edges, edges[1:]):
            band = bottom - top
            if band <= 0:
                continue
            spans = sorted((r.x, r.x2) for r in self.rects
                           if r.y <= top and r.y2 >= bottom)
            reach = 0
            width = 0
            for start, end in spans:
                if end <= reach:
                    continue
                width += end - max(start, reach)
                reach = end
            covered += width * band
        return covered / float(self.width * self.height)

    def duplicates(self) -> list[tuple[int, int]]:
        """Pairs of indices whose geometry is identical."""
        seen: dict[tuple, int] = {}
        pairs: list[tuple[int, int]] = []
        for index, rect in enumerate(self.rects):
            first = seen.get(rect.key())
            if first is None:
                seen[rect.key()] = index
            else:
                pairs.append((first, index))
        return pairs


# ----------------------------------------------------------------------
# Format detection
# ----------------------------------------------------------------------

def sniff_format(text: str) -> Optional[str]:
    """Which `.rect` dialect `text` is, or None when it is neither.

    Both dialects use the same extension, so the first non-blank line decides:
    a KV3 header means Source 2, a `Rectangles` block means Hammer++.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("<!--") and "kv3" in line:
            return "source2"
        if line.lower().startswith("rectangles"):
            return "hammerpp"
        # A bare `{` before either marker: KV3 without its header comment.
        if line.startswith("{"):
            return "source2" if "RectangleSets" in text else "hammerpp"
        return None
    return None


# ----------------------------------------------------------------------
# Hammer++ / Strata text format
# ----------------------------------------------------------------------

_HPP_PAIR = re.compile(r'"?\s*(-?[0-9.]+)\s+(-?[0-9.]+)\s*"?')


def read_hammerpp(text: str) -> RectSheet:
    """Parse the Hammer++/Strata plaintext `.rect` format.

    Tolerant on input where the real files vary - quoted or bare keys, tabs or
    spaces, trailing `//` comments, floats where ints are expected - because
    several third-party editors write this format and they do not agree.
    """
    sheet = RectSheet(source_format="hammerpp")
    current: Optional[dict] = None
    depth = 0

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("//"):
            sheet.comments.append(line[2:].strip())
            continue
        line = line.split("//", 1)[0].strip()
        if not line:
            continue

        if line.startswith("{"):
            depth += 1
            continue
        if line.startswith("}"):
            depth -= 1
            if current is not None:
                sheet.rects.append(_rect_from_minmax(current))
                current = None
            continue

        token = line.split(None, 1)[0].strip('"').lower()
        if token == "rectangle":
            current = {}
            continue
        if token == "rectangles" or current is None:
            continue

        if token in ("min", "max"):
            match = _HPP_PAIR.search(line[len(line.split(None, 1)[0]):])
            if match:
                current[token] = (int(float(match.group(1))),
                                  int(float(match.group(2))))
        elif token in ("rotate", "reflect", "alt", "tile", "tiling"):
            parts = line.replace("\t", " ").split()
            try:
                value = int(float(parts[-1].strip('"')))
            except (ValueError, IndexError):
                value = 1
            current["tiling" if token in ("tile", "tiling") else token] = bool(value)

    return sheet


def _rect_from_minmax(data: dict) -> Rect:
    x, y = data.get("min", (0, 0))
    x2, y2 = data.get("max", (x + 1, y + 1))
    rect = Rect(x=x, y=y, w=x2 - x, h=y2 - y,
                rotate=bool(data.get("rotate")),
                reflect=bool(data.get("reflect")),
                alt=bool(data.get("alt")),
                tiling=bool(data.get("tiling")))
    return rect.normalised()


def write_hammerpp(sheet: RectSheet, *, flags: bool = True,
                   comments: bool = True) -> str:
    """Serialise to the Hammer++/Strata plaintext format.

    The exact layout matters for interoperability: RectMaker's reader is
    positional (it steps five lines per rectangle) rather than a real KeyValues
    parser, so the canonical one-key-per-line shape with tab indentation is
    emitted verbatim - two tabs before `min`/`max`, the value double-quoted.

    `flags` off writes plain Hammer++ output. Hammer++ ignores the flags
    anyway, but a file without them is unambiguous about what it expects.
    """
    lines: list[str] = []
    if comments and sheet.comments:
        lines.extend(f"// {comment}" for comment in sheet.comments)
    lines.append("Rectangles")
    lines.append("{")
    for rect in sheet.rects:
        rect = rect.normalised()
        lines.append("\trectangle")
        lines.append("\t{")
        lines.append(f'\t\tmin\t\t"{rect.x} {rect.y}"')
        lines.append(f'\t\tmax\t\t"{rect.x2} {rect.y2}"')
        if flags:
            if rect.rotate:
                lines.append("\t\trotate\t1")
            if rect.reflect:
                lines.append("\t\treflect\t1")
            if rect.alt:
                lines.append("\t\talt\t1")
        lines.append("\t}")
    lines.append("}")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------
# Source 2 KV3 format
# ----------------------------------------------------------------------

_KV3_RECT = re.compile(
    r"\{\s*"
    r"min\s*=\s*\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\s*"
    r"max\s*=\s*\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\s*"
    r"(?:inset\s*=\s*\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\s*)?"
    r"properties\s*=\s*(null|\{.*?\})",
    re.DOTALL)


def read_source2(text: str, width: int, height: int) -> RectSheet:
    """Parse a Source 2 KV3 `.rect` into pixel coordinates.

    `width` and `height` are the texture's pixel size, which the file does not
    record - Source 2 stores every coordinate as a fraction of
    :data:`SOURCE2_EXTENT`. Without the real size the rects would be
    meaningless, so this is required rather than guessed.

    Only the first `RectangleSets` entry is read. Multi-set files are legal but
    every one of the 94 sheets Valve ships has exactly one unnamed set.
    """
    if width <= 0 or height <= 0:
        raise HotspotError(
            "Source 2 rect coordinates are a fraction of the texture, so the "
            "texture's pixel size is needed to convert them. Load the texture "
            "first, or set the size by hand.")

    sheet = RectSheet(width=width, height=height, source_format="source2")
    for match in _KV3_RECT.finditer(text):
        x, y, x2, y2 = (int(match.group(index)) for index in range(1, 5))
        properties = match.group(7) or "null"
        inset_x = int(match.group(5) or 0)
        inset_y = int(match.group(6) or 0)

        sheet.rects.append(Rect(
            x=_to_pixels(x, width), y=_to_pixels(y, height),
            w=_to_pixels(x2 - x, width), h=_to_pixels(y2 - y, height),
            rotate="allowRotation = true" in properties,
            tiling="allowTiling = true" in properties,
            inset_x=_to_pixels(inset_x, width),
            inset_y=_to_pixels(inset_y, height),
        ).normalised())
    return sheet


def write_source2(sheet: RectSheet) -> str:
    """Serialise to the Source 2 KV3 format, converting pixels back to 0..32768."""
    if not (sheet.width and sheet.height):
        raise HotspotError("Writing Source 2 rects needs the texture size")

    lines = [KV3_HEADER, "{", "\tRectangleSets = ", "\t[", "\t\t{",
             '\t\t\tname = ""', "\t\t\tproperties = null",
             "\t\t\trectangles = ", "\t\t\t["]
    for rect in sheet.rects:
        rect = rect.normalised()
        lines.append("\t\t\t\t{")
        lines.append(f"\t\t\t\t\tmin = [ {_to_units(rect.x, sheet.width)}, "
                     f"{_to_units(rect.y, sheet.height)} ]")
        lines.append(f"\t\t\t\t\tmax = [ {_to_units(rect.x2, sheet.width)}, "
                     f"{_to_units(rect.y2, sheet.height)} ]")
        if rect.inset_x or rect.inset_y:
            lines.append(f"\t\t\t\t\tinset = [ "
                         f"{_to_units(rect.inset_x, sheet.width)}, "
                         f"{_to_units(rect.inset_y, sheet.height)} ]")
        if rect.rotate or rect.tiling:
            lines.append("\t\t\t\t\tproperties = ")
            lines.append("\t\t\t\t\t{")
            if rect.rotate:
                lines.append("\t\t\t\t\t\tallowRotation = true")
            if rect.tiling:
                lines.append("\t\t\t\t\t\tallowTiling = true")
            lines.append("\t\t\t\t\t}")
        else:
            lines.append("\t\t\t\t\tproperties = null")
        lines.append("\t\t\t\t},")
    lines += ["\t\t\t]", "\t\t},", "\t]", "}"]
    return "\n".join(lines) + "\n"


def _to_pixels(units: int, extent_px: int) -> int:
    return int(round(units * extent_px / SOURCE2_EXTENT))


def _to_units(pixels: int, extent_px: int) -> int:
    if extent_px <= 0:
        return 0
    return int(round(pixels * SOURCE2_EXTENT / extent_px))


# ----------------------------------------------------------------------
# Strata embedded VTF resource
# ----------------------------------------------------------------------

#: Strata's hotspot resource tag. Deliberately low so it sorts before the
#: high-resolution image data inside the VTF.
VTF_HOTSPOT_TAG = b"\x2b\x00\x00"

_VTF_ROTATION = 0x1
_VTF_REFLECTION = 0x2
_VTF_ALT = 0x4


def write_vtf_resource(sheet: RectSheet) -> bytes:
    """Pack the rects as Strata's embedded VTF hotspot resource.

    Layout, per Strata's published spec: a 4-byte header (version, flags,
    little-endian rect count) then 9 bytes per rect (flag byte, then four
    unsigned shorts). Coordinates are pixels, so anything past 65535 cannot be
    represented - which no real texture reaches.

    Returns the resource body only; embedding it in a VTF is a separate job.
    """
    body = bytearray(struct.pack("<BBH", 1, 0, len(sheet.rects)))
    for rect in sheet.rects:
        rect = rect.normalised()
        flags = ((_VTF_ROTATION if rect.rotate else 0)
                 | (_VTF_REFLECTION if rect.reflect else 0)
                 | (_VTF_ALT if rect.alt else 0))
        for value in (rect.x, rect.y, rect.x2, rect.y2):
            if not 0 <= value <= 0xFFFF:
                raise HotspotError(
                    f"{value} does not fit the resource's 16-bit coordinates")
        body += struct.pack("<BHHHH", flags, rect.x, rect.y, rect.x2, rect.y2)
    return bytes(body)


# ----------------------------------------------------------------------
# Reading and writing whole files
# ----------------------------------------------------------------------

def read_sheet(path: Any, *, width: int = 0, height: int = 0) -> RectSheet:
    """Read a `.rect` of either dialect, sniffing which one it is."""
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8", errors="replace")
    kind = sniff_format(text)
    if kind == "source2":
        sheet = read_source2(text, width, height)
    elif kind == "hammerpp":
        sheet = read_hammerpp(text)
        sheet.width, sheet.height = width, height
    else:
        raise HotspotError(
            f"{file_path.name} is not a rect file - expected a KV3 header "
            "(Source 2) or a 'Rectangles' block (Hammer++)")
    sheet.source_path = file_path
    return sheet


def write_sheet(sheet: RectSheet, path: Any, *, target: str = "hammerpp",
                flags: bool = True) -> Path:
    """Write a sheet in `target` format. Returns the path written."""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    if target == "source2":
        text = write_source2(sheet)
    elif target == "hammerpp":
        text = write_hammerpp(sheet, flags=flags)
    else:
        raise HotspotError(f"Unknown target format: {target}")
    # Newline pinned so the file is identical on every platform - several
    # third-party readers of this format are positional.
    file_path.write_text(text, encoding="utf-8", newline="\n")
    return file_path


def describe_losses(sheet: RectSheet, target: str) -> list[str]:
    """What writing to `target` would silently drop. Empty when nothing is lost."""
    losses: list[str] = []
    if target == "hammerpp":
        if any(rect.tiling for rect in sheet.rects):
            losses.append("allowTiling has no Hammer++/Strata equivalent")
        if any(rect.inset_x or rect.inset_y for rect in sheet.rects):
            losses.append("per-rect inset has no Hammer++/Strata equivalent")
    elif target == "source2":
        if any(rect.reflect for rect in sheet.rects):
            losses.append("reflect is Strata-only; Source 2 has no mirror flag")
        if any(rect.alt for rect in sheet.rects):
            losses.append("alt is Strata-only; Source 2 has no alternate group")
    return losses


# ----------------------------------------------------------------------
# VMT wiring
# ----------------------------------------------------------------------

#: Accepts both prefixes. VDC and every shipped example use `%`, but Hammer++'s
#: own changelog says `$`, so files in the wild carry both.
_RECTANGLEMAP = re.compile(r'(?im)^([ \t]*)"?[%$]rectanglemap"?[ \t]+\S.*$')


def material_relative_path(path: Any) -> str:
    """A texture path relative to `materials/`, without its extension.

    That is the form `%rectanglemap` and `$basetexture` both take. Falls back to
    the bare stem when the path is not under a `materials` folder.
    """
    file_path = Path(path)
    parts = [part.lower() for part in file_path.parts]
    stem = file_path.with_suffix("")
    if "materials" in parts:
        index = len(parts) - 1 - parts[::-1].index("materials")
        return "/".join(stem.parts[index + 1:])
    return stem.name


#: The `$basetexture` line, which `%rectanglemap` is placed under so the two
#: references that must agree sit side by side.
_BASETEXTURE = re.compile(r'(?im)^[ \t]*"?\$basetexture"?[ \t]+\S.*$')


def inject_rectanglemap(vmt_path: Any, reference: str) -> str:
    """Point a VMT's `%rectanglemap` at `reference`. Returns what was done.

    An existing entry is replaced in place, keeping its indentation and its
    original `%`/`$` prefix. Otherwise the key goes directly under
    `$basetexture`, or first in the shader block when there is none, matching
    the indentation the file already uses. The file's line endings are kept.

    Returns ``"updated"``, ``"added"`` or ``"unchanged"``.
    """
    file_path = Path(vmt_path)
    text, newline = _read_vmt(file_path)
    line = f'"%rectanglemap" "{reference}"'

    existing = _RECTANGLEMAP.search(text)
    if existing is not None:
        if existing.group(0).strip() == line:
            return "unchanged"
        # Keep whichever prefix the file already used - a project that
        # standardised on one should not be quietly switched to the other.
        prefix = "$" if "$rectanglemap" in existing.group(0).lower() else "%"
        replacement = f'{existing.group(1)}"{prefix}rectanglemap" "{reference}"'
        file_path.write_text(_RECTANGLEMAP.sub(lambda _m: replacement, text,
                                               count=1),
                             encoding="utf-8", newline=newline)
        return "updated"

    basetexture = _BASETEXTURE.search(text)
    if basetexture is not None:
        indent = basetexture.group(0)[:len(basetexture.group(0))
                                      - len(basetexture.group(0).lstrip())]
        at = basetexture.end()
    else:
        brace = text.find("{")
        if brace == -1:
            raise HotspotError(f"{file_path.name} has no shader block to add to")
        indent = _detect_indent(text)
        at = brace + 1
    updated = f"{text[:at]}\n{indent}{line}{text[at:]}"
    file_path.write_text(updated, encoding="utf-8", newline=newline)
    return "added"


def _read_vmt(path: Path) -> tuple[str, str]:
    """A VMT's text with newlines normalised, and the newline it was written with."""
    raw = path.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    return text, newline


def _detect_indent(text: str) -> str:
    """The indentation the VMT already uses for its parameters."""
    for raw in text.splitlines():
        stripped = raw.lstrip()
        if stripped.startswith('"$') and raw != stripped:
            return raw[:len(raw) - len(stripped)]
    return "\t"


def read_rectanglemap(vmt_path: Any) -> Optional[str]:
    """The `%rectanglemap` reference a VMT already carries, if any."""
    try:
        text = Path(vmt_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _RECTANGLEMAP.search(text)
    if match is None:
        return None
    values = re.findall(r'"([^"]*)"', match.group(0))
    return values[-1] if values else None


# ----------------------------------------------------------------------
# Pairing a material with its texture and rect file
# ----------------------------------------------------------------------

#: Texture formats a material's `$basetexture` may be found as, best first.
TEXTURE_EXTENSIONS = (".vtf", ".png", ".tga", ".jpg", ".jpeg", ".bmp")


def materials_root(path: Any) -> Optional[Path]:
    """The nearest `materials` folder above `path`, or None.

    The same folder :func:`material_relative_path` measures against, so a
    reference built by one resolves through the other.
    """
    for parent in Path(path).parents:
        if parent.name.lower() == "materials":
            return parent
    return None


def _clean_reference(reference: str, extension: str) -> str:
    cleaned = reference.replace("\\", "/").strip().strip("/")
    if cleaned.lower().endswith(extension):
        cleaned = cleaned[:-len(extension)]
    return cleaned


def _content_roots(root: Path) -> list[Path]:
    """`root` first, then every other `materials` folder the game mounts with it.

    A GMod addon commonly ships its VMTs and VTFs in different addons, so a
    `$basetexture` that is not under the VMT's own `materials` folder is looked
    for in the sibling addons and the game's own `materials`.
    """
    roots = [root]
    addon = root.parent
    if addon.parent.name.lower() != "addons":
        return roots
    roots.append(addon.parent.parent / "materials")
    try:
        roots.extend(sorted(sibling / "materials"
                            for sibling in addon.parent.iterdir()
                            if sibling != addon))
    except OSError:
        pass
    return roots


def resolve_basetexture(vmt_path: Any) -> Optional[Path]:
    """The texture file a VMT's `$basetexture` names, or None.

    Tries the VMT's own folder, its `materials` root, and then the other content
    roots a GMod install mounts beside it. A `.vtf` wins over a source image of
    the same name.
    """
    from s2kit.pipelines import vmt as vmt_module

    file_path = Path(vmt_path)
    reference = vmt_module.read_texture_refs(file_path).get("color")
    if not reference:
        return None
    cleaned = _clean_reference(reference, ".vtf")

    candidates = [file_path.parent / Path(cleaned).name]
    root = materials_root(file_path)
    if root is not None:
        candidates += [content / cleaned for content in _content_roots(root)]
    for stem in candidates:
        for extension in TEXTURE_EXTENSIONS:
            candidate = stem.with_name(stem.name + extension)
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
    return None


def find_material_for_texture(texture_path: Any) -> Optional[Path]:
    """The VMT beside a texture whose `$basetexture` names that texture.

    Matched on the reference itself rather than by resolving files, so a sheet
    still being authored as a PNG pairs with the VMT that will point at its VTF.
    """
    from s2kit.pipelines import vmt as vmt_module

    texture = Path(texture_path)
    wanted = material_relative_path(texture).lower()
    loose = materials_root(texture) is None
    try:
        materials = sorted(texture.parent.glob("*.vmt"))
    except OSError:
        return None
    for material in materials:
        reference = vmt_module.read_texture_refs(material).get("color")
        if not reference:
            continue
        cleaned = _clean_reference(reference, ".vtf").lower()
        if cleaned == wanted or (loose and Path(cleaned).name == wanted):
            return material
    return None


def resolve_rectanglemap(reference: str, vmt_path: Any) -> Path:
    """Where a `%rectanglemap` reference points on disk.

    The reference is materials-relative and carries no extension. A VMT outside
    any `materials` folder can only mean a file beside it.
    """
    cleaned = _clean_reference(reference, ".rect")
    root = materials_root(vmt_path)
    if root is None:
        return Path(vmt_path).parent / (Path(cleaned).name + ".rect")
    return root / (cleaned + ".rect")


def rect_path_for_material(vmt_path: Any,
                           texture_path: Optional[Any] = None) -> Path:
    """The `.rect` a material uses, whether or not it exists yet.

    In order: whatever the VMT's `%rectanglemap` already names, a rect named
    after the material beside the VMT, one named after the texture beside the
    texture, and otherwise a new file named after the material beside the VMT -
    which is where Hammer++ and Strata expect to find it.
    """
    material = Path(vmt_path)
    reference = read_rectanglemap(material)
    if reference:
        return resolve_rectanglemap(reference, material)
    beside_material = material.with_suffix(".rect")
    if beside_material.is_file() or texture_path is None:
        return beside_material
    beside_texture = Path(texture_path).with_suffix(".rect")
    return beside_texture if beside_texture.is_file() else beside_material


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------

@dataclass
class Issue:
    """One problem found in a sheet."""

    level: str          # error | warning | info
    message: str
    index: int = -1     # rect it applies to, -1 for sheet-wide


def validate(sheet: RectSheet, *, grid: int = 0) -> list[Issue]:
    """Check a sheet for the mistakes that produce a broken or wasteful fit.

    Overlap is deliberately **not** an error. Valve's own sheets overlap
    heavily - authoring several candidate rects over the same art is how you
    give Fit something to choose between.
    """
    issues: list[Issue] = []
    if not sheet.rects:
        issues.append(Issue("warning", "The sheet has no rectangles"))
        return issues

    for index, rect in enumerate(sheet.rects):
        if rect.w <= 0 or rect.h <= 0:
            issues.append(Issue("error", f"Rect {index + 1} has no area", index))
            continue
        if sheet.width and sheet.height:
            if (rect.x < 0 or rect.y < 0
                    or rect.x2 > sheet.width or rect.y2 > sheet.height):
                issues.append(Issue(
                    "error",
                    f"Rect {index + 1} runs outside the texture "
                    f"({rect.x},{rect.y})-({rect.x2},{rect.y2}) vs "
                    f"{sheet.width}x{sheet.height}", index))
        if rect.w < 4 or rect.h < 4:
            issues.append(Issue(
                "warning", f"Rect {index + 1} is only {rect.w}x{rect.h}px", index))
        if grid > 1 and any(value % grid for value in
                            (rect.x, rect.y, rect.x2, rect.y2)):
            issues.append(Issue(
                "info", f"Rect {index + 1} is not aligned to the {grid}px grid",
                index))

    for first, second in sheet.duplicates():
        issues.append(Issue(
            "warning",
            f"Rect {second + 1} is identical to rect {first + 1} - Fit treats "
            "them as one candidate", second))

    if sheet.width and sheet.height:
        covered = sheet.coverage()
        if covered < 0.5:
            issues.append(Issue(
                "info",
                f"Rects cover {covered * 100:.0f}% of the texture - the rest "
                "can never be chosen by Fit"))
    return issues


# ----------------------------------------------------------------------
# Fit simulation
# ----------------------------------------------------------------------

def match_score(rect: Rect, face_w: float, face_h: float) -> float:
    """How well `rect` suits a face of this size. Lower is better.

    Hammer picks "the closest matching rectangle within a margin of error" and
    chooses randomly among ties. The exact metric is not published, so this is a
    reasonable stand-in rather than a reimplementation: aspect ratio dominates,
    with relative area as the tie-break, both measured in log space so being
    twice too big scores the same as half too small.

    A `rotate` rect is also scored turned 90 degrees, and the better one wins.
    """
    if face_w <= 0 or face_h <= 0 or rect.w <= 0 or rect.h <= 0:
        return math.inf

    def score(rw: float, rh: float) -> float:
        aspect = abs(math.log((rw / rh) / (face_w / face_h)))
        area = abs(math.log((rw * rh) / (face_w * face_h)))
        return aspect * 4.0 + area
    best = score(rect.w, rect.h)
    if rect.rotate:
        best = min(best, score(rect.h, rect.w))
    return best


def best_matches(sheet: RectSheet, face_w: float, face_h: float, *,
                 limit: int = 5, include_alt: bool = False
                 ) -> list[tuple[int, float]]:
    """The rects Fit would most likely choose for a face, best first.

    Returns `(index, score)` pairs. `alt` rects are excluded unless asked for,
    mirroring Strata: they are held back until Alt is pressed.
    """
    scored = [(index, match_score(rect, face_w, face_h))
              for index, rect in enumerate(sheet.rects)
              if include_alt or not rect.alt]
    scored = [pair for pair in scored if math.isfinite(pair[1])]
    scored.sort(key=lambda pair: (pair[1], pair[0]))
    return scored[:limit]


def aspect_coverage(sheet: RectSheet,
                    aspects: Sequence[float] = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)
                    ) -> list[tuple[float, float]]:
    """How well the sheet serves a range of face aspect ratios.

    For each aspect, the score of the best-matching rect. This is the question a
    level designer actually has - "will this sheet handle a long thin wall?" -
    and it is answerable before ever loading the map.
    """
    results: list[tuple[float, float]] = []
    for aspect in aspects:
        matches = best_matches(sheet, aspect * 128.0, 128.0, limit=1)
        results.append((aspect, matches[0][1] if matches else math.inf))
    return results


# ----------------------------------------------------------------------
# Layout generation
# ----------------------------------------------------------------------

def grid_layout(width: int, height: int, columns: int, rows: int) -> list[Rect]:
    """A `columns` x `rows` grid covering the whole texture.

    Edges are computed from the exact fractions and then differenced, so the
    cells tile the texture with no gap or overlap even when the size does not
    divide evenly.
    """
    columns, rows = max(1, columns), max(1, rows)
    xs = [round(index * width / columns) for index in range(columns + 1)]
    ys = [round(index * height / rows) for index in range(rows + 1)]
    return [Rect(x=xs[c], y=ys[r], w=xs[c + 1] - xs[c], h=ys[r + 1] - ys[r])
            for r in range(rows) for c in range(columns)]


def strip_layout(width: int, height: int, counts: Sequence[int], *,
                 vertical: bool = False) -> list[Rect]:
    """Rows (or columns) of differing subdivision - the classic trim sheet.

    `counts` gives the number of cells in each band, so `(1, 2, 4)` is a full
    -width strip, then a half-split band, then a quarter-split band.
    """
    bands = len(counts) or 1
    rects: list[Rect] = []
    edges = [round(index * (width if vertical else height) / bands)
             for index in range(bands + 1)]
    for band, count in enumerate(counts):
        count = max(1, count)
        across = width if not vertical else height
        splits = [round(index * across / count) for index in range(count + 1)]
        for cell in range(count):
            if vertical:
                rects.append(Rect(x=edges[band], y=splits[cell],
                                  w=edges[band + 1] - edges[band],
                                  h=splits[cell + 1] - splits[cell]))
            else:
                rects.append(Rect(x=splits[cell], y=edges[band],
                                  w=splits[cell + 1] - splits[cell],
                                  h=edges[band + 1] - edges[band]))
    return rects


def subdivide(rect: Rect, columns: int, rows: int) -> list[Rect]:
    """Split one rect into a grid of smaller ones, keeping its flags."""
    columns, rows = max(1, columns), max(1, rows)
    xs = [rect.x + round(index * rect.w / columns) for index in range(columns + 1)]
    ys = [rect.y + round(index * rect.h / rows) for index in range(rows + 1)]
    return [replace(rect, x=xs[c], y=ys[r],
                    w=xs[c + 1] - xs[c], h=ys[r + 1] - ys[r])
            for r in range(rows) for c in range(columns)]


#: Ready-made layouts, as (label, callable taking width/height).
PRESETS: tuple[tuple[str, Any], ...] = (
    ("Whole texture", lambda w, h: [Rect(0, 0, w, h)]),
    ("2 x 1", lambda w, h: grid_layout(w, h, 2, 1)),
    ("1 x 2", lambda w, h: grid_layout(w, h, 1, 2)),
    ("2 x 2", lambda w, h: grid_layout(w, h, 2, 2)),
    ("4 x 4", lambda w, h: grid_layout(w, h, 4, 4)),
    ("Trim: 4 rows", lambda w, h: strip_layout(w, h, (1, 1, 1, 1))),
    ("Trim: 1 / 2 / 4", lambda w, h: strip_layout(w, h, (1, 2, 4))),
    ("Trim: 1 / 2 / 4 / 8", lambda w, h: strip_layout(w, h, (1, 2, 4, 8))),
    ("Columns 2 / 4", lambda w, h: strip_layout(w, h, (2, 4), vertical=True)),
)


# ----------------------------------------------------------------------
# Detection from the image
# ----------------------------------------------------------------------

def detect_rects(image: Any, *, min_size: int = 8, alpha_threshold: int = 8,
                 snap: int = 0) -> list[Rect]:
    """Find rectangular regions in a texture automatically.

    Two strategies, picked by what the image carries:

    * **Alpha islands** - when the texture has a real alpha channel, each
      connected opaque region becomes one rect from its bounding box. This is
      what a cut-out trim sheet wants.
    * **Seam detection** - otherwise, look for fully-uniform rows and columns
      (the gutters between panels) and cut the texture along them. This is what
      a solid trim sheet wants, and it is what actually finds the panel edges on
      a sheet like Valve's tiles.

    v2 scanned a fixed grid and emitted whole cells regardless of content, which
    could only ever reproduce the grid it was told to use.
    """
    import numpy as np

    pixels = np.asarray(image)
    if pixels.ndim == 2:
        pixels = pixels[:, :, None]
    height, width = pixels.shape[:2]

    if pixels.shape[2] >= 4 and int(pixels[:, :, 3].min()) < alpha_threshold:
        rects = _detect_alpha_islands(pixels[:, :, 3], alpha_threshold, min_size)
    else:
        rects = _detect_seams(pixels[:, :, :3], min_size)

    if snap > 1:
        rects = [rect.snapped(snap, width, height) for rect in rects]
    return [rect for rect in rects if rect.w >= min_size and rect.h >= min_size]


def _detect_alpha_islands(alpha: Any, threshold: int, min_size: int) -> list[Rect]:
    """Bounding box of every connected opaque region."""
    import numpy as np

    mask = alpha >= threshold
    try:
        from scipy import ndimage
    except ImportError:
        # Without scipy, fall back to one box around everything opaque.
        ys, xs = np.nonzero(mask)
        if not len(xs):
            return []
        return [Rect(int(xs.min()), int(ys.min()),
                     int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1))]

    labels, count = ndimage.label(mask)
    rects: list[Rect] = []
    for y_slice, x_slice in ndimage.find_objects(labels):
        rect = Rect(int(x_slice.start), int(y_slice.start),
                    int(x_slice.stop - x_slice.start),
                    int(y_slice.stop - y_slice.start))
        if rect.w >= min_size and rect.h >= min_size:
            rects.append(rect)
    return rects


def _detect_seams(rgb: Any, min_size: int, *, depth: int = 6) -> list[Rect]:
    """Cut the texture along uniform rows and columns, recursively.

    A trim sheet's panels are separated by a constant-colour gutter or a hard
    edge, and a line of pixels that is all one colour is almost always such a
    seam rather than real art.

    The recursion is what makes this work on real sheets. Panels are rarely a
    single grid: a sheet is usually bands, with each band split differently. A
    gutter between two panels in the top band is *not* uniform over the whole
    texture, because whatever sits below it interrupts the line. So each cut
    splits the region and the halves are examined independently - the classic
    guillotine decomposition, which is also how trim sheets are drawn.
    """
    height, width = rgb.shape[:2]
    found: list[Rect] = []
    _cut_region(rgb, 0, 0, width, height, min_size, depth, found)
    return found


def _cut_region(rgb: Any, x: int, y: int, width: int, height: int,
                min_size: int, depth: int, out: list[Rect]) -> None:
    import numpy as np

    if width < min_size or height < min_size:
        return
    region = rgb[y:y + height, x:x + width]

    if depth > 0:
        # Horizontal first: sheets band vertically more often than not, and
        # cutting the long way first gives the second pass more to work with.
        rows = np.all(region == region[:, :1, :], axis=(1, 2))
        for split in _interior_runs(rows, height, min_size):
            _cut_region(rgb, x, y, width, split, min_size, depth - 1, out)
            _cut_region(rgb, x, y + split, width, height - split,
                        min_size, depth - 1, out)
            return

        columns = np.all(region == region[:1, :, :], axis=(0, 2))
        for split in _interior_runs(columns, width, min_size):
            _cut_region(rgb, x, y, split, height, min_size, depth - 1, out)
            _cut_region(rgb, x + split, y, width - split, height,
                        min_size, depth - 1, out)
            return

    out.append(Rect(x, y, width, height))


def _interior_runs(uniform: Any, extent: int, min_size: int) -> list[int]:
    """Midpoints of uniform runs that lie strictly inside the region.

    Runs touching either edge are skipped: those are the sheet's own border, and
    cutting there would shave a sliver off instead of separating two panels.
    Returns at most one split - the recursion finds the rest.
    """
    splits: list[int] = []
    start: Optional[int] = None
    for index in range(extent):
        if uniform[index]:
            if start is None:
                start = index
        elif start is not None:
            if start > 0:
                middle = (start + index) // 2
                if min_size <= middle <= extent - min_size:
                    splits.append(middle)
            start = None
    return splits[:1]


# ----------------------------------------------------------------------
# Finding Valve's own rects
# ----------------------------------------------------------------------

def find_matching_rect(texture_path: Any, roots: Iterable[Any]) -> Optional[Path]:
    """Locate a shipped `.rect` that belongs to this texture.

    Porting an HLA or CS2 hotspot material means Valve already authored the
    rectangles - importing them beats redrawing them by eye. The texture keeps
    its name through a VRF export, so the stem is enough to find it, after
    stripping the channel suffixes the exporter appends.
    """
    stem = Path(texture_path).stem.lower()
    for suffix in ("_color", "_normal", "_rough", "_metal", "_ao", "_trans",
                   "_selfillum", "_envmask", "_phong"):
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
            break

    candidates = [stem, f"{stem}_hs", f"{stem}_rect"]
    for root in roots:
        folder = Path(root)
        if not folder.is_dir():
            continue
        for found in folder.rglob("*.rect"):
            if found.stem.lower() in candidates:
                return found
    return None
