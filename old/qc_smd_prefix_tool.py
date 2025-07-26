import tkinter as tk
from tkinter import filedialog, messagebox
import os

class QCSMDEditorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("QC + SMD Prefix Tool")
        self.root.geometry("500x300")

        self.qc_path = ""
        self.smd_path = ""

        tk.Button(root, text="Select QC File", command=self.select_qc).pack(pady=5)
        tk.Button(root, text="Select SMD File", command=self.select_smd).pack(pady=5)

        tk.Label(root, text="Model Prefix (adds to QC + renames SMD):").pack()
        self.model_prefix_entry = tk.Entry(root)
        self.model_prefix_entry.pack(pady=5, fill=tk.X, padx=10)

        tk.Label(root, text="Texture Prefix (adds to materials in SMD):").pack()
        self.texture_prefix_entry = tk.Entry(root)
        self.texture_prefix_entry.pack(pady=5, fill=tk.X, padx=10)

        tk.Button(root, text="Apply and Save", command=self.apply_changes).pack(pady=15)

    def select_qc(self):
        path = filedialog.askopenfilename(filetypes=[("QC Files", "*.qc")])
        if path:
            self.qc_path = path
            messagebox.showinfo("QC Selected", f"QC File selected: {os.path.basename(path)}")

    def select_smd(self):
        path = filedialog.askopenfilename(filetypes=[("SMD Files", "*.smd")])
        if path:
            self.smd_path = path
            messagebox.showinfo("SMD Selected", f"SMD File selected: {os.path.basename(path)}")

    def apply_changes(self):
        if not self.qc_path or not self.smd_path:
            messagebox.showerror("Missing File", "You must select both a QC and an SMD file.")
            return

        model_prefix = self.model_prefix_entry.get().strip()
        texture_prefix = self.texture_prefix_entry.get().strip()

        if not model_prefix and not texture_prefix:
            messagebox.showerror("Missing Prefixes", "Please enter at least one prefix.")
            return

        # Read and modify QC file
        with open(self.qc_path, "r") as qc_file:
            qc_lines = qc_file.readlines()

        new_qc_lines = []
        for line in qc_lines:
            if "$modelname" in line.lower():
                parts = line.strip().split()
                if len(parts) >= 2:
                    path, ext = os.path.splitext(parts[1])
                    new_line = f'{parts[0]} {path}{model_prefix}{ext}\n'
                    new_qc_lines.append(new_line)
                else:
                    new_qc_lines.append(line)
            elif ".smd" in line.lower():
                smd_line = line.strip().replace(".smd", f"{model_prefix}.smd")
                new_qc_lines.append(smd_line + "\n")
            else:
                new_qc_lines.append(line)

        new_qc_path = self.qc_path.replace(".qc", f"{model_prefix}.qc")
        with open(new_qc_path, "w") as out_qc:
            out_qc.writelines(new_qc_lines)

        # Read and modify SMD file
        with open(self.smd_path, "r") as smd_file:
            smd_lines = smd_file.readlines()

        new_smd_lines = []
        for line in smd_lines:
            if "materials/" in line.lower():
                new_smd_lines.append(line.replace("materials/", f"materials/{texture_prefix}"))
            else:
                new_smd_lines.append(line)

        smd_dir = os.path.dirname(self.smd_path)
        smd_base = os.path.splitext(os.path.basename(self.smd_path))[0]
        new_smd_name = f"{smd_base}{model_prefix}.smd"
        new_smd_path = os.path.join(smd_dir, new_smd_name)
        with open(new_smd_path, "w") as out_smd:
            out_smd.writelines(new_smd_lines)

        messagebox.showinfo("Done", f"Saved:\nQC: {new_qc_path}\nSMD: {new_smd_path}")

if __name__ == "__main__":
    root = tk.Tk()
    app = QCSMDEditorApp(root)
    root.mainloop()