"""
Fake PBR Baker Tool - Create fake PBR textures by combining base, roughness, and AO maps.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk, ImageEnhance, ImageOps, ImageChops
import os
from .base_tool import BaseTool, register_tool
from .utils import PlaceholderEntry, browse_file

@register_tool
class FakePBRBakerTool(BaseTool):
    @property
    def name(self) -> str:
        return "Fake PBR Baker"
    
    @property
    def description(self) -> str:
        return "Create fake PBR textures by combining base, roughness, and AO maps"
    
    @property
    def dependencies(self) -> list:
        return ["PIL"]
    
    def create_tab(self, parent) -> ttk.Frame:
        return FakePBRBakerTab(parent, self.config)

class FakePBRBakerTab(ttk.Frame):
    def __init__(self, parent, config):
        super().__init__(parent)
        self.config = config
        
        # Default settings
        self.defaults = {
            'blend': 35.0,
            'contrast': 200,
            'whites': 0,
            'dark': 0.0,
            'white': 0.0,
            'invert': False,
            'preview_res': 64
        }
        
        # Images and thumbnails
        self.base_image = None
        self.rough_image = None
        self.ao_image = None
        self.base_thumb = None
        self.rough_thumb = None
        self.ao_thumb = None
        self.baked_image = None
        
        self.setup_ui()
    
    def setup_ui(self):
        """Set up the user interface."""
        main_frame = ttk.Frame(self)
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)
        
        # Input section
        input_frame = ttk.LabelFrame(main_frame, text="Input Images", padding=10)
        input_frame.pack(fill="x", pady=(0, 10))
        
        # Base texture input
        ttk.Label(input_frame, text="Base Texture:").grid(row=0, column=0, sticky="w", pady=2)
        self.base_path = PlaceholderEntry(input_frame, placeholder="Select base/diffuse texture...")
        self.base_path.grid(row=0, column=1, sticky="ew", padx=(5, 0), pady=2)
        ttk.Button(input_frame, text="Browse", 
                  command=self.browse_base).grid(row=0, column=2, padx=(5, 0), pady=2)
        
        # Roughness texture input
        ttk.Label(input_frame, text="Roughness Map:").grid(row=1, column=0, sticky="w", pady=2)
        self.rough_path = PlaceholderEntry(input_frame, placeholder="Select roughness/metalness map...")
        self.rough_path.grid(row=1, column=1, sticky="ew", padx=(5, 0), pady=2)
        ttk.Button(input_frame, text="Browse", 
                  command=self.browse_rough).grid(row=1, column=2, padx=(5, 0), pady=2)
        
        # AO texture input
        ttk.Label(input_frame, text="AO Map (Optional):").grid(row=2, column=0, sticky="w", pady=2)
        self.ao_path = PlaceholderEntry(input_frame, placeholder="Select ambient occlusion map...")
        self.ao_path.grid(row=2, column=1, sticky="ew", padx=(5, 0), pady=2)
        ttk.Button(input_frame, text="Browse", 
                  command=self.browse_ao).grid(row=2, column=2, padx=(5, 0), pady=2)
        
        input_frame.columnconfigure(1, weight=1)
        
        # Preview section
        preview_frame = ttk.LabelFrame(main_frame, text="Preview", padding=10)
        preview_frame.pack(fill="both", expand=True, pady=(0, 10))
        
        preview_container = ttk.Frame(preview_frame)
        preview_container.pack(fill="both", expand=True)
        
        # Input previews
        inputs_frame = ttk.Frame(preview_container)
        inputs_frame.pack(side="left", fill="both", expand=True)
        
        # Base preview
        base_frame = ttk.Frame(inputs_frame)
        base_frame.pack(side="top", fill="both", expand=True, pady=(0, 5))
        ttk.Label(base_frame, text="Base", font=("Arial", 9, "bold")).pack()
        self.preview_base = ttk.Label(base_frame, text="Load base\\ntexture")
        self.preview_base.pack(expand=True)
        
        # Roughness preview
        rough_frame = ttk.Frame(inputs_frame)
        rough_frame.pack(side="top", fill="both", expand=True, pady=(0, 5))
        ttk.Label(rough_frame, text="Roughness", font=("Arial", 9, "bold")).pack()
        self.preview_rough = ttk.Label(rough_frame, text="Load roughness\\nmap")
        self.preview_rough.pack(expand=True)
        
        # AO preview
        ao_frame = ttk.Frame(inputs_frame)
        ao_frame.pack(side="top", fill="both", expand=True)
        ttk.Label(ao_frame, text="AO", font=("Arial", 9, "bold")).pack()
        self.preview_ao = ttk.Label(ao_frame, text="Load AO\\nmap")
        self.preview_ao.pack(expand=True)
        
        # Result preview
        result_frame = ttk.Frame(preview_container)
        result_frame.pack(side="right", fill="both", expand=True, padx=(10, 0))
        ttk.Label(result_frame, text="Baked Result", font=("Arial", 10, "bold")).pack()
        self.preview_result = ttk.Label(result_frame, text="Configure settings\\nand bake")
        self.preview_result.pack(expand=True)
        
        # Controls section
        controls_frame = ttk.LabelFrame(main_frame, text="Baking Settings", padding=10)
        controls_frame.pack(fill="x", pady=(0, 10))
        
        # First row of controls
        controls_row1 = ttk.Frame(controls_frame)
        controls_row1.pack(fill="x", pady=(0, 5))
        
        # Blend strength
        ttk.Label(controls_row1, text="Blend:").grid(row=0, column=0, sticky="w", padx=(0, 5))
        self.blend_var = tk.DoubleVar(value=self.defaults['blend'])
        self.blend_scale = ttk.Scale(controls_row1, from_=0, to=100, orient="horizontal",
                                    variable=self.blend_var, command=self.update_preview)
        self.blend_scale.grid(row=0, column=1, sticky="ew", padx=(0, 5))
        self.blend_label = ttk.Label(controls_row1, text="35.0%")
        self.blend_label.grid(row=0, column=2, padx=(0, 20))
        
        # Contrast
        ttk.Label(controls_row1, text="Contrast:").grid(row=0, column=3, sticky="w", padx=(0, 5))
        self.contrast_var = tk.IntVar(value=self.defaults['contrast'])
        self.contrast_scale = ttk.Scale(controls_row1, from_=100, to=300, orient="horizontal",
                                       variable=self.contrast_var, command=self.update_preview)
        self.contrast_scale.grid(row=0, column=4, sticky="ew", padx=(0, 5))
        self.contrast_label = ttk.Label(controls_row1, text="200%")
        self.contrast_label.grid(row=0, column=5)
        
        controls_row1.columnconfigure(1, weight=1)
        controls_row1.columnconfigure(4, weight=1)
        
        # Second row of controls
        controls_row2 = ttk.Frame(controls_frame)
        controls_row2.pack(fill="x", pady=(0, 5))
        
        # Whites adjustment
        ttk.Label(controls_row2, text="Whites:").grid(row=0, column=0, sticky="w", padx=(0, 5))
        self.whites_var = tk.IntVar(value=self.defaults['whites'])
        self.whites_scale = ttk.Scale(controls_row2, from_=-100, to=100, orient="horizontal",
                                     variable=self.whites_var, command=self.update_preview)
        self.whites_scale.grid(row=0, column=1, sticky="ew", padx=(0, 5))
        self.whites_label = ttk.Label(controls_row2, text="0")
        self.whites_label.grid(row=0, column=2, padx=(0, 20))
        
        # Dark adjustment
        ttk.Label(controls_row2, text="Dark:").grid(row=0, column=3, sticky="w", padx=(0, 5))
        self.dark_var = tk.DoubleVar(value=self.defaults['dark'])
        self.dark_scale = ttk.Scale(controls_row2, from_=-50, to=50, orient="horizontal",
                                   variable=self.dark_var, command=self.update_preview)
        self.dark_scale.grid(row=0, column=4, sticky="ew", padx=(0, 5))
        self.dark_label = ttk.Label(controls_row2, text="0.0")
        self.dark_label.grid(row=0, column=5)
        
        controls_row2.columnconfigure(1, weight=1)
        controls_row2.columnconfigure(4, weight=1)
        
        # Third row of controls
        controls_row3 = ttk.Frame(controls_frame)
        controls_row3.pack(fill="x")
        
        # White point
        ttk.Label(controls_row3, text="White Point:").grid(row=0, column=0, sticky="w", padx=(0, 5))
        self.white_var = tk.DoubleVar(value=self.defaults['white'])
        self.white_scale = ttk.Scale(controls_row3, from_=-50, to=50, orient="horizontal",
                                    variable=self.white_var, command=self.update_preview)
        self.white_scale.grid(row=0, column=1, sticky="ew", padx=(0, 5))
        self.white_label = ttk.Label(controls_row3, text="0.0")
        self.white_label.grid(row=0, column=2, padx=(0, 20))
        
        # Invert checkbox
        self.invert_var = tk.BooleanVar(value=self.defaults['invert'])
        ttk.Checkbutton(controls_row3, text="Invert Roughness", 
                       variable=self.invert_var, command=self.update_preview).grid(row=0, column=3, padx=(0, 20))
        
        # Preview resolution
        ttk.Label(controls_row3, text="Preview Size:").grid(row=0, column=4, sticky="w", padx=(0, 5))
        self.preview_res_var = tk.IntVar(value=self.defaults['preview_res'])
        res_combo = ttk.Combobox(controls_row3, textvariable=self.preview_res_var, 
                                values=[32, 64, 128, 256], state="readonly", width=8)
        res_combo.grid(row=0, column=5)
        res_combo.bind("<<ComboboxSelected>>", self.update_preview)
        
        controls_row3.columnconfigure(1, weight=1)
        
        # Action buttons
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill="x", pady=(0, 10))
        
        ttk.Button(button_frame, text="Reset to Defaults", 
                  command=self.reset_settings).pack(side="left")
        ttk.Button(button_frame, text="Bake Full Resolution", 
                  command=self.bake_full).pack(side="left", padx=(10, 0))
        ttk.Button(button_frame, text="Save Result", 
                  command=self.save_result).pack(side="left", padx=(10, 0))
        ttk.Button(button_frame, text="Batch Process Folder", 
                  command=self.batch_process).pack(side="right")
        
        # Status label
        self.status_label = ttk.Label(main_frame, text="Ready", foreground="green")
        self.status_label.pack(pady=(10, 0))
    
    def browse_base(self):
        """Browse for base texture file."""
        path = browse_file(
            title="Select Base Texture",
            filetypes=[("Image Files", "*.png *.jpg *.jpeg *.tga *.bmp")]
        )
        if path:
            self.base_path.set_text(path)
            self.load_base_image()
    
    def browse_rough(self):
        """Browse for roughness texture file."""
        path = browse_file(
            title="Select Roughness Map",
            filetypes=[("Image Files", "*.png *.jpg *.jpeg *.tga *.bmp")]
        )
        if path:
            self.rough_path.set_text(path)
            self.load_rough_image()
    
    def browse_ao(self):
        """Browse for AO texture file."""
        path = browse_file(
            title="Select AO Map",
            filetypes=[("Image Files", "*.png *.jpg *.jpeg *.tga *.bmp")]
        )
        if path:
            self.ao_path.set_text(path)
            self.load_ao_image()
    
    def load_base_image(self):
        """Load the base texture image."""
        path = self.base_path.get()
        if not path or not os.path.exists(path):
            return
        
        try:
            self.base_image = Image.open(path).convert("RGB")
            self.update_base_preview()
            self.update_preview()
            self.status_label.config(text="Base texture loaded", foreground="green")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load base texture: {e}")
            self.status_label.config(text="Error loading base texture", foreground="red")
    
    def load_rough_image(self):
        """Load the roughness texture image."""
        path = self.rough_path.get()
        if not path or not os.path.exists(path):
            return
        
        try:
            self.rough_image = Image.open(path).convert("L")  # Convert to grayscale
            self.update_rough_preview()
            self.update_preview()
            self.status_label.config(text="Roughness map loaded", foreground="green")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load roughness map: {e}")
            self.status_label.config(text="Error loading roughness map", foreground="red")
    
    def load_ao_image(self):
        """Load the AO texture image."""
        path = self.ao_path.get()
        if not path or not os.path.exists(path):
            return
        
        try:
            self.ao_image = Image.open(path).convert("L")  # Convert to grayscale
            self.update_ao_preview()
            self.update_preview()
            self.status_label.config(text="AO map loaded", foreground="green")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load AO map: {e}")
            self.status_label.config(text="Error loading AO map", foreground="red")
    
    def update_base_preview(self):
        """Update the base texture preview."""
        if not self.base_image:
            return
        
        preview_size = (80, 80)
        self.base_thumb = self.base_image.copy()
        self.base_thumb.thumbnail(preview_size)
        
        photo = ImageTk.PhotoImage(self.base_thumb)
        self.preview_base.config(image=photo, text="")
        self.preview_base.image = photo  # Keep a reference
    
    def update_rough_preview(self):
        """Update the roughness map preview."""
        if not self.rough_image:
            return
        
        preview_size = (80, 80)
        rough_thumb = self.rough_image.copy()
        rough_thumb.thumbnail(preview_size)
        
        photo = ImageTk.PhotoImage(rough_thumb)
        self.preview_rough.config(image=photo, text="")
        self.preview_rough.image = photo  # Keep a reference
    
    def update_ao_preview(self):
        """Update the AO map preview."""
        if not self.ao_image:
            return
        
        preview_size = (80, 80)
        ao_thumb = self.ao_image.copy()
        ao_thumb.thumbnail(preview_size)
        
        photo = ImageTk.PhotoImage(ao_thumb)
        self.preview_ao.config(image=photo, text="")
        self.preview_ao.image = photo  # Keep a reference
    
    def update_preview(self, value=None):
        """Update all control labels and result preview."""
        # Update labels
        self.blend_label.config(text=f"{self.blend_var.get():.1f}%")
        self.contrast_label.config(text=f"{self.contrast_var.get()}%")
        self.whites_label.config(text=str(self.whites_var.get()))
        self.dark_label.config(text=f"{self.dark_var.get():.1f}")
        self.white_label.config(text=f"{self.white_var.get():.1f}")
        
        # Update result preview
        if self.base_image and self.rough_image:
            self.bake_preview()
    
    def bake_preview(self):
        """Create a preview of the baked result."""
        try:
            preview_res = self.preview_res_var.get()
            result = self.bake_textures(preview_resolution=preview_res)
            
            if result:
                photo = ImageTk.PhotoImage(result)
                self.preview_result.config(image=photo, text="")
                self.preview_result.image = photo  # Keep a reference
                
        except Exception as e:
            self.status_label.config(text=f"Preview error: {e}", foreground="red")
    
    def bake_textures(self, preview_resolution=None):
        """Bake the textures with current settings."""
        if not self.base_image or not self.rough_image:
            return None
        
        try:
            # Use preview resolution if specified, otherwise use full resolution
            if preview_resolution:
                base = self.base_image.copy()
                base.thumbnail((preview_resolution, preview_resolution))
                rough = self.rough_image.copy()
                rough = rough.resize(base.size, Image.Resampling.LANCZOS)
                if self.ao_image:
                    ao = self.ao_image.copy()
                    ao = ao.resize(base.size, Image.Resampling.LANCZOS)
                else:
                    ao = None
            else:
                base = self.base_image.copy()
                rough = self.rough_image.resize(base.size, Image.Resampling.LANCZOS)
                if self.ao_image:
                    ao = self.ao_image.resize(base.size, Image.Resampling.LANCZOS)
                else:
                    ao = None
            
            # Apply invert to roughness if needed
            if self.invert_var.get():
                rough = ImageOps.invert(rough)
            
            # Apply contrast to roughness
            contrast_factor = self.contrast_var.get() / 100.0
            if contrast_factor != 1.0:
                enhancer = ImageEnhance.Contrast(rough)
                rough = enhancer.enhance(contrast_factor)
            
            # Apply whites adjustment to roughness
            whites_adj = self.whites_var.get()
            if whites_adj != 0:
                rough_array = list(rough.getdata())
                rough_array = [min(255, max(0, pixel + whites_adj)) for pixel in rough_array]
                rough.putdata(rough_array)
            
            # Convert roughness to RGB for blending
            rough_rgb = Image.merge("RGB", (rough, rough, rough))
            
            # Apply blend
            blend_factor = self.blend_var.get() / 100.0
            result = Image.blend(base, rough_rgb, blend_factor)
            
            # Apply AO if available
            if ao:
                # Apply dark adjustment to AO
                dark_adj = self.dark_var.get()
                if dark_adj != 0:
                    ao_array = list(ao.getdata())
                    ao_array = [min(255, max(0, pixel + dark_adj)) for pixel in ao_array]
                    ao.putdata(ao_array)
                
                # Apply white point adjustment
                white_adj = self.white_var.get()
                if white_adj != 0:
                    ao_array = list(ao.getdata())
                    ao_array = [min(255, max(0, pixel + white_adj)) for pixel in ao_array]
                    ao.putdata(ao_array)
                
                # Multiply blend AO with result
                ao_rgb = Image.merge("RGB", (ao, ao, ao))
                result = ImageChops.multiply(result, ao_rgb)
                
                # Normalize the result
                result = ImageEnhance.Brightness(result).enhance(1.2)
            
            self.baked_image = result
            return result
            
        except Exception as e:
            self.status_label.config(text=f"Baking error: {e}", foreground="red")
            return None
    
    def reset_settings(self):
        """Reset all settings to defaults."""
        self.blend_var.set(self.defaults['blend'])
        self.contrast_var.set(self.defaults['contrast'])
        self.whites_var.set(self.defaults['whites'])
        self.dark_var.set(self.defaults['dark'])
        self.white_var.set(self.defaults['white'])
        self.invert_var.set(self.defaults['invert'])
        self.preview_res_var.set(self.defaults['preview_res'])
        self.update_preview()
        self.status_label.config(text="Settings reset to defaults", foreground="green")
    
    def bake_full(self):
        """Bake at full resolution."""
        if not self.base_image or not self.rough_image:
            messagebox.showerror("Error", "Please load base and roughness textures first.")
            return
        
        self.status_label.config(text="Baking full resolution...", foreground="blue")
        self.update()  # Update UI
        
        result = self.bake_textures()
        if result:
            self.baked_image = result
            # Update preview with downscaled version
            preview_img = result.copy()
            preview_img.thumbnail((200, 200))
            photo = ImageTk.PhotoImage(preview_img)
            self.preview_result.config(image=photo, text="")
            self.preview_result.image = photo
            
            self.status_label.config(text="Full resolution baking complete", foreground="green")
        else:
            self.status_label.config(text="Baking failed", foreground="red")
    
    def save_result(self):
        """Save the baked result."""
        if not self.baked_image:
            messagebox.showerror("Error", "No baked image to save. Please bake first.")
            return
        
        output_path = filedialog.asksaveasfilename(
            title="Save Baked Result",
            defaultextension=".png",
            filetypes=[("PNG Files", "*.png"), ("JPEG Files", "*.jpg"), ("TGA Files", "*.tga")]
        )
        
        if output_path:
            try:
                # Convert to RGB if saving as JPEG
                if output_path.lower().endswith(('.jpg', '.jpeg')):
                    save_image = self.baked_image.convert("RGB")
                else:
                    save_image = self.baked_image
                
                save_image.save(output_path)
                self.status_label.config(text=f"Saved: {os.path.basename(output_path)}", foreground="green")
                
            except Exception as e:
                messagebox.showerror("Error", f"Failed to save image: {e}")
                self.status_label.config(text="Error saving image", foreground="red")
    
    def batch_process(self):
        """Batch process a folder of textures."""
        if not self.rough_image:
            messagebox.showerror("Error", "Please load a roughness map template first.")
            return
        
        input_folder = filedialog.askdirectory(title="Select folder with base textures")
        if not input_folder:
            return
        
        output_folder = filedialog.askdirectory(title="Select output folder")
        if not output_folder:
            return
        
        # Process all images in the folder
        processed = 0
        errors = 0
        
        for filename in os.listdir(input_folder):
            if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.tga', '.bmp')):
                input_path = os.path.join(input_folder, filename)
                output_name = f"baked_{filename}"
                output_path = os.path.join(output_folder, output_name)
                
                try:
                    # Load base image
                    base_img = Image.open(input_path).convert("RGB")
                    
                    # Temporarily set as current base
                    old_base = self.base_image
                    self.base_image = base_img
                    
                    # Bake result
                    result = self.bake_textures()
                    
                    if result:
                        # Save result
                        if output_path.lower().endswith(('.jpg', '.jpeg')):
                            save_img = result.convert("RGB")
                        else:
                            save_img = result
                        
                        save_img.save(output_path)
                        processed += 1
                    else:
                        errors += 1
                    
                    # Restore original base
                    self.base_image = old_base
                    
                except Exception as e:
                    print(f"Error processing {filename}: {e}")
                    errors += 1
        
        messagebox.showinfo("Batch Complete", 
                           f"Processed {processed} images.\n{errors} errors occurred.")
        self.status_label.config(text=f"Batch complete: {processed} processed, {errors} errors", 
                                foreground="green" if errors == 0 else "orange")
