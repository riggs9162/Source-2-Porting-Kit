"""JSON schemas and strict external validation for shared tool declarations."""

from __future__ import annotations

import math
from typing import Any, Iterable

from s2kit.core import params as P


class RequestError(ValueError):
    """Invalid agent input with errors addressable by field name."""

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("; ".join(f"{key}: {message}" for key, message in errors.items()))


def parameter_schema(param: P.Param) -> dict:
    """Describe a declared parameter without loading its executor."""
    result: dict[str, Any] = {"title": param.label, "default": param.default,
                              "description": param.help, "x-group": param.group}
    if isinstance(param, P.ChoiceParam):
        result["enum"] = [option.value for option in param.choices]
        result["x-options"] = [{"value": option.value, "label": option.label} for option in param.choices]
    elif isinstance(param, P.BoolParam):
        result["type"] = "boolean"
    elif isinstance(param, (P.IntParam, P.FloatParam)):
        result["type"] = "integer" if isinstance(param, P.IntParam) else "number"
        if param.min is not None:
            result["minimum"] = param.min
        if param.max is not None:
            result["maximum"] = param.max
    else:
        result["type"] = "string"
    if isinstance(param, P.PathParam):
        result.update({"x-path-mode": param.mode, "x-must-exist": param.must_exist,
                       "x-extensions": list(param.extensions)})
        if param.mode == "open_files":
            result.pop("type", None)
            result["anyOf"] = [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]
    if param.depends_on:
        result["x-depends-on"] = list(param.depends_on)
    if param.required:
        result["minLength"] = 1
    return result


def schema(params: Iterable[P.Param]) -> dict:
    """Build an object schema including default-aware conditional requirements."""
    params = list(params)
    result: dict[str, Any] = {"type": "object", "additionalProperties": False,
                              "properties": {p.key: parameter_schema(p) for p in params}}
    required = [p.key for p in params if p.required and p.depends_on is None]
    if required:
        result["required"] = required
    defaults = P.defaults(params)
    for param in params:
        if not param.required or not param.depends_on:
            continue
        key, expected = param.depends_on
        values = list(expected) if isinstance(expected, (list, tuple, set)) else [expected]
        condition: dict = {"properties": {key: {"enum": values}}, "required": [key]}
        if defaults.get(key) in values:
            condition = {"anyOf": [condition, {"not": {"required": [key]}}]}
        result.setdefault("allOf", []).append({"if": condition, "then": {"required": [param.key]}})
    return result


def strict_values(params: Iterable[P.Param], raw: dict, *, validate_paths: bool = True) -> dict:
    """Reject external type/range errors before permissive UI coercion can hide them."""
    if not isinstance(raw, dict):
        raise RequestError({"request": "Expected a JSON object"})
    params = list(params)
    known = {param.key: param for param in params}
    errors = {str(key): "Unknown field" for key in raw if key not in known}
    for key, value in raw.items():
        param = known.get(key)
        if param is None:
            continue
        valid = True
        if isinstance(param, P.ChoiceParam):
            valid = any(value == option.value and isinstance(value, bool) == isinstance(option.value, bool)
                        for option in param.choices)
        elif isinstance(param, P.BoolParam):
            valid = isinstance(value, bool)
        elif isinstance(param, (P.IntParam, P.FloatParam)):
            valid = isinstance(value, (int, float)) and not isinstance(value, bool)
            valid = valid and math.isfinite(value)
            if valid and isinstance(param, P.IntParam):
                valid = int(value) == value
            if valid:
                valid = (param.min is None or value >= param.min) and (param.max is None or value <= param.max)
        elif isinstance(param, P.PathParam) and param.mode == "open_files":
            valid = isinstance(value, str) or isinstance(value, list) and all(isinstance(v, str) for v in value)
        else:
            valid = isinstance(value, str)
        if not valid:
            errors[key] = "Invalid type, choice or range; see the tool schema"
    if errors:
        raise RequestError(errors)
    values = P.coerce_values(params, raw)
    for param in params:
        if not P.is_visible(param, values):
            continue
        if not validate_paths and isinstance(param, P.PathParam):
            continue
        error = param.validate(values[param.key])
        if error:
            errors[param.key] = error
    if errors:
        raise RequestError(errors)
    return values
