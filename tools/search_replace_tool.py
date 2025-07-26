"""
Search and Replace Tool - Batch search and replace in filenames and file contents.
"""

import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from .base_tool import BaseTool, register_tool
from .utils import PlaceholderEntry, browse_folder

@register_tool
class SearchReplaceTool(BaseTool):
    @property
    def name(self) -> str:
        return "Search & Replace"

    @property
    def description(self) -> str:
        return "Batch search and replace text in filenames and file contents"

    @property
    def dependencies(self) -> list:
        return []  # No external dependencies

    def create_tab(self, parent) -> ttk.Frame:
        return SearchReplaceTab(parent, self.config)

class SearchReplaceTab(ttk.Frame):
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
        self.folder_path = PlaceholderEntry(input_frame, placeholder="Select folder to search and replace in...")
        self.folder_path.grid(row=0, column=1, sticky="ew", padx=(5, 0), pady=2)
        ttk.Button(input_frame, text="Browse",
                command=self.browse_folder).grid(row=0, column=2, padx=(5, 0), pady=2)

        input_frame.columnconfigure(1, weight=1)

        # Search and replace section
        search_frame = ttk.LabelFrame(main_frame, text="Search & Replace", padding=10)
        search_frame.pack(fill="x", pady=(0, 10))

        ttk.Label(search_frame, text="Search for:").grid(row=0, column=0, sticky="w", pady=2)
        self.search_text = tk.StringVar()
        search_entry = ttk.Entry(search_frame, textvariable=self.search_text, width=40)
        search_entry.grid(row=0, column=1, sticky="ew", padx=(5, 0), pady=2)

        ttk.Label(search_frame, text="Replace with:").grid(row=1, column=0, sticky="w", pady=2)
        self.replace_text = tk.StringVar()
        replace_entry = ttk.Entry(search_frame, textvariable=self.replace_text, width=40)
        replace_entry.grid(row=1, column=1, sticky="ew", padx=(5, 0), pady=2)

        search_frame.columnconfigure(1, weight=1)

        # Options section
        options_frame = ttk.LabelFrame(main_frame, text="Options", padding=10)
        options_frame.pack(fill="x", pady=(0, 10))

        # Target options
        target_frame = ttk.Frame(options_frame)
        target_frame.pack(fill="x", pady=(0, 5))

        ttk.Label(target_frame, text="Apply to:").pack(side="left")

        self.rename_files_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(target_frame, text="Filenames",
                    variable=self.rename_files_var).pack(side="left", padx=(10, 0))

        self.modify_contents_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(target_frame, text="File contents",
                    variable=self.modify_contents_var).pack(side="left", padx=(10, 0))

        # File type filters
        filter_frame = ttk.Frame(options_frame)
        filter_frame.pack(fill="x", pady=(5, 0))

        ttk.Label(filter_frame, text="File types:").grid(row=0, column=0, sticky="w", pady=2)
        self.file_extensions = tk.StringVar(value="*.vmt, *.qc, *.smd, *.txt")
        ext_entry = ttk.Entry(filter_frame, textvariable=self.file_extensions, width=50)
        ext_entry.grid(row=0, column=1, sticky="ew", padx=(5, 0), pady=2)

        ttk.Label(filter_frame, text="(comma-separated, e.g. *.vmt, *.qc)").grid(row=1, column=1, sticky="w", padx=(5, 0))

        filter_frame.columnconfigure(1, weight=1)

        # Search options
        search_options_frame = ttk.Frame(options_frame)
        search_options_frame.pack(fill="x", pady=(10, 0))

        self.case_sensitive_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(search_options_frame, text="Case sensitive",
                        variable=self.case_sensitive_var).pack(side="left")

        self.whole_words_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(search_options_frame, text="Whole words only",
                        variable=self.whole_words_var).pack(side="left", padx=(10, 0))

        self.create_backup_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(search_options_frame, text="Create backups",
                        variable=self.create_backup_var).pack(side="left", padx=(10, 0))

        # Action buttons
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill="x", pady=(0, 10))

        ttk.Button(button_frame, text="Preview Changes",
                    command=self.preview_changes).pack(side="left")
        ttk.Button(button_frame, text="Apply Changes",
                    command=self.apply_changes).pack(side="left", padx=(10, 0))

        # Results section
        results_frame = ttk.LabelFrame(main_frame, text="Results", padding=10)
        results_frame.pack(fill="both", expand=True)

        self.results_text = ScrolledText(results_frame, height=12, width=70)
        self.results_text.pack(fill="both", expand=True)

        # Status label
        self.status_label = ttk.Label(main_frame, text="Ready", foreground="green")
        self.status_label.pack(pady=(10, 0))

    def browse_folder(self):
        """Browse for target folder."""
        path = browse_folder(title="Select folder to search and replace in")
        if path:
            self.folder_path.set_text(path)

    def get_file_extensions(self):
        """Parse file extensions from the input field."""
        ext_text = self.file_extensions.get().strip()
        if not ext_text:
            return []

        extensions = []
        for ext in ext_text.split(','):
            ext = ext.strip()
            if ext:
                if not ext.startswith('*.'):
                    ext = '*.' + ext.lstrip('.')
                extensions.append(ext.lower())

        return extensions

    def find_target_files(self, folder_path):
        """Find files that match the specified extensions."""
        target_files = []
        extensions = self.get_file_extensions()

        if not os.path.exists(folder_path):
            return target_files

        for root, dirs, files in os.walk(folder_path):
            for file in files:
                file_path = os.path.join(root, file)

                # Check if file matches any extension filter
                if not extensions:  # If no filter, include all files
                    target_files.append(file_path)
                else:
                    for ext in extensions:
                        if file.lower().endswith(ext[1:]):  # Remove * from *.ext
                            target_files.append(file_path)
                            break

        return target_files

    def search_in_filename(self, filename, search_text, case_sensitive, whole_words):
        """Check if search text is found in filename."""
        search_target = filename if case_sensitive else filename.lower()
        search_term = search_text if case_sensitive else search_text.lower()

        if whole_words:
            import re
            pattern = r'\\b' + re.escape(search_term) + r'\\b'
            return bool(re.search(pattern, search_target, 0 if case_sensitive else re.IGNORECASE))
        else:
            return search_term in search_target

        def search_in_file_content(self, file_path, search_text, case_sensitive, whole_words):
            """Search for text in file content and return line numbers where found."""
        matches = []

        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line_num, line in enumerate(f, 1):
                    search_target = line if case_sensitive else line.lower()
                    search_term = search_text if case_sensitive else search_text.lower()

                    if whole_words:
                        import re
                        pattern = r'\\b' + re.escape(search_term) + r'\\b'
                        if re.search(pattern, search_target, 0 if case_sensitive else re.IGNORECASE):
                            matches.append(line_num)
                    else:
                        if search_term in search_target:
                            matches.append(line_num)

        except Exception as e:
            print(f"Error reading {file_path}: {e}")

        return matches

    def replace_in_filename(self, file_path, search_text, replace_text, case_sensitive, whole_words):
        """Replace text in filename and return new path."""
        directory = os.path.dirname(file_path)
        filename = os.path.basename(file_path)

        if whole_words:
            import re
            pattern = r'\\b' + re.escape(search_text) + r'\\b'
            new_filename = re.sub(pattern, replace_text, filename,
                                flags=0 if case_sensitive else re.IGNORECASE)
        else:
            if case_sensitive:
                new_filename = filename.replace(search_text, replace_text)
            else:
                # Case-insensitive replacement
                import re
                new_filename = re.sub(re.escape(search_text), replace_text, filename, flags=re.IGNORECASE)

        if new_filename != filename:
            return os.path.join(directory, new_filename)
        else:
            return None  # No change

    def replace_in_file_content(self, file_path, search_text, replace_text, case_sensitive, whole_words, create_backup):
        """Replace text in file content."""
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()

            original_content = content

            if whole_words:
                import re
                pattern = r'\\b' + re.escape(search_text) + r'\\b'
                new_content = re.sub(pattern, replace_text, content,
                                    flags=0 if case_sensitive else re.IGNORECASE)
            else:
                if case_sensitive:
                    new_content = content.replace(search_text, replace_text)
                else:
                    import re
                    new_content = re.sub(re.escape(search_text), replace_text, content, flags=re.IGNORECASE)

            if new_content != original_content:
                # Create backup if requested
                if create_backup:
                    backup_path = file_path + '.backup'
                    with open(backup_path, 'w', encoding='utf-8') as f:
                        f.write(original_content)

                # Write modified content
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(new_content)

                # Count replacements
                if case_sensitive:
                    count = original_content.count(search_text)
                else:
                    count = original_content.lower().count(search_text.lower())

                return count

            return 0

        except Exception as e:
            raise Exception(f"Error processing {file_path}: {e}")

    def preview_changes(self):
        """Preview what changes would be made."""
        folder_path = self.folder_path.get()
        search_text = self.search_text.get()
        replace_text = self.replace_text.get()

        if not folder_path:
            messagebox.showerror("Error", "Please select a folder first.")
            return

        if not search_text:
            messagebox.showerror("Error", "Please enter text to search for.")
            return

        case_sensitive = self.case_sensitive_var.get()
        whole_words = self.whole_words_var.get()
        rename_files = self.rename_files_var.get()
        modify_contents = self.modify_contents_var.get()

        if not rename_files and not modify_contents:
            messagebox.showerror("Error", "Please select at least one option (filenames or contents).")
            return

        target_files = self.find_target_files(folder_path)

        if not target_files:
            self.results_text.delete("1.0", "end")
            self.results_text.insert("1.0", "No files found matching the specified criteria.")
            return

        preview_text = f"Preview of changes for search term '{search_text}':\\n\\n"

        filename_changes = 0
        content_changes = 0

        for file_path in target_files:
            file_results = []

            # Check filename changes
            if rename_files:
                filename = os.path.basename(file_path)
                if self.search_in_filename(filename, search_text, case_sensitive, whole_words):
                    new_path = self.replace_in_filename(file_path, search_text, replace_text,
                                                        case_sensitive, whole_words)
                    if new_path:
                        file_results.append(f"  Filename: {filename} → {os.path.basename(new_path)}")
                        filename_changes += 1

            # Check content changes
            if modify_contents:
                matches = self.search_in_file_content(file_path, search_text, case_sensitive, whole_words)
                if matches:
                    file_results.append(f"  Content: Found on lines {', '.join(map(str, matches[:5]))}")
                    if len(matches) > 5:
                        file_results.append(f"    ... and {len(matches) - 5} more lines")
                    content_changes += 1

            if file_results:
                preview_text += f"{os.path.relpath(file_path, folder_path)}:\\n"
                preview_text += "\\n".join(file_results) + "\\n\\n"

        preview_text += f"Summary:\\n"
        preview_text += f"Files with filename changes: {filename_changes}\\n"
        preview_text += f"Files with content changes: {content_changes}\\n"

        self.results_text.delete("1.0", "end")
        self.results_text.insert("1.0", preview_text)

        self.status_label.config(text=f"Preview complete: {filename_changes + content_changes} files affected",
                                foreground="blue")

    def apply_changes(self):
        """Apply the search and replace changes."""
        folder_path = self.folder_path.get()
        search_text = self.search_text.get()
        replace_text = self.replace_text.get()

        if not folder_path:
            messagebox.showerror("Error", "Please select a folder first.")
            return

        if not search_text:
            messagebox.showerror("Error", "Please enter text to search for.")
            return

        case_sensitive = self.case_sensitive_var.get()
        whole_words = self.whole_words_var.get()
        rename_files = self.rename_files_var.get()
        modify_contents = self.modify_contents_var.get()
        create_backup = self.create_backup_var.get()

        if not rename_files and not modify_contents:
            messagebox.showerror("Error", "Please select at least one option (filenames or contents).")
            return

        # Confirm the operation
        result = messagebox.askyesno("Confirm Changes",
                                    f"Apply search and replace operation?\\n\\n"
                                    f"Search: '{search_text}'\\n"
                                    f"Replace: '{replace_text}'\\n\\n"
                                    f"Target: {folder_path}\\n"
                                    f"Backups: {'Yes' if create_backup else 'No'}")
        if not result:
            return

        target_files = self.find_target_files(folder_path)

        if not target_files:
            messagebox.showinfo("No Files", "No files found matching the specified criteria.")
            return

        files_renamed = 0
        files_content_changed = 0
        total_replacements = 0
        errors = []

        results_text = f"Search and Replace Results:\\n\\n"

        try:
            for file_path in target_files:
                file_changed = False
                file_results = []

                # Handle filename changes
                if rename_files:
                    filename = os.path.basename(file_path)
                    if self.search_in_filename(filename, search_text, case_sensitive, whole_words):
                        new_path = self.replace_in_filename(file_path, search_text, replace_text,
                                                            case_sensitive, whole_words)
                        if new_path:
                            try:
                                os.rename(file_path, new_path)
                                file_results.append(f"  Renamed: {filename} → {os.path.basename(new_path)}")
                                files_renamed += 1
                                file_changed = True
                                file_path = new_path  # Update path for content processing
                            except Exception as e:
                                errors.append(f"Error renaming {file_path}: {e}")

                # Handle content changes
                if modify_contents:
                    matches = self.search_in_file_content(file_path, search_text, case_sensitive, whole_words)
                    if matches:
                        try:
                            replacements = self.replace_in_file_content(file_path, search_text, replace_text,
                                                                        case_sensitive, whole_words, create_backup)
                            if replacements > 0:
                                file_results.append(f"  Content: {replacements} replacements made")
                                files_content_changed += 1
                                total_replacements += replacements
                                file_changed = True
                        except Exception as e:
                            errors.append(f"Error modifying content of {file_path}: {e}")

                if file_changed:
                    results_text += f"{os.path.relpath(file_path, folder_path)}:\\n"
                    results_text += "\\n".join(file_results) + "\\n\\n"

            # Summary
            results_text += f"Operation Summary:\\n"
            results_text += f"Files renamed: {files_renamed}\\n"
            results_text += f"Files with content changes: {files_content_changed}\\n"
            results_text += f"Total text replacements: {total_replacements}\\n"
            results_text += f"Errors: {len(errors)}\\n\\n"

            if errors:
                results_text += "Errors encountered:\\n"
                for error in errors[:10]:  # Show first 10 errors
                    results_text += f"  {error}\\n"
                if len(errors) > 10:
                    results_text += f"  ... and {len(errors) - 10} more errors\\n"

            self.results_text.delete("1.0", "end")
            self.results_text.insert("1.0", results_text)

            # Show completion message
            messagebox.showinfo("Operation Complete",
                                f"Search and replace completed.\\n\\n"
                                f"Files renamed: {files_renamed}\\n"
                                f"Files with content changes: {files_content_changed}\\n"
                                f"Total replacements: {total_replacements}\\n"
                                f"Errors: {len(errors)}")

            self.status_label.config(
                text=f"Complete: {files_renamed + files_content_changed} files processed, {len(errors)} errors",
                foreground="green" if len(errors) == 0 else "orange"
            )

        except Exception as e:
            messagebox.showerror("Error", f"Operation failed: {e}")
            self.status_label.config(text="Operation failed", foreground="red")
