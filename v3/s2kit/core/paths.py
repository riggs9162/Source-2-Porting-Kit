"""
Well-known directories, resolved once and created on demand.

Everything the app writes at runtime (settings, history, logs, preview cache)
lives under the v3 root when running from source, or next to the executable
when frozen.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def app_root() -> Path:
    """The v3 directory (or the folder holding the frozen executable)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # s2kit/core/paths.py -> s2kit/core -> s2kit -> v3
    return Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def repo_root() -> Path:
    """The porting-kit repository root (parent of v3). Used to find manuals."""
    return app_root().parent


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_dir() -> Path:
    """User settings, tool presets and run history."""
    return _ensure(app_root() / "config")


def history_dir() -> Path:
    """One JSON file of run records per tool."""
    return _ensure(config_dir() / "history")


def logs_dir() -> Path:
    """Rotating application log plus per-run log exports."""
    return _ensure(app_root() / "logs")


def cache_dir() -> Path:
    """Derived artefacts we can always regenerate (thumbnails, waveforms)."""
    return _ensure(app_root() / ".cache")


def assets_dir() -> Path:
    """Static assets served to the Flet client (icons, fonts); a frozen build reads its bundled copy."""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled) / "assets"
    return _ensure(app_root() / "assets")
