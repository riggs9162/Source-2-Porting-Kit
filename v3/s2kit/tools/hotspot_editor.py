"""
Hotspot Editor.

Authors the `.rect` files Hammer++ and Strata read to fit a material to a face.
Load a trim sheet, lay rectangles over the regions you want Fit to choose
between, save; the VMT gets its `%rectanglemap` line wired up at the same time.

This tool breaks the usual shape of a page in one way, on purpose: the Run tab
is the editor rather than a parameter form. Everything else stays - Save is a
real job, so it lands in the log, the history and the output table like any
other run, and the Preview tab opens what it wrote.

What it does that v2 did not:

* **Reads Source 2 `.rect` files too.** Both formats use the same extension and
  are otherwise unrelated; a Half-Life: Alyx sheet's rectangles can be imported
  and converted rather than redrawn by eye.
* **Fit preview.** Type a face size and see which rectangle Hammer would pick,
  and what its runners-up are. That is the question a sheet exists to answer.
* **Real region detection**, by alpha islands or by cutting along the seams
  between panels, instead of scanning a fixed grid and emitting whole cells.
* **Validation** for the mistakes that actually break a sheet, while leaving
  overlap alone - overlapping candidates is how Fit gets a choice.
"""

from __future__ import annotations

from s2kit.definitions.hotspot_editor import GRID_SIZES

import tempfile
from pathlib import Path
from typing import Any, Optional

import flet as ft

from s2kit.core.jobs import JobContext
from s2kit.definitions.hotspot_editor import HotspotEditor as Definition
from s2kit.pipelines import hotspot
from s2kit.pipelines.hotspot import Rect, RectSheet
from s2kit.ui import common, theme
from s2kit.ui.hotspot_canvas import HotspotCanvas
from s2kit.ui.tool_page import ToolPage


#: How many edits are kept. Snapshots are a list of small dataclasses, so this
#: costs almost nothing even on a busy sheet.
UNDO_DEPTH = 100

#: Arrow keys, as the (dx, dy) they nudge by. Flet spells them "Arrow Up";
#: the handler strips spaces before looking them up.
_ARROWS: dict[str, tuple[int, int]] = {
    "ARROWLEFT": (-1, 0), "ARROWRIGHT": (1, 0),
    "ARROWUP": (0, -1), "ARROWDOWN": (0, 1),
}


