"""
Declarative parameter schema.

One schema type serves three jobs, which is why it lives in the core and knows
nothing about Flet:

* tool inputs   - what the Run tab renders and what a job receives
* preferences   - global and per-tool settings pages render the same way
* history       - a run record stores plain values that can be fed straight
                  back into the form, or into a headless call

A tool declares `PARAMS = [...]`; the UI turns each entry into a control, the
core turns each entry into a validated dict. Adding a knob is a one-line
change and it is automatically persisted, restorable and validated.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence


@dataclass(frozen=True)
class Option:
    """One entry in a :class:`ChoiceParam`."""

    value: Any
    label: str
    help: str = ""
    icon: str = ""


def options(*pairs: Any) -> list[Option]:
    """Build an option list from `value` or `(value, label)` shorthand."""
    built: list[Option] = []
    for pair in pairs:
        if isinstance(pair, Option):
            built.append(pair)
        elif isinstance(pair, (tuple, list)):
            value, label, *rest = pair
            built.append(Option(value, str(label), rest[0] if rest else ""))
        else:
            built.append(Option(pair, str(pair).replace("_", " ").title()))
    return built


@dataclass
class Param:
    """Base class for every parameter kind."""

    key: str
    label: str
    default: Any = None
    help: str = ""
    group: str = "General"
    required: bool = False
    advanced: bool = False
    width: Optional[int] = None
    #: Show this control only while another param holds one of these values.
    depends_on: Optional[tuple[str, Any]] = None
    #: Extra validation hook; return an error string or None.
    validator: Optional[Callable[[Any], Optional[str]]] = None
    #: Restart needed before the value takes effect (preferences only).
    restart_required: bool = False

    def coerce(self, value: Any) -> Any:
        """Convert a raw UI/JSON value into the type this param promises."""
        return value

    def validate(self, value: Any) -> Optional[str]:
        """Return an error message for `value`, or None when it is fine."""
        if self.required and _is_blank(value):
            return f"{self.label} is required"
        if self.validator is not None:
            return self.validator(value)
        return None


@dataclass
class TextParam(Param):
    """Free text. `lines > 1` renders a multiline editor."""

    default: str = ""
    hint: str = ""
    lines: int = 1
    max_lines: int = 8
    password: bool = False
    prefix_icon: str = ""

    def coerce(self, value: Any) -> str:
        return "" if value is None else str(value)


@dataclass
class PathParam(Param):
    """A filesystem path with a browse button and last-directory memory."""

    default: str = ""
    #: open_file | open_files | directory | save_file
    mode: str = "open_file"
    extensions: Sequence[str] = field(default_factory=tuple)
    must_exist: bool = True
    #: Suggested file name for save_file dialogs.
    suggested_name: str = ""

    def coerce(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, (list, tuple)):
            return os.pathsep.join(str(v) for v in value)
        return str(value)

    def paths(self, value: Any) -> list[Path]:
        """Split a stored value into individual paths (multi-select aware)."""
        raw = self.coerce(value)
        return [Path(p) for p in raw.split(os.pathsep) if p.strip()]

    def validate(self, value: Any) -> Optional[str]:
        base = super().validate(value)
        if base:
            return base
        raw = self.coerce(value)
        if not raw:
            return None
        if self.must_exist and self.mode != "save_file":
            for path in self.paths(raw):
                if not path.exists():
                    return f"Path does not exist: {path}"
                if self.mode == "directory" and not path.is_dir():
                    return f"Not a folder: {path}"
        return None


@dataclass
class ChoiceParam(Param):
    """A fixed set of values, rendered as a dropdown or a segmented button."""

    choices: Sequence[Option] = field(default_factory=list)
    #: dropdown | segmented | radio
    style: str = "dropdown"
    searchable: bool = False

    def coerce(self, value: Any) -> Any:
        allowed = [option.value for option in self.choices]
        if value in allowed:
            return value
        # Round-tripping through JSON turns enums into strings; match on text.
        text = str(value)
        for candidate in allowed:
            if str(candidate) == text:
                return candidate
        return self.default

    def label_for(self, value: Any) -> str:
        for option in self.choices:
            if option.value == value:
                return option.label
        return str(value)


@dataclass
class BoolParam(Param):
    """A switch."""

    default: bool = False
    #: switch | checkbox
    style: str = "switch"

    def coerce(self, value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)


@dataclass
class IntParam(Param):
    """A whole number, optionally bounded."""

    default: int = 0
    min: Optional[int] = None
    max: Optional[int] = None
    step: int = 1
    suffix: str = ""
    #: field | slider
    style: str = "field"

    def coerce(self, value: Any) -> int:
        try:
            result = int(float(value))
        except (TypeError, ValueError):
            return int(self.default or 0)
        if self.min is not None:
            result = max(self.min, result)
        if self.max is not None:
            result = min(self.max, result)
        return result

    def validate(self, value: Any) -> Optional[str]:
        base = super().validate(value)
        if base:
            return base
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            return f"{self.label} must be a whole number"
        if self.min is not None and number < self.min:
            return f"{self.label} must be at least {self.min}"
        if self.max is not None and number > self.max:
            return f"{self.label} must be at most {self.max}"
        return None


@dataclass
class FloatParam(Param):
    """A decimal number, optionally bounded."""

    default: float = 0.0
    min: Optional[float] = None
    max: Optional[float] = None
    step: float = 0.1
    decimals: int = 2
    suffix: str = ""
    style: str = "field"

    def coerce(self, value: Any) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return float(self.default or 0.0)
        if self.min is not None:
            result = max(self.min, result)
        if self.max is not None:
            result = min(self.max, result)
        return round(result, self.decimals)

    def validate(self, value: Any) -> Optional[str]:
        base = super().validate(value)
        if base:
            return base
        try:
            number = float(value)
        except (TypeError, ValueError):
            return f"{self.label} must be a number"
        if self.min is not None and number < self.min:
            return f"{self.label} must be at least {self.min}"
        if self.max is not None and number > self.max:
            return f"{self.label} must be at most {self.max}"
        return None


@dataclass
class ColorParam(Param):
    """A colour, stored as a Flet colour name or #rrggbb string."""

    default: str = "#ffffff"
    swatches: Sequence[str] = field(default_factory=tuple)

    def coerce(self, value: Any) -> str:
        return str(value or self.default)


