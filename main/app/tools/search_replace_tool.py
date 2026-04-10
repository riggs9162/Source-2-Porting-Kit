"""
Search and Replace Tool
Allows searching and replacing in filenames or file contents with blacklist support
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QCheckBox, QTextEdit, QFileDialog, QGroupBox,
    QListWidget, QListWidgetItem
)
from PySide6.QtCore import Qt, QThread, Signal
from pathlib import Path
import shutil
from datetime import datetime
from app.tools.base_tool import BaseTool


class SearchReplaceWorker(QThread):
    """Worker thread for search and replace operations"""
    
    progress = Signal(str, str)  # message, level
    finished = Signal(int, int)  # files_processed, matches_found
    
    def __init__(self, directory, search_text, replace_text, 
                 search_filenames, search_contents, blacklist, create_backup, recursive):
        super().__init__()
        self.directory = Path(directory)
        self.search_text = search_text
        self.replace_text = replace_text
        self.search_filenames = search_filenames
        self.search_contents = search_contents
        self.blacklist = blacklist
        self.create_backup = create_backup
        self.recursive = recursive
        self.files_processed = 0
        self.matches_found = 0
        self.rename_history = []  # For undo support (new_path, old_path)
        self.content_history = []  # For undo support (file_path, original_content)
        
    def run(self):
        """Execute the search and replace operation"""
        try:
            if self.create_backup:
                self.progress.emit("Creating backup...", "INFO")
                self._create_backup()
                
            mode = "recursively" if self.recursive else "non-recursively"
            self.progress.emit(f"Starting search {mode} in: {self.directory}", "INFO")
            
            # Get all files in directory
            if self.recursive:
                all_files = list(self.directory.rglob('*'))
            else:
                all_files = list(self.directory.glob('*'))
            file_list = [f for f in all_files if f.is_file()]
            
            for file_path in file_list:
                # Check blacklist
                if self._is_blacklisted(file_path):
                    continue
                    
                try:
                    # Search and replace in filename
                    if self.search_filenames and self.search_text in file_path.name:
                        old_path = file_path
                        new_name = file_path.name.replace(self.search_text, self.replace_text)
                        new_path = file_path.parent / new_name
                        file_path.rename(new_path)
                        self.rename_history.append((new_path, old_path))  # Store for undo
                        self.matches_found += 1
                        self.progress.emit(f"Renamed: {old_path.name} → {new_name}", "SUCCESS")
                        file_path = new_path  # Update reference for content search
                    
                    # Search and replace in file contents
                    if self.search_contents:
                        self._replace_in_content(file_path)
                    
                    self.files_processed += 1
                    
                except Exception as e:
                    self.progress.emit(f"Error processing {file_path.name}: {str(e)}", "ERROR")
            
            self.finished.emit(self.files_processed, self.matches_found)
            
        except Exception as e:
            self.progress.emit(f"Operation failed: {str(e)}", "ERROR")
            self.finished.emit(self.files_processed, self.matches_found)
    
    def _is_blacklisted(self, file_path: Path) -> bool:
        """Check if file matches blacklist patterns"""
        filename = file_path.name
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
                        if filename.startswith(start) and filename.endswith(end):
                            return True
                    elif start:
                        if filename.startswith(start):
                            return True
                    elif end:
                        if filename.endswith(end):
                            return True
            else:
                # Exact match or contains
                if pattern in filename:
                    return True
        return False
    
    def _replace_in_content(self, file_path: Path):
        """Replace text in file contents"""
        try:
            # Try to read as text
            content = file_path.read_text(encoding='utf-8')
            if self.search_text in content:
                # Store original content for undo
                self.content_history.append((file_path, content))
                
                new_content = content.replace(self.search_text, self.replace_text)
                file_path.write_text(new_content, encoding='utf-8')
                self.matches_found += 1
                self.progress.emit(f"Updated content: {file_path.name}", "SUCCESS")
        except (UnicodeDecodeError, PermissionError):
            # Skip binary files or files we can't access
            pass
    
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
            'rename_history': self.rename_history,
            'content_history': self.content_history
        }


class SearchReplaceTool(BaseTool):
    """Search and Replace tool for files and contents"""
    
    def __init__(self):
        super().__init__("Search & Replace")
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
        
        self.filename_check = QCheckBox("Search && replace in filenames")
        self.filename_check.setChecked(True)
        options_layout.addWidget(self.filename_check)
        
        self.content_check = QCheckBox("Search && replace in file contents")
        self.content_check.setChecked(False)
        options_layout.addWidget(self.content_check)
        
        self.recursive_check = QCheckBox("Process subdirectories recursively")
        self.recursive_check.setChecked(True)
        self.recursive_check.setToolTip("Include all files in subdirectories")
        options_layout.addWidget(self.recursive_check)
        
        self.backup_check = QCheckBox("Create backup before processing")
        self.backup_check.setChecked(False)
        options_layout.addWidget(self.backup_check)
        
        options_group.setLayout(options_layout)
        self.content_layout.addWidget(options_group)
        
        # Blacklist
        blacklist_group = QGroupBox("File Blacklist")
        blacklist_layout = QVBoxLayout()
        
        blacklist_label = QLabel("Patterns to exclude (one per line, supports * wildcard):")
        blacklist_label.setStyleSheet("color: #808080; font-size: 9pt;")
        blacklist_layout.addWidget(blacklist_label)
        
        self.blacklist_input = QTextEdit()
        self.blacklist_input.setPlaceholderText("Examples:\n*.txt\nsoundscape_*\n*_temp.vmt")
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
        """Start the search and replace process"""
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
        
        if not self.filename_check.isChecked() and not self.content_check.isChecked():
            self.log("Please select at least one search option", "ERROR")
            return
        
        # Get blacklist patterns
        blacklist = [line.strip() for line in self.blacklist_input.toPlainText().split('\n')]
        blacklist = [p for p in blacklist if p]  # Remove empty lines
        
        # Disable button during processing
        self.process_btn.setEnabled(False)
        self.clear_log()
        
        # Create and start worker
        self.worker = SearchReplaceWorker(
            directory=directory,
            search_text=search_text,
            replace_text=self.replace_input.text(),
            search_filenames=self.filename_check.isChecked(),
            search_contents=self.content_check.isChecked(),
            blacklist=blacklist,
            create_backup=self.backup_check.isChecked(),
            recursive=self.recursive_check.isChecked()
        )
        
        self.worker.progress.connect(self.log)
        self.worker.finished.connect(self.processing_finished)
        self.worker.start()
        
        self.log("Processing started...", "INFO")
        self.emit_status("Processing...")
    
    def processing_finished(self, files_processed: int, matches_found: int):
        """Handle processing completion"""
        self.process_btn.setEnabled(True)
        self.log(f"Processing complete! Files processed: {files_processed}, Matches found: {matches_found}", "SUCCESS")
        
        if matches_found > 0:
            # Store undo data
            self.last_undo_data = self.worker.get_undo_data()
            self.undo_btn.setEnabled(True)
            self.log("Undo data saved. You can undo this operation.", "INFO")
        
        self.emit_status("Ready")
    
    def undo_last_operation(self):
        """Undo the last search and replace operation"""
        from PySide6.QtWidgets import QMessageBox
        
        if not self.last_undo_data:
            self.log("No operation to undo", "WARNING")
            return
        
        reply = QMessageBox.question(
            self,
            "Undo Operation?",
            "This will revert all changes from the last search && replace operation.\n\n"
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
        
        # Restore file contents first
        for file_path, original_content in self.last_undo_data['content_history']:
            try:
                if file_path.exists():
                    file_path.write_text(original_content, encoding='utf-8')
                    self.log(f"Restored content: {file_path.name}", "SUCCESS")
                    success_count += 1
                else:
                    self.log(f"File not found: {file_path.name}", "WARNING")
                    error_count += 1
            except Exception as e:
                self.log(f"Error restoring content of {file_path.name}: {str(e)}", "ERROR")
                error_count += 1
        
        # Reverse the rename history
        for new_path, old_path in reversed(self.last_undo_data['rename_history']):
            try:
                if new_path.exists():
                    new_path.rename(old_path)
                    self.log(f"Restored filename: {new_path.name} → {old_path.name}", "SUCCESS")
                    success_count += 1
                else:
                    self.log(f"File not found: {new_path.name}", "WARNING")
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
