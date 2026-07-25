"""
Folder Search and Replace Tool
Allows searching and replacing in folder names with blacklist support
"""

from PySide6.QtWidgets import (
    QLabel, QLineEdit, QPushButton, QCheckBox, QTextEdit, QMessageBox
)
from PySide6.QtCore import QThread, Signal
from pathlib import Path
from datetime import datetime
from app.tools.base_tool import BaseTool
from app.ui import widgets
import shutil


class FolderSearchReplaceWorker(QThread):
    """Worker thread for folder search and replace operations"""

    progress = Signal(str, str)  # message, level
    progress_value = Signal(int, int)  # folders done, total (drives the progress bar)
    finished = Signal(int, int)  # folders_processed, matches_found

    def __init__(self, directory, search_text, replace_text, blacklist, create_backup, recursive):
        super().__init__()
        self.directory = Path(directory)
        self.search_text = search_text
        self.replace_text = replace_text
        self.blacklist = blacklist
        self.create_backup = create_backup
        self.recursive = recursive
        self.folders_processed = 0
        self.matches_found = 0
        self.rename_history = []  # For undo support (new_path, old_path)

    def run(self):
        """Execute the folder search and replace operation"""
        try:
            if self.create_backup:
                self.progress.emit("Creating backup...", "INFO")
                self._create_backup()

            mode = "recursively" if self.recursive else "non-recursively"
            self.progress.emit(f"Starting search {mode} in: {self.directory}", "INFO")

            # Get all folders in directory
            if self.recursive:
                all_folders = list(self.directory.rglob('*'))
            else:
                all_folders = list(self.directory.glob('*'))
            folder_list = [f for f in all_folders if f.is_dir()]

            # Renames are cheap, so throttle bar updates to ~200 steps or a
            # huge tree would flood the event queue with signal emissions.
            total = len(folder_list)
            emit_step = max(1, total // 200)
            self.progress_value.emit(0, total)

            for idx, folder_path in enumerate(folder_list, start=1):
                if idx % emit_step == 0 or idx == total:
                    self.progress_value.emit(idx, total)

                # Check blacklist
                if self._is_blacklisted(folder_path):
                    continue

                try:
                    # Search and replace in folder name
                    if self.search_text in folder_path.name:
                        old_path = folder_path
                        new_name = folder_path.name.replace(self.search_text, self.replace_text)
                        new_path = folder_path.parent / new_name
                        folder_path.rename(new_path)
                        self.rename_history.append((new_path, old_path))  # Store for undo
                        self.matches_found += 1
                        self.progress.emit(f"Renamed: {old_path.name} → {new_name}", "SUCCESS")

                    self.folders_processed += 1

                except Exception as e:
                    self.progress.emit(f"Error processing {folder_path.name}: {str(e)}", "ERROR")

            self.finished.emit(self.folders_processed, self.matches_found)

        except Exception as e:
            self.progress.emit(f"Operation failed: {str(e)}", "ERROR")
            self.finished.emit(self.folders_processed, self.matches_found)

    def _is_blacklisted(self, folder_path: Path) -> bool:
        """Check if folder matches blacklist patterns"""
        foldername = folder_path.name
        for pattern in self.blacklist:
            pattern = pattern.strip()
            if not pattern:
                continue

            # Handle wildcards
            if '*' in pattern:
                pattern_parts = pattern.split('*')
                if len(pattern_parts) == 2:
                    start, end = pattern_parts
                    if start and end:
                        if foldername.startswith(start) and foldername.endswith(end):
                            return True
                    elif start:
                        if foldername.startswith(start):
                            return True
                    elif end:
                        if foldername.endswith(end):
                            return True
            else:
                # Exact match or contains
                if pattern in foldername:
                    return True
        return False

    def _create_backup(self):
        """Create a backup of the directory"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"{self.directory.name}_backup_{timestamp}"
        backup_path = self.directory.parent / backup_name

        try:
            shutil.copytree(self.directory, backup_path)
            self.progress.emit(f"Backup created: {backup_path}", "SUCCESS")
        except Exception as e:
            self.progress.emit(f"Backup failed: {str(e)}", "ERROR")

    def get_undo_data(self):
        """Get data needed for undo operation"""
        return {
            'rename_history': self.rename_history
        }


class FolderSearchReplaceTool(BaseTool):
    """Folder Search and Replace tool"""

    def __init__(self):
        super().__init__("Folder Search & Replace")
        self.last_undo_data = None
        self.setup_tool_ui()

    def setup_tool_ui(self):
        """Setup the tool-specific UI"""
        self.dir_picker = self.create_picker(
            "dir", "directory",
            placeholder="Select a directory to process...",
            dialog_title="Select Directory")
        self.add_group("Directory", self.dir_picker)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Text to search for...")
        self.replace_input = QLineEdit()
        self.replace_input.setPlaceholderText("Replacement text...")
        self.add_group(
            "Search && Replace",
            widgets.hbox(QLabel("Search for:"), self.search_input),
            widgets.hbox(QLabel("Replace with:"), self.replace_input),
        )

        self.recursive_check = QCheckBox("Recursive (include subfolders)")
        self.recursive_check.setChecked(True)
        self.recursive_check.setToolTip(
            "When on, scan all subfolders of the input. When off, scan only "
            "the input folder itself."
        )

        self.backup_check = QCheckBox("Create backups (.bak)")
        self.backup_check.setChecked(False)
        self.backup_check.setToolTip(
            "Save the original alongside the modified copy."
        )

        self.add_group("Options", self.recursive_check, self.backup_check)

        blacklist_label = QLabel("Patterns to exclude (one per line, supports * wildcard):")
        blacklist_label.setStyleSheet("color: #808080; font-size: 9pt;")

        self.blacklist_input = QTextEdit()
        self.blacklist_input.setPlaceholderText("Examples:\n*temp\nbackup_*\n*_old")
        self.blacklist_input.setMaximumHeight(80)

        self.add_group("Folder Blacklist", blacklist_label, self.blacklist_input)

        self.clear_log_btn = QPushButton("Clear Log")
        self.clear_log_btn.clicked.connect(self.clear_log)

        self.undo_btn = QPushButton("Undo Last")
        self.undo_btn.clicked.connect(self.undo_last_operation)
        self.undo_btn.setEnabled(False)

        self.process_btn = self.make_run_button("Process", self.start_processing)
        self.add_action_row(self.clear_log_btn, self.undo_btn, self.process_btn)
        self.content_layout.addStretch()

    def start_processing(self):
        """Start the folder search and replace process"""
        # Validate inputs
        directory = self.dir_picker.path()
        search_text = self.search_input.text()

        if not directory:
            self.log("Please select a directory", "ERROR")
            return

        if not Path(directory).exists():
            self.log("Directory does not exist", "ERROR")
            return

        if not search_text:
            self.log("Please enter search text", "ERROR")
            return

        # Get blacklist patterns
        blacklist = [line.strip() for line in self.blacklist_input.toPlainText().split('\n')]
        blacklist = [p for p in blacklist if p]  # Remove empty lines

        self.start_worker(
            FolderSearchReplaceWorker(
                directory=directory,
                search_text=search_text,
                replace_text=self.replace_input.text(),
                blacklist=blacklist,
                create_backup=self.backup_check.isChecked(),
                recursive=self.recursive_check.isChecked()
            ),
            self.processing_finished,
            busy_widgets=(self.process_btn,),
            status="Processing...",
        )
        self.log("Processing started...", "INFO")

    def processing_finished(self, folders_processed: int, matches_found: int):
        """Handle processing completion"""
        self.log(f"Processing complete! Folders processed: {folders_processed}, Matches found: {matches_found}", "SUCCESS")

        if matches_found > 0:
            # Store undo data
            self.last_undo_data = self.worker.get_undo_data()
            self.undo_btn.setEnabled(True)
            self.log("Undo data saved. You can undo this operation.", "INFO")

        self.emit_status("Ready")

    def undo_last_operation(self):
        """Undo the last folder search and replace operation"""
        if not self.last_undo_data:
            self.log("No operation to undo", "WARNING")
            return

        reply = QMessageBox.question(
            self,
            "Undo Operation?",
            "This will revert all changes from the last folder search && replace operation.\n\n"
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
                    self.log(f"Restored folder name: {new_path.name} → {old_path.name}", "SUCCESS")
                    success_count += 1
                else:
                    self.log(f"Folder not found: {new_path.name}", "WARNING")
                    error_count += 1
            except Exception as e:
                self.log(f"Error restoring {new_path.name}: {str(e)}", "ERROR")
                error_count += 1

        self.log(
            f"Undo complete! Restored {success_count} changes, {error_count} errors.",
            "SUCCESS" if error_count == 0 else "WARNING"
        )

        self.last_undo_data = None
        self.undo_btn.setEnabled(False)
