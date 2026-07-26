"""
Bone Backport Tool - Converts Source 2 bone names to Source 1 ValveBiped format.
"""

import os
import re
import shutil
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from .base_tool import BaseTool, register_tool
from .utils import PlaceholderEntry, browse_folder

# Bone mapping from Source 2 to ValveBiped (Source 1)
BONE_MAPPING = {
    # Root
    "pelvis":        "ValveBiped.Bip01_Pelvis",

    # Spine
    "spine_0":       "ValveBiped.Bip01_Spine",
    "spine_1":       "ValveBiped.Bip01_Spine1",
    "spine_2":       "ValveBiped.Bip01_Spine2",
    "spine_3":       "ValveBiped.Bip01_Spine4",  # optional

    # Neck & Head
    "neck_0":        "ValveBiped.Bip01_Neck1",
    "head":          "ValveBiped.Bip01_Head1",

    # Left Arm
    "clavicle_L":            "ValveBiped.Bip01_L_Clavicle",
    "arm_upper_L":           "ValveBiped.Bip01_L_UpperArm",
    "arm_lower_L":           "ValveBiped.Bip01_L_Forearm",
    "hand_L":                "ValveBiped.Bip01_L_Hand",

    # Left Fingers
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

    # Right Arm
    "clavicle_R":            "ValveBiped.Bip01_R_Clavicle",
    "arm_upper_R":           "ValveBiped.Bip01_R_UpperArm",
    "arm_lower_R":           "ValveBiped.Bip01_R_Forearm",
    "hand_R":                "ValveBiped.Bip01_R_Hand",

    # Right Fingers
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

    # Left Leg
    "leg_upper_L":           "ValveBiped.Bip01_L_Thigh",
    "leg_lower_L":           "ValveBiped.Bip01_L_Calf",
    "ankle_L":               "ValveBiped.Bip01_L_Foot",
    "ball_L":                "ValveBiped.Bip01_L_Toe0",

    # Right Leg
    "leg_upper_R":           "ValveBiped.Bip01_R_Thigh",
    "leg_lower_R":           "ValveBiped.Bip01_R_Calf",
    "ankle_R":               "ValveBiped.Bip01_R_Foot",
    "ball_R":                "ValveBiped.Bip01_R_Toe0",
}

UNMAPPED_BONES = [
    "blender_implicit",
    "finger_index_2_L", "finger_middle_2_L", "finger_ring_2_L", "finger_pinky_2_L",
    "finger_index_2_R", "finger_middle_2_R", "finger_ring_2_R", "finger_pinky_2_R",
    "arm_upper_L_TWIST", "arm_upper_L_TWIST1", "arm_lower_L_TWIST", "arm_lower_L_TWIST1",
    "arm_upper_R_TWIST", "arm_upper_R_TWIST1", "arm_lower_R_TWIST", "arm_lower_R_TWIST1",
    "scap_0_L", "scap_0_R",
    "neck_0_TWIST", "neckNape_HLPR",
    "pect_0_L", "pect_0_R",
    "foot_pole_L", "foot_pole_R", "knee_pole_L", "knee_pole_R",
    "weapon_hand_R"
]

@register_tool
class BoneBackportTool(BaseTool):
    @property
    def name(self) -> str:
        return "Bone Backport"

    @property
    def description(self) -> str:
        return "Convert Source 2 bone names to Source 1 ValveBiped format in QC and SMD files"

    @property
    def dependencies(self) -> list:
        return []  # No external dependencies

    def create_tab(self, parent) -> ttk.Frame:
        return BoneBackportTab(parent, self.config)

