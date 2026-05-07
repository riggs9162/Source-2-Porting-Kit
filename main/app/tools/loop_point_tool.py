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
)

from app.tools.base_tool import BaseTool

SUPPORTED_EXTENSIONS = {".ogg", ".mp3", ".wav"}
DEFAULT_KEYWORDS = ["_lp", "_loop", "_looped", "_looping"]


class LoopConversionWorker(QThread):
    """Background worker to convert audio to WAV and add loop points."""

    progress = Signal(str, str)  # message, level
    finished = Signal(int, int, int)  # total, converted, failed

    def __init__(
        self,
        files: List[Path],
        overwrite_existing: bool,
        process_wav: bool,
        delete_original: bool,
    ):
        super().__init__()
        self.files = files
        self.overwrite_existing = overwrite_existing
        self.process_wav = process_wav
        self.delete_original = delete_original

    def run(self):
        converted = 0
        failed = 0
        total = len(self.files)

        for src in self.files:
            if self.isInterruptionRequested():
                break
            try:
                dest = self._compute_destination(src)

                if src.suffix.lower() != ".wav":
                    self._convert_to_wav(src, dest)
                elif self.process_wav and dest != src:
                    # Copy WAV so we do not overwrite unless requested
                    dest.write_bytes(src.read_bytes())

                if src.suffix.lower() == ".wav" and not self.process_wav:
                    # Skipped because user opted out
                    continue

                self._add_loop_points(dest)
                converted += 1
                self.progress.emit(f"Processed {src.name} → {dest.name}", "SUCCESS")

                # Remove original if requested and the output is different
                if self.delete_original and src.exists() and src.resolve() != dest.resolve():
                    try:
                        src.unlink()
                        self.progress.emit(f"Removed original {src.name}", "INFO")
                    except Exception as exc:  # noqa: BLE001
                        self.progress.emit(f"Could not remove {src.name}: {exc}", "WARNING")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                self.progress.emit(f"Failed {src.name}: {exc}", "ERROR")

        self.finished.emit(total, converted, failed)

    def _compute_destination(self, src: Path) -> Path:
        if src.suffix.lower() == ".wav":
            if self.overwrite_existing:
                return src
            return src.with_name(f"{src.stem}_loop.wav")

        dest = src.with_suffix(".wav")
        if not self.overwrite_existing and dest.exists():
            dest = src.with_name(f"{src.stem}_loop.wav")
        return dest

    def _convert_to_wav(self, src: Path, dest: Path) -> None:
        try:
            import ffmpeg  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "ffmpeg-python is not installed. Please install dependencies from requirements.txt."
            ) from exc

        dest.parent.mkdir(parents=True, exist_ok=True)

        # Convert to 16-bit PCM WAV to maximize compatibility
        stream = ffmpeg.input(str(src))
        stream = ffmpeg.output(
            stream,
            str(dest),
            format="wav",
            acodec="pcm_s16le",
        )
        ffmpeg.run(stream, overwrite_output=True, quiet=True)

    def _add_loop_points(self, wav_path: Path) -> None:
        with wave.open(str(wav_path), "rb") as wf:
            params = wf.getparams()
            frames = wf.readframes(wf.getnframes())
            sample_rate = wf.getframerate()
            frame_count = wf.getnframes()

        if frame_count <= 1:
            raise ValueError("Audio is too short to loop (needs more than 1 frame).")

        loop_start = 0
        loop_end = frame_count - 1

        temp_path = wav_path.with_suffix(".tmp_loop.wav")
        try:
            with wave.open(str(temp_path), "wb") as out:
                out.setparams(params)
                out.writeframes(frames)

            raw = temp_path.read_bytes()
            raw = self._strip_existing_smpl(raw)
            smpl_chunk = self._build_smpl_chunk(sample_rate, loop_start, loop_end)

            updated = bytearray(raw)
            updated.extend(smpl_chunk)

            # Update RIFF chunk size (bytes 4-8) = file size - 8
            riff_size = len(updated) - 8
            updated[4:8] = struct.pack("<I", riff_size)

            wav_path.write_bytes(updated)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def _strip_existing_smpl(self, data: bytes) -> bytes:
        """Remove any existing smpl chunk to avoid duplicates."""
        pos = 12  # Skip RIFF header (4 bytes ID, 4 size, 4 WAVE)
        data_len = len(data)
        while pos + 8 <= data_len:
            chunk_id = data[pos : pos + 4]
            if len(chunk_id) < 4:
                break
            chunk_size = int.from_bytes(data[pos + 4 : pos + 8], "little")
            pad = chunk_size % 2
            next_pos = pos + 8 + chunk_size + pad

            if chunk_id == b"smpl":
                return data[:pos] + data[next_pos:]

            pos = next_pos
        return data

    def _build_smpl_chunk(self, sample_rate: int, loop_start: int, loop_end: int) -> bytes:
        # Sample period in nanoseconds per sample
        sample_period = int(1_000_000_000 / sample_rate) if sample_rate else 0

        # smpl chunk header fields (9 uint32)
        header = struct.pack(
            "<IIIIIIIII",
            0,  # manufacturer
            0,  # product
            sample_period,
            60,  # midi_unity_note (middle C)
            0,  # midi_pitch_fraction
            0,  # smpte_format
            0,  # smpte_offset
            1,  # num_sample_loops
            0,  # sampler_data
        )

        loop = struct.pack(
            "<IIIIII",
            0,  # cue_point_id
            0,  # type (forward)
            loop_start,
            loop_end,
            0,  # fraction
            0,  # play_count (0 = infinite)
        )

        chunk_data = header + loop
        chunk_size = len(chunk_data)
        return b"smpl" + struct.pack("<I", chunk_size) + chunk_data


