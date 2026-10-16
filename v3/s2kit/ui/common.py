"""
Shared UI chrome: cards, info tables, stat tiles, empty states, sortable data
tables.

Everything here is generic - no tool knows about another tool's widgets - and
every piece honours the appearance preferences through
:mod:`s2kit.ui.theme`.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence

import flet as ft

from s2kit.core.jobs import Metric, Table
from s2kit.core.settings import Settings
from s2kit.ui import theme
from s2kit.ui.controls import state


# ----------------------------------------------------------------------
# Text helpers
# ----------------------------------------------------------------------

def truncate_middle(text: str, limit: int = 72) -> str:
    """Shorten a long path so both ends stay readable."""
    if len(text) <= limit:
        return text
    keep = (limit - 3) // 2
    return f"{text[:keep]}...{text[-keep:]}"


def duration_text(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def caption(text: str, *, colour: str = ft.Colors.ON_SURFACE_VARIANT,
            size: Optional[float] = None) -> ft.Text:
    """Small secondary text - help lines, hints, counters."""
    return ft.Text(text, size=size or 11, color=colour, no_wrap=False)


def heading(text: str, *, size: float = 15) -> ft.Text:
    return ft.Text(text, size=size, weight=ft.FontWeight.W_600)


def mono(text: str, settings: Settings, *, size: Optional[float] = None,
         colour: Optional[str] = None, selectable: bool = True) -> ft.Text:
    """Monospaced text in the user's chosen face."""
    return ft.Text(
        text,
        font_family=theme.mono_font(settings),
        size=size or theme.scaled(settings, 12),
        color=colour,
        selectable=selectable,
        no_wrap=False,
    )


def pill(text: str, *, colour: str = ft.Colors.PRIMARY,
         icon_name: str = "") -> ft.Container:
    """A small rounded label, used for badges and status chips."""
    content: list[ft.Control] = []
    if icon_name:
        content.append(ft.Icon(theme.icon(icon_name), size=12, color=colour))
    content.append(ft.Text(text, size=10, weight=ft.FontWeight.W_600, color=colour))
    return ft.Container(
        content=ft.Row(content, spacing=4, tight=True),
        padding=ft.Padding.symmetric(horizontal=8, vertical=2),
        border_radius=ft.BorderRadius.all(10),
        bgcolor=ft.Colors.with_opacity(0.14, colour),
    )


# ----------------------------------------------------------------------
# Containers
# ----------------------------------------------------------------------

def section(title: str, *controls: ft.Control, subtitle: str = "",
            icon_name: str = "", actions: Sequence[ft.Control] = (),
            padding: int = 14, spacing: int = 10,
            expand: bool | int = False) -> ft.Container:
    """A titled card wrapping a block of related controls."""
    header: list[ft.Control] = []
    if icon_name:
        header.append(ft.Icon(theme.icon(icon_name), size=16,
                              color=ft.Colors.PRIMARY))
    header.append(
        ft.Column(
            spacing=1,
            tight=True,
            expand=True,
            controls=[heading(title)] + ([caption(subtitle)] if subtitle else []),
        )
    )
    if actions:
        header.append(ft.Row(list(actions), spacing=4, tight=True))

    body = [ft.Row(header, spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER)]
    body.extend(controls)

    return ft.Container(
        content=ft.Column(body, spacing=spacing, tight=not expand,
                          expand=True if expand else None),
        padding=padding,
        border_radius=ft.BorderRadius.all(10),
        bgcolor=ft.Colors.with_opacity(0.35, ft.Colors.SURFACE_CONTAINER_HIGHEST),
        border=ft.Border.all(1, ft.Colors.with_opacity(0.12, ft.Colors.OUTLINE)),
        expand=expand or None,
    )


