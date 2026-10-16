"""
The base tool page.

Every tool in the kit is a subclass of :class:`ToolPage` and gets the same
chrome, for free:

* **Run**      - the parameter form built from `PARAMS`, with validation,
                 presets, last-used values and reset.
* **Output**   - metrics, result tables and output files reported by the job.
* **Preview**  - texture / model / sound / text viewers for anything it wrote.
* **Log**      - levelled, filterable, searchable, savable.
* **History**  - every past run, restorable and repeatable.
* **Settings** - the tool's own preferences (when it declares `PREFERENCES`).
* **Help**     - the tool's markdown notes (when it declares `HELP`).

A tool therefore consists of a schema and one function::

    @tool(id="my-tool", title="My Tool", category="Utility", icon="BUILD")
    class MyTool(ToolPage):
        PARAMS = [PathParam("input", "Input folder", mode="directory")]

        def run_job(self, ctx: JobContext) -> None:
            ctx.info(f"Working on {ctx.params['input']}")

`run_job` runs on a worker thread and must only talk to its
:class:`~s2kit.core.jobs.JobContext` - never to Flet controls. Everything the
UI shows is derived from the events that context emits.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional, Sequence

import flet as ft

from s2kit.core import params as P
from s2kit.core import preferences
from s2kit.core.history import RunRecord, record_from_handle
from s2kit.core.jobs import (
    Artifact, JobContext, JobEvent, JobHandle, LogLine, drain,
)
from s2kit.core.registry import ToolSpec
from s2kit.ui import common, theme
from s2kit.ui.context import AppContext
from s2kit.ui.controls import state
from s2kit.ui.fields import ParamForm
from s2kit.ui.history_view import HistoryView
from s2kit.ui.log_view import LogView
from s2kit.ui.output_view import OutputView
from s2kit.ui.preview import PreviewPane

logger = logging.getLogger(__name__)

#: How often the UI drains the job event queue. Fast enough to feel live,
#: slow enough that a job logging thousands of lines a second batches nicely.
PUMP_INTERVAL = 0.09


@ft.control
class ToolPage(ft.Container):
    """Shared chrome and job plumbing for every tool."""

    ctx: Optional[AppContext] = state()

    # Subclass declarations. These are deliberately *unannotated*: `@ft.control`
    # applies `@dataclass`, and an annotated class attribute would become an
    # init field whose inherited default would shadow whatever a subclass
    # declares. Plain class attributes are left alone and override normally.
    SPEC = None                 # ToolSpec, set by the @tool decorator
    PARAMS = ()                 # the Run tab form schema
    PREFERENCES = ()            # the Settings tab / global preference block
    HELP = ""                   # markdown shown on the Help tab
    RUN_LABEL = "Run"
    RUN_ICON = "PLAY_ARROW"
    #: Set False for read-only tools (inspectors) so no confirmation is asked.
    WRITES_FILES = True

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """Whether a run is still in flight from the page's point of view.

        The worker thread finishing is not the end of a run: the event pump
        still has to drain the last batch and `_finish` has to apply the
        summary, metrics and history. `_handle` is cleared only after that, so
        this stays True until the page is genuinely idle again - which is what
        the Run button, the shell and the self-test need to know.
        """
        return self._handle is not None

    @property
    def last_handle(self) -> Optional[JobHandle]:
        """The most recent job handle, running or finished."""
        return self._last_handle

    @property
    def spec(self) -> Optional[ToolSpec]:
        """The registry entry this page was registered with."""
        return self.SPEC

    @property
    def tool_id(self) -> str:
        return self.SPEC.id if self.SPEC else self.__class__.__name__.lower()

    @property
    def title(self) -> str:
        return self.SPEC.title if self.SPEC else self.__class__.__name__

    @property
    def description(self) -> str:
        return self.SPEC.description if self.SPEC else ""

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def init(self):
        self.expand = True
        self._handle: Optional[JobHandle] = None
        self._last_handle: Optional[JobHandle] = None
        self._pump_task: Optional[asyncio.Future] = None
        self._log_lines: list[LogLine] = []
        self._level_counts: dict[str, int] = {}
        self._summary = ""
        self._pending_progress: Optional[tuple[int, int]] = None

    def build(self):
        ctx = self.ctx
        settings = ctx.settings

        # -- parameter form ------------------------------------------------
        self.form = ParamForm(ctx, list(self.PARAMS), owner_id=self.tool_id,
                              on_change=self._sync_run_mode,
                              on_values_changed=self._sync_run_mode)
        if bool(settings.get("restore_last_params", True)):
            stored = settings.tool_params(self.tool_id)
            if stored:
                self.form.set_values(stored)

        # -- panes ---------------------------------------------------------
        self.log_view = LogView(ctx=ctx, tool_id=self.tool_id)
        self.preview = PreviewPane(ctx=ctx, tool_id=self.tool_id)
        self.output = OutputView(ctx=ctx, on_preview=self._preview_artifact)
        self.history = HistoryView(
            ctx=ctx, tool_id=self.tool_id, schema=list(self.PARAMS),
            on_restore=self._restore_record,
            on_rerun=self._rerun_record,
            on_show_log=self._load_record_log,
        )

        # -- header --------------------------------------------------------
        self.run_button = ft.FilledButton(
            self.RUN_LABEL, icon=theme.icon(self.RUN_ICON),
            on_click=lambda _e: self.start_run())
        self.cancel_button = ft.OutlinedButton(
            "Cancel", icon=ft.Icons.STOP_CIRCLE, disabled=True,
            on_click=lambda _e: self.cancel_run())
        self.progress_bar = ft.ProgressBar(value=0, bar_height=4, visible=False)
        self.status_text = common.caption("Ready")
        self.elapsed_text = common.caption("")
        self.counts_row = ft.Row(spacing=6, tight=True)
        self.mode_text = common.caption("")
        self.result_button = ft.TextButton("View results", visible=False,
                                          on_click=lambda _e: self.select_tab("Output"))
        self._sync_run_mode()

        header = ft.Container(
            padding=ft.Padding.only(left=16, right=16, top=12, bottom=8),
            content=ft.Column(
                spacing=8,
                tight=True,
                controls=[
                    ft.Row(
                        spacing=12,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Container(
                                width=38, height=38,
                                border_radius=ft.BorderRadius.all(10),
                                bgcolor=ft.Colors.with_opacity(0.14,
                                                               ft.Colors.PRIMARY),
                                alignment=ft.Alignment.CENTER,
                                content=ft.Icon(
                                    theme.icon(self.SPEC.icon if self.SPEC
                                               else "BUILD"),
                                    size=20, color=ft.Colors.PRIMARY),
                            ),
                            ft.Column(
                                spacing=1,
                                tight=True,
                                expand=True,
                                controls=[
                                    ft.Row(
                                        spacing=8,
                                        tight=True,
                                        vertical_alignment=(
                                            ft.CrossAxisAlignment.CENTER),
                                        controls=[
                                            ft.Text(self.title, size=17,
                                                    weight=ft.FontWeight.W_700),
                                            *([common.pill(
                                                self.SPEC.badge,
                                                colour=ft.Colors.AMBER_400)]
                                              if self.SPEC and self.SPEC.badge
                                              else []),
                                        ],
                                    ),
                                    common.caption(self.description),
                                ],
                            ),
                        ],
                    ),
                    ft.Row([self.mode_text, self.counts_row, self.result_button,
                            self.cancel_button, self.run_button, self._overflow_menu()],
                           wrap=True, spacing=8, run_spacing=4),
                    ft.Row(
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Container(expand=True, content=self.progress_bar),
                            self.elapsed_text,
                            self.status_text,
                        ],
                    ),
                ],
            ),
        )

        # -- tabs ----------------------------------------------------------
        # Flet swallows exceptions raised inside build(), which would leave a
        # half-constructed page and a confusing AttributeError later. Each tab
        # body is therefore built defensively and a failure becomes a visible
        # panel with the traceback instead of a silently broken page.
        tabs: list[tuple[str, Any, ft.Control]] = [
            ("Run", ft.Icons.TUNE, self._safe_tab("Run", self._run_tab)),
            ("Output", ft.Icons.INSIGHTS, self.output),
            ("Preview", ft.Icons.VISIBILITY, self.preview),
            ("Log", ft.Icons.TERMINAL, ft.Container(padding=12,
                                                    content=self.log_view)),
            ("History", ft.Icons.HISTORY, ft.Container(padding=12,
                                                       content=self.history)),
        ]
        if self.PREFERENCES:
            tabs.append(("Settings", ft.Icons.SETTINGS,
                         self._safe_tab("Settings", self._settings_tab)))
        if self.HELP:
            tabs.append(("Help", ft.Icons.HELP_OUTLINE,
                         self._safe_tab("Help", self._help_tab)))

        self._tab_index = {name: index for index, (name, _i, _c)
                          in enumerate(tabs)}
        self.tabs = ft.Tabs(
            length=len(tabs),
            selected_index=0,
            expand=True,
            content=ft.Column(
                expand=True,
                spacing=0,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[
                    ft.TabBar(
                        tabs=[ft.Tab(label=name, icon=icon)
                              for name, icon, _control in tabs],
                        scrollable=True,
                        tab_alignment=ft.TabAlignment.START,
                    ),
                    ft.TabBarView(
                        expand=True,
                        controls=[control for _n, _i, control in tabs],
                    ),
                ],
            ),
        )

        self.content = ft.Column(
            expand=True,
            spacing=0,
            # STRETCH, or a wide tab body (a texture, a waveform) sizes the
            # column to its own intrinsic width and the page overflows.
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                header,
                ft.Divider(height=1, thickness=1),
                self.tabs,
            ],
        )

    # ------------------------------------------------------------------
    # Tab bodies
    # ------------------------------------------------------------------

    def _safe_tab(self, name: str, factory: Any) -> ft.Control:
        """Build a tab body, turning a failure into a readable error panel."""
        try:
            return factory()
        except Exception as exc:
            import traceback

            logger.exception("%s: the %s tab failed to build", self.tool_id, name)
            return common.scroll_column(
                common.section(
                    f"The {name} tab failed to build",
                    ft.Text(f"{type(exc).__name__}: {exc}", size=12,
                            color=ft.Colors.RED_400, selectable=True,
                            no_wrap=False),
                    common.mono(traceback.format_exc(), self.ctx.settings),
                    icon_name="BUG_REPORT",
                    subtitle="This is a bug in the tool, not in your input.",
                )
            )

    def _run_tab(self) -> ft.Control:
        blocks: list[ft.Control] = []
        intro = self.intro_controls()
        if intro:
            blocks.extend(intro)
        if self.PARAMS:
            blocks.extend(self.form.build())
        else:
            blocks.append(common.section(
                "No parameters",
                common.caption("This tool runs without any input."),
            ))
        extra = self.extra_controls()
        if extra:
            blocks.extend(extra)
        self.form.scroll_host = common.scroll_column(*blocks)
        return self.form.scroll_host

    def _sync_run_mode(self, key: str = "", value: Any = None) -> None:
        """Keep mutation mode explicit in the action and persistent header."""
        if not hasattr(self, "mode_text"):
            return
        if self.tool_id not in {"filename-sanitizer", "sound-optimizer", "search-replace"}:
            self.mode_text.visible = False
            return
        values = self.form.values()
        if values.get("action") == "undo":
            label, mode = "Undo last changes", "Undo mode"
        elif values.get("dry_run", True):
            label, mode = "Preview changes", "Preview only: no files changed"
        else:
            label, mode = "Apply changes", "Apply mode: changes files on disk"
        self.run_button.content = label
        self.mode_text.value = mode
        _safe_update(self.run_button)
        _safe_update(self.mode_text)

    def _settings_tab(self) -> ft.Control:
        """The tool's own preference block, stored per tool."""
        store = self.ctx.settings.tool_pref_store(self.tool_id)
        self.pref_form = ParamForm(
            self.ctx, list(self.PREFERENCES), owner_id=f"{self.tool_id}:prefs",
            store=store, on_change=lambda *_a: self.on_preferences_changed(),
        )
        reset = ft.OutlinedButton(
            "Reset this tool's settings", icon=ft.Icons.RESTART_ALT,
            on_click=lambda _e: self._reset_tool_prefs(store))
        return common.scroll_column(
            common.section(
                f"{self.title} settings",
                common.caption("These apply to this tool only and persist "
                               "between sessions. App-wide options live in "
                               "Preferences."),
                icon_name="SETTINGS",
            ),
            *self.pref_form.build(),
            ft.Row([reset], spacing=8),
        )

    def _help_tab(self) -> ft.Control:
        return common.scroll_column(
            ft.Markdown(self.HELP, selectable=True,
                        extension_set=ft.MarkdownExtensionSet.GITHUB_WEB,
                        on_tap_link=lambda e: self.ctx.page.run_task(
                            self.ctx.page.launch_url, e.data)),
        )

    def _overflow_menu(self) -> ft.Control:
        items = [
            ft.PopupMenuItem(content=ft.Text("Reset parameters"),
                             icon=ft.Icons.RESTART_ALT,
                             on_click=lambda _e: self._reset_params()),
            ft.PopupMenuItem(content=ft.Text("Copy parameters as JSON"),
                             icon=ft.Icons.DATA_OBJECT,
                             on_click=lambda _e: self._copy_params()),
            ft.PopupMenuItem(content=ft.Text("Save as preset..."),
                             icon=ft.Icons.BOOKMARK_ADD,
                             on_click=lambda _e: self.ctx.page.run_task(
                                 self._save_preset)),
            ft.PopupMenuItem(content=ft.Text("Load preset..."),
                             icon=ft.Icons.BOOKMARKS,
                             on_click=lambda _e: self.ctx.page.run_task(
                                 self._load_preset)),
            ft.PopupMenuItem(),
            ft.PopupMenuItem(content=ft.Text("Clear log"),
                             icon=ft.Icons.DELETE_SWEEP,
                             on_click=lambda _e: self.log_view.clear()),
            ft.PopupMenuItem(content=ft.Text("Clear output"),
                             icon=ft.Icons.LAYERS_CLEAR,
                             on_click=lambda _e: self.output.clear()),
        ]
        return ft.PopupMenuButton(items=items, tooltip="More actions",
                                  icon=ft.Icons.MORE_VERT)

    # ------------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------------

    def intro_controls(self) -> list[ft.Control]:
        """Controls placed above the parameter form (notes, warnings)."""
        return []

    def extra_controls(self) -> list[ft.Control]:
        """Controls placed below the parameter form (custom widgets)."""
        return []

    def run_job(self, ctx: JobContext) -> None:
        """The actual work. Runs on a worker thread; talks only to `ctx`."""
        raise NotImplementedError(
            f"{type(self).__name__} must implement run_job(ctx)")

    def validate(self, values: dict[str, Any]) -> dict[str, str]:
        """Extra validation beyond the schema. Return {param key: message}."""
        return {}

    def on_run_finished(self, handle: JobHandle) -> None:
        """Called on the UI thread after a run ends."""

    def on_preferences_changed(self) -> None:
        """Called when a global or tool preference changed while this is open."""
        self.log_view.apply_preferences()
        self.preview.refresh_preferences()

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    def start_run(self) -> None:
        """Validate, then start the job (and the UI event pump)."""
        if self._handle is not None and self._handle.running:
            self.ctx.toast("This tool is already running", level="WARNING")
            return

        values = self.form.values()
        errors = self.form.validate(self.validate(values))
        if errors:
            first = next(iter(errors.values()))
            self.ctx.toast(first, level="ERROR")
            self._select_tab("Run")
            self.ctx.page.run_task(self.form.reveal_error, next(iter(errors)))
            return

        if bool(self.ctx.settings.get("history_autosave_params", True)):
            self.ctx.settings.save_tool_params(self.tool_id, values)

        self._begin(values)

    def _begin(self, values: dict[str, Any]) -> None:
        self.log_view.clear()
        self.output.clear()
        self.preview.begin_run()
        self.result_button.visible = False
        _safe_update(self.result_button)
        self._log_lines = []
        self._level_counts = {}
        self._summary = ""
        self.counts_row.controls = []
        _safe_update(self.counts_row)

        prefs = dict(self.ctx.settings.as_dict())
        prefs.update(self.ctx.settings.tool_prefs(self.tool_id))

        handle = self.ctx.jobs.start(self.tool_id, values, prefs, self.run_job)
        self._handle = handle
        self._last_handle = handle

        self.run_button.disabled = True
        self.cancel_button.disabled = False
        self.progress_bar.visible = True
        self.progress_bar.value = None       # indeterminate until told otherwise
        self.status_text.value = "Running..."
        _safe_update(self.run_button)
        _safe_update(self.cancel_button)
        _safe_update(self.progress_bar)
        _safe_update(self.status_text)

        self.ctx.set_status(f"{self.title}: running")
        self._pump_task = self.ctx.page.run_task(self._pump, handle)

    def cancel_run(self) -> None:
        if self._handle is not None and self._handle.running:
            self._handle.cancel()
            self.status_text.value = "Cancelling..."
            self.cancel_button.disabled = True
            _safe_update(self.status_text)
            _safe_update(self.cancel_button)

    async def _pump(self, handle: JobHandle) -> None:
        """Drain job events in batches until the job is done and the queue dry."""
        finished = False
        while True:
            batch = drain(handle.events, limit=800)
            if batch:
                self._apply_events(batch)
                finished = finished or any(event.kind == "done" for event in batch)
            if handle.running:
                self.elapsed_text.value = common.duration_text(handle.duration)
                _safe_update(self.elapsed_text)
            elif finished and handle.events.empty():
                break
            await asyncio.sleep(PUMP_INTERVAL)
        self._finish(handle)

    def _apply_events(self, batch: Sequence[JobEvent]) -> None:
        """Apply one drained batch; each control updates at most once."""
        lines: list[LogLine] = []
        progress: Optional[tuple[int, int]] = None
        status: Optional[str] = None

        for event in batch:
            if event.kind == "log":
                lines.append(event.payload)
            elif event.kind == "progress":
                progress = event.payload
            elif event.kind == "status":
                status = event.payload
            elif event.kind == "metric":
                self.output.add_metric(event.payload)
            elif event.kind == "table":
                self.output.add_table(event.payload)
            elif event.kind == "artifact":
                self._on_artifact(event.payload)
            elif event.kind == "summary":
                self._summary = event.payload

        if lines:
            self._log_lines.extend(lines)
            for line in lines:
                self._level_counts[line.level] = (
                    self._level_counts.get(line.level, 0) + 1)
            self.log_view.append(lines)
            self._refresh_counts()
        if progress is not None:
            done, total = progress
            self.progress_bar.value = (None if total <= 0
                                       else max(0.0, min(1.0, done / total)))
            _safe_update(self.progress_bar)
        if status is not None:
            self.status_text.value = status
            _safe_update(self.status_text)

    def _on_artifact(self, artifact: Artifact) -> None:
        self.output.add_artifact(artifact)
        if bool(self.ctx.pref("auto_preview", True)) or self.preview.contains(artifact.path):
            self.preview.add(artifact, generated=True)

    def _refresh_counts(self) -> None:
        chips: list[ft.Control] = []
        for level in ("ERROR", "WARNING", "SUCCESS"):
            count = self._level_counts.get(level, 0)
            if count:
                chips.append(common.pill(f"{count} {level.lower()}",
                                         colour=theme.level_colour(level)))
        self.counts_row.controls = chips
        _safe_update(self.counts_row)

    def _finish(self, handle: JobHandle) -> None:
        """Wrap up a finished run: chrome, banner, history, notification."""
        handle.summary = self._summary or _default_summary(handle)

        self.run_button.disabled = False
        self.cancel_button.disabled = True
        self.progress_bar.visible = False
        self.progress_bar.value = 0
        self.status_text.value = f"{handle.status.title()} in {common.duration_text(handle.duration)}"
        self.elapsed_text.value = ""
        for control in (self.run_button, self.cancel_button, self.progress_bar,
                        self.status_text, self.elapsed_text):
            _safe_update(control)

        self.output.set_banner(handle.summary, handle.status)
        self.result_button.visible = True
        self.result_button.tooltip = handle.summary
        _safe_update(self.result_button)
        self.ctx.set_status(f"{self.title}: {handle.status}")

        self._record_history(handle)

        if bool(self.ctx.pref("notify_on_finish", True)):
            level = {"success": "SUCCESS", "failed": "ERROR"}.get(
                handle.status, "WARNING")
            self.ctx.toast(f"{self.title}: {handle.summary}", level=level)
        if handle.status == "failed" and bool(self.ctx.pref("beep_on_error", False)):
            _beep()
        if bool(self.ctx.pref("log_to_file", True)):
            self.log_view.write_run_file(self.tool_id)

        self.on_run_finished(handle)
        self._handle = None

    def _record_history(self, handle: JobHandle) -> None:
        limit = int(self.ctx.settings.get("history_limit", 50))
        if limit <= 0:
            return
        self.ctx.history.limit = limit
        record = record_from_handle(
            handle,
            log=self._log_lines,
            metrics=self.output.metrics(),
            counts=self._level_counts,
            store_log=bool(self.ctx.settings.get("history_store_log", True)),
        )
        self.ctx.history.add(record)
        self.history.refresh()

    # ------------------------------------------------------------------
    # History actions
    # ------------------------------------------------------------------

    def _restore_record(self, record: RunRecord) -> None:
        self.form.set_values(record.params)
        self._select_tab("Run")

    def _rerun_record(self, record: RunRecord) -> None:
        self.form.set_values(record.params)
        self.start_run()

    def _load_record_log(self, record: RunRecord) -> None:
        self.log_view.set_lines(record.log_lines())
        self._select_tab("Log")

    # ------------------------------------------------------------------
    # Parameter actions
    # ------------------------------------------------------------------

    def _reset_params(self) -> None:
        self.form.reset()
        self.ctx.toast("Parameters reset to defaults")

    def _copy_params(self) -> None:
        import json

        self.ctx.copy(json.dumps(self.form.values(), indent=2, default=str),
                      notice="Parameters copied as JSON")

    def _reset_tool_prefs(self, store: Any) -> None:
        store.reset(list(self.PREFERENCES))
        self.pref_form.set_values(P.defaults(self.PREFERENCES))
        self.ctx.toast(f"{self.title} settings reset")

    async def _save_preset(self) -> None:
        name = await self.ctx.prompt("Save preset", "Preset name")
        if not name:
            return
        self.ctx.settings.save_preset(self.tool_id, name, self.form.values())
        self.ctx.toast(f"Preset '{name}' saved", level="SUCCESS")

    async def _load_preset(self) -> None:
        presets = self.ctx.settings.presets(self.tool_id)
        if not presets:
            self.ctx.toast("No presets saved for this tool yet", level="WARNING")
            return

        chosen: dict[str, Optional[str]] = {"name": None}

        def pick(name: str):
            def handler(_event: Any) -> None:
                chosen["name"] = name
                self.ctx.close_dialog()
                self.form.set_values(presets[name])
                self._select_tab("Run")
                self.ctx.toast(f"Loaded preset '{name}'")
            return handler

        def remove(name: str):
            def handler(_event: Any) -> None:
                self.ctx.settings.delete_preset(self.tool_id, name)
                self.ctx.close_dialog()
                self.ctx.toast(f"Preset '{name}' deleted")
            return handler

        rows = [
            ft.Row(
                spacing=6,
                controls=[
                    ft.TextButton(name, on_click=pick(name), expand=True),
                    ft.IconButton(ft.Icons.DELETE_OUTLINE, icon_size=16,
                                  tooltip="Delete", on_click=remove(name)),
                ],
            )
            for name in sorted(presets)
        ]
        self.ctx.show_dialog(ft.AlertDialog(
            title=ft.Text("Load preset"),
            content=ft.Container(width=380,
                                 content=ft.Column(rows, spacing=2, tight=True)),
            actions=[ft.TextButton("Close",
                                   on_click=lambda _e: self.ctx.close_dialog())],
        ))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _preview_artifact(self, artifact: Artifact) -> None:
        self.preview.add(artifact, select=True)
        self._select_tab("Preview")

    def select_tab(self, name: str) -> None:
        """Show one of this page's tabs by label ("Run", "Log", ...)."""
        index = getattr(self, "_tab_index", {}).get(name)
        if index is None:
            return
        self.tabs.selected_index = index
        _safe_update(self.tabs)

    def _select_tab(self, name: str) -> None:
        self.select_tab(name)

    def on_activated(self) -> None:
        """Called by the shell each time this page becomes visible."""
        self.history.refresh()

    def on_deactivated(self) -> None:
        """Called by the shell when navigating away. Jobs keep running."""

    @classmethod
    def register_preferences(cls) -> None:
        """Publish `PREFERENCES` so the global Preferences page lists them too."""
        if cls.PREFERENCES and cls.SPEC is not None:
            preferences.register_tool_section(
                cls.SPEC.id, cls.SPEC.title, cls.PREFERENCES,
                icon=cls.SPEC.icon,
                description=cls.SPEC.description,
            )


def _default_summary(handle: JobHandle) -> str:
    if handle.status == "success":
        return f"Finished in {common.duration_text(handle.duration)}"
    if handle.status == "cancelled":
        return "Cancelled"
    return handle.error or "Failed"


def _beep() -> None:
    try:
        import sys

        if sys.platform == "win32":
            import winsound

            winsound.MessageBeep(winsound.MB_ICONHAND)
        else:
            print("\a", end="", flush=True)
    except Exception:
        pass


def _safe_update(control: object) -> None:
    try:
        control.update()  # type: ignore[attr-defined]
    except Exception:
        pass
