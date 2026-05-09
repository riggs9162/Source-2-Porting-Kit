"""
OGG Converter Tool
Converts audio files to OGG format, skipping WAV files with loop points.
"""

from pathlib import Path
from threading import Lock
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
from app.utils.audio_runner import (
    default_workers,
    parallel_for_each,
    run_ffmpeg,
    wav_has_chunk,
)

AUDIO_EXTENSIONS = {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac"}


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
        self._counter_lock = Lock()
        self._converted = 0
        self._skipped = 0
        self._failed = 0

    def run(self):
        total = len(self.files)
        if total == 0:
            self.finished.emit(0, 0, 0, 0)
            return

        # Pre-create destination folder once when a shared output is set.
        if self.output_folder:
            try:
                self.output_folder.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.progress.emit(f"Could not create output folder: {exc}", "ERROR")
                self.finished.emit(total, 0, 0, total)
                return

        parallel_for_each(
            self.files,
            self._process_one,
            max_workers=default_workers(),
            should_stop=self.isInterruptionRequested,
        )

        self.finished.emit(total, self._converted, self._skipped, self._failed)

    # --- per-file pipeline ----------------------------------------------------

    def _process_one(self, src: Path) -> None:
        if self.isInterruptionRequested():
            return

        try:
            if src.suffix.lower() == ".wav" and wav_has_chunk(src, b"smpl"):
                self.progress.emit(f"Skipped {src.name} (has loop points)", "WARNING")
                self._inc("skipped")
                return

            dest = (
                self.output_folder / f"{src.stem}.ogg"
                if self.output_folder
                else src.with_suffix(".ogg")
            )

            if dest.exists() and not self.overwrite_existing:
                self.progress.emit(f"Skipped {src.name} (output exists)", "WARNING")
                self._inc("skipped")
                return

            if not self.output_folder:
                dest.parent.mkdir(parents=True, exist_ok=True)

            self._convert_to_ogg(src, dest)
            self._inc("converted")
            self.progress.emit(f"Converted {src.name} → {dest.name}", "SUCCESS")

            if self.delete_original:
                try:
                    src.unlink()
                    self.progress.emit(f"Removed original {src.name}", "INFO")
                except OSError as exc:
                    self.progress.emit(
                        f"Could not remove {src.name}: {exc}", "WARNING"
                    )

        except Exception as exc:  # noqa: BLE001
            self._inc("failed")
            self.progress.emit(f"Failed {src.name}: {exc}", "ERROR")

    def _inc(self, kind: str) -> None:
        with self._counter_lock:
            if kind == "converted":
                self._converted += 1
            elif kind == "skipped":
                self._skipped += 1
            elif kind == "failed":
                self._failed += 1

    def _convert_to_ogg(self, src: Path, dest: Path) -> None:
        """Convert audio to OGG/Vorbis via a single ffmpeg pass."""
        # We've already gated on `dest.exists()` and `overwrite_existing`
        # above, so passing -y here is safe and avoids ffmpeg prompting.
        run_ffmpeg(
            [
                "-y",
                "-i",
                str(src),
                "-vn",
                "-c:a",
                "libvorbis",
                "-q:a",
                str(self.quality),
                str(dest),
            ]
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
            
            # Single rglob pass, then filter by extension — much faster
            # than running one rglob per extension on large trees.
            files_to_convert = [
                p for p in folder.rglob("*")
                if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
            ]
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
