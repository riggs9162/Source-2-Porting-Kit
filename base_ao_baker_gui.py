import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk, ImageChops
import os

class AOBakerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Base + AO Baker")
        self.root.geometry("600x500")

        # Images
        self.base_image = None
        self.ao_image = None
        self.output_image = None

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
        tk.Button(control_frame, text="Load AO Texture", command=self.load_ao).grid(row=0, column=1, padx=5, pady=5)

        tk.Label(control_frame, text="AO Strength (%)").grid(row=1, column=0)
        self.ao_slider = tk.Scale(control_frame, from_=0, to=100, orient=tk.HORIZONTAL,
                                  length=300, resolution=1, command=self.update_preview)
        self.ao_slider.set(50)
        self.ao_slider.grid(row=1, column=1, padx=5)

        tk.Button(root, text="Save Result", command=self.save_output).pack(pady=10)

        # Log
        self.log_text = tk.Label(root, text="", anchor="w")
        self.log_text.pack(fill=tk.X, padx=10)

    def log(self, msg):
        self.log_text.config(text=msg)

    def load_base(self):
        path = filedialog.askopenfilename(filetypes=[("Image Files", "*.png *.jpg *.jpeg *.tga")])
        if path:
            try:
                self.base_image = Image.open(path).convert("RGB")
                self.log(f"Loaded base: {os.path.basename(path)}")
                self.update_preview()
            except Exception as e:
                self.log(f"Error loading base: {e}")

    def load_ao(self):
        path = filedialog.askopenfilename(filetypes=[("Image Files", "*.png *.jpg *.jpeg *.tga")])
        if path:
            try:
                self.ao_image = Image.open(path).convert("L")
                self.log(f"Loaded AO: {os.path.basename(path)}")
                self.update_preview()
            except Exception as e:
                self.log(f"Error loading AO: {e}")

    def update_preview(self, event=None):
        if self.base_image is None or self.ao_image is None:
            return

        base = self.base_image.copy()
        ao = self.ao_image.resize(base.size)
        ao_rgb = Image.merge("RGB", [ao] * 3)

        # Multiply base by AO map
        multiplied = ImageChops.multiply(base, ao_rgb)

        # Blend between base and multiplied by slider strength
        strength = self.ao_slider.get() / 100.0
        result = Image.blend(base, multiplied, alpha=strength)
        self.output_image = result

        # Show previews
        before = base.resize((256, 256))
        after = result.resize((256, 256))
        self.before_tk = ImageTk.PhotoImage(before)
        self.after_tk = ImageTk.PhotoImage(after)
        self.preview_before.config(image=self.before_tk)
        self.preview_after.config(image=self.after_tk)

    def save_output(self):
        if self.output_image:
            path = filedialog.asksaveasfilename(defaultextension=".png",
                                                filetypes=[("PNG Files", "*.png")])
            if path:
                try:
                    self.output_image.save(path)
                    self.log(f"Saved result: {os.path.basename(path)}")
                except Exception as e:
                    self.log(f"Error saving: {e}")
        else:
            self.log("No output to save.")

if __name__ == "__main__":
    root = tk.Tk()
    from PIL import Image  # ensure PIL is imported
    app = AOBakerApp(root)
    root.mainloop()
