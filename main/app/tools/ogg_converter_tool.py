"""
OGG Converter Tool
Converts audio files to OGG format, skipping WAV files with loop points.
"""

from pathlib import Path
from threading import Lock
from typing import List, Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton, QSpinBox

from app.tools.base_tool import BaseTool
from app.ui import widgets
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
    progress_value = Signal(int, int)  # files done, total (drives the progress bar)
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
        self._done_files = 0

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
            self._process_one_inner(src)
        finally:
            # Progress counts files finished regardless of outcome; emitted
            # from pool threads (safe — cross-thread signals are queued).
            with self._counter_lock:
                self._done_files += 1
                done = self._done_files
            self.progress_value.emit(done, len(self.files))

    def _process_one_inner(self, src: Path) -> None:
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
        self.selected_files: List[Path] = []
        self.setup_tool_ui()

    def setup_tool_ui(self):
        self.folder_picker = self.create_picker(
            "dir", "source folder", label="Source Folder:",
            placeholder="Select folder containing audio files...",
            dialog_title="Select folder containing audio files")
        self.folder_picker.changed.connect(self._folder_picked)

        self.files_label = QLabel("No files selected")
        self.files_label.setStyleSheet("color: #808080;")
        files_btn = QPushButton("Select Files...")
        files_btn.clicked.connect(self.select_files)
        files_row = widgets.hbox(QLabel("Or select files:"), self.files_label, files_btn)
        self.add_group("Input", self.folder_picker, files_row)

        self.output_picker = self.create_picker(
            "dir", "output folder", label="Output Folder:",
            placeholder="Leave empty to convert in-place...",
            dialog_title="Select output folder for OGG files")
        self.add_group("Output", self.output_picker)

        self.quality_spin = QSpinBox()
        self.quality_spin.setRange(0, 10)
        self.quality_spin.setValue(5)
        self.quality_spin.setToolTip("Higher = better quality, larger file size")
        quality_row = widgets.hbox(QLabel("OGG Quality (0-10):"), self.quality_spin,
                                   stretch_after=True)

        self.overwrite_check = QCheckBox("Replace existing outputs")
        self.overwrite_check.setChecked(False)
        self.overwrite_check.setToolTip(
            "Overwrite outputs that already exist in the destination folder."
        )
        self.delete_original_check = QCheckBox("Delete originals after conversion")
        self.delete_original_check.setChecked(False)
        self.add_group("Settings", quality_row, self.overwrite_check, self.delete_original_check)

        self.content_layout.addWidget(widgets.note_label(
            "⚠️ WAV files with loop points will be automatically skipped.\n"
            "Supported formats: WAV, MP3, FLAC, M4A, AAC"
        ))

        self.cancel_btn = self.make_cancel_button()
        self.convert_btn = self.make_run_button("Process", self.start_conversion)
        self.add_action_row(self.cancel_btn, self.convert_btn)

    def _folder_picked(self, folder: str):
        self.selected_files.clear()
        self.files_label.setText("No files selected")
        self.files_label.setStyleSheet("color: #808080;")

    def select_files(self):
        """Select individual audio files."""
        files = widgets.browse_open_files(
            self, "Select audio files",
            file_filter="Audio Files (*.wav *.mp3 *.ogg *.flac *.m4a *.aac);;All Files (*.*)",
            settings=self.settings, remember_key=f"{self.tool_name}/source folder")
        if files:
            self.selected_files = [Path(f) for f in files]
            self.files_label.setText(f"{len(files)} file(s) selected")
            self.files_label.setStyleSheet("color: #00FF00;")
            self.folder_picker.set_path("")

    def start_conversion(self):
        """Start the conversion process."""
        if self.worker and self.worker.isRunning():
            self.log("A conversion is already running.", "WARNING")
            return

        # Gather files to convert
        files_to_convert = []

        if self.selected_files:
            files_to_convert = self.selected_files
        elif self.folder_picker.path():
            folder = Path(self.folder_picker.path())
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
        if self.output_picker.path():
            output_folder = Path(self.output_picker.path())

        # Get settings
        quality = self.quality_spin.value()
        overwrite = self.overwrite_check.isChecked()
        delete_original = self.delete_original_check.isChecked()

        self.log(f"Starting conversion of {len(files_to_convert)} file(s)...", "INFO")
        self.start_worker(
            OggConversionWorker(
                files=files_to_convert,
                output_folder=output_folder,
                quality=quality,
                overwrite_existing=overwrite,
                delete_original=delete_original,
            ),
            self.on_conversion_finished,
            busy_widgets=(self.convert_btn,),
            cancel_button=self.cancel_btn,
            clear_log=False,
        )

    def on_conversion_finished(self, total: int, converted: int, skipped: int, failed: int):
        """Handle conversion completion."""
        self.log(
            f"Done. Total: {total}, Converted: {converted}, Skipped: {skipped}, Failed: {failed}",
            "SUCCESS" if failed == 0 else "WARNING",
        )
        self.emit_status("OGG conversion complete")
