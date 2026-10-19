"""Framework-free tool declarations and lazy execution/UI adapters."""

from __future__ import annotations

import importlib
from typing import Any, TYPE_CHECKING

from s2kit.core import preferences

if TYPE_CHECKING:
    from s2kit.core.jobs import JobContext


def load_object(reference: str) -> Any:
    """Load a module attribute only when execution or a page needs it."""
    module, name = reference.split(":", 1)
    return getattr(importlib.import_module(module), name)


class ToolDefinition:
    """One schema shared by interactive pages and headless execution."""

    SPEC = None
    PARAMS = ()
    PREFERENCES = ()
    HELP = ""
    RUN_LABEL = "Run"
    RUN_ICON = "PLAY_ARROW"
    WRITES_FILES = True
    HEADLESS = True
    EXECUTOR = ""
    UI_CLASS = ""
    CUSTOM_VALIDATION = False

    def validate(self, values: dict[str, Any]) -> dict[str, str]:
        """Apply the executor's additional validation when it declares any."""
        if self.CUSTOM_VALIDATION:
            return load_object(self.EXECUTOR)().validate(values)
        return {}

    def run_job(self, ctx: JobContext) -> None:
        """Run the lazily imported, framework-free executor."""
        load_object(self.EXECUTOR)().run_job(ctx)

    @classmethod
    def register_preferences(cls) -> None:
        """Publish the same preference declaration to every interface."""
        if cls.PREFERENCES and cls.SPEC:
            preferences.register_tool_section(
                cls.SPEC.id, cls.SPEC.title, cls.PREFERENCES,
                icon=cls.SPEC.icon, description=cls.SPEC.description)

    @classmethod
    def create_page(cls, **kwargs: Any) -> Any:
        """Load Flet only when a caller actually opens a tool page."""
        if cls.UI_CLASS:
            page_class = load_object(cls.UI_CLASS)
            page_class.SPEC = cls.SPEC
            return page_class(**kwargs)
        from s2kit.ui.tool_page import ToolPage

        definition = cls()
        attributes = {key: getattr(cls, key) for key in (
            "SPEC", "PARAMS", "PREFERENCES", "HELP", "RUN_LABEL", "RUN_ICON", "WRITES_FILES")}
        attributes["run_job"] = lambda self, ctx: definition.run_job(ctx)
        attributes["validate"] = lambda self, values: definition.validate(values)
        page_class = type(cls.__name__ + "Page", (ToolPage,), attributes)
        return page_class(**kwargs)
