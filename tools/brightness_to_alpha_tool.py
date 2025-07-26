"""
Brightness to Alpha Tool - Convert image brightness to alpha channel.
"""

import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk

from .base_tool import BaseTool, register_tool
from .utils import save_config


def convert_brightness_to_alpha(image_path, output_path, threshold=200, invert=False):
    """
    Convert image brightness to alpha channel.
    
    Args:
        image_path: Path to input image
        output_path: Path for output image
        threshold: Brightness threshold (0-255)
        invert: Whether to invert the alpha logic
    """
    try:
        # Load image and convert to RGBA
        image = Image.open(image_path).convert("RGBA")
        
        # Get grayscale version for brightness calculation
        grayscale = image.convert("L")
        
        # Create new image with alpha based on brightness
        result = Image.new("RGBA", image.size)
        
        # Process pixels
        for y in range(image.height):
            for x in range(image.width):
                r, g, b, a = image.getpixel((x, y))
                brightness = grayscale.getpixel((x, y))
                
                # Calculate alpha based on brightness and threshold
                if invert:
                    alpha = 255 if brightness < threshold else 0
                else:
                    alpha = 255 if brightness >= threshold else 0
                
                result.putpixel((x, y), (r, g, b, alpha))
        
        # Save result
        result.save(output_path)
        return True
        
    except Exception as e:
        print(f"Error converting brightness to alpha: {e}")
        return False