class HotspotEditor(ToolPage):
    """A canvas editor for `.rect` hotspot files."""

    SPEC = Definition.SPEC
    PARAMS = Definition.PARAMS
    PREFERENCES = Definition.PREFERENCES
    HELP = Definition.HELP
    RUN_LABEL = Definition.RUN_LABEL
    RUN_ICON = Definition.RUN_ICON
    WRITES_FILES = Definition.WRITES_FILES


    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def init(self):
        super().init()
        self._texture_path: Optional[Path] = None
        #: The VMT the sheet belongs to: opened directly, or found beside the
        #: texture. Save names the rect after it and wires its %rectanglemap.
        self._material_path: Optional[Path] = None
        self._rect_path: Optional[Path] = None
        self._preview_png: Optional[Path] = None
        self._undo: list[list[Rect]] = []
        self._redo: list[list[Rect]] = []
        self._scratch = Path(tempfile.mkdtemp(prefix="s2kit-hotspot-"))
        #: True while one of our text fields has focus, so the editing keys
        #: stay out of the way. Flet delivers keys page-wide, not to the focus.
        self._typing = False
        #: Rows of the rectangle list, reused rather than rebuilt. See
        #: _refresh_list.
        self._row_pool: list[ft.Container] = []
        self._fit_pool: list[ft.Row] = []

    # ------------------------------------------------------------------
    # The Run tab is the editor
    # ------------------------------------------------------------------

    def _run_tab(self) -> ft.Control:
        prefs = self.ctx.settings.tool_prefs(self.tool_id)
        self.canvas = HotspotCanvas(
            ctx=self.ctx, on_change=self._on_canvas_change,
            on_status=self._set_cursor_text)
        self._apply_view_prefs(prefs)

        self._cursor_text = common.caption("")
        self._sheet_text = common.caption("No texture loaded")
        self._target_text = common.caption("")
        self._rect_list = ft.Column(spacing=2, scroll=ft.ScrollMode.AUTO,
                                    expand=True)
        self._issues = ft.Column(spacing=2, tight=True)
        self._fit_result = ft.Column(spacing=2, tight=True)

        self._grid_choice = ft.Dropdown(
            width=104, dense=True, value="16",
            options=[ft.DropdownOption(str(size), f"{size} px")
                     for size in GRID_SIZES],
            on_select=self._on_grid_changed)
        self._snap_switch = ft.Switch(
            value=bool(prefs.get("snap", True)), label="Snap",
            on_change=lambda e: self.canvas.configure(snap=e.control.value))
        self._face_w = ft.TextField(
            label="Face width", value="256", width=110, dense=True,
            on_change=self._update_fit,
            on_focus=lambda _e: self._on_field_focus(True),
            on_blur=lambda _e: self._on_field_focus(False))
        self._face_h = ft.TextField(
            label="height", value="64", width=94, dense=True,
            on_change=self._update_fit,
            on_focus=lambda _e: self._on_field_focus(True),
            on_blur=lambda _e: self._on_field_focus(False))

        return ft.Row(
            expand=True, spacing=0,
            vertical_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Container(expand=True, padding=8, content=ft.Column(
                    expand=True, spacing=8,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                    controls=[self._toolbar(), self.canvas, self._statusbar()])),
                ft.VerticalDivider(width=1),
                ft.Container(width=330, padding=10, content=self._sidebar()),
            ])

    def _toolbar(self) -> ft.Control:
        def button(label, icon, handler, tooltip=""):
            return ft.OutlinedButton(label, icon=theme.icon(icon),
                                     tooltip=tooltip or label,
                                     on_click=lambda _e: handler())

        return ft.Row(spacing=6, wrap=True, controls=[
            button("Open material", "DESCRIPTION", self._pick_material,
                   "Open a .vmt - its $basetexture and .rect load with it"),
            button("Open texture", "IMAGE", self._pick_texture,
                   "Load the trim sheet to draw on"),
            button("Open .rect", "FOLDER_OPEN", self._pick_rect,
                   "Read an existing rect file - either format"),
            ft.VerticalDivider(width=6),
            button("Layout", "GRID_ON", self._show_layouts,
                   "Replace everything with a generated layout"),
            button("Detect", "AUTO_FIX_HIGH", self._detect,
                   "Find the regions automatically"),
            ft.VerticalDivider(width=6),
            button("Duplicate", "CONTENT_COPY", self._duplicate),
            button("Delete", "DELETE_OUTLINE", self._delete),
            button("Undo", "UNDO", self._undo_step),
            button("Redo", "REDO", self._redo_step),
            ft.VerticalDivider(width=6),
            self._grid_choice,
            self._snap_switch,
            ft.OutlinedButton("Fit view", icon=theme.icon("FIT_SCREEN"),
                              on_click=lambda _e: self.canvas.fit()),
        ])

    def _statusbar(self) -> ft.Control:
        return ft.Row(spacing=14, controls=[
            self._sheet_text, self._target_text, ft.Container(expand=True),
            self._cursor_text])

    def _sidebar(self) -> ft.Control:
        return ft.Column(expand=True, spacing=10, controls=[
            common.section(
                "Selected",
                ft.Row(spacing=4, wrap=True, controls=[
                    ft.Checkbox(label="rotate", data="rotate",
                                on_change=self._on_flag),
                    ft.Checkbox(label="reflect", data="reflect",
                                on_change=self._on_flag),
                    ft.Checkbox(label="alt", data="alt",
                                on_change=self._on_flag),
                ]),
                common.caption("Strata honours these. Hammer++ ignores them."),
                icon_name="TUNE"),
            common.section(
                "Fit preview",
                ft.Row(spacing=6, controls=[self._face_w, self._face_h]),
                self._fit_result,
                icon_name="FIND_IN_PAGE"),
            common.section("Rectangles", ft.Container(
                height=200, content=self._rect_list), icon_name="TABLE_ROWS"),
            common.section("Checks", self._issues, icon_name="RULE"),
        ], scroll=ft.ScrollMode.AUTO)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _pick_material(self) -> None:
        self.ctx.page.run_task(self._pick_material_async)

    async def _pick_material_async(self) -> None:
        picked = await self.ctx.pick_file(title="Open a material",
                                          extensions=("vmt",))
        if picked:
            self._load_texture(picked[0])

    def _pick_texture(self) -> None:
        self.ctx.page.run_task(self._pick_texture_async)

    async def _pick_texture_async(self) -> None:
        picked = await self.ctx.pick_file(
            title="Open a trim sheet",
            extensions=("png", "jpg", "jpeg", "tga", "bmp", "vtf", "vmt"))
        if picked:
            self._load_texture(picked[0])

    def _load_texture(self, path: Path) -> None:
        """Load a trim sheet, given either its VMT or the texture itself.

        Whichever is opened, the other half of the pair is found: a VMT's
        `$basetexture` is decoded onto the canvas, and a texture is matched to
        the VMT beside it that names it. The rect file then follows the
        material - see :func:`hotspot.rect_path_for_material` - and is loaded
        when one already exists.
        """
        from s2kit.media import images

        path = Path(path)
        material: Optional[Path] = None
        source = path
        if path.suffix.lower() == ".vmt":
            material = path
            source = hotspot.resolve_basetexture(path)
            if source is None:
                self.ctx.toast(f"Could not find the $basetexture of {path.name}",
                               level="ERROR")
                return
        else:
            material = hotspot.find_material_for_texture(path)

        # Decoded rather than preview-rendered: rectangles are authored in real
        # texel coordinates, so the canvas has to show the texture at its native
        # size. `render_preview` scales to its `max_size` in both directions,
        # which would silently rescale every coordinate the user then draws.
        try:
            image, info = images.load(source)
        except Exception as exc:
            self.ctx.toast(f"Could not read {source.name}: {exc}", level="ERROR")
            return

        self._preview_png = self._scratch / f"{source.stem}.png"
        image.save(self._preview_png)
        self._texture_path = source
        self._material_path = material

        sheet = self.canvas.sheet
        sheet.width, sheet.height = info.width, info.height
        self.canvas.load(sheet, self._preview_png)
        label = f"{source.name}   {info.width} x {info.height}"
        if material is not None:
            label = f"{material.name}  ->  {label}"
        self._sheet_text.value = label
        self._rect_path = (hotspot.rect_path_for_material(material, source)
                           if material is not None
                           else source.with_suffix(".rect"))
        self._show_target()

        if self._rect_path.is_file():
            self._load_rect(self._rect_path)
        else:
            self._on_canvas_change()
        self._safe_update(self._sheet_text)

    def _show_target(self) -> None:
        """Say in the status bar where Save will write."""
        self._target_text.value = ("" if self._rect_path is None
                                   else f"Saves to {self._rect_path.name}")
        self._safe_update(self._target_text)

    def _pick_rect(self) -> None:
        self.ctx.page.run_task(self._pick_rect_async)

    async def _pick_rect_async(self) -> None:
        picked = await self.ctx.pick_file(title="Open a rect file",
                                          extensions=("rect",))
        if picked:
            self._load_rect(picked[0])

    def _load_rect(self, path: Path) -> None:
        sheet = self.canvas.sheet
        try:
            loaded = hotspot.read_sheet(path, width=sheet.width,
                                        height=sheet.height)
        except hotspot.HotspotError as exc:
            self.ctx.toast(str(exc), level="ERROR")
            return

        self._push_undo()
        loaded.width = loaded.width or sheet.width
        loaded.height = loaded.height or sheet.height
        self.canvas.load(loaded, self._preview_png)
        self._rect_path = Path(path)
        self._show_target()
        note = (f"{len(loaded)} rect(s) from {Path(path).name}")
        if loaded.source_format == "source2":
            note += " - converted from Source 2 coordinates"
        self.ctx.toast(note)
        self._on_canvas_change()

    # ------------------------------------------------------------------
    # Editing
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Keyboard
    # ------------------------------------------------------------------

    def handle_key(self, event: ft.KeyboardEvent) -> bool:
        """Editor shortcuts. Returns True only for keys actually consumed.

        Flet delivers key events page-wide regardless of focus, so a plain
        Delete would eat a character out of the Fit fields if we did not track
        which of our own text fields has focus.
        """
        if self._typing or getattr(self, "canvas", None) is None:
            return False
        key = (event.key or "").upper().replace(" ", "")
        ctrl = bool(event.ctrl or event.meta)

        if ctrl:
            if key == "A":
                self.canvas.select(list(range(len(self.canvas.sheet.rects))))
                self._on_canvas_change()
            elif key == "D":
                self._duplicate()
            elif key == "Z" and event.shift:
                self._redo_step()
            elif key == "Z":
                self._undo_step()
            elif key == "Y":
                self._redo_step()
            elif key == "S":
                self.start_run()
            else:
                return False
            return True

        if key in ("DELETE", "BACKSPACE"):
            self._delete()
        elif key == "ESCAPE":
            self.canvas.select([])
            self._on_canvas_change()
        elif key == "F":
            self.canvas.fit()
        elif key == "G":
            self._toggle_grid()
        elif key in _ARROWS:
            dx, dy = _ARROWS[key]
            step = self.canvas._grid if event.shift else 1
            self._nudge(dx * step, dy * step)
        else:
            return False
        return True

    def _toggle_grid(self) -> None:
        store = self.ctx.settings.tool_pref_store(self.tool_id)
        show = not bool(store.all().get("show_grid", True))
        store.set("show_grid", show)
        self.canvas.configure(show_grid=show)

    def _nudge(self, dx: int, dy: int) -> None:
        """Move the selection by whole pixels, ignoring snap.

        Nudging is what you reach for when snap has put a rectangle one pixel
        out, so it deliberately does not re-snap.
        """
        from dataclasses import replace

        selection = self.canvas.selection
        if not selection or not (dx or dy):
            return
        self._push_undo()
        rects = list(self.canvas.sheet.rects)
        width, height = self.canvas.sheet.size
        for index in selection:
            rect = rects[index]
            moved = replace(rect, x=rect.x + dx, y=rect.y + dy)
            rects[index] = moved.clamped(width, height) if width else moved
        self.canvas.set_rects(rects, select=selection)
        self._on_canvas_change()

    def _on_field_focus(self, focused: bool) -> None:
        self._typing = focused

    def _push_undo(self) -> None:
        """Snapshot before a change. Selection alone never pushes.

        v2 snapshotted on every click, which filled a 50-deep stack with
        identical states and made Undo useless after a bit of clicking around.
        """
        from dataclasses import replace

        self._undo.append([replace(rect) for rect in self.canvas.sheet.rects])
        del self._undo[:-UNDO_DEPTH]
        self._redo.clear()

    def _undo_step(self) -> None:
        if not self._undo:
            return
        from dataclasses import replace

        self._redo.append([replace(r) for r in self.canvas.sheet.rects])
        self.canvas.set_rects(self._undo.pop())
        self._on_canvas_change()

    def _redo_step(self) -> None:
        if not self._redo:
            return
        from dataclasses import replace

        self._undo.append([replace(r) for r in self.canvas.sheet.rects])
        self.canvas.set_rects(self._redo.pop())
        self._on_canvas_change()

    def _delete(self) -> None:
        selection = set(self.canvas.selection)
        if not selection:
            return
        self._push_undo()
        self.canvas.set_rects([rect for index, rect
                               in enumerate(self.canvas.sheet.rects)
                               if index not in selection])
        self._on_canvas_change()

    def _duplicate(self) -> None:
        from dataclasses import replace

        selection = self.canvas.selection
        if not selection:
            return
        self._push_undo()
        rects = list(self.canvas.sheet.rects)
        added: list[int] = []
        for index in selection:
            source = rects[index]
            rects.append(replace(source, x=source.x + 8, y=source.y + 8))
            added.append(len(rects) - 1)
        self.canvas.set_rects(rects, select=added)
        self._on_canvas_change()

    def _on_flag(self, event: ft.ControlEvent) -> None:
        from dataclasses import replace

        selection = self.canvas.selection
        if not selection:
            return
        field, value = event.control.data, bool(event.control.value)
        self._push_undo()
        rects = list(self.canvas.sheet.rects)
        for index in selection:
            rects[index] = replace(rects[index], **{field: value})
        self.canvas.set_rects(rects, select=selection)
        self._on_canvas_change()

    def _on_grid_changed(self, event: ft.ControlEvent) -> None:
        self.canvas.configure(grid=int(event.control.value or 16))

    def _apply_view_prefs(self, prefs: dict) -> None:
        """Push every view preference onto the canvas."""
        self.canvas.configure(
            grid=int(prefs.get("default_grid", 16)),
            snap=bool(prefs.get("snap", True)),
            show_grid=bool(prefs.get("show_grid", True)),
            show_labels=bool(prefs.get("show_labels", True)),
            grid_style=str(prefs.get("grid_style", "lines")),
            grid_color=str(prefs.get("grid_color", "white")),
            grid_opacity=int(prefs.get("grid_opacity", 6)) / 100.0,
            grid_major=int(prefs.get("grid_major", 8)),
            hover=bool(prefs.get("hover_feedback", True)))

    def on_preferences_changed(self) -> None:
        """Re-skin the canvas as the settings are edited, not on next open.

        Grid appearance is the one class of setting you judge by looking at it,
        so it has to land on the texture while the Settings tab is still up.
        """
        super().on_preferences_changed()
        if getattr(self, "canvas", None) is None:
            return
        prefs = self.ctx.settings.tool_prefs(self.tool_id)
        self._apply_view_prefs(prefs)
        # The toolbar duplicates two of these; keep them in step.
        grid = int(prefs.get("default_grid", 16))
        if self._grid_choice.value != grid:
            self._grid_choice.value = grid
            self._grid_choice.update()
        snap = bool(prefs.get("snap", True))
        if self._snap_switch.value != snap:
            self._snap_switch.value = snap
            self._snap_switch.update()

    def _show_layouts(self) -> None:
        sheet = self.canvas.sheet
        if not sheet.width:
            self.ctx.toast("Load a texture first", level="WARNING")
            return

        def apply(maker) -> None:
            self._push_undo()
            self.canvas.set_rects(maker(sheet.width, sheet.height))
            self._on_canvas_change()
            self.ctx.close_dialog()

        self.ctx.show_dialog(ft.AlertDialog(
            title=ft.Text("Replace with a layout"),
            content=ft.Column(tight=True, spacing=4, width=320, controls=[
                ft.OutlinedButton(label, width=300,
                                  on_click=lambda _e, m=maker: apply(m))
                for label, maker in hotspot.PRESETS]),
            actions=[ft.TextButton("Cancel",
                                   on_click=lambda _e: self.ctx.close_dialog())]))

    def _detect(self) -> None:
        if self._preview_png is None:
            self.ctx.toast("Load a texture first", level="WARNING")
            return
        from PIL import Image

        prefs = self.ctx.settings.tool_prefs(self.tool_id)
        with Image.open(self._preview_png) as image:
            found = hotspot.detect_rects(
                image.convert("RGBA"),
                min_size=int(prefs.get("detect_min_size", 16)),
                snap=self.canvas._grid if self._snap_switch.value else 0)
        if not found:
            self.ctx.toast("Nothing detected - try a smaller minimum size",
                           level="WARNING")
            return
        self._push_undo()
        self.canvas.set_rects(found)
        self.ctx.toast(f"Detected {len(found)} region(s)")
        self._on_canvas_change()

    # ------------------------------------------------------------------
    # Sidebar refresh
    # ------------------------------------------------------------------

    def _on_canvas_change(self) -> None:
        self._refresh_list()
        self._refresh_issues()
        self._update_fit()

    def _set_cursor_text(self, text: str) -> None:
        self._cursor_text.value = text
        self._safe_update(self._cursor_text)

    def _make_row(self, index: int) -> ft.Container:
        """One reusable row of the rectangle list."""
        from s2kit.ui.hotspot_canvas import PALETTE

        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=6, vertical=3),
            border_radius=6,
            on_click=lambda _e, i=index: self._select_only(i),
            content=ft.Row(spacing=8, controls=[
                ft.Container(width=10, height=10, border_radius=3,
                             bgcolor=PALETTE[index % len(PALETTE)]),
                ft.Text(f"{index + 1}", size=11, width=22),
                ft.Text("", size=11, width=78),
                ft.Text("", size=11, expand=True),
                ft.Text("", size=10, color=ft.Colors.AMBER_300),
            ]))

    def _refresh_list(self) -> None:
        """Update the rectangle list in place.

        Rebuilding every row meant a drag that finished on a 50-rect sheet sent
        the whole table again as new controls. Rows are pooled and only their
        changed text is assigned, so finishing a drag costs a few hundred bytes
        instead of tens of kilobytes.
        """
        selection = set(self.canvas.selection)
        rects = self.canvas.sheet.rects
        while len(self._row_pool) < len(rects):
            self._row_pool.append(self._make_row(len(self._row_pool)))

        rows: list[ft.Control] = []
        highlight = ft.Colors.with_opacity(0.14, ft.Colors.PRIMARY)
        for index, rect in enumerate(rects):
            row = self._row_pool[index]
            cells = row.content.controls
            flags = "".join(letter for letter, on in
                            (("R", rect.rotate), ("M", rect.reflect),
                             ("A", rect.alt)) if on)
            wanted = highlight if index in selection else None
            if row.bgcolor != wanted:
                row.bgcolor = wanted
            for cell, value in ((cells[2], f"{rect.x},{rect.y}"),
                                (cells[3], f"{rect.w}x{rect.h}"),
                                (cells[4], flags)):
                if cell.value != value:
                    cell.value = value
            rows.append(row)

        if not rows:
            rows = [common.caption("Drag to draw one.")]
        if self._rect_list.controls != rows:
            self._rect_list.controls = rows
        self._safe_update(self._rect_list)

    def _select_only(self, index: int) -> None:
        self.canvas.select([index])
        self._on_canvas_change()

    def _refresh_issues(self) -> None:
        sheet = self.canvas.sheet
        issues = hotspot.validate(sheet, grid=self.canvas._grid)
        tones = {"error": ft.Colors.RED_400, "warning": ft.Colors.AMBER_400,
                 "info": ft.Colors.BLUE_300}
        rows: list[ft.Control] = []
        if sheet.width and sheet.rects:
            rows.append(common.caption(
                f"{len(sheet)} rect(s)  -  {sheet.coverage() * 100:.0f}% of the "
                "texture covered"))
        for issue in issues[:14]:
            rows.append(ft.Row(spacing=6, controls=[
                ft.Icon(ft.Icons.CIRCLE, size=8, color=tones[issue.level]),
                ft.Text(issue.message, size=11, expand=True, no_wrap=False),
            ]))
        self._issues.controls = rows or [common.caption("No problems found.")]
        self._safe_update(self._issues)

    def _update_fit(self, _event: Any = None) -> None:
        """Rank the rects Fit would choose for the face size typed in."""
        sheet = self.canvas.sheet
        try:
            face_w = float(self._face_w.value or 0)
            face_h = float(self._face_h.value or 0)
        except ValueError:
            face_w = face_h = 0.0

        rows: list[ft.Control] = []
        if face_w > 0 and face_h > 0 and sheet.rects:
            from s2kit.ui.hotspot_canvas import PALETTE

            # Pooled like the rectangle list: this runs on every keystroke in
            # the face fields, so it must not rebuild controls to do it.
            for rank, (index, score) in enumerate(
                    hotspot.best_matches(sheet, face_w, face_h, limit=4)):
                rect = sheet.rects[index]
                while len(self._fit_pool) <= rank:
                    self._fit_pool.append(ft.Row(spacing=8, controls=[
                        ft.Container(width=10, height=10, border_radius=3),
                        ft.Text("", size=11, width=28),
                        ft.Text("", size=11, expand=True),
                        ft.Text("", size=10),
                    ]))
                row = self._fit_pool[rank]
                swatch, number, size, verdict = row.controls
                colour = PALETTE[index % len(PALETTE)]
                if swatch.bgcolor != colour:
                    swatch.bgcolor = colour
                for cell, value in ((number, f"#{index + 1}"),
                                    (size, f"{rect.w}x{rect.h}"),
                                    (verdict, "best" if rank == 0
                                     else f"{score:.2f}")):
                    if cell.value != value:
                        cell.value = value
                weight = ft.FontWeight.W_700 if rank == 0 else None
                if number.weight != weight:
                    number.weight = weight
                tone = (ft.Colors.GREEN_300 if rank == 0
                        else ft.Colors.ON_SURFACE_VARIANT)
                if verdict.color != tone:
                    verdict.color = tone
                rows.append(row)

        if not rows:
            rows = [common.caption("Type a face size to see what Fit would "
                                   "pick.")]
        if self._fit_result.controls != rows:
            self._fit_result.controls = rows
        self._safe_update(self._fit_result)

    @staticmethod
    def _safe_update(control: Any) -> None:
        try:
            control.update()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Saving - a real job, so it logs and lands in history
    # ------------------------------------------------------------------

    def start_run(self) -> None:
        sheet = self.canvas.sheet
        if not sheet.rects:
            self.ctx.toast("Nothing to save - draw a rectangle first",
                           level="WARNING")
            return
        if self._rect_path is None:
            self.ctx.toast("Load a texture first, so the .rect has a home",
                           level="WARNING")
            return
        super().start_run()

    def run_job(self, ctx: JobContext) -> None:
        from dataclasses import replace

        sheet = RectSheet(
            rects=[replace(rect) for rect in self.canvas.sheet.rects],
            width=self.canvas.sheet.width, height=self.canvas.sheet.height)
        target = str(ctx.prefs.get("target", "hammerpp"))
        rect_path = Path(self._rect_path)

        for issue in hotspot.validate(sheet, grid=self.canvas._grid):
            if issue.level == "error":
                ctx.error(issue.message)
            elif issue.level == "warning":
                ctx.warn(issue.message)
        if any(issue.level == "error"
               for issue in hotspot.validate(sheet)):
            raise ValueError("Fix the errors above before saving")

        for loss in hotspot.describe_losses(sheet, target):
            ctx.warn(f"Saving as {target}: {loss}")

        written = hotspot.write_sheet(
            sheet, rect_path, target=target,
            flags=bool(ctx.prefs.get("write_flags", True)))
        ctx.success(f"Wrote {written.name} ({len(sheet)} rectangles)")
        ctx.artifact(written, label=written.name)

        if bool(ctx.prefs.get("update_vmt", True)):
            self._wire_vmt(ctx, rect_path, target)

        ctx.metric("Rectangles", len(sheet), tone="good")
        ctx.metric("Coverage", f"{sheet.coverage() * 100:.0f}%")
        ctx.metric("Format", "Source 2" if target == "source2" else "Hammer++")
        ctx.table("Rectangles", ["#", "Min", "Max", "Size", "Flags"],
                  [[index + 1, f"{r.x} {r.y}", f"{r.x2} {r.y2}",
                    f"{r.w}x{r.h}",
                    " ".join(name for name, on in
                             (("rotate", r.rotate), ("reflect", r.reflect),
                              ("alt", r.alt)) if on) or "-"]
                   for index, r in enumerate(sheet.rects)],
                  numeric_columns=(0,))
        ctx.summary(f"{len(sheet)} rectangle(s) -> {written.name}")

    def _wire_vmt(self, ctx: JobContext, rect_path: Path, target: str) -> None:
        """Point the material's `%rectanglemap` at the rect file just written.

        Only a Hammer++ rect is wired: `%rectanglemap` is read by Hammer++ and
        Strata, neither of which reads the Source 2 KV3 dialect.
        """
        reference = hotspot.material_relative_path(rect_path)
        line = f'"%rectanglemap" "{reference}"'
        if target == "source2":
            ctx.info(f"Saved as Source 2 - {line} left out, since Hammer++ "
                     "cannot read that format")
            return

        vmt_path = self._material_path or rect_path.with_suffix(".vmt")
        if not vmt_path.is_file():
            ctx.warn(f"No .vmt found for {rect_path.name} - add {line} to it "
                     "by hand")
            return
        if hotspot.materials_root(rect_path) is None:
            ctx.warn(f"{rect_path.name} is not under a materials folder, so "
                     "Hammer cannot resolve the reference written for it")

        action = hotspot.inject_rectanglemap(vmt_path, reference)
        ctx.info(f"{action} {line} in {vmt_path.name}")
        if action != "unchanged":
            ctx.artifact(vmt_path, label=vmt_path.name)
