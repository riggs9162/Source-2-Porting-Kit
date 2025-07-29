"""
Texture Conversion Tool - Convert Source 2 textures to VTF/VMT format.
"""

import os
import re
import json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from PIL import Image

# Try to import VTFLib for texture conversion
try:
    import VTFLibWrapper.VTFLib as VTFLib
    import VTFLibWrapper.VTFLibEnums as VTFLibEnums
    VTFLIB_AVAILABLE = True
except ImportError:
    VTFLIB_AVAILABLE = False

from .base_tool import BaseTool, register_tool
from .utils import PlaceholderEntry, browse_folder, determine_surfaceprop, browse_folder_with_context, save_config


def convert_png_to_vtf(vtf_lib, png_src, vtf_dst, clamp):
    if not VTFLIB_AVAILABLE:
        print(f"[ERROR] VTFLib not available for conversion: {png_src}")
        return False
        
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
    
    try:
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
    except Exception as e:
        print(f"[ERROR] VTF conversion failed for {png_src}: {e}")
        return False


def _extract_base_name(filename: str) -> str:
    """Return the texture name without the Source 2 suffixes."""
    name = os.path.splitext(os.path.basename(filename))[0]
    m = re.match(r"(.*?)(?:_color_|_normal_).*", name, re.IGNORECASE)
    return m.group(1) if m else name


def texture_conversion_folder(input_dir, output_dir, material_path, clamp, vmt_type, surface_prop, extra_params=None):
    if not VTFLIB_AVAILABLE:
        messagebox.showerror("Missing Dependency", 
                            "VTFLib is required for texture conversion.\n"
                            "Please install VTFLibWrapper to use this tool.")
        return
        
    if not os.path.isdir(input_dir):
        messagebox.showerror("Textures", f"Input not found:\n{input_dir}")
        return
    if not output_dir.strip():
        messagebox.showerror("Textures", "Select an output folder.")
        return
    os.makedirs(output_dir, exist_ok=True)

    try:
        vtf_lib = VTFLib.VTFLib()
    except Exception as e:
        messagebox.showerror("VTFLib Error", f"Failed to initialize VTFLib: {e}")
        return
        
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


class TextureTab(ttk.Frame):
    def __init__(self, parent, config):
        super().__init__(parent)
        self.config = config

        # Input folder
        ttk.Label(self, text="Input Folder:").grid(row=0, column=0, pady=5, padx=5, sticky="e")
        self.entry_input = PlaceholderEntry(self, width=50)
        self.entry_input.insert(0, config.get("texture_input", ""))
        self.entry_input.grid(row=0, column=1, pady=5, padx=5, sticky="ew")
        ttk.Button(self, text="Browse…", command=lambda: browse_folder_with_context(self.entry_input, context_key="texture_tool_input_folder", title="Select Input Folder")).grid(row=0, column=2, padx=5)

        # Output folder
        ttk.Label(self, text="Output Folder:").grid(row=1, column=0, pady=5, padx=5, sticky="e")
        self.entry_output = PlaceholderEntry(self, width=50)
        self.entry_output.insert(0, config.get("texture_output", ""))
        self.entry_output.grid(row=1, column=1, pady=5, padx=5, sticky="ew")
        ttk.Button(self, text="Browse…", command=lambda: browse_folder_with_context(self.entry_output, context_key="texture_tool_output_folder", title="Select Output Folder")).grid(row=1, column=2, padx=5)

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


@register_tool
class TextureConversionTool(BaseTool):
    @property
    def name(self) -> str:
        return "Textures → VTF/VMT"
    
    @property
    def description(self) -> str:
        return "Convert Source 2 textures to Source 1 VTF/VMT format"
    
    @property
    def dependencies(self) -> list:
        return ["PIL", "VTFLibWrapper.VTFLib"]
    
    def create_tab(self, parent) -> ttk.Frame:
        return TextureTab(parent, self.config)
