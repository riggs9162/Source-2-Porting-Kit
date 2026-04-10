"""
Hotspot Editor Tool - Create and edit .rect hotspot files for Hammer/Strata.

Features:
- Load a texture image and draw axis-aligned rectangles.
- Edit flags per-rectangle (rotate, reflect, alt).
- Open existing .rect files, visualize, and modify.
- Save .rect files in the documented text format.
"""

import os
import random
from math import floor, ceil
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk

from .base_tool import BaseTool, register_tool
from .utils import PlaceholderEntry, browse_file_with_context, save_file_with_context


@register_tool
class HotspotEditorTool(BaseTool):
    @property
    def name(self) -> str:
        return "Hotspot Editor"

    @property
    def description(self) -> str:
        return "Create and edit .rect hotspot files for Hammer/Strata"

    @property
    def dependencies(self) -> list:
        return ["PIL"]

    def create_tab(self, parent) -> ttk.Frame:
        return HotspotEditorTab(parent, self.config)


class HotspotEditorTab(ttk.Frame):
    def __init__(self, parent, config):
        super().__init__(parent)
        self.config = config

        # State
        self.image_path = ""
        self.image = None  # PIL image
        self.photo = None  # ImageTk for canvas
        self.canvas = None

        # Transform state
        self.base_scale = 1.0  # base scale to fit preview
        self.zoom = 1.0        # user zoom factor
        self.scale_x = 1.0
        self.scale_y = 1.0
        self.offset_x = 0      # pan X in canvas pixels
        self.offset_y = 0      # pan Y in canvas pixels
        self.max_preview = (1024, 768)
        self.image_item = None  # canvas image item id

        # Rectangles: list of dicts with keys: min, max, rotate, reflect, alt, canvas_id
        self.rects = []
        self.selected_index = None
        self.drag_start = None
        self.temp_rect_id = None
        self.dragging_handle = None  # (rect_index, handle_key)
        self.pan_start = None

        # Undo/Redo stacks
        self.undo_stack = []
        self.redo_stack = []

        # Grid/Snap settings
        self.snap_enabled = tk.BooleanVar(value=True)
        self.grid_sizes = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]
        self.grid_size = tk.IntVar(value=16)
        self.show_grid = tk.BooleanVar(value=True)
        self.alpha_threshold = tk.IntVar(value=1)  # for auto-detect (0-255)
        self.grid_items = []  # canvas ids for grid lines

        self._build_ui()
        self._bind_shortcuts()

    def _bind_shortcuts(self):
        self.canvas.bind_all("<Control-z>", self._undo)
        self.canvas.bind_all("<Control-y>", self._redo)

    def _save_state(self):
        # Save current state for undo
        self.undo_stack.append([rect.copy() for rect in self.rects])
        self.redo_stack.clear()  # Clear redo stack on new action

    def _undo(self, event=None):
        if not self.undo_stack:
            return
        self.redo_stack.append([rect.copy() for rect in self.rects])
        self.rects = self.undo_stack.pop()
        self._rebuild_tree()
        self._refresh_canvas_rects()

    def _redo(self, event=None):
        if not self.redo_stack:
            return
        self.undo_stack.append([rect.copy() for rect in self.rects])
        self.rects = self.redo_stack.pop()
        self._rebuild_tree()
        self._refresh_canvas_rects()

    # ----- UI -----
    def _build_ui(self):
        main = ttk.Frame(self)
        main.pack(fill="both", expand=True, padx=10, pady=10)

        # Top controls: image + rect file paths
        paths = ttk.LabelFrame(main, text="Paths", padding=10)
        paths.pack(fill="x")

        # Image path
        row = 0
        ttk.Label(paths, text="Image:").grid(row=row, column=0, sticky="e")
        self.image_entry = PlaceholderEntry(paths, placeholder="Select texture image (png/jpg/tga/vtf if converted)")
        self.image_entry.grid(row=row, column=1, sticky="ew", padx=5)
        ttk.Button(paths, text="Browse...", command=self._browse_image).grid(row=row, column=2)
        ttk.Button(paths, text="Load", command=self._load_image_from_entry).grid(row=row, column=3, padx=(5, 0))

        row += 1
        ttk.Label(paths, text=".rect:").grid(row=row, column=0, sticky="e", pady=(6, 0))
        self.rect_entry = PlaceholderEntry(paths, placeholder="Select or type .rect file path")
        self.rect_entry.grid(row=row, column=1, sticky="ew", padx=5, pady=(6, 0))
        ttk.Button(paths, text="Open...", command=self._open_rect).grid(row=row, column=2, pady=(6, 0))
        ttk.Button(paths, text="Save As...", command=self._save_rect).grid(row=row, column=3, padx=(5, 0), pady=(6, 0))

        paths.columnconfigure(1, weight=1)

        # Middle: canvas and sidebar
        middle = ttk.Frame(main)
        middle.pack(fill="both", expand=True, pady=(10, 0))

        # Canvas area
        canvas_frame = ttk.LabelFrame(middle, text="Preview", padding=6)
        canvas_frame.pack(side="left", fill="both", expand=True)

        self.canvas = tk.Canvas(canvas_frame, bg="#1e1e1e", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._on_canvas_press)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        self.canvas.bind("<Button-2>", self._on_pan_start)
        self.canvas.bind("<B2-Motion>", self._on_pan_drag)
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)

        # Sidebar: rectangles list and flags
        side = ttk.LabelFrame(middle, text="Rectangles", padding=10)
        side.pack(side="right", fill="y")

        self.tree = ttk.Treeview(side, columns=("min", "max", "flags"), show="headings", height=14)
        self.tree.heading("min", text="min (x y)")
        self.tree.heading("max", text="max (x y)")
        self.tree.heading("flags", text="flags")
        self.tree.column("min", width=110)
        self.tree.column("max", width=110)
        self.tree.column("flags", width=120)
        self.tree.pack(fill="x")
        self.tree.bind("<<TreeviewSelect>>", self._on_select_rect)

        btns = ttk.Frame(side)
        btns.pack(fill="x", pady=6)
        ttk.Button(btns, text="Delete", command=self._delete_selected).pack(side="left")
        ttk.Button(btns, text="Clear All", command=self._clear_rects).pack(side="left", padx=(6, 0))

        # Snap/Grid controls
        grid_box = ttk.LabelFrame(side, text="Grid & Snap", padding=6)
        grid_box.pack(fill="x", pady=(6, 0))
        ttk.Checkbutton(grid_box, text="Snap to grid", variable=self.snap_enabled).pack(anchor="w")
        ttk.Checkbutton(grid_box, text="Show grid", variable=self.show_grid, command=self._redraw_all).pack(anchor="w")
        size_row = ttk.Frame(grid_box)
        size_row.pack(fill="x", pady=(4, 0))
        ttk.Label(size_row, text="Grid size").pack(side="left")
        self.grid_combo = ttk.Combobox(size_row, width=6, values=self.grid_sizes, textvariable=self.grid_size, state="readonly")
        self.grid_combo.pack(side="left", padx=(6, 0))
        self.grid_combo.bind("<<ComboboxSelected>>", lambda e: self._redraw_all())

        # Auto-detect based on grid
        detect_row = ttk.LabelFrame(side, text="Auto-detect (grid)", padding=6)
        detect_row.pack(fill="x", pady=(6, 0))
        thr_row = ttk.Frame(detect_row)
        thr_row.pack(fill="x")
        ttk.Label(thr_row, text="Alpha >").pack(side="left")
        self.thr_entry = ttk.Spinbox(thr_row, from_=0, to=255, textvariable=self.alpha_threshold, width=5)
        self.thr_entry.pack(side="left", padx=(4, 0))
        ttk.Button(detect_row, text="Auto-detect", command=self._auto_detect_grid_cells).pack(fill="x", pady=(4, 0))

        flags = ttk.LabelFrame(side, text="Flags")
        flags.pack(fill="x", pady=(6, 0))
        self.var_rotate = tk.BooleanVar(value=False)
        self.var_reflect = tk.BooleanVar(value=False)
        self.var_alt = tk.BooleanVar(value=False)
        ttk.Checkbutton(flags, text="rotate", variable=self.var_rotate, command=self._apply_flags).pack(anchor="w")
        ttk.Checkbutton(flags, text="reflect", variable=self.var_reflect, command=self._apply_flags).pack(anchor="w")
        ttk.Checkbutton(flags, text="alt", variable=self.var_alt, command=self._apply_flags).pack(anchor="w")

        # Bottom: help/snippet
        bottom = ttk.LabelFrame(main, text="VMT snippet", padding=10)
        bottom.pack(fill="x", pady=(10, 0))
        self.vmt_label = ttk.Label(bottom, text="%rectanglemap \"<materials/path/to/texture>\"")
        self.vmt_label.pack(anchor="w")

        # Status
        self.status = ttk.Label(main, text="Load an image to begin", foreground="green")
        self.status.pack(anchor="w", pady=(8, 0))

        # Add Scale Rectangles button to the sidebar
        scale_btn = ttk.Button(side, text="Scale Rectangles", command=self._show_scale_dialog)
        scale_btn.pack(fill="x", pady=(6, 0))

    # ----- Image handling -----
    def _browse_image(self):
        path = browse_file_with_context(self.image_entry, context_key="hotspot_image",
                                        filetypes=[("Images", "*.png *.jpg *.jpeg *.tga *.bmp"), ("All files", "*.*")],
                                        title="Select Texture Image")
        if path:
            self._load_image(path)

    def _load_image_from_entry(self):
        p = self.image_entry.get()
        if not p:
            return
        self._load_image(p)

    def _load_image(self, path):
        try:
            img = Image.open(path)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open image: {e}")
            return

        self.image_path = path
        self.image = img.convert("RGBA")
        self._update_canvas_image()
        self._update_vmt_snippet()
        self.status.config(text=f"Loaded image: {os.path.basename(path)}")

    def _compute_base_scale(self):
        if not self.image:
            return 1.0
        iw, ih = self.image.size
        maxw, maxh = self.max_preview
        return min(maxw / iw, maxh / ih, 1.0)

    def _render_scaled_image(self):
        if not self.image:
            return
        iw, ih = self.image.size
        self.scale_x = self.scale_y = self.base_scale * self.zoom
        dw, dh = max(1, int(iw * self.scale_x)), max(1, int(ih * self.scale_y))
        disp = self.image.resize((dw, dh), Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(disp)
        if self.image_item is None:
            self.image_item = self.canvas.create_image(self.offset_x, self.offset_y, anchor=tk.NW, image=self.photo)
        else:
            self.canvas.itemconfigure(self.image_item, image=self.photo)
            self.canvas.coords(self.image_item, self.offset_x, self.offset_y)

    def _redraw_all(self):
        if not self.image:
            return
        # Clear everything and redraw image, grid, rects
        self.canvas.delete("all")
        self.image_item = None
        self._render_scaled_image()
        self._draw_grid()
        for i, rect in enumerate(self.rects):
            self._draw_rect_on_canvas(rect, selected=(i == self.selected_index))

    def _image_to_canvas(self, x, y):
        return x * self.scale_x + self.offset_x, y * self.scale_y + self.offset_y

    def _canvas_to_image(self, x, y):
        if self.scale_x == 0 or self.scale_y == 0:
            return x, y
        return int(round((x - self.offset_x) / self.scale_x)), int(round((y - self.offset_y) / self.scale_y))

    def _snap_val(self, v, mode="nearest"):
        g = max(1, int(self.grid_size.get()))
        if mode == "down":
            return (v // g) * g
        if mode == "up":
            return ((v + g - 1) // g) * g
        # nearest
        return int(round(v / g)) * g

    def _apply_snap_rect(self, x0, y0, x1, y1):
        if not self.snap_enabled.get():
            return x0, y0, x1, y1
        sx0 = self._snap_val(min(x0, x1), mode="down")
        sy0 = self._snap_val(min(y0, y1), mode="down")
        sx1 = self._snap_val(max(x0, x1), mode="up")
        sy1 = self._snap_val(max(y0, y1), mode="up")
        return sx0, sy0, sx1, sy1

    def _draw_grid(self):
        # Draw grid overlay if enabled
        for gid in self.grid_items:
            try:
                self.canvas.delete(gid)
            except Exception:
                pass
        self.grid_items.clear()
        if not (self.image and self.show_grid.get()):
            return
        g = max(1, int(self.grid_size.get()))
        iw, ih = self.image.size
        # Compute visible bounds in image coords
        # For simplicity draw full image grid
        color_minor = "#2a2a2a"
        color_major = "#3d3d3d"
        major_every = 8  # thicker every 8 cells
        for x in range(0, iw + 1, g):
            cx0, cy0 = self._image_to_canvas(x, 0)
            cx1, cy1 = self._image_to_canvas(x, ih)
            col = color_major if (x // g) % major_every == 0 else color_minor
            self.grid_items.append(self.canvas.create_line(cx0, cy0, cx1, cy1, fill=col))
        for y in range(0, ih + 1, g):
            cx0, cy0 = self._image_to_canvas(0, y)
            cx1, cy1 = self._image_to_canvas(iw, y)
            col = color_major if (y // g) % major_every == 0 else color_minor
            self.grid_items.append(self.canvas.create_line(cx0, cy0, cx1, cy1, fill=col))

    # ----- Rect handling -----
    def _on_canvas_press(self, event):
        if not self.image:
            return
        # Check if user clicked on a resize handle first
        handle_hit = self._hit_test_handle(event.x, event.y)
        if handle_hit is not None:
            self.dragging_handle = handle_hit  # (rect_index, handle_key)
            return
        # Otherwise begin drawing a new rectangle
        self.drag_start = (event.x, event.y)
        if self.temp_rect_id:
            self.canvas.delete(self.temp_rect_id)
            self.temp_rect_id = None
        self.temp_rect_id = self.canvas.create_rectangle(event.x, event.y, event.x, event.y,
                                                         outline="#00ffff", width=2, dash=(3, 2))

    def _on_canvas_drag(self, event):
        if not self.image:
            return
        # If resizing via handle
        if self.dragging_handle is not None:
            ridx, hkey = self.dragging_handle
            r = self.rects[ridx]
            # Opposite corner stays fixed
            (ix0, iy0), (ix1, iy1) = r['min'], r['max']
            # Determine which corner is being dragged
            if hkey == 'nw':
                nx, ny = self._canvas_to_image(event.x, event.y)
                nx, ny, _, _ = self._apply_snap_rect(nx, ny, ix1, iy1)
                r['min'] = (max(0, min(nx, ix1-1)), max(0, min(ny, iy1-1)))
            elif hkey == 'ne':
                nx, ny = self._canvas_to_image(event.x, event.y)
                _, ny, nx, _ = self._apply_snap_rect(ix0, ny, nx, iy1)
                r['min'] = (ix0, max(0, min(ny, iy1-1)))
                r['max'] = (max(nx, ix0+1), r['max'][1])
            elif hkey == 'sw':
                nx, ny = self._canvas_to_image(event.x, event.y)
                nx, _, _, ny = self._apply_snap_rect(nx, iy0, ix1, ny)
                r['min'] = (max(0, min(nx, ix1-1)), iy0)
                r['max'] = (r['max'][0], max(ny, iy0+1))
            elif hkey == 'se':
                nx, ny = self._canvas_to_image(event.x, event.y)
                _, _, nx, ny = self._apply_snap_rect(ix0, iy0, nx, ny)
                r['max'] = (max(nx, ix0+1), max(ny, iy0+1))
            self._update_tree_item(ridx)
            self._draw_rect_on_canvas(r, selected=(self.selected_index == ridx))
            return
        # Drawing new temp rect
        if not self.drag_start or not self.temp_rect_id:
            return
        x0, y0 = self.drag_start
        x1, y1 = event.x, event.y
        self.canvas.coords(self.temp_rect_id, x0, y0, x1, y1)

    def _on_canvas_release(self, event):
        # End any handle drag
        if self.dragging_handle is not None:
            self.dragging_handle = None
            return
        if not self.image or not self.drag_start:
            return
        x0, y0 = self.drag_start
        x1, y1 = event.x, event.y
        self.drag_start = None

        # Normalize and convert to image coords
        x_min, x_max = sorted([x0, x1])
        y_min, y_max = sorted([y0, y1])
        ix0, iy0 = self._canvas_to_image(x_min, y_min)
        ix1, iy1 = self._canvas_to_image(x_max, y_max)
        ix0, iy0, ix1, iy1 = self._apply_snap_rect(ix0, iy0, ix1, iy1)

        # Ignore too small rectangles
        if abs(ix1 - ix0) < 1 or abs(iy1 - iy0) < 1:
            if self.temp_rect_id:
                self.canvas.delete(self.temp_rect_id)
                self.temp_rect_id = None
            return

        self._save_state()  # Save state before adding a new rectangle

        color, glow = self._random_neon_color()
        rect = {
            'min': (max(0, min(ix0, ix1)), max(0, min(iy0, iy1))),
            'max': (max(ix0, ix1), max(iy0, iy1)),
            'rotate': 0, 'reflect': 0, 'alt': 0,
            'canvas_ids': {},
            'color': color,
            'glow': glow
        }
        self.rects.append(rect)
        self._add_tree_item(len(self.rects) - 1)
        self._draw_rect_on_canvas(rect)

        if self.temp_rect_id:
            self.canvas.delete(self.temp_rect_id)
            self.temp_rect_id = None
        self.status.config(text=f"Added rectangle #{len(self.rects)}")

    def _draw_rect_on_canvas(self, rect, selected=False):
        (ix0, iy0), (ix1, iy1) = rect['min'], rect['max']
        x0, y0 = self._image_to_canvas(ix0, iy0)
        x1, y1 = self._image_to_canvas(ix1, iy1)
        # Delete existing items
        ids = rect.get('canvas_ids') or {}
        for key in list(ids.keys()):
            try:
                if isinstance(ids[key], list):
                    for hid in ids[key]:
                        self.canvas.delete(hid)
                else:
                    self.canvas.delete(ids[key])
            except Exception:
                pass
        rect['canvas_ids'] = {}
        # Draw glow outer stroke and inner neon outline
        main_col = rect.get('color', '#00ffaa')
        glow_col = rect.get('glow', '#006655')
        glow_w = 6 if selected else 5
        main_w = 3 if selected else 2
        glow_id = self.canvas.create_rectangle(x0, y0, x1, y1, outline=glow_col, width=glow_w)
        main_id = self.canvas.create_rectangle(x0, y0, x1, y1, outline=main_col, width=main_w, dash=(6, 3))
        rect['canvas_ids']['glow'] = glow_id
        rect['canvas_ids']['main'] = main_id
        # Draw resize handles when selected
        if selected:
            handles = []
            for hkey, (hx, hy) in {
                'nw': (x0, y0), 'ne': (x1, y0), 'sw': (x0, y1), 'se': (x1, y1)
            }.items():
                sz = 6
                hid = self.canvas.create_rectangle(hx - sz, hy - sz, hx + sz, hy + sz,
                                                   fill=main_col, outline=glow_col, width=2)
                self.canvas.addtag_withtag('handle', hid)
                # store mapping for hit-testing
                handles.append(hid)
            rect['canvas_ids']['handles'] = handles

    def _refresh_canvas_rects(self):
        if not self.image:
            return
        self._draw_grid()
        for i, r in enumerate(self.rects):
            self._draw_rect_on_canvas(r, selected=(i == self.selected_index))

    def _hit_test_handle(self, x, y):
        # Return (rect_index, handle_key) if a handle is under (x, y)
        items = self.canvas.find_overlapping(x, y, x, y)
        if not items:
            return None
        for idx, r in enumerate(self.rects):
            ids = r.get('canvas_ids', {})
            if 'handles' in ids:
                for hid in ids['handles']:
                    if hid in items:
                        # Decide which handle by proximity to corners
                        (ix0, iy0), (ix1, iy1) = r['min'], r['max']
                        corners = {
                            'nw': self._image_to_canvas(ix0, iy0),
                            'ne': self._image_to_canvas(ix1, iy0),
                            'sw': self._image_to_canvas(ix0, iy1),
                            'se': self._image_to_canvas(ix1, iy1),
                        }
                        # pick closest corner
                        best_key = min(corners.keys(), key=lambda k: (corners[k][0]-x)**2 + (corners[k][1]-y)**2)
                        self.selected_index = idx
                        self.tree.selection_set(str(idx))
                        return (idx, best_key)
        return None

    def _on_pan_start(self, event):
        if not self.image:
            return
        self.pan_start = (event.x, event.y)

    def _on_pan_drag(self, event):
        if not self.image or not self.pan_start:
            return
        dx = event.x - self.pan_start[0]
        dy = event.y - self.pan_start[1]
        self.pan_start = (event.x, event.y)
        self.offset_x += dx
        self.offset_y += dy
        self._redraw_all()

    def _on_mouse_wheel(self, event):
        if not self.image:
            return
        # Zoom around mouse cursor
        old_zoom = self.zoom
        factor = 1.1 if event.delta > 0 else 1/1.1
        new_zoom = max(0.1, min(8.0, old_zoom * factor))
        if abs(new_zoom - old_zoom) < 1e-3:
            return
        # anchor image point under cursor
        ix, iy = self._canvas_to_image(event.x, event.y)
        # update zoom
        self.zoom = new_zoom
        # Update scales and image
        old_scale = self.scale_x
        self._render_scaled_image()
        # compute new canvas position of the image point and adjust offset to keep it under cursor
        nx, ny = self._image_to_canvas(ix, iy)
        self.offset_x += (event.x - nx)
        self.offset_y += (event.y - ny)
        self._redraw_all()

    # ----- Tree/selection -----
    def _add_tree_item(self, idx):
        rect = self.rects[idx]
        flags = self._flags_to_str(rect)
        self.tree.insert("", "end", iid=str(idx), values=(f"{rect['min'][0]} {rect['min'][1]}",
                                                            f"{rect['max'][0]} {rect['max'][1]}",
                                                            flags))

    def _update_tree_item(self, idx):
        rect = self.rects[idx]
        flags = self._flags_to_str(rect)
        self.tree.item(str(idx), values=(f"{rect['min'][0]} {rect['min'][1]}",
                                         f"{rect['max'][0]} {rect['max'][1]}",
                                         flags))

    def _rebuild_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for i in range(len(self.rects)):
            self._add_tree_item(i)

    def _on_select_rect(self, event=None):
        sel = self.tree.selection()
        if not sel:
            self.selected_index = None
            self.var_rotate.set(False)
            self.var_reflect.set(False)
            self.var_alt.set(False)
            self._refresh_canvas_rects()
            return
        idx = int(sel[0])
        self.selected_index = idx
        r = self.rects[idx]
        self.var_rotate.set(bool(r['rotate']))
        self.var_reflect.set(bool(r['reflect']))
        self.var_alt.set(bool(r['alt']))
        self._refresh_canvas_rects()

    def _apply_flags(self):
        if self.selected_index is None:
            return
        r = self.rects[self.selected_index]
        r['rotate'] = 1 if self.var_rotate.get() else 0
        r['reflect'] = 1 if self.var_reflect.get() else 0
        r['alt'] = 1 if self.var_alt.get() else 0
        self._update_tree_item(self.selected_index)
        self._refresh_canvas_rects()

    def _delete_selected(self):
        if self.selected_index is None:
            return
        self._save_state()  # Save state before deleting
        # Remove canvas item
        r = self.rects[self.selected_index]
        ids = r.get('canvas_ids', {})
        for key in list(ids.keys()):
            try:
                if isinstance(ids[key], list):
                    for hid in ids[key]:
                        self.canvas.delete(hid)
                else:
                    self.canvas.delete(ids[key])
            except Exception:
                pass
        del self.rects[self.selected_index]
        # Re-index tree iids
        self._rebuild_tree()
        self.selected_index = None
        self._refresh_canvas_rects()

    def _clear_rects(self):
        self._save_state()  # Save state before clearing
        self.rects.clear()
        self._rebuild_tree()
        self.canvas.delete("all")
        self.image_item = None
        self._render_scaled_image()
        self._draw_grid()

    # ----- .rect IO -----
    def _open_rect(self):
        path = browse_file_with_context(self.rect_entry, context_key="hotspot_rect",
                                        filetypes=[("Rect files", "*.rect"), ("All files", "*.*")],
                                        title="Open .rect file")
        if not path:
            return
        self._load_rect_file(path)

    def _load_rect_file(self, path):
        try:
            rects = []
            current = None
            with open(path, 'r', encoding='utf-8') as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith('//'):
                        continue
                    if line.startswith('rectangle'):
                        color, glow = self._random_neon_color()
                        current = {'min': (0, 0), 'max': (0, 0), 'rotate': 0, 'reflect': 0, 'alt': 0,
                                   'canvas_ids': {}, 'color': color, 'glow': glow}
                    elif line.startswith('{'):
                        continue
                    elif line.startswith('}'):
                        if current is not None:
                            rects.append(current)
                            current = None
                    elif current is not None:
                        # parse key value pairs, e.g., min "x y" / rotate 1
                        parts = line.replace('\t', ' ').split()
                        key = parts[0]
                        if key in ('min', 'max'):
                            # Expect: min "x y"
                            rest = line[line.find('"') + 1: line.rfind('"')]
                            xs = rest.split()
                            if len(xs) >= 2:
                                val = (int(float(xs[0])), int(float(xs[1])))
                                current[key] = val
                        else:
                            try:
                                current[key] = 1 if int(parts[-1]) != 0 else 0
                            except Exception:
                                pass
            self.rects = rects
            self._rebuild_tree()
            self._redraw_all()
            self.status.config(text=f"Loaded {len(self.rects)} rectangles from {os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to read .rect file: {e}")

    def _save_rect(self):
        path = save_file_with_context(context_key="hotspot_rect", title="Save .rect file",
                                      defaultextension=".rect",
                                      filetypes=[("Rect files", "*.rect"), ("All files", "*.*")])
        if not path:
            return
        try:
            # Generate a .rect file if none is loaded
            if not self.rects:
                self.rects.append({
                    'min': (0, 0), 'max': (10, 10),
                    'rotate': 0, 'reflect': 0, 'alt': 0,
                    'canvas_ids': {}, 'color': "#00ffaa", 'glow': "#006655"
                })

            with open(path, 'w', encoding='utf-8') as f:
                f.write("Rectangles\n{")
                f.write("\n")
                for r in self.rects:
                    f.write("\trectangle\n\t{\n")
                    f.write(f"\t\tmin\t\t\"{r['min'][0]} {r['min'][1]}\"\n")
                    f.write(f"\t\tmax\t\t\"{r['max'][0]} {r['max'][1]}\"\n")
                    if r.get('rotate', 0):
                        f.write("\t\trotate\t1\n")
                    if r.get('reflect', 0):
                        f.write("\t\treflect\t1\n")
                    if r.get('alt', 0):
                        f.write("\t\talt\t1\n")
                    f.write("\t}\n")
                f.write("}\n")

            # Update the rect path to use the saved path
            self.rect_entry.set_text(path)
            self.status.config(text=f"Saved .rect with {len(self.rects)} rectangles")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save .rect: {e}")

    def _scale_rects(self, scale_x, scale_y):
        """Scale all rectangles by the given x and y factors."""
        if not self.rects:
            messagebox.showwarning("Scale Rectangles", "No rectangles to scale.")
            return

        self._save_state()  # Save state for undo

        for rect in self.rects:
            rect['min'] = (int(rect['min'][0] * scale_x), int(rect['min'][1] * scale_y))
            rect['max'] = (int(rect['max'][0] * scale_x), int(rect['max'][1] * scale_y))

        self._rebuild_tree()
        self._refresh_canvas_rects()
        self.status.config(text=f"Scaled all rectangles by ({scale_x}, {scale_y})")

    def _show_scale_dialog(self):
        """Show a dialog with sliders to scale rectangles."""
        dialog = tk.Toplevel(self)
        dialog.title("Scale Rectangles")

        ttk.Label(dialog, text="Scale X:").grid(row=0, column=0, padx=5, pady=5, sticky="e")
        scale_x_slider = ttk.Scale(dialog, from_=0.1, to=5.0, orient="horizontal")
        scale_x_slider.grid(row=0, column=1, padx=5, pady=5)
        scale_x_slider.set(2.0)

        ttk.Label(dialog, text="Scale Y:").grid(row=1, column=0, padx=5, pady=5, sticky="e")
        scale_y_slider = ttk.Scale(dialog, from_=0.1, to=5.0, orient="horizontal")
        scale_y_slider.grid(row=1, column=1, padx=5, pady=5)
        scale_y_slider.set(2.0)

        def apply_scaling():
            scale_x = scale_x_slider.get()
            scale_y = scale_y_slider.get()
            self._scale_rects(scale_x, scale_y)
            dialog.destroy()

        ttk.Button(dialog, text="Apply", command=apply_scaling).grid(row=2, column=0, columnspan=2, pady=10)
        dialog.transient(self)
        dialog.grab_set()
        self.wait_window(dialog)

    # ----- Helpers -----
    def _flags_to_str(self, r):
        flags = []
        if r.get('rotate', 0):
            flags.append('rotate')
        if r.get('reflect', 0):
            flags.append('reflect')
        if r.get('alt', 0):
            flags.append('alt')
        return ','.join(flags) if flags else '-'

    def _update_vmt_snippet(self):
        # Try to propose a materials path from the image path
        if not self.image_path:
            self.vmt_label.config(text="%rectanglemap \"<materials/path/to/texture>\"")
            return
        stem = os.path.splitext(self.image_path)[0]
        # If the path contains a materials folder, trim up to that
        lower = stem.replace('\\', '/').lower()
        idx = lower.rfind('/materials/')
        if idx != -1:
            rel = stem[idx + len('/materials/'):].replace('\\', '/')
        else:
            rel = os.path.basename(stem)
        self.vmt_label.config(text=f"%rectanglemap \"{rel}\"")

    # ----- Image loading overrides -----
    def _load_image(self, path):
        try:
            img = Image.open(path)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to open image: {e}")
            return

        self.image_path = path
        self.image = img.convert("RGBA")
        self.base_scale = self._compute_base_scale()
        self.zoom = 1.0
        self.offset_x = 0
        self.offset_y = 0
        self._redraw_all()
        self._update_vmt_snippet()
        self.status.config(text=f"Loaded image: {os.path.basename(path)}")

    # ----- Colors & detection -----
    def _random_neon_color(self):
        # Pick from a neon palette
        palette = [
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
        r, g, b = random.choice(palette)
        color = f"#{r:02x}{g:02x}{b:02x}"
        glow = f"#{max(0, r//3):02x}{max(0, g//3):02x}{max(0, b//3):02x}"
        return color, glow

    def _auto_detect_grid_cells(self):
        if not self.image:
            messagebox.showwarning("Auto-detect", "Load an image first.")
            return
        g = max(1, int(self.grid_size.get()))
        iw, ih = self.image.size
        alpha = self.image.split()[3]
        thr = int(self.alpha_threshold.get())
        added = 0
        # Clear previous temp selection
        for y in range(0, ih, g):
            for x in range(0, iw, g):
                x1 = min(x + g, iw)
                y1 = min(y + g, ih)
                region = alpha.crop((x, y, x1, y1))
                # Check if any pixel > thr
                # Using getbbox on a thresholded mask for speed
                if thr <= 0:
                    bbox = region.getbbox()
                else:
                    # Create a binary mask by point() mapping
                    mask = region.point(lambda p: 255 if p > thr else 0)
                    bbox = mask.getbbox()
                if bbox is not None:
                    color, glow = self._random_neon_color()
                    rect = {
                        'min': (x, y), 'max': (x1, y1),
                        'rotate': 0, 'reflect': 0, 'alt': 0,
                        'canvas_ids': {}, 'color': color, 'glow': glow
                    }
                    self.rects.append(rect)
                    added += 1
        self._rebuild_tree()
        self._refresh_canvas_rects()
        self.status.config(text=f"Auto-detected {added} grid cells")
