"""
VMT Duplicator Tool - Copy VMT and associated VTF files with new names.
"""

import os
import shutil
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import re
from .base_tool import BaseTool, register_tool
from .utils import PlaceholderEntry, browse_file

@register_tool
class VMTDuplicatorTool(BaseTool):
    @property
    def name(self) -> str:
        return "VMT Duplicator"

    @property
    def description(self) -> str:
        return "Copy VMT files and their associated VTF files with new names"

    @property
    def dependencies(self) -> list:
        return []  # No external dependencies

    def create_tab(self, parent) -> ttk.Frame:
        return VMTDuplicatorTab(parent, self.config)

class VMTDuplicatorTab(ttk.Frame):
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

        # VMT file input
        ttk.Label(input_frame, text="VMT File:").grid(row=0, column=0, sticky="w", pady=2)
        self.vmt_path = PlaceholderEntry(input_frame, placeholder="Select VMT file to duplicate...")
        self.vmt_path.grid(row=0, column=1, sticky="ew", padx=(5, 0), pady=2)
        ttk.Button(input_frame, text="Browse",
                command=self.browse_vmt_file).grid(row=0, column=2, padx=(5, 0), pady=2)

        input_frame.columnconfigure(1, weight=1)

        # Settings section
        settings_frame = ttk.LabelFrame(main_frame, text="Settings", padding=10)
        settings_frame.pack(fill="x", pady=(0, 10))

        # New name input
        ttk.Label(settings_frame, text="New Name:").grid(row=0, column=0, sticky="w", pady=2)
        self.new_name = PlaceholderEntry(settings_frame, placeholder="Enter new name (e.g., armor_vision)")
        self.new_name.grid(row=0, column=1, sticky="ew", padx=(5, 0), pady=2)

        # Output directory
        ttk.Label(settings_frame, text="Output Directory:").grid(row=1, column=0, sticky="w", pady=2)
        self.output_dir = PlaceholderEntry(settings_frame, placeholder="Select output directory...")
        self.output_dir.grid(row=1, column=1, sticky="ew", padx=(5, 0), pady=2)
        ttk.Button(settings_frame, text="Browse",
                command=self.browse_output_dir).grid(row=1, column=2, padx=(5, 0), pady=2)

        # Options
        options_frame = ttk.Frame(settings_frame)
        options_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))

        self.update_vmt_content = tk.BooleanVar(value=True)
        ttk.Checkbutton(options_frame, text="Update VMT texture references", 
                       variable=self.update_vmt_content).pack(side="left")

        self.copy_to_same_dir = tk.BooleanVar(value=False)
        ttk.Checkbutton(options_frame, text="Copy to same directory as source", 
                       variable=self.copy_to_same_dir,
                       command=self.toggle_output_dir).pack(side="left", padx=(20, 0))

        settings_frame.columnconfigure(1, weight=1)

        # Preview section
        preview_frame = ttk.LabelFrame(main_frame, text="Preview", padding=10)
        preview_frame.pack(fill="both", expand=True, pady=(0, 10))

        # Files list
        columns = ("Original", "New Name", "Status")
        self.tree = ttk.Treeview(preview_frame, columns=columns, show="headings", height=8)
        
        for col in columns:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=200)

        scrollbar = ttk.Scrollbar(preview_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # Action buttons
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill="x", pady=(0, 10))

        ttk.Button(button_frame, text="Preview Files", 
                  command=self.preview_files).pack(side="left")
        ttk.Button(button_frame, text="Copy Files", 
                  command=self.copy_files).pack(side="left", padx=(10, 0))
        ttk.Button(button_frame, text="Clear", 
                  command=self.clear_preview).pack(side="right")

        # Status label
        self.status_label = ttk.Label(main_frame, text="Ready", foreground="green")
        self.status_label.pack(pady=(10, 0))

        # Bind events
        self.vmt_path.bind('<KeyRelease>', self.on_vmt_path_change)
        self.new_name.bind('<KeyRelease>', self.on_settings_change)

    def browse_vmt_file(self):
        """Browse for VMT file."""
        path = browse_file(
            title="Select VMT File",
            filetypes=[("VMT Files", "*.vmt"), ("All Files", "*.*")]
        )
        if path:
            self.vmt_path.set_text(path)
            self.auto_fill_settings(path)
            self.on_vmt_path_change()

    def browse_output_dir(self):
        """Browse for output directory."""
        directory = filedialog.askdirectory(title="Select Output Directory")
        if directory:
            self.output_dir.set_text(directory)

    def toggle_output_dir(self):
        """Toggle output directory field based on checkbox."""
        if self.copy_to_same_dir.get():
            self.output_dir.config(state="disabled")
        else:
            self.output_dir.config(state="normal")

    def auto_fill_settings(self, vmt_path):
        """Auto-fill settings based on VMT file."""
        if not vmt_path:
            return

        # Extract base name from VMT file
        base_name = os.path.splitext(os.path.basename(vmt_path))[0]
        
        # Set default new name
        if not self.new_name.get():
            self.new_name.set_text(f"{base_name}_copy")

        # Set default output directory to same as source
        if not self.output_dir.get():
            source_dir = os.path.dirname(vmt_path)
            self.output_dir.set_text(source_dir)

    def on_vmt_path_change(self, event=None):
        """Handle VMT path changes."""
        self.on_settings_change()

    def on_settings_change(self, event=None):
        """Handle settings changes."""
        if self.vmt_path.get() and self.new_name.get():
            self.preview_files()

    def find_associated_files(self, vmt_path):
        """Find VTF files associated with the VMT file."""
        if not os.path.exists(vmt_path):
            return []

        base_name = os.path.splitext(os.path.basename(vmt_path))[0]
        source_dir = os.path.dirname(vmt_path)
        
        # Look for VTF files with the same base name
        associated_files = []
        
        # Common texture suffixes
        common_suffixes = ['', '_normal', '_spec', '_detail', '_bump', '_height', '_ao', '_rough', '_metal']
        
        for suffix in common_suffixes:
            vtf_name = f"{base_name}{suffix}.vtf"
            vtf_path = os.path.join(source_dir, vtf_name)
            if os.path.exists(vtf_path):
                associated_files.append(vtf_path)

        # Also look for any other VTF files that start with the base name
        try:
            for file in os.listdir(source_dir):
                if file.lower().endswith('.vtf') and file.startswith(base_name):
                    vtf_path = os.path.join(source_dir, file)
                    if vtf_path not in associated_files:
                        associated_files.append(vtf_path)
        except (OSError, PermissionError):
            pass

        return associated_files

    def preview_files(self):
        """Preview the files that will be copied."""
        self.clear_preview()

        vmt_path = self.vmt_path.get()
        new_name = self.new_name.get()

        if not vmt_path or not new_name:
            self.status_label.config(text="Please select VMT file and enter new name", foreground="orange")
            return

        if not os.path.exists(vmt_path):
            self.status_label.config(text="VMT file does not exist", foreground="red")
            return

        try:
            # Add VMT file
            original_name = os.path.splitext(os.path.basename(vmt_path))[0]
            new_vmt_name = f"{new_name}.vmt"
            self.tree.insert("", "end", values=(os.path.basename(vmt_path), new_vmt_name, "Ready"))

            # Find and add associated VTF files
            associated_files = self.find_associated_files(vmt_path)
            
            for vtf_path in associated_files:
                vtf_filename = os.path.basename(vtf_path)
                vtf_base = os.path.splitext(vtf_filename)[0]
                
                # Replace the original base name with the new name
                new_vtf_name = vtf_base.replace(original_name, new_name, 1) + ".vtf"
                self.tree.insert("", "end", values=(vtf_filename, new_vtf_name, "Ready"))

            file_count = len(associated_files) + 1  # +1 for VMT
            self.status_label.config(text=f"Found {file_count} files to copy", foreground="green")

        except Exception as e:
            self.status_label.config(text=f"Preview error: {e}", foreground="red")

    def clear_preview(self):
        """Clear the preview list."""
        for item in self.tree.get_children():
            self.tree.delete(item)

    def update_vmt_content_references(self, vmt_path, output_path, original_name, new_name):
        """Update texture references inside the VMT file."""
        try:
            with open(vmt_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()

            # Simple and direct string replacement approach
            # Order matters - do specific patterns first, then general ones
            patterns = [
                # Match path/original_name_suffix (with underscore suffix)
                (rf'(\S*/){re.escape(original_name)}_([^"\s]*)"', rf'\1{new_name}_\2"'),
                # Match path/original_name" (end of path, no suffix)
                (rf'(\S*/){re.escape(original_name)}"', rf'\1{new_name}"'),
                # Match just "original_name" at end of quoted string (fallback)
                (rf'"{re.escape(original_name)}"', rf'"{new_name}"'),
            ]

            for pattern, replacement in patterns:
                content = re.sub(pattern, replacement, content)

            # Write updated content
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(content)

        except Exception as e:
            # If update fails, just copy the original file
            shutil.copy2(vmt_path, output_path)
            raise e

    def copy_files(self):
        """Copy the files with new names."""
        vmt_path = self.vmt_path.get()
        new_name = self.new_name.get()

        if not vmt_path or not new_name:
            messagebox.showerror("Error", "Please select VMT file and enter new name")
            return

        # Determine output directory
        if self.copy_to_same_dir.get():
            output_dir = os.path.dirname(vmt_path)
        else:
            output_dir = self.output_dir.get()
            if not output_dir:
                messagebox.showerror("Error", "Please select output directory")
                return

        if not os.path.exists(output_dir):
            try:
                os.makedirs(output_dir)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to create output directory: {e}")
                return

        try:
            copied_files = 0
            errors = []
            original_name = os.path.splitext(os.path.basename(vmt_path))[0]

            # Update tree items status
            for item in self.tree.get_children():
                self.tree.set(item, "Status", "Copying...")
                self.update()

            # Copy VMT file
            vmt_item = self.tree.get_children()[0]  # VMT is always first
            try:
                new_vmt_path = os.path.join(output_dir, f"{new_name}.vmt")
                
                if self.update_vmt_content.get():
                    self.update_vmt_content_references(vmt_path, new_vmt_path, original_name, new_name)
                else:
                    shutil.copy2(vmt_path, new_vmt_path)
                
                self.tree.set(vmt_item, "Status", "✓ Copied")
                copied_files += 1
            except Exception as e:
                self.tree.set(vmt_item, "Status", f"✗ Error: {str(e)[:20]}...")
                errors.append(f"VMT: {e}")

            # Copy VTF files
            associated_files = self.find_associated_files(vmt_path)
            vtf_items = self.tree.get_children()[1:]  # Skip VMT (first item)

            for i, vtf_path in enumerate(associated_files):
                if i < len(vtf_items):
                    item = vtf_items[i]
                    try:
                        vtf_filename = os.path.basename(vtf_path)
                        vtf_base = os.path.splitext(vtf_filename)[0]
                        new_vtf_name = vtf_base.replace(original_name, new_name, 1) + ".vtf"
                        new_vtf_path = os.path.join(output_dir, new_vtf_name)
                        
                        shutil.copy2(vtf_path, new_vtf_path)
                        self.tree.set(item, "Status", "✓ Copied")
                        copied_files += 1
                    except Exception as e:
                        self.tree.set(item, "Status", f"✗ Error: {str(e)[:20]}...")
                        errors.append(f"{os.path.basename(vtf_path)}: {e}")

            # Show results
            if errors:
                error_msg = f"Copied {copied_files} files with {len(errors)} errors:\n\n"
                error_msg += "\n".join(errors[:5])  # Show first 5 errors
                if len(errors) > 5:
                    error_msg += f"\n... and {len(errors) - 5} more errors"
                messagebox.showwarning("Copy Complete with Errors", error_msg)
                self.status_label.config(text=f"Copied {copied_files} files with {len(errors)} errors", foreground="orange")
            else:
                messagebox.showinfo("Success", f"Successfully copied {copied_files} files to:\n{output_dir}")
                self.status_label.config(text=f"Successfully copied {copied_files} files", foreground="green")

        except Exception as e:
            messagebox.showerror("Error", f"Copy operation failed: {e}")
            self.status_label.config(text=f"Copy failed: {e}", foreground="red")
