"""
The preference schema: every user-facing setting in one declarative place.

Preferences reuse the tool parameter schema (`core.params`), so the exact same
field widgets render the Preferences page, a tool's Settings tab and a tool's
Run tab. Adding a knob anywhere in the app is one `Param` entry - it is then
persisted, validated, searchable, resettable and exportable for free.

Sections come from two places:

* :data:`GLOBAL_SECTIONS` - app-wide preferences declared here.
* :func:`register_tool_section` - a tool declaring `PREFERENCES = [...]`
  contributes its own section at import time, stored per tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Protocol

from s2kit.core.params import (
    BoolParam, ChoiceParam, FloatParam, IntParam, Param, PathParam, TextParam,
    options,
)


class PrefStore(Protocol):
    """Anything that can read and write preference values by key."""

    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any, *, save: bool = True) -> None: ...


@dataclass
class PrefSection:
    """A titled block of related preferences."""

    id: str
    title: str
    params: list[Param]
    icon: str = "TUNE"
    description: str = ""
    #: None for app-wide preferences, otherwise the owning tool id.
    tool_id: Optional[str] = None
    order: int = 100

    def keys(self) -> list[str]:
        return [param.key for param in self.params]


# ----------------------------------------------------------------------
# App-wide preferences
# ----------------------------------------------------------------------

_THEME_MODES = options(
    ("dark", "Dark"),
    ("light", "Light"),
    ("system", "Follow system"),
)

_ACCENTS = options(
    ("cyan", "Cyan"), ("blue", "Blue"), ("indigo", "Indigo"), ("violet", "Violet"),
    ("magenta", "Magenta"), ("orange", "Orange"), ("amber", "Amber"),
    ("green", "Green"), ("teal", "Teal"), ("red", "Red"), ("slate", "Slate"),
)

_VIEWER_BACKGROUNDS = options(
    ("checker", "Alpha checker"),
    ("dark", "Dark"),
    ("light", "Light"),
    ("magenta", "Magenta (alpha debug)"),
)

_SHADING_MODES = options(
    ("shaded", "Shaded"),
    ("wireframe", "Wireframe"),
    ("normals", "Normals"),
    ("depth", "Depth"),
)

_SORT_MODES = options(
    ("category", "By category"),
    ("alphabetical", "Alphabetical"),
    ("recent", "Recently used"),
)


GLOBAL_SECTIONS: list[PrefSection] = [
    PrefSection(
        id="appearance",
        title="Appearance",
        icon="PALETTE",
        description="Theme, accent colour and density of the whole app.",
        order=10,
        params=[
            ChoiceParam("theme_mode", "Theme", default="dark", choices=_THEME_MODES,
                        style="segmented", group="Theme",
                        help="Dark is the default; 'Follow system' tracks Windows."),
            ChoiceParam("accent", "Accent colour", default="cyan", choices=_ACCENTS,
                        group="Theme",
                        help="Seeds the Material colour scheme for the whole UI."),
            TextParam("font_family", "UI font", default="", group="Typography",
                      hint="Segoe UI", help="Leave empty for the Flet default."),
            FloatParam("text_scale", "Text scale", default=1.0, min=0.8, max=1.6,
                       step=0.05, decimals=2, style="slider", group="Typography",
                       help="Scales every text style in the app."),
            TextParam("mono_font", "Monospace font", default="Consolas",
                      group="Typography",
                      help="Used by the log pane and the text viewer."),
            BoolParam("compact_density", "Compact density", default=False,
                      group="Layout",
                      help="Tighter padding everywhere; fits more on screen."),
            BoolParam("show_tool_icons", "Show tool icons in the sidebar",
                      default=True, group="Layout"),
            ChoiceParam("form_columns", "Parameter form layout", default="auto",
                        choices=options(("auto", "Auto (2 columns when wide)"),
                                        ("one", "Always one column"),
                                        ("two", "Always two columns")),
                        group="Layout",
                        help="Tools with many option groups fit on one screen "
                             "in two columns instead of a long scroll."),
            ChoiceParam("sidebar_sort", "Sidebar order", default="category",
                        choices=_SORT_MODES, group="Layout"),
        ],
    ),
    PrefSection(
        id="behaviour",
        title="Behaviour",
        icon="TOGGLE_ON",
        description="What the app remembers and when it asks before acting.",
        order=20,
        params=[
            BoolParam("remember_last_tool", "Reopen the last tool on startup",
                      default=True, group="Session"),
            BoolParam("restore_last_params", "Restore the last used parameters",
                      default=True, group="Session",
                      help="Each tool reopens with the values from its last run."),
            BoolParam("remember_window_size", "Remember window size and position",
                      default=True, group="Session"),
            BoolParam("confirm_destructive", "Confirm before overwriting files",
                      default=True, group="Safety"),
            BoolParam("dry_run_default", "Default new runs to dry-run",
                      default=False, group="Safety",
                      help="Tools that support a preview mode start with it on."),
            BoolParam("notify_on_finish", "Show a toast when a run finishes",
                      default=True, group="Feedback"),
            BoolParam("beep_on_error", "Play the system sound on failure",
                      default=False, group="Feedback"),
            IntParam("max_parallel", "Default worker threads", default=0, min=0,
                     max=64, group="Performance", suffix="threads",
                     help="0 = pick automatically from the CPU count."),
        ],
    ),
    PrefSection(
        id="runs",
        title="Runs, logs & history",
        icon="RECEIPT_LONG",
        description="How much of each run is kept and how the log pane behaves.",
        order=30,
        params=[
            IntParam("history_limit", "Run records kept per tool", default=50,
                     min=0, max=500, group="History",
                     help="0 disables history entirely."),
            BoolParam("history_store_log", "Store the full log with each record",
                      default=True, group="History"),
            BoolParam("history_autosave_params", "Save parameters with each run",
                      default=True, group="History"),
            IntParam("log_buffer_lines", "Log lines held in memory", default=5000,
                     min=200, max=200_000, group="Log pane"),
            BoolParam("log_autoscroll", "Follow the log while a run is active",
                      default=True, group="Log pane"),
            BoolParam("log_show_timestamps", "Show timestamps", default=True,
                      group="Log pane"),
            BoolParam("log_wrap", "Wrap long lines", default=False,
                      group="Log pane"),
            ChoiceParam("log_min_level", "Minimum level shown", default="DEBUG",
                        choices=options(
                            ("DEBUG", "Debug"), ("INFO", "Info"),
                            ("WARNING", "Warning"), ("ERROR", "Error")),
                        group="Log pane"),
            BoolParam("log_to_file", "Also write tool logs to disk", default=True,
                      group="Log pane",
                      help="One file per run under logs/runs/."),
        ],
    ),
    PrefSection(
        id="viewers",
        title="Viewers & previews",
        icon="VISIBILITY",
        description="Texture, model, audio and text preview behaviour.",
        order=40,
        params=[
            BoolParam("auto_preview", "Preview artefacts as a run produces them",
                      default=True, group="General"),
            IntParam("preview_max_mb", "Skip previews larger than", default=256,
                     min=1, max=4096, suffix="MB", group="General"),
            ChoiceParam("viewer_bg", "Image background", default="checker",
                        choices=_VIEWER_BACKGROUNDS, group="Textures"),
            BoolParam("image_smooth", "Smooth image scaling", default=False,
                      group="Textures",
                      help="Off shows hard texels, which is what you want for "
                           "inspecting a normal map."),
            BoolParam("show_alpha_channel", "Offer an alpha-only view",
                      default=True, group="Textures"),
            IntParam("model_viewer_size", "Model render size", default=512,
                     min=256, max=2048, step=64, suffix="px", group="Models"),
            ChoiceParam("model_viewer_shading", "Default shading",
                        default="shaded", choices=_SHADING_MODES, group="Models"),
            BoolParam("model_show_wireframe", "Overlay wireframe", default=False,
                      group="Models"),
            IntParam("model_viewer_max_faces", "Face budget", default=60000,
                     min=2000, max=1_000_000, step=1000, group="Models",
                     help="Meshes above this are decimated for the preview only."),
            IntParam("waveform_height", "Waveform height", default=140, min=60,
                     max=400, suffix="px", group="Audio"),
            BoolParam("waveform_rms", "Draw the RMS envelope too", default=True,
                      group="Audio"),
            IntParam("text_viewer_tab_width", "Text viewer tab width", default=4,
                     min=1, max=8, group="Text"),
            BoolParam("text_viewer_line_numbers", "Show line numbers",
                      default=True, group="Text"),
        ],
    ),
    PrefSection(
        id="programs",
        title="External programs",
        icon="TERMINAL",
        description="Paths to the executables the pipelines shell out to. "
                    "Leave empty to use whatever is on PATH.",
        order=50,
        params=[
            PathParam("vrf_cli_path", "Source2Viewer-CLI", mode="open_file",
                      extensions=("exe",), must_exist=False, group="Source 2"),
            PathParam("studiomdl_path", "studiomdl.exe", mode="open_file",
                      extensions=("exe",), must_exist=False, group="Source 1"),
            PathParam("gameinfo_path", "gameinfo.txt", mode="open_file",
                      extensions=("txt",), must_exist=False, group="Source 1",
                      help="The mod folder studiomdl compiles against."),
            PathParam("ffmpeg_path", "ffmpeg.exe", mode="open_file",
                      extensions=("exe",), must_exist=False, group="Audio"),
            PathParam("default_output_dir", "Default output folder",
                      mode="directory", must_exist=False, group="Paths",
                      help="Pre-fills the output picker on every tool."),
            TextParam("default_material_path", "Default material path",
                      default="models/ports", group="Paths",
                      help="Used when a tool writes VMTs without being told where."),
        ],
    ),
    PrefSection(
        id="advanced",
        title="Advanced",
        icon="SCIENCE",
        description="Diagnostics and escape hatches.",
        order=90,
        params=[
            BoolParam("debug_mode", "Developer mode", default=False,
                      help="Shows internal ids, timings and the raw event feed."),
            BoolParam("log_flet_internals", "Log Flet framework messages",
                      default=False, restart_required=True),
            BoolParam("cache_previews", "Cache generated previews on disk",
                      default=True,
                      help="Speeds up reopening the same texture or model."),
            IntParam("cache_limit_mb", "Preview cache limit", default=512,
                     min=0, max=8192, suffix="MB"),
        ],
    ),
]


# ----------------------------------------------------------------------
# Tool-contributed sections
# ----------------------------------------------------------------------

_TOOL_SECTIONS: dict[str, PrefSection] = {}


def register_tool_section(tool_id: str, title: str, params: Iterable[Param],
                          *, icon: str = "TUNE", description: str = "") -> PrefSection:
    """Register a tool's own preference block. Re-registering replaces it."""
    section = PrefSection(
        id=f"tool:{tool_id}",
        title=title,
        params=list(params),
        icon=icon,
        description=description,
        tool_id=tool_id,
    )
    _TOOL_SECTIONS[tool_id] = section
    return section


