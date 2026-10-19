"""
Background job execution.

Every tool run is a job: a plain function executed on a worker thread that
talks back through a queue of events. The UI drains that queue on a timer and
applies the batch, which keeps a job that emits ten thousand log lines from
flooding the render loop, and keeps the job function itself completely
framework-free - it only ever touches :class:`JobContext`.

A job reports through the context:

    ctx.info("...")                  log lines, levelled
    ctx.progress(done, total)        determinate or indeterminate progress
    ctx.status("Converting ...")     one-line status under the progress bar
    ctx.metric("Files", 42)          stat tiles on the Output tab
    ctx.table(...)                   result tables on the Output tab
    ctx.artifact(path)               files the Preview tab can open
    ctx.raise_if_cancelled()         cooperative cancellation
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
import traceback
import uuid
from concurrent.futures import FIRST_COMPLETED, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "SUCCESS")
LEVEL_ORDER = {name: index for index, name in enumerate(LEVELS)}

STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"


class JobCancelled(Exception):
    """Raised inside a job when the user asked it to stop."""


# ----------------------------------------------------------------------
# Payloads
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class LogLine:
    """One levelled log message with a wall-clock timestamp."""

    timestamp: float
    level: str
    message: str

    def formatted(self, show_timestamp: bool = True) -> str:
        if not show_timestamp:
            return f"[{self.level}] {self.message}"
        stamp = time.strftime("%H:%M:%S", time.localtime(self.timestamp))
        return f"[{stamp}] [{self.level}] {self.message}"


@dataclass
class Table:
    """A result table rendered on the Output tab."""

    title: str
    columns: Sequence[str]
    rows: list[Sequence[Any]]
    numeric_columns: Sequence[int] = ()
    #: Column index the table sorts by initially, or None to keep job order.
    sort_column: Optional[int] = None
    sort_ascending: bool = True
    #: Column index holding a file path; those cells become preview links.
    path_column: Optional[int] = None


@dataclass
class Artifact:
    """A file a run produced, offered for preview and for opening on disk."""

    path: Path
    label: str = ""
    kind: str = ""      # image | model | audio | text | data (auto-detected)
    note: str = ""

    def display_name(self) -> str:
        return self.label or self.path.name


@dataclass
class Metric:
    """A single headline number shown as a stat tile."""

    label: str
    value: Any
    unit: str = ""
    tone: str = "neutral"   # neutral | good | warn | bad


@dataclass
class JobEvent:
    """Anything a worker sends to the UI. `kind` selects the payload type."""

    kind: str
    payload: Any = None


@dataclass
class BatchOutcome:
    """What :meth:`JobContext.map` did: results in input order, plus counts."""

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    results: list[Any] = field(default_factory=list)
    errors: list[tuple[Any, Exception]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.cancelled == 0

    def successful(self) -> list[Any]:
        """Non-None results, i.e. the items that got through."""
        return [result for result in self.results if result is not None]

    def summary(self, unit: str = "item") -> str:
        text = f"{self.succeeded}/{self.total} {unit}(s)"
        if self.failed:
            text += f", {self.failed} failed"
        if self.cancelled:
            text += f", {self.cancelled} cancelled"
        return text


# ----------------------------------------------------------------------
# Context handed to job functions
# ----------------------------------------------------------------------

class JobContext:
    """The only thing a job function talks to. Thread-safe by construction."""

    def __init__(self, job_id: str, params: dict[str, Any],
                 prefs: dict[str, Any], events: "queue.Queue[JobEvent]",
                 cancel_event: threading.Event):
        self.job_id = job_id
        self.params = params
        #: Effective preference values (global + this tool's own block).
        self.prefs = prefs
        self._events = events
        self._cancel = cancel_event
        self._started = time.time()
        self._artifacts: list[Artifact] = []
        self._counts = {level: 0 for level in LEVELS}

    # -- logging -------------------------------------------------------

    def log(self, message: str, level: str = "INFO") -> None:
        level = level.upper() if level.upper() in LEVEL_ORDER else "INFO"
        self._counts[level] += 1
        self._events.put(JobEvent("log", LogLine(time.time(), level, str(message))))

    def debug(self, message: str) -> None:
        self.log(message, "DEBUG")

    def info(self, message: str) -> None:
        self.log(message, "INFO")

    def warn(self, message: str) -> None:
        self.log(message, "WARNING")

    def error(self, message: str) -> None:
        self.log(message, "ERROR")

    def success(self, message: str) -> None:
        self.log(message, "SUCCESS")

    def counts(self) -> dict[str, int]:
        """How many messages of each level were emitted so far."""
        return dict(self._counts)

    # -- progress ------------------------------------------------------

    def progress(self, done: int, total: int = 0) -> None:
        """Report progress. `total <= 0` means indeterminate."""
        self._events.put(JobEvent("progress", (int(done), int(total))))

    def status(self, text: str) -> None:
        """Replace the one-line status shown next to the progress bar."""
        self._events.put(JobEvent("status", str(text)))

    def step(self, index: int, total: int, text: str) -> None:
        """Progress plus status plus a log line - the common per-item call."""
        self.progress(index, total)
        self.status(text)
        self.info(text)

    # -- results -------------------------------------------------------

    def metric(self, label: str, value: Any, unit: str = "",
               tone: str = "neutral") -> None:
        self._events.put(JobEvent("metric", Metric(label, value, unit, tone)))

    def table(self, title: str, columns: Sequence[str],
              rows: Iterable[Sequence[Any]], **kwargs: Any) -> None:
        self._events.put(
            JobEvent("table", Table(title, list(columns), [list(r) for r in rows],
                                    **kwargs))
        )

    def artifact(self, path: Any, label: str = "", kind: str = "",
                 note: str = "") -> Artifact:
        """Register an output file so the Preview tab can open it."""
        item = Artifact(Path(path), label, kind, note)
        self._artifacts.append(item)
        self._events.put(JobEvent("artifact", item))
        return item

    def summary(self, text: str) -> None:
        """The one-line outcome stored in the history record."""
        self._events.put(JobEvent("summary", str(text)))

    # -- cancellation --------------------------------------------------

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    @property
    def cancel_event(self) -> threading.Event:
        """The raw flag, for handing to :func:`s2kit.core.process.run_streamed`."""
        return self._cancel

    def raise_if_cancelled(self) -> None:
        """Call inside loops; raises :class:`JobCancelled` when stopping."""
        if self._cancel.is_set():
            raise JobCancelled()

    @property
    def elapsed(self) -> float:
        return time.time() - self._started

    # -- batch work ----------------------------------------------------

    def worker_count(self, requested: int = 0) -> int:
        """How many threads a batch should use.

        `requested` wins when positive, then the `max_parallel` preference,
        then the CPU count. Capped at 32 - past that the disk is the limit and
        the log becomes unreadable.
        """
        if requested <= 0:
            requested = int(self.prefs.get("max_parallel", 0) or 0)
        if requested <= 0:
            requested = os.cpu_count() or 4
        return max(1, min(32, requested))

    def map(self, items: Sequence[Any], worker: Callable[[Any], Any], *,
            workers: int = 0,
            label: Callable[[Any], str] = str,
            unit: str = "item",
            log_each: bool = True) -> "BatchOutcome":
        """Run `worker(item)` over `items` on a thread pool, with progress.

        Exceptions are caught per item and recorded, so one bad file cannot
        abort a 500-file batch; cancellation stops queuing new work and lets
        the running items finish. Results come back in input order.

        Returns a :class:`BatchOutcome` with the results, the failures and the
        counts a tool typically turns into metrics.
        """
        from concurrent.futures import ThreadPoolExecutor

        entries = list(items)
        total = len(entries)
        outcome = BatchOutcome(total=total)
        if not total:
            return outcome

        count = self.worker_count(workers)
        self.debug(f"Processing {total} {unit}(s) across {count} worker(s)")
        outcome.results = [None] * total
        done = 0

        with ThreadPoolExecutor(max_workers=count) as pool:
            futures = {}
            next_index = 0
            while futures or next_index < total:
                if self.cancelled:
                    for future in futures:
                        future.cancel()
                    break
                while next_index < total and len(futures) < count * 2 and not self.cancelled:
                    item = entries[next_index]
                    futures[pool.submit(worker, item)] = (next_index, item)
                    next_index += 1
                completed, _ = wait(futures, timeout=0.05, return_when=FIRST_COMPLETED)
                for future in completed:
                    index, item = futures.pop(future)
                    done += 1
                    name = label(item)
                    try:
                        outcome.results[index] = future.result()
                        outcome.succeeded += 1
                        if log_each:
                            self.debug(f"[{done}/{total}] {name}")
                    except JobCancelled:
                        outcome.cancelled += 1
                    except Exception as exc:
                        outcome.failed += 1
                        outcome.errors.append((item, exc))
                        self.error(f"{name}: {type(exc).__name__}: {exc}")
                    self.progress(done, total)
                    self.status(f"{done}/{total}  {name}")

        if self.cancelled:
            raise JobCancelled()
        return outcome


# ----------------------------------------------------------------------
# Handle + runner
# ----------------------------------------------------------------------

JobFunction = Callable[[JobContext], Any]


@dataclass
class JobHandle:
    """Everything the UI needs to follow, cancel and record one run."""

    job_id: str
    tool_id: str
    params: dict[str, Any]
    events: "queue.Queue[JobEvent]"
    cancel_event: threading.Event
    started_at: float
    thread: Optional[threading.Thread] = None
    finished_at: float = 0.0
    status: str = STATUS_RUNNING
    error: str = ""
    traceback: str = ""
    summary: str = ""
    artifacts: list[Artifact] = field(default_factory=list)

    @property
    def running(self) -> bool:
        return self.status == STATUS_RUNNING

    @property
    def duration(self) -> float:
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at)

    def cancel(self) -> None:
        self.cancel_event.set()

    def join(self, timeout: Optional[float] = None) -> None:
        """Block until the worker exits - used by tests and shutdown."""
        if self.thread is not None:
            self.thread.join(timeout)


class JobRunner:
    """Starts jobs on daemon threads and keeps the active ones addressable."""

    def __init__(self) -> None:
        self._jobs: dict[str, JobHandle] = {}
        self._lock = threading.Lock()

    def start(self, tool_id: str, params: dict[str, Any],
              prefs: dict[str, Any], function: JobFunction, *,
              queue_size: int = 0) -> JobHandle:
        """Run `function(ctx)` on a worker thread and return its handle."""
        job_id = uuid.uuid4().hex[:12]
        handle = JobHandle(
            job_id=job_id,
            tool_id=tool_id,
            params=dict(params),
            events=queue.Queue(maxsize=queue_size),
            cancel_event=threading.Event(),
            started_at=time.time(),
        )
        ctx = JobContext(job_id, dict(params), dict(prefs),
                         handle.events, handle.cancel_event)

        def worker() -> None:
            try:
                function(ctx)
                handle.status = (STATUS_CANCELLED if ctx.cancelled else
                                 STATUS_PARTIAL if ctx.counts()["ERROR"] else STATUS_SUCCESS)
            except JobCancelled:
                handle.status = STATUS_CANCELLED
                ctx.warn("Cancelled by user.")
            except Exception as exc:
                # Killing a child process (or interrupting a parse of its
                # truncated output) surfaces as an ordinary exception. If the
                # user asked to stop, that is a cancellation, not a failure.
                if ctx.cancelled:
                    handle.status = STATUS_CANCELLED
                    ctx.warn("Cancelled by user.")
                    return
                handle.status = STATUS_FAILED
                handle.error = f"{type(exc).__name__}: {exc}"
                handle.traceback = traceback.format_exc()
                ctx.error(handle.error)
                for line in handle.traceback.rstrip().splitlines():
                    ctx.debug(line)
                logger.exception("Job %s (%s) failed", job_id, tool_id)
            finally:
                handle.finished_at = time.time()
                handle.artifacts = list(ctx._artifacts)
                handle.events.put(JobEvent("done", handle.status))
                with self._lock:
                    self._jobs.pop(job_id, None)

        thread = threading.Thread(target=worker, name=f"job-{tool_id}-{job_id}",
                                  daemon=True)
        handle.thread = thread
        with self._lock:
            self._jobs[job_id] = handle
        logger.info("Starting job %s for %s", job_id, tool_id)
        thread.start()
        return handle

    def active(self) -> list[JobHandle]:
        with self._lock:
            return list(self._jobs.values())

    def active_count(self) -> int:
        with self._lock:
            return len(self._jobs)

    def cancel_all(self) -> None:
        for handle in self.active():
            handle.cancel()


def drain(events: "queue.Queue[JobEvent]", limit: int = 500) -> list[JobEvent]:
    """Pop up to `limit` events without blocking - the UI pump's read side."""
    batch: list[JobEvent] = []
    for _ in range(limit):
        try:
            batch.append(events.get_nowait())
        except queue.Empty:
            break
    return batch
