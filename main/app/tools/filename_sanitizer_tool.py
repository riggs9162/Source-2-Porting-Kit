"""
Filename Sanitizer Tool
Recursively scans and sanitizes filenames for Source engine compatibility
"""

from PySide6.QtWidgets import (
    QLabel, QLineEdit, QPushButton, QCheckBox, QSpinBox, QMessageBox
)
from PySide6.QtCore import QThread, Signal
from pathlib import Path
import re
from datetime import datetime
from typing import Dict
from app.tools.base_tool import BaseTool
from app.ui import widgets


class SanitizerWorker(QThread):
    """Worker thread for filename sanitization operations"""

    progress = Signal(str, str)  # message, level
    progress_value = Signal(int, int)  # files done, total (drives the progress bar)
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

            # Renames are cheap, so throttle bar updates to ~200 steps or a
            # huge tree would flood the event queue with signal emissions.
            total = len(file_list)
            emit_step = max(1, total // 200)
            self.progress_value.emit(0, total)

            for file_path in file_list:
                self.files_processed += 1
                if self.files_processed % emit_step == 0 or self.files_processed == total:
                    self.progress_value.emit(self.files_processed, total)

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
        self.last_undo_data = None
        self.setup_tool_ui()

    def setup_tool_ui(self):
        """Setup the tool-specific UI"""
        self.dir_picker = self.create_picker(
            "dir", "directory",
            placeholder="Select a directory to process...",
            dialog_title="Select Directory")
        self.add_group("Directory", self.dir_picker)

        self.allowed_input = QLineEdit("_-. ")
        self.allowed_input.setPlaceholderText("Characters to keep (e.g., _-. )")
        self.allowed_input.setToolTip("Characters that are allowed in addition to alphanumeric")

        self.replacement_input = QLineEdit("_")
        self.replacement_input.setMaximumWidth(50)
        self.replacement_input.setToolTip("Character to replace invalid characters with")

        self.add_group(
            "Character Options",
            widgets.hbox(QLabel("Allowed characters:"), self.allowed_input),
            widgets.hbox(QLabel("Replacement char:"), self.replacement_input,
                         stretch_after=True),
        )

        self.lowercase_check = QCheckBox("Enforce lowercase filenames")
        self.lowercase_check.setChecked(True)

        self.whitespace_check = QCheckBox("Trim && collapse whitespace")
        self.whitespace_check.setChecked(True)

        self.recursive_check = QCheckBox("Recursive (include subfolders)")
        self.recursive_check.setChecked(True)
        self.recursive_check.setToolTip(
            "When on, scan all subfolders of the input. When off, scan only "
            "the input folder itself."
        )

        self.update_refs_check = QCheckBox("Update file references in text files (SLOW)")
        self.update_refs_check.setChecked(False)
        self.update_refs_check.setToolTip("Scan and update references to renamed files in .vmt, .qc, .txt, etc.")

        self.max_length_spin = QSpinBox()
        self.max_length_spin.setMinimum(0)
        self.max_length_spin.setMaximum(255)
        self.max_length_spin.setValue(0)
        self.max_length_spin.setSpecialValueText("No limit")
        self.max_length_spin.setToolTip("Maximum length for filenames (0 = no limit)")

        self.add_group(
            "Processing Options",
            self.lowercase_check, self.whitespace_check,
            self.recursive_check, self.update_refs_check,
            widgets.hbox(QLabel("Max filename length:"), self.max_length_spin,
                         stretch_after=True),
        )

        info_label = QLabel(
            "Removes patterns automatically:\n"
            "• Hex codes (6-8 chars): building_1_hs_color_psd_e044ecec.png → building_1_hs_color_psd.png\n"
            "• Trailing numbers: a2_sewer_xen001_normal_orm_980868409.tga → a2_sewer_xen001_normal_orm.tga\n"
            "• brick_corner_damage_interior_debris_color_png_dea6845.png → brick_corner_damage_interior_debris_color_png.png"
        )
        info_label.setStyleSheet("color: #808080; font-size: 9pt;")
        self.add_group("Hex Code Pattern", info_label)

        self.clear_log_btn = QPushButton("Clear Log")
        self.clear_log_btn.clicked.connect(self.clear_log)

        self.undo_btn = QPushButton("Undo Last")
        self.undo_btn.clicked.connect(self.undo_last_operation)
        self.undo_btn.setEnabled(False)

        self.dry_run_btn = self.make_run_button(
            "Dry run (no writes)", lambda: self.start_processing(dry_run=True),
            min_width=140)
        self.dry_run_btn.setToolTip(
            "Do everything except writing files — useful for previewing the work."
        )

        self.process_btn = self.make_run_button(
            "Process", lambda: self.start_processing(dry_run=False), min_width=100)

        self.add_action_row(self.clear_log_btn, self.undo_btn,
                            self.dry_run_btn, self.process_btn)
        self.content_layout.addStretch()

    def start_processing(self, dry_run: bool = False):
        """Start the sanitization process"""
        # Validate inputs
        directory = self.dir_picker.path()

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

        mode = "Dry run" if dry_run else "Processing"
        self.start_worker(
            SanitizerWorker(
                directory=directory,
                options=options,
                dry_run=dry_run
            ),
            lambda p, r: self.processing_finished(p, r, dry_run),
            busy_widgets=(self.process_btn, self.dry_run_btn),
            status=f"{mode}...",
        )
        self.log(f"{mode} started...", "INFO")

    def processing_finished(self, files_processed: int, files_renamed: int, was_dry_run: bool):
        """Handle processing completion"""
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