def tool_section(tool_id: str) -> Optional[PrefSection]:
    """The preference section a tool registered, if any."""
    return _TOOL_SECTIONS.get(tool_id)


def tool_sections() -> list[PrefSection]:
    """Every tool-contributed section, sorted by title."""
    return sorted(_TOOL_SECTIONS.values(), key=lambda s: s.title.lower())


def all_sections() -> list[PrefSection]:
    """Global sections first (by declared order), then tool sections."""
    return sorted(GLOBAL_SECTIONS, key=lambda s: s.order) + tool_sections()


# ----------------------------------------------------------------------
# Lookup helpers
# ----------------------------------------------------------------------

def global_params() -> list[Param]:
    """Flat list of every app-wide preference param."""
    return [param for section in GLOBAL_SECTIONS for param in section.params]


def schema_defaults() -> dict[str, Any]:
    """Default value of every app-wide preference, keyed by settings key."""
    return {param.key: param.default for param in global_params()}


_PARAM_INDEX: dict[str, Param] = {}


def find(key: str) -> Optional[Param]:
    """The global preference param for a settings key, if there is one."""
    if not _PARAM_INDEX:
        _PARAM_INDEX.update({param.key: param for param in global_params()})
    return _PARAM_INDEX.get(key)


def default_for(key: str, fallback: Any = None) -> Any:
    """Default value for a settings key, falling back when it is not a pref."""
    param = find(key)
    return param.default if param is not None else fallback


def search(query: str) -> list[tuple[PrefSection, Param]]:
    """Fuzzy-ish preference search for the settings page search box."""
    needle = query.strip().lower()
    if not needle:
        return []
    hits: list[tuple[PrefSection, Param]] = []
    for section in all_sections():
        for param in section.params:
            haystack = f"{param.label} {param.help} {param.key} {section.title}"
            if needle in haystack.lower():
                hits.append((section, param))
    return hits
