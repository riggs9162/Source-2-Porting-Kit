"""
Viewer base class.

A viewer takes a path and shows it. Decoding always happens on a worker thread
(textures, meshes and waveforms are all slow enough to drop frames), so the
base class owns the loading/error/ready state machine and subclasses only
implement `decode()` - which runs off the UI thread - and `present()` - which
runs back on it.
"""

from __future__ import annotations

import logging
import traceback
from pathlib import Path
from typing import Any, Optional

import flet as ft

from s2kit.media import probe
from s2kit.ui import common
from s2kit.ui.context import AppContext
from s2kit.ui.controls import state

logger = logging.getLogger(__name__)


@ft.control
class BaseViewer(ft.Container):
    """Common chrome and threading for every file viewer."""

    ctx: Optional[AppContext] = state()
    path: Optional[Path] = state()

    #: Human name shown in the preview header.
    display_name: str = state("File")

    def init(self):
        self.expand = True
        self._generation = 0
        self._description: Optional[probe.FileDescription] = None

    def build(self):
        self._status = ft.Row(
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[ft.ProgressRing(width=14, height=14, stroke_width=2),
                      common.caption("Loading...")],
            visible=False,
        )
        self._error = ft.Container(visible=False)
        self._body = ft.Container(expand=True)
        self._toolbar = ft.Row(spacing=6, wrap=True,
                               vertical_alignment=ft.CrossAxisAlignment.CENTER,
                               controls=self.build_toolbar())
        self.content = ft.Column(
            expand=True,
            spacing=8,
            # STRETCH, or a wide child (a 1100px waveform, a long path field)
            # sizes the column to its own intrinsic width and overflows.
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[self._toolbar, self._status, self._error, self._body],
        )
        if self.path is not None:
            self.load(self.path)

    # ------------------------------------------------------------------
    # Subclass API
    # ------------------------------------------------------------------

    def build_toolbar(self) -> list[ft.Control]:
        """View-specific controls shown above the content."""
        return []

    def decode(self, path: Path) -> Any:
        """Read and prepare the file. Runs on a worker thread."""
        raise NotImplementedError

    def present(self, payload: Any) -> ft.Control:
        """Turn a decoded payload into controls. Runs on the UI thread."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load(self, path: Any) -> None:
        """Show `path`, decoding on a worker thread."""
        target = Path(path)
        self.path = target
        self._description = probe.describe(target)
        self._generation += 1
        generation = self._generation

        if not target.exists():
            self._show_error(f"{target} does not exist")
            return

        limit_mb = int(self.ctx.pref("preview_max_mb", 256) or 0)
        if limit_mb and self._description.size > limit_mb * 1024 * 1024:
            self._show_error(
                f"{self._description.size_text()} is above the "
                f"{limit_mb} MB preview limit (Preferences > Viewers)."
            )
            return

        self._set_busy(True)
        self._error.visible = False
        _safe_update(self._error)

        def work() -> None:
            try:
                payload = self.decode(target)
            except Exception as exc:
                logger.debug("Viewer decode failed for %s\n%s", target,
                             traceback.format_exc())
                if generation == self._generation:
                    self._show_error(str(exc) or type(exc).__name__)
                    self._set_busy(False)
                return
            if generation != self._generation:
                return   # a newer load superseded this one
            try:
                self._body.content = self.present(payload)
            except Exception as exc:
                logger.exception("Viewer present failed for %s", target)
                self._show_error(f"Could not display the file: {exc}")
            finally:
                self._set_busy(False)
                _safe_update(self._body)

        self.ctx.run_thread(work)

    def reload(self) -> None:
        if self.path is not None:
            self.load(self.path)

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def _set_busy(self, busy: bool) -> None:
        self._status.visible = busy
        _safe_update(self._status)

    def _show_error(self, message: str) -> None:
        self._error.content = ft.Container(
            padding=12,
            border_radius=ft.BorderRadius.all(8),
            bgcolor=ft.Colors.with_opacity(0.12, ft.Colors.RED_400),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.4, ft.Colors.RED_400)),
            content=ft.Row(
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.START,
                controls=[
                    ft.Icon(ft.Icons.ERROR_OUTLINE, size=16,
                            color=ft.Colors.RED_400),
                    ft.Text(message, size=12, no_wrap=False, expand=True,
                            selectable=True),
                ],
            ),
        )
        self._error.visible = True
        self._body.content = None
        _safe_update(self._error)
        _safe_update(self._body)

    @property
    def has_error(self) -> bool:
        """Whether the last load failed (used by the self-test)."""
        error = getattr(self, "_error", None)
        return bool(error is not None and error.visible)

    @property
    def busy(self) -> bool:
        status = getattr(self, "_status", None)
        return bool(status is not None and status.visible)

    def file_rows(self) -> list[tuple[str, str]]:
        """Generic file facts, for the info panel of any viewer."""
        return self._description.rows() if self._description else []

    def refresh_preferences(self) -> None:
        """Re-decode after a viewer preference changed."""
        self.reload()


def _safe_update(control: object) -> None:
    try:
        control.update()  # type: ignore[attr-defined]
    except Exception:
        pass
