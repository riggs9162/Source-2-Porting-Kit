import tkinter as tk
from tkinter import filedialog
from PIL import Image, ImageTk
import os
import math

class ColorTransparencyTool:
    def __init__(self, root):
        self.root = root
        self.root.title("Color-Based Transparency Tool")
        self.root.geometry("700x600")

        # Images
        self.base_image = None
        self.base_thumb = None
        self.output_image = None

        # Selected color (RGB)
        self.selected_color = (0, 0, 0)

        # UI Frames
        top_frame = tk.Frame(root)
        top_frame.pack(pady=10)
        control_frame = tk.Frame(root)
        control_frame.pack(pady=10)
        log_frame = tk.Frame(root)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # Previews
        self.preview_before = tk.Label(top_frame)
        self.preview_before.pack(side=tk.LEFT, padx=10)
        self.preview_after = tk.Label(top_frame)
        self.preview_after.pack(side=tk.RIGHT, padx=10)
        self.preview_before.bind("<Button-1>", self.pick_color)

        # Controls
        tk.Button(control_frame, text="Load Base Texture", command=self.load_base).grid(row=0, column=0, padx=5, pady=5)
        tk.Label(control_frame, text="Picked Color:").grid(row=0, column=1)
        self.swatch = tk.Label(control_frame, bg="#000000", width=4, relief=tk.SUNKEN)
        self.swatch.grid(row=0, column=2, padx=5)

        tk.Label(control_frame, text="Exact Transparency (%)").grid(row=1, column=0, sticky="e")
        self.exact_slider = tk.Scale(control_frame, from_=0, to=100, orient=tk.HORIZONTAL,
                                     length=300, command=lambda e: self.update_preview())
        self.exact_slider.set(100)
        self.exact_slider.grid(row=1, column=1, columnspan=2, padx=5)

        tk.Label(control_frame, text="Tolerance (0–442)").grid(row=2, column=0, sticky="e")
        self.tol_slider = tk.Scale(control_frame, from_=0, to=442, orient=tk.HORIZONTAL,
                                   length=300, command=lambda e: self.update_preview())
        self.tol_slider.set(50)
        self.tol_slider.grid(row=2, column=1, columnspan=2, padx=5)

        tk.Label(control_frame, text="Neighbor Transparency (%)").grid(row=3, column=0, sticky="e")
        self.neighbor_slider = tk.Scale(control_frame, from_=0, to=100, orient=tk.HORIZONTAL,
                                        length=300, command=lambda e: self.update_preview())
        self.neighbor_slider.set(50)
        self.neighbor_slider.grid(row=3, column=1, columnspan=2, padx=5)

        tk.Button(control_frame, text="Reset", command=self.reset_sliders).grid(row=4, column=1, pady=10)
        tk.Button(control_frame, text="Save Output", command=self.save_output).grid(row=4, column=2)

        # Log area
        tk.Label(log_frame, text="Log:").pack(anchor="w")
        self.log_text = tk.Text(log_frame, height=6, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def log(self, msg):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def load_base(self):
        path = filedialog.askopenfilename(filetypes=[("Image Files", "*.png;*.jpg;*.tga")])
        if not path:
            return
        try:
            self.base_image = Image.open(path).convert("RGBA")
            self.log(f"Loaded base: {os.path.basename(path)}")
        except Exception as e:
            self.log(f"Error loading base: {e}")
            return
        self._make_thumbnail()
        self.update_preview()

    def _make_thumbnail(self):
        thumb_size = (300, 300)
        self.base_thumb = self.base_image.resize(thumb_size, Image.NEAREST)
        self.tk_before = ImageTk.PhotoImage(self.base_thumb)
        self.preview_before.config(image=self.tk_before)

    def pick_color(self, event):
        if not self.base_thumb:
            return
        x, y = event.x, event.y
        w, h = self.base_thumb.size
        x = min(max(x, 0), w - 1)
        y = min(max(y, 0), h - 1)
        r, g, b, _ = self.base_thumb.getpixel((x, y))
        self.selected_color = (r, g, b)
        hexcol = f"#{r:02x}{g:02x}{b:02x}"
        self.swatch.config(bg=hexcol)
        self.log(f"Picked color: {hexcol}")
        self.update_preview()

    def update_preview(self):
        if not (self.base_image and self.base_thumb):
            return
        thumb = self.base_thumb.copy()
        exact = self.exact_slider.get() / 100.0
        tol = self.tol_slider.get()
        neigh = self.neighbor_slider.get() / 100.0
        px = thumb.load()
        target = self.selected_color
        w, h = thumb.size

        for yy in range(h):
            for xx in range(w):
                r, g, b, a = px[xx, yy]
                d = math.sqrt((r - target[0])**2 + (g - target[1])**2 + (b - target[2])**2)
                if d == 0 and exact > 0:
                    a = int(a * (1 - exact))
                elif d <= tol and neigh > 0:
                    weight = 1 - d / tol
                    a = int(a * (1 - neigh * weight))
                px[xx, yy] = (r, g, b, a)

        self.tk_after = ImageTk.PhotoImage(thumb)
        self.preview_after.config(image=self.tk_after)
        self._apply_full(exact, tol, neigh)

    def _apply_full(self, exact, tol, neigh):
        base = self.base_image.copy()
        px = base.load()
        w, h = base.size
        for yy in range(h):
            for xx in range(w):
                r, g, b, a = px[xx, yy]
                d = math.sqrt((r - self.selected_color[0])**2 +
                              (g - self.selected_color[1])**2 +
                              (b - self.selected_color[2])**2)
                if d == 0 and exact > 0:
                    a = int(a * (1 - exact))
                elif d <= tol and neigh > 0:
                    weight = 1 - d / tol
                    a = int(a * (1 - neigh * weight))
                px[xx, yy] = (r, g, b, a)
        self.output_image = base

    def reset_sliders(self):
        self.exact_slider.set(100)
        self.tol_slider.set(50)
        self.neighbor_slider.set(50)
        self.log("Sliders reset to defaults")
        self.update_preview()

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
    import math
    from PIL import Image
    root = tk.Tk()
    app = ColorTransparencyTool(root)
    root.mainloop()
