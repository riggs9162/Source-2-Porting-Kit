"""
The shared log pane.

Every tool page gets one. Levelled colours, level filter chips, text search,
follow-tail toggle, wrap toggle, copy and save-to-file - all driven by the
log preferences so a change in Preferences applies to every open tool.

Lines are appended in batches by the job pump rather than one at a time, and
the in-memory buffer is capped by `log_buffer_lines`; a run that emits 200k
lines stays responsive because the ListView builds rows on demand.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from pathlib import Path
from typing import Iterable, Optional, Sequence

import flet as ft

from s2kit.core import paths
from s2kit.core.jobs import LEVELS, LogLine
from s2kit.ui import common, theme
from s2kit.ui.context import AppContext
from s2kit.ui.controls import is_ready, state

logger = logging.getLogger(__name__)


@ft.control
class LogView(ft.Container):
    """A filterable, levelled log pane fed by :class:`LogLine` batches."""

    ctx: Optional[AppContext] = state()
    tool_id: str = state("")

    def init(self):
        self._ready = False
        settings = self.ctx.settings if self.ctx else None
        cap = int(settings.get("log_buffer_lines", 5000)) if settings else 5000
        self._lines: deque[LogLine] = deque(maxlen=max(200, cap))
        self._levels: set[str] = set(LEVELS)
        self._search = ""
        self._follow = bool(settings.get("log_autoscroll", True)) if settings else True
        self._wrap = bool(settings.get("log_wrap", False)) if settings else False
        self._show_time = (bool(settings.get("log_show_timestamps", True))
                           if settings else True)
        self.expand = True

        minimum = str(settings.get("log_min_level", "DEBUG")) if settings else "DEBUG"
        if minimum in LEVELS:
            index = LEVELS.index(minimum)
            self._levels = set(LEVELS[index:]) | {"SUCCESS"}

    def build(self):
        self._list = ft.ListView(
            expand=True,
            spacing=0,
            auto_scroll=self._follow,
            build_controls_on_demand=True,
            padding=ft.Padding.symmetric(horizontal=10, vertical=6),
        )
        self._counter = common.caption("no output yet")
        self._chips = {
            level: ft.Chip(
                label=ft.Text(level.title(), size=10),
                selected=level in self._levels,
                show_checkmark=False,
                on_select=lambda e, lv=level: self._toggle_level(lv, e.control.selected),
                selected_color=ft.Colors.with_opacity(0.22,
                                                      theme.level_colour(level)),
                padding=ft.Padding.symmetric(horizontal=2, vertical=0),
                visual_density=ft.VisualDensity.COMPACT,
            )
            for level in LEVELS
        }
        self._search_field = ft.TextField(
            hint_text="Search log...",
            prefix_icon=ft.Icons.SEARCH,
            dense=True, height=34, width=220, text_size=12,
            on_change=self._on_search,
        )
        self._follow_button = ft.IconButton(
            ft.Icons.VERTICAL_ALIGN_BOTTOM,
            tooltip="Follow new output",
            icon_size=16,
            selected=self._follow,
            selected_icon=ft.Icons.VERTICAL_ALIGN_BOTTOM,
            icon_color=(ft.Colors.PRIMARY if self._follow
                        else ft.Colors.ON_SURFACE_VARIANT),
            on_click=self._toggle_follow,
        )
        self._wrap_button = ft.IconButton(
            ft.Icons.WRAP_TEXT, tooltip="Wrap long lines", icon_size=16,
            icon_color=(ft.Colors.PRIMARY if self._wrap
                        else ft.Colors.ON_SURFACE_VARIANT),
            on_click=self._toggle_wrap,
        )

        toolbar = ft.Row(
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                self._search_field,
                ft.Row(list(self._chips.values()), spacing=4, tight=True),
                ft.Container(expand=True),
                self._counter,
                self._follow_button,
                self._wrap_button,
                ft.IconButton(ft.Icons.CONTENT_COPY, tooltip="Copy log",
                              icon_size=16, on_click=self._copy),
                ft.IconButton(ft.Icons.SAVE_ALT, tooltip="Save log to file",
                              icon_size=16, on_click=self._save),
                ft.IconButton(ft.Icons.DELETE_SWEEP, tooltip="Clear log",
                              icon_size=16, on_click=lambda _e: self.clear()),
            ],
        )

        self.content = ft.Column(
            expand=True,
            spacing=6,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                toolbar,
                ft.Container(
                    expand=True,
                    bgcolor=ft.Colors.with_opacity(
                        0.55, ft.Colors.SURFACE_CONTAINER_LOWEST),
                    border_radius=ft.BorderRadius.all(8),
                    border=ft.Border.all(
                        1, ft.Colors.with_opacity(0.12, ft.Colors.OUTLINE)),
                    content=self._list,
                ),
            ],
        )
        self._ready = True
        self._rebuild()

    # ------------------------------------------------------------------
    # Feeding
    # ------------------------------------------------------------------

    def append(self, lines: Iterable[LogLine]) -> None:
        """Add a batch of lines; only the visible ones reach the ListView."""
        if not is_ready(self):
            self._lines.extend(lines)
            return
        added = 0
        for line in lines:
            self._lines.append(line)
            if self._passes(line):
                self._list.controls.append(self._row(line))
                added += 1
        # The deque discards old lines; mirror that in the rendered list so the
        # two never drift apart on a very long run.
        overflow = len(self._list.controls) - (self._lines.maxlen or 5000)
        if overflow > 0:
            del self._list.controls[:overflow]
        if added:
            self._update_counter()
            _safe_update(self._list)
            _safe_update(self._counter)

    def set_lines(self, lines: Sequence[LogLine]) -> None:
        """Replace the whole buffer (loading a history record's log)."""
        self._lines.clear()
        self._lines.extend(lines)
        if is_ready(self):
            self._rebuild()

    def clear(self) -> None:
        self._lines.clear()
        if not is_ready(self):
            return
        self._list.controls = []
        self._update_counter()
        _safe_update(self._list)
        _safe_update(self._counter)

    def lines(self) -> list[LogLine]:
        return list(self._lines)

    def as_text(self) -> str:
        return "\n".join(line.formatted(self._show_time) for line in self._lines)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _row(self, line: LogLine) -> ft.Control:
        settings = self.ctx.settings
        colour = theme.level_colour(line.level)
        font = theme.mono_font(settings)
        size = theme.scaled(settings, 11.5)

        spans: list[ft.Control] = []
        if self._show_time:
            spans.append(ft.Text(time.strftime("%H:%M:%S",
                                               time.localtime(line.timestamp)),
                                 font_family=font, size=size,
                                 color=ft.Colors.with_opacity(0.55,
                                                              ft.Colors.ON_SURFACE)))
        spans.append(ft.Text(f"{line.level:<7}", font_family=font, size=size,
                             color=colour, weight=ft.FontWeight.W_600))
        spans.append(ft.Text(line.message, font_family=font, size=size,
                             color=colour if line.level in ("ERROR", "WARNING")
                             else None,
                             no_wrap=not self._wrap,
                             overflow=(ft.TextOverflow.ELLIPSIS if not self._wrap
                                       else None),
                             selectable=True, expand=True))
        return ft.Row(spans, spacing=8, tight=True,
                      vertical_alignment=ft.CrossAxisAlignment.START)

    def _passes(self, line: LogLine) -> bool:
        if line.level not in self._levels:
            return False
        if self._search and self._search not in line.message.lower():
            return False
        return True

    def _rebuild(self) -> None:
        if not is_ready(self):
            return
        self._list.controls = [self._row(line) for line in self._lines
                              if self._passes(line)]
        self._update_counter()
        _safe_update(self._list)
        _safe_update(self._counter)

    def _update_counter(self) -> None:
        total = len(self._lines)
        shown = len(self._list.controls)
        if not total:
            self._counter.value = "no output yet"
        elif shown == total:
            self._counter.value = f"{total:,} lines"
        else:
            self._counter.value = f"{shown:,} of {total:,} lines"

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _toggle_level(self, level: str, selected: bool) -> None:
        if selected:
            self._levels.add(level)
        else:
            self._levels.discard(level)
        self._rebuild()

    def _on_search(self, event: ft.ControlEvent) -> None:
        self._search = (event.control.value or "").strip().lower()
        self._rebuild()

    def _toggle_follow(self, _event: ft.ControlEvent) -> None:
        self._follow = not self._follow
        self._list.auto_scroll = self._follow
        self._follow_button.icon_color = (ft.Colors.PRIMARY if self._follow
                                          else ft.Colors.ON_SURFACE_VARIANT)
        _safe_update(self._list)
        _safe_update(self._follow_button)

    def _toggle_wrap(self, _event: ft.ControlEvent) -> None:
        self._wrap = not self._wrap
        self._wrap_button.icon_color = (ft.Colors.PRIMARY if self._wrap
                                        else ft.Colors.ON_SURFACE_VARIANT)
        _safe_update(self._wrap_button)
        self._rebuild()

    def _copy(self, _event: ft.ControlEvent) -> None:
        text = self.as_text()
        if not text:
            self.ctx.toast("The log is empty", level="WARNING")
            return
        self.ctx.copy(text, notice=f"Copied {len(self._lines):,} log lines")

    def _save(self, _event: ft.ControlEvent) -> None:
        if not self._lines:
            self.ctx.toast("The log is empty", level="WARNING")
            return
        directory = paths.logs_dir() / "runs"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = directory / f"{self.tool_id or 'tool'}-{stamp}.log"
        try:
            target.write_text(self.as_text(), encoding="utf-8")
        except OSError as exc:
            self.ctx.toast(f"Could not write the log: {exc}", level="ERROR")
            return
        self.ctx.toast(f"Saved {target.name}", level="SUCCESS", action="Show",
                       on_action=lambda _e: self.ctx.reveal(target))

    # ------------------------------------------------------------------
    # Preferences
    # ------------------------------------------------------------------

    def apply_preferences(self) -> None:
        """Re-read the log preferences after the user changed them."""
        if not is_ready(self):
            return
        settings = self.ctx.settings
        cap = max(200, int(settings.get("log_buffer_lines", 5000)))
        if cap != self._lines.maxlen:
            self._lines = deque(self._lines, maxlen=cap)
        self._show_time = bool(settings.get("log_show_timestamps", True))
        self._wrap = bool(settings.get("log_wrap", False))
        self._follow = bool(settings.get("log_autoscroll", True))
        self._list.auto_scroll = self._follow
        minimum = str(settings.get("log_min_level", "DEBUG"))
        if minimum in LEVELS:
            self._levels = set(LEVELS[LEVELS.index(minimum):]) | {"SUCCESS"}
            for level, chip in self._chips.items():
                chip.selected = level in self._levels
                _safe_update(chip)
        self._rebuild()

    def write_run_file(self, tool_id: str) -> Optional[Path]:
        """Persist this run's log next to the app log (the `log_to_file` pref)."""
        if not self._lines:
            return None
        directory = paths.logs_dir() / "runs"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / (f"{tool_id}-"
                                  f"{time.strftime('%Y%m%d-%H%M%S')}.log")
            target.write_text(self.as_text(), encoding="utf-8")
            return target
        except OSError as exc:
            logger.warning("Could not write run log: %s", exc)
            return None


def _safe_update(control: object) -> None:
    try:
        control.update()  # type: ignore[attr-defined]
    except Exception:
        pass
