"""
Filename Sanitizer Tool
Recursively scans and sanitizes filenames for Source engine compatibility
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QCheckBox, QTextEdit, QFileDialog, QGroupBox,
    QSpinBox, QMessageBox
)
from PySide6.QtCore import Qt, QThread, Signal
from pathlib import Path
import re
import shutil
from datetime import datetime
from typing import Dict, List, Tuple, Optional
from app.tools.base_tool import BaseTool


class SanitizerWorker(QThread):
    """Worker thread for filename sanitization operations"""
    
    progress = Signal(str, str)  # message, level
    finished = Signal(int, int)  # files_processed, files_renamed
    
    def __init__(self, directory, options, dry_run=False):
        super().__init__()
        self.directory = Path(directory)
        self.options = options
        self.dry_run = dry_run
        self.files_processed = 0
        self.files_renamed = 0
        self.rename_history = []  # For undo support
        self.reference_updates = []  # Track content updates
        
    def run(self):
        """Execute the sanitization operation"""
        try:
            mode = "recursively" if self.options['recursive'] else "non-recursively"
            self.progress.emit(f"Scanning directory {mode}: {self.directory}", "INFO")
            
            # Get all files
            if self.options['recursive']:
                all_files = list(self.directory.rglob('*'))
            else:
                all_files = list(self.directory.glob('*'))
            file_list = [f for f in all_files if f.is_file()]
            
            self.progress.emit(f"Found {len(file_list)} files to process", "INFO")
            
            # First pass: sanitize filenames
            rename_map = {}  # old_path -> new_path
            
            for file_path in file_list:
                self.files_processed += 1
                
                try:
                    new_name = self._sanitize_filename(file_path.name)
                    
                    if new_name != file_path.name:
                        new_path = file_path.parent / new_name
                        
                        # Handle name collisions
                        new_path = self._resolve_collision(new_path, file_path)
                        new_name = new_path.name
                        
                        if self.dry_run:
                            self.progress.emit(
                                f"[DRY RUN] Would rename: {file_path.name} → {new_name}",
                                "INFO"
                            )
                        else:
                            # Perform rename
                            file_path.rename(new_path)
                            self.rename_history.append((new_path, file_path))  # Store for undo
                            rename_map[str(file_path)] = str(new_path)
                            
                            self.progress.emit(
                                f"Renamed: {file_path.name} → {new_name}",
                                "SUCCESS"
                            )
                        
                        self.files_renamed += 1
                        
                except Exception as e:
                    self.progress.emit(f"Error processing {file_path.name}: {str(e)}", "ERROR")
            
            # Second pass: update file references if enabled
            if self.options['update_references'] and not self.dry_run and rename_map:
                self.progress.emit("Updating file references...", "INFO")
                self._update_file_references(rename_map)
            
            if self.dry_run:
                self.progress.emit(
                    f"Dry run complete. Would rename {self.files_renamed} files.",
                    "INFO"
                )
            else:
                self.progress.emit(
                    f"Sanitization complete! Renamed {self.files_renamed} of {self.files_processed} files.",
                    "SUCCESS"
                )
            
            self.finished.emit(self.files_processed, self.files_renamed)
            
        except Exception as e:
            self.progress.emit(f"Operation failed: {str(e)}", "ERROR")
            self.finished.emit(self.files_processed, self.files_renamed)
    
    def _sanitize_filename(self, filename: str) -> str:
        """Sanitize a single filename"""
        name, ext = filename.rsplit('.', 1) if '.' in filename else (filename, '')
        
        # Remove hex codes between underscores or at end (e.g., _e044ecec, _dea6845)
        # Matches 6-8 character hex codes after an underscore
        name = re.sub(r'_[0-9a-fA-F]{6,8}(?=_|$)', '', name)
        
        # Remove trailing long numeric sequences (e.g., _980868409) but keep short variant numbers (_1, _2, _03, etc.)
        # Only matches underscore followed by 5+ digits at the end
        name = re.sub(r'_\d{5,}$', '', name)
        
        # Apply lowercase if enabled
        if self.options['lowercase']:
            name = name.lower()
            ext = ext.lower()
        
        # Trim/collapse whitespace if enabled
        if self.options['trim_whitespace']:
            name = ' '.join(name.split())  # Collapse multiple spaces
            name = name.strip()
        
        # Replace disallowed characters
        allowed_chars = self.options['allowed_chars']
        replacement = self.options['replacement_char']
        
        sanitized = ''
        for char in name:
            if char.isalnum() or char in allowed_chars:
                sanitized += char
            else:
                sanitized += replacement
        
        # Collapse consecutive replacement characters
        if replacement:
            pattern = re.escape(replacement) + '+'
            sanitized = re.sub(pattern, replacement, sanitized)
        
        # Remove leading/trailing replacement characters
        sanitized = sanitized.strip(replacement)
        
        # Apply max length if set
        max_length = self.options['max_length']
        if max_length > 0 and len(sanitized) > max_length:
            sanitized = sanitized[:max_length]
        
        # Reconstruct filename
        result = f"{sanitized}.{ext}" if ext else sanitized
        
        # Ensure we didn't create an empty name
        if not sanitized or sanitized == replacement * len(sanitized):
            result = f"unnamed_{datetime.now().strftime('%Y%m%d%H%M%S')}"
            if ext:
                result += f".{ext}"
        
        return result
    
    def _resolve_collision(self, new_path: Path, original_path: Path) -> Path:
        """Resolve filename collisions by appending a number"""
        if not new_path.exists() or new_path == original_path:
            return new_path
        
        name_stem = new_path.stem
        suffix = new_path.suffix
        parent = new_path.parent
        counter = 1
        
        while True:
            test_path = parent / f"{name_stem}_{counter}{suffix}"
            if not test_path.exists():
                self.progress.emit(
                    f"Collision detected, using: {test_path.name}",
                    "WARNING"
                )
                return test_path
            counter += 1
            
            if counter > 1000:  # Safety limit
                raise Exception("Too many collision attempts")
    
    def _update_file_references(self, rename_map: Dict[str, str]):
        """Update file references in text files"""
        text_extensions = {'.txt', '.vmt', '.qc', '.cfg', '.ini', '.json', '.xml', '.html', '.css', '.js'}
        
        # Get all text files that might contain references
        all_files = list(self.directory.rglob('*'))
        text_files = [f for f in all_files if f.is_file() and f.suffix.lower() in text_extensions]
        
        updates_made = 0
        
        for file_path in text_files:
            try:
                content = file_path.read_text(encoding='utf-8')
                original_content = content
                
                # Replace old filenames with new ones
                for old_path, new_path in rename_map.items():
                    old_name = Path(old_path).name
                    new_name = Path(new_path).name
                    
                    if old_name in content:
                        content = content.replace(old_name, new_name)
                
                # Write back if changed
                if content != original_content:
                    file_path.write_text(content, encoding='utf-8')
                    self.reference_updates.append((file_path, original_content))
                    updates_made += 1
                    self.progress.emit(f"Updated references in: {file_path.name}", "SUCCESS")
                    
            except (UnicodeDecodeError, PermissionError):
                # Skip binary files or files we can't access
                pass
        
        if updates_made > 0:
            self.progress.emit(f"Updated references in {updates_made} files", "INFO")
    
    def get_undo_data(self):
        """Get data needed for undo operation"""
        return {
            'rename_history': self.rename_history,
            'reference_updates': self.reference_updates
        }


class FilenameSanitizerTool(BaseTool):
    """Filename sanitizer tool for Source engine compatibility"""
    
    def __init__(self):
        super().__init__("Filename Sanitizer")
        self.worker = None
        self.last_undo_data = None
        self.setup_tool_ui()
        
    def setup_tool_ui(self):
        """Setup the tool-specific UI"""
        # Directory selection
        dir_group = QGroupBox("Directory")
        dir_layout = QHBoxLayout()
        
        self.dir_input = QLineEdit()
        self.dir_input.setPlaceholderText("Select a directory to process...")
        dir_layout.addWidget(self.dir_input)
        
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self.browse_directory)
        dir_layout.addWidget(browse_btn)
        
        dir_group.setLayout(dir_layout)
        self.content_layout.addWidget(dir_group)
        
        # Character options
        char_group = QGroupBox("Character Options")
        char_layout = QVBoxLayout()
        
        # Allowed characters
        allowed_row = QHBoxLayout()
        allowed_row.addWidget(QLabel("Allowed characters:"))
        self.allowed_input = QLineEdit("_-. ")
        self.allowed_input.setPlaceholderText("Characters to keep (e.g., _-. )")
        self.allowed_input.setToolTip("Characters that are allowed in addition to alphanumeric")
        allowed_row.addWidget(self.allowed_input)
        char_layout.addLayout(allowed_row)
        
        # Replacement character
        replace_row = QHBoxLayout()
        replace_row.addWidget(QLabel("Replacement char:"))
        self.replacement_input = QLineEdit("_")
        self.replacement_input.setMaximumWidth(50)
        self.replacement_input.setToolTip("Character to replace invalid characters with")
        replace_row.addWidget(self.replacement_input)
        replace_row.addStretch()
        char_layout.addLayout(replace_row)
        
        char_group.setLayout(char_layout)
        self.content_layout.addWidget(char_group)
        
        # Processing options
        options_group = QGroupBox("Processing Options")
        options_layout = QVBoxLayout()
        
        self.lowercase_check = QCheckBox("Enforce lowercase filenames")
        self.lowercase_check.setChecked(True)
        options_layout.addWidget(self.lowercase_check)
        
        self.whitespace_check = QCheckBox("Trim && collapse whitespace")
        self.whitespace_check.setChecked(True)
        options_layout.addWidget(self.whitespace_check)
        
        self.recursive_check = QCheckBox("Process subdirectories recursively")
        self.recursive_check.setChecked(True)
        self.recursive_check.setToolTip("Include all files in subdirectories")
        options_layout.addWidget(self.recursive_check)
        
        self.update_refs_check = QCheckBox("Update file references in text files (SLOW)")
        self.update_refs_check.setChecked(False)
        self.update_refs_check.setToolTip("Scan and update references to renamed files in .vmt, .qc, .txt, etc.")
        options_layout.addWidget(self.update_refs_check)
        
        # Max length
        length_row = QHBoxLayout()
        length_row.addWidget(QLabel("Max filename length:"))
        self.max_length_spin = QSpinBox()
        self.max_length_spin.setMinimum(0)
        self.max_length_spin.setMaximum(255)
        self.max_length_spin.setValue(0)
        self.max_length_spin.setSpecialValueText("No limit")
        self.max_length_spin.setToolTip("Maximum length for filenames (0 = no limit)")
        length_row.addWidget(self.max_length_spin)
        length_row.addStretch()
        options_layout.addLayout(length_row)
        
        options_group.setLayout(options_layout)
        self.content_layout.addWidget(options_group)
        
        # Pattern info
        info_group = QGroupBox("Hex Code Pattern")
        info_layout = QVBoxLayout()
        info_label = QLabel(
            "Removes patterns automatically:\n"
            "• Hex codes (6-8 chars): building_1_hs_color_psd_e044ecec.png → building_1_hs_color_psd.png\n"
            "• Trailing numbers: a2_sewer_xen001_normal_orm_980868409.tga → a2_sewer_xen001_normal_orm.tga\n"
            "• brick_corner_damage_interior_debris_color_png_dea6845.png → brick_corner_damage_interior_debris_color_png.png"
        )
        info_label.setStyleSheet("color: #808080; font-size: 9pt;")
        info_layout.addWidget(info_label)
        info_group.setLayout(info_layout)
        self.content_layout.addWidget(info_group)
        
        # Action buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        self.clear_log_btn = QPushButton("Clear Log")
        self.clear_log_btn.clicked.connect(self.clear_log)
        button_layout.addWidget(self.clear_log_btn)
        
        self.undo_btn = QPushButton("Undo Last")
        self.undo_btn.clicked.connect(self.undo_last_operation)
        self.undo_btn.setEnabled(False)
        button_layout.addWidget(self.undo_btn)
        
        self.dry_run_btn = QPushButton("Dry Run")
        self.dry_run_btn.setMinimumWidth(100)
        self.dry_run_btn.clicked.connect(lambda: self.start_processing(dry_run=True))
        self.dry_run_btn.setToolTip("Preview changes without actually renaming files")
        button_layout.addWidget(self.dry_run_btn)
        
        self.process_btn = QPushButton("Process")
        self.process_btn.setMinimumWidth(100)
        self.process_btn.clicked.connect(lambda: self.start_processing(dry_run=False))
        button_layout.addWidget(self.process_btn)
        
        self.content_layout.addLayout(button_layout)
        self.content_layout.addStretch()
        
    def browse_directory(self):
        """Open directory browser"""
        directory = QFileDialog.getExistingDirectory(
            self,
            "Select Directory",
            self.dir_input.text() or ""
        )
        if directory:
            self.dir_input.setText(directory)
            self.log(f"Selected directory: {directory}", "INFO")
    
    def start_processing(self, dry_run=False):
        """Start the sanitization process"""
        # Validate inputs
        directory = self.dir_input.text()
        
        if not directory:
            self.log("Please select a directory", "ERROR")
            return
        
        if not Path(directory).exists():
            self.log("Directory does not exist", "ERROR")
            return
        
        # Warn about reference updates
        if self.update_refs_check.isChecked() and not dry_run:
            reply = QMessageBox.question(
                self,
                "Update References?",
                "Updating file references will scan and modify text files.\n"
                "This operation can take a long time on large directories.\n\n"
                "Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No
            )
            if reply == QMessageBox.No:
                return
        
        # Get options
        options = {
            'allowed_chars': self.allowed_input.text(),
            'replacement_char': self.replacement_input.text()[:1] if self.replacement_input.text() else '_',
            'lowercase': self.lowercase_check.isChecked(),
            'trim_whitespace': self.whitespace_check.isChecked(),
            'max_length': self.max_length_spin.value(),
            'recursive': self.recursive_check.isChecked(),
            'update_references': self.update_refs_check.isChecked()
        }
        
        # Disable buttons during processing
        self.process_btn.setEnabled(False)
        self.dry_run_btn.setEnabled(False)
        self.clear_log()
        
        # Create and start worker
        self.worker = SanitizerWorker(
            directory=directory,
            options=options,
            dry_run=dry_run
        )
        
        self.worker.progress.connect(self.log)
        self.worker.finished.connect(lambda p, r: self.processing_finished(p, r, dry_run))
        self.worker.start()
        
        mode = "Dry run" if dry_run else "Processing"
        self.log(f"{mode} started...", "INFO")
        self.emit_status(f"{mode}...")
    
    def processing_finished(self, files_processed: int, files_renamed: int, was_dry_run: bool):
        """Handle processing completion"""
        self.process_btn.setEnabled(True)
        self.dry_run_btn.setEnabled(True)
        
        if not was_dry_run and files_renamed > 0:
            # Store undo data
            self.last_undo_data = self.worker.get_undo_data()
            self.undo_btn.setEnabled(True)
            self.log("Undo data saved. You can undo this operation.", "INFO")
        
        self.emit_status("Ready")
    
    def undo_last_operation(self):
        """Undo the last sanitization operation"""
        if not self.last_undo_data:
            self.log("No operation to undo", "WARNING")
            return
        
        reply = QMessageBox.question(
            self,
            "Undo Operation?",
            "This will revert all filename changes from the last operation.\n"
            "File reference updates cannot be undone automatically.\n\n"
            "Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.No:
            return
        
        self.clear_log()
        self.log("Starting undo operation...", "INFO")
        
        success_count = 0
        error_count = 0
        
        # Reverse the rename history
        for new_path, old_path in reversed(self.last_undo_data['rename_history']):
            try:
                if new_path.exists():
                    new_path.rename(old_path)
                    self.log(f"Restored: {new_path.name} → {old_path.name}", "SUCCESS")
                    success_count += 1
                else:
                    self.log(f"File not found: {new_path.name}", "WARNING")
                    error_count += 1
            except Exception as e:
                self.log(f"Error restoring {new_path.name}: {str(e)}", "ERROR")
                error_count += 1
        
        self.log(
            f"Undo complete! Restored {success_count} files, {error_count} errors.",
            "SUCCESS" if error_count == 0 else "WARNING"
        )
        
        if self.last_undo_data['reference_updates']:
            self.log(
                "Note: File content changes were not reverted automatically. "
                "Please restore from backup if needed.",
                "WARNING"
            )
        
        self.last_undo_data = None
        self.undo_btn.setEnabled(False)
