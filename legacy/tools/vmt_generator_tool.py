"""
VMT Generator Tool - Generate VMT files from VTF files using a template.
"""

import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from .base_tool import BaseTool, register_tool
from .utils import PlaceholderEntry, browse_folder, browse_file, browse_folder_with_context, browse_file_with_context, save_file_with_context

@register_tool
class VMTGeneratorTool(BaseTool):
    @property
    def name(self) -> str:
        return "VMT Generator"

    @property
    def description(self) -> str:
        return "Generate VMT files for VTF textures using a template VMT file"

    @property
    def dependencies(self) -> list:
        return []  # No external dependencies

    def create_tab(self, parent) -> ttk.Frame:
        return VMTGeneratorTab(parent, self.config)

class VMTGeneratorTab(ttk.Frame):
    def __init__(self, parent, config):
        super().__init__(parent)
        self.config = config
        self.setup_ui()

    def setup_ui(self):
        """Set up the user interface."""
        main_frame = ttk.Frame(self)
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)

        # Input section
        input_frame = ttk.LabelFrame(main_frame, text="Input", padding=10)
        input_frame.pack(fill="x", pady=(0, 10))

        # VTF folder input
        ttk.Label(input_frame, text="VTF Folder:").grid(row=0, column=0, sticky="w", pady=2)
        self.vtf_folder = PlaceholderEntry(input_frame, placeholder="Select folder containing VTF files...")
        self.vtf_folder.grid(row=0, column=1, sticky="ew", padx=(5, 0), pady=2)
        ttk.Button(input_frame, text="Browse",
                command=self.browse_vtf_folder).grid(row=0, column=2, padx=(5, 0), pady=2)

        # Template VMT input
        ttk.Label(input_frame, text="Template VMT:").grid(row=1, column=0, sticky="w", pady=2)
        self.template_path = PlaceholderEntry(input_frame, placeholder="Select template VMT file...")
        self.template_path.grid(row=1, column=1, sticky="ew", padx=(5, 0), pady=2)
        ttk.Button(input_frame, text="Browse",
                command=self.browse_template).grid(row=1, column=2, padx=(5, 0), pady=2)

        # Output folder input
        ttk.Label(input_frame, text="Output Folder:").grid(row=2, column=0, sticky="w", pady=2)
        self.output_folder = PlaceholderEntry(input_frame, placeholder="Select output folder for VMT files...")
        self.output_folder.grid(row=2, column=1, sticky="ew", padx=(5, 0), pady=2)
        ttk.Button(input_frame, text="Browse",
                command=self.browse_output_folder).grid(row=2, column=2, padx=(5, 0), pady=2)

        input_frame.columnconfigure(1, weight=1)

        # Template preview section
        template_frame = ttk.LabelFrame(main_frame, text="Template Preview & Edit", padding=10)
        template_frame.pack(fill="both", expand=True, pady=(0, 10))

        # Instructions
        instructions = ttk.Label(template_frame,
                                text="Template VMT content (use {{TEXTURE_NAME}} as placeholder for texture path):")
        instructions.pack(anchor="w")

        # Template text area
        self.template_text = ScrolledText(template_frame, height=10, width=70)
        self.template_text.pack(fill="both", expand=True, pady=(5, 0))

        # Default template
        default_template = '''\"VertexLitGeneric\"
{
    \"$basetexture\" \"{{TEXTURE_NAME}}\"
    \"$surfaceprop\" \"default\"
}'''
        self.template_text.insert("1.0", default_template)

        # Template buttons
        template_buttons = ttk.Frame(template_frame)
        template_buttons.pack(fill="x", pady=(5, 0))

        ttk.Button(template_buttons, text="Load from Template File",
                command=self.load_template_file).pack(side="left")
        ttk.Button(template_buttons, text="Save Template",
                command=self.save_template).pack(side="left", padx=(10, 0))
        ttk.Button(template_buttons, text="Reset to Default",
                command=self.reset_template).pack(side="left", padx=(10, 0))

        # Preset templates dropdown
        ttk.Label(template_buttons, text="Presets:").pack(side="left", padx=(20, 5))
        self.preset_var = tk.StringVar()
        preset_combo = ttk.Combobox(template_buttons, textvariable=self.preset_var,
                                    values=["VertexLitGeneric", "LightmappedGeneric", "UnlitGeneric",
                                            "Refract", "Water", "Decal"], state="readonly", width=15)
        preset_combo.pack(side="left")
        preset_combo.bind("<<ComboboxSelected>>", self.load_preset_template)

        # Options section
        options_frame = ttk.LabelFrame(main_frame, text="Options", padding=10)
        options_frame.pack(fill="x", pady=(0, 10))

        self.overwrite_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(options_frame, text="Overwrite existing VMT files",
                        variable=self.overwrite_var).pack(anchor="w")

        self.relative_paths_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options_frame, text="Use relative texture paths",
                        variable=self.relative_paths_var).pack(anchor="w")

        self.preserve_structure_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options_frame, text="Preserve folder structure",
                        variable=self.preserve_structure_var).pack(anchor="w")

        # Generate button and output
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill="x", pady=(0, 10))

        ttk.Button(button_frame, text="Preview Generated VMTs",
                    command=self.preview_generation).pack(side="left")
        ttk.Button(button_frame, text="Generate VMT Files",
                    command=self.generate_vmts).pack(side="left", padx=(10, 0))

        # Status label
        self.status_label = ttk.Label(main_frame, text="Ready", foreground="green")
        self.status_label.pack(pady=(10, 0))

    def browse_vtf_folder(self):
        """Browse for VTF folder."""
        path = browse_folder_with_context(self.vtf_folder, context_key="vmt_generator_vtf_folder", 
                                        title="Select folder containing VTF files")

    def browse_template(self):
        """Browse for template VMT file."""
        path = browse_file_with_context(
            self.template_path, context_key="vmt_generator_template",
            title="Select template VMT file",
            filetypes=[("VMT Files", "*.vmt"), ("Text Files", "*.txt"), ("All Files", "*.*")]
        )
        if path:
            self.load_template_file()

    def browse_output_folder(self):
        """Browse for output folder."""
        path = browse_folder_with_context(self.output_folder, context_key="vmt_generator_output_folder",
                                        title="Select output folder for VMT files")

    def load_template_file(self):
        """Load template from the selected file."""
        template_path = self.template_path.get()
        if not template_path or not os.path.exists(template_path):
            return

        try:
            with open(template_path, 'r', encoding='utf-8') as f:
                content = f.read()

            self.template_text.delete("1.0", "end")
            self.template_text.insert("1.0", content)
            self.status_label.config(text="Template loaded", foreground="green")

        except Exception as e:
            messagebox.showerror("Error", f"Failed to load template: {e}")
            self.status_label.config(text="Error loading template", foreground="red")

    def save_template(self):
        """Save current template to a file."""
        output_path = save_file_with_context(
            context_key="vmt_generator_save_template",
            title="Save Template",
            defaultextension=".vmt",
            filetypes=[("VMT Files", "*.vmt"), ("Text Files", "*.txt")]
        )

        if output_path:
            try:
                template_content = self.template_text.get("1.0", "end-1c")
                with open(output_path, 'w', encoding='utf-8') as f:
                    f.write(template_content)

                self.status_label.config(text=f"Template saved: {os.path.basename(output_path)}", foreground="green")

            except Exception as e:
                messagebox.showerror("Error", f"Failed to save template: {e}")
                self.status_label.config(text="Error saving template", foreground="red")

    def reset_template(self):
        """Reset template to default."""
        default_template = '''\"VertexLitGeneric\"\n{
    \"$basetexture\" \"{TEXTURE_NAME}\"
    \"$surfaceprop\" \"default\"\n}'''
        self.template_text.delete("1.0", "end")
        self.template_text.insert("1.0", default_template)

    def load_preset_template(self, event=None):
        """Load a preset template based on selection."""
        preset = self.preset_var.get()

        templates = {
            "VertexLitGeneric": '''\"VertexLitGeneric\"\n{
    \"$basetexture\" \"{TEXTURE_NAME}\"
    \"$surfaceprop\" \"default\"\n}''',
            "LightmappedGeneric": '''\"LightmappedGeneric\"\n{
    \"$basetexture\" \"{TEXTURE_NAME}\"
    \"$surfaceprop\" \"default\"\n}''',
            "UnlitGeneric": '''\"UnlitGeneric\"\n{
    \"$basetexture\" \"{TEXTURE_NAME}\"
    \"$surfaceprop\" \"default\"
    \"$vertexcolor\" \"1\"
    \"$vertexalpha\" \"1\"\n}''',
            "Refract": '''\"Refract\"\n{
    \"$refracttexture\" \"_rt_WaterRefraction\"
    \"$dudvmap\" \"{TEXTURE_NAME}\"
    \"$normalmap\" \"{TEXTURE_NAME}_normal\"
    \"$refractamount\" \"0.5\"
    \"$surfaceprop\" \"glass\"\n}''',
            "Water": '''\"Water\"\n{
    \"$basetexture\" \"{TEXTURE_NAME}\"
    \"$normalmap\" \"{TEXTURE_NAME}_normal\"
    \"$surfaceprop\" \"water\"
    \"$cheapwaterstartdistance\" \"500\"
    \"$cheapwaterenddistance\" \"1000\"\n}''',
            "Decal": '''\"DecalModulate\"\n{
    \"$basetexture\" \"{TEXTURE_NAME}\"
    \"$decal\" \"1\"
    \"$decalscale\" \"1\"\n}'''
        }

        if preset in templates:
            self.template_text.delete("1.0", "end")
            self.template_text.insert("1.0", templates[preset])

    def find_vtf_files(self, folder_path):
        """Find all VTF files in the specified folder."""
        vtf_files = []

        if not os.path.exists(folder_path):
            return vtf_files

        for root, dirs, files in os.walk(folder_path):
            for file in files:
                if file.lower().endswith('.vtf'):
                    vtf_files.append(os.path.join(root, file))

        return vtf_files

    def generate_texture_path(self, vtf_path, vtf_folder, relative_paths):
        """Generate the texture path for the VMT file."""
        if relative_paths:
            # Get relative path from VTF folder
            rel_path = os.path.relpath(vtf_path, vtf_folder)
            # Remove .vtf extension and convert to forward slashes
            texture_path = os.path.splitext(rel_path)[0].replace('\\\\', '/')
        else:
            # Use just the filename without extension
            texture_path = os.path.splitext(os.path.basename(vtf_path))[0]

        return texture_path

    def generate_vmt_content(self, texture_path):
        """Generate VMT content using the template."""
        template_content = self.template_text.get("1.0", "end-1c")
        return template_content.replace("{TEXTURE_NAME}", texture_path)

    def preview_generation(self):
        """Preview what VMT files would be generated."""
        vtf_folder = self.vtf_folder.get()
        if not vtf_folder:
            messagebox.showerror("Error", "Please select a VTF folder first.")
            return

        vtf_files = self.find_vtf_files(vtf_folder)
        if not vtf_files:
            messagebox.showinfo("No Files", "No VTF files found in the selected folder.")
            return

        # Create preview window
        preview_window = tk.Toplevel(self)
        preview_window.title("VMT Generation Preview")
        preview_window.geometry("800x600")

        text_widget = ScrolledText(preview_window, wrap="word")
        text_widget.pack(fill="both", expand=True, padx=10, pady=10)

        preview_text = f"Preview of VMT files that would be generated:\\n\\n"
        preview_text += f"Found {len(vtf_files)} VTF files:\\n\\n"

        relative_paths = self.relative_paths_var.get()

        for i, vtf_file in enumerate(vtf_files[:10]):  # Show first 10 for preview
            texture_path = self.generate_texture_path(vtf_file, vtf_folder, relative_paths)
            vmt_content = self.generate_vmt_content(texture_path)

            preview_text += f"File {i+1}: {os.path.basename(vtf_file)}\\n"
            preview_text += f"Texture path: {texture_path}\\n"
            preview_text += f"VMT content:\\n{vmt_content}\\n"
            preview_text += "-" * 50 + "\\n\\n"

        if len(vtf_files) > 10:
            preview_text += f"... and {len(vtf_files) - 10} more files\\n"

        text_widget.insert("1.0", preview_text)
        text_widget.config(state="disabled")

    def generate_vmts(self):
        """Generate VMT files for all VTF files."""
        vtf_folder = self.vtf_folder.get()
        output_folder = self.output_folder.get()

        if not vtf_folder:
            messagebox.showerror("Error", "Please select a VTF folder first.")
            return

        if not output_folder:
            messagebox.showerror("Error", "Please select an output folder first.")
            return

        vtf_files = self.find_vtf_files(vtf_folder)
        if not vtf_files:
            messagebox.showinfo("No Files", "No VTF files found in the selected folder.")
            return

        # Confirm generation
        result = messagebox.askyesno("Confirm Generation",
                                    f"Generate VMT files for {len(vtf_files)} VTF files?")
        if not result:
            return

        relative_paths = self.relative_paths_var.get()
        preserve_structure = self.preserve_structure_var.get()
        overwrite = self.overwrite_var.get()

        generated = 0
        skipped = 0
        errors = 0

        try:
            for vtf_file in vtf_files:
                try:
                    # Generate texture path
                    texture_path = self.generate_texture_path(vtf_file, vtf_folder, relative_paths)

                    # Generate VMT content
                    vmt_content = self.generate_vmt_content(texture_path)

                    # Determine output path
                    if preserve_structure:
                        # Preserve folder structure
                        rel_path = os.path.relpath(vtf_file, vtf_folder)
                        vmt_filename = os.path.splitext(rel_path)[0] + '.vmt'
                        vmt_output_path = os.path.join(output_folder, vmt_filename)

                        # Create subdirectories if needed
                        os.makedirs(os.path.dirname(vmt_output_path), exist_ok=True)
                    else:
                        # Flat structure
                        vmt_filename = os.path.splitext(os.path.basename(vtf_file))[0] + '.vmt'
                        vmt_output_path = os.path.join(output_folder, vmt_filename)

                    # Check if file exists and handle overwrite
                    if os.path.exists(vmt_output_path) and not overwrite:
                        skipped += 1
                        continue

                    # Write VMT file
                    with open(vmt_output_path, 'w', encoding='utf-8') as f:
                        f.write(vmt_content)

                    generated += 1

                except Exception as e:
                    print(f"Error processing {vtf_file}: {e}")
                    errors += 1

            # Show results
            messagebox.showinfo("Generation Complete",
                                f"Generated {generated} VMT files.\\n"
                                f"Skipped {skipped} existing files.\\n"
                                f"{errors} errors occurred.")

            self.status_label.config(
                text=f"Complete: {generated} generated, {skipped} skipped, {errors} errors",
                foreground="green" if errors == 0 else "orange"
            )

        except Exception as e:
            messagebox.showerror("Error", f"Generation failed: {e}")
            self.status_label.config(text="Generation failed", foreground="red")
