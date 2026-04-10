"""
Hotspot Editor Tool - Create and edit .rect hotspot files for Hammer++/Strata.

Draw axis-aligned rectangles over a texture preview, assign flags,
and export in the Valve .rect text format used by hotspot texturing.
"""

import os
import random
from pathlib import Path

from PySide6.QtCore import Qt, QRectF, QPointF, Signal
from PySide6.QtGui import (
    QPixmap, QImage, QPen, QColor, QBrush, QPainter, QKeySequence,
    QShortcut, QTransform,
)
from PySide6.QtWidgets import (
    QGraphicsView, QGraphicsScene, QGraphicsRectItem, QGraphicsPixmapItem,
    QGroupBox, QHBoxLayout, QVBoxLayout, QLabel, QLineEdit, QPushButton,
    QFileDialog, QCheckBox, QComboBox, QSpinBox, QTreeWidget, QTreeWidgetItem,
    QSplitter, QWidget, QMessageBox, QHeaderView, QDialog, QDoubleSpinBox,
    QFormLayout, QDialogButtonBox, QMenu,
)

from app.tools.base_tool import BaseTool
from app.core.settings import Settings


# ---------------------------------------------------------------------------
# Neon colour palette for rectangle outlines
# ---------------------------------------------------------------------------

NEON_PALETTE = [
    (255, 20, 147),   # DeepPink
    (0, 255, 255),    # Cyan
    (57, 255, 20),    # Neon Green
    (255, 105, 180),  # HotPink
    (255, 0, 255),    # Magenta
    (255, 255, 0),    # Yellow
    (0, 191, 255),    # DeepSkyBlue
    (255, 140, 0),    # DarkOrange
    (173, 255, 47),   # GreenYellow
    (0, 255, 127),    # SpringGreen
]

# ---------------------------------------------------------------------------
# Preset rect layouts (name -> list of (x, y, w, h) in normalised 0-1 space)
# ---------------------------------------------------------------------------

RECT_PRESETS = {
    "Full Texture": [(0, 0, 1, 1)],
    "2x1 Horizontal": [(0, 0, 0.5, 1), (0.5, 0, 0.5, 1)],
    "1x2 Vertical": [(0, 0, 1, 0.5), (0, 0.5, 1, 0.5)],
    "2x2 Grid": [
        (0, 0, 0.5, 0.5), (0.5, 0, 0.5, 0.5),
        (0, 0.5, 0.5, 0.5), (0.5, 0.5, 0.5, 0.5),
    ],
    "3x1 Horizontal Strips": [
        (0, 0, 1/3, 1), (1/3, 0, 1/3, 1), (2/3, 0, 1/3, 1),
    ],
    "1x3 Vertical Strips": [
        (0, 0, 1, 1/3), (0, 1/3, 1, 1/3), (0, 2/3, 1, 1/3),
    ],
    "4x4 Grid": [
        (c / 4, r / 4, 0.25, 0.25) for r in range(4) for c in range(4)
    ],
    "Top Bar + Bottom Panel": [
        (0, 0, 1, 0.125), (0, 0.125, 1, 0.875),
    ],
    "Trim Sheet (4 Rows)": [
        (0, r / 4, 1, 0.25) for r in range(4)
    ],
}


# ---------------------------------------------------------------------------
# Custom QGraphicsRectItem that stores rect metadata
# ---------------------------------------------------------------------------

class HotspotRectItem(QGraphicsRectItem):
    """A rectangle on the canvas representing one hotspot region."""

    def __init__(self, rect: QRectF, color: QColor, glow: QColor):
        super().__init__(rect)
        self.base_color = color
        self.glow_color = glow
        self.flags_data = {"rotate": 0, "reflect": 0, "alt": 0}
        self._selected_visual = False
        self._apply_pen()

    def _apply_pen(self):
        if self._selected_visual:
            pen = QPen(self.base_color, 3, Qt.DashLine)
        else:
            pen = QPen(self.base_color, 2, Qt.DashLine)
        self.setPen(pen)
        self.setBrush(QBrush(QColor(self.base_color.red(), self.base_color.green(),
                                     self.base_color.blue(), 30)))

    def set_selected_visual(self, selected: bool):
        self._selected_visual = selected
        self._apply_pen()

    def pixel_min(self):
        r = self.rect()
        return (int(r.x()), int(r.y()))

    def pixel_max(self):
        r = self.rect()
        return (int(r.x() + r.width()), int(r.y() + r.height()))


# ---------------------------------------------------------------------------
# Resize handle identifiers
# ---------------------------------------------------------------------------

HANDLE_NONE = -1
HANDLE_TL = 0   # top-left
HANDLE_T  = 1   # top-center
HANDLE_TR = 2   # top-right
HANDLE_R  = 3   # middle-right
HANDLE_BR = 4   # bottom-right
HANDLE_B  = 5   # bottom-center
HANDLE_BL = 6   # bottom-left
HANDLE_L  = 7   # middle-left

HANDLE_CURSORS = {
    HANDLE_TL: Qt.SizeFDiagCursor,
    HANDLE_T:  Qt.SizeVerCursor,
    HANDLE_TR: Qt.SizeBDiagCursor,
    HANDLE_R:  Qt.SizeHorCursor,
    HANDLE_BR: Qt.SizeFDiagCursor,
    HANDLE_B:  Qt.SizeVerCursor,
    HANDLE_BL: Qt.SizeBDiagCursor,
    HANDLE_L:  Qt.SizeHorCursor,
}


# ---------------------------------------------------------------------------
# Graphics view with zoom / pan / drawing
# ---------------------------------------------------------------------------

