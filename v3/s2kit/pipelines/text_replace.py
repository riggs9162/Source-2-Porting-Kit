"""
Search and replace across a folder tree - names, folder names and contents.

v2 shipped this as two tools that shared 90% of their code and disagreed about
the other 10%: one renamed files and edited contents, the other renamed folders,
each with its own copy of a hand-rolled single-`*` blacklist matcher. They are
one operation on three kinds of target, so this is one engine with a target set.

Five things it does that v2's pair did not, each of which was a real way to lose
data:

* **Folders are renamed deepest-first.** v2 walked `rglob('*')` in top-down
  order and renamed as it went, so renaming a parent invalidated every child
  path still queued behind it and the rest of the subtree failed with ENOENT.
* **Contents are read and written as bytes.** `Path.write_text` translates `\\n`
  to `os.linesep`, so v2 silently rewrote every LF file in a tree to CRLF the
  moment one line matched. Decoding to text, replacing, and encoding back leaves
  every byte the replacement did not touch exactly as it was found - line
  endings, BOM and all.
* **Collisions are a decision, not a race.** `Path.rename` overwrites the target
  on POSIX and raises on Windows; neither is an answer. The caller picks skip or
  suffix.
* **Binary files are detected, not stumbled over.** A NUL byte in the first
  block means binary. Relying on `UnicodeDecodeError` (v2) passes any binary
  that happens to decode - which, for a latin-1 fallback, is all of them.
* **Undo survives a restart.** v2 held the undo data in the widget, so closing
  the tool - or the app - lost it. A run writes a journal to the cache
  directory with the original bytes of every file it edited.

Journals are replayed in reverse. That matters more than it looks: a file whose
contents were edited and which was then renamed - or which merely sits inside a
folder that was renamed afterwards - is recorded under the path it had at the
time. Undoing the later entries first puts the tree back into the state each
earlier entry was recorded against, so every path in the journal is valid again
by the time it is used.
"""

from __future__ import annotations

import fnmatch
import itertools
import json
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from s2kit.core import paths
from s2kit.pipelines.porting import LogFn, check_cancelled, log_stdout

#: The three kinds of target a run can touch, and the keys of a target set.
FILE = "file"
FOLDER = "folder"
CONTENT = "content"

#: Characters that make an exclude pattern a glob rather than a substring.
GLOB_CHARS = frozenset("*?[")

#: How much of a file is read to decide whether it is binary.
BINARY_SNIFF = 8192

#: Hard cap on how many paths a single run will look at, so a mistaken pick of
#: `C:\\` reports a limit instead of walking the whole disk.
WALK_LIMIT = 50_000

JOURNAL_VERSION = 1


# ----------------------------------------------------------------------
# Matching
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Matcher:
    """A compiled search, its replacement, and how to apply the two."""

    pattern: re.Pattern[str]
    replacement: str
    regex: bool

    def apply(self, text: str) -> tuple[str, int]:
        """The replaced text and how many replacements were made."""
        if self.regex:
            try:
                return self.pattern.subn(self.replacement, text)
            except re.error as exc:
                raise ValueError(f"Invalid replacement template: {exc}") from exc
        # Literal mode goes through a function so a replacement containing a
        # backslash or a \1 is inserted as typed rather than expanded.
        return self.pattern.subn(lambda _match: self.replacement, text)

    def hits(self, text: str) -> int:
        return sum(1 for _match in self.pattern.finditer(text))


def build_matcher(search: str, replace: str = "", *, regex: bool = False,
                  case_sensitive: bool = True) -> Matcher:
    """Compile a search/replace pair, refusing the ones that cannot end well.

    Raises :class:`ValueError` with a message meant for the user - a bad regex
    is an input error, not a bug.
    """
    if not search:
        raise ValueError("Nothing to search for")
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        compiled = re.compile(search if regex else re.escape(search), flags)
    except re.error as exc:
        raise ValueError(f"Invalid regular expression: {exc}") from exc
    # A pattern that matches nothing at all matches *between* every character,
    # so replacing it would rewrite every name in the tree. Never intended.
    if compiled.search("") is not None:
        raise ValueError("The pattern matches the empty string, which would "
                         "insert the replacement between every character")
    return Matcher(compiled, replace, regex)


# ----------------------------------------------------------------------
# Selection
# ----------------------------------------------------------------------

