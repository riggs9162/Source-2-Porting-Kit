"""
Param schema -> Flet controls.

:class:`ParamForm` takes a list of :class:`~s2kit.core.params.Param` and gives
back a live form: grouped cards, per-field validation messages, dependency
visibility, browse buttons wired to the shared file picker with last-directory
memory, reset-to-default, and a values dict that round-trips through JSON.

The same class renders a tool's Run tab, a tool's Settings tab and the global
Preferences page. Bind mode decides where values live:

* ``ParamForm(...)``                      - values kept in the form (tool run)
* ``ParamForm(..., store=...)``           - values written straight through to
                                            a settings store (preferences)
"""

from __future__ import annotations

import logging
import asyncio
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

import flet as ft

from s2kit.core import params as P
from s2kit.core.preferences import PrefStore
from s2kit.ui import common, theme
from s2kit.ui.context import AppContext

logger = logging.getLogger(__name__)


class FieldBinding:
    """One param and the control(s) that edit it."""

    def __init__(self, param: P.Param, control: ft.Control,
                 getter: Callable[[], Any], setter: Callable[[Any], None],
                 row: ft.Control, error: ft.Text):
        self.param = param
        self.control = control
        self.get = getter
        self.set = setter
        self.row = row
        self.error = error

    def show_error(self, message: str) -> None:
        self.error.value = message
        self.error.visible = bool(message)
        _safe_update(self.error)


