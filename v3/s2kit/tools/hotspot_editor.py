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

import tempfile
from pathlib import Path
from typing import Any, Optional

import flet as ft

from s2kit.core.jobs import JobContext
from s2kit.core.params import BoolParam, ChoiceParam, IntParam, options
from s2kit.core.registry import tool
from s2kit.pipelines import hotspot
from s2kit.pipelines.hotspot import Rect, RectSheet
from s2kit.ui import common, theme
from s2kit.ui.hotspot_canvas import HotspotCanvas
from s2kit.ui.tool_page import ToolPage

GRID_SIZES = (1, 2, 4, 8, 16, 32, 64, 128, 256)

#: How many edits are kept. Snapshots are a list of small dataclasses, so this
#: costs almost nothing even on a busy sheet.
UNDO_DEPTH = 100

#: Arrow keys, as the (dx, dy) they nudge by. Flet spells them "Arrow Up";
#: the handler strips spaces before looking them up.
_ARROWS: dict[str, tuple[int, int]] = {
    "ARROWLEFT": (-1, 0), "ARROWRIGHT": (1, 0),
    "ARROWUP": (0, -1), "ARROWDOWN": (0, 1),
}


@tool(
    id="hotspot-editor",
    title="Hotspot Editor",
    category="Materials",
    icon="HIGHLIGHT_ALT",
    description="Draw the rectangles Hammer++ and Strata use to fit a trim "
                "sheet to a face.",
    keywords=("hotspot", "rect", "rectanglemap", "trim", "trimsheet", "hammer",
              "hammer++", "strata", "fit", "atlas", "subrect", "panel"),
    order=40,
)
class HotspotEditor(ToolPage):
    """A canvas editor for `.rect` hotspot files."""

    RUN_LABEL = "Save .rect"
    RUN_ICON = "SAVE"

    PARAMS = []

    PREFERENCES = [
        ChoiceParam("default_grid", "Default grid", default=16,
                    group="Editing",
                    choices=options(*[(size, f"{size} px") for size in GRID_SIZES])),
        BoolParam("snap", "Snap to grid", default=True, group="Editing"),
        BoolParam("show_grid", "Show the grid", default=True, group="Editing"),
        BoolParam("show_labels", "Number the rectangles", default=True,
                  group="Editing"),
        BoolParam("hover_feedback", "Highlight and read out under the cursor",
                  default=True, group="Editing",
                  help="Costs one event per pixel of mouse travel. Turn it off "
                       "if the canvas feels heavy."),
        ChoiceParam("grid_style", "Grid style", default="lines", group="Grid",
                    choices=options(
                        ("lines", "Lines"),
                        ("dots", "Dots at the intersections"),
                        ("crosses", "Small crosses")),
                    help="Dots and crosses mark the same positions as lines "
                         "without laying a mesh over the art."),
        ChoiceParam("grid_color", "Grid colour", default="white", group="Grid",
                    choices=options(
                        ("white", "White"), ("black", "Black"),
                        ("cyan", "Cyan"), ("magenta", "Magenta"),
                        ("yellow", "Yellow"), ("green", "Green"),
                        ("orange", "Orange"), ("red", "Red")),
                    help="Black is the one that reads on a bright sheet."),
        IntParam("grid_opacity", "Grid strength", default=6, min=1, max=100,
                 suffix="%", group="Grid",
                 help="How visible the fine grid is. The emphasised lines "
                      "scale with this."),
        ChoiceParam("grid_major", "Emphasise every", default=8, group="Grid",
                    choices=options(
                        (0, "Never - one flat grid"),
                        (2, "2 cells"), (4, "4 cells"),
                        (8, "8 cells"), (16, "16 cells")),
                    help="Draws a stronger line at this interval, so you can "
                         "count cells without measuring."),
        ChoiceParam("target", "Save as", default="hammerpp", group="Output",
                    choices=options(
                        ("hammerpp", "Hammer++ / Strata (.rect)"),
                        ("source2", "Source 2 KV3 (.rect)"))),
        BoolParam("write_flags", "Write rotate / reflect / alt", default=True,
                  group="Output",
                  help="Strata honours these; Hammer++ ignores them. Off keeps "
                       "the file plain."),
        BoolParam("update_vmt", "Wire up the VMT's %rectanglemap", default=True,
                  group="Output",
                  help="Adds or updates the line in the matching .vmt beside "
                       "the texture."),
        IntParam("detect_min_size", "Smallest detected region", default=16,
                 min=2, max=512, suffix="px", group="Detection"),
    ]

    HELP = """
### Hotspot Editor

Hotspot texturing means authoring one texture full of trim and panel regions,
saying where those regions are, and letting the editor pick the right one per
face. In Hammer++ or Strata you apply the material, press **Fit**, and it snaps
the face's UVs onto whichever rectangle best matches its size and aspect. One
material, one draw call, no manual aligning.

#### Getting rectangles onto the sheet

| | |
| --- | --- |
| **Draw** | Drag on empty space. |
| **Move** | Drag a rectangle. |
| **Resize** | Drag one of its eight handles. |
| **Pan / zoom** | Right-drag, and the wheel. Double-click fits the view. |
| **Layouts** | A grid or trim-sheet split, in one click. |
| **Detect** | Finds the regions automatically - see below. |

Everything snaps to the grid unless you turn snapping off. Moving snaps the
*distance*, not the position, so a rectangle that started off-grid keeps its
offset instead of jumping.

#### Keyboard

| | |
| --- | --- |
| **Delete** / **Backspace** | Delete the selection. |
| **Arrows** | Nudge by one pixel. **Shift** nudges by one grid cell. |
| **Ctrl+A** / **Esc** | Select all, or select nothing. |
| **Ctrl+D** | Duplicate. |
| **Ctrl+Z** / **Ctrl+Y** | Undo, redo. **Ctrl+Shift+Z** also redoes. |
| **Ctrl+S** | Save the `.rect`. |
| **F** / **G** | Fit the view; toggle the grid. |

Nudging deliberately ignores snapping - it is what you reach for when snap has
left a rectangle a pixel out. These keys stand down while you are typing in the
Fit fields.

#### The grid

Cell size and snapping are on the toolbar; how the grid *looks* is under
**Settings**, and changes land on the texture as you make them.

| | |
| --- | --- |
| **Style** | Lines, dots at the intersections, or small crosses. Dots and crosses mark the same positions without laying a mesh over the art. |
| **Colour** | Eight inks. White suits most Source textures; black is the one that reads on a blown-out sheet. |
| **Strength** | How visible the fine grid is, 1-100%. The emphasised lines scale with it. |
| **Emphasise every** | A stronger line every N cells, so you can count cells without measuring. |

Whatever the cell size, the grid is only drawn while the lines are far enough
apart to mean something - zoom out far enough and it doubles the interval, then
stops. So a 4 px grid is safe to leave on.

#### Detect

Two strategies, picked from what the texture carries:

* **Alpha islands** - if the sheet has real transparency, every connected opaque
  region becomes a rectangle.
* **Seams** - otherwise it cuts along rows and columns that are one flat colour,
  which is what the gutters between panels are. The cut recurses, so a sheet
  that is bands-then-cells comes out right rather than as one coarse grid.

#### Fit preview

Type a face size and the preview names the rectangle Hammer would most likely
choose, with its runners-up. Fit is deliberately random among close matches -
pressing it again re-rolls - so the ranking matters more than the winner.

Use it to answer the question a trim sheet exists to answer: *does this sheet
cover the face shapes my map actually has?* If nothing scores well for a long
thin wall, the sheet needs a long thin rectangle.

#### Saving

Writes the `.rect` beside the texture and, unless you turn it off, adds
`%rectanglemap` to the matching VMT. The reference is materials-relative and
carries no extension, which is the form Hammer expects.

Overlapping rectangles are fine and are not reported as a problem - authoring
several candidates over the same art is how you give Fit something to choose
between. Valve's own sheets overlap heavily.

#### The two `.rect` formats

Source 2 and Hammer++ both use `.rect` and are otherwise unrelated: Source 2
stores KV3 with coordinates as a fraction of the texture, Hammer++ stores plain
text in pixels. **Open** reads either, so a Half-Life: Alyx sheet's rectangles
can be imported and converted instead of redrawn.
"""

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def init(self):
        super().init()
        self._texture_path: Optional[Path] = None
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
            self._sheet_text, ft.Container(expand=True), self._cursor_text])

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

    def _pick_texture(self) -> None:
        self.ctx.page.run_task(self._pick_texture_async)

    async def _pick_texture_async(self) -> None:
        picked = await self.ctx.pick_file(
            title="Open a trim sheet",
            extensions=("png", "jpg", "jpeg", "tga", "bmp", "vtf", "vmt"))
        if picked:
            self._load_texture(picked[0])

    def _load_texture(self, path: Path) -> None:
        """Decode any supported texture into a PNG the canvas can draw."""
        from s2kit.media import images

        path = Path(path)
        source = path
        if path.suffix.lower() == ".vmt":
            reference = self._basetexture_of(path)
            if reference is None:
                self.ctx.toast(f"{path.name} has no $basetexture", level="ERROR")
                return
            source = reference

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
        self._texture_path = path if path.suffix.lower() == ".vmt" else source

        sheet = self.canvas.sheet
        sheet.width, sheet.height = info.width, info.height
        self.canvas.load(sheet, self._preview_png)
        self._sheet_text.value = (f"{source.name}   {info.width} x {info.height}")
        self._rect_path = source.with_suffix(".rect")

        # A rect file already beside the texture is almost certainly its own.
        if self._rect_path.is_file():
            self._load_rect(self._rect_path)
        else:
            self._on_canvas_change()
        self._safe_update(self._sheet_text)

    @staticmethod
    def _basetexture_of(vmt_path: Path) -> Optional[Path]:
        """Resolve a VMT's `$basetexture` to a file on disk."""
        from s2kit.pipelines import vmt as vmt_module

        refs = vmt_module.read_texture_refs(vmt_path)
        reference = refs.get("color")
        if not reference:
            return None
        return vmt_module.resolve_texture_ref(reference, vmt_path.parent)

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
            self._wire_vmt(ctx, rect_path)

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

    def _wire_vmt(self, ctx: JobContext, rect_path: Path) -> None:
        """Point the matching VMT at the rect file we just wrote."""
        candidates = [rect_path.with_suffix(".vmt")]
        if self._texture_path is not None:
            candidates.insert(0, Path(self._texture_path).with_suffix(".vmt"))
        vmt_path = next((path for path in candidates if path.is_file()), None)
        if vmt_path is None:
            ctx.warn(f"No .vmt beside {rect_path.name} - add "
                     f'"%rectanglemap" "{hotspot.material_relative_path(rect_path)}"'
                     " to it by hand")
            return

        reference = hotspot.material_relative_path(rect_path)
        action = hotspot.inject_rectanglemap(vmt_path, reference)
        ctx.info(f'{action} "%rectanglemap" "{reference}" in {vmt_path.name}')
        if action != "unchanged":
            ctx.artifact(vmt_path, label=vmt_path.name)