def collapsible(title: str, *controls: ft.Control, subtitle: str = "",
                icon_name: str = "", expanded: bool = False) -> ft.Control:
    """A card that folds away - used for advanced parameter groups."""
    return ft.Container(
        content=ft.ExpansionTile(
            title=heading(title),
            subtitle=caption(subtitle) if subtitle else None,
            leading=(ft.Icon(theme.icon(icon_name), size=16,
                             color=ft.Colors.PRIMARY) if icon_name else None),
            expanded=expanded,
            controls_padding=ft.Padding.only(left=12, right=12, bottom=12),
            tile_padding=ft.Padding.symmetric(horizontal=12, vertical=2),
            dense=True,
            controls=[ft.Column(list(controls), spacing=10, tight=True)],
            shape=ft.RoundedRectangleBorder(radius=10),
            collapsed_shape=ft.RoundedRectangleBorder(radius=10),
        ),
        border_radius=ft.BorderRadius.all(10),
        bgcolor=ft.Colors.with_opacity(0.35, ft.Colors.SURFACE_CONTAINER_HIGHEST),
        border=ft.Border.all(1, ft.Colors.with_opacity(0.12, ft.Colors.OUTLINE)),
    )


def empty_state(title: str, message: str = "", *, icon_name: str = "INBOX",
                action: Optional[ft.Control] = None) -> ft.Container:
    """The friendly placeholder every empty tab shows."""
    controls: list[ft.Control] = [
        ft.Icon(theme.icon(icon_name), size=42,
                color=ft.Colors.with_opacity(0.35, ft.Colors.ON_SURFACE)),
        ft.Text(title, size=14, weight=ft.FontWeight.W_600,
                text_align=ft.TextAlign.CENTER),
    ]
    if message:
        controls.append(ft.Text(message, size=12,
                                color=ft.Colors.ON_SURFACE_VARIANT,
                                text_align=ft.TextAlign.CENTER, no_wrap=False))
    if action is not None:
        controls.append(ft.Container(content=action, padding=ft.Padding.only(top=6)))
    return ft.Container(
        content=ft.Column(controls, spacing=8, tight=True,
                          horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                          alignment=ft.MainAxisAlignment.CENTER),
        alignment=ft.Alignment.CENTER,
        padding=32,
        expand=True,
    )


def scroll_column(*controls: ft.Control, spacing: int = 12,
                  padding: int = 16) -> ft.Column:
    """The standard scrolling page body: padded content in a scroll view."""
    inner = ft.Column(list(controls), spacing=spacing, tight=True)
    if not padding:
        inner.scroll = ft.ScrollMode.AUTO
        inner.expand = True
        inner.tight = False
        return inner
    return ft.Column(
        [ft.Container(content=inner, padding=padding)],
        spacing=0,
        scroll=ft.ScrollMode.AUTO,
        expand=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )


# ----------------------------------------------------------------------
# Info tables & stat tiles
# ----------------------------------------------------------------------

def info_table(rows: Iterable[tuple[str, str]], settings: Settings, *,
               label_width: int = 130, value_mono: bool = False) -> ft.Column:
    """Two-column label/value list, the standard metadata panel."""
    built: list[ft.Control] = []
    for label, value in rows:
        value_control = (mono(str(value), settings) if value_mono
                         else ft.Text(str(value), size=12, selectable=True,
                                      no_wrap=False, expand=True))
        built.append(
            ft.Row(
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.START,
                controls=[
                    ft.Container(width=label_width,
                                 content=ft.Text(label, size=12,
                                                 color=ft.Colors.ON_SURFACE_VARIANT)),
                    value_control,
                ],
            )
        )
    return ft.Column(built, spacing=4, tight=True)