class HotspotGraphicsView(QGraphicsView):
    """Custom view that supports zoom-to-cursor, middle-click pan, rect drawing,
    click-to-select, drag-to-move, and resize handles."""

    rect_drawn = Signal(QRectF)       # emitted when user finishes drawing a new rect
    rect_clicked = Signal(int)        # index of HotspotRectItem clicked (-1 = deselect)
    rect_modified = Signal()          # emitted after a move or resize is committed

    def __init__(self, scene: QGraphicsScene, parent=None):
        super().__init__(scene, parent)
        self.setRenderHint(QPainter.Antialiasing)
        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setTransformationAnchor(QGraphicsView.NoAnchor)
        self.setResizeAnchor(QGraphicsView.NoAnchor)
        self.setBackgroundBrush(QBrush(QColor("#1e1e1e")))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._zoom = 1.0
        self._panning = False
        self._pan_start = QPointF()
        self._drawing = False
        self._draw_origin = QPointF()
        self._temp_rect: QGraphicsRectItem | None = None
        self.snap_enabled = True
        self.grid_size = 16
        self.drawing_enabled = True  # can be toggled off

        # Move / resize state
        self._moving = False
        self._move_origin = QPointF()
        self._resizing = False
        self._resize_handle = HANDLE_NONE
        self._resize_origin = QPointF()
        self._resize_orig_rect = QRectF()
        self._active_item: HotspotRectItem | None = None

        # Reference to tool's rect list for hit-testing
        self.rect_items_ref: list[HotspotRectItem] = []

        # Resize handle visuals
        self._handle_items: list[QGraphicsRectItem] = []

    # -- handle size in scene coordinates (constant screen size) --
    def _handle_half(self) -> float:
        return max(3.0, 5.0 / self._zoom)

    def _handle_positions(self, r: QRectF) -> list[QPointF]:
        """Return the 8 handle centre points for a rect."""
        cx = r.x() + r.width() / 2
        cy = r.y() + r.height() / 2
        return [
            QPointF(r.left(),  r.top()),      # TL
            QPointF(cx,        r.top()),      # T
            QPointF(r.right(), r.top()),      # TR
            QPointF(r.right(), cy),           # R
            QPointF(r.right(), r.bottom()),   # BR
            QPointF(cx,        r.bottom()),   # B
            QPointF(r.left(),  r.bottom()),   # BL
            QPointF(r.left(),  cy),           # L
        ]

    def show_handles(self, item: HotspotRectItem | None):
        """Draw resize handles around the given item (or clear them)."""
        self._clear_handles()
        if item is None:
            return
        h = self._handle_half()
        positions = self._handle_positions(item.rect())
        pen = QPen(QColor(255, 255, 255), 0)
        brush = QBrush(QColor(item.base_color.red(), item.base_color.green(),
                               item.base_color.blue(), 200))
        for pt in positions:
            handle = self.scene().addRect(
                QRectF(pt.x() - h, pt.y() - h, h * 2, h * 2), pen, brush
            )
            handle.setZValue(100)
            self._handle_items.append(handle)

    def _clear_handles(self):
        for hi in self._handle_items:
            self.scene().removeItem(hi)
        self._handle_items.clear()

    def _hit_handle(self, scene_pos: QPointF, item: HotspotRectItem) -> int:
        """Return handle index if scene_pos is over a handle of item, else HANDLE_NONE."""
        h = self._handle_half() * 1.5  # slightly generous hit area
        positions = self._handle_positions(item.rect())
        for i, pt in enumerate(positions):
            if abs(scene_pos.x() - pt.x()) <= h and abs(scene_pos.y() - pt.y()) <= h:
                return i
        return HANDLE_NONE

    def _hit_rect(self, scene_pos: QPointF) -> int:
        """Return index of the topmost HotspotRectItem under scene_pos, or -1."""
        for i in reversed(range(len(self.rect_items_ref))):
            if self.rect_items_ref[i].rect().contains(scene_pos):
                return i
        return -1

    # -- zoom --
    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        new_zoom = self._zoom * factor
        if new_zoom < 0.05 or new_zoom > 50.0:
            return
        old_pos = self.mapToScene(event.position().toPoint())
        self.scale(factor, factor)
        self._zoom *= factor
        new_pos = self.mapToScene(event.position().toPoint())
        delta = new_pos - old_pos
        self.translate(delta.x(), delta.y())
        # Refresh handle sizes
        if self._active_item:
            self.show_handles(self._active_item)

    # -- mouse press --
    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._panning = True
            self._pan_start = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            return

        if event.button() == Qt.LeftButton:
            scene_pos = self.mapToScene(event.position().toPoint())

            # 1. Check resize handles on selected item first
            if self._active_item is not None:
                handle = self._hit_handle(scene_pos, self._active_item)
                if handle != HANDLE_NONE:
                    self._resizing = True
                    self._resize_handle = handle
                    self._resize_origin = self._snap_point(scene_pos)
                    self._resize_orig_rect = QRectF(self._active_item.rect())
                    self.setCursor(HANDLE_CURSORS.get(handle, Qt.SizeAllCursor))
                    return

            # 2. Check if clicking on any rect (select / start move)
            hit = self._hit_rect(scene_pos)
            if hit >= 0:
                self.rect_clicked.emit(hit)
                self._active_item = self.rect_items_ref[hit]
                self._moving = True
                self._move_origin = self._snap_point(scene_pos)
                self.setCursor(Qt.SizeAllCursor)
                return

            # 3. Click on empty area: deselect, then start drawing
            if self._active_item is not None:
                self.rect_clicked.emit(-1)
                self._active_item = None
                self._clear_handles()

            if self.drawing_enabled:
                self._drawing = True
                self._draw_origin = self._snap_point(scene_pos)
                pen = QPen(QColor("#00ffff"), 2, Qt.DashLine)
                self._temp_rect = self.scene().addRect(
                    QRectF(self._draw_origin, self._draw_origin), pen
                )
                return

        super().mousePressEvent(event)

    # -- mouse move --
    def mouseMoveEvent(self, event):
        scene_pos = self.mapToScene(event.position().toPoint())

        if self._panning:
            delta = event.position() - self._pan_start
            self._pan_start = event.position()
            self.translate(delta.x() / self._zoom, delta.y() / self._zoom)
            return

        if self._resizing and self._active_item:
            cur = self._snap_point(scene_pos)
            self._apply_resize(cur)
            return

        if self._moving and self._active_item:
            cur = self._snap_point(scene_pos)
            dx = cur.x() - self._move_origin.x()
            dy = cur.y() - self._move_origin.y()
            if dx != 0 or dy != 0:
                r = self._active_item.rect()
                self._active_item.setRect(QRectF(r.x() + dx, r.y() + dy,
                                                  r.width(), r.height()))
                self._move_origin = cur
                self.show_handles(self._active_item)
            return

        if self._drawing and self._temp_rect:
            current = self._snap_point(scene_pos)
            r = QRectF(self._draw_origin, current).normalized()
            self._temp_rect.setRect(r)
            return

        # Hover cursor: show resize cursor when over a handle
        if self._active_item is not None:
            handle = self._hit_handle(scene_pos, self._active_item)
            if handle != HANDLE_NONE:
                self.setCursor(HANDLE_CURSORS.get(handle, Qt.ArrowCursor))
            else:
                hit = self._hit_rect(scene_pos)
                self.setCursor(Qt.SizeAllCursor if hit >= 0 else Qt.ArrowCursor)
        else:
            self.setCursor(Qt.ArrowCursor)

        super().mouseMoveEvent(event)

    # -- mouse release --
    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton and self._panning:
            self._panning = False
            self.setCursor(Qt.ArrowCursor)
            return

        if event.button() == Qt.LeftButton:
            if self._resizing:
                self._resizing = False
                self._resize_handle = HANDLE_NONE
                self.setCursor(Qt.ArrowCursor)
                self.rect_modified.emit()
                if self._active_item:
                    self.show_handles(self._active_item)
                return

            if self._moving:
                self._moving = False
                self.setCursor(Qt.ArrowCursor)
                self.rect_modified.emit()
                if self._active_item:
                    self.show_handles(self._active_item)
                return

            if self._drawing:
                self._drawing = False
                if self._temp_rect:
                    current = self._snap_point(self.mapToScene(event.position().toPoint()))
                    r = QRectF(self._draw_origin, current).normalized()
                    self.scene().removeItem(self._temp_rect)
                    self._temp_rect = None
                    if r.width() >= 1 and r.height() >= 1:
                        self.rect_drawn.emit(r)
                return

        super().mouseReleaseEvent(event)

    # -- resize logic --
    def _apply_resize(self, cur: QPointF):
        """Update the active item's rect based on the current handle drag position."""
        orig = self._resize_orig_rect
        dx = cur.x() - self._resize_origin.x()
        dy = cur.y() - self._resize_origin.y()
        h = self._resize_handle

        x0 = orig.x()
        y0 = orig.y()
        x1 = orig.x() + orig.width()
        y1 = orig.y() + orig.height()

        # Adjust edges based on which handle is being dragged
        if h in (HANDLE_TL, HANDLE_T, HANDLE_TR):
            y0 += dy
        if h in (HANDLE_BL, HANDLE_B, HANDLE_BR):
            y1 += dy
        if h in (HANDLE_TL, HANDLE_L, HANDLE_BL):
            x0 += dx
        if h in (HANDLE_TR, HANDLE_R, HANDLE_BR):
            x1 += dx

        # Enforce minimum size (1 pixel)
        if x1 - x0 < 1:
            if h in (HANDLE_TL, HANDLE_L, HANDLE_BL):
                x0 = x1 - 1
            else:
                x1 = x0 + 1
        if y1 - y0 < 1:
            if h in (HANDLE_TL, HANDLE_T, HANDLE_TR):
                y0 = y1 - 1
            else:
                y1 = y0 + 1

        new_rect = QRectF(x0, y0, x1 - x0, y1 - y0)
        self._active_item.setRect(new_rect)
        self.show_handles(self._active_item)

    # -- snapping helper --
    def _snap_point(self, pt: QPointF) -> QPointF:
        if not self.snap_enabled:
            return pt
        g = max(1, self.grid_size)
        sx = round(pt.x() / g) * g
        sy = round(pt.y() / g) * g
        return QPointF(sx, sy)

    def reset_view(self):
        self.resetTransform()
        self._zoom = 1.0

    def fit_image(self, pixmap_item: QGraphicsPixmapItem):
        if pixmap_item is None:
            return
        self.fitInView(pixmap_item, Qt.KeepAspectRatio)
        self._zoom = self.transform().m11()


