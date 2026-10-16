"""
The shared Output tab.

Whatever a job reports through :class:`~s2kit.core.jobs.JobContext` shows up
here in a fixed order: the outcome banner, stat tiles, result tables, then the
files it wrote. Tools do not build any of it - they call `ctx.metric`,
`ctx.table` and `ctx.artifact`.
"""

from __future__ import annotations

from typing import Callable, Optional

import flet as ft

from s2kit.core.jobs import Artifact, Metric, Table
from s2kit.ui import common, theme
from s2kit.ui.context import AppContext
from s2kit.ui.controls import is_ready, state


@ft.control
class OutputView(ft.Container):
    """Metrics, tables and artefacts for the current (or last) run."""

    ctx: Optional[AppContext] = state()
    on_preview: Optional[Callable[[Artifact], None]] = state()

    def init(self):
        self._ready = False
        self._banner_text = ""
        self._banner_status = ""
        self._metrics: list[Metric] = []
        self._tables: list[Table] = []
        self._artifacts: list[Artifact] = []
        self.expand = True

    def build(self):
        self._banner = ft.Container(visible=False)
        self._metrics_row = ft.Row(spacing=10, wrap=True, run_spacing=10,
                                   visible=False)
        self._tables_column = ft.Column(spacing=12, tight=True)
        self._artifacts_column = ft.Column(spacing=12, tight=True)
        self._empty = common.empty_state(
            "No results yet",
            "Run the tool and its metrics, tables and output files appear here.",
            icon_name="INSIGHTS")

        self._body = ft.Column(
            expand=True,
            scroll=ft.ScrollMode.AUTO,
            spacing=12,
            controls=[
                ft.Container(
                    padding=16,
                    content=ft.Column(
                        spacing=12,
                        tight=True,
                        controls=[
                            self._banner,
                            self._metrics_row,
                            self._tables_column,
                            self._artifacts_column,
                        ],
                    ),
                )
            ],
        )
        self.content = ft.Column(
            expand=True,
            spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[self._empty, self._body],
        )
        self._ready = True
        self._rebuild_all()

    # ------------------------------------------------------------------
    # Feeding
    # ------------------------------------------------------------------

    def clear(self) -> None:
        self._metrics = []
        self._tables = []
        self._artifacts = []
        self._banner_text = ""
        self._banner_status = ""
        if not is_ready(self):
            return
        self._metrics_row.controls = []
        self._metrics_row.visible = False
        self._tables_column.controls = []
        self._artifacts_column.controls = []
        self._banner.visible = False
        self._sync_empty()
        _safe_update(self)

    def _rebuild_all(self) -> None:
        """Render everything from stored state - used by `build()`."""
        self._metrics_row.controls = [common.stat_tile(metric)
                                      for metric in self._metrics]
        self._metrics_row.visible = bool(self._metrics)
        self._tables_column.controls = [self._table_card(spec)
                                        for spec in self._tables]
        self._rebuild_artifacts()
        if self._banner_text:
            self.set_banner(self._banner_text, self._banner_status)
        self._sync_empty()
        _safe_update(self)

    def set_banner(self, message: str, status: str) -> None:
        """The outcome strip at the top of the tab."""
        self._banner_text = message
        self._banner_status = status
        if not is_ready(self):
            return
        colour = theme.status_colour(status)
        self._banner.content = ft.Row(
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Icon(_status_icon(status), color=colour, size=18),
                ft.Text(message, size=13, weight=ft.FontWeight.W_600,
                        no_wrap=False, expand=True),
                common.pill(status, colour=colour),
            ],
        )
        self._banner.padding = 12
        self._banner.border_radius = ft.BorderRadius.all(10)
        self._banner.bgcolor = ft.Colors.with_opacity(0.10, colour)
        self._banner.border = ft.Border.all(1, ft.Colors.with_opacity(0.3, colour))
        self._banner.visible = True
        self._sync_empty()
        _safe_update(self._banner)

    def add_metric(self, metric: Metric) -> None:
        # A metric reported twice replaces the first - jobs update counters as
        # they go rather than emitting a new tile per item.
        for index, existing in enumerate(self._metrics):
            if existing.label == metric.label:
                self._metrics[index] = metric
                if is_ready(self):
                    self._metrics_row.controls[index] = common.stat_tile(metric)
                    _safe_update(self._metrics_row)
                return
        self._metrics.append(metric)
        if not is_ready(self):
            return
        self._metrics_row.controls.append(common.stat_tile(metric))
        self._metrics_row.visible = True
        self._sync_empty()
        _safe_update(self._metrics_row)

    def add_table(self, spec: Table) -> None:
        self._tables.append(spec)
        if not is_ready(self):
            return
        self._tables_column.controls.append(self._table_card(spec))
        self._sync_empty()
        _safe_update(self._tables_column)

    def _table_card(self, spec: Table) -> ft.Control:
        table = common.SortableTable(
            table=spec,
            settings=self.ctx.settings,
            on_copy=lambda text: self.ctx.copy(text, notice="Table copied as CSV"),
            on_open_path=self._open_path,
        )
        return common.section(
            spec.title,
            ft.Container(height=min(460, 120 + 30 * len(spec.rows)), content=table),
            subtitle=f"{len(spec.rows):,} rows",
            icon_name="TABLE_CHART",
        )

    def add_artifact(self, artifact: Artifact) -> None:
        self._artifacts.append(artifact)
        self._rebuild_artifacts()

    def metrics(self) -> dict[str, object]:
        """Metric values for the history record."""
        return {metric.label: f"{metric.value} {metric.unit}".strip()
                for metric in self._metrics}

    def artifacts(self) -> list[Artifact]:
        return list(self._artifacts)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _rebuild_artifacts(self) -> None:
        if not is_ready(self):
            return
        rows = [self._artifact_row(artifact) for artifact in self._artifacts]
        self._artifacts_column.controls = [
            common.section(
                "Output files",
                ft.Column(rows, spacing=2, tight=True),
                subtitle=f"{len(self._artifacts)} file(s)",
                icon_name="FOLDER_ZIP",
                actions=[
                    ft.IconButton(ft.Icons.FOLDER_OPEN, icon_size=16,
                                  tooltip="Open the output folder",
                                  on_click=self._open_folder),
                    ft.IconButton(ft.Icons.CONTENT_COPY, icon_size=16,
                                  tooltip="Copy every path",
                                  on_click=self._copy_paths),
                ],
            )
        ] if self._artifacts else []
        self._sync_empty()
        _safe_update(self._artifacts_column)

    def _artifact_row(self, artifact: Artifact) -> ft.Control:
        from s2kit.media.probe import kind_for
        from s2kit.ui import viewers

        exists = artifact.path.exists()
        kind = kind_for(artifact.path)
        return ft.Row(
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            controls=[
                ft.Icon(theme.icon(viewers.kind_icon(kind)), size=14,
                        color=ft.Colors.PRIMARY if exists else ft.Colors.OUTLINE),
                ft.Column(
                    spacing=0,
                    tight=True,
                    expand=True,
                    controls=[
                        ft.Text(artifact.display_name(), size=12, no_wrap=True,
                                overflow=ft.TextOverflow.ELLIPSIS),
                        common.caption(
                            common.truncate_middle(str(artifact.path), 84), size=10),
                    ],
                ),
                ft.IconButton(ft.Icons.VISIBILITY, icon_size=15,
                              tooltip="Preview", disabled=not exists,
                              on_click=lambda _e, a=artifact: self._preview(a)),
                ft.IconButton(ft.Icons.FOLDER_OPEN, icon_size=15,
                              tooltip="Reveal", disabled=not exists,
                              on_click=lambda _e, a=artifact: self.ctx.reveal(a.path)),
                ft.IconButton(ft.Icons.OPEN_IN_NEW, icon_size=15,
                              tooltip="Open externally", disabled=not exists,
                              on_click=lambda _e, a=artifact:
                                  self.ctx.open_external(a.path)),
            ],
        )

    def _preview(self, artifact: Artifact) -> None:
        if self.on_preview is not None:
            self.on_preview(artifact)

    def _open_path(self, path: str) -> None:
        if self.on_preview is not None:
            from pathlib import Path

            self.on_preview(Artifact(path=Path(path)))

    def _open_folder(self, _event: ft.ControlEvent) -> None:
        if self._artifacts:
            self.ctx.reveal(self._artifacts[0].path.parent)

    def _copy_paths(self, _event: ft.ControlEvent) -> None:
        text = "\n".join(str(artifact.path) for artifact in self._artifacts)
        self.ctx.copy(text, notice=f"Copied {len(self._artifacts)} paths")

    def _sync_empty(self) -> None:
        has_content = bool(self._metrics or self._tables or self._artifacts
                           or self._banner.visible)
        self._empty.visible = not has_content
        self._body.visible = has_content
        _safe_update(self._empty)
        _safe_update(self._body)


def _status_icon(status: str):
    return {
        "success": ft.Icons.CHECK_CIRCLE,
        "failed": ft.Icons.ERROR,
        "cancelled": ft.Icons.CANCEL,
        "running": ft.Icons.HOURGLASS_TOP,
    }.get(status.lower(), ft.Icons.INFO)


def _safe_update(control: object) -> None:
    try:
        control.update()  # type: ignore[attr-defined]
    except Exception:
        pass
