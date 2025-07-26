"""
Color Transparency Tool - Make specific colors transparent in images.
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk
import os
import math
from .base_tool import BaseTool, register_tool
from .utils import PlaceholderEntry, browse_file

@register_tool
class ColorTransparencyTool(BaseTool):
    @property
    def name(self) -> str:
        return "Color Transparency"

    @property
    def description(self) -> str:
        return "Make specific colors transparent in images with tolerance control"

    @property
    def dependencies(self) -> list:
        return ["PIL"]

    def create_tab(self, parent) -> ttk.Frame:
        return ColorTransparencyTab(parent, self.config)

class ColorTransparencyTab(ttk.Frame):
    def __init__(self, parent, config):
        super().__init__(parent)
        self.config = config

        # Initialize variables
        self.base_image = None
        self.base_thumb = None
        self.output_image = None
        self.selected_color = (0, 0, 0)

        self.setup_ui()

    def setup_ui(self):
        """Set up the user interface."""
        main_frame = ttk.Frame(self)
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)

        # Input section
        input_frame = ttk.LabelFrame(main_frame, text="Input Image", padding=10)
        input_frame.pack(fill="x", pady=(0, 10))

        ttk.Label(input_frame, text="Image File:").grid(row=0, column=0, sticky="w", pady=2)
        self.image_path = PlaceholderEntry(input_frame, placeholder="Select image file...")
        self.image_path.grid(row=0, column=1, sticky="ew", padx=(5, 0), pady=2)
        ttk.Button(input_frame, text="Browse",
                command=self.browse_image).grid(row=0, column=2, padx=(5, 0), pady=2)

        input_frame.columnconfigure(1, weight=1)

        # Preview section
        preview_frame = ttk.LabelFrame(main_frame, text="Preview", padding=10)
        preview_frame.pack(fill="both", expand=True, pady=(0, 10))

        preview_container = ttk.Frame(preview_frame)
        preview_container.pack(fill="both", expand=True)

        # Before/After previews
        before_frame = ttk.Frame(preview_container)
        before_frame.pack(side="left", fill="both", expand=True, padx=(0, 5))
        ttk.Label(before_frame, text="Original", font=("Arial", 10, "bold")).pack()
        self.preview_before = ttk.Label(before_frame, text="Load image\nto see preview")
        self.preview_before.pack(expand=True)
        self.preview_before.bind("<Button-1>", self.on_image_click)

        after_frame = ttk.Frame(preview_container)
        after_frame.pack(side="right", fill="both", expand=True, padx=(5, 0))
        ttk.Label(after_frame, text="With Transparency", font=("Arial", 10, "bold")).pack()
        self.preview_after = ttk.Label(after_frame, text="Select color\nto make transparent")
        self.preview_after.pack(expand=True)

        # Color selection section
        color_frame = ttk.LabelFrame(main_frame, text="Color Selection", padding=10)
        color_frame.pack(fill="x", pady=(0, 10))

        # Color display and RGB inputs
        color_container = ttk.Frame(color_frame)
        color_container.pack(fill="x")

        # Color swatch
        ttk.Label(color_container, text="Selected Color:").grid(row=0, column=0, sticky="w", pady=2)
        self.color_canvas = tk.Canvas(color_container, width=50, height=30, bg="black")
        self.color_canvas.grid(row=0, column=1, padx=(5, 0), pady=2)

        # RGB input
        ttk.Label(color_container, text="RGB:").grid(row=0, column=2, sticky="w", padx=(10, 0), pady=2)

        rgb_frame = ttk.Frame(color_container)
        rgb_frame.grid(row=0, column=3, padx=(5, 0), pady=2)

        tk.Label(rgb_frame, text="R:", width=2).grid(row=0, column=0, sticky="w")
        self.r_var = tk.IntVar(value=0)
        self.r_spin = tk.Spinbox(rgb_frame, from_=0, to=255, width=4, textvariable=self.r_var,
                                command=self.on_rgb_change)
        self.r_spin.grid(row=0, column=1, padx=(0, 5))

        tk.Label(rgb_frame, text="G:", width=2).grid(row=0, column=2, sticky="w")
        self.g_var = tk.IntVar(value=0)
        self.g_spin = tk.Spinbox(rgb_frame, from_=0, to=255, width=4, textvariable=self.g_var,
                                command=self.on_rgb_change)
        self.g_spin.grid(row=0, column=3, padx=(0, 5))

        tk.Label(rgb_frame, text="B:", width=2).grid(row=0, column=4, sticky="w")
        self.b_var = tk.IntVar(value=0)
        self.b_spin = tk.Spinbox(rgb_frame, from_=0, to=255, width=4, textvariable=self.b_var,
                                command=self.on_rgb_change)
        self.b_spin.grid(row=0, column=5)

        # Common color buttons
        common_frame = ttk.Frame(color_frame)
        common_frame.pack(fill="x", pady=(10, 0))

        ttk.Label(common_frame, text="Common Colors:").pack(anchor="w")

        button_frame = ttk.Frame(common_frame)
        button_frame.pack(fill="x", pady=(5, 0))

        common_colors = [
            ("Black", (0, 0, 0)),
            ("White", (255, 255, 255)),
            ("Red", (255, 0, 0)),
            ("Green", (0, 255, 0)),
            ("Blue", (0, 0, 255)),
            ("Cyan", (0, 255, 255)),
            ("Magenta", (255, 0, 255)),
            ("Yellow", (255, 255, 0))
        ]

        for i, (name, color) in enumerate(common_colors):
            ttk.Button(button_frame, text=name,
                        command=lambda c=color: self.set_color(c)).grid(row=0, column=i, padx=2)

        # Controls section
        controls_frame = ttk.LabelFrame(main_frame, text="Controls", padding=10)
        controls_frame.pack(fill="x", pady=(0, 10))

        # Tolerance slider
        ttk.Label(controls_frame, text="Tolerance:").grid(row=0, column=0, sticky="w", pady=2)
        self.tolerance = tk.IntVar(value=10)
        self.tolerance_slider = ttk.Scale(controls_frame, from_=0, to=100, orient="horizontal",
                                            variable=self.tolerance, command=self.update_preview)
        self.tolerance_slider.grid(row=0, column=1, sticky="ew", padx=(5, 0), pady=2)
        self.tolerance_label = ttk.Label(controls_frame, text="10")
        self.tolerance_label.grid(row=0, column=2, padx=(5, 0), pady=2)

        # Alpha value slider
        ttk.Label(controls_frame, text="Transparency:").grid(row=1, column=0, sticky="w", pady=2)
        self.alpha_value = tk.IntVar(value=0)
        self.alpha_slider = ttk.Scale(controls_frame, from_=0, to=255, orient="horizontal",
                                        variable=self.alpha_value, command=self.update_preview)
        self.alpha_slider.grid(row=1, column=1, sticky="ew", padx=(5, 0), pady=2)
        self.alpha_label = ttk.Label(controls_frame, text="0 (Fully Transparent)")
        self.alpha_label.grid(row=1, column=2, padx=(5, 0), pady=2)

        controls_frame.columnconfigure(1, weight=1)

        # Output section
        output_frame = ttk.LabelFrame(main_frame, text="Output", padding=10)
        output_frame.pack(fill="x")

        ttk.Button(output_frame, text="Apply Transparency",
                    command=self.apply_transparency).pack(side="left")
        ttk.Button(output_frame, text="Save Result",
                    command=self.save_output).pack(side="left", padx=(10, 0))
        ttk.Button(output_frame, text="Batch Process Folder",
                    command=self.batch_process).pack(side="left", padx=(10, 0))

        # Status label
        self.status_label = ttk.Label(main_frame, text="Ready", foreground="green")
        self.status_label.pack(pady=(10, 0))

    def browse_image(self):
        """Browse for image file."""
        path = browse_file(
            title="Select Image",
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
            self.base_image = Image.open(path).convert("RGBA")
            self.update_preview_image()
            self.status_label.config(text="Image loaded", foreground="green")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load image: {e}")
            self.status_label.config(text="Error loading image", foreground="red")

        def update_preview_image(self):
            """Update the before preview image."""
        if not self.base_image:
            return

        # Create thumbnail for preview
        self.base_thumb = self.base_image.copy()
        self.base_thumb.thumbnail((250, 250))

        # Convert to display format (with white background for transparency)
        display_img = Image.new("RGB", self.base_thumb.size, (255, 255, 255))
        display_img.paste(self.base_thumb, mask=self.base_thumb.split()[-1])

        photo = ImageTk.PhotoImage(display_img)
        self.preview_before.config(image=photo, text="")
        self.preview_before.image = photo  # Keep a reference

    def on_image_click(self, event):
        """Handle click on the preview image to select color."""
        if not self.base_thumb:
            return

        # Get click coordinates relative to the image
        widget_width = self.preview_before.winfo_width()
        widget_height = self.preview_before.winfo_height()
        img_width, img_height = self.base_thumb.size

        # Calculate image position in widget (centered)
        x_offset = (widget_width - img_width) // 2
        y_offset = (widget_height - img_height) // 2

        # Adjust click coordinates
        img_x = event.x - x_offset
        img_y = event.y - y_offset

        # Check if click is within image bounds
        if 0 <= img_x < img_width and 0 <= img_y < img_height:
            # Get pixel color
            pixel = self.base_thumb.getpixel((img_x, img_y))
            self.set_color((pixel[0], pixel[1], pixel[2]))

    def set_color(self, color):
        """Set the selected color."""
        self.selected_color = color
        self.r_var.set(color[0])
        self.g_var.set(color[1])
        self.b_var.set(color[2])
        self.update_color_display()
        self.update_preview()

    def on_rgb_change(self):
        """Handle RGB input changes."""
        r = max(0, min(255, self.r_var.get()))
        g = max(0, min(255, self.g_var.get()))
        b = max(0, min(255, self.b_var.get()))

        self.selected_color = (r, g, b)
        self.update_color_display()
        self.update_preview()

    def update_color_display(self):
        """Update the color swatch display."""
        color_hex = f"#{self.selected_color[0]:02x}{self.selected_color[1]:02x}{self.selected_color[2]:02x}"
        self.color_canvas.config(bg=color_hex)

    def update_preview(self, value=None):
        """Update tolerance and alpha labels and preview."""
        tolerance = int(self.tolerance.get())
        alpha = int(self.alpha_value.get())

        self.tolerance_label.config(text=str(tolerance))

        if alpha == 0:
            alpha_text = "0 (Fully Transparent)"
        elif alpha == 255:
            alpha_text = "255 (Opaque)"
        else:
            alpha_text = str(alpha)
        self.alpha_label.config(text=alpha_text)

        # Update after preview if image is loaded
        if self.base_image:
            self.apply_transparency(preview_only=True)

    def color_distance(self, color1, color2):
        """Calculate color distance using Euclidean distance."""
        return math.sqrt(
            (color1[0] - color2[0]) ** 2 +
            (color1[1] - color2[1]) ** 2 +
            (color1[2] - color2[2]) ** 2
        )

    def apply_transparency(self, preview_only=False):
        """Apply transparency to the image."""
        if not self.base_image:
            if not preview_only:
                messagebox.showerror("Error", "Please load an image first.")
            return

        tolerance = self.tolerance.get()
        alpha = self.alpha_value.get()
        target_color = self.selected_color

        try:
            # Work on a copy
            result = self.base_image.copy()
            width, height = result.size

            # Process each pixel
            for x in range(width):
                for y in range(height):
                    pixel = result.getpixel((x, y))
                    pixel_color = (pixel[0], pixel[1], pixel[2])

                    # Check if pixel color is within tolerance of target color
                    distance = self.color_distance(pixel_color, target_color)
                    max_distance = tolerance * 4.41  # Normalize to 0-255 range

                    if distance <= max_distance:
                        # Set alpha based on distance (closer = more transparent)
                        if tolerance > 0:
                            fade_factor = 1.0 - (distance / max_distance)
                            new_alpha = int(alpha + (pixel[3] - alpha) * (1.0 - fade_factor))
                        else:
                            new_alpha = alpha

                        new_alpha = max(0, min(255, new_alpha))
                        result.putpixel((x, y), (pixel[0], pixel[1], pixel[2], new_alpha))

            self.output_image = result

            if preview_only:
                # Update after preview
                after_thumb = result.copy()
                after_thumb.thumbnail((250, 250))

                # Create checkered background to show transparency
                checker_size = 10
                checker_bg = Image.new("RGB", after_thumb.size, (255, 255, 255))

                for x in range(0, after_thumb.width, checker_size):
                    for y in range(0, after_thumb.height, checker_size):
                        if (x // checker_size + y // checker_size) % 2:
                            checker_bg.paste((200, 200, 200), (x, y, min(x + checker_size, after_thumb.width),
                                                                min(y + checker_size, after_thumb.height)))

                # Composite the image over the checker background
                display_img = Image.alpha_composite(checker_bg.convert("RGBA"), after_thumb)

                after_photo = ImageTk.PhotoImage(display_img)
                self.preview_after.config(image=after_photo, text="")
                self.preview_after.image = after_photo  # Keep a reference
            else:
                self.status_label.config(text="Transparency applied", foreground="green")

        except Exception as e:
            error_msg = f"Error applying transparency: {e}"
            if not preview_only:
                messagebox.showerror("Error", error_msg)
            self.status_label.config(text=error_msg, foreground="red")

    def save_output(self):
        """Save the result image."""
        if not self.output_image:
            messagebox.showerror("Error", "No processed image to save. Please apply transparency first.")
            return

        output_path = filedialog.asksaveasfilename(
            title="Save Transparent Image",
            defaultextension=".png",
            filetypes=[("PNG Files", "*.png"), ("TGA Files", "*.tga")]
        )

        if output_path:
            try:
                self.output_image.save(output_path)
                self.status_label.config(text=f"Saved: {os.path.basename(output_path)}", foreground="green")

            except Exception as e:
                messagebox.showerror("Error", f"Failed to save image: {e}")
                self.status_label.config(text="Error saving image", foreground="red")

    def batch_process(self):
        """Batch process a folder of images."""
        if not self.selected_color:
            messagebox.showerror("Error", "Please select a color first.")
            return

        input_folder = filedialog.askdirectory(title="Select folder with images")
        if not input_folder:
            return

        output_folder = filedialog.askdirectory(title="Select output folder")
        if not output_folder:
            return

        tolerance = self.tolerance.get()
        alpha = self.alpha_value.get()
        target_color = self.selected_color

        # Process all images in the folder
        processed = 0
        errors = 0

        for filename in os.listdir(input_folder):
            if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.tga', '.bmp')):
                input_path = os.path.join(input_folder, filename)
                output_name = os.path.splitext(filename)[0] + "_transparent.png"
                output_path = os.path.join(output_folder, output_name)

                try:
                    # Load image
                    img = Image.open(input_path).convert("RGBA")

                    # Apply transparency
                    width, height = img.size
                    for x in range(width):
                        for y in range(height):
                            pixel = img.getpixel((x, y))
                            pixel_color = (pixel[0], pixel[1], pixel[2])

                            distance = self.color_distance(pixel_color, target_color)
                            max_distance = tolerance * 4.41

                            if distance <= max_distance:
                                if tolerance > 0:
                                    fade_factor = 1.0 - (distance / max_distance)
                                    new_alpha = int(alpha + (pixel[3] - alpha) * (1.0 - fade_factor))
                                else:
                                    new_alpha = alpha

                                new_alpha = max(0, min(255, new_alpha))
                                img.putpixel((x, y), (pixel[0], pixel[1], pixel[2], new_alpha))

                    # Save result
                    img.save(output_path)
                    processed += 1

                except Exception as e:
                    print(f"Error processing {filename}: {e}")
                    errors += 1

        messagebox.showinfo("Batch Complete",
                            f"Processed {processed} images.\\n{errors} errors occurred.")
        self.status_label.config(text=f"Batch complete: {processed} processed, {errors} errors",
                                foreground="green" if errors == 0 else "orange")