def stat_tile(metric: Metric, *, width: int = 150) -> ft.Container:
    """A single headline number."""
    colour = theme.tone_colour(metric.tone)
    value = f"{metric.value}{(' ' + metric.unit) if metric.unit else ''}"
    return ft.Container(
        width=width,
        padding=ft.Padding.symmetric(horizontal=12, vertical=10),
        border_radius=ft.BorderRadius.all(10),
        bgcolor=ft.Colors.with_opacity(0.10, colour),
        border=ft.Border.all(1, ft.Colors.with_opacity(0.22, colour)),
        content=ft.Column(
            spacing=2,
            tight=True,
            controls=[
                ft.Text(metric.label.upper(), size=9, weight=ft.FontWeight.W_700,
                        color=ft.Colors.ON_SURFACE_VARIANT),
                ft.Text(value, size=18, weight=ft.FontWeight.W_700, color=colour,
                        no_wrap=True, overflow=ft.TextOverflow.ELLIPSIS),
            ],
        ),
    )


def metrics_row(metrics: Sequence[Metric]) -> ft.Control:
    """A wrapping row of stat tiles."""
    return ft.Row([stat_tile(metric) for metric in metrics], spacing=10,
                  wrap=True, run_spacing=10)


# ----------------------------------------------------------------------
# Sortable, filterable data table
# ----------------------------------------------------------------------

