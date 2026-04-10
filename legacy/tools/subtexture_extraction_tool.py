"""
Subtexture Extraction Tool - Extract sub-regions from textures based on defined regions.
"""

import os
import json
import re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from PIL import Image, ImageTk, ImageDraw
from .base_tool import BaseTool, register_tool
from .utils import PlaceholderEntry, browse_file, browse_file_with_context, browse_folder_with_context, save_file_with_context

# Try to import VTFLib for VTF file support
try:
    import VTFLibWrapper.VTFLib as VTFLib
    import VTFLibWrapper.VTFLibEnums as VTFLibEnums
    VTFLIB_AVAILABLE = True
except ImportError:
    VTFLIB_AVAILABLE = False

class Region:
    """Represents a rectangular region for extraction."""
    def __init__(self, name, x, y, w, h):
        self.name = name
        self.x = x
        self.y = y
        self.w = w
        self.h = h

    def to_dict(self):
        return {"name": self.name, "x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @classmethod
    def from_dict(cls, d):
        return cls(d["name"], d["x"], d["y"], d["w"], d["h"])

    def __str__(self):
        return f"{self.name}: ({self.x}, {self.y}, {self.w}, {self.h})"

def load_vtf_as_pil_image(vtf_path):
    """Load a VTF file and convert it to a PIL Image."""
    if not VTFLIB_AVAILABLE:
        raise ImportError("VTFLib is not available. Cannot load VTF files.")

    if not os.path.exists(vtf_path):
        raise FileNotFoundError(f"VTF file not found: {vtf_path}")

    vtf_lib = None
    try:
        # Initialize VTFLib
        vtf_lib = VTFLib.VTFLib()

        # Load the VTF file
        if not vtf_lib.image_load(vtf_path):
            raise RuntimeError(f"Failed to load VTF file: {vtf_path}")

        # Get image properties
        width = vtf_lib.image_get_width()
        height = vtf_lib.image_get_height()

        print(f"VTF loaded: {width}x{height}")  # Debug

        # Try to convert to RGBA format first
        try:
            if not vtf_lib.image_convert(VTFLibEnums.ImageFormat.ImageFormatRGBA8888):
                raise RuntimeError("Failed to convert VTF to RGBA format")

            # Get the image data
            image_data = vtf_lib.image_get_data(0, 0, 0, 0)  # frame, face, slice, mip

            if not image_data:
                raise RuntimeError("Failed to get RGBA image data from VTF")

            # Create PIL Image from the data
            pil_image = Image.frombytes('RGBA', (width, height), image_data)

        except Exception as rgba_error:
            print(f"RGBA conversion failed: {rgba_error}, trying RGB...")

            # Fallback to RGB format
            try:
                if not vtf_lib.image_convert(VTFLibEnums.ImageFormat.ImageFormatRGB888):
                    raise RuntimeError("Failed to convert VTF to RGB format")

                image_data = vtf_lib.image_get_data(0, 0, 0, 0)
                if not image_data:
                    raise RuntimeError("Failed to get RGB image data from VTF")

                # Create PIL Image from RGB data and convert to RGBA
                pil_image = Image.frombytes('RGB', (width, height), image_data)
                pil_image = pil_image.convert('RGBA')

            except Exception as rgb_error:
                raise RuntimeError(f"Failed to convert VTF to usable format. RGBA error: {rgba_error}, RGB error: {rgb_error}")

        return pil_image

    except Exception as e:
        raise RuntimeError(f"Error loading VTF file {vtf_path}: {str(e)}")
    finally:
        # Clean up VTFLib resources
        if vtf_lib:
            try:
                vtf_lib.image_destroy()
            except:
                pass

@register_tool
class SubtextureExtractionTool(BaseTool):
    @property
    def name(self) -> str:
        return "Subtexture Extraction"

    @property
    def description(self) -> str:
        return "Extract sub-regions from textures based on defined rectangular areas"

    @property
    def dependencies(self) -> list:
        deps = ["PIL"]
        # VTFLibWrapper is optional but recommended for VTF file support
        if not VTFLIB_AVAILABLE:
            deps.append("VTFLibWrapper.VTFLib")
        return deps

    def create_tab(self, parent) -> ttk.Frame:
        return SubtextureExtractionTab(parent, self.config)

class SubtextureExtractionTab(ttk.Frame):
    def __init__(self, parent, config):
        super().__init__(parent)
        self.config = config

        # Initialize variables
        self.source_image = None
        self.preview_image = None
        self.regions = []
        self.selected_region = None
        self.canvas_scale = 1.0

        # Grid and snap settings
        self.snap_to_grid = tk.BooleanVar(value=True)
        self.grid_size = tk.IntVar(value=16)
        self.grid_sizes = [4, 8, 16, 32, 64, 128, 256, 512, 1024]
        self.show_grid = tk.BooleanVar(value=True)

        # Quality of life settings
        self.auto_name_regions = tk.BooleanVar(value=True)
        self.preserve_aspect_ratio = tk.BooleanVar(value=False)
        self.show_coordinates = tk.BooleanVar(value=True)
        self.show_dimensions = tk.BooleanVar(value=True)
        self.highlight_selected = tk.BooleanVar(value=True)

        # VMT processing
        self.vmt_file_path = None
        self.related_textures = []

        self.setup_ui()

    def setup_ui(self):
        """Set up the user interface."""
        main_frame = ttk.Frame(self)
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)

        # Input section
        input_frame = ttk.LabelFrame(main_frame, text="Source Image", padding=10)
        input_frame.pack(fill="x", pady=(0, 10))

        # Source image selection
        source_row = 0
        ttk.Label(input_frame, text="Source Image:").grid(row=source_row, column=0, sticky="w", pady=2)
        self.image_path = PlaceholderEntry(input_frame, placeholder="Select source image file...")
        self.image_path.grid(row=source_row, column=1, sticky="ew", padx=(5, 0), pady=2)
        ttk.Button(input_frame, text="Browse",
                  command=self.browse_image).grid(row=source_row, column=2, padx=(5, 0), pady=2)

        # VMT file selection
        vmt_row = 1
        ttk.Label(input_frame, text="VMT File (optional):").grid(row=vmt_row, column=0, sticky="w", pady=2)
        self.vmt_path = PlaceholderEntry(input_frame, placeholder="Select VMT file for automatic texture detection...")
        self.vmt_path.grid(row=vmt_row, column=1, sticky="ew", padx=(5, 0), pady=2)
        ttk.Button(input_frame, text="Browse",
                  command=self.browse_vmt).grid(row=vmt_row, column=2, padx=(5, 0), pady=2)

        input_frame.columnconfigure(1, weight=1)

        # Main content area
        content_frame = ttk.Frame(main_frame)
        content_frame.pack(fill="both", expand=True, pady=(0, 10))

        # Left side - Image preview and controls
        left_frame = ttk.Frame(content_frame)
        left_frame.pack(side="left", fill="both", expand=True)

        # Image preview
        preview_frame = ttk.LabelFrame(left_frame, text="Image Preview", padding=10)
        preview_frame.pack(fill="both", expand=True, pady=(0, 10))

        # Canvas for image display
        canvas_container = ttk.Frame(preview_frame)
        canvas_container.pack(fill="both", expand=True)

        # Add scrollbars
        h_scrollbar = ttk.Scrollbar(canvas_container, orient="horizontal")
        v_scrollbar = ttk.Scrollbar(canvas_container, orient="vertical")

        self.image_canvas = tk.Canvas(canvas_container, bg="gray90",
                                     xscrollcommand=h_scrollbar.set,
                                     yscrollcommand=v_scrollbar.set)

        h_scrollbar.config(command=self.image_canvas.xview)
        v_scrollbar.config(command=self.image_canvas.yview)

        h_scrollbar.pack(side="bottom", fill="x")
        v_scrollbar.pack(side="right", fill="y")
        self.image_canvas.pack(side="left", fill="both", expand=True)

        # Bind canvas events
        self.image_canvas.bind("<Button-1>", self.on_canvas_click)
        self.image_canvas.bind("<B1-Motion>", self.on_canvas_drag)
        self.image_canvas.bind("<ButtonRelease-1>", self.on_canvas_release)
        self.image_canvas.bind("<MouseWheel>", self.on_mouse_wheel)
        self.image_canvas.bind("<Button-3>", self.on_right_click)  # Right-click context menu

        # Keyboard shortcuts
        self.image_canvas.bind("<Key>", self.on_key_press)
        self.image_canvas.focus_set()  # Allow canvas to receive key events

        # Canvas control buttons
        canvas_controls = ttk.Frame(preview_frame)
        canvas_controls.pack(fill="x", pady=(5, 0))

        ttk.Button(canvas_controls, text="Zoom In",
                  command=self.zoom_in).pack(side="left")
        ttk.Button(canvas_controls, text="Zoom Out",
                  command=self.zoom_out).pack(side="left", padx=(5, 0))
        ttk.Button(canvas_controls, text="Fit to Window",
                  command=self.fit_to_window).pack(side="left", padx=(5, 0))

        self.zoom_label = ttk.Label(canvas_controls, text="100%")
        self.zoom_label.pack(side="right")

        # Right side - Regions and controls
        right_frame = ttk.Frame(content_frame)
        right_frame.pack(side="right", fill="y", padx=(10, 0))
        right_frame.config(width=320)

        # Settings frame
        settings_frame = ttk.LabelFrame(right_frame, text="Settings", padding=10)
        settings_frame.pack(fill="x", pady=(0, 10))

        # Grid settings
        grid_frame = ttk.Frame(settings_frame)
        grid_frame.pack(fill="x", pady=(0, 5))

        ttk.Checkbutton(grid_frame, text="Snap to Grid",
                       variable=self.snap_to_grid,
                       command=self.update_canvas).pack(side="left")
        ttk.Checkbutton(grid_frame, text="Show Grid",
                       variable=self.show_grid,
                       command=self.update_canvas).pack(side="left", padx=(10, 0))

        # Grid size slider
        grid_size_frame = ttk.Frame(settings_frame)
        grid_size_frame.pack(fill="x", pady=(0, 5))

        ttk.Label(grid_size_frame, text="Grid Size:").pack(side="left")
        self.grid_size_label = ttk.Label(grid_size_frame, text="16")
        self.grid_size_label.pack(side="right")

        self.grid_slider = ttk.Scale(grid_size_frame, from_=0, to=len(self.grid_sizes)-1,
                                    orient="horizontal", command=self.on_grid_size_change)
        self.grid_slider.pack(fill="x", padx=(5, 5))
        self.grid_slider.set(2)  # Default to 16

        # Quality of life settings
        qol_frame = ttk.Frame(settings_frame)
        qol_frame.pack(fill="x")

        ttk.Checkbutton(qol_frame, text="Auto-name regions",
                       variable=self.auto_name_regions).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(qol_frame, text="Preserve aspect ratio",
                       variable=self.preserve_aspect_ratio).grid(row=0, column=1, sticky="w")
        ttk.Checkbutton(qol_frame, text="Show coordinates",
                       variable=self.show_coordinates,
                       command=self.update_canvas).grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(qol_frame, text="Show dimensions",
                       variable=self.show_dimensions,
                       command=self.update_canvas).grid(row=1, column=1, sticky="w")

        # Region definition
        region_def_frame = ttk.LabelFrame(right_frame, text="Define Region", padding=10)
        region_def_frame.pack(fill="x", pady=(0, 10))

        # Region name
        ttk.Label(region_def_frame, text="Name:").grid(row=0, column=0, sticky="w", pady=2)
        self.region_name = tk.StringVar()
        ttk.Entry(region_def_frame, textvariable=self.region_name, width=20).grid(row=0, column=1, sticky="ew", padx=(5, 0), pady=2)

        # Coordinates
        ttk.Label(region_def_frame, text="X:").grid(row=1, column=0, sticky="w", pady=2)
        self.region_x = tk.IntVar()
        x_spinbox = tk.Spinbox(region_def_frame, from_=0, to=9999, textvariable=self.region_x, width=10)
        x_spinbox.grid(row=1, column=1, sticky="ew", padx=(5, 0), pady=2)
        x_spinbox.bind("<FocusOut>", self.on_coordinate_change)

        ttk.Label(region_def_frame, text="Y:").grid(row=2, column=0, sticky="w", pady=2)
        self.region_y = tk.IntVar()
        y_spinbox = tk.Spinbox(region_def_frame, from_=0, to=9999, textvariable=self.region_y, width=10)
        y_spinbox.grid(row=2, column=1, sticky="ew", padx=(5, 0), pady=2)
        y_spinbox.bind("<FocusOut>", self.on_coordinate_change)

        ttk.Label(region_def_frame, text="Width:").grid(row=3, column=0, sticky="w", pady=2)
        self.region_w = tk.IntVar()
        w_spinbox = tk.Spinbox(region_def_frame, from_=1, to=9999, textvariable=self.region_w, width=10)
        w_spinbox.grid(row=3, column=1, sticky="ew", padx=(5, 0), pady=2)
        w_spinbox.bind("<FocusOut>", self.on_coordinate_change)

        ttk.Label(region_def_frame, text="Height:").grid(row=4, column=0, sticky="w", pady=2)
        self.region_h = tk.IntVar()
        h_spinbox = tk.Spinbox(region_def_frame, from_=1, to=9999, textvariable=self.region_h, width=10)
        h_spinbox.grid(row=4, column=1, sticky="ew", padx=(5, 0), pady=2)
        h_spinbox.bind("<FocusOut>", self.on_coordinate_change)

        region_def_frame.columnconfigure(1, weight=1)

        # Region buttons
        region_buttons = ttk.Frame(region_def_frame)
        region_buttons.grid(row=5, column=0, columnspan=2, pady=(10, 0), sticky="ew")

        ttk.Button(region_buttons, text="Add Region",
                  command=self.add_region).pack(side="left")
        ttk.Button(region_buttons, text="Update Region",
                  command=self.update_region).pack(side="left", padx=(5, 0))
        ttk.Button(region_buttons, text="Delete Region",
                  command=self.delete_region).pack(side="left", padx=(5, 0))

        # Additional region tools
        region_tools = ttk.Frame(region_def_frame)
        region_tools.grid(row=6, column=0, columnspan=2, pady=(5, 0), sticky="ew")

        ttk.Button(region_tools, text="Duplicate Region",
                  command=self.duplicate_region).pack(side="left")
        ttk.Button(region_tools, text="Center on Image",
                  command=self.center_region).pack(side="left", padx=(5, 0))

        # Region list
        regions_frame = ttk.LabelFrame(right_frame, text="Regions", padding=10)
        regions_frame.pack(fill="both", expand=True, pady=(0, 10))

        # Listbox with scrollbar
        list_container = ttk.Frame(regions_frame)
        list_container.pack(fill="both", expand=True)

        list_scrollbar = ttk.Scrollbar(list_container)
        list_scrollbar.pack(side="right", fill="y")

        self.regions_listbox = tk.Listbox(list_container, yscrollcommand=list_scrollbar.set)
        self.regions_listbox.pack(side="left", fill="both", expand=True)
        list_scrollbar.config(command=self.regions_listbox.yview)

        self.regions_listbox.bind("<<ListboxSelect>>", self.on_region_select)

        # Region management buttons
        region_mgmt = ttk.Frame(regions_frame)
        region_mgmt.pack(fill="x", pady=(5, 0))

        ttk.Button(region_mgmt, text="Save Regions",
                  command=self.save_regions).pack(side="left")
        ttk.Button(region_mgmt, text="Load Regions",
                  command=self.load_regions).pack(side="left", padx=(5, 0))
        ttk.Button(region_mgmt, text="Export CSV",
                  command=self.export_regions_csv).pack(side="left", padx=(5, 0))
        ttk.Button(region_mgmt, text="Clear All",
                  command=self.clear_regions).pack(side="right")

        # VMT Tools
        vmt_frame = ttk.LabelFrame(right_frame, text="VMT Tools", padding=10)
        vmt_frame.pack(fill="x", pady=(0, 10))

        ttk.Button(vmt_frame, text="Show Related Textures",
                  command=self.show_related_textures_info).pack(fill="x", pady=(0, 5))
        ttk.Button(vmt_frame, text="Process All Related Textures",
                  command=self.process_vmt_textures).pack(fill="x")

        # Extraction section
        extract_frame = ttk.LabelFrame(right_frame, text="Extract", padding=10)
        extract_frame.pack(fill="x")

        ttk.Button(extract_frame, text="Extract Selected",
                  command=self.extract_selected).pack(fill="x", pady=(0, 5))
        ttk.Button(extract_frame, text="Extract All Regions",
                  command=self.extract_all).pack(fill="x")

        # Status frame with additional info
        status_frame = ttk.Frame(main_frame)
        status_frame.pack(fill="x", pady=(10, 0))

        self.status_label = ttk.Label(status_frame, text="Ready", foreground="green")
        self.status_label.pack(side="left")

        # VMT status label
        self.vmt_status_label = ttk.Label(status_frame, text="", foreground="purple")
        self.vmt_status_label.pack(side="left", padx=(10, 0))

        # Image info label
        self.image_info_label = ttk.Label(status_frame, text="", foreground="blue")
        self.image_info_label.pack(side="right")

        # Keyboard shortcuts info
        shortcuts_text = "Shortcuts: Delete=Remove Region, G=Toggle Grid, Ctrl+S=Save, Ctrl+O=Load, Ctrl+D=Duplicate, +/-=Zoom"
        ttk.Label(main_frame, text=shortcuts_text, font=("Arial", 8), foreground="gray").pack(pady=(5, 0))

        # Add tooltips
        self.add_tooltips()

        # Show VTF support status
        if VTFLIB_AVAILABLE:
            self.status_label.config(text="Ready (VTF support available)", foreground="green")
        else:
            self.status_label.config(text="Ready (VTF support not available - install VTFLibWrapper for VTF files)", foreground="orange")

        # Drawing state
        self.drawing = False
        self.draw_start_x = 0
        self.draw_start_y = 0
        self.current_rect = None

    def add_tooltips(self):
        """Add tooltips to UI elements."""
        # Grid settings tooltips
        ToolTip(self.grid_slider, "Adjust grid size for snapping. Use Hammer editor standard sizes.")

        # VMT tooltip
        ToolTip(self.vmt_path, "Select a VMT file to automatically load the base texture and find related textures (normalmap, detail, etc.)")

        # Canvas tooltip
        ToolTip(self.image_canvas, "Left-click and drag to create regions. Right-click for context menu. Mouse wheel to zoom. Supports PNG, JPG, TGA, BMP, and VTF files.")

    def browse_image(self):
        """Browse for source image file."""
        path = browse_file_with_context(
            self.image_path, context_key="subtexture_extraction_source_image",
            title="Select Source Image",
            filetypes=[("Image Files", "*.png *.jpg *.jpeg *.tga *.bmp *.vtf")]
        )
        if path:
            self.load_image()

    def browse_vmt(self):
        """Browse for VMT file."""
        path = browse_file_with_context(
            self.vmt_path, context_key="subtexture_extraction_vmt",
            title="Select VMT File",
            filetypes=[("VMT Files", "*.vmt"), ("All Files", "*.*")]
        )
        if path:
            self.vmt_file_path = path
            self.status_label.config(text=f"Selected VMT: {os.path.basename(path)}", foreground="blue")
            self.analyze_vmt_file()

    def analyze_vmt_file(self):
        """Analyze VMT file to find related textures."""
        if not self.vmt_file_path or not os.path.exists(self.vmt_file_path):
            self.status_label.config(text="VMT file not found", foreground="red")
            return

        try:
            with open(self.vmt_file_path, 'r', encoding='utf-8', errors='ignore') as f:
                vmt_content = f.read()

            print(f"VMT Content Preview: {vmt_content[:200]}...")  # Debug output
            vmt_content_lower = vmt_content.lower()

            # Common VMT texture parameters - prioritize basetexture first
            texture_params = [
                (r'\$basetexture\s+"?([^"\s]+)"?', 'basetexture'),
                (r'\$bumpmap\s+"?([^"\s]+)"?', 'bumpmap'),
                (r'\$normalmap\s+"?([^"\s]+)"?', 'normalmap'),
                (r'\$detail\s+"?([^"\s]+)"?', 'detail'),
                (r'\$envmapmask\s+"?([^"\s]+)"?', 'envmapmask'),
                (r'\$phongexponenttexture\s+"?([^"\s]+)"?', 'phongexponent'),
                (r'\$phongwarptexture\s+"?([^"\s]+)"?', 'phongwarp'),
                (r'\$selfillummask\s+"?([^"\s]+)"?', 'selfillum'),
                (r'\$blendmodulatetexture\s+"?([^"\s]+)"?', 'blendmodulate')
            ]

            self.related_textures = []
            base_texture_path = None
            base_dir = os.path.dirname(self.vmt_file_path)

            # Search for textures in priority order
            for param_pattern, param_type in texture_params:
                matches = re.findall(param_pattern, vmt_content_lower)
                print(f"Searching for {param_type}: found {len(matches)} matches")  # Debug
                for match in matches:
                    # Clean up the texture path
                    texture_path = match.strip().replace('\\', '/')
                    print(f"  Processing texture path: {texture_path}")  # Debug

                    # Try different file extensions and locations
                    found_texture = None
                    for ext in ['.vtf', '.tga', '.png', '.jpg', '.jpeg']:
                        # First try the basic relative search
                        search_dirs = [
                            base_dir,  # Same directory as VMT
                            os.path.join(base_dir, '..', 'materials'),  # Parent materials folder
                            os.path.join(base_dir, '..'),  # Parent directory
                            os.path.dirname(base_dir)  # One level up from VMT directory
                        ]

                        for search_dir in search_dirs:
                            if not search_dir:
                                continue

                            # Try both with and without materials subdirectory
                            possible_paths = [
                                os.path.join(search_dir, texture_path + ext),
                                os.path.join(search_dir, 'materials', texture_path + ext)
                            ]

                            for full_path in possible_paths:
                                if os.path.exists(full_path):
                                    found_texture = full_path
                                    break

                            if found_texture:
                                break

                        if found_texture:
                            break

                        # If not found locally, do a comprehensive workspace search
                        print(f"Local search failed for {texture_path}{ext}, searching workspace...")
                        found_texture = self.search_texture_in_workspace(texture_path, ext)
                        if found_texture:
                            break

                    if found_texture and found_texture not in self.related_textures:
                        self.related_textures.append(found_texture)

                        # If this is the base texture, save it for auto-loading
                        if param_type == 'basetexture' and not base_texture_path:
                            base_texture_path = found_texture

            # Auto-load the base texture if found
            if base_texture_path:
                self.image_path.set_text(base_texture_path)
                self.load_image()

                # Update VMT status
                vmt_name = os.path.basename(self.vmt_file_path)
                self.vmt_status_label.config(text=f"VMT: {vmt_name} ({len(self.related_textures)} textures)")

                # Show detailed status message
                base_name = os.path.basename(base_texture_path)
                total_textures = len(self.related_textures)
                self.status_label.config(
                    text=f"Auto-loaded base texture: {base_name}",
                    foreground="green"
                )

            elif self.related_textures:
                # Update VMT status
                vmt_name = os.path.basename(self.vmt_file_path)
                self.vmt_status_label.config(text=f"VMT: {vmt_name} ({len(self.related_textures)} textures, no base)")

                self.status_label.config(
                    text=f"Found {len(self.related_textures)} related textures (no base texture)",
                    foreground="orange"
                )
            else:
                # Update VMT status
                vmt_name = os.path.basename(self.vmt_file_path)
                self.vmt_status_label.config(text=f"VMT: {vmt_name} (no textures found)")

                self.status_label.config(
                    text="No related textures found",
                    foreground="orange"
                )

        except Exception as e:
            messagebox.showerror("Error", f"Failed to analyze VMT file: {e}")

    def search_texture_in_workspace(self, texture_path, extension):
        """Search for a texture file across all workspace folders."""
        # Get the base filename from the texture path
        texture_filename = os.path.basename(texture_path) + extension

        # Get all workspace root folders from the environment
        workspace_folders = [
            "a:\\Source 2 Exports",
            "s:\\SteamLibrary\\steamapps\\common\\GarrysMod\\garrysmod\\modelsrc\\riggs9162\\hlvr",
            "s:\\SteamLibrary\\steamapps\\common\\GarrysMod\\garrysmod\\materials\\models\\riggs9162\\hlvr",
            "s:\\SteamLibrary\\steamapps\\common\\GarrysMod\\garrysmod\\addons\\vault-materials",
            "s:\\SteamLibrary\\steamapps\\common\\GarrysMod\\garrysmod\\addons\\vault-models",
            "s:\\SteamLibrary\\steamapps\\common\\GarrysMod\\garrysmod\\addons\\vault-resources",
            "s:\\SteamLibrary\\steamapps\\common\\GarrysMod\\garrysmod\\addons\\vault-sounds",
            "s:\\SteamLibrary\\steamapps\\common\\GarrysMod\\garrysmod\\addons\\Half-Life Alyx Combine Extended"
        ]

        # Search patterns to try
        search_patterns = [
            texture_path + extension,  # Full path as specified in VMT
            texture_filename,  # Just the filename
            texture_path.split('/')[-1] + extension,  # Last part of path + extension
        ]

        # Remove materials/ prefix if present and try again
        if texture_path.startswith('materials/'):
            clean_path = texture_path[10:]  # Remove 'materials/' prefix
            search_patterns.append(clean_path + extension)

        # Add common Source engine path variations
        search_patterns.extend([
            f"materials/{texture_path}" + extension,  # Add materials/ prefix
            f"materials/models/{texture_path}" + extension,  # Add materials/models/ prefix
            f"riggs9162/hlvr/" + texture_path.split('riggs9162/hlvr/')[-1] + extension if 'riggs9162/hlvr/' in texture_path else None,
        ])

        # Remove None values
        search_patterns = [p for p in search_patterns if p is not None]

        print(f"Searching workspace for texture: {texture_path}{extension}")
        print(f"Search patterns: {search_patterns}")

        for workspace_folder in workspace_folders:
            if not os.path.exists(workspace_folder):
                continue

            # First, try direct construction for vault-materials structure
            if 'vault-materials' in workspace_folder:
                direct_path = os.path.join(workspace_folder, 'materials', texture_path + extension)
                if os.path.exists(direct_path):
                    print(f"Found texture (direct construction): {direct_path}")
                    return direct_path

            for pattern in search_patterns:
                # Try direct path construction first
                direct_path = os.path.join(workspace_folder, pattern)
                if os.path.exists(direct_path):
                    print(f"Found texture (direct): {direct_path}")
                    return direct_path

                # Walk through the workspace folder looking for the texture
                for root, dirs, files in os.walk(workspace_folder):
                    # Check if any file matches our pattern
                    for file in files:
                        if file.lower() == os.path.basename(pattern).lower():
                            full_path = os.path.join(root, file)
                            print(f"Found texture: {full_path}")
                            return full_path

                        # Also check if the full relative path matches
                        try:
                            rel_path = os.path.relpath(os.path.join(root, file), workspace_folder)
                            rel_path = rel_path.replace('\\', '/')
                            if rel_path.lower() == pattern.lower():
                                full_path = os.path.join(root, file)
                                print(f"Found texture (relative path match): {full_path}")
                                return full_path
                        except ValueError:
                            # Can happen if paths are on different drives
                            continue

        print(f"Texture not found in workspace: {texture_path}{extension}")
        return None

    def show_related_textures_info(self):
        """Show a dialog with information about all related textures."""
        if not self.related_textures:
            messagebox.showinfo("No Textures", "No related textures found.")
            return

        # Create info dialog
        info_dialog = tk.Toplevel(self)
        info_dialog.title("Related Textures")
        info_dialog.geometry("600x400")
        info_dialog.transient(self)
        info_dialog.grab_set()

        # Center the dialog
        info_dialog.geometry("+%d+%d" % (self.winfo_rootx() + 50, self.winfo_rooty() + 50))

        # Main frame
        main_frame = ttk.Frame(info_dialog, padding=10)
        main_frame.pack(fill="both", expand=True)

        # Title
        ttk.Label(main_frame, text=f"Found {len(self.related_textures)} Related Textures:",
                 font=("Arial", 12, "bold")).pack(pady=(0, 10))

        # Listbox with details
        list_frame = ttk.Frame(main_frame)
        list_frame.pack(fill="both", expand=True, pady=(0, 10))

        scrollbar = ttk.Scrollbar(list_frame)
        scrollbar.pack(side="right", fill="y")

        listbox = tk.Listbox(list_frame, yscrollcommand=scrollbar.set, font=("Consolas", 9))
        listbox.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=listbox.yview)

        # Populate with texture information
        for i, texture_path in enumerate(self.related_textures):
            filename = os.path.basename(texture_path)
            rel_dir = os.path.relpath(os.path.dirname(texture_path), os.path.dirname(self.vmt_file_path))

            # Determine texture type based on common naming conventions
            texture_type = "Unknown"
            lower_name = filename.lower()
            if any(keyword in lower_name for keyword in ['base', 'diffuse', 'color', 'albedo']):
                texture_type = "Base/Diffuse"
            elif any(keyword in lower_name for keyword in ['normal', 'bump']):
                texture_type = "Normal/Bump"
            elif any(keyword in lower_name for keyword in ['spec', 'gloss', 'rough']):
                texture_type = "Specular/Gloss"
            elif any(keyword in lower_name for keyword in ['detail']):
                texture_type = "Detail"
            elif any(keyword in lower_name for keyword in ['mask', 'alpha']):
                texture_type = "Mask/Alpha"

            display_text = f"{i+1:2d}. [{texture_type:12}] {filename}"
            listbox.insert("end", display_text)

        # Button frame
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill="x")

        def load_selected():
            selection = listbox.curselection()
            if selection:
                selected_texture = self.related_textures[selection[0]]
                self.image_path.set_text(selected_texture)
                self.load_image()
                info_dialog.destroy()

        ttk.Button(button_frame, text="Load Selected", command=load_selected).pack(side="left")
        ttk.Button(button_frame, text="Close", command=info_dialog.destroy).pack(side="right")

        # Select first item (base texture if it was auto-loaded)
        if self.related_textures:
            listbox.selection_set(0)

    def on_grid_size_change(self, value):
        """Handle grid size slider change."""
        index = int(float(value))
        self.grid_size.set(self.grid_sizes[index])
        self.grid_size_label.config(text=str(self.grid_sizes[index]))
        self.update_canvas()

    def snap_to_grid_value(self, value):
        """Snap a value to the current grid size."""
        if not self.snap_to_grid.get():
            return value

        grid_size = self.grid_size.get()
        return round(value / grid_size) * grid_size

    def load_image(self):
        """Load the selected image."""
        path = self.image_path.get()
        if not path or not os.path.exists(path):
            return

        try:
            # Check if it's a VTF file
            if path.lower().endswith('.vtf'):
                if not VTFLIB_AVAILABLE:
                    messagebox.showerror("VTF Support Error",
                                       "VTFLib is not available. Cannot load VTF files.\n"
                                       "Please install VTFLibWrapper to load VTF files.")
                    self.status_label.config(text="VTFLib not available", foreground="red")
                    return

                # Load VTF file using our custom function
                self.source_image = load_vtf_as_pil_image(path)
                self.status_label.config(text="VTF image loaded", foreground="green")
            else:
                # Load regular image file
                self.source_image = Image.open(path).convert("RGBA")
                self.status_label.config(text="Image loaded", foreground="green")

            self.display_image()

            # Update image info
            file_size = os.path.getsize(path)
            size_mb = file_size / (1024 * 1024)
            file_ext = os.path.splitext(path)[1].upper()
            self.image_info_label.config(
                text=f"{self.source_image.width}×{self.source_image.height} - {size_mb:.1f}MB ({file_ext})"
            )

        except Exception as e:
            error_msg = f"Failed to load image: {e}"
            messagebox.showerror("Error", error_msg)
            self.status_label.config(text="Error loading image", foreground="red")
            self.image_info_label.config(text="")

    def display_image(self):
        """Display the image on the canvas."""
        if not self.source_image:
            return

        # Calculate initial scale to fit image
        canvas_width = self.image_canvas.winfo_width()
        canvas_height = self.image_canvas.winfo_height()

        if canvas_width > 1 and canvas_height > 1:  # Canvas is initialized
            scale_x = canvas_width / self.source_image.width
            scale_y = canvas_height / self.source_image.height
            self.canvas_scale = min(scale_x, scale_y, 1.0)  # Don't scale up initially
        else:
            self.canvas_scale = 1.0

        self.update_canvas()

    def update_canvas(self):
        """Update the canvas display."""
        if not self.source_image:
            return

        # Clear canvas
        self.image_canvas.delete("all")

        # Scale image
        display_width = int(self.source_image.width * self.canvas_scale)
        display_height = int(self.source_image.height * self.canvas_scale)

        self.preview_image = self.source_image.resize((display_width, display_height), Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(self.preview_image)

        # Display image
        self.image_canvas.config(scrollregion=(0, 0, display_width, display_height))
        self.image_canvas.create_image(0, 0, anchor="nw", image=self.photo)

        # Draw grid if enabled
        if self.show_grid.get():
            self.draw_grid()

        # Draw regions
        self.draw_regions()

        # Update zoom label
        self.zoom_label.config(text=f"{int(self.canvas_scale * 100)}%")

    def draw_grid(self):
        """Draw grid on the canvas."""
        if not self.source_image:
            return

        grid_size = self.grid_size.get() * self.canvas_scale
        display_width = int(self.source_image.width * self.canvas_scale)
        display_height = int(self.source_image.height * self.canvas_scale)

        # Draw vertical lines
        x = 0
        while x <= display_width:
            self.image_canvas.create_line(x, 0, x, display_height,
                                        fill="lightgray", width=1, tags="grid")
            x += grid_size

        # Draw horizontal lines
        y = 0
        while y <= display_height:
            self.image_canvas.create_line(0, y, display_width, y,
                                        fill="lightgray", width=1, tags="grid")
            y += grid_size

    def draw_regions(self):
        """Draw all regions on the canvas."""
        for i, region in enumerate(self.regions):
            # Scale coordinates
            x1 = region.x * self.canvas_scale
            y1 = region.y * self.canvas_scale
            x2 = (region.x + region.w) * self.canvas_scale
            y2 = (region.y + region.h) * self.canvas_scale

            # Choose color based on selection
            if i == self.selected_region and self.highlight_selected.get():
                color = "red"
                width = 3
            else:
                color = "blue"
                width = 2

            # Draw rectangle
            self.image_canvas.create_rectangle(x1, y1, x2, y2, outline=color, width=width, tags="region")

            # Draw label
            label_text = region.name
            if self.show_coordinates.get():
                label_text += f" ({region.x}, {region.y})"
            if self.show_dimensions.get():
                label_text += f" {region.w}x{region.h}"

            self.image_canvas.create_text(x1 + 5, y1 + 5, anchor="nw", text=label_text,
                                        fill=color, font=("Arial", 10, "bold"), tags="region")

            # Draw corner handles for selected region
            if i == self.selected_region and self.highlight_selected.get():
                handle_size = 6
                # Top-left
                self.image_canvas.create_rectangle(x1-handle_size//2, y1-handle_size//2,
                                                 x1+handle_size//2, y1+handle_size//2,
                                                 fill="red", outline="darkred", tags="handle")
                # Top-right
                self.image_canvas.create_rectangle(x2-handle_size//2, y1-handle_size//2,
                                                 x2+handle_size//2, y1+handle_size//2,
                                                 fill="red", outline="darkred", tags="handle")
                # Bottom-left
                self.image_canvas.create_rectangle(x1-handle_size//2, y2-handle_size//2,
                                                 x1+handle_size//2, y2+handle_size//2,
                                                 fill="red", outline="darkred", tags="handle")
                # Bottom-right
                self.image_canvas.create_rectangle(x2-handle_size//2, y2-handle_size//2,
                                                 x2+handle_size//2, y2+handle_size//2,
                                                 fill="red", outline="darkred", tags="handle")

    def on_canvas_click(self, event):
        """Handle canvas click for region drawing."""
        if not self.source_image:
            return

        canvas_x = self.image_canvas.canvasx(event.x)
        canvas_y = self.image_canvas.canvasy(event.y)

        self.drawing = True
        self.draw_start_x = canvas_x / self.canvas_scale
        self.draw_start_y = canvas_y / self.canvas_scale

        # Remove current drawing rectangle
        if self.current_rect:
            self.image_canvas.delete(self.current_rect)
            self.current_rect = None

    def on_canvas_drag(self, event):
        """Handle canvas drag for region drawing."""
        if not self.drawing or not self.source_image:
            return

        canvas_x = self.image_canvas.canvasx(event.x)
        canvas_y = self.image_canvas.canvasy(event.y)

        # Remove previous rectangle
        if self.current_rect:
            self.image_canvas.delete(self.current_rect)

        # Draw new rectangle
        x1 = self.draw_start_x * self.canvas_scale
        y1 = self.draw_start_y * self.canvas_scale

        self.current_rect = self.image_canvas.create_rectangle(
            x1, y1, canvas_x, canvas_y, outline="green", width=2, dash=(5, 5)
        )

    def on_canvas_release(self, event):
        """Handle canvas release to finalize region."""
        if not self.drawing or not self.source_image:
            return

        canvas_x = self.image_canvas.canvasx(event.x)
        canvas_y = self.image_canvas.canvasy(event.y)

        # Calculate region coordinates in image space
        x1_raw = min(self.draw_start_x, canvas_x / self.canvas_scale)
        y1_raw = min(self.draw_start_y, canvas_y / self.canvas_scale)
        x2_raw = max(self.draw_start_x, canvas_x / self.canvas_scale)
        y2_raw = max(self.draw_start_y, canvas_y / self.canvas_scale)

        # Apply grid snapping
        x1 = int(self.snap_to_grid_value(x1_raw))
        y1 = int(self.snap_to_grid_value(y1_raw))
        x2 = int(self.snap_to_grid_value(x2_raw))
        y2 = int(self.snap_to_grid_value(y2_raw))

        # Apply aspect ratio preservation if enabled
        if self.preserve_aspect_ratio.get() and hasattr(self, 'aspect_ratio'):
            width = x2 - x1
            height = y2 - y1
            if width / height > self.aspect_ratio:
                # Adjust width
                width = int(height * self.aspect_ratio)
                x2 = x1 + width
            else:
                # Adjust height
                height = int(width / self.aspect_ratio)
                y2 = y1 + height

        # Update region input fields
        self.region_x.set(max(0, x1))
        self.region_y.set(max(0, y1))
        self.region_w.set(max(1, x2 - x1))
        self.region_h.set(max(1, y2 - y1))

        # Auto-name region if enabled and empty
        if self.auto_name_regions.get() and not self.region_name.get():
            self.region_name.set(f"region_{len(self.regions) + 1}")

        self.drawing = False
        if self.current_rect:
            self.image_canvas.delete(self.current_rect)
            self.current_rect = None

    def zoom_in(self):
        """Zoom in on the image."""
        self.canvas_scale *= 1.25
        self.update_canvas()

    def zoom_out(self):
        """Zoom out on the image."""
        self.canvas_scale /= 1.25
        self.update_canvas()

    def fit_to_window(self):
        """Fit image to window."""
        if not self.source_image:
            return

        canvas_width = self.image_canvas.winfo_width()
        canvas_height = self.image_canvas.winfo_height()

        if canvas_width > 1 and canvas_height > 1:
            scale_x = canvas_width / self.source_image.width
            scale_y = canvas_height / self.source_image.height
            self.canvas_scale = min(scale_x, scale_y)
            self.update_canvas()

    def on_mouse_wheel(self, event):
        """Handle mouse wheel for zooming."""
        if event.delta > 0:
            self.zoom_in()
        else:
            self.zoom_out()

    def on_right_click(self, event):
        """Handle right-click context menu."""
        if not self.source_image:
            return

        # Create context menu
        context_menu = tk.Menu(self, tearoff=0)
        context_menu.add_command(label="Add Region Here", command=lambda: self.add_region_at_position(event))
        context_menu.add_separator()
        context_menu.add_command(label="Zoom In", command=self.zoom_in)
        context_menu.add_command(label="Zoom Out", command=self.zoom_out)
        context_menu.add_command(label="Fit to Window", command=self.fit_to_window)

        try:
            context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            context_menu.grab_release()

    def on_key_press(self, event):
        """Handle keyboard shortcuts."""
        if event.keysym == "Delete" and self.selected_region is not None:
            self.delete_region()
        elif event.keysym == "plus" or event.keysym == "equal":
            self.zoom_in()
        elif event.keysym == "minus":
            self.zoom_out()
        elif event.keysym == "g":
            self.show_grid.set(not self.show_grid.get())
            self.update_canvas()
        elif event.keysym == "s" and event.state & 0x4:  # Ctrl+S
            self.save_regions()
        elif event.keysym == "o" and event.state & 0x4:  # Ctrl+O
            self.load_regions()
        elif event.keysym == "d" and event.state & 0x4:  # Ctrl+D
            if self.selected_region is not None:
                self.duplicate_region()

    def add_region_at_position(self, event):
        """Add a region at the right-click position."""
        canvas_x = self.image_canvas.canvasx(event.x)
        canvas_y = self.image_canvas.canvasy(event.y)

        # Convert to image coordinates
        img_x = int(self.snap_to_grid_value(canvas_x / self.canvas_scale))
        img_y = int(self.snap_to_grid_value(canvas_y / self.canvas_scale))

        # Set default size
        default_size = self.grid_size.get() * 4  # 4 grid units

        self.region_x.set(img_x)
        self.region_y.set(img_y)
        self.region_w.set(default_size)
        self.region_h.set(default_size)

        if self.auto_name_regions.get():
            self.region_name.set(f"region_{len(self.regions) + 1}")

    def on_coordinate_change(self, event=None):
        """Handle coordinate input changes and apply grid snapping."""
        if self.snap_to_grid.get():
            # Apply grid snapping to all coordinate fields
            self.region_x.set(int(self.snap_to_grid_value(self.region_x.get())))
            self.region_y.set(int(self.snap_to_grid_value(self.region_y.get())))
            self.region_w.set(max(1, int(self.snap_to_grid_value(self.region_w.get()))))
            self.region_h.set(max(1, int(self.snap_to_grid_value(self.region_h.get()))))

        # Update canvas to show preview if a region is selected
        if self.selected_region is not None:
            self.update_canvas()

    def add_region(self):
        """Add a new region."""
        name = self.region_name.get().strip()
        if not name:
            messagebox.showerror("Error", "Please enter a region name.")
            return

        # Check for duplicate names
        if any(region.name == name for region in self.regions):
            messagebox.showerror("Error", "A region with this name already exists.")
            return

        x = self.region_x.get()
        y = self.region_y.get()
        w = self.region_w.get()
        h = self.region_h.get()

        if w <= 0 or h <= 0:
            messagebox.showerror("Error", "Width and height must be greater than 0.")
            return

        # Add region
        region = Region(name, x, y, w, h)
        self.regions.append(region)

        # Update listbox
        self.update_regions_list()

        # Update canvas
        self.update_canvas()

        # Clear input fields
        self.region_name.set("")
        self.region_x.set(0)
        self.region_y.set(0)
        self.region_w.set(0)
        self.region_h.set(0)

        self.status_label.config(text=f"Added region: {name}", foreground="green")

    def duplicate_region(self):
        """Duplicate the selected region."""
        if self.selected_region is None:
            messagebox.showerror("Error", "Please select a region to duplicate.")
            return

        source_region = self.regions[self.selected_region]

        # Find a unique name
        base_name = source_region.name
        if base_name.endswith("_copy"):
            base_name = base_name[:-5]

        counter = 1
        new_name = f"{base_name}_copy"
        while any(region.name == new_name for region in self.regions):
            counter += 1
            new_name = f"{base_name}_copy_{counter}"

        # Create duplicate with slight offset
        offset = self.grid_size.get() if self.snap_to_grid.get() else 10
        new_region = Region(
            new_name,
            source_region.x + offset,
            source_region.y + offset,
            source_region.w,
            source_region.h
        )

        self.regions.append(new_region)
        self.update_regions_list()
        self.update_canvas()

        # Select the new region
        self.selected_region = len(self.regions) - 1
        self.regions_listbox.selection_clear(0, "end")
        self.regions_listbox.selection_set(self.selected_region)
        self.on_region_select(None)

        self.status_label.config(text=f"Duplicated region: {new_name}", foreground="green")

    def center_region(self):
        """Center the current region on the image."""
        if not self.source_image:
            messagebox.showerror("Error", "Please load a source image first.")
            return

        w = self.region_w.get()
        h = self.region_h.get()

        if w <= 0 or h <= 0:
            messagebox.showerror("Error", "Please set valid width and height first.")
            return

        # Calculate center position
        center_x = (self.source_image.width - w) // 2
        center_y = (self.source_image.height - h) // 2

        # Apply grid snapping
        center_x = int(self.snap_to_grid_value(center_x))
        center_y = int(self.snap_to_grid_value(center_y))

        self.region_x.set(max(0, center_x))
        self.region_y.set(max(0, center_y))

        self.status_label.config(text="Region centered on image", foreground="green")

    def update_region(self):
        """Update the selected region."""
        if self.selected_region is None:
            messagebox.showerror("Error", "Please select a region to update.")
            return

        name = self.region_name.get().strip()
        if not name:
            messagebox.showerror("Error", "Please enter a region name.")
            return

        # Check for duplicate names (excluding current region)
        for i, region in enumerate(self.regions):
            if i != self.selected_region and region.name == name:
                messagebox.showerror("Error", "A region with this name already exists.")
                return

        x = self.region_x.get()
        y = self.region_y.get()
        w = self.region_w.get()
        h = self.region_h.get()

        if w <= 0 or h <= 0:
            messagebox.showerror("Error", "Width and height must be greater than 0.")
            return

        # Update region
        self.regions[self.selected_region] = Region(name, x, y, w, h)

        # Update listbox
        self.update_regions_list()

        # Update canvas
        self.update_canvas()

        self.status_label.config(text=f"Updated region: {name}", foreground="green")

    def delete_region(self):
        """Delete the selected region."""
        if self.selected_region is None:
            messagebox.showerror("Error", "Please select a region to delete.")
            return

        region_name = self.regions[self.selected_region].name

        # Confirm deletion
        result = messagebox.askyesno("Confirm Deletion",
                                   f"Delete region '{region_name}'?")
        if not result:
            return

        # Delete region
        del self.regions[self.selected_region]
        self.selected_region = None

        # Update listbox
        self.update_regions_list()

        # Update canvas
        self.update_canvas()

        # Clear input fields
        self.region_name.set("")
        self.region_x.set(0)
        self.region_y.set(0)
        self.region_w.set(0)
        self.region_h.set(0)

        self.status_label.config(text=f"Deleted region: {region_name}", foreground="green")

    def update_regions_list(self):
        """Update the regions listbox."""
        self.regions_listbox.delete(0, "end")
        for region in self.regions:
            self.regions_listbox.insert("end", str(region))

    def on_region_select(self, event):
        """Handle region selection in listbox."""
        selection = self.regions_listbox.curselection()
        if selection:
            self.selected_region = selection[0]
            region = self.regions[self.selected_region]

            # Update input fields
            self.region_name.set(region.name)
            self.region_x.set(region.x)
            self.region_y.set(region.y)
            self.region_w.set(region.w)
            self.region_h.set(region.h)

            # Update canvas
            self.update_canvas()
        else:
            self.selected_region = None

    def save_regions(self):
        """Save regions to a JSON file."""
        if not self.regions:
            messagebox.showinfo("No Regions", "No regions to save.")
            return

        output_path = save_file_with_context(
            context_key="subtexture_extraction_save_regions",
            title="Save Regions",
            defaultextension=".json",
            filetypes=[("JSON Files", "*.json")]
        )

        if output_path:
            try:
                data = {
                    "image_path": self.image_path.get(),
                    "regions": [region.to_dict() for region in self.regions]
                }

                with open(output_path, 'w') as f:
                    json.dump(data, f, indent=2)

                self.status_label.config(text=f"Regions saved: {os.path.basename(output_path)}",
                                        foreground="green")

            except Exception as e:
                messagebox.showerror("Error", f"Failed to save regions: {e}")

    def load_regions(self):
        """Load regions from a JSON file."""
        file_path = browse_file_with_context(
            entry=None, context_key="subtexture_extraction_load_regions",
            title="Load Regions",
            filetypes=[("JSON Files", "*.json")]
        )

        if file_path:
            try:
                with open(file_path, 'r') as f:
                    data = json.load(f)

                # Load regions
                self.regions = [Region.from_dict(region_data) for region_data in data.get("regions", [])]

                # Load image path if available
                if "image_path" in data and os.path.exists(data["image_path"]):
                    self.image_path.set_text(data["image_path"])
                    self.load_image()

                # Update UI
                self.update_regions_list()
                self.update_canvas()

                self.status_label.config(text=f"Loaded {len(self.regions)} regions",
                                        foreground="green")

            except Exception as e:
                messagebox.showerror("Error", f"Failed to load regions: {e}")

    def export_regions_csv(self):
        """Export regions to CSV format."""
        if not self.regions:
            messagebox.showinfo("No Regions", "No regions to export.")
            return

        output_path = save_file_with_context(
            context_key="subtexture_extraction_export_csv",
            title="Export Regions to CSV",
            defaultextension=".csv",
            filetypes=[("CSV Files", "*.csv")]
        )

        if output_path:
            try:
                import csv
                with open(output_path, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    # Write header
                    writer.writerow(["Name", "X", "Y", "Width", "Height", "X2", "Y2"])
                    # Write region data
                    for region in self.regions:
                        writer.writerow([
                            region.name,
                            region.x,
                            region.y,
                            region.w,
                            region.h,
                            region.x + region.w,
                            region.y + region.h
                        ])

                self.status_label.config(text=f"Regions exported: {os.path.basename(output_path)}",
                                        foreground="green")

            except Exception as e:
                messagebox.showerror("Error", f"Failed to export regions: {e}")

    def clear_regions(self):
        """Clear all regions."""
        if not self.regions:
            return

        result = messagebox.askyesno("Confirm Clear",
                                   f"Clear all {len(self.regions)} regions?")
        if result:
            self.regions.clear()
            self.selected_region = None
            self.update_regions_list()
            self.update_canvas()

            # Clear input fields
            self.region_name.set("")
            self.region_x.set(0)
            self.region_y.set(0)
            self.region_w.set(0)
            self.region_h.set(0)

            self.status_label.config(text="All regions cleared", foreground="green")

    def load_vmt_textures(self):
        """Load VMT file and automatically detect related textures."""
        if not self.vmt_file_path:
            messagebox.showerror("Error", "Please select a VMT file first.")
            return

        if not self.related_textures:
            messagebox.showwarning("Warning", "No related textures found in the VMT file.")
            return

        # Show selection dialog
        dialog = VMTTextureDialog(self, self.related_textures)
        if dialog.result:
            selected_texture = dialog.result
            self.image_path.set_text(selected_texture)
            self.load_image()

    def process_vmt_textures(self):
        """Process all related textures with the current regions."""
        if not self.related_textures:
            messagebox.showerror("Error", "No related textures found. Load a VMT file first.")
            return

        if not self.regions:
            messagebox.showerror("Error", "No regions defined for extraction.")
            return

        output_folder = browse_folder_with_context(
            entry=None, context_key="subtexture_extraction_vmt_output",
            title="Select output folder for VMT texture processing"
        )
        if not output_folder:
            return

        total_processed = 0
        total_errors = 0

        # Process each texture
        for texture_path in self.related_textures:
            try:
                # Load texture
                texture_image = Image.open(texture_path).convert("RGBA")
                texture_name = os.path.splitext(os.path.basename(texture_path))[0]

                # Create output subdirectory
                texture_output_dir = os.path.join(output_folder, texture_name)
                os.makedirs(texture_output_dir, exist_ok=True)

                # Extract all regions from this texture
                for region in self.regions:
                    try:
                        # Extract region
                        x1 = max(0, region.x)
                        y1 = max(0, region.y)
                        x2 = min(texture_image.width, region.x + region.w)
                        y2 = min(texture_image.height, region.y + region.h)

                        if x2 > x1 and y2 > y1:
                            extracted_image = texture_image.crop((x1, y1, x2, y2))

                            # Save extracted image
                            output_filename = f"{texture_name}_{region.name}.png"
                            output_path = os.path.join(texture_output_dir, output_filename)
                            extracted_image.save(output_path)
                            total_processed += 1
                        else:
                            total_errors += 1

                    except Exception as e:
                        print(f"Error extracting region {region.name} from {texture_name}: {e}")
                        total_errors += 1

            except Exception as e:
                print(f"Error processing texture {texture_path}: {e}")
                total_errors += 1

        messagebox.showinfo("VMT Processing Complete",
                           f"Processed {len(self.related_textures)} textures.\n"
                           f"Extracted {total_processed} sub-textures.\n"
                           f"{total_errors} errors occurred.")

        self.status_label.config(
            text=f"VMT processing complete: {total_processed} extracted, {total_errors} errors",
            foreground="green" if total_errors == 0 else "orange"
        )

    def extract_selected(self):
        """Extract the selected region."""
        if self.selected_region is None:
            messagebox.showerror("Error", "Please select a region to extract.")
            return

        if not self.source_image:
            messagebox.showerror("Error", "Please load a source image first.")
            return

        region = self.regions[self.selected_region]
        self.extract_region(region)

    def extract_all(self):
        """Extract all regions."""
        if not self.regions:
            messagebox.showinfo("No Regions", "No regions defined for extraction.")
            return

        if not self.source_image:
            messagebox.showerror("Error", "Please load a source image first.")
            return

        output_folder = browse_folder_with_context(
            entry=None, context_key="subtexture_extraction_batch_output",
            title="Select output folder for extracted regions"
        )
        if not output_folder:
            return

        extracted = 0
        errors = 0

        for region in self.regions:
            try:
                # Extract region
                x1 = max(0, region.x)
                y1 = max(0, region.y)
                x2 = min(self.source_image.width, region.x + region.w)
                y2 = min(self.source_image.height, region.y + region.h)

                if x2 > x1 and y2 > y1:
                    extracted_image = self.source_image.crop((x1, y1, x2, y2))

                    # Save extracted image
                    output_filename = f"{region.name}.png"
                    output_path = os.path.join(output_folder, output_filename)
                    extracted_image.save(output_path)
                    extracted += 1
                else:
                    errors += 1

            except Exception as e:
                print(f"Error extracting region {region.name}: {e}")
                errors += 1

        messagebox.showinfo("Extraction Complete",
                           f"Extracted {extracted} regions.\n{errors} errors occurred.")
        self.status_label.config(text=f"Extraction complete: {extracted} extracted, {errors} errors",
                                foreground="green" if errors == 0 else "orange")

    def extract_region(self, region):
        """Extract a single region and save it."""
        try:
            # Extract region
            x1 = max(0, region.x)
            y1 = max(0, region.y)
            x2 = min(self.source_image.width, region.x + region.w)
            y2 = min(self.source_image.height, region.y + region.h)

            if x2 <= x1 or y2 <= y1:
                messagebox.showerror("Error", "Invalid region coordinates.")
                return

            extracted_image = self.source_image.crop((x1, y1, x2, y2))

            # Save extracted image
            output_path = save_file_with_context(
                context_key="subtexture_extraction_single_save",
                title=f"Save extracted region: {region.name}",
                defaultextension=".png",
                initialvalue=f"{region.name}.png",
                filetypes=[("PNG Files", "*.png"), ("JPEG Files", "*.jpg"), ("TGA Files", "*.tga")]
            )

            if output_path:
                # Convert to RGB if saving as JPEG
                if output_path.lower().endswith(('.jpg', '.jpeg')):
                    save_image = Image.new("RGB", extracted_image.size, (255, 255, 255))
                    save_image.paste(extracted_image, mask=extracted_image.split()[-1] if len(extracted_image.split()) == 4 else None)
                else:
                    save_image = extracted_image

                save_image.save(output_path)
                self.status_label.config(text=f"Extracted: {os.path.basename(output_path)}",
                                        foreground="green")

        except Exception as e:
            messagebox.showerror("Error", f"Failed to extract region: {e}")
            self.status_label.config(text="Extraction failed", foreground="red")


class VMTTextureDialog:
    """Dialog for selecting which texture to load from VMT-related textures."""

    def __init__(self, parent, texture_list):
        self.result = None

        # Create dialog window
        self.dialog = tk.Toplevel(parent)
        self.dialog.title("Select Texture")
        self.dialog.geometry("500x400")
        self.dialog.transient(parent)
        self.dialog.grab_set()

        # Center the dialog
        self.dialog.geometry("+%d+%d" % (parent.winfo_rootx() + 50, parent.winfo_rooty() + 50))

        # Main frame
        main_frame = ttk.Frame(self.dialog, padding=10)
        main_frame.pack(fill="both", expand=True)

        # Instructions
        ttk.Label(main_frame, text="Select a texture to load as the source image:",
                 font=("Arial", 10, "bold")).pack(pady=(0, 10))

        # Listbox with scrollbar
        list_frame = ttk.Frame(main_frame)
        list_frame.pack(fill="both", expand=True, pady=(0, 10))

        scrollbar = ttk.Scrollbar(list_frame)
        scrollbar.pack(side="right", fill="y")

        self.listbox = tk.Listbox(list_frame, yscrollcommand=scrollbar.set)
        self.listbox.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.listbox.yview)

        # Populate listbox
        for texture_path in texture_list:
            display_name = os.path.basename(texture_path)
            self.listbox.insert("end", f"{display_name} - {texture_path}")

        self.listbox.bind("<Double-Button-1>", self.on_select)

        # Buttons
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill="x")

        ttk.Button(button_frame, text="Select", command=self.on_select).pack(side="right")
        ttk.Button(button_frame, text="Cancel", command=self.on_cancel).pack(side="right", padx=(0, 5))

        # Store texture list
        self.texture_list = texture_list

        # Select first item by default
        if texture_list:
            self.listbox.selection_set(0)

        # Wait for dialog to close
        self.dialog.wait_window()

    def on_select(self, event=None):
        """Handle texture selection."""
        selection = self.listbox.curselection()
        if selection:
            self.result = self.texture_list[selection[0]]
        self.dialog.destroy()

    def on_cancel(self):
        """Handle dialog cancellation."""
        self.result = None
        self.dialog.destroy()


class ToolTip:
    """Simple tooltip class for tkinter widgets."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tooltip = None
        self.widget.bind("<Enter>", self.show_tooltip)
        self.widget.bind("<Leave>", self.hide_tooltip)

    def show_tooltip(self, event=None):
        """Show the tooltip."""
        if self.tooltip:
            return

        x = self.widget.winfo_rootx() + 25
        y = self.widget.winfo_rooty() + 25

        self.tooltip = tk.Toplevel(self.widget)
        self.tooltip.wm_overrideredirect(True)
        self.tooltip.wm_geometry(f"+{x}+{y}")

        label = tk.Label(self.tooltip, text=self.text,
                        background="lightyellow", foreground="black",
                        relief="solid", borderwidth=1,
                        font=("Arial", 9))
        label.pack()

    def hide_tooltip(self, event=None):
        """Hide the tooltip."""
        if self.tooltip:
            self.tooltip.destroy()
            self.tooltip = None