"""
Loop Point Converter Tool

Converts audio files to WAV and embeds a RIFF ``smpl`` loop-point chunk so the
Source engine plays them as seamless loops.
"""

import shutil
import struct
import wave
from pathlib import Path
from threading import Lock
from typing import List

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QCheckBox, QLabel, QLineEdit, QPushButton

from app.tools.base_tool import BaseTool
from app.ui import widgets
from app.utils.audio_runner import default_workers, parallel_for_each, run_ffmpeg

SUPPORTED_EXTENSIONS = {".ogg", ".mp3", ".wav"}
DEFAULT_KEYWORDS = ["_lp", "_loop", "_looped", "_looping"]


class LoopConversionWorker(QThread):
    """Background worker to convert audio to WAV and add loop points."""

    progress = Signal(str, str)  # message, level
    progress_value = Signal(int, int)  # files done, total (drives the progress bar)
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
        self._counter_lock = Lock()
        self._converted = 0
        self._failed = 0
        self._done_files = 0

    def run(self):
        total = len(self.files)
        if total == 0:
            self.finished.emit(0, 0, 0)
            return

        parallel_for_each(
            self.files,
            self._process_one,
            max_workers=default_workers(),
            should_stop=self.isInterruptionRequested,
        )

        self.finished.emit(total, self._converted, self._failed)

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
            dest = self._compute_destination(src)

            if src.suffix.lower() != ".wav":
                self._convert_to_wav(src, dest)
            elif self.process_wav and dest != src:
                # Copy WAV so we do not overwrite unless requested.
                # shutil.copyfile uses the OS fast-copy path on modern
                # Python (~3x faster than read_bytes/write_bytes for large
                # files since it never materializes the full payload in
                # Python memory).
                shutil.copyfile(src, dest)

            if src.suffix.lower() == ".wav" and not self.process_wav:
                return

            self._add_loop_points(dest)
            with self._counter_lock:
                self._converted += 1
            self.progress.emit(f"Processed {src.name} → {dest.name}", "SUCCESS")

            if self.delete_original and src.exists() and src.resolve() != dest.resolve():
                try:
                    src.unlink()
                    self.progress.emit(f"Removed original {src.name}", "INFO")
                except OSError as exc:
                    self.progress.emit(f"Could not remove {src.name}: {exc}", "WARNING")
        except Exception as exc:  # noqa: BLE001
            with self._counter_lock:
                self._failed += 1
            self.progress.emit(f"Failed {src.name}: {exc}", "ERROR")

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
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Direct ffmpeg call avoids the ffmpeg-python wrapper overhead and
        # the extra Python attribute/object plumbing — same single subprocess
        # underneath, just less indirection.
        run_ffmpeg(
            [
                "-y",
                "-i",
                str(src),
                "-vn",
                "-f",
                "wav",
                "-acodec",
                "pcm_s16le",
                str(dest),
            ]
        )

    def _add_loop_points(self, wav_path: Path) -> None:
        # Inspect the file via the wave module to read sample-rate and frame
        # count without copying the audio payload into Python memory.
        with wave.open(str(wav_path), "rb") as wf:
            sample_rate = wf.getframerate()
            frame_count = wf.getnframes()

        if frame_count <= 1:
            raise ValueError("Audio is too short to loop (needs more than 1 frame).")

        # Read the existing file once, strip any pre-existing smpl chunk,
        # append the new one, and rewrite RIFF size — single read + single
        # write. The previous implementation wrote a temp WAV, read it back,
        # then wrote the final file (3 disk passes).
        raw = wav_path.read_bytes()
        stripped = self._strip_existing_smpl(raw)
        smpl_chunk = self._build_smpl_chunk(sample_rate, 0, frame_count - 1)

        updated = bytearray(stripped)
        updated.extend(smpl_chunk)
        # RIFF chunk size = total file size - 8
        updated[4:8] = struct.pack("<I", len(updated) - 8)

        wav_path.write_bytes(bytes(updated))

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
        self.setup_tool_ui()

    def setup_tool_ui(self):
        # One line edit fed by BOTH a file and a folder button, so it stays a
        # plain QLineEdit instead of a single-mode PathPickerRow.
        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("Select a file or folder containing audio…")

        file_btn = QPushButton("File…")
        file_btn.clicked.connect(self.select_file)
        folder_btn = QPushButton("Folder…")
        folder_btn.clicked.connect(self.select_folder)
        path_row = widgets.hbox(self.path_input, file_btn, folder_btn)

        self.recursive_check = QCheckBox("Recursive (include subfolders)")
        self.recursive_check.setChecked(True)
        self.recursive_check.setToolTip(
            "When on, scan all subfolders of the input. When off, scan only "
            "the input folder itself."
        )

        self.process_wav_check = QCheckBox("Add loop chunk to existing WAV")
        self.process_wav_check.setChecked(True)

        self.overwrite_check = QCheckBox("Replace existing outputs")
        self.overwrite_check.setChecked(True)
        self.overwrite_check.setToolTip(
            "Overwrite outputs that already exist in the destination folder."
        )

        self.delete_original_check = QCheckBox("Delete originals after conversion")
        self.delete_original_check.setChecked(True)

        options_row = widgets.hbox(self.recursive_check, self.process_wav_check,
                                   self.overwrite_check, self.delete_original_check)

        self.add_group("Source", path_row, options_row)

        self.keyword_input = QLineEdit(",".join(DEFAULT_KEYWORDS))
        keyword_row = widgets.hbox(QLabel("Keywords (comma separated):"), self.keyword_input)
        self.add_group("Loop keyword detection", keyword_row)

        self.cancel_btn = self.make_cancel_button()
        self.run_btn = self.make_run_button("Process", self.start_conversion)
        self.add_action_row(self.cancel_btn, self.run_btn)

    def select_file(self):
        path = widgets.browse_open_file(
            self, "Select audio file",
            file_filter="Audio Files (*.wav *.ogg *.mp3);;All Files (*.*)",
            settings=self.settings, remember_key=f"{self.tool_name}/input",
            fallback=self.path_input.text().strip())
        if path:
            self.path_input.setText(path)

    def select_folder(self):
        path = widgets.browse_dir(
            self, "Select folder containing audio",
            settings=self.settings, remember_key=f"{self.tool_name}/input",
            fallback=self.path_input.text().strip())
        if path:
            self.path_input.setText(path)

    def start_conversion(self):
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

        self.start_worker(
            LoopConversionWorker(
                files=files,
                overwrite_existing=overwrite,
                process_wav=process_wav,
                delete_original=delete_original,
            ),
            self.on_finished,
            busy_widgets=(self.run_btn,),
            cancel_button=self.cancel_btn,
            status="Starting loop point conversion…",
            clear_log=False,
        )

    def on_finished(self, total: int, converted: int, failed: int):
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
