"""
The application shell.

Owns the sidebar (searchable, grouped, collapsible, favourites-aware), the
status bar, the command palette and the page host. Tool pages are built lazily
on first visit and kept alive afterwards, so a running job survives navigating
away and its log is still there when you come back.
"""

from __future__ import annotations

import logging
from typing import Optional

import flet as ft

from s2kit import __version__
from s2kit.core import registry
from s2kit.ui import common, theme
from s2kit.ui.context import AppContext
from s2kit.ui.controls import state
from s2kit.ui.home import HomePage
from s2kit.ui.preferences_page import PreferencesPage

logger = logging.getLogger(__name__)

HOME_ID = "__home__"
PREFERENCES_ID = "__preferences__"


@ft.control
class AppShell(ft.Container):
    """Sidebar + page host + status bar."""

    ctx: Optional[AppContext] = state()

    def init(self):
        self.expand = True
        self._pages: dict[str, ft.Control] = {}
        self._current = ""
        self._filter = ""
        self._collapsed = False

    def build(self):
        ctx = self.ctx
        self._collapsed = bool(ctx.settings.get("sidebar_collapsed", False))

        # -- host ----------------------------------------------------------
        self._host = ft.Container(expand=True)

        # -- sidebar -------------------------------------------------------
        self._search = ft.TextField(
            hint_text="Search tools...",
            prefix_icon=ft.Icons.SEARCH,
            dense=True, text_size=12, height=36,
            on_change=self._on_search,
        )
        self._tool_list = ft.ListView(expand=True, spacing=2,
                                      padding=ft.Padding.symmetric(horizontal=6,
                                                                   vertical=4))
        self._collapse_button = ft.IconButton(
            ft.Icons.MENU_OPEN, icon_size=17, tooltip="Collapse the sidebar",
            on_click=lambda _e: self.toggle_sidebar())
        self._sidebar = ft.Container(
            width=(theme.Metrics.for_settings(ctx.settings).sidebar_width
                   if not self._collapsed
                   else theme.Metrics().sidebar_collapsed_width),
            content=self._build_sidebar(),
        )

        # -- status bar ----------------------------------------------------
        self._status_text = common.caption("Ready")
        self._jobs_text = common.caption("")
        self._status_bar = ft.Container(
            padding=ft.Padding.symmetric(horizontal=14, vertical=5),
            bgcolor=ft.Colors.with_opacity(0.5, ft.Colors.SURFACE_CONTAINER_HIGHEST),
            content=ft.Row(
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(ft.Icons.CIRCLE, size=8, color=ft.Colors.TEAL_400),
                    self._status_text,
                    ft.Container(expand=True),
                    self._jobs_text,
                    common.caption(f"{registry.count()} tools"),
                    common.caption(f"v{__version__}"),
                ],
            ),
        )

        self.content = ft.Column(
            expand=True,
            spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Row(
                    expand=True,
                    spacing=0,
                    vertical_alignment=ft.CrossAxisAlignment.STRETCH,
                    controls=[
                        self._sidebar,
                        ft.VerticalDivider(width=1, thickness=1),
                        self._host,
                    ],
                ),
                ft.Divider(height=1, thickness=1),
                self._status_bar,
            ],
        )

        ctx.set_status = self.set_status
        ctx.navigate = self.navigate
        ctx.refresh_chrome = self.refresh_chrome

    # ------------------------------------------------------------------
    # Sidebar
    # ------------------------------------------------------------------

    def _build_sidebar(self) -> ft.Control:
        if self._collapsed:
            return ft.Column(
                spacing=4,
                expand=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Container(height=8),
                    ft.IconButton(ft.Icons.MENU, icon_size=18,
                                  tooltip="Expand the sidebar",
                                  on_click=lambda _e: self.toggle_sidebar()),
                    ft.IconButton(ft.Icons.HOME, icon_size=18, tooltip="Home",
                                  on_click=lambda _e: self.navigate(HOME_ID)),
                    ft.IconButton(ft.Icons.SEARCH, icon_size=18,
                                  tooltip="Command palette (Ctrl+K)",
                                  on_click=lambda _e: self.open_palette()),
                    ft.Container(expand=True),
                    ft.IconButton(ft.Icons.SETTINGS, icon_size=18,
                                  tooltip="Preferences",
                                  on_click=lambda _e:
                                      self.navigate(PREFERENCES_ID)),
                    ft.Container(height=8),
                ],
            )

        header = ft.Container(
            padding=ft.Padding.only(left=12, right=6, top=10, bottom=4),
            content=ft.Row(
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(ft.Icons.CONSTRUCTION, size=18,
                            color=ft.Colors.PRIMARY),
                    ft.Column(
                        spacing=0,
                        tight=True,
                        expand=True,
                        controls=[
                            ft.Text("Porting Kit", size=13,
                                    weight=ft.FontWeight.W_700),
                            common.caption(f"v{__version__}", size=9),
                        ],
                    ),
                    self._collapse_button,
                ],
            ),
        )

        self._refresh_tool_list()

        footer = ft.Container(
            padding=ft.Padding.symmetric(horizontal=8, vertical=6),
            content=ft.Column(
                spacing=2,
                tight=True,
                controls=[
                    self._nav_button("Home", ft.Icons.HOME, HOME_ID),
                    self._nav_button("Preferences", ft.Icons.SETTINGS,
                                     PREFERENCES_ID),
                    ft.TextButton(
                        "Command palette  Ctrl+K",
                        icon=ft.Icons.KEYBOARD_COMMAND_KEY,
                        on_click=lambda _e: self.open_palette(),
                        style=ft.ButtonStyle(padding=ft.Padding.symmetric(
                            horizontal=10, vertical=4)),
                    ),
                ],
            ),
        )

        return ft.Column(
            expand=True,
            spacing=4,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                header,
                ft.Container(padding=ft.Padding.symmetric(horizontal=8),
                             content=self._search),
                self._tool_list,
                ft.Divider(height=1, thickness=1),
                footer,
            ],
        )

    def _nav_button(self, label: str, icon, target: str) -> ft.Control:
        selected = self._current == target
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=10, vertical=7),
            border_radius=ft.BorderRadius.all(8),
            bgcolor=(ft.Colors.with_opacity(0.16, ft.Colors.PRIMARY)
                     if selected else None),
            on_click=lambda _e: self.navigate(target),
            content=ft.Row(
                spacing=10,
                controls=[
                    ft.Icon(icon, size=16,
                            color=(ft.Colors.PRIMARY if selected
                                   else ft.Colors.ON_SURFACE_VARIANT)),
                    ft.Text(label, size=12,
                            weight=(ft.FontWeight.W_600 if selected
                                    else ft.FontWeight.NORMAL)),
                ],
            ),
        )

    def _refresh_tool_list(self) -> None:
        settings = self.ctx.settings
        show_icons = bool(settings.get("show_tool_icons", True))
        sort_mode = str(settings.get("sidebar_sort", "category"))
        favourites = settings.favourites()
        specs = registry.search(self._filter) if self._filter else registry.all_tools()

        tiles: list[ft.Control] = []

        if not self._filter and favourites:
            starred = [spec for spec in specs if spec.id in favourites]
            if starred:
                tiles.append(self._group_label("FAVOURITES"))
                tiles.extend(self._tool_tile(spec, show_icons) for spec in starred)

        if sort_mode == "alphabetical":
            for spec in sorted(specs, key=lambda s: s.title.lower()):
                tiles.append(self._tool_tile(spec, show_icons))
        elif sort_mode == "recent":
            order = settings.recent_tools()
            ranked = sorted(specs, key=lambda s: (order.index(s.id)
                                                  if s.id in order else 999,
                                                  s.title.lower()))
            for spec in ranked:
                tiles.append(self._tool_tile(spec, show_icons))
        else:
            current_category = ""
            for spec in specs:
                if spec.category != current_category:
                    current_category = spec.category
                    tiles.append(self._group_label(current_category.upper()))
                tiles.append(self._tool_tile(spec, show_icons))

        if not tiles:
            tiles = [ft.Container(padding=14,
                                  content=common.caption(
                                      f"No tool matches '{self._filter}'"))]
        self._tool_list.controls = tiles
        _safe_update(self._tool_list)

    def _group_label(self, text: str) -> ft.Control:
        return ft.Container(
            padding=ft.Padding.only(left=10, top=10, bottom=2),
            content=ft.Text(text, size=9, weight=ft.FontWeight.W_700,
                            color=ft.Colors.ON_SURFACE_VARIANT),
        )

    def _tool_tile(self, spec: registry.ToolSpec, show_icons: bool) -> ft.Control:
        selected = self._current == spec.id
        row: list[ft.Control] = []
        if show_icons:
            row.append(ft.Icon(theme.icon(spec.icon), size=15,
                               color=(ft.Colors.PRIMARY if selected
                                      else ft.Colors.ON_SURFACE_VARIANT)))
        row.append(ft.Text(spec.title, size=12, expand=True, no_wrap=True,
                           overflow=ft.TextOverflow.ELLIPSIS,
                           weight=(ft.FontWeight.W_600 if selected
                                   else ft.FontWeight.NORMAL)))
        if spec.badge:
            row.append(common.pill(spec.badge, colour=ft.Colors.AMBER_400))
        return ft.Container(
            data=spec.id,
            padding=ft.Padding.symmetric(horizontal=10, vertical=7),
            border_radius=ft.BorderRadius.all(8),
            bgcolor=(ft.Colors.with_opacity(0.16, ft.Colors.PRIMARY)
                     if selected else None),
            tooltip=spec.description or spec.title,
            on_click=lambda _e, t=spec.id: self.navigate(t),
            content=ft.Row(row, spacing=10,
                           vertical_alignment=ft.CrossAxisAlignment.CENTER),
        )

    def _on_search(self, event: ft.ControlEvent) -> None:
        self._filter = (event.control.value or "").strip()
        self._refresh_tool_list()

    def toggle_sidebar(self) -> None:
        self._collapsed = not self._collapsed
        self.ctx.settings.set("sidebar_collapsed", self._collapsed)
        metrics = theme.Metrics.for_settings(self.ctx.settings)
        self._sidebar.width = (metrics.sidebar_collapsed_width if self._collapsed
                               else metrics.sidebar_width)
        self._sidebar.content = self._build_sidebar()
        _safe_update(self._sidebar)

    def refresh_chrome(self) -> None:
        """Rebuild the sidebar after a preference or favourite change."""
        metrics = theme.Metrics.for_settings(self.ctx.settings)
        self._sidebar.width = (metrics.sidebar_collapsed_width if self._collapsed
                               else metrics.sidebar_width)
        self._sidebar.content = self._build_sidebar()
        _safe_update(self._sidebar)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def navigate(self, target: str) -> None:
        """Show a tool page, the home page or the preferences page."""
        if target == self._current:
            return
        previous = self._pages.get(self._current)
        if previous is not None and hasattr(previous, "on_deactivated"):
            previous.on_deactivated()

        page = self._pages.get(target)
        if page is None:
            page = self._create_page(target)
            if page is None:
                self.ctx.toast(f"Unknown page: {target}", level="ERROR")
                return
            self._pages[target] = page

        self._current = target
        self._host.content = page
        _safe_update(self._host)

        if hasattr(page, "on_activated"):
            page.on_activated()

        settings = self.ctx.settings
        if target not in (HOME_ID, PREFERENCES_ID):
            settings.push_recent_tool(target)
            spec = registry.get(target)
            self.set_status(spec.title if spec else target)
            if bool(settings.get("remember_last_tool", True)):
                settings.set("last_tool", target)
        else:
            self.set_status("Ready")
        self.refresh_chrome()

    def _create_page(self, target: str) -> Optional[ft.Control]:
        if target == HOME_ID:
            return HomePage(ctx=self.ctx)
        if target == PREFERENCES_ID:
            return PreferencesPage(ctx=self.ctx)
        spec = registry.get(target)
        if spec is None:
            return None
        try:
            return spec.factory(ctx=self.ctx)
        except Exception as exc:
            logger.exception("Could not create tool page %s", target)
            self.ctx.toast(f"{spec.title} failed to open: {exc}", level="ERROR")
            return None

    def current_page(self) -> Optional[ft.Control]:
        return self._pages.get(self._current)

    # ------------------------------------------------------------------
    # Status bar
    # ------------------------------------------------------------------

    def set_status(self, text: str) -> None:
        self._status_text.value = text
        active = self.ctx.jobs.active_count()
        self._jobs_text.value = (f"{active} job(s) running" if active else "")
        _safe_update(self._status_text)
        _safe_update(self._jobs_text)

    # ------------------------------------------------------------------
    # Command palette
    # ------------------------------------------------------------------

    def open_palette(self) -> None:
        """Ctrl+K: jump to any tool, preference section or shell action."""
        results = ft.ListView(height=340, spacing=2)
        self._palette_results = results
        self._palette_selection = 0
        field = ft.TextField(
            hint_text="Jump to a tool, a setting, or an action...",
            prefix_icon=ft.Icons.SEARCH, autofocus=True, dense=True,
            text_size=13,
        )

        def entries(query: str) -> list[ft.Control]:
            rows: list[ft.Control] = []
            for spec in registry.search(query)[:12]:
                rows.append(self._palette_row(
                    spec.title, spec.category, theme.icon(spec.icon),
                    lambda t=spec.id: self.navigate(t)))
            from s2kit.core import preferences

            for section, param in preferences.search(query)[:8]:
                rows.append(self._palette_row(
                    param.label, f"Preferences  -  {section.title}",
                    theme.icon(section.icon, "TUNE"),
                    lambda s=section.id: self._open_preference(s)))
            for label, icon, action in self._shell_actions():
                if not query or query.lower() in label.lower():
                    rows.append(self._palette_row(label, "Action", icon, action))
            if not rows:
                rows.append(ft.Container(padding=16,
                                         content=common.caption(
                                             f"Nothing matches '{query}'")))
            return rows

        def refresh(_event=None) -> None:
            results.controls = entries((field.value or "").strip())
            self._palette_selection = 0
            self._highlight_palette()
            _safe_update(results)

        field.on_change = refresh
        field.on_submit = lambda _e: self._activate_palette()
        refresh()

        dialog = ft.AlertDialog(
            content=ft.Container(
                width=560,
                content=ft.Column([field, ft.Divider(height=1), results],
                                  spacing=8, tight=True),
            ),
            content_padding=ft.Padding.all(14),
        )
        self._palette = dialog
        self.ctx.show_dialog(dialog)

    def _palette_row(self, title: str, subtitle: str, icon,
                     action) -> ft.Control:
        def handler(_event: ft.ControlEvent) -> None:
            self.ctx.close_dialog()
            action()

        return ft.Container(
            data=action,
            padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            border_radius=ft.BorderRadius.all(8),
            on_click=handler,
            content=ft.Row(
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(icon, size=16, color=ft.Colors.PRIMARY),
                    ft.Column(
                        spacing=0,
                        tight=True,
                        expand=True,
                        controls=[
                            ft.Text(title, size=12, weight=ft.FontWeight.W_600),
                            common.caption(subtitle, size=10),
                        ],
                    ),
                    ft.Icon(ft.Icons.KEYBOARD_RETURN, size=13,
                            color=ft.Colors.OUTLINE),
                ],
            ),
        )

    def _highlight_palette(self) -> None:
        """Highlight the result selected by keyboard navigation."""
        for index, row in enumerate(self._palette_results.controls):
            row.bgcolor = (ft.Colors.with_opacity(0.14, ft.Colors.PRIMARY)
                           if index == self._palette_selection and callable(row.data)
                           else None)
        _safe_update(self._palette_results)

    def _activate_palette(self) -> None:
        """Activate the highlighted result once, including from text submission."""
        if not getattr(getattr(self, "_palette", None), "open", False):
            return
        rows = self._palette_results.controls
        if rows and callable(rows[self._palette_selection].data):
            action = rows[self._palette_selection].data
            self.ctx.close_dialog()
            action()

    def _shell_actions(self) -> list[tuple[str, object, object]]:
        return [
            ("Home", ft.Icons.HOME, lambda: self.navigate(HOME_ID)),
            ("Preferences", ft.Icons.SETTINGS,
             lambda: self.navigate(PREFERENCES_ID)),
            ("Toggle theme", ft.Icons.BRIGHTNESS_6, self.toggle_theme),
            ("Toggle sidebar", ft.Icons.MENU_OPEN, self.toggle_sidebar),
            ("Open config folder", ft.Icons.FOLDER_OPEN, self._open_config),
            ("Open log folder", ft.Icons.ARTICLE, self._open_logs),
        ]

    def _open_preference(self, section_id: str) -> None:
        self.navigate(PREFERENCES_ID)
        page = self._pages.get(PREFERENCES_ID)
        if page is not None and hasattr(page, "select"):
            page.select(section_id)

    def _open_config(self) -> None:
        from s2kit.core import paths

        self.ctx.reveal(paths.config_dir())

    def _open_logs(self) -> None:
        from s2kit.core import paths

        self.ctx.reveal(paths.logs_dir())

    def toggle_theme(self) -> None:
        current = str(self.ctx.settings.get("theme_mode", "dark"))
        nxt = {"dark": "light", "light": "system", "system": "dark"}[current]
        self.ctx.settings.set("theme_mode", nxt)
        self.ctx.apply_theme()
        self.ctx.toast(f"Theme: {nxt}", duration=1500)

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------

    def handle_key(self, event: ft.KeyboardEvent) -> None:
        """Global shortcuts, installed by :mod:`s2kit.app`.

        The open tool gets first refusal, so a page with its own editing keys
        (Delete, the arrows) can claim them. A page must return True only for
        keys it actually handled, or it would swallow the app-wide ones.
        """
        key = (event.key or "").upper()
        if getattr(getattr(self, "_palette", None), "open", False):
            if key in {"ARROW UP", "ARROW DOWN", "ARROWUP", "ARROWDOWN"}:
                rows = self._palette_results.controls
                if rows:
                    delta = -1 if "UP" in key else 1
                    self._palette_selection = (self._palette_selection + delta) % len(rows)
                    self._highlight_palette()
                    self.ctx.page.run_task(self._palette_results.scroll_to,
                                           offset=self._palette_selection * 52, duration=100)
            elif key == "ENTER":
                self._activate_palette()
            elif key == "ESCAPE":
                self.ctx.close_dialog()
            return
        page = self.current_page()
        claim = getattr(page, "handle_key", None)
        if claim is not None:
            try:
                if claim(event):
                    return
            except Exception:
                logger.exception("tool key handler failed")

        key = (event.key or "").upper()
        if event.ctrl and key == "K":
            self.open_palette()
        elif event.ctrl and key == "P":
            self.open_palette()
        elif event.ctrl and key == ",":
            self.navigate(PREFERENCES_ID)
        elif event.ctrl and key == "B":
            self.toggle_sidebar()
        elif event.ctrl and key == "H":
            self.navigate(HOME_ID)
        elif key == "F5":
            page = self.current_page()
            if page is not None and hasattr(page, "start_run"):
                page.start_run()
        elif event.ctrl and event.shift and key == "T":
            self.toggle_theme()


def _safe_update(control: object) -> None:
    try:
        control.update()  # type: ignore[attr-defined]
    except Exception:
        pass
