"""
VMAT to VMT Tool - Convert Source 2 VMAT files to Source 1 VMT format.
"""

import os
import re
import tempfile
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from PIL import Image, ImageTk, ImageChops

from .base_tool import BaseTool, register_tool
from .utils import PlaceholderEntry, browse_folder, browse_file, save_config, browse_file_with_context, browse_folder_with_context

# Try to import VTFLib
try:
    import VTFLibWrapper.VTFLib as VTFLib
    import VTFLibWrapper.VTFLibEnums as VTFLibEnums
    VTFLIB_AVAILABLE = True
except ImportError:
    VTFLIB_AVAILABLE = False


def parse_vmat_file(vmat_path):
    """Parse a VMAT file and extract material information."""
    if not os.path.exists(vmat_path):
        return None
    
    try:
        with open(vmat_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Extract basic material info
        material_info = {
            'shader': 'VertexLitGeneric',  # Default
            'textures': {},
            'parameters': {}
        }
        
        # Look for texture references
        texture_patterns = {
            'g_tColor': 'basetexture',
            'g_tNormal': 'bumpmap',
            'g_tSpecular': 'specular',
            'g_tRoughness': 'roughness'
        }
        
        for vmat_key, vmt_key in texture_patterns.items():
            pattern = rf'{vmat_key}\s*=\s*"([^"]+)"'
            match = re.search(pattern, content, re.IGNORECASE)
            if match:
                material_info['textures'][vmt_key] = match.group(1)
        
        return material_info
        
    except Exception as e:
        print(f"Error parsing VMAT file: {e}")
        return None


def convert_vmat_to_vmt(vmat_path, output_dir, material_prefix, clamp_size=0):
    """Convert a VMAT file to VMT format."""
    if not VTFLIB_AVAILABLE:
        messagebox.showerror("Error", "VTFLib is not available. Cannot convert textures.")
        return False
    
    material_info = parse_vmat_file(vmat_path)
    if not material_info:
        messagebox.showerror("Error", "Failed to parse VMAT file.")
        return False
    
    # Create output paths
    base_name = os.path.splitext(os.path.basename(vmat_path))[0]
    vmt_path = os.path.join(output_dir, f"{base_name}.vmt")
    
    # Generate VMT content
    vmt_content = f'"{material_info["shader"]}"\n{{\n'
    
    # Add material prefix if specified
    if material_prefix:
        vmt_content += f'    "$basetexture" "{material_prefix}/{base_name}"\n'
    else:
        vmt_content += f'    "$basetexture" "{base_name}"\n'
    
    # Add common parameters
    vmt_content += '    "$model" "1"\n'
    vmt_content += '    "$surfaceprop" "metal"\n'
    
    # Close VMT
    vmt_content += '}\n'
    
    # Write VMT file
    try:
        with open(vmt_path, 'w', encoding='utf-8') as f:
            f.write(vmt_content)
        return True
    except Exception as e:
        messagebox.showerror("Error", f"Failed to write VMT file: {str(e)}")
        return False


class VmatToVmtTab(ttk.Frame):
    def __init__(self, parent, config):
        super().__init__(parent)
        self.config = config
        
        # VMAT File selection
        ttk.Label(self, text="VMAT File:").grid(row=0, column=0, sticky="e", padx=5, pady=5)
        self.vmat_var = tk.StringVar(value=config.get("vmat_file", ""))
        ttk.Entry(self, textvariable=self.vmat_var, width=50).grid(row=0, column=1, padx=5, pady=5, sticky="ew")
        ttk.Button(self, text="Browse…", command=self.browse_vmat).grid(row=0, column=2, padx=5, pady=5)
        
        # Output folder
        ttk.Label(self, text="Output Folder:").grid(row=1, column=0, sticky="e", padx=5, pady=5)
        self.output_var = tk.StringVar(value=config.get("vmat_output", ""))
        ttk.Entry(self, textvariable=self.output_var, width=50).grid(row=1, column=1, padx=5, pady=5, sticky="ew")
        ttk.Button(self, text="Browse…", command=self.browse_output).grid(row=1, column=2, padx=5, pady=5)
        
        # Material prefix
        ttk.Label(self, text="Material Prefix:").grid(row=2, column=0, sticky="e", padx=5, pady=5)
        self.prefix_var = tk.StringVar(value=config.get("vmat_prefix", ""))
        ttk.Entry(self, textvariable=self.prefix_var, width=50).grid(row=2, column=1, padx=5, pady=5, sticky="ew")
        
        # Clamp size
        ttk.Label(self, text="Clamp Size (0=no clamp):").grid(row=3, column=0, sticky="e", padx=5, pady=5)
        self.clamp_var = tk.StringVar(value=str(config.get("vmat_clamp", 0)))
        ttk.Entry(self, textvariable=self.clamp_var, width=10).grid(row=3, column=1, sticky="w", padx=5, pady=5)
        
        # Convert button
        ttk.Button(self, text="Convert VMAT", command=self.on_convert).grid(row=4, column=0, columnspan=3, pady=15)
        
        # Log area
        ttk.Label(self, text="Log:").grid(row=5, column=0, sticky="w", padx=5)
        self.log_text = ScrolledText(self, height=10, width=70)
        self.log_text.grid(row=6, column=0, columnspan=3, padx=5, pady=5, sticky="nsew")
        
        # Configure grid weights
        self.columnconfigure(1, weight=1)
        self.rowconfigure(6, weight=1)
        
        # Check for VTFLib availability
        if not VTFLIB_AVAILABLE:
            self.log("Warning: VTFLib not available. Texture conversion will be limited.")
    
    def log(self, message):
        """Add a message to the log."""
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
    
    def browse_vmat(self):
        path = browse_file_with_context(
            entry=None, context_key="vmat_to_vmt_source_file",
            filetypes=[("VMAT Files", "*.vmat"), ("All Files", "*.*")],
            title="Select VMAT File"
        )
        if path:
            self.vmat_var.set(path)
    
    def browse_output(self):
        path = browse_folder_with_context(
            entry=None, context_key="vmat_to_vmt_output_dir",
            title="Select Output Directory"
        )
        if path:
            self.output_var.set(path)
    
    def on_convert(self):
        vmat_file = self.vmat_var.get().strip()
        output_dir = self.output_var.get().strip()
        material_prefix = self.prefix_var.get().strip()
        
        try:
            clamp_size = int(self.clamp_var.get().strip())
        except ValueError:
            self.log("Error: Invalid clamp size")
            return
        
        if not vmat_file or not output_dir:
            messagebox.showerror("Error", "Please select both VMAT file and output directory.")
            return
        
        if not os.path.exists(vmat_file):
            messagebox.showerror("Error", "VMAT file does not exist.")
            return
        
        # Save settings
        self.config["vmat_file"] = vmat_file
        self.config["vmat_output"] = output_dir
        self.config["vmat_prefix"] = material_prefix
        self.config["vmat_clamp"] = clamp_size
        save_config(self.config)
        
        # Create output directory
        os.makedirs(output_dir, exist_ok=True)
        
        self.log(f"Converting VMAT: {os.path.basename(vmat_file)}")
        
        if convert_vmat_to_vmt(vmat_file, output_dir, material_prefix, clamp_size):
            self.log("Conversion completed successfully!")
            messagebox.showinfo("Success", "VMAT conversion completed!")
        else:
            self.log("Conversion failed!")


@register_tool
class VmatToVmtTool(BaseTool):
    @property
    def name(self) -> str:
        return "VMAT → VMT"
    
    @property
    def description(self) -> str:
        return "Convert Source 2 VMAT files to Source 1 VMT format"
    
    @property
    def dependencies(self) -> list:
        return ["PIL", "VTFLibWrapper.VTFLib"]
    
    def create_tab(self, parent) -> ttk.Frame:
        return VmatToVmtTab(parent, self.config)
