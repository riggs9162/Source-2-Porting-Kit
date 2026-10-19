"""Lazy tool discovery through framework-free declarations."""

from __future__ import annotations

import importlib
import pkgutil

from s2kit.core import registry


def discover() -> int:
    """Register declarations and preferences without importing executors or UI."""
    from s2kit import definitions

    for module in pkgutil.iter_modules(definitions.__path__):
        if not module.name.startswith("_"):
            importlib.import_module(f"s2kit.definitions.{module.name}")
    for spec in registry.all_tools():
        if spec.definition:
            spec.definition.register_preferences()
    return registry.count()