@ft.control
class SortableTable(ft.Container):
    """A DataTable with sorting, text filtering, row cap and CSV export.

    Job results arrive as :class:`~s2kit.core.jobs.Table`; the Output tab wraps
    each in one of these so every tool gets the same table behaviour.
    """

    table: Optional[Table] = state()
    settings: Optional[Settings] = state()
    on_open_path: Optional[Callable[[str], Any]] = state()
    on_copy: Optional[Callable[[str], Any]] = state()
    row_cap: int = state(500)
    show_toolbar: bool = state(True)

    def init(self):
        self._filter = ""
        self._sort_column = None
        self._sort_ascending = True
        self.expand = True

    def build(self):
        spec = self.table
        if spec is not None:
            self._sort_column = spec.sort_column
            self._sort_ascending = spec.sort_ascending

        self._search = ft.TextField(
            hint_text="Filter rows...",
            prefix_icon=ft.Icons.SEARCH,
            dense=True,
            height=36,
            width=260,
            text_size=12,
            on_change=self._on_filter,
        )
        self._count = caption("")
        self._data_table = ft.DataTable(
            columns=self._columns(),
            rows=[],
            column_spacing=22,
            heading_row_height=34,
            data_row_min_height=30,
            data_row_max_height=48,
            divider_thickness=0.4,
            show_bottom_border=True,
            heading_text_style=ft.TextStyle(size=11, weight=ft.FontWeight.W_700),
            data_text_style=ft.TextStyle(size=12),
        )

        toolbar = ft.Row(
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                self._search,
                self._count,
                ft.Container(expand=True),
                ft.IconButton(ft.Icons.CONTENT_COPY, tooltip="Copy as CSV",
                              icon_size=16, on_click=self._copy_csv),
            ],
        )

        self.content = ft.Column(
            expand=True,
            spacing=8,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                *([toolbar] if self.show_toolbar else []),
                ft.Container(
                    expand=True,
                    content=ft.Column(
                        expand=True,
                        scroll=ft.ScrollMode.AUTO,
                        controls=[ft.Row(scroll=ft.ScrollMode.AUTO, tight=True,
                                         controls=[self._data_table])],
                    ),
                ),
            ],
        )
        self._refresh_rows()

    # -- data ----------------------------------------------------------

    def set_table(self, spec: Table) -> None:
        """Replace the underlying data (a rerun, or a different result set)."""
        self.table = spec
        self._sort_column = spec.sort_column
        self._sort_ascending = spec.sort_ascending
        self._data_table.columns = self._columns()
        self._refresh_rows()

    def _columns(self) -> list[ft.DataColumn]:
        spec = self.table
        if spec is None:
            return [ft.DataColumn(ft.Text("-"))]
        return [
            ft.DataColumn(
                ft.Text(str(name)),
                numeric=index in tuple(spec.numeric_columns),
                on_sort=self._on_sort,
            )
            for index, name in enumerate(spec.columns)
        ]

    def _visible_rows(self) -> list[Sequence[Any]]:
        spec = self.table
        if spec is None:
            return []
        rows = list(spec.rows)
        if self._filter:
            needle = self._filter.lower()
            rows = [row for row in rows
                    if any(needle in str(cell).lower() for cell in row)]
        if self._sort_column is not None:
            index = self._sort_column
            rows.sort(key=lambda row: _sort_key(row, index),
                      reverse=not self._sort_ascending)
        return rows

    def _refresh_rows(self) -> None:
        spec = self.table
        rows = self._visible_rows()
        total = len(spec.rows) if spec else 0
        shown = rows[: self.row_cap] if self.row_cap else rows

        self._data_table.rows = [self._build_row(row) for row in shown]
        self._data_table.sort_column_index = self._sort_column
        self._data_table.sort_ascending = self._sort_ascending

        note = f"{len(rows):,} of {total:,} rows"
        if len(shown) < len(rows):
            note += f"  -  showing the first {len(shown):,}"
        self._count.value = note
        _safe_update(self)

    def _build_row(self, row: Sequence[Any]) -> ft.DataRow:
        spec = self.table
        path_column = spec.path_column if spec else None
        cells: list[ft.DataCell] = []
        for index, value in enumerate(row):
            text = "" if value is None else str(value)
            if index == path_column and text and self.on_open_path is not None:
                cells.append(ft.DataCell(
                    ft.Row(spacing=4, tight=True, controls=[
                        ft.Icon(ft.Icons.OPEN_IN_NEW, size=12,
                                color=ft.Colors.PRIMARY),
                        ft.Text(truncate_middle(text, 64), size=12,
                                color=ft.Colors.PRIMARY),
                    ]),
                    on_tap=lambda _e, p=text: self.on_open_path(p),
                ))
            else:
                cells.append(ft.DataCell(ft.Text(text, size=12, selectable=True,
                                                 no_wrap=True,
                                                 overflow=ft.TextOverflow.ELLIPSIS)))
        # DataTable requires every row to match the column count exactly.
        expected = len(spec.columns) if spec else len(cells)
        while len(cells) < expected:
            cells.append(ft.DataCell(ft.Text("")))
        return ft.DataRow(cells=cells[:expected])

    # -- events --------------------------------------------------------

    def _on_filter(self, event: ft.ControlEvent) -> None:
        self._filter = (event.control.value or "").strip()
        self._refresh_rows()

    def _on_sort(self, event: ft.DataColumnSortEvent) -> None:
        if self._sort_column == event.column_index:
            self._sort_ascending = not self._sort_ascending
        else:
            self._sort_column = event.column_index
            self._sort_ascending = True
        self._refresh_rows()

    def _copy_csv(self, _event: ft.ControlEvent) -> None:
        if self.on_copy is not None:
            self.on_copy(self.to_csv())

    def to_csv(self) -> str:
        """The currently visible rows as CSV text."""
        spec = self.table
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        if spec is not None:
            writer.writerow(spec.columns)
            writer.writerows(self._visible_rows())
        return buffer.getvalue()


def _sort_key(row: Sequence[Any], index: int) -> tuple[int, float, str]:
    """Sort numbers numerically and text alphabetically, numbers first."""
    value = row[index] if index < len(row) else ""
    if isinstance(value, (int, float)):
        return (0, float(value), "")
    text = str(value)
    try:
        return (0, float(text.replace(",", "")), "")
    except ValueError:
        return (1, 0.0, text.lower())


def _safe_update(control: Any) -> None:
    try:
        control.update()
    except Exception:
        pass


# ----------------------------------------------------------------------
# Progress + status strip
# ----------------------------------------------------------------------

@dataclass
class ProgressState:
    """What the progress strip is currently showing."""

    active: bool = False
    done: int = 0
    total: int = 0
    status: str = ""
    started: float = 0.0
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def fraction(self) -> Optional[float]:
        if not self.active:
            return 0.0
        if self.total <= 0:
            return None       # indeterminate
        return max(0.0, min(1.0, self.done / self.total))
