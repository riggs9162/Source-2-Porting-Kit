"""
Tool package.

:func:`discover` imports every module in this package so their `@tool`
decorators run, then publishes each tool's preference block. Dropping a new
`*.py` in here is all it takes to add a tool - there is no central list to
edit.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

from s2kit.core import registry

logger = logging.getLogger(__name__)


def discover() -> int:
    """Import every tool module and register its preferences. Returns the count."""
    for module in pkgutil.iter_modules(__path__):
        if module.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{__name__}.{module.name}")
        except Exception:
            # One broken tool must not stop the app from starting.
            logger.exception("Could not import tool module %r", module.name)

    for spec in registry.all_tools():
        register = getattr(spec.factory, "register_preferences", None)
        if callable(register):
            try:
                register()
            except Exception:
                logger.exception("Could not register preferences for %s", spec.id)
    return registry.count()
