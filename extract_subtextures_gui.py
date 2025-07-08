import os
import sys
import json
import tempfile
import subprocess
import math
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk, ImageChops

class Region:
    def __init__(self, name, x, y, w, h):
        self.name = name
        self.x = x
        self.y = y
        self.w = w
        self.h = h

    def to_dict(self):
        return {"name": self.name, "x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @classmethod
    def from_dict(cls, d):
        return cls(d["name"], d["x"], d["y"], d["w"], d["h"])

    def __str__(self):
        return f"{self.name}: ({self.x}, {self.y}, {self.w}, {self.h})"

class ExtractSubtexturesGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Extract Subtextures GUI")
        self.geometry("1200x800")
        self.regions = []
        self.image = None
        self.photo = None
        self.scale = 1.0
        self.rect_id = None
        self.start_x = self.start_y = 0
        # Snap grid options
        self.snap_sizes = [1,2,4,8,16,32,64,128,256,512,1024]
        self.snap_index = tk.IntVar(value=1)  # default snap = 2 px
        self._build_ui()

    def _build_ui(self):
        # Left control panel
        ctrl = ttk.Frame(self)
        ctrl.pack(side="left", fill="y", padx=5, pady=5)

        # Instructions
        inst = ("Instructions:\n"
                "1) Load PNG.\n"
                "2) Drag on image to select region.\n"
                "3) Adjust Snap, X, Y, W, H.\n"
                "4) Name region and click 'Add Region'.\n"
                "5) Save mesh or Export All.")
        ttk.Label(ctrl, text=inst, justify="left", wraplength=200).pack(fill="x", pady=(0,10))

        # Load & Output
        ttk.Button(ctrl, text="Load PNG...", command=self.load_image).pack(fill="x")
        ttk.Button(ctrl, text="Select Output...", command=self.select_output).pack(fill="x", pady=(5,0))

        # Clamp size
        ttk.Label(ctrl, text="Clamp Size (px):").pack(pady=(10,0))
        self.clamp_var = tk.IntVar(value=0)
        ttk.Entry(ctrl, textvariable=self.clamp_var, width=10).pack()

        # Filename suffix
        ttk.Label(ctrl, text="Filename Suffix:").pack(pady=(10,0))
        self.suffix_var = tk.StringVar(value="")
        ttk.Entry(ctrl, textvariable=self.suffix_var).pack(fill="x")

        # Snap grid
        ttk.Label(ctrl, text="Snap Grid (X/Y) [px]:").pack(pady=(10,0))
        snap_frame = ttk.Frame(ctrl)
        snap_frame.pack(fill="x")
        self.snap_scale = tk.Scale(
            snap_frame, from_=0, to=len(self.snap_sizes)-1,
            orient="horizontal", showvalue=False, command=self._on_snap_change
        )
        self.snap_scale.set(self.snap_index.get())
        self.snap_scale.pack(side="left", fill="x", expand=True)
        self.snap_label = ttk.Label(snap_frame, text=f"{self.snap_sizes[self.snap_index.get()]} px")
        self.snap_label.pack(side="right")

        ttk.Separator(ctrl, orient="horizontal").pack(fill="x", pady=10)

        # Mesh import/export
        ttk.Button(ctrl, text="Save Mesh...", command=self.save_mesh).pack(fill="x")
        ttk.Button(ctrl, text="Load Mesh...", command=self.load_mesh).pack(fill="x", pady=(0,5))

        # Region list
        ttk.Label(ctrl, text="Regions:").pack()
        self.listbox = tk.Listbox(ctrl, width=30, height=12)
        self.listbox.pack()
        self.listbox.bind("<<ListboxSelect>>", self.on_region_select)

        # Region name input
        name_frame = ttk.Frame(ctrl)
        name_frame.pack(fill="x", pady=(5,0))
        ttk.Label(name_frame, text="Name:").pack(side="left")
        self.name_var = tk.StringVar()
        ttk.Entry(name_frame, textvariable=self.name_var).pack(side="right", fill="x", expand=True)

        ttk.Button(ctrl, text="Add Region", command=self.add_region).pack(fill="x", pady=(5,0))
        ttk.Button(ctrl, text="Remove Region", command=self.remove_region).pack(fill="x")

        ttk.Separator(ctrl, orient="horizontal").pack(fill="x", pady=10)

        # Sliders for X, Y, W, H
        for label, var in [("X", "x"), ("Y", "y"), ("W", "w"), ("H", "h")]:
            ttk.Label(ctrl, text=label).pack()
            scale = tk.Scale(
                ctrl, from_=0, to=1000, orient="horizontal",
                command=lambda val, v=var: self.on_slider_change(v, val)
            )
            scale.pack(fill="x")
            setattr(self, f"{var}_slider", scale)

        ttk.Button(ctrl, text="Export All", command=self.export_all).pack(fill="x", pady=10)

        # Canvas for image
        self.canvas = tk.Canvas(self, bg="black")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self.on_mouse_down)
        self.canvas.bind("<B1-Motion>", self.on_mouse_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_mouse_up)

        # Preview panel
        preview_frame = ttk.Frame(self)
        preview_frame.pack(side="right", fill="y", padx=5, pady=5)
        ttk.Label(preview_frame, text="Preview:").pack()
        self.preview_label = ttk.Label(preview_frame)
        self.preview_label.pack()

    def _on_snap_change(self, val):
        idx = int(float(val))
        self.snap_index.set(idx)
        self.snap_label.config(text=f"{self.snap_sizes[idx]} px")

    def load_image(self):
        path = filedialog.askopenfilename(filetypes=[("PNG images","*.png"),("All files","*.*")])
        if not path:
            return
        self.img_path = path
        self.image = Image.open(path)
        self._refresh_canvas()

    def select_output(self):
        d = filedialog.askdirectory()
        if d:
            self.output_dir = d

    def _refresh_canvas(self):
        # Fit image
        cw = self.canvas.winfo_width() or 800
        ch = self.canvas.winfo_height() or 600
        iw, ih = self.image.size
        self.scale = min(cw/iw, ch/ih, 1.0)
        resized = self.image.resize((int(iw*self.scale), int(ih*self.scale)), Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(resized)
        self.canvas.delete("all")
        self.canvas.create_image(0,0,anchor="nw",image=self.photo)
        for reg in self.regions:
            self._draw_region(reg)

    def on_mouse_down(self, event):
        if not self.image:
            return
        self.start_x = int(event.x / self.scale)
        self.start_y = int(event.y / self.scale)
        if self.rect_id:
            self.canvas.delete(self.rect_id)
            self.rect_id = None

    def on_mouse_drag(self, event):
        if not self.image:
            return
        x1 = self.start_x*self.scale
        y1 = self.start_y*self.scale
        x2, y2 = event.x, event.y
        if self.rect_id:
            self.canvas.coords(self.rect_id, x1, y1, x2, y2)
        else:
            self.rect_id = self.canvas.create_rectangle(x1, y1, x2, y2, outline="red")

    def on_mouse_up(self, event):
        if not self.image or not self.rect_id:
            return
        x1 = self.start_x
        y1 = self.start_y
        x2 = int(event.x/self.scale)
        y2 = int(event.y/self.scale)
        x = min(x1,x2); y = min(y1,y2)
        w = abs(x2-x1); h = abs(y2-y1)
        # Snap X/Y
        snap = self.snap_sizes[self.snap_index.get()]
        x = (x//snap)*snap
        y = (y//snap)*snap
        # Snap W/H to power-of-two
        w = self._closest_pow2(w, self.image.size[0])
        h = self._closest_pow2(h, self.image.size[1])
        # Update sliders
        for var, val in [("x",x),("y",y),("w",w),("h",h)]:
            slider = getattr(self, f"{var}_slider")
            limit = self.image.size[0] if var in ("x","w") else self.image.size[1]
            slider.config(to=limit)
            slider.set(val)
        # Default name
        self.name_var.set(f"region_{len(self.regions)+1}")

    def _closest_pow2(self, val, maxval):
        exps = [2**e for e in range(1, int(math.log2(maxval))+1)]
        if not exps:
            return val
        return min(exps, key=lambda x: abs(x-val))

    def on_slider_change(self, var, val):
        if not self.rect_id or not self.image:
            return
        iv = int(float(val))
        if var in ("x","y"):
            snap = self.snap_sizes[self.snap_index.get()]
            iv = (iv//snap)*snap
            getattr(self, f"{var}_slider").set(iv)
        else:  # w/h
            iv = self._closest_pow2(iv, self.image.size[0] if var=="w" else self.image.size[1])
            getattr(self, f"{var}_slider").set(iv)
        x = self.x_slider.get(); y = self.y_slider.get()
        w = self.w_slider.get(); h = self.h_slider.get()
        x1, y1 = x*self.scale, y*self.scale
        x2, y2 = (x+w)*self.scale, (y+h)*self.scale
        self.canvas.coords(self.rect_id, x1, y1, x2, y2)

    def add_region(self):
        if not self.rect_id:
            messagebox.showerror("Error", "No selection to add.")
            return
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("Error", "Please specify a region name.")
            return
        x = self.x_slider.get(); y = self.y_slider.get()
        w = self.w_slider.get(); h = self.h_slider.get()
        reg = Region(name, x, y, w, h)
        self.regions.append(reg)
        self.listbox.insert(tk.END, str(reg))
        self.canvas.delete(self.rect_id)
        self.rect_id = None

    def remove_region(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        self.listbox.delete(idx)
        del self.regions[idx]

    def on_region_select(self, event):
        sel = self.listbox.curselection()
        if not sel:
            return
        reg = self.regions[sel[0]]
        for var in ("x","y","w","h"):
            slider = getattr(self, f"{var}_slider")
            limit = self.image.size[0] if var in ("x","w") else self.image.size[1]
            slider.config(to=limit)
            slider.set(getattr(reg, var))
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(
            reg.x*self.scale, reg.y*self.scale,
            (reg.x+reg.w)*self.scale, (reg.y+reg.h)*self.scale,
            outline="blue"
        )
        crop = self.image.crop((reg.x, reg.y, reg.x+reg.w, reg.y+reg.h))
        preview = crop.resize((200,200), Image.LANCZOS)
        self.preview_img = ImageTk.PhotoImage(preview)
        self.preview_label.config(image=self.preview_img)

    def save_mesh(self):
        if not self.regions:
            messagebox.showerror("Error", "No regions to save.")
            return
        path = filedialog.asksaveasfilename(
            title="Save Mesh…", defaultextension=".json",
            filetypes=[("JSON","*.json"),("All","*.*")]
        )
        if not path:
            return
        data = {"regions": [r.to_dict() for r in self.regions]}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        messagebox.showinfo("Saved", f"Mesh saved to {path}")

    def load_mesh(self):
        # Load a previously-saved JSON “mesh”
        path = filedialog.askopenfilename(
            title="Load Mesh…", filetypes=[("JSON files","*.json"),("All","*.*")]
        )
        if not path:
            return
        if not hasattr(self, "image") or self.image is None:
            messagebox.showerror("Error", "Load your source PNG first before importing a mesh.")
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            regs = [Region.from_dict(d) for d in data.get("regions", [])]
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load mesh:\n{e}")
            return

        # Clear existing
        self.regions.clear()
        self.listbox.delete(0, tk.END)
        self.canvas.delete("all")
        self._refresh_canvas()

        # Add loaded regions
        for reg in regs:
            self.regions.append(reg)
            self.listbox.insert(tk.END, str(reg))
            self._draw_region(reg)

        messagebox.showinfo("Loaded", f"{len(regs)} regions loaded from:\n{os.path.basename(path)}")

    def export_all(self):
        if not hasattr(self, "output_dir"):
            messagebox.showerror("Error", "Please select an output folder.")
            return
        if not self.regions:
            messagebox.showerror("Error", "No regions defined.")
            return
        clamp = self.clamp_var.get()
        suffix = self.suffix_var.get().strip()
        convert_script = os.path.join(os.path.dirname(__file__), "convert_image.py")
        if not os.path.isfile(convert_script):
            messagebox.showerror("Error", "convert_image.py not found.")
            return
        for reg in self.regions:
            crop = self.image.crop((reg.x, reg.y, reg.x+reg.w, reg.y+reg.h))
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            crop.save(tmp.name)
            tmp.close()
            name = reg.name + suffix
            out_vtf = os.path.join(self.output_dir, f"{name}.vtf")
            cmd = [sys.executable, convert_script, tmp.name, out_vtf, str(clamp)]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                messagebox.showwarning("Error", f"Conversion failed for {reg.name}: {proc.stderr.strip()}")
            os.unlink(tmp.name)
        messagebox.showinfo("Done", "Export complete.")

    def _draw_region(self, region, outline="blue"):
        x1, y1 = region.x*self.scale, region.y*self.scale
        x2, y2 = (region.x+region.w)*self.scale, (region.y+region.h)*self.scale
        self.canvas.create_rectangle(x1, y1, x2, y2, outline=outline)

if __name__ == "__main__":
    app = ExtractSubtexturesGUI()
    app.mainloop()
