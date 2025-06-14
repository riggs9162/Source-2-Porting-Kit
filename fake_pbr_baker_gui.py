import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk, ImageEnhance, ImageOps, ImageChops
import os

class FakePBRBaker:
    def __init__(self, root):
        self.root = root
        self.root.title("Fake PBR Baker Optimized + Resets")
        self.root.geometry("1000x950")

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

        # Load buttons
        tk.Button(control_frame, text="Load Base Texture", command=self.load_base).grid(row=0, column=0, padx=5)
        tk.Button(control_frame, text="Load Roughness Texture", command=self.load_roughness).grid(row=0, column=1, padx=5)
        tk.Button(control_frame, text="Load AO Texture", command=self.load_ao).grid(row=0, column=2, padx=5)

        # Blend Strength
        tk.Label(control_frame, text="Blend Strength (%)").grid(row=1, column=0, sticky="e")
        self.blend_slider = tk.Scale(control_frame, from_=0, to=100, resolution=0.5,
                                     orient=tk.HORIZONTAL, length=300, command=lambda e: self.update_preview())
        self.blend_slider.set(self.defaults['blend'])
        self.blend_slider.grid(row=1, column=1, padx=5)
        tk.Button(control_frame, text="Reset", command=self.reset_blend).grid(row=1, column=2, padx=5)

        # Roughness Contrast
        tk.Label(control_frame, text="Roughness Contrast (%)").grid(row=2, column=0, sticky="e")
        self.contrast_slider = tk.Scale(control_frame, from_=10, to=300, resolution=1,
                                        orient=tk.HORIZONTAL, length=300, command=lambda e: self.update_preview())
        self.contrast_slider.set(self.defaults['contrast'])
        self.contrast_slider.grid(row=2, column=1, padx=5)
        tk.Button(control_frame, text="Reset", command=self.reset_contrast).grid(row=2, column=2, padx=5)

        # Reduce Whites
        tk.Label(control_frame, text="Reduce Whites in Roughness").grid(row=3, column=0, sticky="e")
        self.whites_slider = tk.Scale(control_frame, from_=0, to=255, resolution=1,
                                      orient=tk.HORIZONTAL, length=300, command=lambda e: self.update_preview())
        self.whites_slider.set(self.defaults['whites'])
        self.whites_slider.grid(row=3, column=1, padx=5)
        tk.Button(control_frame, text="Reset", command=self.reset_whites).grid(row=3, column=2, padx=5)

        # Dark Shading
        tk.Label(control_frame, text="Dark Shading Intensity (%)").grid(row=4, column=0, sticky="e")
        self.dark_slider = tk.Scale(control_frame, from_=0, to=100, resolution=0.5,
                                    orient=tk.HORIZONTAL, length=300, command=lambda e: self.update_preview())
        self.dark_slider.set(self.defaults['dark'])
        self.dark_slider.grid(row=4, column=1, padx=5)
        tk.Button(control_frame, text="Reset", command=self.reset_dark).grid(row=4, column=2, padx=5)

        # White Shading
        tk.Label(control_frame, text="White Shading Intensity (%)").grid(row=5, column=0, sticky="e")
        self.white_slider = tk.Scale(control_frame, from_=0, to=100, resolution=0.5,
                                     orient=tk.HORIZONTAL, length=300, command=lambda e: self.update_preview())
        self.white_slider.set(self.defaults['white'])
        self.white_slider.grid(row=5, column=1, padx=5)
        tk.Button(control_frame, text="Reset", command=self.reset_white).grid(row=5, column=2, padx=5)

        # Invert Roughness
        self.invert_var = tk.IntVar(value=int(self.defaults['invert']))
        self.invert_check = tk.Checkbutton(control_frame, text="Invert Roughness",
                                           variable=self.invert_var, command=self.update_preview)
        self.invert_check.grid(row=6, column=1)
        tk.Button(control_frame, text="Reset", command=self.reset_invert).grid(row=6, column=2, padx=5)

        # Preview Resolution Slider
        tk.Label(control_frame, text="Preview Resolution").grid(row=7, column=0, sticky="e")
        self.res_slider = tk.Scale(control_frame, from_=16, to=256, resolution=16,
                                   orient=tk.HORIZONTAL, length=300, command=lambda e: self.change_preview_res())
        self.res_slider.set(self.defaults['preview_res'])
        self.res_slider.grid(row=7, column=1, padx=5)
        tk.Button(control_frame, text="Reset", command=self.reset_resolution).grid(row=7, column=2, padx=5)

        # Save & Reset All
        tk.Button(control_frame, text="Save Baked Texture", command=self.save_output).grid(row=8, column=1, pady=10)
        tk.Button(control_frame, text="Reset All", command=self.reset_all).grid(row=8, column=2)

        # Log area
        log_frame = tk.Frame(root)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        tk.Label(log_frame, text="Log:").pack(anchor="w")
        self.log_text = tk.Text(log_frame, height=6, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)

    # Reset functions per control
    def reset_blend(self):
        self.blend_slider.set(self.defaults['blend']); self.update_preview()
    def reset_contrast(self):
        self.contrast_slider.set(self.defaults['contrast']); self.update_preview()
    def reset_whites(self):
        self.whites_slider.set(self.defaults['whites']); self.update_preview()
    def reset_dark(self):
        self.dark_slider.set(self.defaults['dark']); self.update_preview()
    def reset_white(self):
        self.white_slider.set(self.defaults['white']); self.update_preview()
    def reset_invert(self):
        self.invert_var.set(int(self.defaults['invert'])); self.update_preview()
    def reset_resolution(self):
        self.res_slider.set(self.defaults['preview_res']); self.generate_thumbnails(); self.update_preview()
    def reset_all(self):
        self.reset_blend(); self.reset_contrast(); self.reset_whites()
        self.reset_dark(); self.reset_white(); self.reset_invert()
        self.reset_resolution(); self.log("All settings reset")

    def log(self, msg):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def load_base(self):
        path = filedialog.askopenfilename(filetypes=[("Image Files","*.png;*.jpg;*.tga")])
        if path:
            try:
                self.base_image = Image.open(path).convert("RGB")
                self.log(f"Loaded base: {os.path.basename(path)}")
                self.generate_thumbnails()
                self.update_preview()
            except Exception as e:
                self.log(f"Error loading base: {e}")

    def load_roughness(self):
        path = filedialog.askopenfilename(filetypes=[("Image Files","*.png;*.jpg;*.tga")])
        if path:
            try:
                self.rough_image = Image.open(path).convert("L")
                self.log(f"Loaded roughness: {os.path.basename(path)}")
                self.generate_thumbnails()
                self.update_preview()
            except Exception as e:
                self.log(f"Error loading roughness: {e}")

    def load_ao(self):
        path = filedialog.askopenfilename(filetypes=[("Image Files","*.png;*.jpg;*.tga")])
        if path:
            try:
                self.ao_image = Image.open(path).convert("L")
                self.log(f"Loaded AO: {os.path.basename(path)}")
                self.generate_thumbnails()
                self.update_preview()
            except Exception as e:
                self.log(f"Error loading AO: {e}")

    def generate_thumbnails(self):
        res = self.res_slider.get()
        if self.base_image: self.base_thumb = self.base_image.resize((res,res), Image.NEAREST)
        if self.rough_image: self.rough_thumb = self.rough_image.resize((res,res), Image.NEAREST)
        if self.ao_image: self.ao_thumb = self.ao_image.resize((res,res), Image.NEAREST)

    def change_preview_res(self):
        self.generate_thumbnails()
        self.update_preview()
        self.log(f"Preview resolution set to {self.res_slider.get()}")

    def update_preview(self):
        if not (self.base_thumb and self.rough_thumb): return
        try:
            base = self.base_thumb.copy()
            rough = self.rough_thumb.copy()
            # Contrast & reduce whites
            rough = ImageEnhance.Contrast(rough).enhance(self.contrast_slider.get()/100.0)
            rough = rough.point(lambda p: min(p,255-self.whites_slider.get()))
            # Masks
            dark_mask = ImageOps.invert(rough)
            white_mask = rough
            highlights = rough if self.invert_var.get() else ImageOps.invert(rough)
            # Scale masks
            blend = self.blend_slider.get()/100.0
            high_m = highlights.point(lambda p:int(p*blend))
            dark_m = dark_mask.point(lambda p:int(p*self.dark_slider.get()/100.0))
            white_m = white_mask.point(lambda p:int(p*self.white_slider.get()/100.0))
            # Composite low-res
            res_img = base.copy()
            res_img = Image.composite(Image.new("RGB",base.size,(255,255,255)),res_img,high_m)
            res_img = Image.composite(Image.new("RGB",base.size,(0,0,0)),res_img,dark_m)
            res_img = Image.composite(Image.new("RGB",base.size,(0,0,0)),res_img,white_m)
            # AO
            if self.ao_thumb:
                ao_rgb = Image.merge("RGB",[self.ao_thumb]*3)
                res_img = ImageChops.multiply(res_img, ao_rgb)
            # Upscale preview
            display = res_img.resize((256,256),Image.NEAREST)
            before = self.base_thumb.resize((256,256),Image.NEAREST)
            self.before_tk = ImageTk.PhotoImage(before)
            self.after_tk = ImageTk.PhotoImage(display)
            self.preview_before.config(image=self.before_tk)
            self.preview_after.config(image=self.after_tk)
        except Exception as e:
            self.log(f"Preview error: {e}")

    def save_output(self):
        if not self.base_image or not self.rough_image:
            self.log("Incomplete inputs"); return
        base = self.base_image.copy()
        rough = self.rough_image.copy()
        rough = ImageEnhance.Contrast(rough).enhance(self.contrast_slider.get()/100.0)
        rough = rough.point(lambda p: min(p,255-self.whites_slider.get()))
        dark_mask = ImageOps.invert(rough)
        white_mask = rough
        highlights = rough if self.invert_var.get() else ImageOps.invert(rough)
        blend = self.blend_slider.get()/100.0
        high_m = highlights.point(lambda p:int(p*blend))
        dark_m = dark_mask.point(lambda p:int(p*self.dark_slider.get()/100.0))
        white_m = white_mask.point(lambda p:int(p*self.white_slider.get()/100.0))
        result = Image.composite(Image.new("RGB",base.size,(255,255,255)),base,high_m)
        result = Image.composite(Image.new("RGB",base.size,(0,0,0)),result,dark_m)
        result = Image.composite(Image.new("RGB",base.size,(0,0,0)),result,white_m)
        if self.ao_image:
            ao_rgb = Image.merge("RGB",[self.ao_image]*3)
            result = ImageChops.multiply(result, ao_rgb)
        path = filedialog.asksaveasfilename(defaultextension=".png",filetypes=[("PNG","*.png")])
        if path:
            result.save(path)
            self.log(f"Saved baked: {os.path.basename(path)}")

if __name__ == "__main__":
    root = tk.Tk()
    from PIL import Image
    app = FakePBRBaker(root)
    root.mainloop()
