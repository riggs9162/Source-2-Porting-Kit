"""
Theming, driven entirely by preferences.

Everything visual the user can change - light/dark, accent seed, UI font,
monospace font, text scale, density - is read from :class:`Settings` here and
nowhere else, so `apply(page, settings)` after any preference change is enough
to restyle the whole app live.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import flet as ft

from s2kit.core.settings import Settings

#: Accent key -> Material seed colour. Keys match the `accent` preference.
ACCENTS: dict[str, str] = {
    "cyan": ft.Colors.CYAN,
    "blue": ft.Colors.BLUE,
    "indigo": ft.Colors.INDIGO,
    "violet": ft.Colors.DEEP_PURPLE,
    "magenta": ft.Colors.PINK,
    "orange": ft.Colors.DEEP_ORANGE,
    "amber": ft.Colors.AMBER,
    "green": ft.Colors.GREEN,
    "teal": ft.Colors.TEAL,
    "red": ft.Colors.RED,
    "slate": ft.Colors.BLUE_GREY,
}

#: Log level -> colour. Also used by the status bar and metric tiles.
LEVEL_COLOURS: dict[str, str] = {
    "DEBUG": ft.Colors.OUTLINE,
    "INFO": ft.Colors.ON_SURFACE,
    "WARNING": ft.Colors.AMBER_400,
    "ERROR": ft.Colors.RED_400,
    "SUCCESS": ft.Colors.TEAL_400,
}

TONE_COLOURS: dict[str, str] = {
    "neutral": ft.Colors.ON_SURFACE,
    "good": ft.Colors.TEAL_400,
    "warn": ft.Colors.AMBER_400,
    "bad": ft.Colors.RED_400,
}

STATUS_COLOURS: dict[str, str] = {
    "running": ft.Colors.BLUE_400,
    "success": ft.Colors.TEAL_400,
    "failed": ft.Colors.RED_400,
    "cancelled": ft.Colors.AMBER_400,
}

#: Base sizes for the Material text styles we scale with `text_scale`.
_BASE_TEXT_SIZES: dict[str, float] = {
    "display_large": 57, "display_medium": 45, "display_small": 36,
    "headline_large": 32, "headline_medium": 28, "headline_small": 24,
    "title_large": 22, "title_medium": 16, "title_small": 14,
    "body_large": 16, "body_medium": 14, "body_small": 12,
    "label_large": 14, "label_medium": 12, "label_small": 11,
}

MONO_FALLBACKS = ("Consolas", "Cascadia Mono", "Courier New", "monospace")


@dataclass(frozen=True)
class Metrics:
    """Spacing tokens, tightened when the compact-density preference is on."""

    gap: int = 12
    gap_small: int = 8
    gap_large: int = 20
    page_padding: int = 16
    card_padding: int = 14
    row_height: int = 40
    sidebar_width: int = 248
    sidebar_collapsed_width: int = 64
    radius: int = 10

    @staticmethod
    def for_settings(settings: Settings) -> "Metrics":
        if bool(settings.get("compact_density")):
            return Metrics(gap=8, gap_small=4, gap_large=12, page_padding=10,
                           card_padding=10, row_height=32, sidebar_width=224,
                           radius=8)
        return Metrics()


def accent_colour(settings: Settings) -> str:
    """Seed colour for the current accent preference."""
    return ACCENTS.get(str(settings.get("accent", "cyan")), ft.Colors.CYAN)


def theme_mode(settings: Settings) -> ft.ThemeMode:
    return {
        "dark": ft.ThemeMode.DARK,
        "light": ft.ThemeMode.LIGHT,
        "system": ft.ThemeMode.SYSTEM,
    }.get(str(settings.get("theme_mode", "dark")), ft.ThemeMode.DARK)


def mono_font(settings: Settings) -> str:
    """The monospace family for logs and the text viewer."""
    return str(settings.get("mono_font") or MONO_FALLBACKS[0])


def text_scale(settings: Settings) -> float:
    try:
        return max(0.7, min(2.0, float(settings.get("text_scale", 1.0))))
    except (TypeError, ValueError):
        return 1.0


def scaled(settings: Settings, size: float) -> float:
    """Scale a hand-picked font size by the user's text scale."""
    return round(size * text_scale(settings), 1)


def build_theme(settings: Settings) -> ft.Theme:
    """A Material theme reflecting every appearance preference."""
    scale = text_scale(settings)
    text_theme = ft.TextTheme(**{
        name: ft.TextStyle(size=round(size * scale, 1), color=ft.Colors.ON_SURFACE)
        for name, size in _BASE_TEXT_SIZES.items()
    })
    return ft.Theme(
        color_scheme_seed=accent_colour(settings),
        font_family=str(settings.get("font_family") or "") or None,
        text_theme=text_theme,
        visual_density=(ft.VisualDensity.COMPACT
                        if bool(settings.get("compact_density"))
                        else ft.VisualDensity.STANDARD),
        scrollbar_theme=ft.ScrollbarTheme(thickness=8, radius=4,
                                          thumb_visibility=True,
                                          interactive=True),
        card_theme=ft.CardTheme(elevation=0),
    )


def apply(page: ft.Page, settings: Settings) -> None:
    """Push the current appearance preferences onto a live page."""
    page.theme = build_theme(settings)
    page.dark_theme = build_theme(settings)
    page.theme_mode = theme_mode(settings)


def icon(name: str, fallback: str = "BUILD") -> Any:
    """Resolve an `ft.Icons` member name (as stored in specs) to its value."""
    if not name:
        name = fallback
    value = getattr(ft.Icons, name.upper(), None)
    if value is None:
        value = getattr(ft.Icons, fallback.upper(), ft.Icons.BUILD)
    return value


def level_colour(level: str) -> str:
    return LEVEL_COLOURS.get(level.upper(), ft.Colors.ON_SURFACE)


def status_colour(status: str) -> str:
    return STATUS_COLOURS.get(status.lower(), ft.Colors.ON_SURFACE_VARIANT)


def tone_colour(tone: str) -> str:
    return TONE_COLOURS.get(tone.lower(), ft.Colors.ON_SURFACE)


def viewer_background(settings: Settings) -> Optional[str]:
    """Flet colour for the preview backdrop, or None for the checkerboard."""
    return {
        "checker": None,
        "dark": "#18181c",
        "light": "#ebebeb",
        "magenta": "#ff00ff",
    }.get(str(settings.get("viewer_bg", "checker")))
