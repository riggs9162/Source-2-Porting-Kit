"""Shared search replace schema and metadata."""
from __future__ import annotations

from s2kit.core.registry import tool
from s2kit.core.tool_definition import ToolDefinition
from s2kit.core.params import BoolParam, ChoiceParam, IntParam, PathParam, TextParam, options

WHEN_REPLACE = ("action", "replace")


@tool(
    id="search-replace",
    title="Search & Replace",
    category="Utility",
    icon="FIND_REPLACE",
    description="Rename files and folders, and rewrite what is inside them, "
                "across a whole tree.",
    keywords=("rename", "replace", "search", "bulk", "batch", "regex",
              "find", "substitute", "refactor"),
)
class SearchReplace(ToolDefinition):
    """Shared declaration for SearchReplace."""
    EXECUTOR = "s2kit.tools.search_replace:SearchReplace"
    CUSTOM_VALIDATION = True
    RUN_LABEL = "Run"
    RUN_ICON = "FIND_REPLACE"
    PARAMS = [
        ChoiceParam("action", "Action", default="replace", group="Action",
                    style="segmented",
                    choices=options(("replace", "Search & replace"),
                                    ("undo", "Undo last run"))),

        PathParam("folder", "Folder", mode="directory", required=True,
                  group="Target", depends_on=WHEN_REPLACE),
        BoolParam("recursive", "Include subfolders", default=True,
                  group="Target", depends_on=WHEN_REPLACE),
        TextParam("extensions", "Only these extensions", default="",
                  group="Target", depends_on=WHEN_REPLACE,
                  hint="vmt, vtf, qc  (empty = every file)",
                  help="Comma separated, with or without the leading dot. "
                       "Applies to files; folders are matched by name."),

        TextParam("search", "Search for", required=True, group="Search",
                  depends_on=WHEN_REPLACE, hint="hlvr_"),
        TextParam("replace", "Replace with", default="", group="Search",
                  depends_on=WHEN_REPLACE, hint="hla_  (empty = delete it)"),
        ChoiceParam("mode", "Pattern", default="literal", group="Search",
                    style="segmented", depends_on=WHEN_REPLACE,
                    choices=options(("literal", "Plain text"),
                                    ("regex", "Regular expression"))),
        BoolParam("case_sensitive", "Case sensitive", default=True,
                  group="Search", depends_on=WHEN_REPLACE),

        BoolParam("rename_files", "Rename files", default=True,
                  group="Apply to", depends_on=WHEN_REPLACE),
        BoolParam("rename_folders", "Rename folders", default=False,
                  group="Apply to", depends_on=WHEN_REPLACE,
                  help="Renamed deepest-first, so a parent moving cannot "
                       "strand its children."),
        BoolParam("edit_contents", "Edit file contents", default=False,
                  group="Apply to", depends_on=WHEN_REPLACE,
                  help="Text files only. Binary files are detected and left "
                       "alone."),

        TextParam("exclude", "Skip anything matching", default="",
                  group="Filters", lines=3, depends_on=WHEN_REPLACE,
                  hint="*.bak\nbackup_*\n_temp",
                  help="One per line. A pattern with * ? or [ is a glob; "
                       "anything else matches as a substring. Matched against "
                       "every folder on the way down, so a match skips the "
                       "whole subtree."),
        TextParam("include", "Only files matching", default="",
                  group="Filters", lines=2, advanced=True,
                  depends_on=WHEN_REPLACE, hint="ref_*.smd",
                  help="One per line, same syntax. Empty means every file."),

        BoolParam("dry_run", "Dry run (report only, change nothing)",
                  default=True, group="Safety", depends_on=WHEN_REPLACE),
    ]
    PREFERENCES = [
        ChoiceParam("on_collision", "If the new name already exists",
                    default="skip", group="Behaviour",
                    choices=options(("skip", "Skip it"),
                                    ("suffix", "Add _1, _2, ..."))),
        BoolParam("skip_hidden", "Skip hidden files and folders", default=True,
                  group="Behaviour",
                  help="Anything whose name starts with a dot, at any depth."),
        BoolParam("backup_contents", "Write a .bak beside every edited file",
                  default=False, group="Behaviour",
                  help="Belt and braces - the undo journal already keeps the "
                       "original of every file that gets rewritten."),
        IntParam("max_size_mb", "Skip files larger than", default=16,
                 min=1, max=512, suffix=" MB", group="Behaviour",
                 help="Content editing only. A match in a 2 GB file is almost "
                      "always an accident."),
        BoolParam("latin1_fallback", "Read non-UTF-8 text as Latin-1",
                  default=False, group="Behaviour", advanced=True,
                  help="Old Source content is often cp1252. Latin-1 maps every "
                       "byte, so untouched regions still round-trip exactly."),
        IntParam("journal_keep", "Undo journals to keep", default=5,
                 min=0, max=50, group="Undo",
                 help="Older ones are deleted after each run. 0 disables undo."),
    ]
    HELP = """
### Search & Replace

One search term, three kinds of target:

* **File names** - `hlvr_headcrab.vmt` -> `hla_headcrab.vmt`
* **Folder names** - renamed deepest-first, so renaming a parent cannot strand
  the children still queued behind it
* **File contents** - text files only; anything with a NUL byte in the first
  block is binary and is left alone

The three run in that order in a single pass, which is the only ordering where
every path stays valid from one step to the next.

**Dry run is on by default.** Read the table on the Output tab, then turn it off
and run again.

#### Patterns

*Plain text* matches literally - a `.` is a dot, and a `\\1` in the replacement
is inserted as typed. *Regular expression* is full Python `re`, so groups work:
searching `^(.*)_diffuse$` and replacing with `\\1_color` renames by structure.
A pattern that can match the empty string is refused rather than applied.

The filter patterns are globs when they contain `*`, `?` or `[`, and
case-insensitive substrings otherwise - so `_temp` and `*_temp.vmt` both do what
they look like they do.

#### Undo

Every run that changes something writes a journal to the cache directory: the
before and after of each rename, and the original bytes of each rewritten file.
Switch **Action** to *Undo last run* to replay the newest one backwards. It
survives closing the tool and closing the app, and it refuses to overwrite a
name that has since come back rather than guessing.

Undo cannot help with a file that was deleted or moved out from under it after
the run; those are reported and skipped.
"""
