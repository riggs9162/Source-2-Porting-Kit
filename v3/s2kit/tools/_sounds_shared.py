"""Shared input filter fields for folder-based sound tools."""

from s2kit.core.params import TextParam


def sound_filter_params(*, quad: bool = False) -> list[TextParam]:
    """Create independent filter schemas, with set-level matching guidance for quad sounds."""
    target = "set names (without _l/_ls/_r/_rs or extensions)" if quad else "filenames"
    return [
        TextParam("include", "Only sounds matching", default="", group="Filters",
                  lines=2,
                  help=f"Match {target} or relative paths, ignoring case. "
                       "Use words or globs such as ambient/*; separate patterns "
                       "with commas, semicolons or new lines. Empty includes all."),
        TextParam("exclude", "Skip sounds matching", default="", group="Filters",
                  lines=2,
                  help="Same pattern syntax as above. Exclusions take priority."),
        TextParam("input_extensions", "Input extensions", default="", group="Filters",
                  hint="wav, ogg, mp3",
                  help="Comma-, semicolon- or space-separated source extensions. "
                       "Empty includes every supported format; this does not "
                       "change the output format."),
    ]


def sound_filter_values(params: dict) -> dict[str, str]:
    """Read filters with empty defaults for older presets and history entries."""
    return {key: str(params.get(key, "") or "")
            for key in ("include", "exclude", "input_extensions")}