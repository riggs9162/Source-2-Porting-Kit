"""
Filename Sanitizer.

Renames files so studiomdl, the VPK packer and Source's filesystem stop
choking on them: lower-cases, strips characters Source cannot address, and
collapses whitespace. Defaults to a dry run because renaming is the kind of
thing you want to read before you do.
"""

from __future__ import annotations

from s2kit.definitions.filename_sanitizer import ILLEGAL

import re
from pathlib import Path

from s2kit.core.jobs import JobContext
from s2kit.media import probe
from s2kit.definitions.filename_sanitizer import FilenameSanitizer as _FilenameSanitizerDefinition

#: Characters Source paths cannot carry, plus the Windows-illegal set.
COLLAPSE_WHITESPACE = re.compile(r"\s+")


class FilenameSanitizer(_FilenameSanitizerDefinition):
    """Bulk rename with a mandatory preview step."""


    def run_job(self, ctx: JobContext) -> None:
        folder = Path(str(ctx.params["folder"]))
        if not folder.is_dir():
            raise ValueError(f"Not a folder: {folder}")

        dry_run = bool(ctx.params["dry_run"])
        extensions = _parse_extensions(str(ctx.params["extensions"] or ""))
        skip_hidden = bool(ctx.prefs.get("skip_hidden", True))
        collision = str(ctx.prefs.get("on_collision", "skip"))

        ctx.info(f"Scanning {folder}"
                 + (f" for {', '.join(sorted(extensions))}" if extensions else ""))
        files = probe.find_files(folder, extensions=extensions or None,
                                recursive=bool(ctx.params["recursive"]))
        if skip_hidden:
            files = [path for path in files if not path.name.startswith(".")]
        if not files:
            ctx.warn("No matching files.")
            ctx.summary("Nothing to rename")
            return

        ctx.info(f"{len(files):,} candidate file(s)"
                 + ("  [dry run]" if dry_run else ""))

        rows: list[list[object]] = []
        renamed = unchanged = skipped = failed = 0

        for index, path in enumerate(files, start=1):
            ctx.raise_if_cancelled()
            ctx.progress(index, len(files))

            clean = self._sanitize(path.name, ctx)
            if clean == path.name:
                unchanged += 1
                continue

            target = path.with_name(clean)
            if target.exists() and target != path:
                if collision == "skip":
                    skipped += 1
                    ctx.warn(f"{path.name}: '{clean}' already exists, skipped")
                    rows.append([path.name, clean, "skipped", str(path.parent)])
                    continue
                target = _unique(target)
                clean = target.name

            if dry_run:
                renamed += 1
                rows.append([path.name, clean, "would rename", str(path.parent)])
                ctx.info(f"{path.name}  ->  {clean}")
                continue

            try:
                path.rename(target)
            except OSError as exc:
                failed += 1
                ctx.error(f"{path.name}: {exc}")
                rows.append([path.name, clean, "failed", str(exc)[:70]])
                continue

            renamed += 1
            rows.append([path.name, clean, "renamed", str(path.parent)])
            ctx.debug(f"{path.name} -> {clean}")

        if bool(ctx.prefs.get("rename_folders", False)) and not dry_run:
            renamed += self._rename_folders(folder, ctx, rows)

        ctx.metric("Renamed" if not dry_run else "Would rename", renamed,
                   tone="good" if renamed else "neutral")
        ctx.metric("Already clean", unchanged)
        if skipped:
            ctx.metric("Skipped", skipped, tone="warn")
        if failed:
            ctx.metric("Failed", failed, tone="bad")

        if rows:
            ctx.table("Renames", ["Current name", "New name", "Result", "Detail"],
                      rows, sort_column=0)
        else:
            ctx.success("Every filename is already Source-safe.")

        verb = "would rename" if dry_run else "renamed"
        message = f"{verb} {renamed}, {unchanged} already clean"
        if skipped:
            message += f", {skipped} skipped"
        if failed:
            message += f", {failed} failed"
        (ctx.warn if dry_run and renamed else ctx.success)(message)
        if dry_run and renamed:
            ctx.warn("Dry run - nothing was changed. Turn it off to apply.")
        ctx.summary(message)

    # ------------------------------------------------------------------
    # Rules
    # ------------------------------------------------------------------

    def _sanitize(self, name: str, ctx: JobContext) -> str:
        stem, dot, suffix = name.rpartition(".")
        if not dot:                     # no extension
            stem, suffix = name, ""
        result = stem

        if bool(ctx.params["ascii_only"]):
            import unicodedata

            result = (unicodedata.normalize("NFKD", result)
                      .encode("ascii", "ignore").decode("ascii"))
        if bool(ctx.params["strip_illegal"]):
            result = re.sub(f"[{ILLEGAL}/\\\\]", "", result)
        separator = {"underscore": "_", "dash": "-", "remove": ""}[
            str(ctx.params["spaces"])]
        result = COLLAPSE_WHITESPACE.sub(separator, result.strip())
        if bool(ctx.params["collapse"]):
            result = re.sub(r"_{2,}", "_", result)
            result = re.sub(r"-{2,}", "-", result)
            result = result.strip("._-")
        if bool(ctx.params["lowercase"]):
            result = result.lower()
            suffix = suffix.lower()
        if not result:
            result = "unnamed"
        return f"{result}.{suffix}" if suffix else result

    def _rename_folders(self, root: Path, ctx: JobContext,
                        rows: list[list[object]]) -> int:
        """Rename folders deepest-first so parent paths stay valid."""
        folders = sorted((p for p in root.rglob("*") if p.is_dir()),
                         key=lambda p: len(p.parts), reverse=True)
        renamed = 0
        for folder in folders:
            ctx.raise_if_cancelled()
            clean = self._sanitize(folder.name, ctx)
            if clean == folder.name:
                continue
            target = folder.with_name(clean)
            if target.exists():
                rows.append([folder.name + "/", clean + "/", "skipped",
                             "target exists"])
                continue
            try:
                folder.rename(target)
                renamed += 1
                rows.append([folder.name + "/", clean + "/", "renamed",
                             str(folder.parent)])
            except OSError as exc:
                ctx.error(f"{folder.name}/: {exc}")
                rows.append([folder.name + "/", clean + "/", "failed",
                             str(exc)[:70]])
        return renamed


def _parse_extensions(raw: str) -> set[str]:
    parts = [part.strip().lower() for part in raw.replace(";", ",").split(",")]
    return {part if part.startswith(".") else f".{part}"
            for part in parts if part}


def _unique(target: Path) -> Path:
    index = 1
    candidate = target
    while candidate.exists():
        candidate = target.with_name(f"{target.stem}_{index}{target.suffix}")
        index += 1
    return candidate