# ---------------------------------------------------------------------------
# Main tool widget
# ---------------------------------------------------------------------------

class HotspotEditorTool(BaseTool):
    """PySide6 hotspot .rect editor with canvas, grid, presets, and flags."""

    def __init__(self):
        super().__init__("Hotspot Editor")
        self.settings = Settings()
        self.rect_items: list[HotspotRectItem] = []
        self.selected_index: int | None = None
        self.pixmap_item: QGraphicsPixmapItem | None = None
        self.image_path = ""
        self.image_width = 0
        self.image_height = 0
        self.grid_lines: list = []

        # undo / redo stacks  (each entry is a list of serialised rects)
        self.undo_stack: list[list[dict]] = []
        self.redo_stack: list[list[dict]] = []

        self._colour_index = 0
        self._setup_tool_ui()
        self._setup_shortcuts()

    # ---------------------------------------------------------------
    # UI construction
    # ---------------------------------------------------------------

    def _setup_tool_ui(self):
        # Top bar: file paths
        paths_group = QGroupBox("Paths")
        paths_layout = QVBoxLayout()

        # Image row
        img_row = QHBoxLayout()
        img_row.addWidget(QLabel("Image:"))
        self.image_input = QLineEdit()
        self.image_input.setPlaceholderText("Select texture image (PNG/TGA/JPG/BMP)...")
        img_row.addWidget(self.image_input)
        img_browse = QPushButton("Browse...")
        img_browse.clicked.connect(self._browse_image)
        img_row.addWidget(img_browse)
        img_load = QPushButton("Load")
        img_load.clicked.connect(self._load_image_from_input)
        img_row.addWidget(img_load)
        paths_layout.addLayout(img_row)

        # Rect file row
        rect_row = QHBoxLayout()
        rect_row.addWidget(QLabel(".rect:"))
        self.rect_input = QLineEdit()
        self.rect_input.setPlaceholderText("Select or type .rect file path...")
        rect_row.addWidget(self.rect_input)
        rect_open = QPushButton("Open...")
        rect_open.clicked.connect(self._open_rect_file)
        rect_row.addWidget(rect_open)
        rect_save = QPushButton("Save As...")
        rect_save.clicked.connect(self._save_rect_file)
        rect_row.addWidget(rect_save)
        paths_layout.addLayout(rect_row)

        paths_group.setLayout(paths_layout)
        self.content_layout.addWidget(paths_group)

        # Main area: canvas + sidebar
        main_splitter = QSplitter(Qt.Horizontal)

        # -- Canvas --
        canvas_widget = QWidget()
        canvas_layout = QVBoxLayout(canvas_widget)
        canvas_layout.setContentsMargins(0, 0, 0, 0)

        self.scene = QGraphicsScene()
        self.view = HotspotGraphicsView(self.scene)
        self.view.rect_items_ref = self.rect_items
        self.view.rect_drawn.connect(self._on_rect_drawn)
        self.view.rect_clicked.connect(self._on_canvas_click)
        self.view.rect_modified.connect(self._on_canvas_modify)
        canvas_layout.addWidget(self.view)

        # Canvas toolbar
        canvas_toolbar = QHBoxLayout()
        fit_btn = QPushButton("Fit View")
        fit_btn.setMaximumWidth(80)
        fit_btn.clicked.connect(self._fit_view)
        canvas_toolbar.addWidget(fit_btn)
        reset_btn = QPushButton("Reset Zoom")
        reset_btn.setMaximumWidth(90)
        reset_btn.clicked.connect(self._reset_zoom)
        canvas_toolbar.addWidget(reset_btn)

        self.coords_label = QLabel("Cursor: -")
        canvas_toolbar.addWidget(self.coords_label)
        canvas_toolbar.addStretch()

        self.dimensions_label = QLabel("")
        canvas_toolbar.addWidget(self.dimensions_label)
        canvas_layout.addLayout(canvas_toolbar)

        main_splitter.addWidget(canvas_widget)

        # -- Sidebar --
        sidebar = QWidget()
        sidebar.setMinimumWidth(260)
        sidebar.setMaximumWidth(360)
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(4, 0, 4, 0)

        # Rectangle list
        list_group = QGroupBox("Rectangles")
        list_layout = QVBoxLayout()
        self.rect_tree = QTreeWidget()
        self.rect_tree.setHeaderLabels(["#", "Min (x y)", "Max (x y)", "Flags"])
        self.rect_tree.setColumnWidth(0, 30)
        self.rect_tree.setColumnWidth(1, 80)
        self.rect_tree.setColumnWidth(2, 80)
        self.rect_tree.header().setStretchLastSection(True)
        self.rect_tree.setSelectionMode(QTreeWidget.SingleSelection)
        self.rect_tree.currentItemChanged.connect(self._on_tree_selection_changed)
        self.rect_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.rect_tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        list_layout.addWidget(self.rect_tree)

        btn_row = QHBoxLayout()
        del_btn = QPushButton("Delete")
        del_btn.clicked.connect(self._delete_selected)
        btn_row.addWidget(del_btn)
        dup_btn = QPushButton("Duplicate")
        dup_btn.clicked.connect(self._duplicate_selected)
        btn_row.addWidget(dup_btn)
        clear_btn = QPushButton("Clear All")
        clear_btn.clicked.connect(self._clear_all_rects)
        btn_row.addWidget(clear_btn)
        list_layout.addLayout(btn_row)

        undo_row = QHBoxLayout()
        undo_btn = QPushButton("Undo")
        undo_btn.setToolTip("Ctrl+Z")
        undo_btn.clicked.connect(self._undo)
        undo_row.addWidget(undo_btn)
        redo_btn = QPushButton("Redo")
        redo_btn.setToolTip("Ctrl+Y / Ctrl+Shift+Z")
        redo_btn.clicked.connect(self._redo)
        undo_row.addWidget(redo_btn)
        list_layout.addLayout(undo_row)

        list_group.setLayout(list_layout)
        side_layout.addWidget(list_group)

        # Flags
        flags_group = QGroupBox("Flags (selected rect)")
        flags_layout = QVBoxLayout()
        self.flag_rotate = QCheckBox("rotate")
        self.flag_rotate.toggled.connect(self._apply_flags)
        flags_layout.addWidget(self.flag_rotate)
        self.flag_reflect = QCheckBox("reflect")
        self.flag_reflect.toggled.connect(self._apply_flags)
        flags_layout.addWidget(self.flag_reflect)
        self.flag_alt = QCheckBox("alt")
        self.flag_alt.toggled.connect(self._apply_flags)
        flags_layout.addWidget(self.flag_alt)
        flags_group.setLayout(flags_layout)
        side_layout.addWidget(flags_group)

        # Grid & Snap
        grid_group = QGroupBox("Grid && Snap")
        grid_layout = QVBoxLayout()
        self.snap_check = QCheckBox("Snap to grid")
        self.snap_check.setChecked(True)
        self.snap_check.toggled.connect(self._on_snap_toggled)
        grid_layout.addWidget(self.snap_check)
        self.show_grid_check = QCheckBox("Show grid")
        self.show_grid_check.setChecked(True)
        self.show_grid_check.toggled.connect(self._redraw_grid)
        grid_layout.addWidget(self.show_grid_check)

        gs_row = QHBoxLayout()
        gs_row.addWidget(QLabel("Grid size:"))
        self.grid_size_combo = QComboBox()
        for s in [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]:
            self.grid_size_combo.addItem(str(s), s)
        self.grid_size_combo.setCurrentText("16")
        self.grid_size_combo.currentIndexChanged.connect(self._on_grid_size_changed)
        gs_row.addWidget(self.grid_size_combo)
        grid_layout.addLayout(gs_row)
        grid_group.setLayout(grid_layout)
        side_layout.addWidget(grid_group)

        # Presets
        preset_group = QGroupBox("Presets")
        preset_layout = QVBoxLayout()
        self.preset_combo = QComboBox()
        for name in RECT_PRESETS:
            self.preset_combo.addItem(name)
        preset_layout.addWidget(self.preset_combo)
        apply_preset_btn = QPushButton("Apply Preset")
        apply_preset_btn.clicked.connect(self._apply_preset)
        preset_layout.addWidget(apply_preset_btn)
        preset_group.setLayout(preset_layout)
        side_layout.addWidget(preset_group)

        # Auto-detect
        detect_group = QGroupBox("Auto-detect (alpha)")
        detect_layout = QVBoxLayout()
        thr_row = QHBoxLayout()
        thr_row.addWidget(QLabel("Alpha threshold:"))
        self.alpha_threshold_spin = QSpinBox()
        self.alpha_threshold_spin.setRange(0, 255)
        self.alpha_threshold_spin.setValue(1)
        thr_row.addWidget(self.alpha_threshold_spin)
        detect_layout.addLayout(thr_row)
        detect_btn = QPushButton("Auto-detect Grid Cells")
        detect_btn.clicked.connect(self._auto_detect)
        detect_layout.addWidget(detect_btn)
        detect_group.setLayout(detect_layout)
        side_layout.addWidget(detect_group)

        # Scale rects
        scale_btn = QPushButton("Scale Rectangles...")
        scale_btn.clicked.connect(self._show_scale_dialog)
        side_layout.addWidget(scale_btn)

        # VMT snippet
        vmt_group = QGroupBox("VMT snippet")
        vmt_layout = QVBoxLayout()
        self.vmt_label = QLabel('%rectanglemap "<materials/path/to/texture>"')
        self.vmt_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.vmt_label.setWordWrap(True)
        vmt_layout.addWidget(self.vmt_label)
        vmt_group.setLayout(vmt_layout)
        side_layout.addWidget(vmt_group)

        # Help / Instructions
        help_group = QGroupBox("Help")
        help_layout = QVBoxLayout()
        help_label = QLabel(
            "<b>Drawing:</b> Left-click and drag on empty canvas to draw a new rectangle.<br>"
            "<b>Selecting:</b> Left-click on an existing rectangle to select it.<br>"
            "<b>Moving:</b> Left-click and drag a selected rectangle to reposition it.<br>"
            "<b>Resizing:</b> Drag the square handles on a selected rectangle's corners "
            "or edges to resize it.<br>"
            "<b>Panning:</b> Middle-click and drag to pan the view.<br>"
            "<b>Zooming:</b> Scroll the mouse wheel to zoom in/out.<br>"
            "<br>"
            "<b>Shortcuts:</b><br>"
            "Ctrl+Z &mdash; Undo<br>"
            "Ctrl+Y / Ctrl+Shift+Z &mdash; Redo<br>"
            "Delete &mdash; Delete selected rect<br>"
            "Ctrl+D &mdash; Duplicate selected rect<br>"
            "<br>"
            "<b>Flags</b> (Strata Hammer):<br>"
            "<i>rotate</i> &mdash; Allow the region to be rotated to fit.<br>"
            "<i>reflect</i> &mdash; Allow random horizontal flipping.<br>"
            "<i>alt</i> &mdash; Only chosen when Alt key is held.<br>"
            "<br>"
            "Place the exported .rect file next to your VMT/VTF and add "
            "<code>%rectanglemap</code> to your VMT to enable hotspot texturing."
        )
        help_label.setWordWrap(True)
        help_label.setTextFormat(Qt.RichText)
        help_label.setStyleSheet("font-size: 11px; color: #aaaaaa;")
        help_layout.addWidget(help_label)
        help_group.setLayout(help_layout)
        side_layout.addWidget(help_group)

        side_layout.addStretch()
        main_splitter.addWidget(sidebar)

        main_splitter.setStretchFactor(0, 3)
        main_splitter.setStretchFactor(1, 1)

        self.content_layout.addWidget(main_splitter, 1)

        # Track mouse position for coordinate readout
        self.view.setMouseTracking(True)
        self.view.viewport().installEventFilter(self)

    def _setup_shortcuts(self):
        QShortcut(QKeySequence("Ctrl+Z"), self, self._undo)
        QShortcut(QKeySequence("Ctrl+Y"), self, self._redo)
        QShortcut(QKeySequence("Ctrl+Shift+Z"), self, self._redo)
        QShortcut(QKeySequence("Delete"), self, self._delete_selected)
        QShortcut(QKeySequence("Ctrl+D"), self, self._duplicate_selected)

    # ---------------------------------------------------------------
    # Event filter for coordinate readout
    # ---------------------------------------------------------------

    def eventFilter(self, obj, event):
        if obj == self.view.viewport() and event.type() == event.Type.MouseMove:
            scene_pos = self.view.mapToScene(event.position().toPoint())
            ix, iy = int(scene_pos.x()), int(scene_pos.y())
            self.coords_label.setText(f"Cursor: {ix}, {iy}")
        return super().eventFilter(obj, event)

    # ---------------------------------------------------------------
    # Colour helpers
    # ---------------------------------------------------------------

    def _next_colour(self):
        r, g, b = NEON_PALETTE[self._colour_index % len(NEON_PALETTE)]
        self._colour_index += 1
        color = QColor(r, g, b)
        glow = QColor(max(0, r // 3), max(0, g // 3), max(0, b // 3))
        return color, glow

    def _random_colour(self):
        r, g, b = random.choice(NEON_PALETTE)
        color = QColor(r, g, b)
        glow = QColor(max(0, r // 3), max(0, g // 3), max(0, b // 3))
        return color, glow

    # ---------------------------------------------------------------
    # Image handling
    # ---------------------------------------------------------------

    def _browse_image(self):
        start_dir = self.settings.get("hotspot_last_image_dir", "") or ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Texture Image", start_dir,
            "Images (*.png *.jpg *.jpeg *.tga *.bmp);;All files (*.*)",
        )
        if path:
            self.image_input.setText(path)
            self.settings.set("hotspot_last_image_dir", str(Path(path).parent))
            self.settings.save()
            self._load_image(path)

    def _load_image_from_input(self):
        path = self.image_input.text().strip()
        if path:
            self._load_image(path)

    def _load_image(self, path: str):
        if not os.path.isfile(path):
            self.log(f"File not found: {path}", "ERROR")
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            self.log(f"Failed to load image: {path}", "ERROR")
            return

        self.image_path = path
        self.image_width = pixmap.width()
        self.image_height = pixmap.height()

        # Remove old pixmap
        if self.pixmap_item:
            self.scene.removeItem(self.pixmap_item)

        self.pixmap_item = QGraphicsPixmapItem(pixmap)
        self.pixmap_item.setZValue(-10)
        self.scene.addItem(self.pixmap_item)

        self._redraw_grid()
        self.view.fit_image(self.pixmap_item)
        self._update_vmt_snippet()
        self.dimensions_label.setText(f"{self.image_width} x {self.image_height}")
        self.log(f"Loaded image: {os.path.basename(path)} ({self.image_width}x{self.image_height})", "SUCCESS")
        self.emit_status(f"Loaded {os.path.basename(path)}")

    # ---------------------------------------------------------------
    # Grid drawing
    # ---------------------------------------------------------------

    def _redraw_grid(self, *_args):
        # Remove old grid lines
        for item in self.grid_lines:
            self.scene.removeItem(item)
        self.grid_lines.clear()

        if not self.show_grid_check.isChecked() or self.image_width == 0:
            return

        g = self.grid_size_combo.currentData() or 16
        iw, ih = self.image_width, self.image_height

        minor_pen = QPen(QColor(255, 255, 255, 25), 0)
        major_pen = QPen(QColor(255, 255, 255, 55), 0)
        major_every = 8

        cell = 0
        for x in range(0, iw + 1, g):
            pen = major_pen if cell % major_every == 0 else minor_pen
            line = self.scene.addLine(x, 0, x, ih, pen)
            line.setZValue(-5)
            self.grid_lines.append(line)
            cell += 1

        cell = 0
        for y in range(0, ih + 1, g):
            pen = major_pen if cell % major_every == 0 else minor_pen
            line = self.scene.addLine(0, y, iw, y, pen)
            line.setZValue(-5)
            self.grid_lines.append(line)
            cell += 1

    def _on_grid_size_changed(self):
        g = self.grid_size_combo.currentData() or 16
        self.view.grid_size = g
        self._redraw_grid()

    def _on_snap_toggled(self, checked):
        self.view.snap_enabled = checked

    # ---------------------------------------------------------------
    # Rectangle management
    # ---------------------------------------------------------------

    def _on_rect_drawn(self, rect: QRectF):
        """Called when user finishes drawing a new rectangle on the canvas."""
        if self.image_width == 0:
            self.log("Load an image before drawing rectangles", "WARNING")
            return
        # Clamp to image bounds
        x0 = max(0, int(rect.x()))
        y0 = max(0, int(rect.y()))
        x1 = min(self.image_width, int(rect.x() + rect.width()))
        y1 = min(self.image_height, int(rect.y() + rect.height()))
        if x1 - x0 < 1 or y1 - y0 < 1:
            return

        self._save_undo()
        self._add_rect(x0, y0, x1, y1)
        self.log(f"Added rect #{len(self.rect_items)}: ({x0},{y0}) -> ({x1},{y1})", "INFO")

    def _add_rect(self, x0, y0, x1, y1, flags=None, color=None, glow=None):
        """Create a HotspotRectItem and add it to the scene and tree."""
        if color is None:
            color, glow = self._next_colour()
        r = QRectF(x0, y0, x1 - x0, y1 - y0)
        item = HotspotRectItem(r, color, glow)
        item.setZValue(0)
        if flags:
            item.flags_data.update(flags)
        self.scene.addItem(item)
        self.rect_items.append(item)
        self._add_tree_row(len(self.rect_items) - 1)
        return item

    def _add_tree_row(self, idx):
        item = self.rect_items[idx]
        mn = item.pixel_min()
        mx = item.pixel_max()
        flags_str = self._flags_str(item.flags_data)
        tw = QTreeWidgetItem([str(idx + 1), f"{mn[0]} {mn[1]}", f"{mx[0]} {mx[1]}", flags_str])

        # Colour indicator
        tw.setForeground(0, item.base_color)
        self.rect_tree.addTopLevelItem(tw)

    def _rebuild_tree(self):
        self.rect_tree.clear()
        for i in range(len(self.rect_items)):
            self._add_tree_row(i)

    def _update_tree_row(self, idx):
        tw = self.rect_tree.topLevelItem(idx)
        if tw is None:
            return
        item = self.rect_items[idx]
        mn = item.pixel_min()
        mx = item.pixel_max()
        tw.setText(0, str(idx + 1))
        tw.setText(1, f"{mn[0]} {mn[1]}")
        tw.setText(2, f"{mx[0]} {mx[1]}")
        tw.setText(3, self._flags_str(item.flags_data))
        tw.setForeground(0, item.base_color)

    @staticmethod
    def _flags_str(flags: dict) -> str:
        parts = [k for k in ("rotate", "reflect", "alt") if flags.get(k)]
        return ", ".join(parts) if parts else "-"

    # ---------------------------------------------------------------
    # Selection
    # ---------------------------------------------------------------

    def _on_canvas_click(self, index: int):
        """Handle click-to-select from the canvas."""
        if index < 0:
            # Deselect
            if self.selected_index is not None and self.selected_index < len(self.rect_items):
                self.rect_items[self.selected_index].set_selected_visual(False)
            self.selected_index = None
            self.rect_tree.setCurrentItem(None)
            self._sync_flags_ui(None)
            self.view.show_handles(None)
            return
        if 0 <= index < len(self.rect_items):
            # Save undo before any move/resize starts
            self._save_undo()
            # Select via tree (triggers _on_tree_selection_changed)
            tw = self.rect_tree.topLevelItem(index)
            if tw:
                self.rect_tree.setCurrentItem(tw)

    def _on_canvas_modify(self):
        """Called when a move or resize drag finishes on the canvas."""
        # Rebuild tree row data to reflect new coordinates
        if self.selected_index is not None and self.selected_index < len(self.rect_items):
            self._update_tree_row(self.selected_index)
            self._sync_flags_ui(self.rect_items[self.selected_index])

    def _on_tree_selection_changed(self, current, _previous):
        # Deselect old
        if self.selected_index is not None and self.selected_index < len(self.rect_items):
            self.rect_items[self.selected_index].set_selected_visual(False)
        if current is None:
            self.selected_index = None
            self._sync_flags_ui(None)
            self.view.show_handles(None)
            return
        idx = self.rect_tree.indexOfTopLevelItem(current)
        if idx < 0 or idx >= len(self.rect_items):
            self.selected_index = None
            self._sync_flags_ui(None)
            self.view.show_handles(None)
            return
        self.selected_index = idx
        self.rect_items[idx].set_selected_visual(True)
        self._sync_flags_ui(self.rect_items[idx])
        self.view._active_item = self.rect_items[idx]
        self.view.show_handles(self.rect_items[idx])

    def _sync_flags_ui(self, item: HotspotRectItem | None):
        self.flag_rotate.blockSignals(True)
        self.flag_reflect.blockSignals(True)
        self.flag_alt.blockSignals(True)
        if item:
            self.flag_rotate.setChecked(bool(item.flags_data.get("rotate")))
            self.flag_reflect.setChecked(bool(item.flags_data.get("reflect")))
            self.flag_alt.setChecked(bool(item.flags_data.get("alt")))
        else:
            self.flag_rotate.setChecked(False)
            self.flag_reflect.setChecked(False)
            self.flag_alt.setChecked(False)
        self.flag_rotate.blockSignals(False)
        self.flag_reflect.blockSignals(False)
        self.flag_alt.blockSignals(False)

    def _apply_flags(self):
        if self.selected_index is None:
            return
        item = self.rect_items[self.selected_index]
        item.flags_data["rotate"] = 1 if self.flag_rotate.isChecked() else 0
        item.flags_data["reflect"] = 1 if self.flag_reflect.isChecked() else 0
        item.flags_data["alt"] = 1 if self.flag_alt.isChecked() else 0
        self._update_tree_row(self.selected_index)

    # ---------------------------------------------------------------
    # Delete / Duplicate / Clear
    # ---------------------------------------------------------------

    def _delete_selected(self):
        if self.selected_index is None:
            return
        self._save_undo()
        self.view._active_item = None
        self.view._clear_handles()
        item = self.rect_items.pop(self.selected_index)
        self.scene.removeItem(item)
        self.selected_index = None
        self._rebuild_tree()
        self.log(f"Deleted rectangle", "INFO")

    def _duplicate_selected(self):
        if self.selected_index is None:
            return
        self._save_undo()
        src = self.rect_items[self.selected_index]
        mn = src.pixel_min()
        mx = src.pixel_max()
        # Offset the duplicate slightly
        offset = self.view.grid_size
        new = self._add_rect(mn[0] + offset, mn[1] + offset, mx[0] + offset, mx[1] + offset,
                             flags=dict(src.flags_data))
        self.log(f"Duplicated rect #{self.selected_index + 1}", "INFO")

    def _clear_all_rects(self):
        if not self.rect_items:
            return
        reply = QMessageBox.question(self, "Clear All", "Remove all rectangles?",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply != QMessageBox.Yes:
            return
        self._save_undo()
        for item in self.rect_items:
            self.scene.removeItem(item)
        self.rect_items.clear()
        self.selected_index = None
        self._rebuild_tree()
        self.log("Cleared all rectangles", "INFO")

    # ---------------------------------------------------------------
    # Context menu on tree
    # ---------------------------------------------------------------

    def _on_tree_context_menu(self, pos):
        item = self.rect_tree.itemAt(pos)
        if item is None:
            return
        idx = self.rect_tree.indexOfTopLevelItem(item)
        menu = QMenu(self)
        menu.addAction("Delete", lambda: self._delete_at(idx))
        menu.addAction("Duplicate", lambda: self._duplicate_at(idx))
        menu.addSeparator()
        menu.addAction("Move to Top", lambda: self._move_rect(idx, 0))
        menu.addAction("Move to Bottom", lambda: self._move_rect(idx, len(self.rect_items) - 1))
        menu.exec(self.rect_tree.viewport().mapToGlobal(pos))

    def _delete_at(self, idx):
        if 0 <= idx < len(self.rect_items):
            self._save_undo()
            item = self.rect_items.pop(idx)
            self.scene.removeItem(item)
            self.selected_index = None
            self._rebuild_tree()

    def _duplicate_at(self, idx):
        if 0 <= idx < len(self.rect_items):
            self._save_undo()
            src = self.rect_items[idx]
            mn = src.pixel_min()
            mx = src.pixel_max()
            offset = self.view.grid_size
            self._add_rect(mn[0] + offset, mn[1] + offset, mx[0] + offset, mx[1] + offset,
                           flags=dict(src.flags_data))

    def _move_rect(self, from_idx, to_idx):
        if from_idx == to_idx:
            return
        self._save_undo()
        item = self.rect_items.pop(from_idx)
        self.rect_items.insert(to_idx, item)
        self.selected_index = None
        self._rebuild_tree()

    # ---------------------------------------------------------------
    # Undo / Redo
    # ---------------------------------------------------------------

    def _serialise_rects(self) -> list[dict]:
        result = []
        for item in self.rect_items:
            mn = item.pixel_min()
            mx = item.pixel_max()
            result.append({
                "min": mn, "max": mx,
                "flags": dict(item.flags_data),
                "color": (item.base_color.red(), item.base_color.green(), item.base_color.blue()),
                "glow": (item.glow_color.red(), item.glow_color.green(), item.glow_color.blue()),
            })
        return result

    def _restore_rects(self, data: list[dict]):
        self.view._active_item = None
        self.view._clear_handles()
        for item in self.rect_items:
            self.scene.removeItem(item)
        self.rect_items.clear()
        self.selected_index = None
        for d in data:
            r, g, b = d["color"]
            gr, gg, gb = d["glow"]
            self._add_rect(d["min"][0], d["min"][1], d["max"][0], d["max"][1],
                           flags=d.get("flags"),
                           color=QColor(r, g, b), glow=QColor(gr, gg, gb))
        self._rebuild_tree()

    def _save_undo(self):
        self.undo_stack.append(self._serialise_rects())
        self.redo_stack.clear()
        # Limit stack size
        if len(self.undo_stack) > 50:
            self.undo_stack.pop(0)

    def _undo(self):
        if not self.undo_stack:
            return
        self.redo_stack.append(self._serialise_rects())
        state = self.undo_stack.pop()
        self._restore_rects(state)
        self.log("Undo", "INFO")

    def _redo(self):
        if not self.redo_stack:
            return
        self.undo_stack.append(self._serialise_rects())
        state = self.redo_stack.pop()
        self._restore_rects(state)
        self.log("Redo", "INFO")

    # ---------------------------------------------------------------
    # View controls
    # ---------------------------------------------------------------

    def _fit_view(self):
        if self.pixmap_item:
            self.view.fit_image(self.pixmap_item)

    def _reset_zoom(self):
        self.view.reset_view()

    # ---------------------------------------------------------------
    # Presets
    # ---------------------------------------------------------------

    def _apply_preset(self):
        if self.image_width == 0:
            self.log("Load an image before applying a preset", "WARNING")
            return
        name = self.preset_combo.currentText()
        if name not in RECT_PRESETS:
            return

        self._save_undo()
        # Clear existing
        for item in self.rect_items:
            self.scene.removeItem(item)
        self.rect_items.clear()
        self.selected_index = None

        iw, ih = self.image_width, self.image_height
        g = self.view.grid_size if self.view.snap_enabled else 1

        for (nx, ny, nw, nh) in RECT_PRESETS[name]:
            x0 = int(nx * iw)
            y0 = int(ny * ih)
            x1 = int((nx + nw) * iw)
            y1 = int((ny + nh) * ih)
            # Snap
            if g > 1:
                x0 = round(x0 / g) * g
                y0 = round(y0 / g) * g
                x1 = round(x1 / g) * g
                y1 = round(y1 / g) * g
            x1 = max(x1, x0 + 1)
            y1 = max(y1, y0 + 1)
            self._add_rect(x0, y0, x1, y1)

        self._rebuild_tree()
        self.log(f"Applied preset '{name}' ({len(self.rect_items)} rects)", "SUCCESS")

    # ---------------------------------------------------------------
    # Auto-detect grid cells by alpha
    # ---------------------------------------------------------------

    def _auto_detect(self):
        if self.image_width == 0:
            self.log("Load an image first", "WARNING")
            return
        try:
            from PIL import Image
        except ImportError:
            self.log("Pillow (PIL) is required for auto-detect", "ERROR")
            return

        self._save_undo()

        img = Image.open(self.image_path).convert("RGBA")
        alpha = img.split()[3]
        g = self.grid_size_combo.currentData() or 16
        iw, ih = self.image_width, self.image_height
        thr = self.alpha_threshold_spin.value()
        added = 0

        for y in range(0, ih, g):
            for x in range(0, iw, g):
                x1 = min(x + g, iw)
                y1 = min(y + g, ih)
                region = alpha.crop((x, y, x1, y1))
                if thr <= 0:
                    bbox = region.getbbox()
                else:
                    mask = region.point(lambda p: 255 if p > thr else 0)
                    bbox = mask.getbbox()
                if bbox is not None:
                    self._add_rect(x, y, x1, y1)
                    added += 1

        self._rebuild_tree()
        self.log(f"Auto-detected {added} grid cells (grid={g}, threshold={thr})", "SUCCESS")

    # ---------------------------------------------------------------
    # Scale dialog
    # ---------------------------------------------------------------

    def _show_scale_dialog(self):
        if not self.rect_items:
            self.log("No rectangles to scale", "WARNING")
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Scale Rectangles")
        form = QFormLayout(dlg)

        sx_spin = QDoubleSpinBox()
        sx_spin.setRange(0.01, 10.0)
        sx_spin.setValue(2.0)
        sx_spin.setSingleStep(0.1)
        form.addRow("Scale X:", sx_spin)

        sy_spin = QDoubleSpinBox()
        sy_spin.setRange(0.01, 10.0)
        sy_spin.setValue(2.0)
        sy_spin.setSingleStep(0.1)
        form.addRow("Scale Y:", sy_spin)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)

        if dlg.exec() == QDialog.Accepted:
            self._save_undo()
            sx, sy = sx_spin.value(), sy_spin.value()
            for item in self.rect_items:
                r = item.rect()
                new_rect = QRectF(r.x() * sx, r.y() * sy, r.width() * sx, r.height() * sy)
                item.setRect(new_rect)
            self._rebuild_tree()
            self.log(f"Scaled all rectangles by ({sx:.2f}, {sy:.2f})", "SUCCESS")

    # ---------------------------------------------------------------
    # .rect file I/O
    # ---------------------------------------------------------------

    def _open_rect_file(self):
        start_dir = self.settings.get("hotspot_last_rect_dir", "") or ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open .rect File", start_dir,
            "Rect files (*.rect);;All files (*.*)",
        )
        if not path:
            return
        self.rect_input.setText(path)
        self.settings.set("hotspot_last_rect_dir", str(Path(path).parent))
        self.settings.save()
        self._load_rect_file(path)

    def _load_rect_file(self, path: str):
        try:
            rects = []
            current = None
            with open(path, "r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("//"):
                        continue
                    if line.startswith("rectangle"):
                        current = {"min": (0, 0), "max": (0, 0),
                                   "rotate": 0, "reflect": 0, "alt": 0}
                    elif line.startswith("{"):
                        continue
                    elif line.startswith("}"):
                        if current is not None:
                            rects.append(current)
                            current = None
                    elif current is not None:
                        parts = line.replace("\t", " ").split()
                        key = parts[0]
                        if key in ("min", "max"):
                            rest = line[line.find('"') + 1: line.rfind('"')]
                            xs = rest.split()
                            if len(xs) >= 2:
                                current[key] = (int(float(xs[0])), int(float(xs[1])))
                        elif key in ("rotate", "reflect", "alt"):
                            try:
                                current[key] = 1 if int(parts[-1]) != 0 else 0
                            except (ValueError, IndexError):
                                pass

            # Clear existing and recreate
            self._save_undo()
            for item in self.rect_items:
                self.scene.removeItem(item)
            self.rect_items.clear()
            self.selected_index = None

            for rd in rects:
                flags = {k: rd.get(k, 0) for k in ("rotate", "reflect", "alt")}
                self._add_rect(rd["min"][0], rd["min"][1], rd["max"][0], rd["max"][1], flags=flags)

            self._rebuild_tree()
            self.log(f"Loaded {len(rects)} rectangles from {os.path.basename(path)}", "SUCCESS")
        except Exception as e:
            self.log(f"Failed to read .rect file: {e}", "ERROR")

    def _save_rect_file(self):
        start_dir = self.settings.get("hotspot_last_rect_dir", "") or ""
        # Default filename from image name
        default_name = ""
        if self.image_path:
            default_name = Path(self.image_path).stem + ".rect"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save .rect File",
            os.path.join(start_dir, default_name) if default_name else start_dir,
            "Rect files (*.rect);;All files (*.*)",
        )
        if not path:
            return
        if not path.lower().endswith(".rect"):
            path += ".rect"

        self.rect_input.setText(path)
        self.settings.set("hotspot_last_rect_dir", str(Path(path).parent))
        self.settings.save()

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("Rectangles\n{\n")
                for item in self.rect_items:
                    mn = item.pixel_min()
                    mx = item.pixel_max()
                    f.write("\trectangle\n\t{\n")
                    f.write(f'\t\tmin\t\t"{mn[0]} {mn[1]}"\n')
                    f.write(f'\t\tmax\t\t"{mx[0]} {mx[1]}"\n')
                    if item.flags_data.get("rotate"):
                        f.write("\t\trotate\t1\n")
                    if item.flags_data.get("reflect"):
                        f.write("\t\treflect\t1\n")
                    if item.flags_data.get("alt"):
                        f.write("\t\talt\t1\n")
                    f.write("\t}\n")
                f.write("}\n")
            self.log(f"Saved {len(self.rect_items)} rectangles to {os.path.basename(path)}", "SUCCESS")
            self.emit_status(f"Saved {os.path.basename(path)}")
        except Exception as e:
            self.log(f"Failed to save .rect file: {e}", "ERROR")

    # ---------------------------------------------------------------
    # VMT snippet
    # ---------------------------------------------------------------

    def _update_vmt_snippet(self):
        if not self.image_path:
            self.vmt_label.setText('%rectanglemap "<materials/path/to/texture>"')
            return
        stem = os.path.splitext(self.image_path)[0]
        lower = stem.replace("\\", "/").lower()
        idx = lower.rfind("/materials/")
        if idx != -1:
            rel = stem[idx + len("/materials/"):].replace("\\", "/")
        else:
            rel = os.path.basename(stem)
        self.vmt_label.setText(f'%rectanglemap "{rel}"')
