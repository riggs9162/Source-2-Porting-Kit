import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk, ImageEnhance
import os

class BrightnessToAlphaApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Brightness to Alpha Tool")
        self.root.geometry("620x460")
        self.root.configure(bg="#2e2e2e")
        self.image = None
        self.image_path = ""
        self.preview_label = tk.Label(self.root, bg="#2e2e2e")
        self.preview_label.pack(pady=10)

        self.select_button = tk.Button(root, text="Select Image", command=self.load_image, bg="#3e3e3e", fg="white")
        self.select_button.pack()

        self.slider = tk.Scale(root, from_=0, to=255, orient=tk.HORIZONTAL, label="Brightness Threshold",
                               command=self.update_preview, bg="#2e2e2e", fg="white", highlightbackground="#2e2e2e")
        self.slider.set(200)
        self.slider.pack(fill=tk.X, padx=20)

        self.invert_var = tk.IntVar()
        self.invert_checkbox = tk.Checkbutton(root, text="Invert Alpha Logic", variable=self.invert_var,
                                              command=self.update_preview, bg="#2e2e2e", fg="white", selectcolor="#2e2e2e")
        self.invert_checkbox.pack()

        self.save_button = tk.Button(root, text="Save Image", command=self.save_image, state=tk.DISABLED, bg="#3e3e3e", fg="white")
        self.save_button.pack(pady=10)

    def load_image(self):
        file_path = filedialog.askopenfilename(filetypes=[("Image Files", "*.png *.tga")])
        if not file_path:
            return
        try:
            self.image_path = file_path
            self.image = Image.open(file_path).convert("RGB")
            self.update_preview()
            self.save_button.config(state=tk.NORMAL)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def update_preview(self, event=None):
        if self.image is None:
            return
        threshold = self.slider.get()
        invert = self.invert_var.get()
        grayscale = self.image.convert("L")

        # Create smooth alpha (not binary), use scale from 0 to 255
        def smooth_alpha(p):
            scale = 255 / 50  # spread over ~50 brightness units
            if invert:
                return max(0, min(255, int((threshold - p) * scale)))
            else:
                return max(0, min(255, int((p - threshold) * scale)))

        alpha = grayscale.point(smooth_alpha)

        preview_image = self.image.copy()
        preview_image.putalpha(alpha)
        preview = preview_image.resize((256, 256))
        preview_tk = ImageTk.PhotoImage(preview)
        self.preview_label.config(image=preview_tk)
        self.preview_label.image = preview_tk

    def save_image(self):
        if self.image is None:
            return
        threshold = self.slider.get()
        invert = self.invert_var.get()
        grayscale = self.image.convert("L")

        def smooth_alpha(p):
            scale = 255 / 50
            if invert:
                return max(0, min(255, int((threshold - p) * scale)))
            else:
                return max(0, min(255, int((p - threshold) * scale)))

        alpha = grayscale.point(smooth_alpha)
        output_image = self.image.copy()
        output_image.putalpha(alpha)
        base, ext = os.path.splitext(self.image_path)
        output_path = f"{base}_alpha{ext}"
        output_image.save(output_path)
        messagebox.showinfo("Saved", f"Saved to: {output_path}")

if __name__ == "__main__":
    root = tk.Tk()
    app = BrightnessToAlphaApp(root)
    root.mainloop()