class LoopPointTool(BaseTool):
    """Convert audio files to WAV and add loop points."""

    def __init__(self):
        super().__init__("Loop Point Converter")
        self.worker: Optional[LoopConversionWorker] = None
        self.selected_path: Optional[Path] = None
        self.setup_tool_ui()

    def setup_tool_ui(self):
        # Source selection
        src_group = QGroupBox("Source")
        src_layout = QVBoxLayout()

        path_row = QHBoxLayout()
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("Select a file or folder containing audio…")
        path_row.addWidget(self.path_input)

        file_btn = QPushButton("File…")
        file_btn.clicked.connect(self.select_file)
        path_row.addWidget(file_btn)

        folder_btn = QPushButton("Folder…")
        folder_btn.clicked.connect(self.select_folder)
        path_row.addWidget(folder_btn)

        src_layout.addLayout(path_row)

        options_row = QHBoxLayout()
        self.recursive_check = QCheckBox("Recursive (include subfolders)")
        self.recursive_check.setChecked(True)
        self.recursive_check.setToolTip(
            "When on, scan all subfolders of the input. When off, scan only "
            "the input folder itself."
        )
        options_row.addWidget(self.recursive_check)

        self.process_wav_check = QCheckBox("Add loop chunk to existing WAV")
        self.process_wav_check.setChecked(True)
        options_row.addWidget(self.process_wav_check)

        self.overwrite_check = QCheckBox("Replace existing outputs")
        self.overwrite_check.setChecked(True)
        self.overwrite_check.setToolTip(
            "Overwrite outputs that already exist in the destination folder."
        )
        options_row.addWidget(self.overwrite_check)

        self.delete_original_check = QCheckBox("Delete originals after conversion")
        self.delete_original_check.setChecked(True)
        options_row.addWidget(self.delete_original_check)

        src_layout.addLayout(options_row)
        src_group.setLayout(src_layout)
        self.content_layout.addWidget(src_group)

        # Detection options
        detect_group = QGroupBox("Loop keyword detection")
        detect_layout = QVBoxLayout()

        keyword_row = QHBoxLayout()
        keyword_row.addWidget(QLabel("Keywords (comma separated):"))
        self.keyword_input = QLineEdit(",".join(DEFAULT_KEYWORDS))
        keyword_row.addWidget(self.keyword_input)
        detect_layout.addLayout(keyword_row)

        detect_group.setLayout(detect_layout)
        self.content_layout.addWidget(detect_group)

        # Action buttons — Cancel + primary action right-aligned, like the
        # other batch tools.
        action_row = QHBoxLayout()
        action_row.addStretch()

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_worker)
        action_row.addWidget(self.cancel_btn)

        self.run_btn = QPushButton("Process")
        self.run_btn.clicked.connect(self.start_conversion)
        action_row.addWidget(self.run_btn)

        self.content_layout.addLayout(action_row)

    def select_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select audio file",
            "",
            "Audio Files (*.wav *.ogg *.mp3);;All Files (*.*)",
        )
        if path:
            self.path_input.setText(path)
            self.selected_path = Path(path)

    def select_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Select folder containing audio")
        if path:
            self.path_input.setText(path)
            self.selected_path = Path(path)

    def start_conversion(self):
        if self.worker and self.worker.isRunning():
            self.log("A conversion is already running.", "WARNING")
            return

        path_text = self.path_input.text().strip()
        if not path_text:
            self.log("Please select a file or folder first.", "ERROR")
            return

        selected = Path(path_text)
        if not selected.exists():
            self.log("Selected path does not exist.", "ERROR")
            return

        keywords = [k.strip().lower() for k in self.keyword_input.text().split(",") if k.strip()]
        if not keywords:
            keywords = DEFAULT_KEYWORDS

        recursive = self.recursive_check.isChecked()
        process_wav = self.process_wav_check.isChecked()
        overwrite = self.overwrite_check.isChecked()
        delete_original = self.delete_original_check.isChecked()

        files = self._collect_targets(selected, keywords, recursive, process_wav)
        if not files:
            self.log("No matching audio files found with the provided keywords.", "WARNING")
            return

        self.run_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.emit_status("Starting loop point conversion…")

        self.worker = LoopConversionWorker(
            files=files,
            overwrite_existing=overwrite,
            process_wav=process_wav,
            delete_original=delete_original,
        )
        self.worker.progress.connect(self.log)
        self.worker.finished.connect(self.on_finished)
        self.worker.start()

    def cancel_worker(self):
        if self.worker and self.worker.isRunning():
            self.worker.requestInterruption()
            self.worker.quit()
            self.worker.wait(2000)
            self.log("Conversion cancelled.", "WARNING")
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)

    def on_finished(self, total: int, converted: int, failed: int):
        self.run_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.emit_status("Loop point conversion complete.")
        self.log(
            f"Done. Total queued: {total}, converted: {converted}, failed: {failed}",
            "SUCCESS" if failed == 0 else "WARNING",
        )

    def _collect_targets(
        self,
        selected: Path,
        keywords: List[str],
        recursive: bool,
        process_wav: bool,
    ) -> List[Path]:
        if selected.is_file():
            candidates = [selected]
        else:
            pattern = "**/*" if recursive else "*"
            candidates = [p for p in selected.glob(pattern) if p.is_file()]

        targets: List[Path] = []
        skipped_keyword = 0
        skipped_ext = 0

        for path in candidates:
            ext = path.suffix.lower()
            if ext not in SUPPORTED_EXTENSIONS:
                skipped_ext += 1
                continue

            name_lower = path.stem.lower()
            if not any(keyword in name_lower for keyword in keywords):
                skipped_keyword += 1
                continue

            if ext == ".wav" and not process_wav:
                continue

            targets.append(path)

        if skipped_ext:
            self.log(f"Skipped {skipped_ext} files with unsupported extensions.", "INFO")
        if skipped_keyword:
            self.log(f"Skipped {skipped_keyword} files without loop keywords.", "INFO")

        self.log(f"Queued {len(targets)} file(s) for processing.", "INFO")
        return targets