@dataclass
class Selection:
    """Which paths under `root` a run is allowed to touch."""

    root: Path
    recursive: bool = True
    extensions: frozenset[str] = frozenset()
    #: Only files whose name matches one of these are considered (files only).
    include: tuple[str, ...] = ()
    #: Any path with a component matching one of these is skipped entirely,
    #: which prunes whole subtrees rather than individual files.
    exclude: tuple[str, ...] = ()
    skip_hidden: bool = True
    truncated: bool = False
    _walked: Optional[list[Path]] = field(default=None, repr=False)

    def files(self) -> list[Path]:
        found = [path for path in self._walk()
                 if path.is_file() and self._extension_ok(path)
                 and self.allows(path) and self._included(path)]
        return sorted(found)

    def folders(self) -> list[Path]:
        """Every folder, deepest first - the only safe order to rename in."""
        found = [path for path in self._walk()
                 if path.is_dir() and self.allows(path)]
        return sorted(found, key=lambda path: (len(path.parts), str(path)),
                      reverse=True)

    def allows(self, path: Path) -> bool:
        """Whether `path` survives the hidden and exclude filters."""
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            return False
        parts = relative.parts
        if self.skip_hidden and any(part.startswith(".") for part in parts):
            return False
        if self.exclude and any(matches_any(part, self.exclude)
                                for part in parts):
            return False
        return True

    def _included(self, path: Path) -> bool:
        return not self.include or matches_any(path.name, self.include)

    def _extension_ok(self, path: Path) -> bool:
        return not self.extensions or path.suffix.lower() in self.extensions

    def _walk(self) -> list[Path]:
        """The tree, walked once and remembered - files() and folders() share it."""
        if self._walked is None:
            if not self.root.is_dir():
                self._walked = []
            else:
                pattern = "**/*" if self.recursive else "*"
                self._walked = list(
                    itertools.islice(self.root.glob(pattern), WALK_LIMIT))
                self.truncated = len(self._walked) >= WALK_LIMIT
        return self._walked


def matches_any(name: str, patterns: Sequence[str]) -> bool:
    """Glob match, or a case-insensitive substring test for a plain word.

    v2 accepted `soundscape_*` and `*.txt` but treated anything without a `*` as
    "contains", which is a genuinely useful shorthand nobody wants to type
    `*temp*` for. Both forms are kept; the glob form is now a real glob.
    """
    for pattern in patterns:
        pattern = pattern.strip()
        if not pattern:
            continue
        if GLOB_CHARS & set(pattern):
            if fnmatch.fnmatch(name, pattern):
                return True
        elif pattern.lower() in name.lower():
            return True
    return False


def parse_patterns(raw: str) -> tuple[str, ...]:
    """One pattern per line, blanks dropped."""
    return tuple(line.strip() for line in str(raw or "").splitlines()
                 if line.strip())


def parse_extensions(raw: str) -> frozenset[str]:
    """`vmt, .vtf; smd` -> `{'.vmt', '.vtf', '.smd'}`."""
    parts = [part.strip().lower()
             for part in str(raw or "").replace(";", ",").split(",")]
    return frozenset(part if part.startswith(".") else f".{part}"
                     for part in parts if part)


# ----------------------------------------------------------------------
# Results
# ----------------------------------------------------------------------

@dataclass
class Change:
    """One thing the run did, or would do in a dry run."""

    kind: str                   # file | folder | content
    path: Path
    target: Path
    hits: int = 0
    status: str = "would"       # would | done | skipped | failed
    note: str = ""


@dataclass
class Outcome:
    """What a whole run did. The tool turns this into metrics and a table."""

    changes: list[Change] = field(default_factory=list)
    files_renamed: int = 0
    folders_renamed: int = 0
    files_edited: int = 0
    replacements: int = 0
    scanned: int = 0
    skipped: int = 0
    failed: int = 0
    truncated: bool = False
    journal: Optional["Journal"] = None

    @property
    def total(self) -> int:
        return self.files_renamed + self.folders_renamed + self.files_edited

    def summary(self, dry_run: bool) -> str:
        verb = "would change" if dry_run else "changed"
        parts = []
        if self.files_renamed:
            parts.append(f"{self.files_renamed} file name(s)")
        if self.folders_renamed:
            parts.append(f"{self.folders_renamed} folder name(s)")
        if self.files_edited:
            parts.append(f"{self.files_edited} file(s) edited")
        if not parts:
            return "nothing matched"
        text = f"{verb} {', '.join(parts)} ({self.replacements} replacement(s))"
        if self.skipped:
            text += f", {self.skipped} skipped"
        if self.failed:
            text += f", {self.failed} failed"
        return text


