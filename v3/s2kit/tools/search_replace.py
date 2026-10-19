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

from s2kit.definitions.search_replace import WHEN_REPLACE

from pathlib import Path
from typing import Any

from s2kit.core.jobs import JobContext
from s2kit.pipelines import text_replace as tr
from s2kit.definitions.search_replace import SearchReplace as _SearchReplaceDefinition

#: Everything except the action switch itself is only relevant to a replace.


class SearchReplace(_SearchReplaceDefinition):
    """One matcher, three kinds of target, one undo journal."""


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
