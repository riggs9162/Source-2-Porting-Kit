"""
Texture viewer.

Anything Pillow reads, plus VTF through sourcepp: mip and frame selection,
channel isolation, pan/zoom, an alpha-aware background, a log-scale histogram
and the full header info panel. Background, scaling filter and the alpha view
all follow the Viewers preferences.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import flet as ft

from s2kit.media import images
from s2kit.ui import common
from s2kit.ui.controls import state
from s2kit.ui.viewers.base import BaseViewer

CHANNEL_LABELS = (
    ("rgba", "RGBA"),
    ("rgb", "RGB"),
    ("r", "R"),
    ("g", "G"),
    ("b", "B"),
    ("a", "A"),
    ("luminance", "Lum"),
)


@ft.control
class ImageViewer(BaseViewer):
    """Texture preview with channel, mip and background controls."""

    display_name: str = state("Texture")

    def init(self):
        super().init()
        self._channel = "rgba"
        self._mip = 0
        self._frame = 0
        self._info: Optional[images.TextureInfo] = None
        self._show_histogram = False

    def build_toolbar(self) -> list[ft.Control]:
        settings = self.ctx.settings
        self._channel_button = ft.SegmentedButton(
            segments=[ft.Segment(value=key, label=ft.Text(label, size=11))
                      for key, label in CHANNEL_LABELS],
            selected=[self._channel],
            allow_empty_selection=False,
            show_selected_icon=False,
            on_change=self._on_channel,
        )
        self._mip_dropdown = ft.Dropdown(
            value="0",
            options=[ft.DropdownOption(key="0", text="Mip 0")],
            width=110, dense=True, text_size=11,
            on_select=self._on_mip,
            visible=False,
        )
        self._frame_dropdown = ft.Dropdown(
            value="0",
            options=[ft.DropdownOption(key="0", text="Frame 0")],
            width=120, dense=True, text_size=11,
            on_select=self._on_frame,
            visible=False,
        )
        self._background_dropdown = ft.Dropdown(
            value=str(settings.get("viewer_bg", "checker")),
            options=[ft.DropdownOption(key=key, text=label) for key, label in (
                ("checker", "Checker"), ("dark", "Dark"), ("light", "Light"),
                ("magenta", "Magenta"))],
            width=120, dense=True, text_size=11,
            on_select=self._on_background,
        )
        self._histogram_button = ft.IconButton(
            ft.Icons.BAR_CHART, tooltip="Toggle histogram", icon_size=17,
            on_click=self._toggle_histogram,
        )
        return [
            self._channel_button,
            self._mip_dropdown,
            self._frame_dropdown,
            self._background_dropdown,
            self._histogram_button,
            ft.IconButton(ft.Icons.REFRESH, tooltip="Reload", icon_size=17,
                          on_click=lambda _e: self.reload()),
        ]

    # ------------------------------------------------------------------
    # Decode / present
    # ------------------------------------------------------------------

    def decode(self, path: Path) -> dict[str, Any]:
        settings = self.ctx.settings
        png, info = images.render_preview(
            path,
            max_size=2048,
            channel=self._channel,
            background=str(settings.get("viewer_bg", "checker")),
            mip=self._mip,
            frame=self._frame,
            smooth=bool(settings.get("image_smooth", False)),
        )
        payload: dict[str, Any] = {"png": png, "info": info, "histogram": None}
        if self._show_histogram:
            try:
                payload["histogram"] = images.histogram(path)
            except Exception:
                payload["histogram"] = None
        return payload

    def present(self, payload: dict[str, Any]) -> ft.Control:
        info: images.TextureInfo = payload["info"]
        self._info = info
        self._sync_dropdowns(info)
        settings = self.ctx.settings

        canvas = ft.InteractiveViewer(
            expand=True,
            min_scale=0.2,
            max_scale=32,
            scale_enabled=True,
            pan_enabled=True,
            content=ft.Image(
                src=payload["png"],
                fit=ft.BoxFit.CONTAIN,
                expand=True,
                filter_quality=(ft.FilterQuality.MEDIUM
                                if bool(settings.get("image_smooth", False))
                                else ft.FilterQuality.NONE),
                gapless_playback=True,
            ),
        )

        side: list[ft.Control] = [
            common.section("Texture", common.info_table(info.rows(), settings),
                           icon_name="IMAGE"),
            common.section("File", common.info_table(self.file_rows(), settings),
                           icon_name="DESCRIPTION"),
        ]
        if payload.get("histogram"):
            side.insert(1, common.section(
                "Histogram",
                ft.Image(src=payload["histogram"], width=256, height=96,
                         fit=ft.BoxFit.FILL),
                subtitle="log scale, per channel",
                icon_name="BAR_CHART",
            ))

        return ft.Row(
            expand=True,
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.START,
            controls=[
                ft.Container(
                    expand=True,
                    padding=6,
                    border_radius=ft.BorderRadius.all(8),
                    bgcolor=ft.Colors.with_opacity(
                        0.5, ft.Colors.SURFACE_CONTAINER_LOWEST),
                    border=ft.Border.all(
                        1, ft.Colors.with_opacity(0.12, ft.Colors.OUTLINE)),
                    content=canvas,
                ),
                ft.Container(
                    width=300,
                    content=ft.Column(side, spacing=10, tight=True,
                                      scroll=ft.ScrollMode.AUTO),
                ),
            ],
        )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _sync_dropdowns(self, info: images.TextureInfo) -> None:
        if info.mip_count > 1:
            self._mip_dropdown.options = [
                ft.DropdownOption(key=str(index),
                                  text=f"Mip {index}"
                                       f"  ({max(1, info.width >> index)}px)")
                for index in range(info.mip_count)
            ]
            self._mip_dropdown.value = str(min(self._mip, info.mip_count - 1))
            self._mip_dropdown.visible = True
        else:
            self._mip_dropdown.visible = False
        _safe_update(self._mip_dropdown)

        if info.frame_count > 1:
            self._frame_dropdown.options = [
                ft.DropdownOption(key=str(index), text=f"Frame {index}")
                for index in range(info.frame_count)
            ]
            self._frame_dropdown.value = str(min(self._frame, info.frame_count - 1))
            self._frame_dropdown.visible = True
        else:
            self._frame_dropdown.visible = False
        _safe_update(self._frame_dropdown)

    def _on_channel(self, event: ft.ControlEvent) -> None:
        for value in event.control.selected or ():
            self._channel = str(value)
            break
        self.reload()

    def _on_mip(self, event: ft.ControlEvent) -> None:
        self._mip = int(event.control.value or 0)
        self.reload()

    def _on_frame(self, event: ft.ControlEvent) -> None:
        self._frame = int(event.control.value or 0)
        self.reload()

    def _on_background(self, event: ft.ControlEvent) -> None:
        # Persisted: the background is a real preference, not view-local state.
        self.ctx.settings.set("viewer_bg", str(event.control.value))
        self.reload()

    def _toggle_histogram(self, _event: ft.ControlEvent) -> None:
        self._show_histogram = not self._show_histogram
        self._histogram_button.icon_color = (ft.Colors.PRIMARY
                                             if self._show_histogram else None)
        _safe_update(self._histogram_button)
        self.reload()


def _safe_update(control: object) -> None:
    try:
        control.update()  # type: ignore[attr-defined]
    except Exception:
        pass
