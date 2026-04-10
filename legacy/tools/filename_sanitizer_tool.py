"""
Filename Sanitizer Tool - Recursively sanitizes filenames by removing hash-like patterns.
"""

import os
import re
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter.scrolledtext import ScrolledText
from .base_tool import BaseTool, register_tool
from .utils import PlaceholderEntry, browse_folder_with_context


@register_tool
class FilenameSanitizerTool(BaseTool):
    @property
    def name(self) -> str:
        return "Filename Sanitizer"

    @property
    def description(self) -> str:
        return "Recursively sanitizes filenames by removing hash-like patterns (e.g., '_1b37cc96')"

    @property
    def dependencies(self) -> list:
        return []  # No external dependencies

    def create_tab(self, parent) -> ttk.Frame:
        return FilenameSanitizerTab(parent, self.config)


class FilenameSanitizerTab(ttk.Frame):
    def __init__(self, parent, config):
        super().__init__(parent)
        self.config = config
        self.setup_ui()

    def setup_ui(self):
        """Set up the user interface."""
        main_frame = ttk.Frame(self)
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)

        # Input section
        input_frame = ttk.LabelFrame(main_frame, text="Target Folder", padding=10)
        input_frame.pack(fill="x", pady=(0, 10))

        ttk.Label(input_frame, text="Root Folder:").grid(row=0, column=0, sticky="w", pady=2)
        self.folder_path = PlaceholderEntry(input_frame, placeholder="Select folder to sanitize filenames in...")
        self.folder_path.grid(row=0, column=1, sticky="ew", padx=(5, 0), pady=2)
        ttk.Button(input_frame, text="Browse",
                  command=self.browse_folder).grid(row=0, column=2, padx=(5, 0), pady=2)

        input_frame.columnconfigure(1, weight=1)

        # Pattern configuration section
        pattern_frame = ttk.LabelFrame(main_frame, text="Hash Pattern Configuration", padding=10)
        pattern_frame.pack(fill="x", pady=(0, 10))

        ttk.Label(pattern_frame, text="Hash Pattern:").grid(row=0, column=0, sticky="w", pady=2)
        self.hash_pattern = tk.StringVar(value=r"_[0-9a-fA-F]{8}")
        pattern_entry = ttk.Entry(pattern_frame, textvariable=self.hash_pattern, width=30)
        pattern_entry.grid(row=0, column=1, sticky="ew", padx=(5, 0), pady=2)

        ttk.Label(pattern_frame, text="(Regex pattern to match hash-like strings)").grid(row=1, column=1, sticky="w", padx=(5, 0))

        # Predefined patterns
        presets_frame = ttk.Frame(pattern_frame)
        presets_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        ttk.Label(presets_frame, text="Presets:").pack(side="left")
        ttk.Button(presets_frame, text="8-char hex (_1b37cc96)",
                  command=lambda: self.hash_pattern.set(r"_[0-9a-fA-F]{8}")).pack(side="left", padx=(5, 0))
        ttk.Button(presets_frame, text="6-char hex (_a1b2c3)",
                  command=lambda: self.hash_pattern.set(r"_[0-9a-fA-F]{6}")).pack(side="left", padx=(5, 0))
        ttk.Button(presets_frame, text="Any length hex (_[hex]+)",
                  command=lambda: self.hash_pattern.set(r"_[0-9a-fA-F]+")).pack(side="left", padx=(5, 0))

        pattern_frame.columnconfigure(1, weight=1)

        # Options section
        options_frame = ttk.LabelFrame(main_frame, text="Options", padding=10)
        options_frame.pack(fill="x", pady=(0, 10))

        self.preview_only_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options_frame, text="Preview only (don't rename files)",
                       variable=self.preview_only_var).pack(anchor="w", pady=2)

        self.case_insensitive_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options_frame, text="Case insensitive matching",
                       variable=self.case_insensitive_var).pack(anchor="w", pady=2)

        # File type filters
        filter_frame = ttk.Frame(options_frame)
        filter_frame.pack(fill="x", pady=(5, 0))

        ttk.Label(filter_frame, text="File extensions to include:").grid(row=0, column=0, sticky="w", pady=2)
        self.file_extensions = tk.StringVar(value="*")
        ext_entry = ttk.Entry(filter_frame, textvariable=self.file_extensions, width=50)
        ext_entry.grid(row=0, column=1, sticky="ew", padx=(5, 0), pady=2)

        ttk.Label(filter_frame, text="(comma-separated, e.g. .vtf,.vmt,.smd or * for all)").grid(row=1, column=1, sticky="w", padx=(5, 0))

        filter_frame.columnconfigure(1, weight=1)

        # Action buttons
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill="x", pady=(0, 10))

        ttk.Button(button_frame, text="Scan Files",
                  command=self.scan_files).pack(side="left", padx=(0, 5))
        ttk.Button(button_frame, text="Apply Rename",
                  command=self.apply_rename).pack(side="left", padx=(0, 5))
        ttk.Button(button_frame, text="Clear Results",
                  command=self.clear_results).pack(side="left", padx=(0, 5))

        # Results section
        results_frame = ttk.LabelFrame(main_frame, text="Results", padding=10)
        results_frame.pack(fill="both", expand=True)

        # Create treeview for showing changes
        self.results_tree = ttk.Treeview(results_frame, columns=("Original", "Sanitized", "Status"), show="tree headings")
        self.results_tree.heading("#0", text="Path")
        self.results_tree.heading("Original", text="Original Name")
        self.results_tree.heading("Sanitized", text="Sanitized Name")
        self.results_tree.heading("Status", text="Status")

        self.results_tree.column("#0", width=300)
        self.results_tree.column("Original", width=200)
        self.results_tree.column("Sanitized", width=200)
        self.results_tree.column("Status", width=100)

        # Scrollbars for treeview
        tree_scroll_y = ttk.Scrollbar(results_frame, orient="vertical", command=self.results_tree.yview)
        tree_scroll_x = ttk.Scrollbar(results_frame, orient="horizontal", command=self.results_tree.xview)
        self.results_tree.configure(yscrollcommand=tree_scroll_y.set, xscrollcommand=tree_scroll_x.set)

        self.results_tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll_y.grid(row=0, column=1, sticky="ns")
        tree_scroll_x.grid(row=1, column=0, sticky="ew")

        results_frame.columnconfigure(0, weight=1)
        results_frame.rowconfigure(0, weight=1)

        # Status bar
        self.status_var = tk.StringVar(value="Ready")
        status_label = ttk.Label(main_frame, textvariable=self.status_var, relief="sunken")
        status_label.pack(fill="x", pady=(5, 0))

    def browse_folder(self):
        """Browse for a folder using context-aware dialog."""
        path = browse_folder_with_context(self.folder_path, context_key="filename_sanitizer_folder",
                                        title="Select folder to sanitize filenames in")
        if path:
            self.folder_path.set_text(path)

    def get_file_extensions(self):
        """Parse file extensions from the input field."""
        ext_text = self.file_extensions.get().strip()
        if ext_text == "*" or ext_text == "":
            return None  # All files
        
        # Split by comma and clean up
        extensions = [ext.strip() for ext in ext_text.split(",")]
        # Ensure extensions start with a dot
        extensions = [ext if ext.startswith('.') else f'.{ext}' for ext in extensions if ext]
        return extensions if extensions else None

    def sanitize_filename(self, filename):
        """Sanitize a filename by removing hash-like patterns."""
        pattern = self.hash_pattern.get()
        if not pattern:
            return filename
        
        flags = re.IGNORECASE if self.case_insensitive_var.get() else 0
        
        try:
            # Remove the hash pattern from the filename
            sanitized = re.sub(pattern, '', filename, flags=flags)
            return sanitized
        except re.error as e:
            self.status_var.set(f"Invalid regex pattern: {e}")
            return filename

    def should_include_file(self, filename):
        """Check if a file should be included based on extension filters."""
        extensions = self.get_file_extensions()
        if extensions is None:
            return True
        
        _, ext = os.path.splitext(filename.lower())
        return ext in [e.lower() for e in extensions]

    def scan_files(self):
        """Scan the target folder for files that would be renamed."""
        folder_path = self.folder_path.get()
        if not folder_path or not os.path.isdir(folder_path):
            messagebox.showerror("Error", "Please select a valid folder.")
            return

        self.clear_results()
        self.status_var.set("Scanning files...")
        self.update()

        total_files = 0
        files_to_rename = 0
        
        try:
            for root, dirs, files in os.walk(folder_path):
                # Create folder nodes in tree
                rel_root = os.path.relpath(root, folder_path)
                if rel_root == ".":
                    folder_node = ""
                else:
                    folder_node = self.results_tree.insert("", "end", text=rel_root, 
                                                         values=("", "", "Folder"))

                for filename in files:
                    total_files += 1
                    
                    if not self.should_include_file(filename):
                        continue
                    
                    sanitized_name = self.sanitize_filename(filename)
                    
                    if sanitized_name != filename:
                        files_to_rename += 1
                        status = "To be renamed"
                        
                        # Add file to tree
                        if folder_node:
                            self.results_tree.insert(folder_node, "end", text=filename,
                                                   values=(filename, sanitized_name, status))
                        else:
                            self.results_tree.insert("", "end", text=filename,
                                                   values=(filename, sanitized_name, status))

            self.status_var.set(f"Scan complete: {files_to_rename} files to rename out of {total_files} total files")
            
            # Expand all nodes
            self.expand_all_nodes()
            
        except Exception as e:
            messagebox.showerror("Error", f"Error scanning files: {str(e)}")
            self.status_var.set("Error occurred during scan")

    def expand_all_nodes(self):
        """Expand all nodes in the treeview."""
        def expand_node(node):
            self.results_tree.item(node, open=True)
            for child in self.results_tree.get_children(node):
                expand_node(child)
        
        for node in self.results_tree.get_children():
            expand_node(node)

    def apply_rename(self):
        """Apply the filename sanitization (rename files)."""
        if self.preview_only_var.get():
            messagebox.showinfo("Preview Mode", "Preview mode is enabled. No files will be renamed.\n\nUncheck 'Preview only' to apply changes.")
            return

        folder_path = self.folder_path.get()
        if not folder_path or not os.path.isdir(folder_path):
            messagebox.showerror("Error", "Please select a valid folder.")
            return

        # Count files to be renamed
        rename_count = 0
        for item in self.results_tree.get_children(""):
            rename_count += self._count_rename_items(item)

        if rename_count == 0:
            messagebox.showinfo("No Changes", "No files need to be renamed.")
            return

        # Confirm with user
        result = messagebox.askyesno("Confirm Rename", 
                                   f"This will rename {rename_count} files.\n\nThis action cannot be undone.\n\nContinue?")
        if not result:
            return

        self.status_var.set("Renaming files...")
        self.update()

        renamed_count = 0
        error_count = 0

        try:
            for root, dirs, files in os.walk(folder_path):
                for filename in files:
                    if not self.should_include_file(filename):
                        continue
                    
                    sanitized_name = self.sanitize_filename(filename)
                    
                    if sanitized_name != filename:
                        old_path = os.path.join(root, filename)
                        new_path = os.path.join(root, sanitized_name)
                        
                        try:
                            # Check if target file already exists
                            if os.path.exists(new_path):
                                error_count += 1
                                self._update_item_status(filename, "Error: Target exists")
                                continue
                            
                            os.rename(old_path, new_path)
                            renamed_count += 1
                            self._update_item_status(filename, "Renamed")
                            
                        except OSError as e:
                            error_count += 1
                            self._update_item_status(filename, f"Error: {str(e)}")

            status_msg = f"Rename complete: {renamed_count} files renamed"
            if error_count > 0:
                status_msg += f", {error_count} errors"
            self.status_var.set(status_msg)
            
            if error_count > 0:
                messagebox.showwarning("Rename Complete with Errors", 
                                     f"Renamed {renamed_count} files successfully.\n{error_count} files had errors.")
            else:
                messagebox.showinfo("Rename Complete", f"Successfully renamed {renamed_count} files.")
                
        except Exception as e:
            messagebox.showerror("Error", f"Error during rename operation: {str(e)}")
            self.status_var.set("Error occurred during rename")

    def _count_rename_items(self, item):
        """Recursively count items that will be renamed."""
        count = 0
        values = self.results_tree.item(item, "values")
        if len(values) >= 3 and values[2] == "To be renamed":
            count = 1
        
        for child in self.results_tree.get_children(item):
            count += self._count_rename_items(child)
        
        return count

    def _update_item_status(self, filename, status):
        """Update the status of an item in the treeview."""
        def update_recursive(node):
            item_text = self.results_tree.item(node, "text")
            if item_text == filename:
                values = list(self.results_tree.item(node, "values"))
                if len(values) >= 3:
                    values[2] = status
                    self.results_tree.item(node, values=values)
                return True
            
            for child in self.results_tree.get_children(node):
                if update_recursive(child):
                    return True
            return False
        
        for item in self.results_tree.get_children(""):
            if update_recursive(item):
                break

    def clear_results(self):
        """Clear the results treeview."""
        for item in self.results_tree.get_children():
            self.results_tree.delete(item)
        self.status_var.set("Ready")
