"""
File-kind detection and generic metadata.

The Preview tab needs to answer one question fast - "which viewer opens this?"
- without decoding anything. Extension first, magic bytes as a tiebreaker for
the Source formats that share suffixes with unrelated things.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class MediaKind(str, Enum):
    """Which viewer a file belongs to."""

    IMAGE = "image"
    MODEL = "model"
    AUDIO = "audio"
    TEXT = "text"
    DATA = "data"
    ARCHIVE = "archive"
    BINARY = "binary"
    FOLDER = "folder"
    MISSING = "missing"


IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tga", ".tif", ".tiff",
    ".dds", ".vtf", ".pfm", ".hdr", ".ppm", ".ico",
}

MODEL_EXTENSIONS = {
    ".smd", ".dmx", ".obj", ".stl", ".ply", ".glb", ".gltf", ".dae", ".fbx",
    ".mdl", ".vmdl", ".vmdl_c",
}

AUDIO_EXTENSIONS = {
    ".wav", ".ogg", ".mp3", ".flac", ".m4a", ".aac", ".opus", ".wma", ".aif",
    ".aiff",
}

TEXT_EXTENSIONS = {
    ".txt", ".md", ".log", ".qc", ".qci", ".vmt", ".vmat", ".kv", ".kv3",
    ".vdf", ".res", ".cfg", ".ini", ".json", ".xml", ".yaml", ".yml", ".py",
    ".lua", ".hlsl", ".fx", ".glsl", ".csv", ".tsv", ".bat", ".ps1", ".sh",
    ".vbsp", ".vmf", ".vmap_prefab", ".gitignore",
}

DATA_EXTENSIONS = {".csv", ".tsv", ".json"}

ARCHIVE_EXTENSIONS = {".zip", ".vpk", ".gma", ".7z", ".rar", ".tar", ".gz", ".bsp"}

#: Magic bytes -> kind, checked when the extension is ambiguous or absent.
MAGIC: tuple[tuple[bytes, MediaKind], ...] = (
    (b"VTF\x00", MediaKind.IMAGE),
    (b"\x89PNG", MediaKind.IMAGE),
    (b"\xff\xd8\xff", MediaKind.IMAGE),
    (b"DDS ", MediaKind.IMAGE),
    (b"glTF", MediaKind.MODEL),
    (b"IDST", MediaKind.MODEL),
    (b"RIFF", MediaKind.AUDIO),
    (b"OggS", MediaKind.AUDIO),
    (b"fLaC", MediaKind.AUDIO),
    (b"ID3", MediaKind.AUDIO),
    (b"PK\x03\x04", MediaKind.ARCHIVE),
)


def kind_for(path: Any) -> MediaKind:
    """Which :class:`MediaKind` a path is, cheaply."""
    target = Path(path)
    if not target.exists():
        return MediaKind.MISSING
    if target.is_dir():
        return MediaKind.FOLDER

    suffix = target.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return MediaKind.IMAGE
    if suffix in MODEL_EXTENSIONS:
        return MediaKind.MODEL
    if suffix in AUDIO_EXTENSIONS:
        return MediaKind.AUDIO
    if suffix in DATA_EXTENSIONS:
        return MediaKind.DATA
    if suffix in TEXT_EXTENSIONS:
        return MediaKind.TEXT
    if suffix in ARCHIVE_EXTENSIONS:
        return MediaKind.ARCHIVE

    header = _read_header(target)
    for magic, kind in MAGIC:
        if header.startswith(magic):
            return kind
    return MediaKind.TEXT if _looks_like_text(header) else MediaKind.BINARY


@dataclass
class FileDescription:
    """Generic facts about a file, shown above every viewer."""

    path: Path
    kind: MediaKind
    size: int = 0
    modified: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def exists(self) -> bool:
        return self.kind is not MediaKind.MISSING

    def size_text(self) -> str:
        return human_size(self.size)

    def modified_text(self) -> str:
        if not self.modified:
            return "-"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.modified))

    def rows(self) -> list[tuple[str, str]]:
        """Label/value pairs for the info table."""
        base = [
            ("Name", self.path.name),
            ("Folder", str(self.path.parent)),
            ("Kind", self.kind.value),
            ("Size", self.size_text()),
            ("Modified", self.modified_text()),
        ]
        base.extend((key, str(value)) for key, value in self.extra.items())
        return base


def describe(path: Any) -> FileDescription:
    """Stat a file and label its kind, without decoding the contents."""
    target = Path(path)
    kind = kind_for(target)
    try:
        stat = target.stat()
        return FileDescription(target, kind, stat.st_size, stat.st_mtime)
    except OSError:
        return FileDescription(target, kind)


def human_size(size: float) -> str:
    """Byte count as a short human string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _read_header(path: Path, count: int = 512) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read(count)
    except OSError:
        return b""


def _looks_like_text(header: bytes) -> bool:
    if not header:
        return True
    if b"\x00" in header:
        return False
    printable = sum(1 for byte in header if 9 <= byte <= 13 or 32 <= byte < 127)
    return printable / len(header) > 0.85


def find_files(root: Any, extensions: Optional[set[str]] = None,
               recursive: bool = True, limit: int = 20_000) -> list[Path]:
    """List files under a folder, optionally filtered by extension.

    Shared by the folder-driven tools and the asset browser; capped so a
    mistaken pick of `C:\\` cannot hang the UI.
    """
    base = Path(root)
    if not base.is_dir():
        return []
    pattern = "**/*" if recursive else "*"
    found: list[Path] = []
    for candidate in base.glob(pattern):
        if not candidate.is_file():
            continue
        if extensions and candidate.suffix.lower() not in extensions:
            continue
        found.append(candidate)
        if len(found) >= limit:
            break
    return found