class ParamForm:
    """Builds and drives a form for a param schema."""

    def __init__(self, ctx: AppContext, schema: Sequence[P.Param], *,
                 owner_id: str = "form",
                 store: Optional[PrefStore] = None,
                 on_change: Optional[Callable[[str, Any], None]] = None,
                 on_values_changed: Optional[Callable[[], None]] = None,
                 show_advanced: bool = True,
                 columns: int = 1):
        self.ctx = ctx
        self.schema = list(schema)
        self.owner_id = owner_id
        self.store = store
        self.on_change = on_change
        self.on_values_changed = on_values_changed
        self.show_advanced = show_advanced
        self.columns = max(1, columns)
        self._values: dict[str, Any] = P.defaults(self.schema)
        self._bindings: dict[str, FieldBinding] = {}
        #: (group card, its params) so a card whose every row is hidden by
        #: `depends_on` can be hidden too - see `_apply_visibility`.
        self._group_cards: list[tuple[ft.Control, list[P.Param]]] = []
        self.scroll_host = None
        if store is not None:
            for param in self.schema:
                self._values[param.key] = param.coerce(
                    store.get(param.key, param.default))

    # ------------------------------------------------------------------
    # Values
    # ------------------------------------------------------------------

    def values(self) -> dict[str, Any]:
        """Current values, coerced to their declared types."""
        return P.coerce_values(self.schema, self._values)

    def set_values(self, values: dict[str, Any], *, notify: bool = False) -> None:
        """Load a value set into the form (history restore, preset, reset)."""
        coerced = P.coerce_values(self.schema, values)
        for key, value in coerced.items():
            self._values[key] = value
            binding = self._bindings.get(key)
            if binding is not None:
                binding.set(value)
                binding.show_error("")
            if notify:
                self._emit(key, value)
        self._apply_visibility()
        if self.on_values_changed is not None:
            self.on_values_changed()

    def reset(self) -> None:
        self.set_values(P.defaults(self.schema), notify=True)

    def validate(self, custom_errors: Optional[dict[str, str]] = None) -> dict[str, str]:
        """Validate, paint the errors, and return them keyed by param."""
        errors = P.validate_values(self.schema, self.values())
        errors.update(custom_errors or {})
        for param in self.schema:
            binding = self._bindings.get(param.key)
            if binding is not None:
                binding.show_error(errors.get(param.key, ""))
        return errors

    async def reveal_error(self, key: str) -> None:
        """Expand the invalid field's group, scroll to it, and focus its editor."""
        binding = self._bindings.get(key)
        if binding is None:
            return
        for card, params in self._group_cards:
            if binding.param in params and isinstance(card.content, ft.ExpansionTile):
                card.content.expanded = True
                _safe_update(card)
        await asyncio.sleep(0.2)
        control = binding.control
        if isinstance(control, ft.Row):
            control = control.controls[0]
        focus = getattr(control, "focus", None)
        actions = []
        if callable(focus):
            actions.append(focus())
        if self.scroll_host is not None:
            actions.append(self.scroll_host.scroll_to(
                scroll_key=f"{self.owner_id}:{key}", duration=200))
        if actions:
            await asyncio.gather(*actions)

    def summary(self) -> str:
        return P.summarise(self.schema, self.values())

    # ------------------------------------------------------------------
    # Building
    # ------------------------------------------------------------------

    def build(self) -> list[ft.Control]:
        """One card per param group, advanced groups collapsed.

        A tool with many groups (the glTF converter has seven) is laid out in
        two columns on a wide window rather than one long scroll - see the
        `form_columns` preference.
        """
        grouped = P.group_params(self.schema)
        blocks: list[ft.Control] = []
        self._group_cards = []
        for group_name, group_params in grouped.items():
            visible = [p for p in group_params
                       if self.show_advanced or not p.advanced]
            if not visible:
                continue
            rows = [self._build_row(param) for param in visible]
            all_advanced = all(param.advanced for param in visible)
            if all_advanced and len(grouped) > 1:
                card = common.collapsible(group_name, *rows, icon_name="TUNE")
            else:
                card = common.section(group_name, *rows)
            blocks.append(card)
            self._group_cards.append((card, visible))
        self._apply_visibility()
        return self._lay_out(blocks)

    def _lay_out(self, blocks: list[ft.Control]) -> list[ft.Control]:
        """Arrange group cards in one or two responsive columns."""
        mode = str(self.ctx.pref("form_columns", "auto"))
        if mode == "one" or len(blocks) < 3:
            return blocks
        for block in blocks:
            # 12-column grid: full width on small windows, half on large ones.
            # "two" keeps the split even when the window is narrow.
            block.col = {"xs": 12, "lg": 6} if mode == "auto" else {"xs": 6}
        return [ft.ResponsiveRow(blocks, spacing=12, run_spacing=12,
                                 vertical_alignment=ft.CrossAxisAlignment.START)]

    def build_flat(self) -> list[ft.Control]:
        """Rows without group cards - used inside an existing card."""
        rows = [self._build_row(param) for param in self.schema
                if self.show_advanced or not param.advanced]
        self._apply_visibility()
        return rows

    def _build_row(self, param: P.Param) -> ft.Control:
        error = ft.Text("", size=11, color=ft.Colors.RED_400, visible=False,
                        no_wrap=False)
        control, getter, setter = self._build_control(param)

        label_row: list[ft.Control] = [
            ft.Text(param.label, size=12, weight=ft.FontWeight.W_600)
        ]
        if param.required:
            label_row.append(ft.Text("*", size=12, color=ft.Colors.RED_400))
        if param.restart_required:
            label_row.append(common.pill("restart", colour=ft.Colors.AMBER_400))
        if param.advanced:
            label_row.append(common.pill("advanced", colour=ft.Colors.OUTLINE))
        label_row.append(ft.Container(expand=True))
        # A switch belongs on the label line: giving every boolean its own
        # row triples the height of option-heavy groups for no benefit.
        inline = isinstance(param, P.BoolParam)
        if inline:
            label_row.append(control)
        label_row.append(
            ft.IconButton(ft.Icons.RESTART_ALT, icon_size=13,
                          tooltip="Reset to default",
                          on_click=lambda _e, p=param: self._reset_one(p))
        )

        parts: list[ft.Control] = [
            ft.Row(label_row, spacing=6, tight=True,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ]
        if not inline:
            parts.append(control)
        if param.help:
            parts.append(common.caption(param.help))
        parts.append(error)

        # A stable key per field. Without one, swapping the form for another
        # schema (switching preference sections) lets Flutter recycle the
        # widget at the same position for a different param - and the recycled
        # widget reports its state to the old param's handler, silently
        # writing a value the user never picked.
        row = ft.Column(parts, spacing=4, tight=True,
                        key=ft.ScrollKey(f"{self.owner_id}:{param.key}"))
        control.key = ft.ValueKey(f"{self.owner_id}:{param.key}:control")
        self._bindings[param.key] = FieldBinding(param, control, getter, setter,
                                                 row, error)
        return row

    # ------------------------------------------------------------------
    # Control factories
    # ------------------------------------------------------------------

    def _field_key(self, param: P.Param, suffix: str = "") -> ft.ValueKey:
        """A key unique to this form and param, so widgets are never recycled
        across schemas (see the comment in `_build_row`)."""
        tail = f":{suffix}" if suffix else ""
        return ft.ValueKey(f"{self.owner_id}:{param.key}{tail}")

    def _build_control(self, param: P.Param):
        if isinstance(param, P.PathParam):
            return self._path_control(param)
        if isinstance(param, P.BoolParam):
            return self._bool_control(param)
        if isinstance(param, P.ChoiceParam):
            return self._choice_control(param)
        if isinstance(param, P.IntParam):
            return self._number_control(param, integer=True)
        if isinstance(param, P.FloatParam):
            return self._number_control(param, integer=False)
        if isinstance(param, P.ColorParam):
            return self._colour_control(param)
        return self._text_control(param)

    def _text_control(self, param: P.Param):
        multiline = isinstance(param, P.TextParam) and param.lines > 1
        field = ft.TextField(
            value=str(self._values.get(param.key) or ""),
            hint_text=getattr(param, "hint", "") or None,
            multiline=multiline,
            min_lines=getattr(param, "lines", 1) if multiline else 1,
            max_lines=getattr(param, "max_lines", 8) if multiline else 1,
            password=bool(getattr(param, "password", False)),
            can_reveal_password=bool(getattr(param, "password", False)),
            dense=True,
            text_size=12,
            width=param.width,
            expand=None if param.width else True,
            key=self._field_key(param, "input"),
            prefix_icon=(theme.icon(param.prefix_icon)
                         if getattr(param, "prefix_icon", "") else None),
            on_change=lambda e, p=param: self._changed(p, e.control.value),
        )
        getter, setter = (lambda: field.value,
                          lambda v: _assign(field, "value", str(v or "")))
        if param.width:
            return field, getter, setter
        # A bare TextField in a Column sizes to its content, which leaves a
        # multiline box a couple of hundred pixels wide. The Row gives it a
        # horizontal flex parent so it fills the card like every other field.
        return ft.Row([field], spacing=0), getter, setter

    def _path_control(self, param: P.PathParam):
        field = ft.TextField(
            value=str(self._values.get(param.key) or ""),
            hint_text=_path_hint(param),
            dense=True,
            text_size=12,
            expand=True,
            key=self._field_key(param, "input"),
            on_change=lambda e, p=param: self._changed(p, e.control.value),
        )
        browse = ft.IconButton(
            _path_icon(param),
            tooltip=_path_hint(param),
            icon_size=18,
            on_click=lambda _e, p=param: self.ctx.page.run_task(self._browse, p),
        )
        reveal = ft.IconButton(
            ft.Icons.FOLDER_OPEN, tooltip="Reveal in file manager", icon_size=16,
            on_click=lambda _e, p=param: self._reveal(p),
        )
        row = ft.Row([field, browse, reveal], spacing=2,
                     vertical_alignment=ft.CrossAxisAlignment.CENTER)
        return row, lambda: field.value, lambda v: _assign(field, "value", str(v or ""))

    def _bool_control(self, param: P.BoolParam):
        current = bool(self._values.get(param.key))
        if param.style == "checkbox":
            widget = ft.Checkbox(
                value=current, label="", key=self._field_key(param, "input"),
                on_change=lambda e, p=param: self._changed(p, e.control.value),
            )
        else:
            widget = ft.Switch(
                value=current, key=self._field_key(param, "input"),
                on_change=lambda e, p=param: self._changed(p, e.control.value),
            )
        return widget, lambda: widget.value, lambda v: _assign(widget, "value", bool(v))

    def _choice_control(self, param: P.ChoiceParam):
        current = self._values.get(param.key, param.default)

        if param.style == "segmented" and len(param.choices) <= 4:
            widget = ft.SegmentedButton(
                key=self._field_key(param, "input"),
                segments=[ft.Segment(value=str(option.value),
                                     label=ft.Text(option.label, size=12))
                          for option in param.choices],
                selected=[str(current)],
                allow_empty_selection=False,
                show_selected_icon=False,
                on_change=lambda e, p=param: self._changed(
                    p, _first(e.control.selected)),
            )
            return (widget,
                    lambda: _first(widget.selected),
                    lambda v: _assign(widget, "selected", [str(v)]))

        if param.style == "radio":
            group = ft.RadioGroup(
                value=str(current), key=self._field_key(param, "input"),
                content=ft.Column(
                    [ft.Radio(value=str(option.value), label=option.label)
                     for option in param.choices],
                    spacing=0, tight=True,
                ),
                on_change=lambda e, p=param: self._changed(p, e.control.value),
            )
            return group, lambda: group.value, lambda v: _assign(group, "value", str(v))

        dropdown = ft.Dropdown(
            value=str(current), key=self._field_key(param, "input"),
            options=[ft.DropdownOption(key=str(option.value), text=option.label)
                     for option in param.choices],
            dense=True,
            text_size=12,
            width=param.width,
            enable_filter=param.searchable,
            enable_search=param.searchable,
            editable=param.searchable,
            on_select=lambda e, p=param: self._changed(p, e.control.value),
        )
        return (dropdown, lambda: dropdown.value,
                lambda v: _assign(dropdown, "value", str(v)))

    def _number_control(self, param: Any, *, integer: bool):
        current = self._values.get(param.key, param.default)

        if param.style == "slider" and param.min is not None and param.max is not None:
            readout = ft.Text(_number_text(current, param, integer), size=12,
                              width=70, text_align=ft.TextAlign.RIGHT)
            divisions = None
            if integer and param.step:
                span = int(param.max) - int(param.min)
                divisions = max(1, span // max(1, int(param.step)))
            slider = ft.Slider(
                key=self._field_key(param, "input"),
                value=float(current or 0), min=float(param.min),
                max=float(param.max), divisions=divisions, expand=True,
                on_change=lambda e, p=param: self._slider_changed(p, e.control.value,
                                                                  readout, integer),
            )
            row = ft.Row([slider, readout], spacing=8,
                         vertical_alignment=ft.CrossAxisAlignment.CENTER)

            def setter(value: Any) -> None:
                _assign(slider, "value", float(value or 0))
                _assign(readout, "value", _number_text(value, param, integer))

            return row, lambda: slider.value, setter

        field = ft.TextField(
            value=str(current),
            dense=True,
            text_size=12,
            width=param.width or 160,
            key=self._field_key(param, "input"),
            keyboard_type=ft.KeyboardType.NUMBER,
            input_filter=(ft.NumbersOnlyInputFilter() if integer else None),
            suffix=(ft.Text(param.suffix, size=11,
                            color=ft.Colors.ON_SURFACE_VARIANT)
                    if param.suffix else None),
            on_change=lambda e, p=param: self._changed(p, e.control.value),
        )
        return field, lambda: field.value, lambda v: _assign(field, "value", str(v))

    def _colour_control(self, param: P.ColorParam):
        field = ft.TextField(
            value=str(self._values.get(param.key) or param.default),
            dense=True, text_size=12, width=param.width or 160,
            key=self._field_key(param, "input"),
            on_change=lambda e, p=param: self._changed(p, e.control.value),
        )
        swatches = ft.Row(
            spacing=4, tight=True,
            controls=[
                ft.Container(
                    width=18, height=18, bgcolor=swatch,
                    border_radius=ft.BorderRadius.all(4),
                    border=ft.Border.all(1, ft.Colors.OUTLINE),
                    on_click=lambda _e, s=swatch, p=param: (
                        _assign(field, "value", s), self._changed(p, s)),
                )
                for swatch in param.swatches
            ],
        )
        row = ft.Row([field, swatches], spacing=8,
                     vertical_alignment=ft.CrossAxisAlignment.CENTER)
        return row, lambda: field.value, lambda v: _assign(field, "value", str(v))

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _changed(self, param: P.Param, raw: Any) -> None:
        value = param.coerce(raw)
        if value == self._values.get(param.key):
            return          # echo from a control that did not really change
        self._values[param.key] = value
        binding = self._bindings.get(param.key)
        if binding is not None:
            binding.show_error(param.validate(value) or "")
        if self.store is not None:
            self.store.set(param.key, value)
        self._emit(param.key, value)
        self._apply_visibility()

    def _slider_changed(self, param: Any, raw: Any, readout: ft.Text,
                        integer: bool) -> None:
        value = param.coerce(raw)
        readout.value = _number_text(value, param, integer)
        _safe_update(readout)
        self._changed(param, value)

    def _emit(self, key: str, value: Any) -> None:
        if self.on_change is not None:
            try:
                self.on_change(key, value)
            except Exception:
                logger.exception("Form change handler failed for %s", key)

    def _reset_one(self, param: P.Param) -> None:
        self._changed(param, param.default)
        binding = self._bindings.get(param.key)
        if binding is not None:
            binding.set(param.default)

    def _apply_visibility(self) -> None:
        """Honour `depends_on` after every change.

        Group cards follow their rows: a group whose every param is hidden -
        a tool with two mutually exclusive modes has one of these at all times -
        would otherwise render as an empty titled card.
        """
        values = self._values
        for binding in self._bindings.values():
            should_show = P.is_visible(binding.param, values)
            if binding.row.visible != should_show:
                binding.row.visible = should_show
                _safe_update(binding.row)

        for card, params in self._group_cards:
            should_show = any(P.is_visible(param, values) for param in params)
            if card.visible != should_show:
                card.visible = should_show
                _safe_update(card)

    def _reveal(self, param: P.PathParam) -> None:
        raw = str(self._values.get(param.key) or "")
        if not raw:
            self.ctx.toast("Nothing to reveal yet", level="WARNING")
            return
        first = raw.split(os.pathsep)[0]
        target = Path(first)
        self.ctx.reveal(target if target.exists() else target.parent)

    async def _browse(self, param: P.PathParam) -> None:
        """Open the right dialog for a path param and remember the folder."""
        initial = (self.ctx.settings.last_dir(self.owner_id, param.key)
                   or _existing_dir(str(self._values.get(param.key) or "")))
        extensions = tuple(param.extensions)

        if param.mode == "directory":
            picked = await self.ctx.pick_directory(
                title=f"Select {param.label}", initial_directory=initial)
            paths = [picked] if picked else []
        elif param.mode == "save_file":
            picked = await self.ctx.save_file(
                title=f"Save {param.label}",
                file_name=param.suggested_name,
                initial_directory=initial, extensions=extensions)
            paths = [picked] if picked else []
        else:
            paths = await self.ctx.pick_file(
                title=f"Select {param.label}", initial_directory=initial,
                extensions=extensions, multiple=param.mode == "open_files")

        if not paths:
            return
        value = os.pathsep.join(str(path) for path in paths)
        binding = self._bindings.get(param.key)
        if binding is not None:
            binding.set(value)
        self._changed(param, value)

        first = paths[0]
        folder = first if first.is_dir() else first.parent
        self.ctx.settings.remember_dir(self.owner_id, param.key, str(folder))
        self.ctx.settings.push_recent_path(str(first))


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _assign(control: ft.Control, attribute: str, value: Any) -> None:
    setattr(control, attribute, value)
    _safe_update(control)


def _safe_update(control: Any) -> None:
    try:
        control.update()
    except Exception:
        pass


def _first(selection: Iterable[Any]) -> Any:
    for item in selection or ():
        return item
    return None


def _number_text(value: Any, param: Any, integer: bool) -> str:
    try:
        number = int(float(value)) if integer else float(value)
    except (TypeError, ValueError):
        number = param.default
    text = f"{number}" if integer else f"{number:.{param.decimals}f}"
    return f"{text} {param.suffix}".strip()


def _path_icon(param: P.PathParam) -> Any:
    return {
        "directory": ft.Icons.FOLDER,
        "save_file": ft.Icons.SAVE,
        "open_files": ft.Icons.LIBRARY_ADD,
    }.get(param.mode, ft.Icons.INSERT_DRIVE_FILE)


def _path_hint(param: P.PathParam) -> str:
    return {
        "directory": "Browse for a folder",
        "save_file": "Choose where to save",
        "open_files": "Select one or more files",
    }.get(param.mode, "Browse for a file")


def _existing_dir(raw: str) -> str:
    if not raw:
        return ""
    candidate = Path(raw.split(os.pathsep)[0])
    if candidate.is_dir():
        return str(candidate)
    if candidate.parent.is_dir():
        return str(candidate.parent)
    return ""
