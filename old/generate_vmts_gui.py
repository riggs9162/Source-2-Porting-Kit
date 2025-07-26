import os
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

class VMTGeneratorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("VMT Generator")
        self.geometry("600x400")
        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 5, "pady": 5}

        # Folder selector
        ttk.Label(self, text="VTF Folder:").grid(row=0, column=0, sticky="e", **pad)
        self.folder_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.folder_var, width=50).grid(row=0, column=1, **pad)
        ttk.Button(self, text="Browse…", command=self._browse_folder).grid(row=0, column=2, **pad)

        # Template VMT selector
        ttk.Label(self, text="Template VMT:").grid(row=1, column=0, sticky="e", **pad)
        self.template_var = tk.StringVar()
        ttk.Entry(self, textvariable=self.template_var, width=50).grid(row=1, column=1, **pad)
        ttk.Button(self, text="Browse…", command=self._browse_template).grid(row=1, column=2, **pad)

        # Generate button
        self.gen_button = ttk.Button(self, text="Generate VMTs", command=self._generate)
        self.gen_button.grid(row=2, column=0, columnspan=3, pady=(10,5))

        # Log area
        ttk.Label(self, text="Log:").grid(row=3, column=0, sticky="nw", **pad)
        self.log = ScrolledText(self, height=15, wrap="word")
        self.log.grid(row=3, column=1, columnspan=2, sticky="nsew", **pad)

        # Configure resizing
        self.grid_rowconfigure(3, weight=1)
        self.grid_columnconfigure(1, weight=1)

    def _browse_folder(self):
        d = filedialog.askdirectory(title="Select VTF folder")
        if d:
            self.folder_var.set(d)

    def _browse_template(self):
        f = filedialog.askopenfilename(title="Select template VMT",
                                       filetypes=[("VMT files","*.vmt"),("All files","*.*")])
        if f:
            self.template_var.set(f)

    def _log(self, msg):
        self.log.insert(tk.END, msg + "\n")
        self.log.see(tk.END)

    def _generate(self):
        folder = self.folder_var.get().strip()
        template = self.template_var.get().strip()
        self.log.delete("1.0","end")

        # Validate
        if not os.path.isdir(folder):
            messagebox.showerror("Error", f"Invalid folder:\n{folder}")
            return
        if not os.path.isfile(template):
            messagebox.showerror("Error", f"Invalid template VMT:\n{template}")
            return

        # Load template text
        try:
            with open(template, "r", encoding="utf-8") as f:
                template_text = f.read()
        except Exception as e:
            messagebox.showerror("Error", f"Failed to read template:\n{e}")
            return

        orig_base = os.path.splitext(os.path.basename(template))[0]
        files = os.listdir(folder)
        vtfs = [f for f in files if f.lower().endswith(".vtf") and not f.lower().endswith("_normal.vtf")]

        if not vtfs:
            self._log("No base VTF files found.")
            return

        for vtf in sorted(vtfs):
            base = os.path.splitext(vtf)[0]
            vmt_name = base + ".vmt"
            vmt_path = os.path.join(folder, vmt_name)
            if os.path.exists(vmt_path):
                self._log(f"Skipping {vmt_name}: already exists")
                continue
            new_text = template_text.replace(orig_base, base)
            try:
                with open(vmt_path, "w", encoding="utf-8") as outf:
                    outf.write(new_text)
                self._log(f"Created {vmt_name}")
            except Exception as e:
                self._log(f"Error writing {vmt_name}: {e}")

        messagebox.showinfo("Done", "VMT generation complete.")

if __name__ == "__main__":
    app = VMTGeneratorApp()
    app.mainloop()
