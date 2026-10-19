"""Soundscape selection UI backed by the shared headless porter."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Optional

import flet as ft

from s2kit.definitions.soundscape_port import SoundscapePorter as Definition
from s2kit.pipelines import soundscape as ss
from s2kit.tools.soundscape_port import SoundscapePorter
from s2kit.ui import common
from s2kit.ui.tool_page import ToolPage


class SoundscapePorterPage(ToolPage):
    """Interactive picker feeding the same explicit inputs as CLI and MCP."""

    PARAMS = Definition.PARAMS
    PREFERENCES = Definition.PREFERENCES
    HELP = Definition.HELP
    RUN_LABEL = Definition.RUN_LABEL
    RUN_ICON = Definition.RUN_ICON

    def init(self):
        super().init()
        #: Everything the loaded game offers, in file order.
        self._entries: list[ss.SoundscapeEntry] = []
        #: Names the user has ticked.
        self._picked: set[str] = set()
        #: One row per entry, built once and filtered by toggling `visible`.
        self._rows: list[ft.Control] = []
        #: Where the loaded scripts and sound events live on disk.
        self._scripts: Optional[Path] = None
        self._events: Optional[Path] = None
        #: Kept for the session so a VPK is only unpacked once.
        self._scratch: Optional[Path] = None
        self._loading = False

    def extra_controls(self) -> list[ft.Control]:
        """The soundscape list, under the form: you set a source before you can
        browse it."""
        self._search = ft.TextField(
            label="Search", dense=True, expand=True,
            on_change=lambda _e: self._filter())
        self._count = common.caption("Nothing loaded yet.")
        self._list = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO, height=300)
        self._only_s2 = ft.Checkbox(
            label="Only Source 2 ones", value=False,
            on_change=lambda _e: self._filter())

        buttons = ft.Row(spacing=8, wrap=True, controls=[
            ft.FilledTonalButton("Load", icon=ft.Icons.REFRESH,
                                 on_click=lambda _e: self._load()),
            ft.OutlinedButton("Select all shown", icon=ft.Icons.DONE_ALL,
                              on_click=lambda _e: self._select_shown(True)),
            ft.OutlinedButton("Clear", icon=ft.Icons.REMOVE_DONE,
                              on_click=lambda _e: self._select_shown(False)),
        ])
        return [common.section(
            "Soundscapes",
            common.caption("Load the game, then tick what you want to port."),
            buttons,
            ft.Row(spacing=12, controls=[self._search, self._only_s2]),
            self._count,
            self._list,
            icon_name="GRAPHIC_EQ",
        )]

    def _load(self) -> None:
        """Index the source, unpacking the VPK once if that is what we have."""
        if self._loading:
            return
        values = self.form.values()
        source = str(values.get("source", "") or "")
        vpk = str(values.get("vpk", "") or "")
        if not source and not vpk:
            self.ctx.toast("Set a content folder or a VPK first",
                           level="WARNING")
            return
        self._picked = set(str(values.get("soundscapes", "")).splitlines())
        self._loading = True
        self._count.value = "Loading..."
        self._safe_update(self._count)
        threading.Thread(target=self._load_worker, args=(source, vpk),
                         daemon=True).start()

    def _load_worker(self, source: str, vpk: str) -> None:
        try:
            scripts, events = self._resolve_trees(
                Path(source) if source else None, Path(vpk) if vpk else None)
            entries: list[ss.SoundscapeEntry] = []
            for path in sorted(scripts.glob("*.txt")):
                if "manifest" in path.name.lower():
                    continue
                entries.extend(ss.index_soundscapes(
                    path.read_text(encoding="utf-8", errors="ignore"),
                    path.name))
            self._scripts, self._events = scripts, events
            self._entries = entries
            known = {entry.name for entry in entries}
            self._picked = {name for name in self._picked if name in known}
            self._build_rows()
        except Exception as exc:                     # reported in the UI
            self._entries = []
            self._count.value = f"Could not load: {exc}"
            self._safe_update(self._count)
        finally:
            self._loading = False

    def _build_rows(self) -> None:
        """One row per soundscape, created once; searching toggles visibility."""
        self._rows = [self._make_row(entry) for entry in self._entries]
        self._list.controls = self._rows
        self._safe_update(self._list)
        self._filter()

    def _make_row(self, entry: ss.SoundscapeEntry) -> ft.Control:
        tint = {"source2": ft.Colors.GREEN_300, "mixed": ft.Colors.AMBER_300,
                "source1": ft.Colors.BLUE_200}[entry.kind]
        box = ft.Checkbox(
            value=entry.name in self._picked, label="", width=36,
            on_change=lambda e, n=entry.name: self._toggle(n, e.control.value))
        detail = (f"{entry.playevents} event(s)" if entry.playevents
                  else f"{entry.source1_rules} Source 1 rule(s)")
        return ft.Row(spacing=6, controls=[
            box,
            ft.Text(entry.name, size=12, expand=True,
                    overflow=ft.TextOverflow.ELLIPSIS),
            ft.Text(detail, size=11, width=124, color=tint),
            ft.Text(entry.source, size=11, width=168,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                    overflow=ft.TextOverflow.ELLIPSIS),
        ])

    def _toggle(self, name: str, on: bool) -> None:
        if on:
            self._picked.add(name)
        else:
            self._picked.discard(name)
        self._update_count()

    def _filter(self) -> None:
        needle = (self._search.value or "").strip().lower()
        only_s2 = bool(self._only_s2.value)
        for entry, row in zip(self._entries, self._rows):
            shown = (not needle or needle in entry.name.lower()
                     or needle in entry.source.lower())
            if shown and only_s2 and not entry.playevents:
                shown = False
            if row.visible != shown:
                row.visible = shown
        self._safe_update(self._list)
        self._update_count()

    def _select_shown(self, on: bool) -> None:
        for entry, row in zip(self._entries, self._rows):
            if not row.visible:
                continue
            if on:
                self._picked.add(entry.name)
            else:
                self._picked.discard(entry.name)
            row.controls[0].value = on
        self._safe_update(self._list)
        self._update_count()

    def _update_count(self) -> None:
        self.form.set_values({**self.form.values(), "soundscapes": "\n".join(sorted(self._picked))})
        shown = sum(1 for row in self._rows if row.visible)
        self._count.value = (f"{len(self._picked)} selected of {shown} shown "
                             f"({len(self._entries)} in total)")
        self._safe_update(self._count)

    @staticmethod
    def _safe_update(control: Any) -> None:
        try:
            control.update()
        except Exception:
            pass

    def _resolve_trees(self, source, vpk):
        """Reuse the backend resolver and retain its extraction for the picker."""
        backend = SoundscapePorter()
        backend._scratch = self._scratch
        backend._prefs = self.ctx.settings.as_dict()
        result = backend._resolve_trees(source, vpk)
        self._scratch = backend._scratch
        return result

    def validate(self, values):
        """Validate explicit form values through the shared backend."""
        return SoundscapePorter().validate(values)

    def run_job(self, ctx):
        """Run an independent porter without reading picker state."""
        SoundscapePorter().run_job(ctx)
