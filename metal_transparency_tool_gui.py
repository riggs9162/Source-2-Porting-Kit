import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk
import os

class MetalTransparencyTool:
    def __init__(self, root):
        self.root = root
        self.root.title("Metal Mask Transparency Tool")
        self.root.geometry("600x500")

        # Images
        self.base_image = None
        self.mask_image = None
        self.output_image = None

        # UI Frames
        top_frame = tk.Frame(root)
        top_frame.pack(pady=10)
        control_frame = tk.Frame(root)
        control_frame.pack(pady=10)
        log_frame = tk.Frame(root)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # Previews
        self.preview_before = tk.Label(top_frame, text="Before", compound=tk.TOP)
        self.preview_before.pack(side=tk.LEFT, padx=10)
        self.preview_after = tk.Label(top_frame, text="After", compound=tk.TOP)
        self.preview_after.pack(side=tk.RIGHT, padx=10)

        # Load buttons
        tk.Button(control_frame, text="Load Base Texture", command=self.load_base).grid(row=0, column=0, padx=5, pady=5)
        tk.Button(control_frame, text="Load Metal Mask",    command=self.load_mask).grid(row=0, column=1, padx=5)

        # Transparency slider
        tk.Label(control_frame, text="Metal Transparency (%)").grid(row=1, column=0, sticky="e")
        self.trans_slider = tk.Scale(control_frame, from_=0, to=100, orient=tk.HORIZONTAL,
                                     length=300, command=lambda e: self.update_preview())
        self.trans_slider.set(50)
        self.trans_slider.grid(row=1, column=1, padx=5)
        tk.Button(control_frame, text="Reset", command=self.reset_transparency).grid(row=1, column=2, padx=5)

        # Save button
        tk.Button(control_frame, text="Save Output", command=self.save_output).grid(row=2, column=1, pady=10)

        # Log area
        tk.Label(log_frame, text="Log:").pack(anchor="w")
        self.log_text = tk.Text(log_frame, height=6, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def log(self, message):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def load_base(self):
        path = filedialog.askopenfilename(filetypes=[("Image Files", "*.png *.tga *.jpg *.jpeg")])
        if not path:
            return
        try:
            self.base_image = Image.open(path).convert("RGBA")
            self.log(f"Loaded base: {os.path.basename(path)}")
        except Exception as e:
            self.log(f"Error loading base: {e}")
            return
        self.update_preview()

    def load_mask(self):
        path = filedialog.askopenfilename(filetypes=[("Mask Files", "*.png *.tga *.jpg *.jpeg")])
        if not path:
            return
        try:
            self.mask_image = Image.open(path).convert("L")
            self.log(f"Loaded mask: {os.path.basename(path)}")
        except Exception as e:
            self.log(f"Error loading mask: {e}")
            return
        # resize mask to base size if base exists
        if self.base_image:
            self.mask_image = self.mask_image.resize(self.base_image.size, Image.NEAREST)
        self.update_preview()

    def update_preview(self):
        if not (self.base_image and self.mask_image):
            return

        # create thumbnail previews for speed
        thumb_size = (256, 256)
        base_thumb = self.base_image.resize(thumb_size, Image.NEAREST)
        mask_thumb = self.mask_image.resize(thumb_size, Image.NEAREST)

        factor = self.trans_slider.get() / 100.0
        base_pixels = base_thumb.load()
        mask_pixels = mask_thumb.load()

        for y in range(thumb_size[1]):
            for x in range(thumb_size[0]):
                r, g, b, a = base_pixels[x, y]
                m = mask_pixels[x, y] / 255.0
                new_alpha = int(a * (1 - m * factor))
                base_pixels[x, y] = (r, g, b, new_alpha)

        # update preview labels
        self.before_tk = ImageTk.PhotoImage(self.base_image.resize(thumb_size, Image.NEAREST))
        self.after_tk  = ImageTk.PhotoImage(base_thumb)
        self.preview_before.config(image=self.before_tk)
        self.preview_after.config(image=self.after_tk)

        # also apply full-size to self.output_image
        self.apply_full()

    def apply_full(self):
        base = self.base_image.copy()
        mask = self.mask_image.resize(base.size, Image.NEAREST)
        factor = self.trans_slider.get() / 100.0
        px = base.load()
        mp = mask.load()
        w, h = base.size
        for y in range(h):
            for x in range(w):
                r, g, b, a = px[x, y]
                m = mp[x, y] / 255.0
                new_a = int(a * (1 - m * factor))
                px[x, y] = (r, g, b, new_a)
        self.output_image = base

    def reset_transparency(self):
        self.trans_slider.set(50)
        self.update_preview()
        self.log("Transparency reset to 50%")

    def save_output(self):
        if not hasattr(self, "output_image") or self.output_image is None:
            self.log("Nothing to save")
            return
        path = filedialog.asksaveasfilename(defaultextension=".png", filetypes=[("PNG", "*.png")])
        if path:
            try:
                self.output_image.save(path)
                self.log(f"Saved output: {os.path.basename(path)}")
            except Exception as e:
                self.log(f"Error saving: {e}")

if __name__ == "__main__":
    root = tk.Tk()
    from PIL import Image
    app = MetalTransparencyTool(root)
    root.mainloop()
