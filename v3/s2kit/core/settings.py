"""
Persisted application state and preferences.

One JSON file. Values resolve in this order:

1. what the user saved,
2. the default declared on the matching preference :class:`Param`
   (see :mod:`s2kit.core.preferences`),
3. :data:`RUNTIME_DEFAULTS` for internal state that is not a preference.

That ordering means a preference added in a later version resolves without a
migration, and a tool's preference block works the moment it is imported.

Anything can subscribe to changes, which is how the theme applies live.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional

from s2kit.core import paths, preferences
from s2kit.core.params import Param

logger = logging.getLogger(__name__)

RECENT_PATHS_LIMIT = 12

#: Internal state that the user never edits directly in the Preferences page.
RUNTIME_DEFAULTS: dict[str, Any] = {
    "window_width": 1440,
    "window_height": 900,
    "window_top": None,
    "window_left": None,
    "window_maximized": False,
    "sidebar_collapsed": False,
    "last_tool": "",
    "favourite_tools": [],
    "tool_params": {},        # tool_id -> last submitted params
    "tool_prefs": {},         # tool_id -> tool-specific preference values
    "tool_presets": {},       # tool_id -> {preset name: params}
    "last_dirs": {},          # "<tool_id>/<field>" -> directory
    "recent_paths": [],
    "recent_tools": [],       # tool ids, most recent first
    "schema_version": 1,
}

ChangeListener = Callable[[str, Any], None]


class Settings:
    """Dictionary-backed settings with JSON persistence and change events."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else paths.config_dir() / "settings.json"
        self._values: dict[str, Any] = self._load()
        self._listeners: list[ChangeListener] = []
        self._suspend_save = False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                stored = json.load(handle)
            return dict(stored) if isinstance(stored, dict) else {}
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Settings unreadable (%s); starting from defaults", exc)
            return {}

    def save(self) -> None:
        """Write current values out via a temp file so a crash cannot truncate."""
        if self._suspend_save:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._values, handle, indent=2, sort_keys=True)
            tmp.replace(self.path)
        except OSError as exc:
            logger.error("Could not save settings: %s", exc)

    def reset_all(self) -> None:
        """Forget every stored value; defaults take over on the next read."""
        self._values = {}
        self.save()
        self._notify("*", None)

    def reset_keys(self, keys: list[str]) -> None:
        """Reset a specific set of keys (one preference section, typically)."""
        for key in keys:
            self._values.pop(key, None)
        self.save()
        for key in keys:
            self._notify(key, self.get(key))

    # ------------------------------------------------------------------
    # Change notification
    # ------------------------------------------------------------------

    def subscribe(self, listener: ChangeListener) -> Callable[[], None]:
        """Register a change listener; returns an unsubscribe callable."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def _notify(self, key: str, value: Any) -> None:
        for listener in list(self._listeners):
            try:
                listener(key, value)
            except Exception:  # a bad listener must not break settings writes
                logger.exception("Settings listener failed for %r", key)

    # ------------------------------------------------------------------
    # Generic access
    # ------------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        if key in self._values:
            return self._values[key]
        param = preferences.find(key)
        if param is not None:
            return param.default
        if key in RUNTIME_DEFAULTS:
            return copy.deepcopy(RUNTIME_DEFAULTS[key])
        return default

    def set(self, key: str, value: Any, *, save: bool = True) -> None:
        if self._values.get(key, _MISSING) == value:
            return
        self._values[key] = value
        if save:
            self.save()
        self._notify(key, value)

    def update(self, values: dict[str, Any], *, save: bool = True) -> None:
        """Apply several values, saving and notifying once per key."""
        self._suspend_save = True
        try:
            for key, value in values.items():
                self.set(key, value, save=False)
        finally:
            self._suspend_save = False
        if save:
            self.save()

    def as_dict(self) -> dict[str, Any]:
        """Effective values: defaults overlaid with everything stored."""
        merged = copy.deepcopy(RUNTIME_DEFAULTS)
        merged.update(preferences.schema_defaults())
        merged.update(self._values)
        return merged

    def export_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, sort_keys=True)

    def import_json(self, text: str) -> int:
        """Merge a settings export. Returns how many keys were applied."""
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("Settings import must be a JSON object")
        self.update(data)
        return len(data)

    # ------------------------------------------------------------------
    # Tool preferences
    # ------------------------------------------------------------------

    def tool_pref_store(self, tool_id: str) -> "ToolPrefStore":
        """A :class:`~s2kit.core.preferences.PrefStore` scoped to one tool."""
        return ToolPrefStore(self, tool_id)

    def tool_pref(self, tool_id: str, key: str, default: Any = None) -> Any:
        store = self.get("tool_prefs", {})
        values = store.get(tool_id, {}) if isinstance(store, dict) else {}
        if key in values:
            return values[key]
        section = preferences.tool_section(tool_id)
        if section:
            for param in section.params:
                if param.key == key:
                    return param.default
        return default

    def set_tool_pref(self, tool_id: str, key: str, value: Any,
                      *, save: bool = True) -> None:
        store = dict(self.get("tool_prefs", {}) or {})
        values = dict(store.get(tool_id, {}) or {})
        values[key] = value
        store[tool_id] = values
        self.set("tool_prefs", store, save=save)

    def tool_prefs(self, tool_id: str) -> dict[str, Any]:
        """Effective preference values for a tool (defaults + overrides)."""
        section = preferences.tool_section(tool_id)
        result: dict[str, Any] = {}
        if section:
            result.update({param.key: param.default for param in section.params})
        store = self.get("tool_prefs", {})
        if isinstance(store, dict):
            result.update(store.get(tool_id, {}) or {})
        return result

    # ------------------------------------------------------------------
    # Convenience accessors used across the UI
    # ------------------------------------------------------------------

    def last_dir(self, tool_id: str, field: str) -> str:
        dirs = self.get("last_dirs", {})
        if not isinstance(dirs, dict):
            return ""
        return str(dirs.get(f"{tool_id}/{field}", "") or "")

    def remember_dir(self, tool_id: str, field: str, directory: str) -> None:
        dirs = dict(self.get("last_dirs", {}) or {})
        dirs[f"{tool_id}/{field}"] = directory
        self.set("last_dirs", dirs)

    def recent_paths(self) -> list[str]:
        values = self.get("recent_paths", [])
        return [str(v) for v in values] if isinstance(values, list) else []

    def push_recent_path(self, path: str) -> None:
        if not path:
            return
        recents = [p for p in self.recent_paths() if p != path]
        recents.insert(0, path)
        self.set("recent_paths", recents[:RECENT_PATHS_LIMIT])

    def tool_params(self, tool_id: str) -> dict[str, Any]:
        store = self.get("tool_params", {})
        values = store.get(tool_id) if isinstance(store, dict) else None
        return dict(values) if isinstance(values, dict) else {}

    def save_tool_params(self, tool_id: str, values: dict[str, Any]) -> None:
        store = dict(self.get("tool_params", {}) or {})
        store[tool_id] = {k: v for k, v in values.items() if _is_jsonable(v)}
        self.set("tool_params", store)

    def presets(self, tool_id: str) -> dict[str, dict[str, Any]]:
        """Named parameter sets the user saved for a tool."""
        store = self.get("tool_presets", {})
        values = store.get(tool_id) if isinstance(store, dict) else None
        return dict(values) if isinstance(values, dict) else {}

    def save_preset(self, tool_id: str, name: str,
                    values: dict[str, Any]) -> None:
        store = dict(self.get("tool_presets", {}) or {})
        presets = dict(store.get(tool_id, {}) or {})
        presets[name] = {k: v for k, v in values.items() if _is_jsonable(v)}
        store[tool_id] = presets
        self.set("tool_presets", store)

    def delete_preset(self, tool_id: str, name: str) -> bool:
        store = dict(self.get("tool_presets", {}) or {})
        presets = dict(store.get(tool_id, {}) or {})
        if name not in presets:
            return False
        presets.pop(name)
        store[tool_id] = presets
        self.set("tool_presets", store)
        return True

    def favourites(self) -> list[str]:
        values = self.get("favourite_tools", [])
        return [str(v) for v in values] if isinstance(values, list) else []

    def toggle_favourite(self, tool_id: str) -> bool:
        """Flip a tool's favourite flag; returns the new state."""
        favourites = self.favourites()
        if tool_id in favourites:
            favourites.remove(tool_id)
            state = False
        else:
            favourites.append(tool_id)
            state = True
        self.set("favourite_tools", favourites)
        return state

    def push_recent_tool(self, tool_id: str) -> None:
        recents = [t for t in self.get("recent_tools", []) if t != tool_id]
        recents.insert(0, tool_id)
        self.set("recent_tools", recents[:20])

    def recent_tools(self) -> list[str]:
        values = self.get("recent_tools", [])
        return [str(v) for v in values] if isinstance(values, list) else []


class ToolPrefStore:
    """Adapter that makes one tool's preferences look like a flat store."""

    def __init__(self, settings: Settings, tool_id: str):
        self._settings = settings
        self._tool_id = tool_id

    def get(self, key: str, default: Any = None) -> Any:
        return self._settings.tool_pref(self._tool_id, key, default)

    def set(self, key: str, value: Any, *, save: bool = True) -> None:
        self._settings.set_tool_pref(self._tool_id, key, value, save=save)

    def all(self) -> dict[str, Any]:
        return self._settings.tool_prefs(self._tool_id)

    def reset(self, params: list[Param]) -> None:
        for param in params:
            self.set(param.key, param.default, save=False)
        self._settings.save()


class _Missing:
    def __eq__(self, other: object) -> bool:
        return False


_MISSING = _Missing()


def _is_jsonable(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool, type(None), list, dict))
