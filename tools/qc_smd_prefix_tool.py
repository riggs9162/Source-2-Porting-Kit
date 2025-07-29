"""
QC/SMD Prefix Tool - Add prefixes to QC and SMD files for organization.
"""

import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .base_tool import BaseTool, register_tool
from .utils import browse_file_with_context, save_config


def modify_qc_smd_files(qc_path, smd_path, model_prefix, texture_prefix):
    """Modify QC and SMD files with the specified prefixes."""
    if not os.path.exists(qc_path) or not os.path.exists(smd_path):
        raise FileNotFoundError("QC or SMD file not found.")

    # Read and modify QC file
    with open(qc_path, "r") as qc_file:
        qc_lines = qc_file.readlines()

    new_qc_lines = []
    for line in qc_lines:
        if "$modelname" in line.lower():
            parts = line.strip().split()
            if len(parts) >= 2:
                path = parts[1].strip('"')
                path_parts = path.split('/')
                if len(path_parts) > 1:
                    # Keep path structure but modify filename
                    filename = path_parts[-1]
                    path_parts[-1] = filename.replace(".mdl", f"{model_prefix}.mdl")
                    new_path = '/'.join(path_parts)
                else:
                    # Simple filename without path
                    new_path = path.replace(".mdl", f"{model_prefix}.mdl")
                new_line = f'{parts[0]} "{new_path}"\n'
                new_qc_lines.append(new_line)
            else:
                new_qc_lines.append(line)
        elif ".smd" in line.lower():
            smd_line = line.replace(".smd", f"{model_prefix}.smd")
            new_qc_lines.append(smd_line)
        else:
            new_qc_lines.append(line)

    new_qc_path = qc_path.replace(".qc", f"{model_prefix}.qc")
    with open(new_qc_path, "w") as out_qc:
        out_qc.writelines(new_qc_lines)

    # Read and modify SMD file
    with open(smd_path, "r") as smd_file:
        smd_lines = smd_file.readlines()

    new_smd_lines = []
    for line in smd_lines:
        if "materials/" in line.lower():
            new_smd_lines.append(line.replace("materials/", f"materials/{texture_prefix}"))
        else:
            new_smd_lines.append(line)

    smd_dir = os.path.dirname(smd_path)
    smd_base = os.path.splitext(os.path.basename(smd_path))[0]
    new_smd_name = f"{smd_base}{model_prefix}.smd"
    new_smd_path = os.path.join(smd_dir, new_smd_name)
    with open(new_smd_path, "w") as out_smd:
        out_smd.writelines(new_smd_lines)

    return new_qc_path, new_smd_path


class QcSmdPrefixTab(ttk.Frame):
    def __init__(self, parent, config):
        super().__init__(parent)
        self.config = config
        
        self.qc_path = ""
        self.smd_path = ""
        
        # QC File Selection
        ttk.Button(self, text="Select QC File", command=self.select_qc).grid(row=0, column=0, pady=5, padx=5, sticky="w")
        self.qc_label = ttk.Label(self, text="No QC file selected")
        self.qc_label.grid(row=0, column=1, pady=5, padx=5, sticky="w")
        
        # SMD File Selection
        ttk.Button(self, text="Select SMD File", command=self.select_smd).grid(row=1, column=0, pady=5, padx=5, sticky="w")
        self.smd_label = ttk.Label(self, text="No SMD file selected")
        self.smd_label.grid(row=1, column=1, pady=5, padx=5, sticky="w")
        
        # Model Prefix
        ttk.Label(self, text="Model Prefix (adds to QC + renames SMD):").grid(row=2, column=0, pady=5, padx=5, sticky="w")
        self.model_prefix_entry = ttk.Entry(self, width=50)
        self.model_prefix_entry.insert(0, config.get("qc_smd_model_prefix", ""))
        self.model_prefix_entry.grid(row=2, column=1, pady=5, padx=5, sticky="w")
        
        # Texture Prefix
        ttk.Label(self, text="Texture Prefix (adds to materials in SMD):").grid(row=3, column=0, pady=5, padx=5, sticky="w")
        self.texture_prefix_entry = ttk.Entry(self, width=50)
        self.texture_prefix_entry.insert(0, config.get("qc_smd_texture_prefix", ""))
        self.texture_prefix_entry.grid(row=3, column=1, pady=5, padx=5, sticky="w")
        
        # Apply Button
        ttk.Button(self, text="Apply and Save", command=self.on_apply).grid(row=4, column=0, columnspan=2, pady=15)
        
        # Log area
        ttk.Label(self, text="Log:").grid(row=5, column=0, sticky="w", padx=5)
        self.log_text = tk.Text(self, height=8, width=70)
        self.log_text.grid(row=6, column=0, columnspan=2, padx=5, pady=5, sticky="nsew")
        
        self.columnconfigure(1, weight=1)
        self.rowconfigure(6, weight=1)
    
    def log(self, message):
        """Add a message to the log."""
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
    
    def select_qc(self):
        path = browse_file_with_context(
            entry=None, context_key="qc_smd_prefix_qc_file",
            filetypes=[("QC Files", "*.qc")], title="Select QC File"
        )
        if path:
            self.qc_path = path
            self.qc_label.config(text=os.path.basename(path))
            self.log(f"Selected QC file: {os.path.basename(path)}")
    
    def select_smd(self):
        path = browse_file_with_context(
            entry=None, context_key="qc_smd_prefix_smd_file",
            filetypes=[("SMD Files", "*.smd")], title="Select SMD File"
        )
        if path:
            self.smd_path = path
            self.smd_label.config(text=os.path.basename(path))
            self.log(f"Selected SMD file: {os.path.basename(path)}")
    
    def on_apply(self):
        if not self.qc_path or not self.smd_path:
            messagebox.showerror("Missing File", "You must select both a QC and an SMD file.")
            return
            
        model_prefix = self.model_prefix_entry.get().strip()
        texture_prefix = self.texture_prefix_entry.get().strip()
        
        if not model_prefix and not texture_prefix:
            messagebox.showerror("Missing Prefixes", "Please enter at least one prefix.")
            return
            
        # Save to config
        self.config["qc_smd_model_prefix"] = model_prefix
        self.config["qc_smd_texture_prefix"] = texture_prefix
        save_config(self.config)
        
        # Process files
        try:
            self.log(f"Applying prefixes - Model: '{model_prefix}', Texture: '{texture_prefix}'")
            new_qc, new_smd = modify_qc_smd_files(self.qc_path, self.smd_path, model_prefix, texture_prefix)
            self.log(f"Created new QC: {os.path.basename(new_qc)}")
            self.log(f"Created new SMD: {os.path.basename(new_smd)}")
            messagebox.showinfo("Success", f"Files processed successfully:\n\nNew QC: {os.path.basename(new_qc)}\nNew SMD: {os.path.basename(new_smd)}")
        except Exception as e:
            error_msg = f"An error occurred: {str(e)}"
            self.log(f"Error: {error_msg}")
            messagebox.showerror("Error", error_msg)


@register_tool
class QcSmdPrefixTool(BaseTool):
    @property
    def name(self) -> str:
        return "QC/SMD Prefix"
    
    @property
    def description(self) -> str:
        return "Add prefixes to QC and SMD files for organization"
    
    @property
    def dependencies(self) -> list:
        return []  # No special dependencies
    
    def create_tab(self, parent) -> ttk.Frame:
        return QcSmdPrefixTab(parent, self.config)
