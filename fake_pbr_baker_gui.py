import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk, ImageEnhance, ImageOps, ImageChops
import os

class FakePBRBaker:
    def __init__(self, root):
        self.root = root
        self.root.title("Fake PBR Baker Enhanced")
        self.root.geometry("950x800")

        # Images
        self.base_image = None
        self.rough_image = None
        self.ao_image = None
        self.baked_image = None

        # Previews
        preview_frame = tk.Frame(root)
        preview_frame.pack(pady=10)
        self.preview_before = tk.Label(preview_frame)
        self.preview_before.pack(side=tk.LEFT, padx=10)
        self.preview_after = tk.Label(preview_frame)
        self.preview_after.pack(side=tk.RIGHT, padx=10)

        # Controls
        control_frame = tk.Frame(root)
        control_frame.pack(pady=10)

        tk.Button(control_frame, text="Load Base Texture", command=self.load_base).grid(row=0, column=0, padx=5, pady=5)
        tk.Button(control_frame, text="Load Roughness Texture", command=self.load_roughness).grid(row=0, column=1, padx=5, pady=5)
        tk.Button(control_frame, text="Load AO Texture", command=self.load_ao).grid(row=0, column=2, padx=5, pady=5)

        # Blend Strength slider
        tk.Label(control_frame, text="Blend Strength").grid(row=1, column=0)
        self.blend_slider = tk.Scale(control_frame, from_=0, to=100, resolution=0.5,
                                     orient=tk.HORIZONTAL, length=300, command=self.update_preview)
        self.blend_slider.set(35)
        self.blend_slider.grid(row=1, column=1, padx=5)

        # Roughness Contrast slider
        tk.Label(control_frame, text="Roughness Contrast").grid(row=2, column=0)
        self.contrast_slider = tk.Scale(control_frame, from_=10, to=300, resolution=1,
                                        orient=tk.HORIZONTAL, length=300, command=self.update_preview)
        self.contrast_slider.set(200)
        self.contrast_slider.grid(row=2, column=1, padx=5)

        # Reduce Whites slider (pre-shading roughness)
        tk.Label(control_frame, text="Reduce Whites in Roughness").grid(row=3, column=0)
        self.whites_slider = tk.Scale(control_frame, from_=0, to=255, resolution=1,
                                      orient=tk.HORIZONTAL, length=300, command=self.update_preview)
        self.whites_slider.set(0)
        self.whites_slider.grid(row=3, column=1, padx=5)

        # Dark Shading Intensity slider (dark parts shading)
        tk.Label(control_frame, text="Dark Shading Intensity").grid(row=4, column=0)
        self.dark_slider = tk.Scale(control_frame, from_=0, to=100, resolution=0.5,
                                    orient=tk.HORIZONTAL, length=300, command=self.update_preview)
        self.dark_slider.set(0)
        self.dark_slider.grid(row=4, column=1, padx=5)

        # Invert roughness toggle
        self.invert_var = tk.IntVar()
        self.invert_check = tk.Checkbutton(control_frame, text="Invert Roughness", variable=self.invert_var, command=self.update_preview)
        self.invert_check.grid(row=5, column=0, columnspan=2)

        # Save button
        tk.Button(control_frame, text="Save Baked Texture", command=self.save_output).grid(row=6, column=0, columnspan=3, pady=10)

        # Log area
        log_frame = tk.Frame(root)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        tk.Label(log_frame, text="Log:").pack(anchor="w")
        self.log_text = tk.Text(log_frame, height=6, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def log(self, message):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def load_base(self):
        path = filedialog.askopenfilename(filetypes=[("Image Files", "*.png *.jpg *.jpeg *.tga")])
        if path:
            try:
                self.base_image = Image.open(path).convert("RGB")
                self.log(f"Loaded base texture: {os.path.basename(path)}")
                self.update_preview()
            except Exception as e:
                self.log(f"Error loading base texture: {e}")

    def load_roughness(self):
        path = filedialog.askopenfilename(filetypes=[("Image Files", "*.png *.jpg *.jpeg *.tga")])
        if path:
            try:
                self.rough_image = Image.open(path).convert("L")
                self.log(f"Loaded roughness texture: {os.path.basename(path)}")
                self.update_preview()
            except Exception as e:
                self.log(f"Error loading roughness texture: {e}")

    def load_ao(self):
        path = filedialog.askopenfilename(filetypes=[("Image Files", "*.png *.jpg *.jpeg *.tga")])
        if path:
            try:
                self.ao_image = Image.open(path).convert("L")
                self.log(f"Loaded AO texture: {os.path.basename(path)}")
                self.update_preview()
            except Exception as e:
                self.log(f"Error loading AO texture: {e}")

    def update_preview(self, event=None):
        if self.base_image is None or self.rough_image is None:
            return

        try:
            # Prepare images
            base = self.base_image.copy()
            rough = self.rough_image.resize(base.size)

            # Apply roughness contrast
            contrast_val = self.contrast_slider.get() / 100.0
            rough = ImageEnhance.Contrast(rough).enhance(contrast_val)

            # Reduce whites in roughness (pre-shading)
            reduce_white = self.whites_slider.get()
            rough = rough.point(lambda p: min(p, 255 - reduce_white))

            # Prepare masks
            # Highlights mask (dark roughness -> highlights)
            if self.invert_var.get():
                highlights_mask = rough
            else:
                highlights_mask = ImageOps.invert(rough)
            # Dark mask (dark roughness -> shadows)
            dark_mask = ImageOps.invert(rough)

            # Scale masks by strength
            blend_strength = self.blend_slider.get() / 100.0
            highlight_mask_scaled = highlights_mask.point(lambda p: int(p * blend_strength))
            dark_strength = self.dark_slider.get() / 100.0
            dark_mask_scaled = dark_mask.point(lambda p: int(p * dark_strength))

            # Apply highlight overlay (white) and dark overlay (black)
            shading_light = Image.new("RGB", base.size, (255, 255, 255))
            result = Image.composite(shading_light, base, highlight_mask_scaled)

            shading_dark = Image.new("RGB", base.size, (0, 0, 0))
            result = Image.composite(shading_dark, result, dark_mask_scaled)

            # Apply AO if available
            if self.ao_image:
                ao = self.ao_image.resize(base.size)
                ao_rgb = Image.merge("RGB", [ao] * 3)
                result = ImageChops.multiply(result, ao_rgb)

            # Update previews
            before_preview = base.resize((256, 256))
            after_preview = result.resize((256, 256))
            self.before_tk = ImageTk.PhotoImage(before_preview)
            self.after_tk = ImageTk.PhotoImage(after_preview)
            self.preview_before.config(image=self.before_tk)
            self.preview_after.config(image=self.after_tk)

            self.baked_image = result
        except Exception as e:
            self.log(f"Error during preview update: {e}")

    def save_output(self):
        if self.baked_image:
            try:
                save_path = filedialog.asksaveasfilename(defaultextension=".png", filetypes=[("PNG", "*.png")])
                if save_path:
                    self.baked_image.save(save_path)
                    self.log(f"Saved baked texture: {os.path.basename(save_path)}")
            except Exception as e:
                self.log(f"Error saving baked texture: {e}")
        else:
            self.log("No baked image to save.")

if __name__ == "__main__":
    root = tk.Tk()
    app = FakePBRBaker(root)
    root.mainloop()
