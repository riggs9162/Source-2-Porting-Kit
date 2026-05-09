"""
Shared ffmpeg subprocess helpers for audio batch tools.

Audio tools previously routed every file through pydub's `AudioSegment`,
which decodes the source into Python memory and re-encodes it — two ffmpeg
invocations per file plus a full sample copy through the Python layer. Calling
ffmpeg directly skips that round-trip, and the parallel runner spreads work
across cores since ffmpeg releases the GIL while the subprocess runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Tuple, TypeVar

T = TypeVar("T")
R = TypeVar("R")

_FFMPEG_PATH: Optional[str] = None


def ffmpeg_path() -> str:
    """Locate the ffmpeg executable, caching the result."""
    global _FFMPEG_PATH
    if _FFMPEG_PATH is None:
        path = shutil.which("ffmpeg")
        if not path:
            raise RuntimeError(
                "ffmpeg executable not found on PATH. Install ffmpeg and ensure "
                "it is on PATH for audio conversion tools to work."
            )
        _FFMPEG_PATH = path
    return _FFMPEG_PATH


def _no_window_kwargs() -> dict:
    """Suppress the console window flash when spawning ffmpeg on Windows."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}  # type: ignore[attr-defined]
    return {}


def run_ffmpeg(args: Sequence[str], *, timeout: Optional[float] = None) -> None:
    """Run ffmpeg with the given args, raising on non-zero exit.

    The full command line is `[ffmpeg, -hide_banner, -loglevel, error,
    -nostdin, *args]`. Per-process threading is left to ffmpeg's default
    since callers already parallelize across files.
    """
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostdin", *args]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        **_no_window_kwargs(),
    )
    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip() or "ffmpeg failed"
        raise RuntimeError(msg)


def default_workers(cap: int = 8) -> int:
    """Pick a sensible parallel worker count for audio batch jobs."""
    cpu = os.cpu_count() or 2
    # Each ffmpeg process is itself multi-threaded, so cap to avoid
    # oversubscribing the CPU on machines with many cores.
    return max(1, min(cap, cpu))


def parallel_for_each(
    items: Sequence[T],
    func: Callable[[T], R],
    *,
    max_workers: Optional[int] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    on_result: Optional[Callable[[T, Optional[R], Optional[BaseException]], None]] = None,
) -> List[Tuple[T, Optional[R], Optional[BaseException]]]:
    """Run `func(item)` in parallel.

    Results are reported via `on_result(item, value, error)` as each task
    finishes. If `should_stop()` returns True, no new tasks are scheduled and
    pending futures are cancelled (running tasks complete). Returns the list
    of (item, value, error) tuples.
    """
    workers = max_workers or default_workers()
    results: List[Tuple[T, Optional[R], Optional[BaseException]]] = []

    if workers <= 1 or len(items) <= 1:
        for item in items:
            if should_stop and should_stop():
                break
            try:
                value = func(item)
                results.append((item, value, None))
                if on_result:
                    on_result(item, value, None)
            except BaseException as exc:  # noqa: BLE001
                results.append((item, None, exc))
                if on_result:
                    on_result(item, None, exc)
        return results

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(func, item): item for item in items}
        try:
            for fut in as_completed(futures):
                item = futures[fut]
                if should_stop and should_stop():
                    for pending in futures:
                        pending.cancel()
                    break
                try:
                    value = fut.result()
                    results.append((item, value, None))
                    if on_result:
                        on_result(item, value, None)
                except BaseException as exc:  # noqa: BLE001
                    results.append((item, None, exc))
                    if on_result:
                        on_result(item, None, exc)
        except BaseException:
            for pending in futures:
                pending.cancel()
            raise

    return results


def wav_has_chunk(path: Path, target_id: bytes, *, max_scan: int = 4 * 1024 * 1024) -> bool:
    """Stream-scan a WAV file looking for a top-level RIFF chunk by id.

    Reads only chunk headers (8 bytes each) instead of the whole file. Stops
    after `max_scan` bytes to avoid pathological cases. Returns False on any
    error or if the chunk is not found.
    """
    if len(target_id) != 4:
        raise ValueError("target_id must be exactly 4 bytes")

    try:
        with path.open("rb") as fh:
            header = fh.read(12)
            if len(header) < 12 or header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
                return False

            scanned = 12
            while scanned < max_scan:
                hdr = fh.read(8)
                if len(hdr) < 8:
                    return False
                chunk_id = hdr[0:4]
                chunk_size = int.from_bytes(hdr[4:8], "little")
                if chunk_id == target_id:
                    return True
                # Skip payload (+ pad byte if size is odd) without reading it.
                pad = chunk_size & 1
                fh.seek(chunk_size + pad, os.SEEK_CUR)
                scanned += 8 + chunk_size + pad
    except OSError:
        return False
    return False
