"""
AO Baker Tool - Bakes ambient occlusion into base textures.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk, ImageChops
import os
from .base_tool import BaseTool, register_tool
from .utils import PlaceholderEntry, browse_file

@register_tool
class AOBakerTool(BaseTool):
    @property
    def name(self) -> str:
        return "AO Baker"
    
    @property
    def description(self) -> str:
        return "Bake ambient occlusion maps into base textures with adjustable strength"
    
    @property
    def dependencies(self) -> list:
        return ["PIL"]
    
    def create_tab(self, parent) -> ttk.Frame:
        return AOBakerTab(parent, self.config)

class AOBakerTab(ttk.Frame):
    def __init__(self, parent, config):
        super().__init__(parent)
        self.config = config
        
        # Initialize image variables
        self.base_image = None
        self.ao_image = None
        self.output_image = None
        
        self.setup_ui()
    
    def setup_ui(self):
        """Set up the user interface."""
        main_frame = ttk.Frame(self)
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)
        
        # Input section
        input_frame = ttk.LabelFrame(main_frame, text="Input Files", padding=10)
        input_frame.pack(fill="x", pady=(0, 10))
        
        # Base texture input
        ttk.Label(input_frame, text="Base Texture:").grid(row=0, column=0, sticky="w", pady=2)
        self.base_path = PlaceholderEntry(input_frame, placeholder="Select base texture image...")
        self.base_path.grid(row=0, column=1, sticky="ew", padx=(5, 0), pady=2)
        ttk.Button(input_frame, text="Browse", 
                  command=lambda: self.browse_base()).grid(row=0, column=2, padx=(5, 0), pady=2)
        
        # AO texture input
        ttk.Label(input_frame, text="AO Texture:").grid(row=1, column=0, sticky="w", pady=2)
        self.ao_path = PlaceholderEntry(input_frame, placeholder="Select ambient occlusion image...")
        self.ao_path.grid(row=1, column=1, sticky="ew", padx=(5, 0), pady=2)
        ttk.Button(input_frame, text="Browse", 
                  command=lambda: self.browse_ao()).grid(row=1, column=2, padx=(5, 0), pady=2)
        
        input_frame.columnconfigure(1, weight=1)
        
        # Preview section
        preview_frame = ttk.LabelFrame(main_frame, text="Preview", padding=10)
        preview_frame.pack(fill="both", expand=True, pady=(0, 10))
        
        preview_container = ttk.Frame(preview_frame)
        preview_container.pack(fill="both", expand=True)
        
        # Before/After previews
        before_frame = ttk.Frame(preview_container)
        before_frame.pack(side="left", fill="both", expand=True, padx=(0, 5))
        ttk.Label(before_frame, text="Before", font=("Arial", 10, "bold")).pack()
        self.preview_before = ttk.Label(before_frame, text="Load base texture\nto see preview")
        self.preview_before.pack(expand=True)
        
        after_frame = ttk.Frame(preview_container)
        after_frame.pack(side="right", fill="both", expand=True, padx=(5, 0))
        ttk.Label(after_frame, text="After", font=("Arial", 10, "bold")).pack()
        self.preview_after = ttk.Label(after_frame, text="Load AO texture\nto see preview")
        self.preview_after.pack(expand=True)
        
        # Controls section
        controls_frame = ttk.LabelFrame(main_frame, text="Controls", padding=10)
        controls_frame.pack(fill="x", pady=(0, 10))
        
        # AO Strength slider
        ttk.Label(controls_frame, text="AO Strength:").grid(row=0, column=0, sticky="w", pady=2)
        self.ao_strength = tk.IntVar(value=50)
        self.ao_slider = ttk.Scale(controls_frame, from_=0, to=100, orient="horizontal",
                                  variable=self.ao_strength, command=self.update_preview)
        self.ao_slider.grid(row=0, column=1, sticky="ew", padx=(5, 0), pady=2)
        self.strength_label = ttk.Label(controls_frame, text="50%")
        self.strength_label.grid(row=0, column=2, padx=(5, 0), pady=2)
        
        controls_frame.columnconfigure(1, weight=1)
        
        # Output section
        output_frame = ttk.LabelFrame(main_frame, text="Output", padding=10)
        output_frame.pack(fill="x")
        
        ttk.Button(output_frame, text="Save Result", 
                  command=self.save_output).pack(side="left")
        ttk.Button(output_frame, text="Batch Process Folder", 
                  command=self.batch_process).pack(side="left", padx=(10, 0))
        
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
    
    def browse_ao(self):
        """Browse for AO texture file."""
        path = browse_file(
            title="Select AO Texture",
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
            self.base_image = Image.open(path).convert("RGBA")
            self.update_previews()
            self.status_label.config(text="Base texture loaded", foreground="green")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load base texture: {e}")
            self.status_label.config(text="Error loading base texture", foreground="red")
    
    def load_ao_image(self):
        """Load the AO texture image."""
        path = self.ao_path.get()
        if not path or not os.path.exists(path):
            return
        
        try:
            self.ao_image = Image.open(path).convert("L")  # Convert to grayscale
            self.update_previews()
            self.status_label.config(text="AO texture loaded", foreground="green")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load AO texture: {e}")
            self.status_label.config(text="Error loading AO texture", foreground="red")
    
    def update_preview(self, value=None):
        """Update the strength label and preview."""
        strength = int(self.ao_strength.get())
        self.strength_label.config(text=f"{strength}%")
        self.update_previews()
    
    def update_previews(self):
        """Update both preview images."""
        if not self.base_image:
            return
        
        # Update before preview
        before_thumb = self.base_image.copy()
        before_thumb.thumbnail((200, 200))
        before_photo = ImageTk.PhotoImage(before_thumb)
        self.preview_before.config(image=before_photo, text="")
        self.preview_before.image = before_photo  # Keep a reference
        
        # Update after preview if AO is loaded
        if self.ao_image:
            after_thumb = self.bake_ao()
            if after_thumb:
                after_thumb.thumbnail((200, 200))
                after_photo = ImageTk.PhotoImage(after_thumb)
                self.preview_after.config(image=after_photo, text="")
                self.preview_after.image = after_photo  # Keep a reference
    
    def bake_ao(self):
        """Bake AO into the base texture."""
        if not self.base_image or not self.ao_image:
            return None
        
        try:
            # Resize AO to match base if needed
            ao_resized = self.ao_image.resize(self.base_image.size, Image.Resampling.LANCZOS)
            
            # Convert AO to RGBA
            ao_rgba = Image.new("RGBA", ao_resized.size, (255, 255, 255, 255))
            ao_rgba.paste(ao_resized, (0, 0))
            
            # Apply AO strength
            strength = self.ao_strength.get() / 100.0
            
            # Multiply blend the AO with the base
            result = Image.new("RGBA", self.base_image.size)
            
            for x in range(self.base_image.width):
                for y in range(self.base_image.height):
                    base_pixel = self.base_image.getpixel((x, y))
                    ao_pixel = ao_rgba.getpixel((x, y))
                    
                    # Normalize AO value (0-255 to 0-1)
                    ao_factor = ao_pixel[0] / 255.0
                    
                    # Apply strength
                    ao_factor = 1.0 - ((1.0 - ao_factor) * strength)
                    
                    # Multiply base color by AO factor
                    new_r = int(base_pixel[0] * ao_factor)
                    new_g = int(base_pixel[1] * ao_factor)
                    new_b = int(base_pixel[2] * ao_factor)
                    
                    result.putpixel((x, y), (new_r, new_g, new_b, base_pixel[3]))
            
            self.output_image = result
            return result
            
        except Exception as e:
            self.status_label.config(text=f"Error baking AO: {e}", foreground="red")
            return None
    
    def save_output(self):
        """Save the baked result."""
        if not self.output_image:
            if not self.bake_ao():
                messagebox.showerror("Error", "No image to save. Please load both base and AO textures.")
                return
        
        output_path = filedialog.asksaveasfilename(
            title="Save Baked Result",
            defaultextension=".png",
            filetypes=[("PNG Files", "*.png"), ("JPEG Files", "*.jpg"), ("TGA Files", "*.tga")]
        )
        
        if output_path:
            try:
                # Convert to RGB if saving as JPEG
                if output_path.lower().endswith('.jpg') or output_path.lower().endswith('.jpeg'):
                    save_image = Image.new("RGB", self.output_image.size, (255, 255, 255))
                    save_image.paste(self.output_image, mask=self.output_image.split()[-1])
                else:
                    save_image = self.output_image
                
                save_image.save(output_path)
                self.status_label.config(text=f"Saved: {os.path.basename(output_path)}", foreground="green")
                
            except Exception as e:
                messagebox.showerror("Error", f"Failed to save image: {e}")
                self.status_label.config(text="Error saving image", foreground="red")
    
    def batch_process(self):
        """Batch process a folder of images."""
        if not self.ao_image:
            messagebox.showerror("Error", "Please load an AO texture first.")
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
                output_path = os.path.join(output_folder, f"baked_{filename}")
                
                try:
                    # Load base image
                    base_img = Image.open(input_path).convert("RGBA")
                    
                    # Temporarily set as current base
                    old_base = self.base_image
                    self.base_image = base_img
                    
                    # Bake AO
                    result = self.bake_ao()
                    
                    if result:
                        # Save result
                        if output_path.lower().endswith(('.jpg', '.jpeg')):
                            save_img = Image.new("RGB", result.size, (255, 255, 255))
                            save_img.paste(result, mask=result.split()[-1])
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