# ----------------------------------------------------------------------
# Schema helpers
# ----------------------------------------------------------------------

def defaults(params: Iterable[Param]) -> dict[str, Any]:
    """The default value of every param, keyed by param key."""
    return {param.key: param.default for param in params}


def coerce_values(params: Iterable[Param], values: dict[str, Any]) -> dict[str, Any]:
    """Coerce a raw dict (from JSON, the UI, or a CLI) into typed values.

    Keys absent from `values` fall back to the param default, so a schema that
    grew a new field still loads an old history record.
    """
    result: dict[str, Any] = {}
    for param in params:
        raw = values.get(param.key, param.default)
        result[param.key] = param.coerce(raw)
    return result


def validate_values(params: Iterable[Param],
                    values: dict[str, Any]) -> dict[str, str]:
    """Validate every visible param; returns {key: error} for the failures."""
    errors: dict[str, str] = {}
    for param in params:
        if not is_visible(param, values):
            continue
        error = param.validate(values.get(param.key, param.default))
        if error:
            errors[param.key] = error
    return errors


def is_visible(param: Param, values: dict[str, Any]) -> bool:
    """Whether a param's `depends_on` condition is currently satisfied."""
    if param.depends_on is None:
        return True
    other_key, expected = param.depends_on
    actual = values.get(other_key)
    if isinstance(expected, (list, tuple, set)):
        return actual in expected
    return actual == expected


def group_params(params: Iterable[Param]) -> dict[str, list[Param]]:
    """Bucket params by their `group`, preserving declaration order."""
    grouped: dict[str, list[Param]] = {}
    for param in params:
        grouped.setdefault(param.group, []).append(param)
    return grouped


def summarise(params: Iterable[Param], values: dict[str, Any],
              limit: int = 3) -> str:
    """A short one-line description of a value set, for history rows."""
    parts: list[str] = []
    for param in params:
        value = values.get(param.key)
        if _is_blank(value) or value == param.default:
            continue
        if isinstance(param, PathParam):
            value = Path(str(value).split(os.pathsep)[0]).name or value
        elif isinstance(param, ChoiceParam):
            value = param.label_for(value)
        parts.append(f"{param.label}: {value}")
        if len(parts) >= limit:
            break
    return "  ".join(parts) or "defaults"


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())
