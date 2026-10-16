"""
Viewer registry.

Maps a :class:`~s2kit.media.probe.MediaKind` to the control that displays it.
Tools never pick a viewer - they hand the Preview pane a path and the right
one appears.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

import flet as ft

from s2kit.media.probe import MediaKind, kind_for
from s2kit.ui import common
from s2kit.ui.context import AppContext
from s2kit.ui.controls import state
from s2kit.ui.viewers.audio_viewer import AudioViewer
from s2kit.ui.viewers.base import BaseViewer
from s2kit.ui.viewers.image_viewer import ImageViewer
from s2kit.ui.viewers.model_viewer import ModelViewer
from s2kit.ui.viewers.text_viewer import DataViewer, TextViewer

__all__ = [
    "MediaKind", "BaseViewer", "ImageViewer", "ModelViewer", "AudioViewer",
    "TextViewer", "DataViewer", "InfoViewer", "viewer_for", "create_viewer",
    "kind_icon",
]


@ft.control
class InfoViewer(BaseViewer):
    """Fallback for binaries and folders: facts, no attempt at rendering."""

    display_name: str = state("File")

    def decode(self, path: Path) -> dict[str, Any]:
        header = b""
        if path.is_file():
            with open(path, "rb") as handle:
                header = handle.read(256)
        return {"header": header}

    def present(self, payload: dict[str, Any]) -> ft.Control:
        settings = self.ctx.settings
        blocks: list[ft.Control] = [
            common.section("File", common.info_table(self.file_rows(), settings),
                           icon_name="DESCRIPTION"),
        ]
        header: bytes = payload["header"]
        if header:
            dump = "\n".join(_hex_lines(header))
            blocks.append(common.section(
                "First bytes", common.mono(dump, settings), icon_name="MEMORY",
                subtitle="no viewer handles this format yet"))
        return ft.Column(blocks, spacing=10, expand=True,
                         scroll=ft.ScrollMode.AUTO)


#: MediaKind -> viewer factory.
VIEWERS: dict[MediaKind, Callable[..., BaseViewer]] = {
    MediaKind.IMAGE: ImageViewer,
    MediaKind.MODEL: ModelViewer,
    MediaKind.AUDIO: AudioViewer,
    MediaKind.TEXT: TextViewer,
    MediaKind.DATA: DataViewer,
}

KIND_ICONS: dict[MediaKind, str] = {
    MediaKind.IMAGE: "IMAGE",
    MediaKind.MODEL: "VIEW_IN_AR",
    MediaKind.AUDIO: "AUDIOTRACK",
    MediaKind.TEXT: "DESCRIPTION",
    MediaKind.DATA: "TABLE_CHART",
    MediaKind.ARCHIVE: "ARCHIVE",
    MediaKind.BINARY: "MEMORY",
    MediaKind.FOLDER: "FOLDER",
    MediaKind.MISSING: "BROKEN_IMAGE",
}


def kind_icon(kind: MediaKind) -> str:
    """`ft.Icons` member name for a media kind."""
    return KIND_ICONS.get(kind, "INSERT_DRIVE_FILE")


def viewer_for(path: Any) -> Callable[..., BaseViewer]:
    """The viewer class that should open `path`."""
    return VIEWERS.get(kind_for(path), InfoViewer)


def create_viewer(ctx: AppContext, path: Any,
                  kind: Optional[MediaKind] = None) -> BaseViewer:
    """Instantiate the right viewer for a path (without loading it yet)."""
    factory = VIEWERS.get(kind, None) if kind else viewer_for(path)
    return (factory or InfoViewer)(ctx=ctx)


def _hex_lines(data: bytes, per_line: int = 16) -> list[str]:
    lines: list[str] = []
    for offset in range(0, len(data), per_line):
        chunk = data[offset:offset + per_line]
        hex_part = " ".join(f"{byte:02x}" for byte in chunk).ljust(per_line * 3 - 1)
        ascii_part = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in chunk)
        lines.append(f"{offset:08x}  {hex_part}  {ascii_part}")
    return lines
