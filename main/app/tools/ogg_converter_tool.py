"""
OGG Converter Tool
Converts audio files to OGG format, skipping WAV files with loop points.
"""

import struct
import wave
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QVBoxLayout,
    QHBoxLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QCheckBox,
    QFileDialog,
    QSpinBox,
)

from app.tools.base_tool import BaseTool


class OggConversionWorker(QThread):
    """Background worker to convert audio files to OGG."""

    progress = Signal(str, str)  # message, level
    finished = Signal(int, int, int, int)  # total, converted, skipped, failed

    def __init__(
        self,
        files: List[Path],
        output_folder: Optional[Path],
        quality: int,
        overwrite_existing: bool,
        delete_original: bool,
    ):
        super().__init__()
        self.files = files
        self.output_folder = output_folder
        self.quality = quality
        self.overwrite_existing = overwrite_existing
        self.delete_original = delete_original

    def run(self):
        converted = 0
        skipped = 0
        failed = 0
        total = len(self.files)

        for src in self.files:
            if self.isInterruptionRequested():
                break
            
            try:
                # Check if WAV file has loop points
                if src.suffix.lower() == ".wav":
                    if self._has_loop_points(src):
                        self.progress.emit(
                            f"Skipped {src.name} (has loop points)",
                            "WARNING"
                        )
                        skipped += 1
                        continue

                # Determine output path
                if self.output_folder:
                    dest = self.output_folder / f"{src.stem}.ogg"
                else:
                    dest = src.with_suffix(".ogg")

                # Check if output exists
                if dest.exists() and not self.overwrite_existing:
                    self.progress.emit(
                        f"Skipped {src.name} (output exists)",
                        "WARNING"
                    )
                    skipped += 1
                    continue

                # Convert to OGG
                self._convert_to_ogg(src, dest)
                converted += 1
                self.progress.emit(f"Converted {src.name} → {dest.name}", "SUCCESS")

                # Remove original if requested
                if self.delete_original and src.exists():
                    try:
                        src.unlink()
                        self.progress.emit(f"Removed original {src.name}", "INFO")
                    except Exception as exc:
                        self.progress.emit(
                            f"Could not remove {src.name}: {exc}",
                            "WARNING"
                        )

            except Exception as exc:
                failed += 1
                self.progress.emit(f"Failed {src.name}: {exc}", "ERROR")

        self.finished.emit(total, converted, skipped, failed)

    def _has_loop_points(self, wav_path: Path) -> bool:
        """Check if WAV file contains loop points (smpl chunk)."""
        try:
            data = wav_path.read_bytes()
            pos = 12  # Skip RIFF header (4 bytes ID, 4 size, 4 WAVE)
            data_len = len(data)
            
            while pos + 8 <= data_len:
                if pos + 4 > data_len:
                    break
                    
                chunk_id = data[pos : pos + 4]
                if len(chunk_id) < 4:
                    break
                    
                if pos + 8 > data_len:
                    break
                    
                chunk_size = int.from_bytes(data[pos + 4 : pos + 8], "little")
                
                # Found smpl chunk - has loop points
                if chunk_id == b"smpl":
                    return True
                
                pad = chunk_size % 2
                pos = pos + 8 + chunk_size + pad
                
            return False
        except Exception:
            # If we can't read the file, assume no loop points
            return False

    def _convert_to_ogg(self, src: Path, dest: Path) -> None:
        """Convert audio file to OGG using pydub."""
        try:
            from pydub import AudioSegment
        except ImportError as exc:
            raise RuntimeError(
                "pydub is not installed. Please install it with: pip install pydub"
            ) from exc

        dest.parent.mkdir(parents=True, exist_ok=True)

        # Load audio and convert to OGG
        audio = AudioSegment.from_file(str(src))
        
        # Export with quality setting (0-10 scale for OGG)
        audio.export(
            str(dest),
            format="ogg",
            codec="libvorbis",
            parameters=["-q:a", str(self.quality)]
        )


