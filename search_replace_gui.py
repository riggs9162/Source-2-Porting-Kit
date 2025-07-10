import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

class SearchReplaceGui(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Batch Search & Replace (Filenames & VMT)")
        self.geometry("600x400")
        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 5, "pady": 5}
        # Folder selector
        ttk.Label(self, text="Root Folder:").grid(row=0, column=0, sticky="e", **pad)
        self.folder_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.folder_var, width=50).grid(row=0, column=1, **pad)
        ttk.Button(self, text="Browse…", command=self._browse_folder).grid(row=0, column=2, **pad)

        # Substring to replace
        ttk.Label(self, text="Search for:").grid(row=1, column=0, sticky="e", **pad)
        self.search_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.search_var, width=30).grid(row=1, column=1, **pad)

        ttk.Label(self, text="Replace with:").grid(row=2, column=0, sticky="e", **pad)
        self.replace_var = tk.StringVar(value="")
        ttk.Entry(self, textvariable=self.replace_var, width=30).grid(row=2, column=1, **pad)

        # Run button
        ttk.Button(self, text="Run", command=self._run).grid(row=3, column=0, columnspan=3, pady=(10,5))

        # Log output
        self.log = tk.Text(self, height=15, wrap="word")
        self.log.grid(row=4, column=0, columnspan=3, sticky="nsew", **pad)
        self.grid_rowconfigure(4, weight=1)
        self.grid_columnconfigure(1, weight=1)

    def _browse_folder(self):
        d = filedialog.askdirectory(title="Select root folder")
        if d:
            self.folder_var.set(d)

    def _log(self, msg):
        self.log.insert(tk.END, msg + "\n")
        self.log.see(tk.END)

    def _run(self):
        root = self.folder_var.get().strip()
        search = self.search_var.get()
        replace = self.replace_var.get()

        if not root or not os.path.isdir(root):
            messagebox.showerror("Error", "Please select a valid root folder.")
            return
        if not search:
            messagebox.showerror("Error", "Please enter a search string.")
            return

        # 1) Rename files
        for dirpath, _, filenames in os.walk(root):
            for fname in filenames:
                if search in fname:
                    new_name = fname.replace(search, replace)
                    old_path = os.path.join(dirpath, fname)
                    new_path = os.path.join(dirpath, new_name)
                    try:
                        os.rename(old_path, new_path)
                        self._log(f"Renamed: {old_path} → {new_path}")
                    except Exception as e:
                        self._log(f"Failed to rename {old_path}: {e}")

        # 2) Update .vmt contents
        for dirpath, _, filenames in os.walk(root):
            for fname in filenames:
                if fname.lower().endswith(".vmt"):
                    vmt_path = os.path.join(dirpath, fname)
                    try:
                        with open(vmt_path, "r", encoding="utf-8", errors="ignore") as f:
                            content = f.read()
                        new_content = content.replace(search, replace)
                        if new_content != content:
                            with open(vmt_path, "w", encoding="utf-8") as f:
                                f.write(new_content)
                            self._log(f"Updated VMT: {vmt_path}")
                    except Exception as e:
                        self._log(f"Failed to update {vmt_path}: {e}")

        messagebox.showinfo("Done", "Search-and-replace complete.")

if __name__ == "__main__":
    app = SearchReplaceGui()
    app.mainloop()
