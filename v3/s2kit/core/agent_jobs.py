"""Durable headless jobs with bounded event queues and independent cancellation."""

from __future__ import annotations

import ctypes
import dataclasses
import json
import math
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from s2kit.core import paths
from s2kit.core.agent_catalog import get_tool, prepare
from s2kit.core.agent_schema import RequestError
from s2kit.core.jobs import JobRunner
from s2kit.core.tool_definition import load_object


def json_value(value: Any) -> Any:
    """Normalize framework payloads into portable JSON values."""
    if dataclasses.is_dataclass(value):
        return json_value(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    if isinstance(value, Path):
        return str(value.resolve())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "item"):
        return json_value(value.item())
    return str(value)


def process_identity(pid: int) -> str | None:
    """Identify a live process without Windows os.kill's termination semantics."""
    if os.name == "nt":
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                return None
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        stat = Path(f"/proc/{pid}/stat")
        return stat.read_text().rsplit(")", 1)[1].split()[19] if stat.exists() else "alive"
    except (OSError, IndexError):
        return None


class JobStore:
    """Per-job metadata, append-only events and bounded artifact readers."""

    def __init__(self, directory: Path | None = None):
        self.directory = Path(directory) if directory else paths.config_dir() / "agent-jobs"
        self._io_lock = threading.RLock()

    def path(self, job_id: str, suffix: str = ".json") -> Path:
        """Resolve a job-owned file, rejecting path-like identifiers."""
        if not re.fullmatch(r"[0-9a-f]{12}", job_id):
            raise RequestError({"job_id": "Expected a 12-character hexadecimal job ID"})
        return self.directory / (job_id + suffix)

    def save(self, record: dict) -> None:
        """Atomically publish a complete metadata snapshot."""
        target = self.path(record["job_id"])
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        with self._io_lock:
            temporary.write_text(json.dumps(json_value(record), ensure_ascii=False, allow_nan=False), encoding="utf-8")
            for attempt in range(50):
                try:
                    temporary.replace(target)
                    break
                except PermissionError:
                    if attempt == 49:
                        raise
                    time.sleep(0.01)

    def get(self, job_id: str) -> dict:
        """Read a snapshot and recognize jobs whose owning process exited."""
        try:
            with self._io_lock:
                record = json.loads(self.path(job_id).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RequestError({"job_id": "Job not found"}) from exc
        if record["status"] == "running" and process_identity(record["pid"]) != record["process_identity"]:
            record.update(status="interrupted", summary="Owning process exited; resume workflows with the same parameters")
        return record

    def list(self, limit: int = 50) -> list[dict]:
        """Return the newest jobs, including previous runtime sessions."""
        files = sorted((p for p in self.directory.glob("*.json") if re.fullmatch(r"[0-9a-f]{12}", p.stem)),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        return [self.get(path.stem) for path in files[:max(1, min(limit, 200))]]

    def events(self, job_id: str, cursor: int = 0, limit: int = 100) -> dict:
        """Read at most 200 events using an opaque byte-offset cursor."""
        self.get(job_id)
        if cursor < 0 or limit < 1:
            raise RequestError({"cursor": "Cursor must be nonnegative and limit positive"})
        result = []
        path = self.path(job_id, ".jsonl")
        if not path.exists():
            return {"events": [], "next_cursor": cursor}
        with path.open("rb") as stream:
            if cursor > path.stat().st_size:
                raise RequestError({"cursor": "Cursor exceeds the event file size"})
            stream.seek(cursor)
            for _ in range(min(limit, 200)):
                line = stream.readline()
                if not line.endswith(b"\n"):
                    break
                try:
                    result.append(json.loads(line))
                except (ValueError, UnicodeError) as exc:
                    raise RequestError({"cursor": "Cursor must come from an earlier event response"}) from exc
                cursor = stream.tell()
        return {"events": result, "next_cursor": cursor}

    def cancel(self, job_id: str) -> dict:
        """Request cooperative cancellation even from another CLI process."""
        record = self.get(job_id)
        if record["status"] == "running":
            self.path(job_id, ".cancel").touch()
        return {"job_id": job_id, "cancel_requested": record["status"] == "running", "status": record["status"]}

    def read_artifact(self, job_id: str, path: str, cursor: int = 0, limit: int = 16384) -> dict:
        """Read a bounded portion of a registered text artifact or result table."""
        record = self.get(job_id)
        target = Path(path).resolve()
        allowed = {Path(a["path"]).resolve() for a in record["artifacts"]}
        allowed.update(Path(table["data_path"]).resolve() for table in record["tables"])
        if target not in allowed or not target.is_file():
            raise RequestError({"path": "Choose an existing artifact registered by this job"})
        if target.suffix.lower() not in {".json", ".md", ".txt", ".log", ".qc", ".qci", ".vmt", ".vmat",
                                         ".lua", ".smd", ".csv", ".tsv", ".kv", ".kv3", ".cfg"}:
            raise RequestError({"path": "Binary artifacts are available by filesystem path"})
        if cursor < 0 or limit < 1 or cursor > target.stat().st_size:
            raise RequestError({"cursor": "Invalid artifact cursor or limit"})
        with target.open("rb") as stream:
            stream.seek(cursor)
            chunk = stream.read(min(limit, 65536))
            next_cursor = stream.tell()
        return {"path": str(target), "text": chunk.decode("utf-8", errors="replace"),
                "next_cursor": next_cursor, "eof": next_cursor >= target.stat().st_size}


class AgentRuntime:
    """Run one conversion at a time while draining its events independently."""

    def __init__(self, directory: Path | None = None, settings_path: Path | None = None):
        self.store = JobStore(directory)
        self.settings_path = settings_path
        self.runner = JobRunner()
        self._lock = threading.RLock()
        self._active = None
        self._collector = None
        self._closed = False

    def start(self, tool_id: str, params: dict, preferences: dict | None = None,
              on_event: Callable | None = None) -> dict:
        """Validate before launching and return durable identity immediately."""
        with self._lock:
            if self._closed:
                raise RequestError({"runtime": "Runtime is shutting down"})
            if self._collector and self._collector.is_alive():
                raise RequestError({"runtime": "Busy: one conversion is already active"})
            values, prefs = prepare(tool_id, params, preferences, self.settings_path)
            spec = get_tool(tool_id)
            try:
                executor = load_object(spec.definition.EXECUTOR)()
            except ImportError as exc:
                raise RequestError({"dependencies": str(exc)}) from exc
            self.store.directory.mkdir(parents=True, exist_ok=True)
            handle = self.runner.start(tool_id, values, prefs, executor.run_job, queue_size=256)
            record = {"version": 1, "job_id": handle.job_id, "tool_id": tool_id, "status": "running",
                      "params": values, "preferences": prefs, "started_at": handle.started_at, "finished_at": 0,
                      "pid": os.getpid(), "process_identity": process_identity(os.getpid()),
                      "summary": "", "metrics": [], "tables": [], "artifacts": [], "errors": [],
                      "counts": {}, "progress": [0, 0], "event_count": 0}
            try:
                self.store.save(record)
            except OSError:
                handle.cancel()
                while handle.thread.is_alive():
                    try:
                        handle.events.get(timeout=0.1)
                    except queue.Empty:
                        pass
                raise
            self._active = handle
            self._collector = threading.Thread(target=self._collect, args=(handle, record, on_event),
                                                name=f"agent-events-{handle.job_id}", daemon=True)
            self._collector.start()
            return {"job_id": handle.job_id, "tool_id": tool_id, "status": "running"}

    def _collect(self, handle, record: dict, on_event) -> None:
        """Stream events to disk, keeping table previews and metadata bounded."""
        storage_error = None
        last_saved = time.monotonic()
        stream = None
        try:
            stream = self.store.path(handle.job_id, ".jsonl").open("a", encoding="utf-8")
            while True:
                if self.store.path(handle.job_id, ".cancel").exists():
                    handle.cancel()
                try:
                    event = handle.events.get(timeout=0.1)
                except queue.Empty:
                    continue
                payload = json_value(event.payload)
                if event.kind == "table":
                    target = self.store.path(handle.job_id, f".table.{record['event_count']}.json")
                    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                    payload = {**payload, "rows": payload["rows"][:50], "row_count": len(payload["rows"]),
                               "data_path": str(target.resolve())}
                    record["tables"].append(payload)
                elif event.kind == "metric":
                    record["metrics"] = [m for m in record["metrics"] if m["label"] != payload["label"]]
                    record["metrics"].append(payload)
                elif event.kind == "artifact":
                    record["artifacts"].append(payload)
                elif event.kind == "summary":
                    record["summary"] = payload
                elif event.kind == "progress":
                    record["progress"] = payload
                elif event.kind == "log":
                    level = payload["level"]
                    record["counts"][level] = record["counts"].get(level, 0) + 1
                    if level == "ERROR" and len(record["errors"]) < 50:
                        record["errors"].append(payload["message"])
                entry = {"index": record["event_count"], "kind": event.kind, "payload": payload}
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
                record["event_count"] += 1
                if on_event:
                    on_event(entry)
                if event.kind == "done":
                    break
                if time.monotonic() - last_saved >= 0.25:
                    stream.flush()
                    self.store.save(record)
                    last_saved = time.monotonic()
        except Exception as exc:
            storage_error = f"Job event storage failed: {exc}"
            handle.cancel()
            while handle.thread.is_alive():
                try:
                    handle.events.get(timeout=0.1)
                except queue.Empty:
                    pass
        finally:
            if stream:
                stream.close()
            handle.join()
            record.update(status="failed" if storage_error else handle.status, finished_at=handle.finished_at)
            if storage_error:
                record["errors"].append(storage_error)
            if not record["summary"]:
                record["summary"] = handle.error or record["status"].title()
            self.store.save(record)

    def wait(self, job_id: str) -> dict:
        """Wait for the event collector, ensuring the final result is durable."""
        with self._lock:
            collector = self._collector if self._active and self._active.job_id == job_id else None
        if collector:
            while collector.is_alive():
                collector.join(timeout=0.1)
        return self.store.get(job_id)

    def close(self) -> None:
        """Cancel and drain active work before the owning process exits."""
        with self._lock:
            self._closed = True
            if self._active:
                self._active.cancel()
            collector = self._collector
        if collector:
            collector.join()
