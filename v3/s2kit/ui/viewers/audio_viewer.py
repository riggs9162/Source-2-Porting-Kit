"""
Sound player.

Renders a waveform (with an optional RMS envelope), shows the container /
codec / loop-point metadata that matters when porting Source sounds, and
provides transport.

Playback: the bundled Flet desktop client ships no audio widget, so transport
hands the file to the OS default player. If the optional `flet-audio` extension
is importable *and* the running client supports it, the viewer upgrades itself
to in-app play/pause/seek - see :meth:`AudioViewer._try_inline_player`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import flet as ft

from s2kit.bootstrap import module_available
from s2kit.media import audio
from s2kit.ui import common
from s2kit.ui.controls import state
from s2kit.ui.viewers.base import BaseViewer

logger = logging.getLogger(__name__)


@ft.control
class AudioViewer(BaseViewer):
    """Waveform + metadata + transport for a sound file."""

    display_name: str = state("Sound")

    def init(self):
        super().init()
        self._inline = None      # optional flet_audio.Audio control

    def build_toolbar(self) -> list[ft.Control]:
        return [
            ft.FilledTonalButton("Play", icon=ft.Icons.PLAY_ARROW,
                                 on_click=self._play),
            ft.OutlinedButton("Reveal", icon=ft.Icons.FOLDER_OPEN,
                              on_click=lambda _e: self.ctx.reveal(self.path)),
            ft.IconButton(ft.Icons.REFRESH, tooltip="Reload", icon_size=17,
                          on_click=lambda _e: self.reload()),
        ]

    # ------------------------------------------------------------------
    # Decode / present
    # ------------------------------------------------------------------

    def decode(self, path: Path) -> dict[str, Any]:
        settings = self.ctx.settings
        height = int(settings.get("waveform_height", 140))
        png, info = audio.render_waveform(
            path,
            width=1100,
            height=height,
            show_rms=bool(settings.get("waveform_rms", True)),
            ffmpeg_override=str(settings.get("ffmpeg_path", "") or ""),
        )
        return {"png": png, "info": info}

    def present(self, payload: dict[str, Any]) -> ft.Control:
        info: audio.AudioInfo = payload["info"]
        settings = self.ctx.settings

        waveform = ft.Container(
            padding=4,
            border_radius=ft.BorderRadius.all(8),
            bgcolor=ft.Colors.with_opacity(0.5, ft.Colors.SURFACE_CONTAINER_LOWEST),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.12, ft.Colors.OUTLINE)),
            content=ft.Row(controls=[
                ft.Image(src=payload["png"], fit=ft.BoxFit.FILL, expand=True,
                         height=int(settings.get("waveform_height", 140)),
                         gapless_playback=True),
            ]),
        )

        controls: list[ft.Control] = [waveform]
        inline = self._try_inline_player()
        if inline is not None:
            controls.append(inline)

        loop_note = ""
        if info.loop_start is not None:
            seconds = (info.loop_start / info.sample_rate
                       if info.sample_rate else 0.0)
            loop_note = f"Loop point at sample {info.loop_start:,} ({seconds:.3f}s)"

        return ft.Column(
            expand=True,
            spacing=12,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                *controls,
                *([common.pill(loop_note, colour=ft.Colors.AMBER_400,
                               icon_name="LOOP")] if loop_note else []),
                ft.Row(
                    spacing=12,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                    controls=[
                        ft.Container(
                            expand=True,
                            content=common.section(
                                "Sound", common.info_table(info.rows(), settings),
                                icon_name="AUDIOTRACK"),
                        ),
                        ft.Container(
                            width=320,
                            content=common.section(
                                "File",
                                common.info_table(self.file_rows(), settings),
                                icon_name="DESCRIPTION"),
                        ),
                    ],
                ),
            ],
        )

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    def _play(self, _event: ft.ControlEvent) -> None:
        if self._inline is not None:
            try:
                self.ctx.page.run_task(self._inline.play)
                return
            except Exception:
                logger.debug("Inline playback failed; falling back to the OS")
        if self.path is not None:
            self.ctx.open_external(self.path)

    def _try_inline_player(self) -> Optional[ft.Control]:
        """Build an in-app transport when the audio extension is available.

        Guarded twice: the Python package has to be importable *and* control
        construction has to succeed. A client built without the extension would
        otherwise fail at render time with an unknown-control error.
        """
        if not module_available("flet_audio"):
            return None
        try:
            import flet_audio  # type: ignore

            self._inline = flet_audio.Audio(src=str(self.path), autoplay=False)
            self.ctx.page.services.append(self._inline)
            return ft.Row(
                spacing=8,
                controls=[
                    ft.IconButton(ft.Icons.PLAY_ARROW, icon_size=20,
                                  tooltip="Play",
                                  on_click=lambda _e: self.ctx.page.run_task(
                                      self._inline.play)),
                    ft.IconButton(ft.Icons.PAUSE, icon_size=20, tooltip="Pause",
                                  on_click=lambda _e: self.ctx.page.run_task(
                                      self._inline.pause)),
                    ft.IconButton(ft.Icons.STOP, icon_size=20, tooltip="Stop",
                                  on_click=lambda _e: self.ctx.page.run_task(
                                      self._inline.release)),
                    common.caption("in-app playback (flet-audio)"),
                ],
            )
        except Exception as exc:
            logger.debug("flet-audio unavailable in this client: %s", exc)
            self._inline = None
            return None
