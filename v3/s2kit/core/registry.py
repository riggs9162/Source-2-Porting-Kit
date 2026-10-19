"""
Tool registry.

A tool announces itself with the :func:`tool` class decorator; the shell reads
the registry to build the sidebar, the command palette and the Preferences
page. Icons are stored as `ft.Icons` member names (strings) so this module
stays framework-free.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

logger = logging.getLogger(__name__)

#: Sidebar grouping, in display order. A tool with an unknown category is
#: appended under its own heading rather than being silently dropped.
CATEGORY_ORDER: tuple[str, ...] = (
    "Models",
    "Materials",
    "Materials / PBR",
    "Sounds",
    "Maps",
    "Utility",
    "Inspect",
)


@dataclass
class ToolSpec:
    """Everything the shell knows about a tool before it is opened."""

    id: str
    title: str
    category: str
    factory: Callable[..., Any]
    icon: str = "BUILD"
    description: str = ""
    keywords: Sequence[str] = field(default_factory=tuple)
    #: Short badge shown next to the title ("beta", "wip", ...).
    badge: str = ""
    experimental: bool = False
    order: int = 100
    definition: Any = None

    def matches(self, query: str) -> bool:
        needle = query.strip().lower()
        if not needle:
            return True
        haystack = " ".join([
            self.title, self.id, self.category, self.description,
            " ".join(self.keywords),
        ]).lower()
        return all(part in haystack for part in needle.split())


_REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    """Add (or replace) a tool in the registry."""
    if spec.id in _REGISTRY:
        logger.debug("Replacing already-registered tool %s", spec.id)
    _REGISTRY[spec.id] = spec
    return spec


def tool(*, id: str, title: str, category: str = "Utility",
         icon: str = "BUILD", description: str = "",
         keywords: Sequence[str] = (), badge: str = "",
         experimental: bool = False, order: int = 100) -> Callable[[type], type]:
    """Register a shared declaration and its lazy page adapter.

    Example::

        @tool(id="vtf-inspect", title="VTF Inspector", category="Inspect",
              icon="IMAGE_SEARCH", description="Read VTF headers and mips.")
        class VtfInspector(ToolDefinition):
            ...
    """

    def decorator(cls: type) -> type:
        spec = ToolSpec(
            id=id, title=title, category=category,
            factory=getattr(cls, "create_page", cls), icon=icon,
            description=description, keywords=tuple(keywords), badge=badge,
            experimental=experimental, order=order,
            definition=cls if hasattr(cls, "EXECUTOR") else None,
        )
        register(spec)
        # The page needs to know its own identity when it saves params.
        cls.SPEC = spec  # type: ignore[attr-defined]
        return cls

    return decorator


def all_tools() -> list[ToolSpec]:
    """Every registered tool, sorted by category order then title."""
    def sort_key(spec: ToolSpec) -> tuple[int, int, str]:
        try:
            category_index = CATEGORY_ORDER.index(spec.category)
        except ValueError:
            category_index = len(CATEGORY_ORDER)
        return (category_index, spec.order, spec.title.lower())

    return sorted(_REGISTRY.values(), key=sort_key)


def get(tool_id: str) -> Optional[ToolSpec]:
    return _REGISTRY.get(tool_id)


def categories() -> list[str]:
    """Categories that actually have tools, in display order."""
    present = {spec.category for spec in _REGISTRY.values()}
    ordered = [name for name in CATEGORY_ORDER if name in present]
    extra = sorted(present - set(CATEGORY_ORDER))
    return ordered + extra


def by_category() -> dict[str, list[ToolSpec]]:
    """Tools bucketed by category, in display order."""
    grouped: dict[str, list[ToolSpec]] = {name: [] for name in categories()}
    for spec in all_tools():
        grouped.setdefault(spec.category, []).append(spec)
    return grouped


def search(query: str) -> list[ToolSpec]:
    return [spec for spec in all_tools() if spec.matches(query)]


def count() -> int:
    return len(_REGISTRY)
