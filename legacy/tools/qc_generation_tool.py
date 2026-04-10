"""
QC Generation Tool - Generate QC files for Source models.
"""

import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .base_tool import BaseTool, register_tool
from .utils import PlaceholderEntry, determine_surfaceprop, save_config


def generate_single_qc(smd_path, model_prefix, materials_path, surface, fps, append_collision):
    """Generate a QC file for a single SMD file."""
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
    """Generate QC files for all SMD files in a folder."""
    cnt = 0
    for r, _, files in os.walk(folder):
        for fn in files:
            if not fn.lower().endswith(".smd"):
                continue
            base, _ = os.path.splitext(fn)
            surface_final = surface if surface and surface != "default" else determine_surfaceprop(base)
            content = (
                f'$modelname "{model_prefix}/{base}.mdl"\n'
                f'$body {base} "{base}.smd"\n\n'
                '$staticprop\n'
                '$contents "solid"\n'
                f'$surfaceprop "{surface_final}"\n'
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
        self.entry_surface.insert(0, str(config.get("qcgen_surface", "")))
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


@register_tool
class QcGenerationTool(BaseTool):
    @property
    def name(self) -> str:
        return "QC Generation"
    
    @property
    def description(self) -> str:
        return "Generate QC files for Source models"
    
    @property
    def dependencies(self) -> list:
        return []  # No special dependencies
    
    def create_tab(self, parent) -> ttk.Frame:
        return QcGenTab(parent, self.config)
