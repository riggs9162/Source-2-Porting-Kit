"""
Search & Replace.

The bulk renamer: one search term applied to file names, folder names and file
contents across a tree. v2 split this across two tools - one for files, one for
folders - which meant renaming `hlvr_` to `hla_` throughout a port took two runs
with two blacklists and two undo buttons that each only knew about half of it.

Defaults to a dry run, and every run that changes anything writes an undo
journal, so the destructive version of the operation is always one step behind a
readable one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from s2kit.core.jobs import JobContext
from s2kit.core.params import (BoolParam, ChoiceParam, IntParam, PathParam,
                               TextParam, options)
from s2kit.core.registry import tool
from s2kit.pipelines import text_replace as tr
from s2kit.ui.tool_page import ToolPage

#: Everything except the action switch itself is only relevant to a replace.
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
class SearchReplace(ToolPage):
    """One matcher, three kinds of target, one undo journal."""

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

    # ------------------------------------------------------------------

    def validate(self, values: dict[str, Any]) -> dict[str, str]:
        if str(values.get("action")) != "replace":
            return {}
        errors: dict[str, str] = {}
        if not any(bool(values.get(key)) for key in
                   ("rename_files", "rename_folders", "edit_contents")):
            errors["rename_files"] = ("Pick at least one thing to apply the "
                                      "replacement to")
        try:
            tr.build_matcher(str(values.get("search") or ""),
                             str(values.get("replace") or ""),
                             regex=str(values.get("mode")) == "regex")
        except ValueError as exc:
            errors["search"] = str(exc)
        return errors

    def run_job(self, ctx: JobContext) -> None:
        if str(ctx.params["action"]) == "undo":
            self._undo(ctx)
        else:
            self._replace(ctx)

    # ------------------------------------------------------------------
    # Replace
    # ------------------------------------------------------------------

    def _replace(self, ctx: JobContext) -> None:
        folder = Path(str(ctx.params["folder"]))
        if not folder.is_dir():
            raise ValueError(f"Not a folder: {folder}")

        dry_run = bool(ctx.params["dry_run"])
        matcher = tr.build_matcher(
            str(ctx.params["search"]), str(ctx.params["replace"] or ""),
            regex=str(ctx.params["mode"]) == "regex",
            case_sensitive=bool(ctx.params["case_sensitive"]))

        selection = tr.Selection(
            root=folder,
            recursive=bool(ctx.params["recursive"]),
            extensions=tr.parse_extensions(str(ctx.params["extensions"] or "")),
            include=tr.parse_patterns(str(ctx.params["include"] or "")),
            exclude=tr.parse_patterns(str(ctx.params["exclude"] or "")),
            skip_hidden=bool(ctx.prefs.get("skip_hidden", True)),
        )

        targets = [name for name, key in ((tr.CONTENT, "edit_contents"),
                                          (tr.FILE, "rename_files"),
                                          (tr.FOLDER, "rename_folders"))
                   if bool(ctx.params[key])]

        ctx.info(f"{'Previewing' if dry_run else 'Replacing'} in {folder}")
        ctx.info(f"  {ctx.params['search']!r}  ->  "
                 f"{str(ctx.params['replace'] or '')!r}"
                 f"   ({', '.join(targets)})")
        if selection.exclude:
            ctx.debug(f"  skipping {', '.join(selection.exclude)}")

        keep = int(ctx.prefs.get("journal_keep", 5))
        journal = (tr.Journal.create(folder)
                   if not dry_run and keep > 0 else None)

        outcome = tr.run(
            selection, matcher, targets=targets, dry_run=dry_run,
            on_collision=str(ctx.prefs.get("on_collision", "skip")),
            backup=bool(ctx.prefs.get("backup_contents", False)),
            max_bytes=int(ctx.prefs.get("max_size_mb", 16)) << 20,
            latin1_fallback=bool(ctx.prefs.get("latin1_fallback", False)),
            journal=journal, on_log=ctx.log, on_progress=ctx.progress,
            cancel=ctx.cancel_event)

        if journal is not None:
            if journal.entries:
                tr.prune_journals(keep)
            else:
                journal.discard()

        if outcome.truncated:
            ctx.warn(f"Stopped after {tr.WALK_LIMIT:,} paths - narrow the "
                     f"folder or add a filter.")

        self._report(ctx, outcome, dry_run)

    def _report(self, ctx: JobContext, outcome: tr.Outcome,
                dry_run: bool) -> None:
        labels = (("Would rename files", "Would rename folders",
                   "Would edit contents") if dry_run else
                  ("Renamed files", "Renamed folders", "Edited contents"))
        for label, value in zip(labels, (outcome.files_renamed,
                                         outcome.folders_renamed,
                                         outcome.files_edited)):
            ctx.metric(label, value, tone="good" if value else "neutral")
        ctx.metric("Replacements", outcome.replacements)
        ctx.metric("Files scanned", outcome.scanned)
        if outcome.skipped:
            ctx.metric("Skipped", outcome.skipped, tone="warn")
        if outcome.failed:
            ctx.metric("Failed", outcome.failed, tone="bad")

        if outcome.changes:
            ctx.table(
                "Changes",
                ["What", "Name", "Becomes", "Hits", "Result", "Where"],
                [[change.kind, change.path.name,
                  "" if change.kind == tr.CONTENT else change.target.name,
                  change.hits, change.status, change.note]
                 for change in outcome.changes],
                numeric_columns=(3,), sort_column=0,
            )

        message = outcome.summary(dry_run)
        if not outcome.total:
            ctx.warn("Nothing matched.")
        elif dry_run:
            ctx.warn(f"Dry run: {message}. Turn it off to apply.")
        else:
            ctx.success(message)
            if outcome.journal is not None and outcome.journal.entries:
                ctx.info("Undo journal written - switch Action to 'Undo last "
                         "run' to reverse this.")
        ctx.summary(message)

    # ------------------------------------------------------------------
    # Undo
    # ------------------------------------------------------------------

    def _undo(self, ctx: JobContext) -> None:
        journal = tr.latest_journal()
        if journal is None:
            ctx.warn("No undo journal on disk - nothing to reverse.")
            ctx.summary("Nothing to undo")
            return

        counts = journal.counts()
        ctx.info(f"Undoing the run from {journal.when}")
        if journal.root:
            ctx.info(f"  in {journal.root}")
        if journal.summary:
            ctx.info(f"  it {journal.summary}")
        ctx.progress(0, len(journal.entries))

        restored, failed = journal.undo(on_log=ctx.log,
                                        cancel=ctx.cancel_event)
        ctx.progress(len(journal.entries), len(journal.entries))

        ctx.metric("Restored", restored, tone="good" if restored else "neutral")
        if failed:
            ctx.metric("Could not restore", failed, tone="bad")
        ctx.metric("File names", counts[tr.FILE])
        ctx.metric("Folder names", counts[tr.FOLDER])
        ctx.metric("File contents", counts[tr.CONTENT])

        if not failed:
            # A fully reversed run is not a run any more; leaving the journal
            # in place invites undoing the same thing twice.
            journal.discard()

        message = f"restored {restored} change(s)"
        if failed:
            message += f", {failed} could not be restored"
            ctx.warn(message + " - the journal is kept so you can retry")
        else:
            ctx.success(message)
        ctx.summary(message)
