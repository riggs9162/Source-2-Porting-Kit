"""
Per-tool run history.

Every finished run becomes a :class:`RunRecord` written to
`config/history/<tool_id>.json`. A record carries enough to be useful later:
the parameter values (so the form can be restored or the run repeated), the
outcome, the metrics, the artefact paths and - optionally - the full log.

Records are capped by the `history_limit` preference; the store is written
newest-first so truncation is a slice.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from s2kit.core import paths
from s2kit.core.jobs import JobHandle, LogLine

logger = logging.getLogger(__name__)


@dataclass
class RunRecord:
    """One archived run of one tool."""

    run_id: str
    tool_id: str
    started_at: float
    finished_at: float
    status: str
    params: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    error: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    log: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def started_text(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started_at))

    def duration_text(self) -> str:
        seconds = self.duration
        if seconds < 1:
            return f"{seconds * 1000:.0f} ms"
        if seconds < 60:
            return f"{seconds:.1f} s"
        minutes, rest = divmod(int(seconds), 60)
        return f"{minutes}m {rest:02d}s"

    def log_lines(self) -> list[LogLine]:
        return [LogLine(entry.get("t", 0.0), entry.get("l", "INFO"),
                        entry.get("m", ""))
                for entry in self.log]

    def as_text(self) -> str:
        """Plain-text export of the whole record, for the clipboard or a file."""
        head = [
            f"Tool      {self.tool_id}",
            f"Run       {self.run_id}",
            f"Started   {self.started_text()}",
            f"Duration  {self.duration_text()}",
            f"Status    {self.status}",
            f"Summary   {self.summary}",
        ]
        if self.error:
            head.append(f"Error     {self.error}")
        head.append("")
        head.append("Parameters")
        head.extend(f"  {key} = {value!r}" for key, value in sorted(self.params.items()))
        if self.artifacts:
            head.append("")
            head.append("Artifacts")
            head.extend(f"  {path}" for path in self.artifacts)
        if self.log:
            head.append("")
            head.append("Log")
            head.extend("  " + line.formatted() for line in self.log_lines())
        return "\n".join(head)


def record_from_handle(handle: JobHandle, *,
                       log: Optional[list[LogLine]] = None,
                       metrics: Optional[dict[str, Any]] = None,
                       counts: Optional[dict[str, int]] = None,
                       store_log: bool = True,
                       log_limit: int = 2000) -> RunRecord:
    """Build a history record from a finished job handle."""
    lines = log or []
    if store_log and log_limit and len(lines) > log_limit:
        # Keep the head and the tail: the start explains the inputs, the tail
        # explains the outcome, and the middle of a 50k-line run is noise.
        head = lines[: log_limit // 2]
        tail = lines[-(log_limit // 2):]
        lines = head + [LogLine(time.time(), "INFO",
                                f"... {len(log or []) - len(head) - len(tail)} "
                                "lines trimmed ...")] + tail
    return RunRecord(
        run_id=handle.job_id,
        tool_id=handle.tool_id,
        started_at=handle.started_at,
        finished_at=handle.finished_at or time.time(),
        status=handle.status,
        params=dict(handle.params),
        summary=handle.summary,
        error=handle.error,
        metrics=dict(metrics or {}),
        artifacts=[str(item.path) for item in handle.artifacts],
        log=([{"t": line.timestamp, "l": line.level, "m": line.message}
              for line in lines] if store_log else []),
        counts=dict(counts or {}),
    )


class HistoryStore:
    """JSON-file-per-tool history with an in-memory cache."""

    def __init__(self, directory: Optional[Path] = None, limit: int = 50):
        self.directory = Path(directory) if directory else paths.history_dir()
        self.limit = limit
        self._cache: dict[str, list[RunRecord]] = {}

    def _file(self, tool_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in tool_id)
        return self.directory / f"{safe}.json"

    def list(self, tool_id: str) -> list[RunRecord]:
        """Records for a tool, newest first."""
        if tool_id in self._cache:
            return self._cache[tool_id]
        records: list[RunRecord] = []
        path = self._file(tool_id)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            for entry in data if isinstance(data, list) else []:
                try:
                    records.append(RunRecord(**entry))
                except TypeError:
                    # A record written by an older schema; keep what we can.
                    known = {f for f in RunRecord.__dataclass_fields__}
                    records.append(RunRecord(**{k: v for k, v in entry.items()
                                                if k in known}))
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            logger.warning("History for %s unreadable: %s", tool_id, exc)
        self._cache[tool_id] = records
        return records

    def get(self, tool_id: str, run_id: str) -> Optional[RunRecord]:
        for record in self.list(tool_id):
            if record.run_id == run_id:
                return record
        return None

    def add(self, record: RunRecord) -> None:
        """Insert a record at the front and persist, honouring the limit."""
        if self.limit <= 0:
            return
        records = self.list(record.tool_id)
        records.insert(0, record)
        del records[self.limit:]
        self._write(record.tool_id, records)

    def remove(self, tool_id: str, run_id: str) -> bool:
        records = self.list(tool_id)
        kept = [r for r in records if r.run_id != run_id]
        if len(kept) == len(records):
            return False
        self._cache[tool_id] = kept
        self._write(tool_id, kept)
        return True

    def clear(self, tool_id: str) -> None:
        self._cache[tool_id] = []
        self._write(tool_id, [])

    def clear_all(self) -> int:
        """Delete every tool's history file; returns how many were removed."""
        removed = 0
        for path in self.directory.glob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        self._cache.clear()
        return removed

    def total_runs(self) -> int:
        return sum(len(self.list(path.stem)) for path in self.directory.glob("*.json"))

    def _write(self, tool_id: str, records: list[RunRecord]) -> None:
        self._cache[tool_id] = records
        path = self._file(tool_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump([asdict(record) for record in records], handle, indent=1)
            tmp.replace(path)
        except OSError as exc:
            logger.error("Could not write history for %s: %s", tool_id, exc)
