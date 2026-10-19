"""Shared filename sanitizer schema and metadata."""
from __future__ import annotations

from s2kit.core.registry import tool
from s2kit.core.tool_definition import ToolDefinition
from s2kit.core.params import BoolParam, ChoiceParam, PathParam, TextParam, options

ILLEGAL = r'<>:"|?*\x00-\x1f'


@tool(
    id="filename-sanitizer",
    title="Filename Sanitizer",
    category="Utility",
    icon="DRIVE_FILE_RENAME_OUTLINE",
    description="Make filenames safe for Source: lower-case, no spaces, no "
                "illegal characters.",
    keywords=("rename", "sanitise", "sanitize", "lowercase", "spaces", "vpk"),
)
class FilenameSanitizer(ToolDefinition):
    """Shared declaration for FilenameSanitizer."""
    EXECUTOR = "s2kit.tools.filename_sanitizer:FilenameSanitizer"
    CUSTOM_VALIDATION = False
    RUN_LABEL = "Sanitize"
    RUN_ICON = "AUTO_FIX_HIGH"
    PARAMS = [
        PathParam("folder", "Folder", mode="directory", required=True,
                  group="Target"),
        BoolParam("recursive", "Include subfolders", default=True,
                  group="Target"),
        TextParam("extensions", "Only these extensions", default="",
                  group="Target", hint="vmt, vtf, smd  (empty = every file)",
                  help="Comma separated, with or without the leading dot."),
        BoolParam("lowercase", "Lower-case everything", default=True,
                  group="Rules"),
        ChoiceParam("spaces", "Spaces become", default="underscore",
                    group="Rules", style="segmented",
                    choices=options(("underscore", "_"), ("dash", "-"),
                                    ("remove", "removed"))),
        BoolParam("strip_illegal", "Remove illegal characters", default=True,
                  group="Rules",
                  help=f"Anything in {ILLEGAL!r} plus control characters."),
        BoolParam("collapse", "Collapse repeated separators", default=True,
                  group="Rules"),
        BoolParam("ascii_only", "Transliterate to ASCII", default=False,
                  group="Rules", advanced=True,
                  help="Drops accents and any character outside ASCII."),
        BoolParam("dry_run", "Dry run (report only, rename nothing)",
                  default=True, group="Safety"),
    ]
    PREFERENCES = [
        BoolParam("skip_hidden", "Skip hidden files", default=True,
                  group="Behaviour"),
        BoolParam("rename_folders", "Also rename folders", default=False,
                  group="Behaviour",
                  help="Folders are renamed deepest-first so paths stay valid."),
        ChoiceParam("on_collision", "If the target name exists",
                    default="skip", group="Behaviour",
                    choices=options(("skip", "Skip the file"),
                                    ("suffix", "Add _1, _2, ..."))),
    ]
    HELP = """
### Filename Sanitizer

Source cannot address every filename Windows allows. Materials with spaces,
mixed case or punctuation are a routine cause of missing textures after a port,
because the engine lower-cases lookups and the VPK packer does not.

Rules applied, in order:

1. transliterate to ASCII (optional)
2. remove illegal characters
3. replace spaces
4. collapse repeated separators
5. lower-case

**Dry run is on by default.** Read the table, then turn it off and run again.
Collisions are skipped rather than overwritten unless you change that in the
tool's Settings tab.
"""