class BrightnessToAlphaTab(ttk.Frame):
    def __init__(self, parent, config):
        super().__init__(parent)
        self.config = config
        
        self.image_path = ""
        self.current_image = None
        
        # File selection
        ttk.Label(self, text="Input Image:").grid(row=0, column=0, sticky="e", padx=5, pady=5)
        self.image_var = tk.StringVar(value=config.get("brightness_alpha_input", ""))
        ttk.Entry(self, textvariable=self.image_var, width=50).grid(row=0, column=1, padx=5, pady=5, sticky="ew")
        ttk.Button(self, text="Browse...", command=self.browse_image).grid(row=0, column=2, padx=5, pady=5)
        
        # Preview frame
        preview_frame = ttk.LabelFrame(self, text="Preview")
        preview_frame.grid(row=1, column=0, columnspan=3, padx=5, pady=5, sticky="nsew")
        
        self.preview_label = ttk.Label(preview_frame, text="No image loaded", anchor="center")
        self.preview_label.pack(expand=True, fill="both", padx=10, pady=10)
        
        # Controls frame
        controls_frame = ttk.LabelFrame(self, text="Settings")
        controls_frame.grid(row=2, column=0, columnspan=3, padx=5, pady=5, sticky="ew")
        
        # Brightness threshold
        ttk.Label(controls_frame, text="Brightness Threshold:").grid(row=0, column=0, sticky="e", padx=5, pady=5)
        self.threshold_var = tk.IntVar(value=config.get("brightness_alpha_threshold", 200))
        threshold_scale = ttk.Scale(controls_frame, from_=0, to=255, orient=tk.HORIZONTAL, 
                                  variable=self.threshold_var, command=self.update_preview)
        threshold_scale.grid(row=0, column=1, padx=5, pady=5, sticky="ew")
        
        self.threshold_label = ttk.Label(controls_frame, text=str(self.threshold_var.get()))
        self.threshold_label.grid(row=0, column=2, padx=5, pady=5)
        
        # Invert checkbox
        self.invert_var = tk.BooleanVar(value=config.get("brightness_alpha_invert", False))
        ttk.Checkbutton(controls_frame, text="Invert Alpha Logic", 
                       variable=self.invert_var, command=self.update_preview).grid(
                           row=1, column=0, columnspan=3, padx=5, pady=5, sticky="w")
        
        # Output settings
        ttk.Label(controls_frame, text="Output File:").grid(row=2, column=0, sticky="e", padx=5, pady=5)
        self.output_var = tk.StringVar(value=config.get("brightness_alpha_output", ""))
        ttk.Entry(controls_frame, textvariable=self.output_var, width=40).grid(row=2, column=1, padx=5, pady=5, sticky="ew")
        ttk.Button(controls_frame, text="Browse...", command=self.browse_output).grid(row=2, column=2, padx=5, pady=5)
        
        # Process button
        ttk.Button(controls_frame, text="Convert to Alpha", command=self.on_convert).grid(
            row=3, column=0, columnspan=3, pady=10)
        
        # Configure grid weights
        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)
        controls_frame.columnconfigure(1, weight=1)
        
        # Update threshold label when scale changes
        self.threshold_var.trace('w', self.update_threshold_label)
        
        # Load image if path is set
        if self.image_var.get():
            self.load_image(self.image_var.get())
    
    def update_threshold_label(self, *args):
        """Update the threshold value label."""
        self.threshold_label.config(text=str(self.threshold_var.get()))
    
    def browse_image(self):
        path = filedialog.askopenfilename(
            filetypes=[("Image Files", "*.png *.jpg *.jpeg *.tga *.bmp"), ("All Files", "*.*")]
        )
        if path:
            self.image_var.set(path)
            self.load_image(path)
    
    def browse_output(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".png",
            filetypes=[("PNG Files", "*.png"), ("TGA Files", "*.tga"), ("All Files", "*.*")]
        )
        if path:
            self.output_var.set(path)
    
    def load_image(self, path):
        """Load and display the image."""
        try:
            self.image_path = path
            self.current_image = Image.open(path)
            self.update_preview()
            
            # Set default output path
            if not self.output_var.get():
                base_name = os.path.splitext(path)[0]
                self.output_var.set(f"{base_name}_alpha.png")
                
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load image: {str(e)}")
    
    def update_preview(self, *args):
        """Update the preview image."""
        if not self.current_image:
            return
            
        try:
            # Create preview with current settings
            threshold = self.threshold_var.get()
            invert = self.invert_var.get()
            
            # Convert to RGBA and process
            image = self.current_image.convert("RGBA")
            grayscale = image.convert("L")
            
            # Create preview (smaller version for performance)
            preview_size = (300, 300)
            preview_img = image.copy()
            preview_img.thumbnail(preview_size, Image.LANCZOS)
            preview_gray = grayscale.copy()
            preview_gray.thumbnail(preview_size, Image.LANCZOS)
            
            # Apply brightness to alpha conversion
            result = Image.new("RGBA", preview_img.size)
            for y in range(preview_img.height):
                for x in range(preview_img.width):
                    r, g, b, a = preview_img.getpixel((x, y))
                    brightness = preview_gray.getpixel((x, y))
                    
                    if invert:
                        alpha = 255 if brightness < threshold else 0
                    else:
                        alpha = 255 if brightness >= threshold else 0
                    
                    result.putpixel((x, y), (r, g, b, alpha))
            
            # Convert to PhotoImage and display
            photo = ImageTk.PhotoImage(result)
            self.preview_label.config(image=photo, text="")
            self.preview_label.image = photo  # Keep reference
            
        except Exception as e:
            print(f"Error updating preview: {e}")
    
    def on_convert(self):
        """Convert the image using current settings."""
        if not self.current_image:
            messagebox.showerror("Error", "Please load an image first.")
            return
            
        output_path = self.output_var.get()
        if not output_path:
            messagebox.showerror("Error", "Please specify an output file.")
            return
        
        # Save settings
        self.config["brightness_alpha_input"] = self.image_var.get()
        self.config["brightness_alpha_output"] = output_path
        self.config["brightness_alpha_threshold"] = self.threshold_var.get()
        self.config["brightness_alpha_invert"] = self.invert_var.get()
        save_config(self.config)
        
        # Convert image
        threshold = self.threshold_var.get()
        invert = self.invert_var.get()
        
        if convert_brightness_to_alpha(self.image_path, output_path, threshold, invert):
            messagebox.showinfo("Success", f"Image converted successfully!\nSaved to: {output_path}")
        else:
            messagebox.showerror("Error", "Failed to convert image.")


@register_tool
class BrightnessToAlphaTool(BaseTool):
    @property
    def name(self) -> str:
        return "Brightness → Alpha"
    
    @property
    def description(self) -> str:
        return "Convert image brightness to alpha channel"
    
    @property
    def dependencies(self) -> list:
        return ["PIL"]
    
    def create_tab(self, parent) -> ttk.Frame:
        return BrightnessToAlphaTab(parent, self.config)
