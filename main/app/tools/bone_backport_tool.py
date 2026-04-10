import os
import shutil
import logging
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, 
    QLineEdit, QPushButton, QCheckBox, QGroupBox, 
    QTextEdit, QFileDialog, QMessageBox, QScrollArea
)
from PySide6.QtCore import Qt, QThread, Signal
from .base_tool import BaseTool

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

class ProcessingThread(QThread):
    """Thread for processing files to avoid freezing the UI"""
    progress_signal = Signal(str)
    finished_signal = Signal(int, int, int)  # processed, changes, errors
    error_signal = Signal(str)

    def __init__(self, folder_path, bone_mapping, options):
        super().__init__()
        self.folder_path = folder_path
        self.bone_mapping = bone_mapping
        self.options = options
        self.is_running = True

    def run(self):
        try:
            qc_files, qci_files, smd_files = self.find_files(self.folder_path)
            
            processed = 0
            errors = 0
            total_changes = 0

            # Process QC files
            if self.options.get('process_qc'):
                for qc_file in qc_files:
                    if not self.is_running: break
                    try:
                        self.progress_signal.emit(f"Processing QC: {os.path.basename(qc_file)}")
                        changes = self.process_qc_file(qc_file, self.bone_mapping)
                        if changes:
                            total_changes += len(changes)
                        processed += 1
                    except Exception as e:
                        self.error_signal.emit(f"Error processing QC file {qc_file}: {e}")
                        errors += 1

            # Process QCI files
            if self.options.get('process_qci'):
                for qci_file in qci_files:
                    if not self.is_running: break
                    try:
                        self.progress_signal.emit(f"Processing QCI: {os.path.basename(qci_file)}")
                        changes = self.process_qc_file(qci_file, self.bone_mapping)
                        if changes:
                            total_changes += len(changes)
                        processed += 1
                    except Exception as e:
                        self.error_signal.emit(f"Error processing QCI file {qci_file}: {e}")
                        errors += 1

            # Process SMD files
            if self.options.get('process_smd'):
                for smd_file in smd_files:
                    if not self.is_running: break
                    try:
                        self.progress_signal.emit(f"Processing SMD: {os.path.basename(smd_file)}")
                        changes = self.process_smd_file(smd_file, self.bone_mapping)
                        if changes:
                            total_changes += len(changes)
                        processed += 1
                    except Exception as e:
                        self.error_signal.emit(f"Error processing SMD file {smd_file}: {e}")
                        errors += 1

            self.finished_signal.emit(processed, total_changes, errors)

        except Exception as e:
            self.error_signal.emit(f"Critical error: {e}")

    def stop(self):
        self.is_running = False

    def find_files(self, folder_path):
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

    def process_qc_file(self, file_path, bone_mapping):
        changes = []
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()

            original_content = content

            for source2_bone, source1_bone in bone_mapping.items():
                if source2_bone in content:
                    content = content.replace(source2_bone, source1_bone)
                    changes.append(f"  {source2_bone} -> {source1_bone}")

            if changes:
                if self.options.get('backup'):
                    backup_path = file_path + '.backup'
                    shutil.copy2(file_path, backup_path)

                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(content)

            return changes
        except Exception as e:
            raise Exception(f"Error processing {file_path}: {e}")

    def process_smd_file(self, file_path, bone_mapping):
        changes = []
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            modified_lines = []

            for line_num, line in enumerate(lines):
                original_line = line
                
                for source2_bone, source1_bone in bone_mapping.items():
                    if source2_bone in line:
                        line = line.replace(source2_bone, source1_bone)
                        if line != original_line:
                            changes.append(f"  Line {line_num + 1}: {source2_bone} -> {source1_bone}")

                modified_lines.append(line)

            if changes:
                if self.options.get('backup'):
                    backup_path = file_path + '.backup'
                    shutil.copy2(file_path, backup_path)

                with open(file_path, 'w', encoding='utf-8') as f:
                    f.writelines(modified_lines)

            return changes
        except Exception as e:
            raise Exception(f"Error processing {file_path}: {e}")


