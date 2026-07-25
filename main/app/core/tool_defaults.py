"""
Lightweight accessor for global tool defaults (Preferences → General).

Tools call into this module at construction time to seed their UI fields
with user-preferred defaults. Wrapped in try/except so a corrupted
settings file falls back to the tool's own hard-coded defaults instead
of crashing the tool.

These are read-once-at-open semantics: changing a default in Preferences
does not retroactively update an already-open tool. The user must close
and reopen the tool (or restart the app) to pick up the new value.
"""

from __future__ import annotations

from typing import Optional


_FALLBACK_MATERIAL_PATH = "models/ports"


def default_material_path(fallback: str = _FALLBACK_MATERIAL_PATH) -> str:
    """Return the user's preferred Material Path, or `fallback`.

    Empty/whitespace strings in settings are treated as "unset" so a
    deliberately blanked Preferences field still surfaces the call-site's
    own default rather than forcing an empty placeholder.
    """
    try:
        from app.core.settings import Settings
        value = str(Settings().get('default_material_path', fallback) or '').strip()
        return value or fallback
    except Exception:
        return fallback


def default_max_parallel(fallback: int) -> int:
    """Return the user's preferred Max Parallel, or `fallback`.

    `fallback` should be the cpu-derived value the tool would otherwise
    use. A stored value of 0 means "auto" — fall back to that cpu value.
    """
    try:
        from app.core.settings import Settings
        value = int(Settings().get('default_max_parallel', 0) or 0)
        return value if value > 0 else fallback
    except Exception:
        return fallback


def default_generate_mipmaps(fallback: bool = True) -> bool:
    """Return whether the Generate Mipmaps checkbox should start ticked."""
    try:
        from app.core.settings import Settings
        return bool(Settings().get('default_generate_mipmaps', fallback))
    except Exception:
        return fallback
