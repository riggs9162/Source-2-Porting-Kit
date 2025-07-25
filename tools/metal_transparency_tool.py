"""
Metal Transparency Tool - Apply transparency effects based on metal masks.
"""

import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk

from .base_tool import BaseTool, register_tool
from .utils import save_config


def apply_metal_transparency(base_image_path, mask_image_path, output_path, transparency_factor):
    """
    Applies transparency to a base image based on the intensity in a metal mask image.
    
    Args:
        base_image_path: Path to the base texture image
        mask_image_path: Path to the metal mask image
        output_path: Path where the output image will be saved
        transparency_factor: Float between 0.0 and 1.0 determining transparency level
    """
    try:
        # Load images
        base_image = Image.open(base_image_path).convert("RGBA")
        mask_image = Image.open(mask_image_path).convert("L")
        
        # Check image size to prevent memory issues
        width, height = base_image.size
        total_pixels = width * height
        
        if total_pixels > 50_000_000:  # Skip very large images (50 megapixels)
            messagebox.showerror("Error", f"Image too large ({width}x{height}). Please use smaller images.")
            return False
        
        # Resize mask to match base if needed
        if base_image.size != mask_image.size:
            mask_image = mask_image.resize(base_image.size, Image.LANCZOS)
            
        # Create output image
        output = Image.new("RGBA", base_image.size)
        
        # Apply mask to alpha channel based on transparency factor
        pixels = base_image.load()
        mask_pixels = mask_image.load()
        out_pixels = output.load()
        
        for y in range(base_image.height):
            for x in range(base_image.width):
                try:
                    r, g, b, a = pixels[x, y]
                    mask_value = mask_pixels[x, y] / 255.0
                    
                    # Adjust alpha based on mask value and transparency factor
                    new_alpha = int(a * (1.0 - mask_value * transparency_factor))
                    out_pixels[x, y] = (r, g, b, new_alpha)
                except (IndexError, ValueError):
                    # Skip individual pixel errors
                    continue
        
        # Save output image
        output.save(output_path)
        return True
        
    except MemoryError:
        messagebox.showerror("Error", "Memory error: Image too large to process.")
        return False
    except Exception as e:
        messagebox.showerror("Error", f"Failed to process images: {str(e)}")
        return False