class BoneBackportTab(ttk.Frame):
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

        # Folder input
        ttk.Label(input_frame, text="QC/SMD Folder:").grid(row=0, column=0, sticky="w", pady=2)
        self.folder_path = PlaceholderEntry(input_frame, placeholder="Select folder containing QC and SMD files...")
        self.folder_path.grid(row=0, column=1, sticky="ew", padx=(5, 0), pady=2)
        ttk.Button(input_frame, text="Browse",
                command=self.browse_folder).grid(row=0, column=2, padx=(5, 0), pady=2)

        input_frame.columnconfigure(1, weight=1)

        # Options section
        options_frame = ttk.LabelFrame(main_frame, text="Options", padding=10)
        options_frame.pack(fill="x", pady=(0, 10))

        self.backup_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options_frame, text="Create backup files",
                    variable=self.backup_var).pack(anchor="w")

        self.process_qc_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options_frame, text="Process QC files",
                    variable=self.process_qc_var).pack(anchor="w")

        self.process_qci_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options_frame, text="Process QCIs",
                    variable=self.process_qci_var).pack(anchor="w")

        self.process_smd_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options_frame, text="Process SMD files",
                    variable=self.process_smd_var).pack(anchor="w")

        # Custom mapping section
        mapping_frame = ttk.LabelFrame(main_frame, text="Custom Bone Mapping", padding=10)
        mapping_frame.pack(fill="both", expand=True, pady=(0, 10))

        # Instructions
        ttk.Label(mapping_frame,
                text="Add custom bone mappings (Source2Name = Source1Name):").pack(anchor="w")

        # Mapping text area
        self.mapping_text = scrolledtext.ScrolledText(mapping_frame, height=8, width=70)
        self.mapping_text.pack(fill="both", expand=True, pady=(5, 0))

        # Default mapping example
        example_text = "# Example custom mappings (one per line):\n"
        example_text += "# custom_bone_L = ValveBiped.Bip01_L_CustomBone\n"
        example_text += "# weapon_bone = ValveBiped.weapon_bone\n\n"
        self.mapping_text.insert("1.0", example_text)

        # Action buttons
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill="x", pady=(0, 10))

        ttk.Button(button_frame, text="Preview Changes",
                command=self.preview_changes).pack(side="left")
        ttk.Button(button_frame, text="Process Files",
                command=self.process_files).pack(side="left", padx=(10, 0))
        ttk.Button(button_frame, text="View Default Mapping",
                command=self.show_default_mapping).pack(side="right")

        # Status label
        self.status_label = ttk.Label(main_frame, text="Ready", foreground="green")
        self.status_label.pack(pady=(10, 0))

    def browse_folder(self):
        """Browse for folder containing QC/SMD files."""
        path = browse_folder(title="Select folder with QC/SMD files")
        if path:
            self.folder_path.set_text(path)

    def get_custom_mapping(self):
        """Parse custom bone mapping from text area."""
        custom_mapping = {}
        mapping_text = self.mapping_text.get("1.0", "end-1c")

        for line in mapping_text.split('\n'):
            line = line.strip()
            if line and not line.startswith('#'):
                if '=' in line:
                    source2, source1 = line.split('=', 1)
                    custom_mapping[source2.strip()] = source1.strip()

        return custom_mapping

    def get_full_mapping(self):
        """Get combined default + custom mapping."""
        full_mapping = BONE_MAPPING.copy()
        custom_mapping = self.get_custom_mapping()
        full_mapping.update(custom_mapping)
        return full_mapping

    def find_files(self, folder_path):
        """Find QC and SMD files in the specified folder."""
        qc_files = []
        qci_files = []
        smd_files = []

        if not os.path.exists(folder_path):
            return qc_files, qci_files, smd_files

        for root, dirs, files in os.walk(folder_path):
            for file in files:
                file_path = os.path.join(root, file)
                if file.lower().endswith('.qc'):
                    qc_files.append(file_path)
                elif file.lower().endswith('.qci'):
                    qci_files.append(file_path)
                elif file.lower().endswith('.smd'):
                    smd_files.append(file_path)

        return qc_files, qci_files, smd_files

    def process_qc_file(self, file_path, bone_mapping, preview_mode=False):
        """Process a QC file to replace bone names."""
        changes = []

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            original_content = content

            # Replace bone names
            for source2_bone, source1_bone in bone_mapping.items():
                if source2_bone in content:
                    content = content.replace(source2_bone, source1_bone)
                    changes.append(f"  {source2_bone} → {source1_bone}")

            if not preview_mode and changes:
                # Create backup if requested
                if self.backup_var.get():
                    backup_path = file_path + '.backup'
                    shutil.copy2(file_path, backup_path)

                # Write modified content
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(content)

            return changes

        except Exception as e:
            raise Exception(f"Error processing {file_path}: {e}")

    def process_smd_file(self, file_path, bone_mapping, preview_mode=False):
        """Process an SMD file to replace bone names."""
        changes = []

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            modified_lines = []

            for line_num, line in enumerate(lines):
                original_line = line

                # Replace bone names in the line
                for source2_bone, source1_bone in bone_mapping.items():
                    if source2_bone in line:
                        line = line.replace(source2_bone, source1_bone)
                        if line != original_line and f"Line {line_num + 1}: {source2_bone} → {source1_bone}" not in changes:
                            changes.append(f"  Line {line_num + 1}: {source2_bone} → {source1_bone}")

                modified_lines.append(line)

            if not preview_mode and changes:
                # Create backup if requested
                if self.backup_var.get():
                    backup_path = file_path + '.backup'
                    shutil.copy2(file_path, backup_path)

                # Write modified content
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.writelines(modified_lines)

            return changes

        except Exception as e:
            raise Exception(f"Error processing {file_path}: {e}")

    def preview_changes(self):
        """Preview what changes would be made."""
        folder_path = self.folder_path.get()
        if not folder_path:
            messagebox.showerror("Error", "Please select a folder first.")
            return

        bone_mapping = self.get_full_mapping()
        qc_files, qci_files, smd_files = self.find_files(folder_path)

        if not qc_files and not smd_files and not qci_files:
            messagebox.showinfo("No Files", "No QC, QCI, or SMD files found in the selected folder.")
            return

        # Create preview window
        preview_window = tk.Toplevel(self)
        preview_window.title("Preview Changes")
        preview_window.geometry("600x500")

        text_widget = scrolledtext.ScrolledText(preview_window, wrap="word")
        text_widget.pack(fill="both", expand=True, padx=10, pady=10)

        preview_text = "Preview of changes that would be made:\n\n"

        # Preview QC files
        if self.process_qc_var.get() and qc_files:
            preview_text += "QC Files:\n"
            for qc_file in qc_files:
                try:
                    changes = self.process_qc_file(qc_file, bone_mapping, preview_mode=True)
                    if changes:
                        preview_text += f"\n{os.path.basename(qc_file)}:\n"
                        preview_text += "\n".join(changes) + "\n"
                    else:
                        preview_text += f"\n{os.path.basename(qc_file)}: No changes needed\n"
                except Exception as e:
                    preview_text += f"\n{os.path.basename(qc_file)}: Error - {e}\n"

        # Preview QCI files
        if self.process_qci_var.get() and qci_files:
            preview_text += "\nQCI Files:\n"
            for qci_file in qci_files:
                try:
                    changes = self.process_qc_file(qci_file, bone_mapping, preview_mode=True)
                    if changes:
                        preview_text += f"\n{os.path.basename(qci_file)}:\n"
                        preview_text += "\n".join(changes) + "\n"
                    else:
                        preview_text += f"\n{os.path.basename(qci_file)}: No changes needed\n"
                except Exception as e:
                    preview_text += f"\n{os.path.basename(qci_file)}: Error - {e}\n"

        # Preview SMD files
        if self.process_smd_var.get() and smd_files:
            preview_text += "\nSMD Files:\n"
            for smd_file in smd_files[:5]:  # Limit to first 5 SMD files for preview
                try:
                    changes = self.process_smd_file(smd_file, bone_mapping, preview_mode=True)
                    if changes:
                        preview_text += f"\n{os.path.basename(smd_file)}:\n"
                        preview_text += "\n".join(changes[:10]) + "\n"  # Limit changes shown
                        if len(changes) > 10:
                            preview_text += f"  ... and {len(changes) - 10} more changes\n"
                    else:
                        preview_text += f"\n{os.path.basename(smd_file)}: No changes needed\n"
                except Exception as e:
                    preview_text += f"\n{os.path.basename(smd_file)}: Error - {e}\n"

            if len(smd_files) > 5:
                preview_text += f"\n... and {len(smd_files) - 5} more SMD files\n"

        text_widget.insert("1.0", preview_text)
        text_widget.config(state="disabled")

    def process_files(self):
        """Process all files in the selected folder."""
        folder_path = self.folder_path.get()
        if not folder_path:
            messagebox.showerror("Error", "Please select a folder first.")
            return

        bone_mapping = self.get_full_mapping()
        qc_files, qci_files, smd_files = self.find_files(folder_path)

        if not qc_files and not qci_files and not smd_files:
            messagebox.showinfo("No Files", "No QC, QCI, or SMD files found in the selected folder.")
            return

        # Confirm processing
        file_count = 0
        if self.process_qc_var.get():
            file_count += len(qc_files)
        if self.process_qci_var.get():
            file_count += len(qci_files)
        if self.process_smd_var.get():
            file_count += len(smd_files)

        result = messagebox.askyesno("Confirm Processing",
                                f"Process {file_count} files?\n"
                                f"Backups will {'be' if self.backup_var.get() else 'NOT be'} created.")

        if not result:
            return

        processed = 0
        errors = 0
        total_changes = 0

        try:
            # Process QC files
            if self.process_qc_var.get():
                for qc_file in qc_files:
                    try:
                        changes = self.process_qc_file(qc_file, bone_mapping)
                        if changes:
                            total_changes += len(changes)
                        processed += 1
                    except Exception as e:
                        print(f"Error processing QC file {qc_file}: {e}")
                        errors += 1

            # Process QCI files
            if self.process_qci_var.get():
                for qci_file in qci_files:
                    try:
                        changes = self.process_qc_file(qci_file, bone_mapping)
                        if changes:
                            total_changes += len(changes)
                        processed += 1
                    except Exception as e:
                        print(f"Error processing QCI file {qci_file}: {e}")
                        errors += 1

            # Process SMD files
            if self.process_smd_var.get():
                for smd_file in smd_files:
                    try:
                        changes = self.process_smd_file(smd_file, bone_mapping)
                        if changes:
                            total_changes += len(changes)
                        processed += 1
                    except Exception as e:
                        print(f"Error processing SMD file {smd_file}: {e}")
                        errors += 1

            # Show results
            messagebox.showinfo("Processing Complete",
                                f"Processed {processed} files successfully.\n"
                                f"Made {total_changes} bone name changes.\n"
                                f"{errors} errors occurred.")

            self.status_label.config(
                text=f"Complete: {processed} files, {total_changes} changes, {errors} errors",
                foreground="green" if errors == 0 else "orange"
            )

        except Exception as e:
            messagebox.showerror("Error", f"Processing failed: {e}")
            self.status_label.config(text="Processing failed", foreground="red")

    def show_default_mapping(self):
        """Show the default bone mapping in a new window."""
        mapping_window = tk.Toplevel(self)
        mapping_window.title("Default Bone Mapping")
        mapping_window.geometry("700x500")

        text_widget = scrolledtext.ScrolledText(mapping_window, wrap="word")
        text_widget.pack(fill="both", expand=True, padx=10, pady=10)

        mapping_text = "Default Source 2 → Source 1 Bone Mapping:\n\n"

        # Group mappings by body part
        groups = {
            "Pelvis & Spine": ["pelvis", "spine_0", "spine_1", "spine_2", "spine_3", "neck_01", "head"],
            "Left Arm": [k for k in BONE_MAPPING.keys() if ("arm_" in k or "clavicle_" in k or "hand_" in k) and k.endswith("_L")],
            "Left Fingers": [k for k in BONE_MAPPING.keys() if "finger_" in k and k.endswith("_L")],
            "Right Arm": [k for k in BONE_MAPPING.keys() if ("arm_" in k or "clavicle_" in k or "hand_" in k) and k.endswith("_R")],
            "Right Fingers": [k for k in BONE_MAPPING.keys() if "finger_" in k and k.endswith("_R")],
            "Left Leg": [k for k in BONE_MAPPING.keys() if ("leg_" in k or "ankle_" in k or "ball_" in k) and k.endswith("_L")],
            "Right Leg": [k for k in BONE_MAPPING.keys() if ("leg_" in k or "ankle_" in k or "ball_" in k) and k.endswith("_R")],
        }

        for group_name, bones in groups.items():
            mapping_text += f"{group_name}:\n"
            for bone in bones:
                if bone in BONE_MAPPING:
                    mapping_text += f"  {bone} → {BONE_MAPPING[bone]}\n"
            mapping_text += "\n"

        text_widget.insert("1.0", mapping_text)
        text_widget.config(state="disabled")