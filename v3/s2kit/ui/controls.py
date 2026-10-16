"""
Helpers and rules for writing custom Flet controls.

Two Flet 0.86 behaviours shape every control in this package:

**1. Python-side fields must be declared with :func:`state`.**
`@ft.control` turns a control class into a dataclass whose fields are
serialised and sent to the Dart client. Anything that is Python-side state -
the app context, a settings object, a callback, a parsed schema - has to be
declared with `state()` or the first `page.update()` dies with
"can not serialize" on the object.

**2. Custom controls subclass `ft.Container`, never `ft.Column`/`ft.Row`.**
A `@ft.control` subclass of a flex control does not apply `expand` to its
children: they collapse to zero along the main axis and stop being bounded on
the cross axis, so anything wide (a texture, a waveform, a long path field)
overflows the window. Verified against 0.86.1 - a plain `ft.Column` lays the
same children out correctly, a subclass of it does not, and neither
`@ft.control("Column")` nor `isolated=True` changes that.

The working shape, used by every control here::

    @ft.control
    class MyPane(ft.Container):
        def init(self):
            self.expand = True

        def build(self):
            self.content = ft.Column(
                expand=True,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[...],
            )

`Container` has no flex-children semantics of its own, so the plain `Column`
inside it behaves normally while `expand` on the container is still honoured
by whatever holds it.
"""

from __future__ import annotations

from dataclasses import field
from typing import Any, Callable, Optional


def state(default: Any = None, *,
          factory: Optional[Callable[[], Any]] = None) -> Any:
    """A control field holding Python-side state, excluded from serialisation.

    Mirrors Flet's own `skip_field()` (`metadata={"skip": True}`) but also
    accepts a default factory, which mutable state needs.
    """
    if factory is not None:
        return field(default_factory=factory, metadata={"skip": True})
    return field(default=default, metadata={"skip": True})


def is_ready(control: Any) -> bool:
    """Whether a control's `build()` has run and its children exist.

    Flet mounts controls progressively: a parent's `build()` runs before its
    children are mounted, so a parent hook (or a job event arriving early) can
    reach a child whose own `build()` has not happened yet. The shared panes
    keep their data in plain Python state, set `_ready` at the end of `build()`
    and render from that state, so an early call updates the data and the
    first build paints it.
    """
    return bool(getattr(control, "_ready", False))