class MetalTransparencyTab(ttk.Frame):
    def __init__(self, parent, config):
        super().__init__(parent)
        self.config = config
        
        self.base_image_path = ""
        self.mask_image_path = ""
        
        # Top frame for preview
        preview_frame = ttk.Frame(self)
        preview_frame.pack(fill="both", expand=True, padx=10, pady=10)
        
        # Preview labels
        self.preview_before = ttk.Label(preview_frame, text="Base Image", border=1, relief="solid")
        self.preview_before.grid(row=0, column=0, padx=10, pady=5)
        
        self.preview_mask = ttk.Label(preview_frame, text="Metal Mask", border=1, relief="solid")
        self.preview_mask.grid(row=0, column=1, padx=10, pady=5)
        
        self.preview_after = ttk.Label(preview_frame, text="Result Preview", border=1, relief="solid")
        self.preview_after.grid(row=0, column=2, padx=10, pady=5)
        
        # Controls frame
        controls_frame = ttk.Frame(self)
        controls_frame.pack(fill="x", padx=10, pady=5)
        
        # Load buttons
        ttk.Button(controls_frame, text="Load Base Texture", command=self.load_base).grid(row=0, column=0, padx=5, pady=5)
        ttk.Button(controls_frame, text="Load Metal Mask", command=self.load_mask).grid(row=0, column=1, padx=5, pady=5)
        
        # Transparency slider
        ttk.Label(controls_frame, text="Metal Transparency (%):").grid(row=1, column=0, padx=5, pady=5, sticky="e")
        self.trans_slider = ttk.Scale(controls_frame, from_=0, to=100, orient=tk.HORIZONTAL, length=300,
                                     command=lambda v: self.update_preview())
        self.trans_slider.set(50)  # Default to 50%
        self.trans_slider.grid(row=1, column=1, padx=5, pady=5, sticky="w")
        
        # Output path
        ttk.Label(controls_frame, text="Output File:").grid(row=2, column=0, padx=5, pady=5, sticky="e")
        self.output_path_var = tk.StringVar()
        ttk.Entry(controls_frame, textvariable=self.output_path_var, width=40).grid(row=2, column=1, padx=5, pady=5, sticky="w")
        ttk.Button(controls_frame, text="Browse...", command=self.browse_output).grid(row=2, column=2, padx=5, pady=5)
        
        # Save button
        ttk.Button(controls_frame, text="Save Output", command=self.save_output).grid(row=3, column=0, columnspan=3, pady=10)
        
        # Log area
        ttk.Label(self, text="Log:").pack(anchor="w", padx=10)
        self.log_text = tk.Text(self, height=6, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
    
    def log(self, message):
        """Add a message to the log."""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)
    
    def load_base(self):
        path = filedialog.askopenfilename(filetypes=[("Image Files", "*.png *.jpg *.jpeg *.tga")])
        if not path:
            return
            
        self.base_image_path = path
        try:
            # Show preview
            img = Image.open(path)
            img = img.resize((200, 200), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self.preview_before.config(image=photo, text="")
            self.preview_before.image = photo  # Keep reference
            
            # Set default output path
            base_name = os.path.splitext(path)[0]
            self.output_path_var.set(f"{base_name}_transparent.png")
            
            self.log(f"Loaded base texture: {os.path.basename(path)}")
            
        except Exception as e:
            self.log(f"Error loading base texture: {str(e)}")
    
    def load_mask(self):
        path = filedialog.askopenfilename(filetypes=[("Image Files", "*.png *.jpg *.jpeg *.tga")])
        if not path:
            return
            
        self.mask_image_path = path
        try:
            # Show preview
            img = Image.open(path)
            img = img.resize((200, 200), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self.preview_mask.config(image=photo, text="")
            self.preview_mask.image = photo  # Keep reference
            
            self.log(f"Loaded metal mask: {os.path.basename(path)}")
            
            # Update preview if base image is loaded too
            if self.base_image_path:
                self.update_preview()
                
        except Exception as e:
            self.log(f"Error loading metal mask: {str(e)}")
    
    def browse_output(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".png",
            filetypes=[("PNG files", "*.png"), ("All files", "*.*")]
        )
        if path:
            self.output_path_var.set(path)
    
    def update_preview(self):
        if not self.base_image_path or not self.mask_image_path:
            return
            
        try:
            # Generate a preview with current transparency setting
            transparency = self.trans_slider.get() / 100.0
            
            base_img = Image.open(self.base_image_path).convert("RGBA")
            mask_img = Image.open(self.mask_image_path).convert("L")
            
            # Resize mask to match base if needed
            if base_img.size != mask_img.size:
                mask_img = mask_img.resize(base_img.size, Image.LANCZOS)
                
            # Create small preview
            preview_size = (200, 200)
            base_preview = base_img.resize(preview_size, Image.LANCZOS)
            mask_preview = mask_img.resize(preview_size, Image.LANCZOS)
            
            result = Image.new("RGBA", preview_size)
            base_pixels = base_preview.load()
            mask_pixels = mask_preview.load()
            result_pixels = result.load()
            
            for y in range(preview_size[1]):
                for x in range(preview_size[0]):
                    r, g, b, a = base_pixels[x, y]
                    mask_value = mask_pixels[x, y] / 255.0
                    new_alpha = int(a * (1.0 - mask_value * transparency))
                    result_pixels[x, y] = (r, g, b, new_alpha)
            
            # Display preview
            photo = ImageTk.PhotoImage(result)
            self.preview_after.config(image=photo, text="")
            self.preview_after.image = photo
            
        except Exception as e:
            self.log(f"Preview error: {str(e)}")
    
    def save_output(self):
        if not self.base_image_path or not self.mask_image_path:
            messagebox.showerror("Error", "You must load both a base image and a mask image first.")
            return
            
        output_path = self.output_path_var.get()
        if not output_path:
            output_path = filedialog.asksaveasfilename(
                defaultextension=".png",
                filetypes=[("PNG files", "*.png"), ("All files", "*.*")]
            )
            if not output_path:
                return
                
        transparency = self.trans_slider.get() / 100.0
        if apply_metal_transparency(self.base_image_path, self.mask_image_path, output_path, transparency):
            self.log(f"Output saved to: {os.path.basename(output_path)}")
            messagebox.showinfo("Success", f"Output saved to: {output_path}")


@register_tool
class MetalTransparencyTool(BaseTool):
    @property
    def name(self) -> str:
        return "Metal Transparency"
    
    @property
    def description(self) -> str:
        return "Apply transparency effects based on metal masks"
    
    @property
    def dependencies(self) -> list:
        return ["PIL"]
    
    def create_tab(self, parent) -> ttk.Frame:
        return MetalTransparencyTab(parent, self.config)
