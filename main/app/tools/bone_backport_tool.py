import os
import shutil
from PySide6.QtWidgets import (
    QLabel, QPushButton, QCheckBox, QTextEdit, QMessageBox
)
from PySide6.QtCore import QThread, Signal
from app.tools.base_tool import BaseTool
from app.ui import widgets
from app.core.bone_maps import SOURCE2_TO_VALVEBIPED

# Bone mapping from Source 2 to ValveBiped (Source 1). Canonical definition
# lives in core/bone_maps.py so the ragdoll pipeline can share it without
# importing this Qt widget module.
BONE_MAPPING = SOURCE2_TO_VALVEBIPED

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
        self.folder_picker = self.create_picker(
            "dir", "folder", label="QC/SMD Folder:",
            placeholder="Select folder containing QC and SMD files...",
            dialog_title="Select Folder")
        self.add_group("Input", self.folder_picker)

        self.backup_check = QCheckBox("Create backups (.bak)")
        self.backup_check.setChecked(True)
        self.backup_check.setToolTip(
            "Save the original alongside the modified copy."
        )

        self.process_qc_check = QCheckBox("Process QC files")
        self.process_qc_check.setChecked(True)

        self.process_qci_check = QCheckBox("Process QCIs")
        self.process_qci_check.setChecked(True)

        self.process_smd_check = QCheckBox("Process SMD files")
        self.process_smd_check.setChecked(True)

        self.add_group("Options", self.backup_check, self.process_qc_check,
                       self.process_qci_check, self.process_smd_check)

        self.mapping_text = QTextEdit()
        self.mapping_text.setPlaceholderText(
            "# Example custom mappings (one per line):\n"
            "# custom_bone_L = ValveBiped.Bip01_L_CustomBone\n"
            "# weapon_bone = ValveBiped.weapon_bone"
        )
        self.mapping_text.setMinimumHeight(100)
        self.add_group("Custom Bone Mapping",
                       QLabel("Add custom bone mappings (Source2Name = Source1Name):"),
                       self.mapping_text)

        # Action buttons: mapping viewer on the left, Process on the right.
        self.default_mapping_btn = QPushButton("View Default Mapping")
        self.default_mapping_btn.clicked.connect(self.show_default_mapping)
        self.process_btn = self.make_run_button("Process", self.process_files)
        btn_row = widgets.hbox(self.default_mapping_btn, stretch_after=True)
        btn_row.addWidget(self.process_btn)
        self.content_layout.addLayout(btn_row)

        # Add stretch to push everything up
        self.content_layout.addStretch()

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
        folder_path = self.folder_picker.path()
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

        # The worker's signals are non-standard, so wire progress/errors
        # manually before start_worker (which only auto-connects `progress`).
        self.processing_thread = ProcessingThread(folder_path, bone_mapping, options)
        self.processing_thread.progress_signal.connect(self.log)
        self.processing_thread.error_signal.connect(self.log_error)
        self.start_worker(
            self.processing_thread,
            self.on_processing_finished,
            finished_signal=self.processing_thread.finished_signal,
            busy_widgets=(self.process_btn,),
            status="Backporting bones...",
        )
        self.log("Starting processing...")

    def on_processing_finished(self, processed, changes, errors):
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