# ----------------------------------------------------------------------
# The run
# ----------------------------------------------------------------------

def run(selection: Selection, matcher: Matcher, *,
        targets: Sequence[str] = (FILE,),
        dry_run: bool = True,
        on_collision: str = "skip",
        backup: bool = False,
        max_bytes: int = 16 << 20,
        latin1_fallback: bool = False,
        journal: Optional["Journal"] = None,
        on_log: LogFn = log_stdout,
        on_progress: Optional[Callable[[int, int], None]] = None,
        cancel: Optional[threading.Event] = None) -> Outcome:
    """Apply `matcher` to everything in `selection` that `targets` names.

    The three passes run in the one order that keeps every path valid: contents
    first (nothing has moved yet), then file names (within folders that still
    have their old names), then folder names deepest-first. Reversing that order
    is how v2's folder tool broke itself.
    """
    wanted = set(targets)
    outcome = Outcome(journal=journal)

    files = (selection.files()
             if wanted & {FILE, CONTENT} else [])
    folders = selection.folders() if FOLDER in wanted else []
    outcome.scanned = len(files)
    outcome.truncated = selection.truncated

    total = len(files) + len(folders)
    if not total:
        return outcome
    done = 0

    for path in files:
        check_cancelled(cancel)
        done += 1
        _tick(on_progress, done, total)

        if CONTENT in wanted:
            _edit_contents(path, matcher, outcome, dry_run=dry_run,
                           backup=backup, max_bytes=max_bytes,
                           latin1_fallback=latin1_fallback, journal=journal,
                           on_log=on_log)
        if FILE in wanted:
            _rename(path, FILE, matcher, outcome, dry_run=dry_run,
                    on_collision=on_collision, journal=journal, on_log=on_log)

    for folder in folders:
        check_cancelled(cancel)
        done += 1
        _tick(on_progress, done, total)
        _rename(folder, FOLDER, matcher, outcome, dry_run=dry_run,
                on_collision=on_collision, journal=journal, on_log=on_log)

    if journal is not None and not dry_run and journal.entries:
        journal.summary = outcome.summary(dry_run=False)
        journal.save()
    return outcome


def _rename(path: Path, kind: str, matcher: Matcher, outcome: Outcome, *,
            dry_run: bool, on_collision: str, journal: Optional["Journal"],
            on_log: LogFn) -> Optional[Path]:
    """Rename one file or folder. Returns the new path, or None if unchanged."""
    new_name, hits = matcher.apply(path.name)
    if not hits or new_name == path.name:
        return None

    label = "folder" if kind == FOLDER else "file"
    if not new_name.strip():
        outcome.skipped += 1
        outcome.changes.append(Change(kind, path, path, hits, "skipped",
                                      "the replacement empties the name"))
        on_log(f"{path.name}: the replacement empties the name, skipped",
               "WARNING")
        return None

    target = path.with_name(new_name)
    # `is_same` and not `!=`: on Windows a case-only rename - FOO.VMT to
    # foo.vmt, which is most of what this tool gets pointed at - has a target
    # that "exists" because it is the file itself.
    if target.exists() and not is_same_path(target, path):
        if on_collision != "suffix":
            outcome.skipped += 1
            outcome.changes.append(Change(kind, path, target, hits, "skipped",
                                          "target name already exists"))
            on_log(f"{path.name}: '{new_name}' already exists, skipped",
                   "WARNING")
            return None
        target = unique_name(target)

    if dry_run:
        _count_rename(outcome, kind, hits)
        outcome.changes.append(Change(kind, path, target, hits, "would",
                                      str(path.parent)))
        on_log(f"{label}: {path.name}  ->  {target.name}")
        return None

    try:
        path.rename(target)
    except OSError as exc:
        outcome.failed += 1
        outcome.changes.append(Change(kind, path, target, hits, "failed",
                                      str(exc)[:90]))
        on_log(f"{path.name}: {exc}", "ERROR")
        return None

    if journal is not None:
        journal.record_rename(kind, path, target)
    _count_rename(outcome, kind, hits)
    outcome.changes.append(Change(kind, path, target, hits, "done",
                                  str(path.parent)))
    on_log(f"{label}: {path.name}  ->  {target.name}")
    return target


