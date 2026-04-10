"""
Folder Search and Replace Tool
Allows searching and replacing in folder names with blacklist support
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QCheckBox, QTextEdit, QFileDialog, QGroupBox
)
from PySide6.QtCore import Qt, QThread, Signal
from pathlib import Path
from datetime import datetime
from app.tools.base_tool import BaseTool
import shutil


class FolderSearchReplaceWorker(QThread):
    """Worker thread for folder search and replace operations"""

    progress = Signal(str, str)  # message, level
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

            for folder_path in folder_list:
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

        # Search and Replace inputs
        search_group = QGroupBox("Search && Replace")
        search_layout = QVBoxLayout()

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Search for:"))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Text to search for...")
        search_row.addWidget(self.search_input)
        search_layout.addLayout(search_row)

        replace_row = QHBoxLayout()
        replace_row.addWidget(QLabel("Replace with:"))
        self.replace_input = QLineEdit()
        self.replace_input.setPlaceholderText("Replacement text...")
        replace_row.addWidget(self.replace_input)
        search_layout.addLayout(replace_row)

        search_group.setLayout(search_layout)
        self.content_layout.addWidget(search_group)

        # Options
        options_group = QGroupBox("Options")
        options_layout = QVBoxLayout()

        self.recursive_check = QCheckBox("Process subdirectories recursively")
        self.recursive_check.setChecked(True)
        self.recursive_check.setToolTip("Include all folders in subdirectories")
        options_layout.addWidget(self.recursive_check)

        self.backup_check = QCheckBox("Create backup before processing")
        self.backup_check.setChecked(False)
        options_layout.addWidget(self.backup_check)

        options_group.setLayout(options_layout)
        self.content_layout.addWidget(options_group)

        # Blacklist
        blacklist_group = QGroupBox("Folder Blacklist")
        blacklist_layout = QVBoxLayout()

        blacklist_label = QLabel("Patterns to exclude (one per line, supports * wildcard):")
        blacklist_label.setStyleSheet("color: #808080; font-size: 9pt;")
        blacklist_layout.addWidget(blacklist_label)

        self.blacklist_input = QTextEdit()
        self.blacklist_input.setPlaceholderText("Examples:\n*temp\nbackup_*\n*_old")
        self.blacklist_input.setMaximumHeight(80)
        blacklist_layout.addWidget(self.blacklist_input)

        blacklist_group.setLayout(blacklist_layout)
        self.content_layout.addWidget(blacklist_group)

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

        self.process_btn = QPushButton("Process")
        self.process_btn.setMinimumWidth(120)
        self.process_btn.clicked.connect(self.start_processing)
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

    def start_processing(self):
        """Start the folder search and replace process"""
        # Validate inputs
        directory = self.dir_input.text()
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

        # Disable button during processing
        self.process_btn.setEnabled(False)
        self.clear_log()

        # Create and start worker
        self.worker = FolderSearchReplaceWorker(
            directory=directory,
            search_text=search_text,
            replace_text=self.replace_input.text(),
            blacklist=blacklist,
            create_backup=self.backup_check.isChecked(),
            recursive=self.recursive_check.isChecked()
        )

        self.worker.progress.connect(self.log)
        self.worker.finished.connect(self.processing_finished)
        self.worker.start()

        self.log("Processing started...", "INFO")
        self.emit_status("Processing...")

    def processing_finished(self, folders_processed: int, matches_found: int):
        """Handle processing completion"""
        self.process_btn.setEnabled(True)
        self.log(f"Processing complete! Folders processed: {folders_processed}, Matches found: {matches_found}", "SUCCESS")

        if matches_found > 0:
            # Store undo data
            self.last_undo_data = self.worker.get_undo_data()
            self.undo_btn.setEnabled(True)
            self.log("Undo data saved. You can undo this operation.", "INFO")

        self.emit_status("Ready")

    def undo_last_operation(self):
        """Undo the last folder search and replace operation"""
        from PySide6.QtWidgets import QMessageBox

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
