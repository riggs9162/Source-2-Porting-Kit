import os
import re
import json
import shutil
import subprocess
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

from PIL import Image
import VTFLibWrapper.VTFLib as VTFLib
import VTFLibWrapper.VTFLibEnums as VTFLibEnums

# Optional drag-and-drop support
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False

CONFIG_FILE = "config.json"
DIGIT_SUFFIX_PATTERN = re.compile(r"^(?P<base>.+)\.(?P<digits>\d{3})\.smd$", re.IGNORECASE)
CONTENT_SUFFIX_PATTERN = re.compile(r"(?P<name>\b[\w\d\-_]+)\.\d{3}\b")

# ─── UTILITIES ────────────────────────────────────────────────────────────────

def force_utf8():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8") # type: ignore

def load_config():
    if os.path.isfile(CONFIG_FILE):
        try:
            return json.load(open(CONFIG_FILE, "r", encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_config(cfg):
    try:
        json.dump(cfg, open(CONFIG_FILE, "w", encoding="utf-8"), indent=2)
    except Exception as e:
        print("[WARN] Could not save config:", e)

# ─── AUTO-SURFACEPROP ─────────────────────────────────────────────────────────

_SURFACE_KEYWORDS = {
    'brick':    'brick',
    'concrete': 'concrete',
    'dirt':     'dirt',
    'glass':    'glass',
    'grass':    'grass',
    'gravel':   'gravel',
    'metal':    'metal',
    'plaster':  'plaster',
    'sand':     'sand',
    'tile':     'tile',
    'water':    'water',
    'wood':     'wood',
}

def determine_surfaceprop(name: str) -> str:
    ln = name.lower()
    for keyword, prop in _SURFACE_KEYWORDS.items():
        if ln.startswith(keyword) or ln.endswith(keyword) or keyword in ln:
            return prop
    return 'default'

# ─── PLAIN ENTRY WITH DND ────────────────────────────────────────────────────

class PlaceholderEntry(ttk.Entry):
    """
    Plain ttk.Entry. Supports drag-and-drop if tkinterdnd2 is available.
    """
    def __init__(self, master=None, **kwargs):
        super().__init__(master, **kwargs)
        if DND_AVAILABLE:
            try:
                self.tk.call('tkdnd::drop_target', 'register', self, DND_FILES)
                self.bind("<<Drop>>", self._on_drop)
            except Exception as e:
                print(f"[WARN] Drag-and-drop initialization failed: {e}")

    def get_real(self):
        return super().get()

    def _on_drop(self, event):
        if DND_AVAILABLE:
            path = event.data.split()[0].strip("{}")
            self.delete(0, tk.END)
            self.insert(0, path)

# ─── GUI HELPERS ─────────────────────────────────────────────────────────────

def browse_folder(entry: PlaceholderEntry):
    p = filedialog.askdirectory()
    if p:
        entry.delete(0, tk.END)
        entry.insert(0, p)

def browse_file(entry: PlaceholderEntry, filetypes):
    p = filedialog.askopenfilename(filetypes=filetypes)
    if p:
        entry.delete(0, tk.END)
        entry.insert(0, p)

# ─── TASK 1: TEXTURE CONVERSION ───────────────────────────────────────────────

def convert_png_to_vtf(vtf_lib, png_src, vtf_dst, clamp):
    try:
        img = Image.open(png_src).convert("RGBA")
    except Exception as e:
        print(f"[ERROR] Opening {png_src}: {e}")
        return False
    w,h = img.size
    if clamp and max(w,h)>clamp:
        scale = clamp/float(max(w,h))
        img = img.resize((int(w*scale),int(h*scale)), Image.Resampling.LANCZOS)
        w,h = img.size
    data = img.tobytes()
    amin,amax = img.getchannel("A").getextrema()
    fmt = (VTFLibEnums.ImageFormat.ImageFormatDXT1
           if (amin==255 and amax==255)
           else VTFLibEnums.ImageFormat.ImageFormatDXT5)
    opts = vtf_lib.create_default_params_structure()
    opts.ImageFormat = fmt
    opts.Flags       = VTFLibEnums.ImageFlag.ImageFlagEightBitAlpha
    opts.Resize      = 1
    vtf_lib.image_create_single(w,h,data,opts)
    vtf_lib.image_save(vtf_dst)
    return True

def _extract_base_name(filename: str) -> str:
    """Return the texture name without the Source 2 suffixes."""
    name = os.path.splitext(os.path.basename(filename))[0]
    m = re.match(r"(.*?)(?:_color_|_normal_).*", name, re.IGNORECASE)
    return m.group(1) if m else name


def texture_conversion_folder(input_dir, output_dir, material_path, clamp, vmt_type, surface_prop, extra_params=None):
    if not os.path.isdir(input_dir):
        messagebox.showerror("Textures", f"Input not found:\n{input_dir}")
        return
    if not output_dir.strip():
        messagebox.showerror("Textures", "Select an output folder.")
        return
    os.makedirs(output_dir, exist_ok=True)

    vtf_lib = VTFLib.VTFLib()
    total_converted = 0

    for root, dirs, files in os.walk(input_dir):
        rel_path = os.path.relpath(root, input_dir)
        out_dir = os.path.join(output_dir, rel_path) if rel_path != "." else output_dir
        os.makedirs(out_dir, exist_ok=True)

        grouped = {}
        for fn in files:
            if not fn.lower().endswith(".png"):
                continue
            ln = fn.lower()
            if ("_color_" in ln or "_color2_" in ln) and "_orm_" not in ln:
                base = _extract_base_name(fn)
                grouped.setdefault(base, {})["color"] = fn
            elif "_normal_" in ln:
                base = _extract_base_name(fn)
                grouped.setdefault(base, {})["normal"] = fn

        for base, m in grouped.items():
            col = m.get("color")
            if not col:
                continue
            nm = m.get("normal")
            bname = os.path.basename(base)
            vtf_c = os.path.join(out_dir, bname + ".vtf")
            vtf_n = os.path.join(out_dir, bname + "_normal.vtf") if nm else None
            vmt = os.path.join(out_dir, bname + ".vmt")
            vmt_type_final = vmt_type if vmt_type in ["VertexLitGeneric", "LightmappedGeneric", "UnlitGeneric", "WorldVertexTransition"] else "VertexLitGeneric"
            surface_prop_final = surface_prop if surface_prop and surface_prop != "default" else determine_surfaceprop(bname)
            if os.path.exists(vtf_c) and os.path.exists(vmt):
                continue
            if not convert_png_to_vtf(vtf_lib, os.path.join(root, col), vtf_c, clamp):
                continue
            if nm:
                convert_png_to_vtf(vtf_lib, os.path.join(root, nm), vtf_n, clamp)
            with open(vmt, "w", encoding="utf-8") as f:
                f.write(f'"{vmt_type_final}"\n{{\n')
                # Compute material path relative to output_dir
                rel_mat_path = os.path.relpath(out_dir, output_dir).replace("\\", "/")
                mat_path = f"{material_path}/{rel_mat_path}" if rel_mat_path != "." else material_path
                mat_path = mat_path.rstrip("/")
                f.write(f'    "$basetexture" "{mat_path}/{bname}"\n')
                if nm:
                    f.write(f'    "$bumpmap" "{mat_path}/{bname}_normal"\n')
                f.write(f'    "$basetexturetransform" "center 0 0 scale 4 4 rotate 0 translate 0 0"\n\n')
                f.write(f'    "$surfaceprop" "{surface_prop_final}"\n\n')
                if vmt_type_final == "VertexLitGeneric":
                    f.write('    "$model" "1"\n\n')
                if extra_params:
                    for k, v in extra_params.items():
                        f.write(f'    "${k}" "{v}"\n')
                f.write("}\n")
            total_converted += 1

    messagebox.showinfo("Textures", f"Texture conversion complete.\nConverted {total_converted} textures to VTF/VMT in {output_dir}.")

# ─── TASK 6: QC GENERATION ────────────────────────────────────────────────────

def generate_single_qc(smd_path, model_prefix, materials_path, surface, fps, append_collision):
    d, f = os.path.split(smd_path)
    base, _ = os.path.splitext(f)
    mdl = f"{model_prefix}/{base}.mdl"
    if (surface == "default" or not surface):
        surface = determine_surfaceprop(base)
    content = (
        f'$modelname "{mdl}"\n'
        f'$body {base} "{base}.smd"\n\n'
        '$staticprop\n'
        '$contents "solid"\n'
        f'$surfaceprop "{surface}"\n'
        '$illumposition 0 0 0\n\n'
        f'$cdmaterials "{materials_path}/"\n\n'
        f'$sequence {base} "{base}.smd" fps {fps}\n\n'
    )
    if append_collision:
        content += (
            f'$collisionmodel "{base}.smd"\n'
            "{\n"
            "    $concave\n"
            "    $automass\n"
            "    $inertia 1\n"
            "    $damping 0\n"
            "    $rotdamping 0\n"
            "}\n"
        )
    qc = os.path.join(d, base + ".qc")
    try:
        with open(qc, "w", encoding="utf-8") as file:
            file.write(content)
        messagebox.showinfo("QC Gen", f"Created {qc}")
    except Exception as e:
        messagebox.showerror("QC Gen", str(e))

def generate_qc_batch(folder, model_prefix, materials_path, surface, fps, append_collision):
    cnt = 0
    for r, _, files in os.walk(folder):
        for fn in files:
            if not fn.lower().endswith(".smd"):
                continue
            base, _ = os.path.splitext(fn)
            if (surface == "default" or not surface):
                surface = determine_surfaceprop(base)
            content = (
                f'$modelname "{model_prefix}/{base}.mdl"\n'
                f'$body {base} "{base}.smd"\n\n'
                '$staticprop\n'
                '$contents "solid"\n'
                f'$surfaceprop "{surface}"\n'
                '$illumposition 0 0 0\n\n'
                f'$cdmaterials "{materials_path}/"\n\n'
                f'$sequence {base} "{base}.smd" fps {fps}\n\n'
            )
            if append_collision:
                content += (
                    f'$collisionmodel "{base}.smd"\n'
                    "{\n"
                    "    $concave\n"
                    "    $automass\n"
                    "    $inertia 1\n"
                    "    $damping 0\n"
                    "    $rotdamping 0\n"
                    "}\n"
                )
            qc = os.path.join(r, base + ".qc")
            try:
                with open(qc, "w", encoding="utf-8") as file:
                    file.write(content)
                cnt += 1
            except Exception as e:
                print(f"[ERROR] Could not create QC for {fn}: {e}")
    messagebox.showinfo("QC Gen", f"Batch created {cnt} files.")

# ─── GUI TABS ──────────────────────────────────────────────────────────────────

class TextureTab(ttk.Frame):
    def __init__(self, parent, config):
        super().__init__(parent)
        self.config = config

        # Input folder
        ttk.Label(self, text="Input Folder:").grid(row=0, column=0, pady=5, padx=5, sticky="e")
        self.entry_input = PlaceholderEntry(self, width=50)
        self.entry_input.insert(0, config.get("texture_input", ""))
        self.entry_input.grid(row=0, column=1, pady=5, padx=5, sticky="ew")
        ttk.Button(self, text="Browse…", command=lambda: browse_folder(self.entry_input)).grid(row=0, column=2, padx=5)

        # Output folder
        ttk.Label(self, text="Output Folder:").grid(row=1, column=0, pady=5, padx=5, sticky="e")
        self.entry_output = PlaceholderEntry(self, width=50)
        self.entry_output.insert(0, config.get("texture_output", ""))
        self.entry_output.grid(row=1, column=1, pady=5, padx=5, sticky="ew")
        ttk.Button(self, text="Browse…", command=lambda: browse_folder(self.entry_output)).grid(row=1, column=2, padx=5)

        # Material path
        ttk.Label(self, text="Material Path:").grid(row=2, column=0, pady=5, padx=5, sticky="e")
        self.entry_material = PlaceholderEntry(self, width=50)
        self.entry_material.insert(0, config.get("texture_material", ""))
        self.entry_material.grid(row=2, column=1, pady=5, padx=5, sticky="ew")

        # Clamp size
        ttk.Label(self, text="Clamp Size:").grid(row=3, column=0, pady=5, padx=5, sticky="e")
        self.entry_clamp = PlaceholderEntry(self, width=50)
        self.entry_clamp.insert(0, str(config.get("texture_clamp", 0)))
        self.entry_clamp.grid(row=3, column=1, sticky="w", padx=5, pady=5)

        # Shader type
        ttk.Label(self, text="VMT Type:").grid(row=4, column=0, pady=5, padx=5, sticky="e")
        self.entry_shader = PlaceholderEntry(self, width=50)
        self.entry_shader.insert(0, str(config.get("texture_shader", "VertexLitGeneric")))
        self.entry_shader.grid(row=4, column=1, sticky="w", padx=5, pady=5)
        ttk.Label(self, text="(VertexLitGeneric, LightmappedGeneric, UnlitGeneric, WorldVertexTransition)").grid(row=4, column=2, sticky="w", padx=5)

        # Surface property
        ttk.Label(self, text="Surface Property:").grid(row=5, column=0, pady=5, padx=5, sticky="e")
        self.entry_surface = PlaceholderEntry(self, width=50)
        self.entry_surface.insert(0, str(config.get("texture_surface", "")))
        self.entry_surface.grid(row=5, column=1, sticky="w", padx=5, pady=5)
        ttk.Label(self, text="(default, concrete, metal, wood, glass, brick, dirt, tile, grass, water)").grid(row=5, column=2, sticky="w", padx=5)

        # Extra parameters (optional)
        ttk.Label(self, text="Extra Parameters (JSON):").grid(row=6, column=0, pady=5, padx=5, sticky="e")
        self.extra_params_text = ScrolledText(self, width=50, height=10)
        self.extra_params_text.insert(tk.END, json.dumps(config.get("texture_extra_params", {}), indent=2))
        self.extra_params_text.grid(row=6, column=1, pady=5, padx=5, sticky="ew")

        # Extra parameters info
        ttk.Label(self, text='Optional parameters for VMT. Example:\n{\n\t"envmap": "env_cubemap",\n\n\t"phong": "1",\n\t"phongboost": "2"\n}').grid(row=6, column=2, columnspan=3, sticky="w", padx=5)

        # Run button
        ttk.Button(self, text="Convert Textures", command=self.on_run).grid(row=9, column=0, columnspan=3, pady=10)
        self.columnconfigure(1, weight=1)

    def on_run(self):
        input_dir = self.entry_input.get_real().strip()
        output_dir = self.entry_output.get_real().strip()
        material_path = self.entry_material.get_real().strip()
        shader = self.entry_shader.get_real().strip()
        surface = self.entry_surface.get_real().strip()
        try:
            clamp = int(self.entry_clamp.get().strip())
        except ValueError:
            return messagebox.showerror("Textures", "Clamp size must be an integer.")
        try:
            extra_params = json.loads(self.extra_params_text.get("1.0", tk.END).strip())
        except json.JSONDecodeError:
            return messagebox.showerror("Textures", "Invalid JSON in Extra Parameters.")
        if not (input_dir and output_dir and material_path):
            return messagebox.showerror("Textures", "Fill in all fields.")
        self.config["texture_input"] = input_dir
        self.config["texture_output"] = output_dir
        self.config["texture_material"] = material_path
        self.config["texture_clamp"] = clamp
        self.config["texture_shader"] = shader
        self.config["texture_surface"] = surface
        self.config["texture_extra_params"] = extra_params
        save_config(self.config)
        texture_conversion_folder(input_dir, output_dir, material_path, clamp, shader, surface, extra_params)

class QcGenTab(ttk.Frame):
    def __init__(self, parent, config):
        super().__init__(parent)
        self.config = config

        # Path entry
        ttk.Label(self, text="SMD/.QC or Folder:").grid(row=0, column=0, pady=5, padx=5, sticky="e")
        self.entry_path = PlaceholderEntry(self, width=50)
        self.entry_path.insert(0, config.get("qcgen_path", ""))
        self.entry_path.grid(row=0, column=1, pady=5, padx=5, sticky="ew")
        ttk.Button(self, text="Browse…", command=self.browse_any).grid(row=0, column=2, padx=5)

        # Model prefix
        ttk.Label(self, text="Model Prefix:").grid(row=1, column=0, sticky="e", padx=5, pady=5)
        self.entry_modelname = PlaceholderEntry(self, width=50)
        self.entry_modelname.insert(0, config.get("qcgen_modelname", ""))
        self.entry_modelname.grid(row=1, column=1, padx=5, pady=5, sticky="ew")

        # Materials path
        ttk.Label(self, text="Materials Path:").grid(row=2, column=0, sticky="e", padx=5, pady=5)
        self.entry_materials = PlaceholderEntry(self, width=50)
        self.entry_materials.insert(0, config.get("qcgen_materials", ""))
        self.entry_materials.grid(row=2, column=1, padx=5, pady=5, sticky="ew")
        ttk.Label(self, text="VertexLitGeneric: models/your/path").grid(row=2, column=2, sticky="w", padx=5)
        ttk.Label(self, text="LightmappedGeneric: your/path", anchor="w").grid(row=2, column=3, sticky="w", padx=5)

        # Surface property
        ttk.Label(self, text="Surface Property:").grid(row=3, column=0, pady=5, padx=5, sticky="e")
        self.entry_surface = PlaceholderEntry(self, width=50)
        self.entry_surface.insert(0, str(config.get("texture_surface", "")))
        self.entry_surface.grid(row=3, column=1, sticky="w", padx=5, pady=5)
        ttk.Label(self, text="(default, concrete, metal, wood, glass, brick, dirt, tile, grass, water)").grid(row=3, column=2, sticky="w", padx=5)

        # FPS
        ttk.Label(self, text="FPS:").grid(row=4, column=0, sticky="e", padx=5, pady=5)
        self.entry_fps = tk.Entry(self, width=10)
        self.entry_fps.insert(0, str(config.get("qcgen_fps", 1)))
        self.entry_fps.grid(row=4, column=1, sticky="w", padx=5, pady=5)

        # Collision checkbox
        self.collision_var = tk.BooleanVar(value=config.get("qcgen_collision", True))
        ttk.Checkbutton(self, text="Append Collision Logic", variable=self.collision_var).grid(row=5, column=0, columnspan=3, pady=5)

        # Generate button
        ttk.Button(self, text="Generate QC(s)", command=self.on_run).grid(row=6, column=0, columnspan=3, pady=10)
        self.columnconfigure(1, weight=1)

    def browse_any(self):
        p = filedialog.askopenfilename(filetypes=[("SMD", "*.smd"), ("QC", "*.qc"), ("All", "*.*")])
        if not p:
            p = filedialog.askdirectory()
        if p:
            self.entry_path.delete(0, tk.END)
            self.entry_path.insert(0, p)

    def on_run(self):
        p = self.entry_path.get_real().strip()
        mp = self.entry_modelname.get_real().strip()
        mpat = self.entry_materials.get_real().strip()
        surface = self.entry_surface.get_real().strip()
        try:
            fps = int(self.entry_fps.get().strip())
        except ValueError:
            return messagebox.showerror("QC Gen", "FPS must be an integer.")
        if not (p and mp and mpat):
            return messagebox.showerror("QC Gen", "Fill in all fields.")
        self.config["qcgen_path"] = p
        self.config["qcgen_modelname"] = mp
        self.config["qcgen_materials"] = mpat
        self.config["qcgen_fps"] = fps
        self.config["qcgen_collision"] = self.collision_var.get()
        self.config["qcgen_surface"] = surface
        save_config(self.config)
        append_collision = self.collision_var.get()
        if p.lower().endswith(".smd"):
            generate_single_qc(p, mp, mpat, surface, fps, append_collision)
        elif p.lower().endswith(".qc"):
            generate_qc_batch(os.path.dirname(p), mp, mpat, surface, fps, append_collision)
        else:
            generate_qc_batch(p, mp, mpat, surface, fps, append_collision)

# ─── MAIN APP ────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Source 2 Porting Kit")
        self.geometry("1200x800")
        force_utf8()

        self.config_data = load_config()
        nb = ttk.Notebook(self); nb.pack(fill="both",expand=True)

        nb.add(TextureTab(nb,      self.config_data), text="Textures → VTF/VMT")
        nb.add(QcGenTab(nb,        self.config_data), text="QC Generation")

        self.protocol("WM_DELETE_WINDOW", lambda: (save_config(self.config_data), self.destroy()))

if __name__ == "__main__":
    if DND_AVAILABLE:
        root = TkinterDnD.Tk()
        force_utf8()
        app = App()
        root.destroy()
        app.mainloop()
    else:
        App().mainloop()
