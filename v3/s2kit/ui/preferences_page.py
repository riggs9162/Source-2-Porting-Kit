"""
The Preferences page.

Rendered entirely from :data:`s2kit.core.preferences.GLOBAL_SECTIONS` plus
whatever blocks the tools registered, using the same field widgets as the tool
forms. Adding a preference anywhere in the app makes it appear here with no
work: searchable, resettable per section, and applied live.

Appearance changes restyle the running app immediately; anything a page has to
re-read is pushed out through the settings change listener the shell installs.
"""

from __future__ import annotations

from typing import Any, Optional

import flet as ft

from s2kit.core import params as P
from s2kit.core import paths, preferences
from s2kit.ui import common, theme
from s2kit.ui.context import AppContext
from s2kit.ui.fields import ParamForm
from s2kit.ui.controls import state


@ft.control
class PreferencesPage(ft.Container):
    """Sectioned, searchable settings for the whole app."""

    ctx: Optional[AppContext] = state()

    def init(self):
        self.expand = True
        self._forms: dict[str, ParamForm] = {}
        self._section_controls: dict[str, ft.Control] = {}
        self._selected = "appearance"

    def build(self):
        self._search = ft.TextField(
            hint_text="Search every setting...",
            prefix_icon=ft.Icons.SEARCH,
            dense=True, text_size=12, expand=True,
            on_change=self._on_search,
        )
        self._nav = ft.ListView(expand=True, spacing=2, padding=ft.Padding.all(6))
        self._body = ft.Column(expand=True, spacing=12, scroll=ft.ScrollMode.AUTO)
        self._results = ft.Column(spacing=8, tight=True, visible=False)

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
                                content=ft.Icon(ft.Icons.SETTINGS, size=20,
                                                color=ft.Colors.PRIMARY),
                            ),
                            ft.Column(
                                spacing=1, tight=True, expand=True,
                                controls=[
                                    ft.Text("Preferences", size=17,
                                            weight=ft.FontWeight.W_700),
                                    common.caption(
                                        "Every option in the app, including the "
                                        "per-tool blocks."),
                                ],
                            ),
                            self._overflow(),
                        ],
                    ),
                    self._search,
                ],
            ),
        )

        self._rebuild_nav()
        self.content = ft.Column(
            expand=True,
            spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                header,
                ft.Divider(height=1, thickness=1),
                ft.Row(
                    expand=True,
                    spacing=0,
                    vertical_alignment=ft.CrossAxisAlignment.STRETCH,
                    controls=[
                        ft.Container(width=228, content=self._nav),
                        ft.VerticalDivider(width=1, thickness=1),
                        ft.Container(
                            expand=True,
                            content=ft.Column(
                                expand=True,
                                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                                controls=[
                                    ft.Container(padding=16,
                                                 content=self._results),
                                    self._body,
                                ],
                            ),
                        ),
                    ],
                ),
            ],
        )
        self.select(self._selected)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _rebuild_nav(self) -> None:
        tiles: list[ft.Control] = []
        last_was_global = True
        for section in preferences.all_sections():
            if section.tool_id and last_was_global:
                tiles.append(ft.Container(
                    padding=ft.Padding.only(left=10, top=12, bottom=4),
                    content=ft.Text("TOOLS", size=9, weight=ft.FontWeight.W_700,
                                    color=ft.Colors.ON_SURFACE_VARIANT)))
                last_was_global = False
            tiles.append(self._nav_tile(section))
        self._nav.controls = tiles
        _safe_update(self._nav)

    def _nav_tile(self, section: preferences.PrefSection) -> ft.Control:
        selected = section.id == self._selected
        return ft.Container(
            data=section.id,
            padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            border_radius=ft.BorderRadius.all(8),
            bgcolor=(ft.Colors.with_opacity(0.16, ft.Colors.PRIMARY)
                     if selected else None),
            on_click=lambda _e, s=section.id: self.select(s),
            content=ft.Row(
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(theme.icon(section.icon, "TUNE"), size=16,
                            color=(ft.Colors.PRIMARY if selected
                                   else ft.Colors.ON_SURFACE_VARIANT)),
                    ft.Text(section.title, size=12,
                            weight=(ft.FontWeight.W_600 if selected
                                    else ft.FontWeight.NORMAL),
                            expand=True, no_wrap=True,
                            overflow=ft.TextOverflow.ELLIPSIS),
                ],
            ),
        )

    def select(self, section_id: str) -> None:
        """Show one preference section."""
        self._selected = section_id
        section = next((s for s in preferences.all_sections()
                        if s.id == section_id), None)
        if section is None:
            return

        form = self._forms.get(section.id)
        if form is None:
            store: Any = (self.ctx.settings.tool_pref_store(section.tool_id)
                          if section.tool_id else self.ctx.settings)
            form = ParamForm(
                self.ctx, section.params,
                owner_id=f"prefs:{section.id}",
                store=store,
                on_change=lambda key, value: self._on_pref_changed(section, key,
                                                                   value),
            )
            self._forms[section.id] = form

        blocks: list[ft.Control] = []
        if section.description:
            blocks.append(common.section(section.title,
                                         common.caption(section.description),
                                         icon_name=section.icon))
        blocks.extend(form.build())
        blocks.append(
            ft.Row(
                spacing=8,
                controls=[
                    ft.OutlinedButton(
                        f"Reset {section.title.lower()}",
                        icon=ft.Icons.RESTART_ALT,
                        on_click=lambda _e, s=section: self._reset_section(s)),
                ],
            )
        )
        if section.id == "advanced":
            blocks.append(self._maintenance_card())

        self._body.controls = [ft.Container(padding=16,
                                            content=ft.Column(blocks, spacing=12,
                                                              tight=True))]
        self._rebuild_nav()
        _safe_update(self._body)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _on_search(self, event: ft.ControlEvent) -> None:
        query = (event.control.value or "").strip()
        if not query:
            self._results.visible = False
            self._body.visible = True
            _safe_update(self._results)
            _safe_update(self._body)
            return

        hits = preferences.search(query)
        rows: list[ft.Control] = []
        for section, param in hits[:40]:
            rows.append(
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=10, vertical=8),
                    border_radius=ft.BorderRadius.all(8),
                    bgcolor=ft.Colors.with_opacity(
                        0.3, ft.Colors.SURFACE_CONTAINER_HIGHEST),
                    on_click=lambda _e, s=section.id: self._jump(s),
                    content=ft.Row(
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Icon(theme.icon(section.icon, "TUNE"), size=15,
                                    color=ft.Colors.PRIMARY),
                            ft.Column(
                                spacing=0, tight=True, expand=True,
                                controls=[
                                    ft.Text(param.label, size=12,
                                            weight=ft.FontWeight.W_600),
                                    common.caption(
                                        f"{section.title}"
                                        f"{'  -  ' + param.help if param.help else ''}",
                                        size=10),
                                ],
                            ),
                            ft.Icon(ft.Icons.CHEVRON_RIGHT, size=16),
                        ],
                    ),
                )
            )
        self._results.controls = [
            common.caption(f"{len(hits)} match(es) for '{query}'"),
            *rows,
        ] if hits else [common.caption(f"Nothing matches '{query}'")]
        self._results.visible = True
        self._body.visible = False
        _safe_update(self._results)
        _safe_update(self._body)

    def _jump(self, section_id: str) -> None:
        self._search.value = ""
        self._results.visible = False
        self._body.visible = True
        _safe_update(self._search)
        _safe_update(self._results)
        self.select(section_id)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_pref_changed(self, section: preferences.PrefSection, key: str,
                         value: Any) -> None:
        """Apply immediately where we can; otherwise say a restart is needed."""
        if section.id == "appearance":
            self.ctx.apply_theme()
        self.ctx.refresh_chrome()
        param = next((p for p in section.params if p.key == key), None)
        if param is not None and param.restart_required:
            self.ctx.toast(f"'{param.label}' applies after a restart",
                           level="WARNING")

    def _reset_section(self, section: preferences.PrefSection) -> None:
        async def run() -> None:
            if not await self.ctx.confirm(
                    f"Reset {section.title}?",
                    "Every option in this section returns to its shipped default.",
                    confirm_label="Reset"):
                return
            if section.tool_id:
                store = self.ctx.settings.tool_pref_store(section.tool_id)
                store.reset(section.params)
            else:
                self.ctx.settings.reset_keys(section.keys())
            form = self._forms.get(section.id)
            if form is not None:
                form.set_values(P.defaults(section.params))
            self.ctx.apply_theme()
            self.ctx.refresh_chrome()
            self.ctx.toast(f"{section.title} reset to defaults")

        self.ctx.page.run_task(run)

    def _maintenance_card(self) -> ft.Control:
        settings = self.ctx.settings
        rows = [
            ("Settings file", str(settings.path)),
            ("History folder", str(paths.history_dir())),
            ("Log folder", str(paths.logs_dir())),
            ("Cache folder", str(paths.cache_dir())),
        ]
        return common.section(
            "Storage & maintenance",
            common.info_table(rows, settings, label_width=110, value_mono=True),
            ft.Row(
                spacing=8,
                wrap=True,
                controls=[
                    ft.OutlinedButton("Open config folder",
                                      icon=ft.Icons.FOLDER_OPEN,
                                      on_click=lambda _e: self.ctx.reveal(
                                          paths.config_dir())),
                    ft.OutlinedButton("Open log folder", icon=ft.Icons.ARTICLE,
                                      on_click=lambda _e: self.ctx.reveal(
                                          paths.logs_dir())),
                    ft.OutlinedButton("Copy settings as JSON",
                                      icon=ft.Icons.DATA_OBJECT,
                                      on_click=lambda _e: self.ctx.copy(
                                          settings.export_json(),
                                          notice="Settings copied")),
                    ft.OutlinedButton("Clear all run history",
                                      icon=ft.Icons.DELETE_SWEEP,
                                      on_click=self._clear_history),
                    ft.OutlinedButton("Reset everything",
                                      icon=ft.Icons.WARNING_AMBER,
                                      on_click=self._reset_all),
                ],
            ),
            icon_name="STORAGE",
        )

    def _clear_history(self, _event: ft.ControlEvent) -> None:
        async def run() -> None:
            if not await self.ctx.confirm(
                    "Clear all run history?",
                    "Every tool's recorded runs are deleted. This cannot be "
                    "undone.", confirm_label="Clear", danger=True):
                return
            removed = self.ctx.history.clear_all()
            self.ctx.toast(f"Cleared history for {removed} tool(s)")

        self.ctx.page.run_task(run)

    def _reset_all(self, _event: ft.ControlEvent) -> None:
        async def run() -> None:
            if not await self.ctx.confirm(
                    "Reset every setting?",
                    "All preferences, remembered folders, presets and window "
                    "state return to defaults.", confirm_label="Reset",
                    danger=True):
                return
            self.ctx.settings.reset_all()
            self._forms.clear()
            self.ctx.apply_theme()
            self.ctx.refresh_chrome()
            self.select(self._selected)
            self.ctx.toast("Every setting reset to defaults", level="SUCCESS")

        self.ctx.page.run_task(run)

    def _overflow(self) -> ft.Control:
        return ft.PopupMenuButton(
            icon=ft.Icons.MORE_VERT,
            tooltip="Preference actions",
            items=[
                ft.PopupMenuItem(content=ft.Text("Copy settings as JSON"),
                                 icon=ft.Icons.DATA_OBJECT,
                                 on_click=lambda _e: self.ctx.copy(
                                     self.ctx.settings.export_json(),
                                     notice="Settings copied")),
                ft.PopupMenuItem(content=ft.Text("Open config folder"),
                                 icon=ft.Icons.FOLDER_OPEN,
                                 on_click=lambda _e: self.ctx.reveal(
                                     paths.config_dir())),
                ft.PopupMenuItem(),
                ft.PopupMenuItem(content=ft.Text("Reset everything"),
                                 icon=ft.Icons.WARNING_AMBER,
                                 on_click=self._reset_all),
            ],
        )

    def on_activated(self) -> None:
        """The shell calls this when the page becomes visible."""
        self._rebuild_nav()


def _safe_update(control: object) -> None:
    try:
        control.update()  # type: ignore[attr-defined]
    except Exception:
        pass
