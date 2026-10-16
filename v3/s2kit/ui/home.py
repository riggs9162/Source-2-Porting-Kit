"""
The landing page.

A dashboard rather than a splash screen: recent tools, favourites, the whole
catalogue as cards, and an environment panel that tells the user straight away
whether the optional pieces (sourcepp, trimesh, ffmpeg, Source2Viewer-CLI) are
wired up - the thing you otherwise only discover when a run fails.
"""

from __future__ import annotations

from typing import Optional

import flet as ft

from s2kit import APP_NAME, __version__, bootstrap
from s2kit.core import registry
from s2kit.ui import common, theme
from s2kit.ui.context import AppContext
from s2kit.ui.controls import state


@ft.control
class HomePage(ft.Container):
    """Dashboard with the tool catalogue and an environment summary."""

    ctx: Optional[AppContext] = state()

    def init(self):
        self.expand = True

    def build(self):
        self.content = common.scroll_column(
            self._hero(),
            *self._recent(),
            *self._catalogue(),
            self._environment(),
        )

    # ------------------------------------------------------------------

    def _hero(self) -> ft.Control:
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=20, vertical=18),
            border_radius=ft.BorderRadius.all(12),
            bgcolor=ft.Colors.with_opacity(0.10, ft.Colors.PRIMARY),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.25,
                                                           ft.Colors.PRIMARY)),
            content=ft.Row(
                spacing=16,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(ft.Icons.CONSTRUCTION, size=34,
                            color=ft.Colors.PRIMARY),
                    ft.Column(
                        spacing=2,
                        tight=True,
                        expand=True,
                        controls=[
                            ft.Text(APP_NAME, size=22,
                                    weight=ft.FontWeight.W_700),
                            common.caption(
                                f"v{__version__}  -  Flet rebuild.  "
                                f"{registry.count()} tools registered.  "
                                "Press Ctrl+K for the command palette."),
                        ],
                    ),
                    ft.FilledTonalButton("Preferences", icon=ft.Icons.SETTINGS,
                                         on_click=lambda _e:
                                             self.ctx.navigate("__preferences__")),
                ],
            ),
        )

    def _recent(self) -> list[ft.Control]:
        settings = self.ctx.settings
        favourites = [registry.get(tool_id) for tool_id in settings.favourites()]
        recents = [registry.get(tool_id) for tool_id in settings.recent_tools()]
        blocks: list[ft.Control] = []

        favourites = [spec for spec in favourites if spec]
        recents = [spec for spec in recents if spec][:6]

        if favourites:
            blocks.append(common.section(
                "Favourites",
                ft.Row([self._chip(spec) for spec in favourites], spacing=8,
                       wrap=True, run_spacing=8),
                icon_name="STAR"))
        if recents:
            blocks.append(common.section(
                "Recently used",
                ft.Row([self._chip(spec) for spec in recents], spacing=8,
                       wrap=True, run_spacing=8),
                icon_name="HISTORY"))
        return blocks

    def _chip(self, spec: registry.ToolSpec) -> ft.Control:
        return ft.Chip(
            label=ft.Text(spec.title, size=12),
            leading=ft.Icon(theme.icon(spec.icon), size=15),
            on_click=lambda _e, t=spec.id: self.ctx.navigate(t),
        )

    def _catalogue(self) -> list[ft.Control]:
        blocks: list[ft.Control] = []
        for category, specs in registry.by_category().items():
            if not specs:
                continue
            blocks.append(common.section(
                category,
                ft.Row([self._card(spec) for spec in specs], spacing=12,
                       wrap=True, run_spacing=12),
                icon_name=_category_icon(category),
            ))
        if not blocks:
            blocks.append(common.empty_state(
                "No tools registered",
                "Drop a module into s2kit/tools and decorate its page with "
                "@tool(...) to make it appear here.",
                icon_name="EXTENSION"))
        return blocks

    def _card(self, spec: registry.ToolSpec) -> ft.Control:
        favourite = spec.id in self.ctx.settings.favourites()
        return ft.Container(
            width=300,
            padding=12,
            border_radius=ft.BorderRadius.all(10),
            bgcolor=ft.Colors.with_opacity(0.45,
                                           ft.Colors.SURFACE_CONTAINER_HIGHEST),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.12,
                                                           ft.Colors.OUTLINE)),
            on_click=lambda _e, t=spec.id: self.ctx.navigate(t),
            content=ft.Column(
                spacing=6,
                tight=True,
                controls=[
                    ft.Row(
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            ft.Icon(theme.icon(spec.icon), size=17,
                                    color=ft.Colors.PRIMARY),
                            ft.Text(spec.title, size=13,
                                    weight=ft.FontWeight.W_600, expand=True,
                                    no_wrap=True,
                                    overflow=ft.TextOverflow.ELLIPSIS),
                            *([common.pill(spec.badge,
                                           colour=ft.Colors.AMBER_400)]
                              if spec.badge else []),
                            ft.IconButton(
                                ft.Icons.STAR if favourite
                                else ft.Icons.STAR_BORDER,
                                icon_size=15,
                                icon_color=(ft.Colors.AMBER_400 if favourite
                                            else None),
                                tooltip="Toggle favourite",
                                on_click=lambda _e, t=spec.id:
                                    self._toggle_favourite(t)),
                        ],
                    ),
                    ft.Text(spec.description or "-", size=11,
                            color=ft.Colors.ON_SURFACE_VARIANT, max_lines=3,
                            no_wrap=False),
                ],
            ),
        )

    def _toggle_favourite(self, tool_id: str) -> None:
        state = self.ctx.settings.toggle_favourite(tool_id)
        self.ctx.toast("Added to favourites" if state
                       else "Removed from favourites", duration=1600)
        self.build()
        _safe_update(self)
        self.ctx.refresh_chrome()

    def _environment(self) -> ft.Control:
        report = bootstrap.probe()
        rows: list[ft.Control] = []
        for status in report.statuses:
            dep = status.dependency
            ok = status.found
            rows.append(ft.Row(
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(ft.Icons.CHECK_CIRCLE if ok
                            else (ft.Icons.ERROR if dep.required
                                  else ft.Icons.REMOVE_CIRCLE_OUTLINE),
                            size=14,
                            color=(ft.Colors.TEAL_400 if ok else
                                   (ft.Colors.RED_400 if dep.required
                                    else ft.Colors.OUTLINE))),
                    ft.Text(dep.module, size=12, width=120),
                    common.caption(status.version or ("missing - " + dep.purpose)),
                ],
            ))
        for name, purpose in bootstrap.EXTERNAL_TOOLS:
            found = report.external.get(name, "")
            rows.append(ft.Row(
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(ft.Icons.CHECK_CIRCLE if found
                            else ft.Icons.REMOVE_CIRCLE_OUTLINE, size=14,
                            color=(ft.Colors.TEAL_400 if found
                                   else ft.Colors.OUTLINE)),
                    ft.Text(name, size=12, width=120),
                    common.caption(common.truncate_middle(found, 60) or
                                   f"not on PATH - {purpose}"),
                ],
            ))

        return common.section(
            "Environment",
            ft.Column(rows, spacing=3, tight=True),
            subtitle="Optional pieces are only needed by the tools that use them.",
            icon_name="FACT_CHECK",
            actions=[
                ft.IconButton(ft.Icons.REFRESH, icon_size=16, tooltip="Re-check",
                              on_click=lambda _e: (self.build(),
                                                   _safe_update(self))),
            ],
        )


def _category_icon(category: str) -> str:
    return {
        "Models": "VIEW_IN_AR",
        "Materials": "TEXTURE",
        "Materials / PBR": "GRADIENT",
        "Sounds": "AUDIOTRACK",
        "Maps": "MAP",
        "Utility": "HANDYMAN",
        "Inspect": "TRAVEL_EXPLORE",
    }.get(category, "FOLDER")


def _safe_update(control: object) -> None:
    try:
        control.update()  # type: ignore[attr-defined]
    except Exception:
        pass
