"""
Text viewer.

Reads QC, VMT, VMAT, KV3, SMD, JSON, logs - anything textual - with line
numbers, search-and-highlight, wrap toggle and a copy button. Large files are
truncated to keep the UI responsive, and it says so rather than silently
lying about the contents.

The data viewer (CSV/TSV/JSON arrays) is here too: same file kind, but it
renders through the sortable table instead when the shape fits.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import flet as ft

from s2kit.core.jobs import Table
from s2kit.ui import common, theme
from s2kit.ui.controls import state
from s2kit.ui.viewers.base import BaseViewer

MAX_CHARACTERS = 2_000_000
MAX_LINES = 20_000
#: Lines rendered per batch; the rest arrive via 'Show more'.
CHUNK_LINES = 2_000


@ft.control
class TextViewer(BaseViewer):
    """Monospaced file viewer with line numbers and search."""

    display_name: str = state("Text")

    def init(self):
        super().init()
        self._search = ""
        self._wrap = False
        self._lines: list[str] = []
        self._visible = CHUNK_LINES

    def build_toolbar(self) -> list[ft.Control]:
        self._search_field = ft.TextField(
            hint_text="Find in file...",
            prefix_icon=ft.Icons.SEARCH,
            dense=True, height=34, width=240, text_size=12,
            on_change=self._on_search,
        )
        self._match_label = common.caption("")
        self._wrap_button = ft.IconButton(
            ft.Icons.WRAP_TEXT, tooltip="Wrap long lines", icon_size=17,
            on_click=self._toggle_wrap,
        )
        return [
            self._search_field,
            self._match_label,
            ft.Container(expand=True),
            self._wrap_button,
            ft.IconButton(ft.Icons.CONTENT_COPY, tooltip="Copy contents",
                          icon_size=17, on_click=self._copy),
            ft.IconButton(ft.Icons.OPEN_IN_NEW, tooltip="Open in editor",
                          icon_size=17,
                          on_click=lambda _e: self.ctx.open_external(self.path)),
            ft.IconButton(ft.Icons.REFRESH, tooltip="Reload", icon_size=17,
                          on_click=lambda _e: self.reload()),
        ]

    def decode(self, path: Path) -> dict[str, Any]:
        raw = path.read_text(encoding="utf-8", errors="replace")
        truncated = False
        if len(raw) > MAX_CHARACTERS:
            raw = raw[:MAX_CHARACTERS]
            truncated = True
        lines = raw.splitlines()
        if len(lines) > MAX_LINES:
            lines = lines[:MAX_LINES]
            truncated = True
        return {"lines": lines, "truncated": truncated, "characters": len(raw)}

    def present(self, payload: dict[str, Any]) -> ft.Control:
        self._lines = payload["lines"]
        self._visible = CHUNK_LINES
        settings = self.ctx.settings
        show_numbers = bool(settings.get("text_viewer_line_numbers", True))

        self._list = ft.ListView(expand=True, spacing=0,
                                 build_controls_on_demand=True,
                                 padding=ft.Padding.symmetric(horizontal=10,
                                                              vertical=8))
        self._show_numbers = show_numbers
        self._render_lines()

        header: list[ft.Control] = [
            common.caption(f"{len(self._lines):,} lines  -  "
                           f"{payload['characters']:,} characters")
        ]
        if payload["truncated"]:
            header.append(common.pill("truncated for preview",
                                      colour=ft.Colors.AMBER_400,
                                      icon_name="WARNING_AMBER"))

        return ft.Column(
            expand=True,
            spacing=6,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Row(header, spacing=8),
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

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_lines(self) -> None:
        """Render a window of lines.

        Every rendered line is two controls that get serialised to the client,
        so a 20k-line SMD would ship 40k of them and take the best part of a
        second before anything appeared. Only `CHUNK_LINES` are rendered at a
        time, with a button to pull in the next batch; a search narrows to the
        matching lines across the whole file rather than only the window.
        """
        settings = self.ctx.settings
        font = theme.mono_font(settings)
        size = theme.scaled(settings, 12)
        tab = " " * int(settings.get("text_viewer_tab_width", 4))
        needle = self._search.lower()
        width = len(str(len(self._lines))) if self._lines else 1

        if needle:
            selected = [(number, text) for number, text
                        in enumerate(self._lines, start=1)
                        if needle in text.lower()]
            total = len(selected)
        else:
            selected = list(enumerate(self._lines, start=1))
            total = len(selected)
        shown = selected[:self._visible]

        rows: list[ft.Control] = []
        for number, text in shown:
            body = text.replace("\t", tab)
            controls: list[ft.Control] = []
            if self._show_numbers:
                controls.append(ft.Text(
                    str(number).rjust(width), font_family=font, size=size,
                    color=ft.Colors.with_opacity(0.4, ft.Colors.ON_SURFACE),
                    no_wrap=True))
            controls.append(ft.Text(
                body or " ", font_family=font, size=size, expand=True,
                selectable=True, no_wrap=not self._wrap,
                overflow=None if self._wrap else ft.TextOverflow.CLIP,
                bgcolor=(ft.Colors.with_opacity(0.22, ft.Colors.AMBER_400)
                         if needle else None)))
            rows.append(ft.Row(controls, spacing=10, tight=True,
                               vertical_alignment=ft.CrossAxisAlignment.START))

        remaining = total - len(shown)
        if remaining > 0:
            rows.append(ft.Container(
                padding=ft.Padding.symmetric(vertical=8),
                content=ft.OutlinedButton(
                    f"Show {min(remaining, CHUNK_LINES):,} more "
                    f"({remaining:,} left)",
                    icon=ft.Icons.EXPAND_MORE, on_click=self._show_more),
            ))

        self._list.controls = rows
        if needle:
            self._match_label.value = f"{total:,} matching line(s)"
        else:
            self._match_label.value = ("" if remaining <= 0
                                       else f"showing {len(shown):,} of {total:,}")
        _safe_update(self._list)
        _safe_update(self._match_label)

    def _show_more(self, _event: ft.ControlEvent) -> None:
        self._visible += CHUNK_LINES
        self._render_lines()

    def _on_search(self, event: ft.ControlEvent) -> None:
        self._search = (event.control.value or "").strip()
        self._visible = CHUNK_LINES
        self._render_lines()

    def _toggle_wrap(self, _event: ft.ControlEvent) -> None:
        self._wrap = not self._wrap
        self._wrap_button.icon_color = ft.Colors.PRIMARY if self._wrap else None
        _safe_update(self._wrap_button)
        self._render_lines()

    def _copy(self, _event: ft.ControlEvent) -> None:
        self.ctx.copy("\n".join(self._lines),
                      notice=f"Copied {len(self._lines):,} lines")


@ft.control
class DataViewer(BaseViewer):
    """CSV / TSV / JSON preview rendered as a sortable table."""

    display_name: str = state("Data")

    def decode(self, path: Path) -> Table:
        text = path.read_text(encoding="utf-8", errors="replace")
        suffix = path.suffix.lower()
        if suffix == ".json":
            return _table_from_json(path.name, text)
        delimiter = "\t" if suffix == ".tsv" else ","
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows = list(reader)
        if not rows:
            raise ValueError("The file has no rows")
        header, *body = rows
        return Table(title=path.name, columns=header, rows=body)

    def present(self, spec: Table) -> ft.Control:
        table = common.SortableTable(
            table=spec,
            settings=self.ctx.settings,
            on_copy=lambda text: self.ctx.copy(text, notice="Table copied as CSV"),
            row_cap=2000,
        )
        return ft.Column(expand=True, spacing=8, controls=[table])


def _table_from_json(name: str, text: str) -> Table:
    """Flatten a JSON array of objects into a table; anything else is an error."""
    data = json.loads(text)
    if isinstance(data, dict):
        return Table(title=name, columns=["Key", "Value"],
                     rows=[[key, json.dumps(value)[:400]]
                           for key, value in data.items()])
    if isinstance(data, list) and data and isinstance(data[0], dict):
        columns: list[str] = []
        for entry in data[:200]:
            for key in entry:
                if key not in columns:
                    columns.append(str(key))
        rows = [[json.dumps(entry.get(column))[:400].strip('"')
                 for column in columns] for entry in data]
        return Table(title=name, columns=columns, rows=rows)
    if isinstance(data, list):
        return Table(title=name, columns=["#", "Value"],
                     rows=[[index, json.dumps(value)[:400]]
                           for index, value in enumerate(data)])
    raise ValueError("JSON root must be an object or an array")


def _safe_update(control: object) -> None:
    try:
        control.update()  # type: ignore[attr-defined]
    except Exception:
        pass
