"""
The shared run-history pane.

Every tool page gets one. Runs are listed newest-first with their outcome,
duration and a one-line summary; selecting one shows its parameters, metrics,
artefacts and stored log, and offers the three things you actually want from
history: restore the parameters into the form, run it again, or open what it
produced.
"""

from __future__ import annotations

from typing import Callable, Optional

import flet as ft

from s2kit.core.history import HistoryStore, RunRecord
from s2kit.core.params import Param
from s2kit.ui import common, theme
from s2kit.ui.context import AppContext
from s2kit.ui.controls import is_ready, state


@ft.control
class HistoryView(ft.Container):
    """Master/detail history browser for one tool."""

    ctx: Optional[AppContext] = state()
    tool_id: str = state("")
    schema: Optional[list] = state()
    on_restore: Optional[Callable[[RunRecord], None]] = state()
    on_rerun: Optional[Callable[[RunRecord], None]] = state()
    on_show_log: Optional[Callable[[RunRecord], None]] = state()

    def init(self):
        self._ready = False
        self._selected: Optional[RunRecord] = None
        self.expand = True

    def build(self):
        self._list = ft.ListView(expand=True, spacing=4,
                                 padding=ft.Padding.all(4))
        self._detail = ft.Column(expand=True, spacing=10,
                                 scroll=ft.ScrollMode.AUTO)
        self._count = common.caption("")

        left = ft.Container(
            width=320,
            expand=False,
            content=ft.Column(
                expand=True,
                spacing=6,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[
                    ft.Row(
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            common.heading("Runs", size=13),
                            ft.Container(expand=True),
                            self._count,
                            ft.IconButton(ft.Icons.REFRESH, icon_size=15,
                                          tooltip="Reload",
                                          on_click=lambda _e: self.refresh()),
                            ft.IconButton(ft.Icons.DELETE_SWEEP, icon_size=15,
                                          tooltip="Clear this tool's history",
                                          on_click=self._clear_all),
                        ],
                    ),
                    ft.Container(
                        expand=True,
                        content=self._list,
                        bgcolor=ft.Colors.with_opacity(
                            0.35, ft.Colors.SURFACE_CONTAINER_HIGHEST),
                        border_radius=ft.BorderRadius.all(8),
                        border=ft.Border.all(
                            1, ft.Colors.with_opacity(0.12, ft.Colors.OUTLINE)),
                    ),
                ],
            ),
        )

        self.content = ft.Row(
            expand=True,
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.START,
            controls=[left, ft.Container(expand=True, content=self._detail)],
        )
        self._ready = True
        self.refresh()

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Reload the record list from disk and repaint."""
        if not is_ready(self):
            return
        records = self._records()
        self._count.value = f"{len(records)}"
        self._list.controls = [self._record_tile(record) for record in records]
        if not records:
            self._list.controls = [
                ft.Container(
                    padding=16,
                    content=common.caption("No runs recorded yet. Finish a run "
                                           "and it lands here."),
                )
            ]
            self._show_placeholder()
        else:
            keep = self._selected and any(r.run_id == self._selected.run_id
                                          for r in records)
            self.select(self._selected if keep else records[0])
        _safe_update(self._list)
        _safe_update(self._count)

    def _records(self) -> list[RunRecord]:
        store: HistoryStore = self.ctx.history
        return store.list(self.tool_id)

    def select(self, record: Optional[RunRecord]) -> None:
        self._selected = record
        if record is None:
            self._show_placeholder()
            return
        self._detail.controls = self._detail_controls(record)
        _safe_update(self._detail)
        # Repaint the list so the selected tile highlights.
        for index, control in enumerate(self._list.controls):
            if isinstance(control, ft.Container) and control.data:
                selected = control.data == record.run_id
                control.bgcolor = (ft.Colors.with_opacity(0.16, ft.Colors.PRIMARY)
                                   if selected else None)
                _safe_update(control)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _record_tile(self, record: RunRecord) -> ft.Control:
        colour = theme.status_colour(record.status)
        return ft.Container(
            data=record.run_id,
            padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            border_radius=ft.BorderRadius.all(8),
            bgcolor=(ft.Colors.with_opacity(0.16, ft.Colors.PRIMARY)
                     if self._selected and self._selected.run_id == record.run_id
                     else None),
            on_click=lambda _e, r=record: self.select(r),
            content=ft.Column(
                spacing=2,
                tight=True,
                controls=[
                    ft.Row(
                        spacing=6,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Icon(_status_icon(record.status), size=13,
                                    color=colour),
                            ft.Text(record.started_text(), size=12,
                                    weight=ft.FontWeight.W_600, expand=True,
                                    no_wrap=True),
                            common.caption(record.duration_text()),
                        ],
                    ),
                    ft.Text(record.summary or record.error or "-", size=11,
                            color=ft.Colors.ON_SURFACE_VARIANT, max_lines=2,
                            overflow=ft.TextOverflow.ELLIPSIS),
                ],
            ),
        )

    def _show_placeholder(self) -> None:
        self._detail.controls = [
            common.empty_state("No run selected",
                               "Pick a run on the left to see what it did.",
                               icon_name="HISTORY")
        ]
        _safe_update(self._detail)

    def _detail_controls(self, record: RunRecord) -> list[ft.Control]:
        settings = self.ctx.settings
        colour = theme.status_colour(record.status)

        actions = ft.Row(
            spacing=8,
            wrap=True,
            controls=[
                ft.FilledTonalButton(
                    "Restore parameters", icon=ft.Icons.SETTINGS_BACKUP_RESTORE,
                    on_click=lambda _e: self._restore(record)),
                ft.FilledTonalButton(
                    "Run again", icon=ft.Icons.REPLAY,
                    on_click=lambda _e: self._rerun(record)),
                ft.OutlinedButton(
                    "Load log", icon=ft.Icons.TERMINAL,
                    on_click=lambda _e: self._load_log(record),
                    disabled=not record.log),
                ft.OutlinedButton(
                    "Copy report", icon=ft.Icons.CONTENT_COPY,
                    on_click=lambda _e: self.ctx.copy(record.as_text(),
                                                      notice="Run report copied")),
                ft.IconButton(ft.Icons.DELETE_OUTLINE, icon_size=17,
                              tooltip="Delete this record",
                              on_click=lambda _e: self._delete(record)),
            ],
        )

        header_rows = [
            ("Started", record.started_text()),
            ("Duration", record.duration_text()),
            ("Status", record.status),
            ("Run id", record.run_id),
        ]
        if record.error:
            header_rows.append(("Error", record.error))

        blocks: list[ft.Control] = [
            ft.Row(
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(_status_icon(record.status), color=colour, size=20),
                    ft.Text(record.summary or record.status.title(), size=15,
                            weight=ft.FontWeight.W_600, expand=True,
                            no_wrap=False),
                    common.pill(record.status, colour=colour),
                ],
            ),
            actions,
            common.section("Run", common.info_table(header_rows, settings)),
        ]

        if record.counts:
            counts = ", ".join(f"{level.title()} {count}"
                               for level, count in record.counts.items() if count)
            blocks.append(common.section("Log levels",
                                         common.info_table([("Counts", counts)],
                                                           settings)))
        if record.metrics:
            blocks.append(common.section(
                "Metrics",
                common.info_table([(k, str(v)) for k, v in record.metrics.items()],
                                  settings)))

        blocks.append(common.section(
            "Parameters",
            common.info_table(self._param_rows(record), settings, value_mono=True),
        ))

        if record.artifacts:
            blocks.append(common.section(
                "Artifacts",
                ft.Column(
                    spacing=2,
                    tight=True,
                    controls=[self._artifact_row(path) for path in record.artifacts],
                ),
                subtitle=f"{len(record.artifacts)} file(s) produced by this run",
            ))
        return blocks

    def _param_rows(self, record: RunRecord) -> list[tuple[str, str]]:
        """Prefer the schema's labels; fall back to raw keys for old records."""
        schema: list[Param] = list(self.schema or [])
        labels = {param.key: param.label for param in schema}
        rows: list[tuple[str, str]] = []
        for key in sorted(record.params):
            value = record.params[key]
            rows.append((labels.get(key, key), _value_text(value)))
        return rows or [("-", "no parameters recorded")]

    def _artifact_row(self, path: str) -> ft.Control:
        from pathlib import Path

        target = Path(path)
        exists = target.exists()
        return ft.Row(
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Icon(ft.Icons.INSERT_DRIVE_FILE if exists
                        else ft.Icons.BROKEN_IMAGE,
                        size=13,
                        color=ft.Colors.PRIMARY if exists else ft.Colors.OUTLINE),
                ft.Text(common.truncate_middle(path, 78), size=11, expand=True,
                        selectable=True,
                        color=None if exists else ft.Colors.ON_SURFACE_VARIANT),
                ft.IconButton(ft.Icons.FOLDER_OPEN, icon_size=14,
                              tooltip="Reveal in file manager", disabled=not exists,
                              on_click=lambda _e, p=target: self.ctx.reveal(p)),
                ft.IconButton(ft.Icons.OPEN_IN_NEW, icon_size=14,
                              tooltip="Open externally", disabled=not exists,
                              on_click=lambda _e, p=target: self.ctx.open_external(p)),
            ],
        )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _restore(self, record: RunRecord) -> None:
        if self.on_restore is not None:
            self.on_restore(record)
            self.ctx.toast("Parameters restored from that run", level="SUCCESS")

    def _rerun(self, record: RunRecord) -> None:
        if self.on_rerun is not None:
            self.on_rerun(record)

    def _load_log(self, record: RunRecord) -> None:
        if self.on_show_log is not None:
            self.on_show_log(record)

    def _delete(self, record: RunRecord) -> None:
        self.ctx.history.remove(self.tool_id, record.run_id)
        if self._selected and self._selected.run_id == record.run_id:
            self._selected = None
        self.refresh()
        self.ctx.toast("Run record deleted")

    def _clear_all(self, _event: ft.ControlEvent) -> None:
        async def run() -> None:
            if not await self.ctx.confirm(
                    "Clear history?",
                    f"This deletes every recorded run for {self.tool_id}.",
                    confirm_label="Clear", danger=True):
                return
            self.ctx.history.clear(self.tool_id)
            self._selected = None
            self.refresh()
            self.ctx.toast("History cleared")

        self.ctx.page.run_task(run)


def _status_icon(status: str):
    return {
        "success": ft.Icons.CHECK_CIRCLE,
        "failed": ft.Icons.ERROR,
        "cancelled": ft.Icons.CANCEL,
        "running": ft.Icons.HOURGLASS_TOP,
    }.get(status.lower(), ft.Icons.HELP_OUTLINE)


def _value_text(value: object) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if value in (None, ""):
        return "-"
    text = str(value)
    return common.truncate_middle(text, 90)


def _safe_update(control: object) -> None:
    try:
        control.update()  # type: ignore[attr-defined]
    except Exception:
        pass
