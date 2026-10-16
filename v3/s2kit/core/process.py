"""
Streaming subprocess execution.

Every external program the kit drives - Source2Viewer-CLI, studiomdl, ffmpeg -
is long-running and chatty, so the shared contract is: forward each output line
as it arrives, and stop promptly when the user cancels.

Cancellation is the part that is easy to get wrong. Reading a pipe blocks, so a
cooperative "check a flag between lines" loop can hang for as long as the child
stays quiet. A watchdog thread therefore kills the child as soon as the cancel
flag is set; that closes the pipe, the read loop ends, and the caller sees
:class:`ProcessCancelled`.

Framework-free: no Flet, no Qt. Jobs pass `ctx.log` as `on_line` and
`ctx.cancel_event` as `cancel`.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

LineHandler = Callable[[str], None]

#: How often the watchdog re-checks the cancel flag.
_CANCEL_POLL = 0.15
#: Grace period between terminate() and kill() when cancelling.
_TERMINATE_GRACE = 3.0


class ProcessError(RuntimeError):
    """The program could not be started (missing binary, permissions, ...)."""


class ProcessCancelled(RuntimeError):
    """The run was stopped because the cancel flag was set."""


def quote(argument: str) -> str:
    """Quote one argument for display in a log line."""
    return f'"{argument}"' if " " in argument else argument


def format_command(args: Sequence[str]) -> str:
    """The command line as the user would type it, for the log."""
    return " ".join(quote(str(a)) for a in args)


def no_window_kwargs() -> dict:
    """Popen kwargs that stop a console window flashing up on Windows."""
    if sys.platform != "win32":
        return {}
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "startupinfo": info,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def run_streamed(args: Sequence[str], on_line: LineHandler, *,
                 cancel: Optional[threading.Event] = None,
                 cwd: Optional[Path] = None,
                 env: Optional[Mapping[str, str]] = None,
                 echo_command: bool = True,
                 timeout: Optional[float] = None) -> int:
    """Run a program, forwarding stdout+stderr line by line. Returns the exit code.

    Args:
        args: the command line; the first element is the executable.
        on_line: called with each output line, already stripped of its newline.
        cancel: setting this event terminates the child promptly.
        cwd: working directory for the child.
        env: complete environment for the child (None inherits ours).
        echo_command: log the command line before running it.
        timeout: kill the child after this many seconds (None = no limit).

    Raises:
        ProcessError: the program could not be started.
        ProcessCancelled: `cancel` was set before or during the run.
    """
    command = [str(a) for a in args]
    if cancel is not None and cancel.is_set():
        raise ProcessCancelled()
    if echo_command:
        on_line("Running: " + format_command(command))

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd) if cwd else None,
            env=dict(env) if env is not None else None,
            **no_window_kwargs(),
        )
    except OSError as exc:
        raise ProcessError(f"Could not start {Path(command[0]).name}: {exc}") from exc

    stop_watchdog = threading.Event()
    cancelled = threading.Event()

    def watchdog() -> None:
        """Kill the child when the caller cancels, or when the timeout expires."""
        waited = 0.0
        while not stop_watchdog.wait(_CANCEL_POLL):
            waited += _CANCEL_POLL
            if cancel is not None and cancel.is_set():
                cancelled.set()
                _terminate(process)
                return
            if timeout is not None and waited >= timeout:
                cancelled.set()
                on_line(f"Timed out after {timeout:.0f}s; terminating.")
                _terminate(process)
                return

    watcher = threading.Thread(target=watchdog, name="process-watchdog",
                               daemon=True)
    watcher.start()

    try:
        assert process.stdout is not None
        for line in process.stdout:
            on_line(line.rstrip("\r\n"))
    finally:
        stop_watchdog.set()
        watcher.join(timeout=1.0)
        code = process.wait()

    if cancelled.is_set() or (cancel is not None and cancel.is_set()):
        raise ProcessCancelled()
    return code


def _terminate(process: subprocess.Popen) -> None:
    """Ask the child to stop, then insist. Takes its children with it."""
    if process.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            # taskkill /T reaches grandchildren (VRF spawns worker processes);
            # a bare terminate() would orphan them.
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           capture_output=True, **no_window_kwargs())
        else:
            process.terminate()
    except OSError as exc:
        logger.debug("terminate failed for pid %s: %s", process.pid, exc)

    try:
        process.wait(timeout=_TERMINATE_GRACE)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass


def capture(args: Sequence[str], *, cancel: Optional[threading.Event] = None,
            cwd: Optional[Path] = None,
            timeout: Optional[float] = None) -> tuple[int, str]:
    """Run a program and return `(exit_code, combined_output)`.

    For short interrogations (a `-b RERL` dump, `ffprobe`) where the output is
    parsed rather than streamed to a log.
    """
    lines: list[str] = []
    code = run_streamed(args, lines.append, cancel=cancel, cwd=cwd,
                        echo_command=False, timeout=timeout)
    return code, "\n".join(lines)


def which(name: str, *, override: str = "") -> Optional[Path]:
    """Locate a program: an explicit override first, then PATH."""
    import shutil

    if override:
        candidate = Path(override)
        if candidate.is_file():
            return candidate
    found = shutil.which(name)
    return Path(found) if found else None


def environment_without(*names: str) -> dict[str, str]:
    """A copy of the environment with some variables removed.

    Source tools inherit `VPROJECT`/`SteamAppId` from a parent shell and then
    compile against the wrong game; the callers that care pass those here.
    """
    env = dict(os.environ)
    for name in names:
        env.pop(name, None)
    return env
