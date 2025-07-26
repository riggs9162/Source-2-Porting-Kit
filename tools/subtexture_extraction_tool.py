"""
Subtexture Extraction Tool - Extract sub-regions from textures based on defined regions.
"""

import os
import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from PIL import Image, ImageTk, ImageDraw
from .base_tool import BaseTool, register_tool
from .utils import PlaceholderEntry, browse_file

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
        return ["PIL"]
    
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
        
        self.setup_ui()
    
    def setup_ui(self):
        """Set up the user interface."""
        main_frame = ttk.Frame(self)
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)
        
        # Input section
        input_frame = ttk.LabelFrame(main_frame, text="Source Image", padding=10)
        input_frame.pack(fill="x", pady=(0, 10))
        
        ttk.Label(input_frame, text="Source Image:").grid(row=0, column=0, sticky="w", pady=2)
        self.image_path = PlaceholderEntry(input_frame, placeholder="Select source image file...")
        self.image_path.grid(row=0, column=1, sticky="ew", padx=(5, 0), pady=2)
        ttk.Button(input_frame, text="Browse", 
                  command=self.browse_image).grid(row=0, column=2, padx=(5, 0), pady=2)
        
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
        right_frame.config(width=300)
        
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
        tk.Spinbox(region_def_frame, from_=0, to=9999, textvariable=self.region_x, width=10).grid(row=1, column=1, sticky="ew", padx=(5, 0), pady=2)
        
        ttk.Label(region_def_frame, text="Y:").grid(row=2, column=0, sticky="w", pady=2)
        self.region_y = tk.IntVar()
        tk.Spinbox(region_def_frame, from_=0, to=9999, textvariable=self.region_y, width=10).grid(row=2, column=1, sticky="ew", padx=(5, 0), pady=2)
        
        ttk.Label(region_def_frame, text="Width:").grid(row=3, column=0, sticky="w", pady=2)
        self.region_w = tk.IntVar()
        tk.Spinbox(region_def_frame, from_=1, to=9999, textvariable=self.region_w, width=10).grid(row=3, column=1, sticky="ew", padx=(5, 0), pady=2)
        
        ttk.Label(region_def_frame, text="Height:").grid(row=4, column=0, sticky="w", pady=2)
        self.region_h = tk.IntVar()
        tk.Spinbox(region_def_frame, from_=1, to=9999, textvariable=self.region_h, width=10).grid(row=4, column=1, sticky="ew", padx=(5, 0), pady=2)
        
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
        ttk.Button(region_mgmt, text="Clear All", 
                  command=self.clear_regions).pack(side="right")
        
        # Extraction section
        extract_frame = ttk.LabelFrame(right_frame, text="Extract", padding=10)
        extract_frame.pack(fill="x")
        
        ttk.Button(extract_frame, text="Extract Selected", 
                  command=self.extract_selected).pack(fill="x", pady=(0, 5))
        ttk.Button(extract_frame, text="Extract All Regions", 
                  command=self.extract_all).pack(fill="x")
        
        # Status label
        self.status_label = ttk.Label(main_frame, text="Ready", foreground="green")
        self.status_label.pack(pady=(10, 0))
        
        # Drawing state
        self.drawing = False
        self.draw_start_x = 0
        self.draw_start_y = 0
        self.current_rect = None
    
    def browse_image(self):
        """Browse for source image file."""
        path = browse_file(
            title="Select Source Image",
            filetypes=[("Image Files", "*.png *.jpg *.jpeg *.tga *.bmp")]
        )
        if path:
            self.image_path.set_text(path)
            self.load_image()
    
    def load_image(self):
        """Load the selected image."""
        path = self.image_path.get()
        if not path or not os.path.exists(path):
            return
        
        try:
            self.source_image = Image.open(path).convert("RGBA")
            self.display_image()
            self.status_label.config(text="Image loaded", foreground="green")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load image: {e}")
            self.status_label.config(text="Error loading image", foreground="red")
    
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
        
        # Draw regions
        self.draw_regions()
        
        # Update zoom label
        self.zoom_label.config(text=f"{int(self.canvas_scale * 100)}%")
    
    def draw_regions(self):
        """Draw all regions on the canvas."""
        for i, region in enumerate(self.regions):
            # Scale coordinates
            x1 = region.x * self.canvas_scale
            y1 = region.y * self.canvas_scale
            x2 = (region.x + region.w) * self.canvas_scale
            y2 = (region.y + region.h) * self.canvas_scale
            
            # Choose color based on selection
            color = "red" if i == self.selected_region else "blue"
            
            # Draw rectangle
            self.image_canvas.create_rectangle(x1, y1, x2, y2, outline=color, width=2, tags="region")
            
            # Draw label
            self.image_canvas.create_text(x1 + 5, y1 + 5, anchor="nw", text=region.name, 
                                        fill=color, font=("Arial", 10, "bold"), tags="region")
    
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
        x1 = int(min(self.draw_start_x, canvas_x / self.canvas_scale))
        y1 = int(min(self.draw_start_y, canvas_y / self.canvas_scale))
        x2 = int(max(self.draw_start_x, canvas_x / self.canvas_scale))
        y2 = int(max(self.draw_start_y, canvas_y / self.canvas_scale))
        
        # Update region input fields
        self.region_x.set(x1)
        self.region_y.set(y1)
        self.region_w.set(x2 - x1)
        self.region_h.set(y2 - y1)
        
        # Suggest a name if empty
        if not self.region_name.get():
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
        
        output_path = filedialog.asksaveasfilename(
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
        file_path = filedialog.askopenfilename(
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
        
        output_folder = filedialog.askdirectory(title="Select output folder for extracted regions")
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
            output_path = filedialog.asksaveasfilename(
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