class OggConverterTool(BaseTool):
    """Convert audio files to OGG format (skips WAV files with loop points)."""

    def __init__(self):
        super().__init__("OGG Converter")
        self.worker: Optional[OggConversionWorker] = None
        self.selected_files: List[Path] = []
        self.setup_tool_ui()

    def setup_tool_ui(self):
        # Input section
        input_group = QGroupBox("Input")
        input_layout = QVBoxLayout()

        # Folder selection
        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("Source Folder:"))
        self.folder_input = QLineEdit()
        self.folder_input.setPlaceholderText("Select folder containing audio files...")
        folder_row.addWidget(self.folder_input)
        folder_btn = QPushButton("Browse...")
        folder_btn.clicked.connect(self.select_folder)
        folder_row.addWidget(folder_btn)
        input_layout.addLayout(folder_row)

        # File selection
        files_row = QHBoxLayout()
        files_row.addWidget(QLabel("Or select files:"))
        self.files_label = QLabel("No files selected")
        self.files_label.setStyleSheet("color: #808080;")
        files_row.addWidget(self.files_label)
        files_btn = QPushButton("Select Files...")
        files_btn.clicked.connect(self.select_files)
        files_row.addWidget(files_btn)
        input_layout.addLayout(files_row)

        input_group.setLayout(input_layout)
        self.content_layout.addWidget(input_group)

        # Output section
        output_group = QGroupBox("Output")
        output_layout = QVBoxLayout()

        # Output folder
        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("Output Folder:"))
        self.output_input = QLineEdit()
        self.output_input.setPlaceholderText("Leave empty to convert in-place...")
        output_row.addWidget(self.output_input)
        output_btn = QPushButton("Browse...")
        output_btn.clicked.connect(self.select_output_folder)
        output_row.addWidget(output_btn)
        output_layout.addLayout(output_row)

        output_group.setLayout(output_layout)
        self.content_layout.addWidget(output_group)

        # Settings section
        settings_group = QGroupBox("Settings")
        settings_layout = QVBoxLayout()

        # Quality setting
        quality_row = QHBoxLayout()
        quality_row.addWidget(QLabel("OGG Quality (0-10):"))
        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(0, 10)
        self.quality_spin.setValue(5)
        self.quality_spin.setToolTip("Higher = better quality, larger file size")
        quality_row.addWidget(self.quality_spin)
        quality_row.addStretch()
        settings_layout.addLayout(quality_row)

        # Options
        self.overwrite_check = QCheckBox("Replace existing outputs")
        self.overwrite_check.setChecked(False)
        self.overwrite_check.setToolTip(
            "Overwrite outputs that already exist in the destination folder."
        )
        settings_layout.addWidget(self.overwrite_check)

        self.delete_original_check = QCheckBox("Delete originals after conversion")
        self.delete_original_check.setChecked(False)
        settings_layout.addWidget(self.delete_original_check)

        settings_group.setLayout(settings_layout)
        self.content_layout.addWidget(settings_group)

        # Info section
        info_label = QLabel(
            "⚠️ WAV files with loop points will be automatically skipped.\n"
            "Supported formats: WAV, MP3, FLAC, M4A, AAC"
        )
        info_label.setStyleSheet("color: #FFA500; padding: 10px; background-color: #2b2b2b; border-radius: 4px;")
        info_label.setWordWrap(True)
        self.content_layout.addWidget(info_label)

        # Action buttons — Cancel + primary action right-aligned, like the
        # other batch tools.
        button_row = QHBoxLayout()
        button_row.addStretch()

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_worker)
        button_row.addWidget(self.cancel_btn)

        self.convert_btn = QPushButton("Process")
        self.convert_btn.clicked.connect(self.start_conversion)
        button_row.addWidget(self.convert_btn)
        self.content_layout.addLayout(button_row)

    def select_folder(self):
        """Select a folder containing audio files."""
        folder = QFileDialog.getExistingDirectory(
            self, "Select folder containing audio files"
        )
        if folder:
            self.folder_input.setText(folder)
            self.selected_files.clear()
            self.files_label.setText("No files selected")

    def select_files(self):
        """Select individual audio files."""
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Select audio files",
            "",
            "Audio Files (*.wav *.mp3 *.ogg *.flac *.m4a *.aac);;All Files (*.*)"
        )
        if files:
            self.selected_files = [Path(f) for f in files]
            self.files_label.setText(f"{len(files)} file(s) selected")
            self.files_label.setStyleSheet("color: #00FF00;")
            self.folder_input.clear()

    def select_output_folder(self):
        """Select output folder for converted files."""
        folder = QFileDialog.getExistingDirectory(
            self, "Select output folder for OGG files"
        )
        if folder:
            self.output_input.setText(folder)

    def start_conversion(self):
        """Start the conversion process."""
        if self.worker and self.worker.isRunning():
            self.log("A conversion is already running.", "WARNING")
            return

        # Gather files to convert
        files_to_convert = []
        
        if self.selected_files:
            files_to_convert = self.selected_files
        elif self.folder_input.text().strip():
            folder = Path(self.folder_input.text().strip())
            if not folder.exists():
                self.log("Source folder does not exist.", "ERROR")
                return
            
            # Find all audio files
            extensions = {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac"}
            for ext in extensions:
                files_to_convert.extend(folder.rglob(f"*{ext}"))
        else:
            self.log("Please select a folder or files first.", "ERROR")
            return

        if not files_to_convert:
            self.log("No audio files found.", "WARNING")
            return

        # Get output folder
        output_folder = None
        if self.output_input.text().strip():
            output_folder = Path(self.output_input.text().strip())

        # Get settings
        quality = self.quality_spin.value()
        overwrite = self.overwrite_check.isChecked()
        delete_original = self.delete_original_check.isChecked()

        self.log(f"Starting conversion of {len(files_to_convert)} file(s)...", "INFO")

        self.convert_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)

        self.worker = OggConversionWorker(
            files=files_to_convert,
            output_folder=output_folder,
            quality=quality,
            overwrite_existing=overwrite,
            delete_original=delete_original,
        )
        self.worker.progress.connect(self.log)
        self.worker.finished.connect(self.on_conversion_finished)
        self.worker.start()

    def cancel_worker(self):
        """Cancel the running worker thread."""
        if self.worker and self.worker.isRunning():
            self.worker.requestInterruption()
            self.worker.quit()
            self.worker.wait(2000)
            self.log("Conversion cancelled.", "WARNING")

        self.convert_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)

    def on_conversion_finished(self, total: int, converted: int, skipped: int, failed: int):
        """Handle conversion completion."""
        self.convert_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)

        self.log(
            f"Done. Total: {total}, Converted: {converted}, Skipped: {skipped}, Failed: {failed}",
            "SUCCESS" if failed == 0 else "WARNING",
        )
        self.emit_status("OGG conversion complete")
