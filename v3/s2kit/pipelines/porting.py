"""
Shared primitives for the model-porting pipeline.

Two conventions that every porting module needs and that v2 did not have a
single home for. Both are declared here rather than in one of the bigger modules
so the dependency graph stays a strict DAG - the low-level stages need
cancellation too, and must not have to reach up to an orchestrator for it.

**One log callback.** v2 had two incompatible signatures - a one-argument
`Callable[[str], None]` in the physics code and a two-argument
`(message, level)` in the I/O code - bridged by `_rlog = lambda m: log(m, "INFO")`
in three places. The unified type below is exactly the shape of
:meth:`s2kit.core.jobs.JobContext.log`, so a tool passes `ctx.log` straight in
with no adapter, and every one-argument call site in the ported code stays valid
unchanged.

**Cooperative cancellation.** The porting stages are long (a dual-quaternion
re-skin is a Python loop over every vertex) and none of them are interruptible on
their own. `check_cancelled` at the top of those loops is what makes the Cancel
button responsive.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

#: `(message: str, level: str = "INFO") -> None`. Deliberately loose so both
#: `on_log("...")` and `on_log("...", "WARNING")` call sites are valid.
LogFn = Callable[..., None]


class PortCancelled(RuntimeError):
    """Raised inside a port when the caller set the cancel event."""


def check_cancelled(cancel: Optional[threading.Event]) -> None:
    """Raise :class:`PortCancelled` if the caller asked the port to stop.

    Only ever call this as a bare statement at the top of a loop body. Putting it
    inside an expression or between two statements that compute a value is how a
    half-finished computation escapes into an output file.
    """
    if cancel is not None and cancel.is_set():
        raise PortCancelled()


def log_stdout(message: str, level: str = "INFO") -> None:
    """The default logger for pipeline functions called outside a job.

    A bare `print` cannot be the default any more: under the unified
    :data:`LogFn` a call like ``on_log("...", "WARNING")`` would render as
    ``"... WARNING"``.
    """
    print(message)