class BoneBackportTool(BaseTool):
    def __init__(self):
        super().__init__("Bone Backport")
        self.setup_tool_ui()
        self.processing_thread = None

    def setup_tool_ui(self):
        # Input section
        input_group = QGroupBox("Input")
        input_layout = QHBoxLayout()
        
        input_layout.addWidget(QLabel("QC/SMD Folder:"))
        self.folder_input = QLineEdit()
        self.folder_input.setPlaceholderText("Select folder containing QC and SMD files...")
        input_layout.addWidget(self.folder_input)
        
        browse_btn = QPushButton("Browse")
        browse_btn.clicked.connect(self.browse_folder)
        input_layout.addWidget(browse_btn)
        
        input_group.setLayout(input_layout)
        self.content_layout.addWidget(input_group)

        # Options section
        options_group = QGroupBox("Options")
        options_layout = QVBoxLayout()
        
        self.backup_check = QCheckBox("Create backup files")
        self.backup_check.setChecked(True)
        options_layout.addWidget(self.backup_check)
        
        self.process_qc_check = QCheckBox("Process QC files")
        self.process_qc_check.setChecked(True)
        options_layout.addWidget(self.process_qc_check)
        
        self.process_qci_check = QCheckBox("Process QCIs")
        self.process_qci_check.setChecked(True)
        options_layout.addWidget(self.process_qci_check)
        
        self.process_smd_check = QCheckBox("Process SMD files")
        self.process_smd_check.setChecked(True)
        options_layout.addWidget(self.process_smd_check)
        
        options_group.setLayout(options_layout)
        self.content_layout.addWidget(options_group)

        # Custom mapping section
        mapping_group = QGroupBox("Custom Bone Mapping")
        mapping_layout = QVBoxLayout()
        
        mapping_layout.addWidget(QLabel("Add custom bone mappings (Source2Name = Source1Name):"))
        
        self.mapping_text = QTextEdit()
        self.mapping_text.setPlaceholderText(
            "# Example custom mappings (one per line):\n"
            "# custom_bone_L = ValveBiped.Bip01_L_CustomBone\n"
            "# weapon_bone = ValveBiped.weapon_bone"
        )
        self.mapping_text.setMinimumHeight(100)
        mapping_layout.addWidget(self.mapping_text)
        
        mapping_group.setLayout(mapping_layout)
        self.content_layout.addWidget(mapping_group)

        # Action buttons
        btn_layout = QHBoxLayout()
        
        self.process_btn = QPushButton("Process Files")
        self.process_btn.clicked.connect(self.process_files)
        btn_layout.addWidget(self.process_btn)
        
        self.default_mapping_btn = QPushButton("View Default Mapping")
        self.default_mapping_btn.clicked.connect(self.show_default_mapping)
        btn_layout.addWidget(self.default_mapping_btn)
        
        self.content_layout.addLayout(btn_layout)
        
        # Add stretch to push everything up
        self.content_layout.addStretch()

    def browse_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder")
        if folder:
            self.folder_input.setText(folder)

    def get_custom_mapping(self):
        custom_mapping = {}
        text = self.mapping_text.toPlainText()
        
        for line in text.split('\n'):
            line = line.strip()
            if line and not line.startswith('#'):
                if '=' in line:
                    parts = line.split('=', 1)
                    if len(parts) == 2:
                        source2, source1 = parts
                        custom_mapping[source2.strip()] = source1.strip()
        
        return custom_mapping

    def get_full_mapping(self):
        full_mapping = BONE_MAPPING.copy()
        custom_mapping = self.get_custom_mapping()
        full_mapping.update(custom_mapping)
        return full_mapping

    def process_files(self):
        folder_path = self.folder_input.text()
        if not folder_path:
            QMessageBox.warning(self, "Error", "Please select a folder first.")
            return

        if not os.path.exists(folder_path):
            QMessageBox.warning(self, "Error", "Selected folder does not exist.")
            return

        options = {
            'backup': self.backup_check.isChecked(),
            'process_qc': self.process_qc_check.isChecked(),
            'process_qci': self.process_qci_check.isChecked(),
            'process_smd': self.process_smd_check.isChecked()
        }

        bone_mapping = self.get_full_mapping()

        # Disable UI
        self.process_btn.setEnabled(False)
        self.log("Starting processing...")

        self.processing_thread = ProcessingThread(folder_path, bone_mapping, options)
        self.processing_thread.progress_signal.connect(self.log)
        self.processing_thread.error_signal.connect(self.log_error)
        self.processing_thread.finished_signal.connect(self.on_processing_finished)
        self.processing_thread.start()

    def on_processing_finished(self, processed, changes, errors):
        self.process_btn.setEnabled(True)
        self.log(f"Processing complete. Processed: {processed}, Changes: {changes}, Errors: {errors}")
        QMessageBox.information(self, "Complete", 
                              f"Processing Complete\n\n"
                              f"Files Processed: {processed}\n"
                              f"Changes Made: {changes}\n"
                              f"Errors: {errors}")

    def show_default_mapping(self):
        mapping_text = "Default Source 2 -> Source 1 Bone Mapping:\n\n"
        
        # Group mappings by body part (simplified grouping for display)
        for bone, mapped in BONE_MAPPING.items():
            mapping_text += f"{bone} -> {mapped}\n"

        msg = QMessageBox(self)
        msg.setWindowTitle("Default Bone Mapping")
        msg.setText("Default Mappings")
        msg.setDetailedText(mapping_text)
        msg.exec()

    def log_error(self, message):
        self.log(f"ERROR: {message}")