def _count_rename(outcome: Outcome, kind: str, hits: int) -> None:
    if kind == FOLDER:
        outcome.folders_renamed += 1
    else:
        outcome.files_renamed += 1
    outcome.replacements += hits


def _edit_contents(path: Path, matcher: Matcher, outcome: Outcome, *,
                   dry_run: bool, backup: bool, max_bytes: int,
                   latin1_fallback: bool, journal: Optional["Journal"],
                   on_log: LogFn) -> None:
    """Replace inside one file, preserving every byte it did not match."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        outcome.failed += 1
        on_log(f"{path.name}: {exc}", "ERROR")
        return
    if size > max_bytes:
        outcome.skipped += 1
        outcome.changes.append(Change(CONTENT, path, path, 0, "skipped",
                                      f"{size / (1 << 20):.1f} MB, over the "
                                      f"size limit"))
        return

    try:
        raw = path.read_bytes()
    except OSError as exc:
        outcome.failed += 1
        on_log(f"{path.name}: {exc}", "ERROR")
        return
    if b"\x00" in raw[:BINARY_SNIFF]:
        return                                  # binary, silently left alone

    text, encoding = _decode(raw, latin1_fallback)
    if text is None:
        return

    try:
        replaced, hits = matcher.apply(text)
    except ValueError as exc:
        outcome.failed += 1
        on_log(f"{path.name}: {exc}", "ERROR")
        return
    if not hits:
        return

    if dry_run:
        outcome.files_edited += 1
        outcome.replacements += hits
        outcome.changes.append(Change(CONTENT, path, path, hits, "would",
                                      str(path.parent)))
        on_log(f"contents: {path.name}  ({hits} replacement(s))")
        return

    if backup:
        try:
            shutil.copy2(path, path.with_name(path.name + ".bak"))
        except OSError as exc:
            outcome.failed += 1
            on_log(f"{path.name}: could not write a backup ({exc})", "ERROR")
            return
    if journal is not None:
        journal.record_content(path, raw)

    try:
        path.write_bytes(replaced.encode(encoding))
    except OSError as exc:
        outcome.failed += 1
        outcome.changes.append(Change(CONTENT, path, path, hits, "failed",
                                      str(exc)[:90]))
        on_log(f"{path.name}: {exc}", "ERROR")
        return

    outcome.files_edited += 1
    outcome.replacements += hits
    outcome.changes.append(Change(CONTENT, path, path, hits, "done",
                                  str(path.parent)))
    on_log(f"contents: {path.name}  ({hits} replacement(s))")


def _decode(raw: bytes, latin1_fallback: bool) -> tuple[Optional[str], str]:
    """Decode text so that re-encoding it reproduces the original bytes.

    UTF-8 first, which round-trips a BOM as a leading `\\ufeff` without any
    special handling. Latin-1 is offered as a fallback because it is the one
    single-byte codec that maps every byte, so an encode of an untouched region
    is guaranteed to give the bytes back.
    """
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        if latin1_fallback:
            return raw.decode("latin-1"), "latin-1"
    return None, ""


def is_same_path(one: Path, two: Path) -> bool:
    """Whether two paths name the same entry, case-insensitive filesystems too."""
    try:
        return one.samefile(two)
    except OSError:
        return os.path.normcase(str(one)) == os.path.normcase(str(two))


def unique_name(target: Path) -> Path:
    """`foo.vmt` -> `foo_1.vmt` -> `foo_2.vmt`, until one is free."""
    index = 1
    candidate = target
    while candidate.exists():
        candidate = target.with_name(f"{target.stem}_{index}{target.suffix}")
        index += 1
    return candidate


def _tick(on_progress: Optional[Callable[[int, int], None]],
          done: int, total: int) -> None:
    if on_progress is not None:
        on_progress(done, total)


# ----------------------------------------------------------------------
# Undo journal
# ----------------------------------------------------------------------

def journal_root() -> Path:
    """Where run journals live. Under the cache, because they are disposable."""
    directory = paths.cache_dir() / "search-replace"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@dataclass
class Journal:
    """An on-disk record of one run, enough to put the tree back.

    Renames are stored as a path pair. Contents are stored as the original
    bytes in a numbered blob beside the journal, because a rewritten file has no
    other copy of what it used to say.
    """

    directory: Path
    root: str = ""
    created: float = 0.0
    summary: str = ""
    entries: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def create(cls, root: Path) -> "Journal":
        stamp = time.strftime("%Y%m%d-%H%M%S")
        directory = journal_root() / stamp
        suffix = 1
        while directory.exists():
            directory = journal_root() / f"{stamp}-{suffix}"
            suffix += 1
        directory.mkdir(parents=True)
        return cls(directory=directory, root=str(root), created=time.time())

    @classmethod
    def load(cls, directory: Path) -> "Journal":
        data = json.loads((Path(directory) / "journal.json")
                          .read_text(encoding="utf-8"))
        return cls(directory=Path(directory), root=data.get("root", ""),
                   created=float(data.get("created", 0.0)),
                   summary=data.get("summary", ""),
                   entries=list(data.get("entries", [])))

    # -- recording -----------------------------------------------------

    def record_rename(self, kind: str, before: Path, after: Path) -> None:
        self.entries.append({"kind": kind, "from": str(before),
                             "to": str(after)})

    def record_content(self, path: Path, original: bytes) -> None:
        blobs = self.directory / "blobs"
        blobs.mkdir(exist_ok=True)
        name = f"{len(self.entries):05d}.bin"
        (blobs / name).write_bytes(original)
        self.entries.append({"kind": CONTENT, "path": str(path), "blob": name})

    def save(self) -> Path:
        target = self.directory / "journal.json"
        target.write_text(json.dumps({
            "version": JOURNAL_VERSION,
            "created": self.created,
            "root": self.root,
            "summary": self.summary,
            "entries": self.entries,
        }, indent=1), encoding="utf-8")
        return target

    def discard(self) -> None:
        """Throw the journal away - a run that changed nothing leaves none."""
        shutil.rmtree(self.directory, ignore_errors=True)

    # -- replay --------------------------------------------------------

    @property
    def when(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.created))

    def counts(self) -> dict[str, int]:
        result = {FILE: 0, FOLDER: 0, CONTENT: 0}
        for entry in self.entries:
            kind = entry.get("kind", "")
            if kind in result:
                result[kind] += 1
        return result

    def undo(self, *, on_log: LogFn = log_stdout,
             cancel: Optional[threading.Event] = None) -> tuple[int, int]:
        """Reverse the run. Returns `(restored, failed)`.

        Entries are replayed newest-first so each one's recorded paths are valid
        again by the time it is reached - see the module docstring.
        """
        restored = failed = 0
        for entry in reversed(self.entries):
            check_cancelled(cancel)
            kind = entry.get("kind", "")
            try:
                if kind == CONTENT:
                    target = Path(entry["path"])
                    blob = self.directory / "blobs" / entry["blob"]
                    if not blob.is_file():
                        on_log(f"{target.name}: the saved copy is gone",
                               "ERROR")
                        failed += 1
                        continue
                    target.write_bytes(blob.read_bytes())
                    on_log(f"contents: restored {target.name}")
                else:
                    before, after = Path(entry["from"]), Path(entry["to"])
                    if not after.exists():
                        on_log(f"{after.name}: no longer there, cannot undo",
                               "WARNING")
                        failed += 1
                        continue
                    if before.exists():
                        on_log(f"{before.name}: exists again, not overwriting",
                               "WARNING")
                        failed += 1
                        continue
                    after.rename(before)
                    on_log(f"name: {after.name}  ->  {before.name}")
                restored += 1
            except (OSError, KeyError) as exc:
                failed += 1
                on_log(f"{entry.get('to') or entry.get('path')}: {exc}",
                       "ERROR")
        return restored, failed


def journals(newest_first: bool = True) -> list[Journal]:
    """Every readable journal on disk."""
    found: list[Journal] = []
    for directory in journal_root().iterdir():
        if not (directory / "journal.json").is_file():
            continue
        try:
            found.append(Journal.load(directory))
        except (OSError, ValueError):
            continue
    found.sort(key=lambda entry: entry.created, reverse=newest_first)
    return found


def latest_journal() -> Optional[Journal]:
    found = journals()
    return found[0] if found else None


def prune_journals(keep: int) -> int:
    """Delete all but the `keep` newest journals. Returns how many went."""
    if keep < 0:
        return 0
    removed = 0
    for stale in journals()[keep:]:
        shutil.rmtree(stale.directory, ignore_errors=True)
        removed += 1
    return removed
