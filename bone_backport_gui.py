import os
import re
import shutil
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext

# your mapping from Source 2 → ValveBiped (Source 1)
MAPPING = {
    # Pelvis & Spine
    "pelvis":        "ValveBiped.Bip01_Pelvis",
    "spine_0":       "ValveBiped.Bip01_Spine",
    "spine_1":       "ValveBiped.Bip01_Spine1",
    "spine_2":       "ValveBiped.Bip01_Spine2",
    "spine_3":       "ValveBiped.Bip01_Spine4",
    # Left Arm & Fingers
    "clavicle_L":            "ValveBiped.Bip01_L_Clavicle",
    "arm_upper_L":           "ValveBiped.Bip01_L_UpperArm",
    "arm_lower_L":           "ValveBiped.Bip01_L_Forearm",
    "hand_L":                "ValveBiped.Bip01_L_Hand",
    "finger_thumb_0_L":      "ValveBiped.Bip01_L_Finger0",
    "finger_thumb_1_L":      "ValveBiped.Bip01_L_Finger01",
    "finger_thumb_2_L":      "ValveBiped.Bip01_L_Finger02",
    "finger_index_meta_L":   "ValveBiped.Bip01_L_Finger1",
    "finger_index_0_L":      "ValveBiped.Bip01_L_Finger11",
    "finger_index_1_L":      "ValveBiped.Bip01_L_Finger12",
    "finger_middle_meta_L":  "ValveBiped.Bip01_L_Finger2",
    "finger_middle_0_L":     "ValveBiped.Bip01_L_Finger21",
    "finger_middle_1_L":     "ValveBiped.Bip01_L_Finger22",
    "finger_ring_meta_L":    "ValveBiped.Bip01_L_Finger3",
    "finger_ring_0_L":       "ValveBiped.Bip01_L_Finger31",
    "finger_ring_1_L":       "ValveBiped.Bip01_L_Finger32",
    "finger_pinky_meta_L":   "ValveBiped.Bip01_L_Finger4",
    "finger_pinky_0_L":      "ValveBiped.Bip01_L_Finger41",
    "finger_pinky_1_L":      "ValveBiped.Bip01_L_Finger42",
    # Right Arm & Fingers
    "clavicle_R":            "ValveBiped.Bip01_R_Clavicle",
    "arm_upper_R":           "ValveBiped.Bip01_R_UpperArm",
    "arm_lower_R":           "ValveBiped.Bip01_R_Forearm",
    "hand_R":                "ValveBiped.Bip01_R_Hand",
    "finger_thumb_0_R":      "ValveBiped.Bip01_R_Finger0",
    "finger_thumb_1_R":      "ValveBiped.Bip01_R_Finger01",
    "finger_thumb_2_R":      "ValveBiped.Bip01_R_Finger02",
    "finger_index_meta_R":   "ValveBiped.Bip01_R_Finger1",
    "finger_index_0_R":      "ValveBiped.Bip01_R_Finger11",
    "finger_index_1_R":      "ValveBiped.Bip01_R_Finger12",
    "finger_middle_meta_R":  "ValveBiped.Bip01_R_Finger2",
    "finger_middle_0_R":     "ValveBiped.Bip01_R_Finger21",
    "finger_middle_1_R":     "ValveBiped.Bip01_R_Finger22",
    "finger_ring_meta_R":    "ValveBiped.Bip01_R_Finger3",
    "finger_ring_0_R":       "ValveBiped.Bip01_R_Finger31",
    "finger_ring_1_R":       "ValveBiped.Bip01_R_Finger32",
    "finger_pinky_meta_R":   "ValveBiped.Bip01_R_Finger4",
    "finger_pinky_0_R":      "ValveBiped.Bip01_R_Finger41",
    "finger_pinky_1_R":      "ValveBiped.Bip01_R_Finger42",
    # Head & Neck
    "neck_0":        "ValveBiped.Bip01_Neck1",
    "head":          "ValveBiped.Bip01_Head1",
    # Left Leg & Toes
    "leg_upper_L":   "ValveBiped.Bip01_L_Thigh",
    "leg_lower_L":   "ValveBiped.Bip01_L_Calf",
    "ankle_L":       "ValveBiped.Bip01_L_Foot",
    "ball_L":        "ValveBiped.Bip01_L_Toe0",
    # Right Leg & Toes
    "leg_upper_R":   "ValveBiped.Bip01_R_Thigh",
    "leg_lower_R":   "ValveBiped.Bip01_R_Calf",
    "ankle_R":       "ValveBiped.Bip01_R_Foot",
    "ball_R":        "ValveBiped.Bip01_R_Toe0",
}

class BoneRenamerApp:
    def __init__(self, master):
        self.master = master
        master.title("SMD Bone Renamer")
        master.resizable(False, False)

        tk.Label(master, text="Select folder with .smd files:").grid(row=0, column=0, padx=10, pady=5)
        tk.Button(master, text="Choose Folder", command=self.choose_folder).grid(row=0, column=1, padx=10, pady=5)
        self.process_btn = tk.Button(master, text="Process", state=tk.DISABLED, command=self.process_folder)
        self.process_btn.grid(row=1, column=0, columnspan=2, pady=5)

        self.log = scrolledtext.ScrolledText(master, width=70, height=20, state=tk.DISABLED)
        self.log.grid(row=2, column=0, columnspan=2, padx=10, pady=5)

    def log_msg(self, msg):
        self.log.config(state=tk.NORMAL)
        self.log.insert(tk.END, msg + "\n")
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

    def choose_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.folder = folder
            self.process_btn.config(state=tk.NORMAL)
            self.log_msg(f"Selected: {folder}")

    def process_folder(self):
        smd_count = 0
        for root, _, files in os.walk(self.folder):
            for fname in files:
                if fname.lower().endswith(".smd"):
                    smd_count += 1
                    fullpath = os.path.join(root, fname)
                    self.log_msg(f"→ Processing {fullpath}")
                    with open(fullpath, "r", encoding="utf-8", errors="ignore") as f:
                        lines = f.readlines()

                    new_lines = []
                    in_nodes = False
                    for line in lines:
                        stripped = line.strip()
                        if stripped == "nodes":
                            in_nodes = True
                            new_lines.append(line)
                            continue
                        if in_nodes and stripped == "end":
                            in_nodes = False
                            new_lines.append(line)
                            continue

                        if in_nodes:
                            m = re.match(r'(\s*\d+)\s+"([^"]+)"(.*)', line)
                            if m:
                                idx, name, rest = m.groups()
                                new_name = MAPPING.get(name, name)
                                if new_name != name:
                                    self.log_msg(f"    Renamed '{name}' → '{new_name}'")
                                line = f'{idx} "{new_name}"{rest}\n'
                        new_lines.append(line)

                    # backup & overwrite
                    bak_path = fullpath + ".bak"
                    shutil.copy(fullpath, bak_path)
                    with open(fullpath, "w", encoding="utf-8") as f:
                        f.writelines(new_lines)

        messagebox.showinfo("Done", f"Processed {smd_count} .smd files.\nBackups have .smd.bak extensions.")
        self.log_msg("All done!")

if __name__ == "__main__":
    root = tk.Tk()
    app = BoneRenamerApp(root)
    root.mainloop()
