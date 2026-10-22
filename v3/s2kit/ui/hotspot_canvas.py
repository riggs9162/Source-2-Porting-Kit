"""
The interactive hotspot canvas.

A texture with rectangles drawn over it that you can draw, drag, resize and
snap. Built on `flet.canvas` for the drawing and one `GestureDetector` for every
interaction, because Flet has no scene-graph equivalent of Qt's QGraphicsView -
there are no per-item widgets to attach handlers to, so this control owns the
view transform and does its own hit-testing.

Controls:

===============  ==============================================================
Left drag        On empty space draws a rect; on a rect moves it; on a handle
                 resizes it.
Left click       Selects. Shift-click adds to the selection.
Right drag       Pans.
Wheel            Zooms about the cursor.
Double click     Fits the texture to the view.
===============  ==============================================================

Everything the editor needs to know is on :class:`HotspotCanvas`; it reports
changes through one `on_change` callback so the tool page can keep its table,
undo stack and validation in step without reaching inside.
"""

from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path
from typing import Any, Callable, Optional

import flet as ft
import flet.canvas as cv

from s2kit.pipelines.hotspot import Rect, RectSheet
from s2kit.ui.controls import state

#: Screen-space size of a resize handle, and the grab tolerance around it.
HANDLE_SIZE = 9.0
HANDLE_GRAB = 11.0

#: Zoom limits and the multiplier per wheel notch.
MIN_ZOOM, MAX_ZOOM, ZOOM_STEP = 0.02, 64.0, 1.2

#: Seconds between coordinate-readout pushes, and between drag frames. Pointer
#: events arrive far faster than either is worth sending to the UI.
STATUS_INTERVAL = 0.08
FRAME_INTERVAL = 1.0 / 60.0

#: Corner and edge handles, as (dx, dy) in 0..1 rect space. Corners first so a
#: corner wins when a click is inside both its box and an edge's.
HANDLES: tuple[tuple[float, float], ...] = (
    (0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0),      # corners
    (0.5, 0.0), (1.0, 0.5), (0.5, 1.0), (0.0, 0.5),      # edges
)

#: Cycled per rect so neighbours are distinguishable at a glance.
PALETTE: tuple[str, ...] = (
    "#ff4d8d", "#22d3ee", "#4ade80", "#f59e0b", "#a78bfa",
    "#f472b6", "#38bdf8", "#facc15", "#34d399", "#fb7185",
)

#: Grid ink. White works on most Source art; black is the one that reads on a
#: blown-out sheet, and the tinted options stay legible over both.
GRID_COLORS: dict[str, str] = {
    "white": "#ffffff",
    "black": "#000000",
    "cyan": "#22d3ee",
    "magenta": "#f472b6",
    "yellow": "#facc15",
    "green": "#34d399",
    "orange": "#fb923c",
    "red": "#f87171",
}

GRID_STYLES: tuple[str, ...] = ("lines", "dots", "crosses")

#: Smallest on-screen spacing a grid may be drawn at, per style. Lines merge
#: into a solid wash sooner than dots do, so they need more room.
_GRID_MIN_SPACING: dict[str, float] = {"lines": 14.0, "dots": 9.0,
                                       "crosses": 11.0}


@ft.control
class HotspotCanvas(ft.Container):
    """Draws a texture and its rectangles, and edits them in place."""

    ctx: Any = state()
    on_change: Optional[Callable[[], None]] = state()
    #: Called with a status string for the coordinate readout.
    on_status: Optional[Callable[[str], None]] = state()

    def init(self):
        self.expand = True
        self._sheet = RectSheet()
        self._texture: Optional[Path] = None
        #: The texture's PNG bytes, handed to the image widget as data. A file
        #: path only resolves in the desktop client; served to a browser it is
        #: a URL on the kit's own web server, which 404s.
        self._texture_data: Optional[bytes] = None
        #: Which file contents `_texture_data` holds; see _texture_key.
        self._texture_id: Optional[tuple] = None
        self._zoom = 1.0
        self._pan = (24.0, 24.0)
        self._selection: set[int] = set()
        self._grid = 16
        self._snap = True
        self._show_grid = True
        self._show_labels = True
        self._hover_feedback = True
        self._view = (900.0, 620.0)
        self._ready = False

        # Grid appearance.
        self._grid_style = "lines"
        self._grid_color = "white"
        self._grid_opacity = 0.06
        self._grid_major = 8

        # The texture is a sibling widget, not a canvas shape; see
        # _place_texture. `_image_dirty` keeps it out of the update when a
        # frame did not move it.
        self._image_src: Optional[tuple] = None
        self._image_dirty = False

        # Everything drawn is pooled by a stable key and mutated in place. See
        # _pooled: handing Flet fresh controls every frame is what made dragging
        # crawl. `_grid_cache` goes further and skips the rebuild entirely while
        # the view transform holds still.
        self._pool: dict[str, Any] = {}
        self._paints: dict[tuple, Any] = {}
        self._styles: dict[tuple, Any] = {}
        self._grid_cache: Optional[tuple[tuple, list[Any]]] = None
        self._last_status = ("", 0.0)
        self._last_frame = 0.0

        # The grid and the sheet edge live on their own canvas; see
        # _render_static. `_static_key` is what they were last drawn for, so a
        # frame that did not move the view leaves that canvas alone entirely.
        self._static_key: Optional[tuple] = None
        self._grid_dirty = False
        #: True while a trailing frame is queued. See _schedule_trailing.
        self._trailing = False

        # Live drag state. `_drag` is None when nothing is happening.
        self._drag: Optional[str] = None          # draw | move | resize | pan
        self._drag_handle = -1
        self._drag_origin = (0.0, 0.0)
        self._drag_start_rects: dict[int, Rect] = {}
        self._preview: Optional[Rect] = None
        self._hover = -1

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def sheet(self) -> RectSheet:
        return self._sheet

    @property
    def selection(self) -> list[int]:
        return sorted(index for index in self._selection
                      if 0 <= index < len(self._sheet.rects))

    def load(self, sheet: RectSheet, texture: Optional[Path] = None) -> None:
        """Replace the sheet, and optionally the texture behind it.

        The texture is read once here; reloading the same unchanged file keeps
        the bytes already on the client rather than sending them again.
        """
        self._sheet = sheet
        if texture is not None:
            self._texture = Path(texture)
            texture_id = self._texture_key()
            if texture_id != self._texture_id:
                self._texture_data = self._texture.read_bytes()
                self._texture_id = texture_id
        self._selection.clear()
        # Pooled shapes are keyed by rect index, which now means something else.
        self._pool.clear()
        self._grid_cache = None
        self._static_key = None
        self.fit()

    def set_rects(self, rects: list[Rect], *, select: Optional[list[int]] = None
                  ) -> None:
        """Replace the rectangles, keeping the texture and the view."""
        self._sheet.rects = rects
        self._selection = set(select or [])
        self.refresh()

    def select(self, indices: list[int]) -> None:
        self._selection = {index for index in indices
                           if 0 <= index < len(self._sheet.rects)}
        self.refresh()

    def configure(self, *, grid: Optional[int] = None,
                  snap: Optional[bool] = None,
                  show_grid: Optional[bool] = None,
                  show_labels: Optional[bool] = None,
                  grid_style: Optional[str] = None,
                  grid_color: Optional[str] = None,
                  grid_opacity: Optional[float] = None,
                  grid_major: Optional[int] = None,
                  hover: Optional[bool] = None) -> None:
        if grid is not None:
            self._grid = max(1, grid)
        if snap is not None:
            self._snap = snap
        if show_grid is not None:
            self._show_grid = show_grid
        if show_labels is not None:
            self._show_labels = show_labels
        if grid_style is not None and grid_style in GRID_STYLES:
            self._grid_style = grid_style
        if grid_color is not None and grid_color in GRID_COLORS:
            self._grid_color = grid_color
        if grid_opacity is not None:
            self._grid_opacity = max(0.01, min(1.0, grid_opacity))
        if grid_major is not None:
            self._grid_major = max(0, grid_major)
        if hover is not None and hover != self._hover_feedback:
            self._hover_feedback = hover
            self._apply_hover()
        self.refresh()

    def _apply_hover(self) -> None:
        """Subscribe to hover only when something wants it.

        Flet delivers a hover event to Python for every pixel the pointer
        travels. That is the price of the coordinate readout and the
        highlight-under-cursor; on a slow machine it is worth not paying.
        """
        gesture = getattr(self, "_gesture", None)
        if gesture is None:
            return
        gesture.on_hover = self._hover_handler if self._hover_feedback else None
        if self._hover != -1 and not self._hover_feedback:
            self._hover = -1
        if self.on_status is not None and not self._hover_feedback:
            self.on_status("")
        try:
            gesture.update()
        except Exception:
            pass

    def fit(self) -> None:
        """Scale and centre the texture so the whole thing is visible."""
        width, height = self._sheet.size
        view_w, view_h = self._view
        if width and height:
            self._zoom = min((view_w - 32) / width, (view_h - 32) / height)
            self._zoom = max(MIN_ZOOM, min(MAX_ZOOM, self._zoom))
            self._pan = ((view_w - width * self._zoom) / 2.0,
                         (view_h - height * self._zoom) / 2.0)
        self.refresh()

    def zoom_to(self, factor: float) -> None:
        centre = (self._view[0] / 2.0, self._view[1] / 2.0)
        self._apply_zoom(factor / max(self._zoom, 1e-6), centre)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self):
        # Three layers, in order of how often they are repainted. The texture
        # is a real Image widget *behind* the canvases, not a shape inside one:
        # a canvas shape has to be resolved to a bitmap every time the canvas
        # repaints - and the canvas repaints on every drag frame - so a
        # 2048-square sheet was being handled 60 times a second. As a widget,
        # Flutter decodes it once and a drag only moves it.
        #
        # The grid and the sheet edge then get a canvas of their own, because
        # dragging a rectangle does not move them: patching them anyway was
        # about half of every drag frame (1.65 ms against 0.86 ms on a 24-rect
        # sheet). Only the rectangles are left on the canvas that repaints.
        self._image = ft.Image(src="", fit=ft.BoxFit.FILL,
                               gapless_playback=True, anti_alias=False,
                               filter_quality=ft.FilterQuality.LOW,
                               visible=False, left=0, top=0,
                               width=0, height=0)
        self._grid_canvas = cv.Canvas(expand=True, shapes=[])
        self._canvas = cv.Canvas(expand=True, shapes=[],
                                 on_resize=self._on_resize,
                                 resize_interval=100)

        quiet = self._quiet_handler
        self._hover_handler = quiet(self._on_hover)
        self._gesture = ft.GestureDetector(
            expand=True,
            mouse_cursor=ft.MouseCursor.PRECISE,
            on_tap_down=quiet(self._on_tap),
            on_double_tap=quiet(lambda _e: self.fit()),
            on_pan_start=quiet(self._on_pan_start),
            on_pan_update=quiet(self._on_pan_update),
            on_pan_end=quiet(self._on_pan_end),
            on_right_pan_start=quiet(self._on_right_pan_start),
            on_right_pan_update=quiet(self._on_right_pan_update),
            on_right_pan_end=quiet(lambda _e: self._end_drag()),
            on_scroll=quiet(self._on_scroll),
            on_hover=self._hover_handler,
            content=ft.Container(expand=True, bgcolor="#141619",
                                 content=ft.Stack(expand=True, controls=[
                                     self._image, self._grid_canvas,
                                     self._canvas])),
        )
        self.content = self._gesture
        if not self._hover_feedback:
            self._gesture.on_hover = None
        self._ready = True
        self._render()

    @staticmethod
    def _quiet_handler(handler: Callable[[Any], Any]) -> Callable[[Any], Any]:
        """Wrap a gesture handler so it cannot trigger a page-wide update.

        Flet auto-updates after any handler that did not call `update()`
        itself, and it does that by walking up to the nearest *isolated*
        ancestor - which, in this app, is only ever the Page. So every event
        this control deliberately ignores - a coalesced frame, a hover that
        moved nothing, a wheel notch past the zoom limit - paid for a
        whole-page patch instead of nothing at all. Measured on a 24-rect
        sheet: 10.2 ms for the ignored event against 1.6 ms for the frame it
        was trying to skip, so the harder the mouse polled the further behind
        the canvas fell. That is the freeze.

        Marking the update as already done suppresses it. Flet resets the flag
        per event, so this cannot leak into another control's handler.
        """
        def wrapped(event: Any) -> Any:
            ft.context.mark_update_called()
            return handler(event)
        return wrapped

    def _on_resize(self, event: Any) -> None:
        self._view = (float(event.width or 900), float(event.height or 620))
        # First real size: frame the texture rather than leaving it in a corner.
        if self._sheet.width and self._zoom == 1.0:
            self.fit()
        else:
            self.refresh()

    # ------------------------------------------------------------------
    # View transform
    # ------------------------------------------------------------------

    def to_screen(self, x: float, y: float) -> tuple[float, float]:
        return (self._pan[0] + x * self._zoom, self._pan[1] + y * self._zoom)

    def to_texture(self, sx: float, sy: float) -> tuple[float, float]:
        return ((sx - self._pan[0]) / self._zoom,
                (sy - self._pan[1]) / self._zoom)

    def _apply_zoom(self, factor: float, at: tuple[float, float]) -> None:
        """Zoom about a screen point, keeping the texel under it fixed."""
        target = max(MIN_ZOOM, min(MAX_ZOOM, self._zoom * factor))
        if target == self._zoom:
            return
        before = self.to_texture(*at)
        self._zoom = target
        after = self.to_texture(*at)
        self._pan = (self._pan[0] + (after[0] - before[0]) * self._zoom,
                     self._pan[1] + (after[1] - before[1]) * self._zoom)
        # Coalesced like a drag: a wheel notch is one event but a trackpad or a
        # high-resolution wheel emits them in bursts, and a zoom frame is the
        # expensive kind - it moves the grid and rescales the texture. The
        # trailing frame is what makes this safe to hold.
        self.refresh(coalesce=True)

    def _snapped(self, value: float) -> float:
        if not self._snap or self._grid <= 1:
            return round(value)
        return round(value / self._grid) * self._grid

    # ------------------------------------------------------------------
    # Hit testing
    # ------------------------------------------------------------------

    def _hit_handle(self, sx: float, sy: float) -> tuple[int, int]:
        """(rect index, handle index) under a screen point, or (-1, -1).

        Only selected rects show handles, so only they are tested - otherwise a
        handle from a rect behind would steal a click meant for the one on top.
        """
        for index in sorted(self._selection, reverse=True):
            if index >= len(self._sheet.rects):
                continue
            rect = self._sheet.rects[index]
            for handle, (fx, fy) in enumerate(HANDLES):
                hx, hy = self.to_screen(rect.x + rect.w * fx,
                                        rect.y + rect.h * fy)
                if abs(sx - hx) <= HANDLE_GRAB and abs(sy - hy) <= HANDLE_GRAB:
                    return index, handle
        return -1, -1

    def _hit_rect(self, sx: float, sy: float) -> int:
        """Topmost rect under a screen point, or -1.

        Iterated in reverse so the last-drawn rect wins, matching what the user
        sees. Ties on containment are broken by area: a small rect drawn inside
        a big one stays clickable.
        """
        tx, ty = self.to_texture(sx, sy)
        hits = [index for index, rect in enumerate(self._sheet.rects)
                if rect.contains(tx, ty)]
        if not hits:
            return -1
        return min(hits, key=lambda index: (self._sheet.rects[index].area,
                                            -index))

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    def _on_tap(self, event: ft.TapEvent) -> None:
        index = self._hit_rect(event.local_position.x, event.local_position.y)
        if index < 0:
            self._selection.clear()
        elif index in self._selection and len(self._selection) > 1:
            self._selection = {index}
        else:
            self._selection = {index}
        self.refresh()
        self._report_change()

    def _on_pan_start(self, event: ft.DragStartEvent) -> None:
        sx, sy = event.local_position.x, event.local_position.y
        self._drag_origin = self.to_texture(sx, sy)

        index, handle = self._hit_handle(sx, sy)
        if index >= 0:
            self._drag, self._drag_handle = "resize", handle
            self._drag_start_rects = {index: self._sheet.rects[index]}
            return

        index = self._hit_rect(sx, sy)
        if index >= 0:
            if index not in self._selection:
                self._selection = {index}
            self._drag = "move"
            self._drag_start_rects = {i: self._sheet.rects[i]
                                      for i in self.selection}
            self.refresh()
            return

        self._selection.clear()
        self._drag = "draw"
        self._preview = None
        self.refresh()

    def _on_pan_update(self, event: ft.DragUpdateEvent) -> None:
        if self._drag is None:
            return
        tx, ty = self.to_texture(event.local_position.x, event.local_position.y)

        if self._drag == "draw":
            x0, y0 = (self._snapped(self._drag_origin[0]),
                      self._snapped(self._drag_origin[1]))
            x1, y1 = self._snapped(tx), self._snapped(ty)
            self._preview = Rect(x=int(min(x0, x1)), y=int(min(y0, y1)),
                                 w=int(abs(x1 - x0)) or 1,
                                 h=int(abs(y1 - y0)) or 1)
        elif self._drag == "move":
            # Snap the delta, not the position, so a rect that started off-grid
            # keeps its offset instead of jumping onto the grid.
            dx = self._snapped(tx - self._drag_origin[0])
            dy = self._snapped(ty - self._drag_origin[1])
            for index, start in self._drag_start_rects.items():
                moved = Rect(**{**start.__dict__,
                                "x": int(start.x + dx), "y": int(start.y + dy)})
                self._sheet.rects[index] = self._bounded(moved)
        elif self._drag == "resize":
            index, start = next(iter(self._drag_start_rects.items()))
            self._sheet.rects[index] = self._bounded(
                self._resized(start, self._drag_handle, tx, ty))
        self.refresh(coalesce=True)

    def _on_pan_end(self, event: ft.DragEndEvent) -> None:
        if self._drag == "draw" and self._preview is not None:
            rect = self._bounded(self._preview)
            if rect.w >= 2 and rect.h >= 2:
                self._sheet.rects.append(rect)
                self._selection = {len(self._sheet.rects) - 1}
        self._preview = None
        self._end_drag()
        self._report_change()

    def _on_right_pan_start(self, event: ft.DragStartEvent) -> None:
        self._drag = "pan"
        self._drag_origin = (event.local_position.x, event.local_position.y)

    def _on_right_pan_update(self, event: ft.DragUpdateEvent) -> None:
        if self._drag != "pan":
            return
        self._pan = (self._pan[0] + event.local_delta.x,
                     self._pan[1] + event.local_delta.y)
        self.refresh(coalesce=True)

    def _end_drag(self) -> None:
        self._drag = None
        self._drag_handle = -1
        self._drag_start_rects = {}
        self.refresh()

    def _on_scroll(self, event: ft.ScrollEvent) -> None:
        notches = -(event.scroll_delta.y or 0) / 120.0
        if notches:
            self._apply_zoom(ZOOM_STEP ** notches,
                             (event.local_position.x, event.local_position.y))

    def _on_hover(self, event: ft.HoverEvent) -> None:
        sx, sy = event.local_position.x, event.local_position.y
        index = self._hit_rect(sx, sy)
        if index != self._hover:
            self._hover = index
            self.refresh()
        if self.on_status is None:
            return
        tx, ty = self.to_texture(sx, sy)
        inside = (0 <= tx <= self._sheet.width
                  and 0 <= ty <= self._sheet.height)
        text = f"{int(tx)}, {int(ty)} px" if inside else ""
        # Hover fires for every pixel of mouse travel, and each status push is a
        # round trip to the UI. Only send real changes, and no faster than the
        # eye can read them.
        last_text, last_at = self._last_status
        now = time.monotonic()
        if text == last_text or (text and now - last_at < STATUS_INTERVAL):
            return
        self._last_status = (text, now)
        self.on_status(text)

    # ------------------------------------------------------------------
    # Geometry helpers
    # ------------------------------------------------------------------

    def _resized(self, start: Rect, handle: int, tx: float, ty: float) -> Rect:
        """Move only the edges the dragged handle owns.

        Measured from the rect as it was when the drag began rather than
        incrementally, so a slow drag cannot accumulate rounding drift.
        """
        fx, fy = HANDLES[handle]
        x0, y0, x1, y1 = start.x, start.y, start.x2, start.y2
        if fx == 0.0:
            x0 = self._snapped(tx)
        elif fx == 1.0:
            x1 = self._snapped(tx)
        if fy == 0.0:
            y0 = self._snapped(ty)
        elif fy == 1.0:
            y1 = self._snapped(ty)
        if x1 - x0 < 1:
            x0, x1 = (x1 - 1, x1) if fx == 0.0 else (x0, x0 + 1)
        if y1 - y0 < 1:
            y0, y1 = (y1 - 1, y1) if fy == 0.0 else (y0, y0 + 1)
        return Rect(**{**start.__dict__, "x": int(x0), "y": int(y0),
                       "w": int(x1 - x0), "h": int(y1 - y0)})

    def _bounded(self, rect: Rect) -> Rect:
        """Keep a rect inside the texture.

        v2 clamped only at draw time, so a dragged or resized rect could end up
        with negative coordinates and get written out that way.
        """
        if self._sheet.width and self._sheet.height:
            return rect.clamped(self._sheet.width, self._sheet.height)
        return rect.normalised()

    def _report_change(self) -> None:
        """Tell the tool page the rectangles moved.

        Not called `_notify`: Flet calls a control's `_notify(name, value)` as
        its property-change hook, so defining one with a different signature
        breaks every attribute assignment on this control.
        """
        if self.on_change is not None:
            self.on_change()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def refresh(self, *, coalesce: bool = False) -> None:
        """Redraw. With `coalesce`, hold the frame if one just went out.

        Pointer and wheel events arrive faster than a frame can be drawn and
        acknowledged. Rendering every one of them does not make the drag
        smoother - it builds a backlog, so the box trails further behind the
        cursor the longer you drag.

        A held frame is not thrown away: a trailing render is queued for the
        end of the interval. Dropping it outright meant the last event of a
        burst never landed unless something else redrew afterwards, which is
        what left a wheel zoom one notch behind where it should have been.
        """
        if not self._ready:
            return
        if coalesce:
            wait = FRAME_INTERVAL - (time.monotonic() - self._last_frame)
            if wait > 0:
                self._schedule_trailing(wait)
                return
        self._paint_frame()

    def _paint_frame(self) -> None:
        """Render and push whichever layers actually changed."""
        self._last_frame = time.monotonic()
        self._render()
        try:
            self._canvas.update()
            if self._grid_dirty:
                self._grid_canvas.update()
                self._grid_dirty = False
            if self._image_dirty:
                self._image.update()
                self._image_dirty = False
        except Exception:
            pass

    def _schedule_trailing(self, wait: float) -> None:
        """Queue the held frame for the end of the interval.

        Only ever one in flight, so a burst of a hundred pointer events costs
        one trailing frame rather than a hundred queued tasks.
        """
        if self._trailing:
            return
        page = getattr(self.ctx, "page", None) if self.ctx is not None else None
        if page is None:
            return
        self._trailing = True
        try:
            page.run_task(self._trailing_frame, wait)
        except Exception:
            self._trailing = False

    async def _trailing_frame(self, wait: float) -> None:
        try:
            await asyncio.sleep(wait)
            if self._ready:
                self._paint_frame()
        finally:
            self._trailing = False

    # ------------------------------------------------------------------
    # Shape pooling
    #
    # A canvas of ~100 shapes rebuilt from scratch on every pan event means
    # ~100 brand-new controls to serialise per frame, which is what made this
    # lag. Reusing the instances turns each frame into a patch of the handful
    # of properties that actually moved.
    # ------------------------------------------------------------------

    def _paint(self, **spec: Any) -> Any:
        """A shared Paint. Paints are values, not controls, so sharing is safe."""
        key = tuple(sorted(spec.items(), key=lambda item: item[0]))
        paint = self._paints.get(key)
        if paint is None:
            paint = ft.Paint(**spec)
            self._paints[key] = paint
        return paint

    def _text_style(self, **spec: Any) -> Any:
        key = tuple(sorted(spec.items(), key=lambda item: item[0]))
        style = self._styles.get(key)
        if style is None:
            style = ft.TextStyle(**spec)
            self._styles[key] = style
        return style

    def _pooled(self, key: str, factory: Callable[[], Any], **props: Any) -> Any:
        """The shape stored under `key`, created once then updated in place.

        Only genuinely changed properties are assigned: an unchanged rect costs
        nothing to redraw, so dragging one box does not re-send the other six.
        """
        shape = self._pool.get(key)
        if shape is None:
            shape = factory()
            self._pool[key] = shape
            for name, value in props.items():
                setattr(shape, name, value)
            return shape
        for name, value in props.items():
            if getattr(shape, name) != value:
                setattr(shape, name, value)
        return shape

    def _pooled_rect(self, key: str, x: float, y: float, width: float,
                     height: float, paint: Any) -> Any:
        return self._pooled(key, lambda: cv.Rect(x=0, y=0, width=0, height=0),
                            x=x, y=y, width=width, height=height, paint=paint)

    def _pooled_text(self, key: str, x: float, y: float, value: str,
                     style: Any) -> Any:
        return self._pooled(key, lambda: cv.Text(x=0, y=0, value=""),
                            x=x, y=y, value=value, style=style)

    def _texture_key(self) -> Optional[tuple]:
        """Identifies the texture file's current contents, without reading it."""
        if self._texture is None:
            return None
        try:
            stat = self._texture.stat()
        except OSError:
            return None
        return (str(self._texture), stat.st_mtime_ns, stat.st_size)

    def _place_texture(self, src: Optional[tuple], x: float = 0.0, y: float = 0.0,
                       width: float = 0.0, height: float = 0.0) -> None:
        """Move the texture widget under the current view transform.

        Only the source and the four geometry numbers ever change, so Flutter
        keeps the decoded bitmap and just lays it out again.
        """
        image = self._image
        if src is None:
            if image.visible:
                image.visible = False
                self._image_dirty = True
            return
        if self._image_src != src:
            image.src = self._texture_data
            self._image_src = src
            self._image_dirty = True
        if not image.visible:
            image.visible = True
            self._image_dirty = True
        for name, value in (("left", x), ("top", y),
                            ("width", width), ("height", height)):
            if getattr(image, name) != value:
                setattr(image, name, value)
                self._image_dirty = True

    def _render(self) -> None:
        width, height = self._sheet.size
        if self._texture_data is not None and width and height:
            x, y = self.to_screen(0, 0)
            self._place_texture(self._texture_id, x, y,
                                width * self._zoom, height * self._zoom)
        else:
            self._place_texture(None)
        self._render_static()
        self._render_overlay()

    def _render_static(self) -> None:
        """The layer that only moves when the view does: sheet edge and grid.

        Returns without touching its canvas unless the view transform or the
        grid's appearance actually changed, which is the whole point of giving
        it a canvas of its own - a rectangle drag moves neither.
        """
        key = (self._grid_key(), self._texture is not None)
        if self._static_key == key:
            return
        self._static_key = key

        shapes: list[Any] = []
        width, height = self._sheet.size
        if self._texture is not None and width and height:
            x, y = self.to_screen(0, 0)
            shapes.append(self._pooled_rect(
                "sheetEdge", x, y, width * self._zoom, height * self._zoom,
                self._paint(style=ft.PaintingStyle.STROKE, stroke_width=1,
                            color="#5a6270")))
        shapes.extend(self._grid_shapes())
        self._grid_canvas.shapes = shapes
        self._grid_dirty = True

    def _render_overlay(self) -> None:
        """The layer a drag actually moves: rectangles, handles, preview."""
        shapes = self._rect_shapes()
        if self._preview is not None:
            px, py = self.to_screen(self._preview.x, self._preview.y)
            shapes.append(self._pooled_rect(
                "preview", px, py, self._preview.w * self._zoom,
                self._preview.h * self._zoom,
                self._paint(style=ft.PaintingStyle.STROKE, stroke_width=2,
                            color="#22d3ee")))
        self._canvas.shapes = shapes

    def _grid_step(self) -> int:
        """The grid interval to actually draw at, in texture pixels.

        A 16 px grid on a 2048 px sheet zoomed to fit is a line every four
        screen pixels: unreadable, and thousands of shapes to serialise. Double
        the interval until the spacing means something. Returns 0 when even that
        cannot help, which is the caller's cue to draw nothing.
        """
        width, height = self._sheet.size
        step = self._grid
        floor = _GRID_MIN_SPACING.get(self._grid_style, 14.0)
        while step * self._zoom < floor:
            step *= 2
        return 0 if step > max(width, height) else step

    def _grid_paints(self) -> tuple[Any, Any]:
        """Minor and major paints for the current grid appearance.

        Built with `with_opacity` rather than a hex literal on purpose: Flet
        reads "#aarrggbb", alpha FIRST, so the intuitive "#ffffff1f" is not
        faint white at all - it is opaque yellow.
        """
        ink = GRID_COLORS.get(self._grid_color, "#ffffff")
        minor = round(self._grid_opacity, 4)
        # Majors carry the reading, so they scale with the minor opacity but
        # keep a floor - otherwise a near-invisible grid loses its landmarks.
        # Rounded because the alpha goes over the wire as a decimal string.
        major = round(min(1.0, max(minor * 2.5, minor + 0.06)), 4)
        if self._grid_style == "lines":
            return (ft.Paint(stroke_width=1,
                             color=ft.Colors.with_opacity(minor, ink)),
                    ft.Paint(stroke_width=1,
                             color=ft.Colors.with_opacity(major, ink)))
        # Dots and crosses are drawn through Points, where stroke_width is the
        # mark size rather than a line thickness.
        cap = ft.StrokeCap.ROUND if self._grid_style == "dots" \
            else ft.StrokeCap.BUTT
        return (ft.Paint(stroke_width=2.0, stroke_cap=cap,
                         color=ft.Colors.with_opacity(minor, ink)),
                ft.Paint(stroke_width=3.5, stroke_cap=cap,
                         color=ft.Colors.with_opacity(major, ink)))

    def _is_major(self, x: int, y: int, step: int) -> bool:
        if self._grid_major <= 0:
            return False
        span = step * self._grid_major
        return x % span == 0 or y % span == 0

    def _grid_key(self) -> tuple:
        """Everything the grid's geometry and ink depend on."""
        return (self._show_grid, self._grid, self._grid_style, self._grid_color,
                round(self._grid_opacity, 4), self._grid_major,
                round(self._zoom, 6), round(self._pan[0], 2),
                round(self._pan[1], 2), self._sheet.size, self._view)

    def _visible_texture_rect(self) -> tuple[float, float, float, float]:
        """The part of the texture currently on screen, in texture pixels."""
        x0, y0 = self.to_texture(0.0, 0.0)
        x1, y1 = self.to_texture(self._view[0], self._view[1])
        return x0, y0, x1, y1

    @staticmethod
    def _grid_span(step: int, extent: int, lo: float, hi: float) -> range:
        """The multiples of `step` in 0..extent that fall between lo and hi.

        Zoomed in, nearly every grid line is off screen: a 16 px grid on a
        2048 sheet is 258 lines however close you are, and the dotted style is
        16k intersections, almost all of them outside the viewport. Emitting
        only what is visible is what keeps a zoom or pan frame's cost
        independent of the sheet size and the zoom level - measured at 6x on a
        2048 sheet, it takes the grid from 259 shapes to about 20.
        """
        start = max(0, int(math.floor(lo / step)) * step)
        end = min(extent, int(math.ceil(hi / step)) * step)
        return range(start, end + 1, step) if end >= start else range(0)

    def _grid_shapes(self) -> list[Any]:
        """The grid, rebuilt only when the view or its appearance changed.

        Moving a rectangle does not move the grid, so the common case during a
        drag is to hand back the identical shape objects - nothing for Flet to
        diff, and the grid is the bulk of the shape count.
        """
        key = self._grid_key()
        if self._grid_cache is not None and self._grid_cache[0] == key:
            return self._grid_cache[1]
        shapes = self._build_grid_shapes()
        self._grid_cache = (key, shapes)
        return shapes

    def _build_grid_shapes(self) -> list[Any]:
        width, height = self._sheet.size
        if not (self._show_grid and width and height and self._grid > 1):
            return []
        step = self._grid_step()
        if not step:
            return []
        minor, major = self._grid_paints()
        if self._grid_style == "lines":
            return self._grid_line_shapes(step, minor, major)
        return self._grid_mark_shapes(step, minor, major)

    def _grid_line_shapes(self, step: int, minor: Any, major: Any) -> list[Any]:
        """Grid lines, pooled so a pan moves them instead of replacing them.

        Each line still spans the whole sheet, so the grid stops at the texture
        edge; only lines that are actually on screen are emitted.
        """
        width, height = self._sheet.size
        shapes: list[Any] = []
        top_y = self.to_screen(0, 0)[1]
        bottom_y = self.to_screen(0, height)[1]
        left_x = self.to_screen(0, 0)[0]
        right_x = self.to_screen(width, 0)[0]
        left, top, right, bottom = self._visible_texture_rect()

        for slot, value in enumerate(self._grid_span(step, width, left, right)):
            sx = self.to_screen(value, 0)[0]
            shapes.append(self._pooled(
                f"gridV:{slot}", lambda: cv.Line(0.0, 0.0, 0.0, 0.0),
                x1=sx, y1=top_y, x2=sx, y2=bottom_y,
                paint=major if self._is_major(value, 1, step) else minor))
        for slot, value in enumerate(self._grid_span(step, height, top, bottom)):
            sy = self.to_screen(0, value)[1]
            shapes.append(self._pooled(
                f"gridH:{slot}", lambda: cv.Line(0.0, 0.0, 0.0, 0.0),
                x1=left_x, y1=sy, x2=right_x, y2=sy,
                paint=major if self._is_major(1, value, step) else minor))
        return shapes

    def _grid_mark_shapes(self, step: int, minor: Any, major: Any) -> list[Any]:
        """Dots or crosses at every intersection.

        Batched into at most four Points shapes rather than one per mark: a
        1024-intersection grid would otherwise be 1024 controls to diff on every
        pan frame.
        """
        width, height = self._sheet.size
        crosses = self._grid_style == "crosses"
        arm = 3.0
        minor_pts: list[Any] = []
        major_pts: list[Any] = []
        left, top, right, bottom = self._visible_texture_rect()
        columns = self._grid_span(step, width, left, right)

        for gy in self._grid_span(step, height, top, bottom):
            for gx in columns:
                sx, sy = self.to_screen(gx, gy)
                bucket = major_pts if self._is_major(gx, gy, step) else minor_pts
                if crosses:
                    bucket.extend((ft.Offset(sx - arm, sy),
                                   ft.Offset(sx + arm, sy),
                                   ft.Offset(sx, sy - arm),
                                   ft.Offset(sx, sy + arm)))
                else:
                    bucket.append(ft.Offset(sx, sy))

        mode = cv.PointMode.LINES if crosses else cv.PointMode.POINTS
        shapes: list[Any] = []
        for name, points, paint in (("minor", minor_pts, minor),
                                    ("major", major_pts, major)):
            if points:
                shapes.append(self._pooled(
                    f"gridPts:{name}", lambda: cv.Points(points=[]),
                    points=points, point_mode=mode, paint=paint))
        return shapes

    def _visible_rect_filter(self) -> Callable[[Rect], bool]:
        """Whether a rectangle is worth drawing at the current view.

        Same reasoning as the grid: zoomed into one corner of a busy sheet,
        most rectangles are off screen, and each one costs four shapes to
        patch on every drag frame. Hit-testing still sees them all, so this is
        purely what gets drawn. The margin keeps a rectangle whose handles
        reach into the viewport from popping out at the edge.
        """
        left, top, right, bottom = self._visible_texture_rect()
        margin = HANDLE_GRAB / max(self._zoom, 1e-6)
        left, top = left - margin, top - margin
        right, bottom = right + margin, bottom + margin

        def visible(rect: Rect) -> bool:
            return not (rect.x2 < left or rect.x > right
                        or rect.y2 < top or rect.y > bottom)
        return visible

    def _rect_shapes(self) -> list[Any]:
        shapes: list[Any] = []
        selected = self._selection
        visible = self._visible_rect_filter()

        for index, rect in enumerate(self._sheet.rects):
            if not visible(rect):
                continue
            colour = PALETTE[index % len(PALETTE)]
            is_selected = index in selected
            x, y = self.to_screen(rect.x, rect.y)
            w, h = rect.w * self._zoom, rect.h * self._zoom

            shapes.append(self._pooled_rect(
                f"fill:{index}", x, y, w, h,
                self._paint(style=ft.PaintingStyle.FILL,
                            color=ft.Colors.with_opacity(
                                0.30 if is_selected
                                else (0.18 if index == self._hover else 0.10),
                                colour))))
            shapes.append(self._pooled_rect(
                f"edge:{index}", x, y, w, h,
                self._paint(style=ft.PaintingStyle.STROKE,
                            stroke_width=3 if is_selected else 1.5,
                            color=colour)))

            if self._show_labels and w > 34 and h > 18:
                shapes.append(self._pooled_text(
                    f"label:{index}", x + 5, y + 3,
                    rect.label or str(index + 1),
                    self._text_style(size=11, color=colour,
                                     weight=ft.FontWeight.W_700)))
                flags = "".join(letter for letter, on in
                                (("R", rect.rotate), ("M", rect.reflect),
                                 ("A", rect.alt), ("T", rect.tiling)) if on)
                if flags and h > 34:
                    shapes.append(self._pooled_text(
                        f"flags:{index}", x + 5, y + 17, flags,
                        self._text_style(size=10,
                                         color=ft.Colors.with_opacity(
                                             0.67, ft.Colors.WHITE))))

        half = HANDLE_SIZE / 2.0
        outline = self._paint(style=ft.PaintingStyle.STROKE, stroke_width=1,
                              color="#0d0f12")
        for index in sorted(selected):
            if index >= len(self._sheet.rects):
                continue
            rect = self._sheet.rects[index]
            if not visible(rect):
                continue
            colour = PALETTE[index % len(PALETTE)]
            fill = self._paint(style=ft.PaintingStyle.FILL, color=colour)
            for handle, (fx, fy) in enumerate(HANDLES):
                hx, hy = self.to_screen(rect.x + rect.w * fx,
                                        rect.y + rect.h * fy)
                shapes.append(self._pooled_rect(
                    f"handle:{index}:{handle}", hx - half, hy - half,
                    HANDLE_SIZE, HANDLE_SIZE, fill))
                shapes.append(self._pooled_rect(
                    f"handleEdge:{index}:{handle}", hx - half, hy - half,
                    HANDLE_SIZE, HANDLE_SIZE, outline))
        return shapes
