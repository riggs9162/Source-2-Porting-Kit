"""
Quad to Stereo Audio Tool
Converts quad audio files (L, LS, R, RS) to stereo format
"""

import math
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
    QRadioButton,
    QButtonGroup,
    QSpinBox,
    QComboBox,
)

from app.tools.base_tool import BaseTool


class QuadToStereoWorker(QThread):
    """Worker thread for quad-to-stereo conversion."""

    progress = Signal(str, str)  # message, level
    finished = Signal(int, int, int)  # converted, skipped, failed

    def __init__(
        self,
        quad_groups: Dict[str, Dict[str, Path]],
        output_folder: Path,
        mix_mode: str,
        volume_adjustment: float,
        output_format: str,
        quality: str,
        overwrite_existing: bool,
        delete_originals: bool,
    ):
        super().__init__()
        self.quad_groups = quad_groups
        self.output_folder = output_folder
        self.mix_mode = mix_mode
        self.volume_adjustment = volume_adjustment
        self.output_format = output_format
        self.quality = quality
        self.overwrite_existing = overwrite_existing
        self.delete_originals = delete_originals

    def run(self):
        try:
            from pydub import AudioSegment
        except ImportError:
            self.progress.emit(
                "pydub library is required. Install with: pip install pydub",
                "ERROR",
            )
            self.finished.emit(0, 0, len(self.quad_groups))
            return

        converted = 0
        skipped = 0
        failed = 0

        self.output_folder.mkdir(parents=True, exist_ok=True)

        self.progress.emit(
            f"Starting conversion of {len(self.quad_groups)} quad groups...",
            "INFO",
        )
        self.progress.emit(f"Mix mode: {self.mix_mode}", "INFO")
        self.progress.emit(f"Volume adjustment: {self.volume_adjustment}x", "INFO")
        self.progress.emit(f"Output format: {self.output_format} ({self.quality})", "INFO")

        for base_name, files in self.quad_groups.items():
            if self.isInterruptionRequested():
                break

            try:
                self.progress.emit(f"Processing: {base_name}", "INFO")

                # Determine output filename
                output_filename = f"{base_name}.{self.output_format}"
                output_path = self.output_folder / output_filename

                # Check if output exists
                if output_path.exists() and not self.overwrite_existing:
                    self.progress.emit(f"  Skipped (file exists): {output_filename}", "WARNING")
                    skipped += 1
                    continue

                # Load audio files
                l_audio = AudioSegment.from_file(str(files["l"]))
                ls_audio = AudioSegment.from_file(str(files["ls"]))
                r_audio = AudioSegment.from_file(str(files["r"]))
                rs_audio = AudioSegment.from_file(str(files["rs"]))

                # Ensure all files have the same length
                min_length = min(len(l_audio), len(ls_audio), len(r_audio), len(rs_audio))
                l_audio = l_audio[:min_length]
                ls_audio = ls_audio[:min_length]
                r_audio = r_audio[:min_length]
                rs_audio = rs_audio[:min_length]

                # Mix channels based on mode
                if self.mix_mode == "balance":
                    # L+LS to left channel, R+RS to right channel
                    left_channel = l_audio.overlay(ls_audio)
                    right_channel = r_audio.overlay(rs_audio)
                else:  # downmix
                    # Mix all channels to stereo
                    mono_mix = l_audio.overlay(ls_audio).overlay(r_audio).overlay(rs_audio)
                    left_channel = mono_mix
                    right_channel = mono_mix

                # Apply volume adjustment
                if self.volume_adjustment != 1.0:
                    db_adjustment = 20 * math.log10(self.volume_adjustment)
                    left_channel = left_channel.apply_gain(db_adjustment)
                    right_channel = right_channel.apply_gain(db_adjustment)

                # Create stereo audio
                stereo_audio = AudioSegment.from_mono_audiosegments(left_channel, right_channel)

                # Export based on format
                export_params: Dict = {}
                if self.output_format == "mp3":
                    export_params["bitrate"] = self.quality
                elif self.output_format == "ogg":
                    export_params["bitrate"] = self.quality

                stereo_audio.export(str(output_path), format=self.output_format, **export_params)

                self.progress.emit(f"  Converted: {output_filename}", "SUCCESS")
                converted += 1

                # Delete originals if requested
                if self.delete_originals:
                    for channel in ["l", "ls", "r", "rs"]:
                        try:
                            files[channel].unlink()
                            self.progress.emit(f"  Deleted: {files[channel].name}", "INFO")
                        except Exception as exc:
                            self.progress.emit(
                                f"  Failed to delete {files[channel].name}: {exc}",
                                "WARNING",
                            )

            except Exception as exc:
                self.progress.emit(f"  Error processing {base_name}: {exc}", "ERROR")
                failed += 1

        self.progress.emit("Conversion complete!", "SUCCESS")
        self.finished.emit(converted, skipped, failed)


class QuadToStereoTool(BaseTool):
    """Convert quad audio files (L, LS, R, RS) to stereo format."""

    def __init__(self):
        super().__init__("Quad to Stereo")
        self.worker: Optional[QuadToStereoWorker] = None
        self.quad_groups: Dict[str, Dict[str, Path]] = {}
        self.setup_tool_ui()

    def setup_tool_ui(self):
        # Input/Output section
        io_group = QGroupBox("Input & Output")
        io_layout = QVBoxLayout()

        # Input folder
        input_row = QHBoxLayout()
        input_row.addWidget(QLabel("Input Folder:"))
        self.input_folder = QLineEdit()
        self.input_folder.setPlaceholderText(
            "Select folder containing quad audio files..."
        )
        input_row.addWidget(self.input_folder)
        browse_input_btn = QPushButton("Browse...")
        browse_input_btn.clicked.connect(self.select_input_folder)
        input_row.addWidget(browse_input_btn)
        io_layout.addLayout(input_row)

        # Output folder
        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("Output Folder:"))
        self.output_folder = QLineEdit()
        self.output_folder.setPlaceholderText("Select output folder for stereo files...")
        output_row.addWidget(self.output_folder)
        browse_output_btn = QPushButton("Browse...")
        browse_output_btn.clicked.connect(self.select_output_folder)
        output_row.addWidget(browse_output_btn)
        io_layout.addLayout(output_row)

        io_group.setLayout(io_layout)
        self.content_layout.addWidget(io_group)

        # Mix mode section
        mix_group = QGroupBox("Mix Mode")
        mix_layout = QVBoxLayout()

        self.mix_mode_group = QButtonGroup()
        balance_radio = QRadioButton("Balance (L+LS → Left, R+RS → Right)")
        balance_radio.setChecked(True)
        downmix_radio = QRadioButton("Downmix (All → Stereo)")

        self.mix_mode_group.addButton(balance_radio, 0)
        self.mix_mode_group.addButton(downmix_radio, 1)

        mix_layout.addWidget(balance_radio)
        mix_layout.addWidget(downmix_radio)
        mix_group.setLayout(mix_layout)
        self.content_layout.addWidget(mix_group)

        # Audio settings section
        audio_group = QGroupBox("Audio Settings")
        audio_layout = QVBoxLayout()

        # Volume adjustment
        volume_row = QHBoxLayout()
        volume_row.addWidget(QLabel("Volume Adjustment:"))
        self.volume_spin = QSpinBox()
        self.volume_spin.setRange(10, 200)
        self.volume_spin.setValue(100)
        self.volume_spin.setSuffix("%")
        self.volume_spin.setToolTip("Adjust output volume (100% = no change)")
        volume_row.addWidget(self.volume_spin)
        volume_row.addStretch()
        audio_layout.addLayout(volume_row)

        # Output format and quality
        format_row = QHBoxLayout()
        format_row.addWidget(QLabel("Output Format:"))
        self.format_combo = QComboBox()
        self.format_combo.addItems(["mp3", "wav", "ogg"])
        self.format_combo.currentTextChanged.connect(self.on_format_changed)
        format_row.addWidget(self.format_combo)

        format_row.addWidget(QLabel("Quality:"))
        self.quality_combo = QComboBox()
        self.quality_combo.addItems(["128k", "192k", "256k", "320k"])
        self.quality_combo.setCurrentText("192k")
        format_row.addWidget(self.quality_combo)
        format_row.addStretch()
        audio_layout.addLayout(format_row)

        audio_group.setLayout(audio_layout)
        self.content_layout.addWidget(audio_group)

        # Options section
        options_group = QGroupBox("Options")
        options_layout = QVBoxLayout()

        self.overwrite_check = QCheckBox("Overwrite existing files")
        self.overwrite_check.setChecked(False)
        options_layout.addWidget(self.overwrite_check)

        self.delete_originals_check = QCheckBox("Delete original quad files after conversion")
        self.delete_originals_check.setChecked(False)
        options_layout.addWidget(self.delete_originals_check)

        options_group.setLayout(options_layout)
        self.content_layout.addWidget(options_group)

        # Action buttons
        button_row = QHBoxLayout()
        self.scan_btn = QPushButton("Scan for Quad Groups")
        self.scan_btn.clicked.connect(self.scan_quad_groups)
        button_row.addWidget(self.scan_btn)

        self.convert_btn = QPushButton("Convert to Stereo")
        self.convert_btn.clicked.connect(self.start_conversion)
        button_row.addWidget(self.convert_btn)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_worker)
        button_row.addWidget(self.cancel_btn)

        button_row.addStretch()
        self.content_layout.addLayout(button_row)

    def on_format_changed(self, format_str: str):
        """Update quality combo visibility based on selected format."""
        self.quality_combo.setEnabled(format_str in ["mp3", "ogg"])

    def select_input_folder(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select folder containing quad audio files"
        )
        if path:
            self.input_folder.setText(path)

    def select_output_folder(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select output folder for stereo files"
        )
        if path:
            self.output_folder.setText(path)

    def find_quad_groups(self, root_path: Path, log_incomplete: bool = False) -> Tuple[Dict[str, Dict[str, Path]], Dict[str, Dict[str, Path]], List[Tuple[str, str]], List[str]]:
        """Find all complete quad audio groups (L, LS, R, RS) or (front_l, front_r, rear_l, rear_r)."""
        # Pattern 2: basename_front_l.wav, basename_front_r.wav, basename_rear_l.wav, basename_rear_r.wav (check FIRST - more specific)
        pattern2 = re.compile(r"(.+?)_(front|rear)_(l|r)$", re.IGNORECASE)
        # Pattern 1: basename_l.mp3, basename_ls.mp3, basename_r.mp3, basename_rs.mp3
        pattern1 = re.compile(r"(.+?)_(l|ls|r|rs)$", re.IGNORECASE)
        
        groups: Dict[str, Dict[str, Path]] = {}
        matched_files: List[Tuple[str, str]] = []
        unmatched_files: List[str] = []

        if not root_path.exists():
            return {}, {}, [], []

        # Supported audio extensions
        audio_exts = {".mp3", ".wav", ".ogg", ".flac", ".m4a"}

        for file_path in root_path.rglob("*"):
            if not file_path.is_file() or file_path.suffix.lower() not in audio_exts:
                continue

            matched = False
            
            # Try pattern 2 FIRST (front_l, front_r, rear_l, rear_r) - more specific
            match = pattern2.match(file_path.stem)
            if match:
                base_name = match.group(1)
                position = match.group(2).lower()  # front or rear
                side = match.group(3).lower()  # l or r

                if base_name not in groups:
                    groups[base_name] = {"root": file_path.parent, "pattern": 2}

                # Map front_l -> l, front_r -> r, rear_l -> ls, rear_r -> rs
                if position == "front" and side == "l":
                    groups[base_name]["l"] = file_path
                    channel_mapped = "l"
                elif position == "front" and side == "r":
                    groups[base_name]["r"] = file_path
                    channel_mapped = "r"
                elif position == "rear" and side == "l":
                    groups[base_name]["ls"] = file_path
                    channel_mapped = "ls"
                elif position == "rear" and side == "r":
                    groups[base_name]["rs"] = file_path
                    channel_mapped = "rs"
                
                matched_files.append((file_path.name, f"Pattern 2: {base_name} - {position}_{side} -> {channel_mapped}"))
                matched = True
                continue
            
            # Try pattern 1 (L, LS, R, RS)
            match = pattern1.match(file_path.stem)
            if match:
                base_name = match.group(1)
                channel = match.group(2).lower()

                if base_name not in groups:
                    groups[base_name] = {"root": file_path.parent, "pattern": 1}

                groups[base_name][channel] = file_path
                matched_files.append((file_path.name, f"Pattern 1: {base_name} - {channel}"))
                matched = True
                continue
            
            if not matched:
                unmatched_files.append(file_path.name)

        # Filter for complete groups (must have all 4 channels)
        complete_groups = {
            base_name: files
            for base_name, files in groups.items()
            if all(ch in files for ch in ["l", "ls", "r", "rs"])
        }
        
        incomplete_groups = {}
        if log_incomplete:
            incomplete_groups = {
                base_name: files
                for base_name, files in groups.items()
                if not all(ch in files for ch in ["l", "ls", "r", "rs"])
            }

        return complete_groups, incomplete_groups, matched_files, unmatched_files

    def scan_quad_groups(self):
        """Scan for quad audio groups and log results."""
        input_text = self.input_folder.text().strip()
        if not input_text:
            self.log("Please select an input folder first.", "ERROR")
            return

        input_path = Path(input_text)
        if not input_path.exists():
            self.log("Input folder does not exist.", "ERROR")
            return

        self.log(f"Scanning for quad audio groups in: {input_path}", "INFO")

        result = self.find_quad_groups(input_path, log_incomplete=True)
        complete_groups, incomplete_groups, matched_files, unmatched_files = result
        
        # Show what files were found
        total_audio_files = len(matched_files) + len(unmatched_files)
        self.log(f"Found {total_audio_files} audio files in directory", "INFO")
        
        if matched_files:
            self.log(f"Matched {len(matched_files)} files to quad patterns:", "SUCCESS")
            for file, info in matched_files[:20]:  # Show first 20 to avoid spam
                self.log(f"  ✓ {file} -> {info}", "INFO")
            if len(matched_files) > 20:
                self.log(f"  ... and {len(matched_files) - 20} more", "INFO")
        
        if unmatched_files:
            self.log(f"Could not match {len(unmatched_files)} files to any quad pattern:", "WARNING")
            for file in unmatched_files[:10]:  # Show first 10
                self.log(f"  ✗ {file}", "WARNING")
            if len(unmatched_files) > 10:
                self.log(f"  ... and {len(unmatched_files) - 10} more", "WARNING")
        
        if incomplete_groups:
            self.log(f"Found {len(incomplete_groups)} incomplete quad groups:", "WARNING")
            for base_name, files in incomplete_groups.items():
                missing = [ch for ch in ["l", "ls", "r", "rs"] if ch not in files]
                present = [ch for ch in ["l", "ls", "r", "rs"] if ch in files]
                self.log(f"  {base_name}: has {present}, missing {missing}", "WARNING")
                for channel in present:
                    self.log(f"    {channel.upper()}: {files[channel].name}", "INFO")

        if not complete_groups:
            self.log("No complete quad audio groups found.", "WARNING")
            self.emit_status("No complete quad groups found")
            return

        self.quad_groups = complete_groups
        self.log(f"Found {len(complete_groups)} complete quad audio groups:", "SUCCESS")

        for base_name, files in complete_groups.items():
            self.log(f"  {base_name}:", "INFO")
            for channel in ["l", "ls", "r", "rs"]:
                if channel in files:
                    rel_path = files[channel].relative_to(input_path)
                    self.log(f"    {channel.upper()}: {rel_path}", "INFO")

        self.emit_status(f"Found {len(complete_groups)} quad groups")

    def start_conversion(self):
        """Start the quad-to-stereo conversion process."""
        if self.worker and self.worker.isRunning():
            self.log("A conversion is already running.", "WARNING")
            return

        if not self.quad_groups:
            self.log("Please scan for quad groups first.", "ERROR")
            return

        output_text = self.output_folder.text().strip()
        if not output_text:
            self.log("Please select an output folder first.", "ERROR")
            return

        output_path = Path(output_text)

        mix_mode = "balance" if self.mix_mode_group.checkedId() == 0 else "downmix"
        volume_adjustment = self.volume_spin.value() / 100.0
        output_format = self.format_combo.currentText()
        quality = self.quality_combo.currentText()
        overwrite = self.overwrite_check.isChecked()
        delete_originals = self.delete_originals_check.isChecked()

        self.scan_btn.setEnabled(False)
        self.convert_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.emit_status("Starting quad-to-stereo conversion...")

        self.worker = QuadToStereoWorker(
            quad_groups=self.quad_groups,
            output_folder=output_path,
            mix_mode=mix_mode,
            volume_adjustment=volume_adjustment,
            output_format=output_format,
            quality=quality,
            overwrite_existing=overwrite,
            delete_originals=delete_originals,
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

        self.scan_btn.setEnabled(True)
        self.convert_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)

    def on_conversion_finished(self, converted: int, skipped: int, failed: int):
        """Handle conversion completion."""
        self.scan_btn.setEnabled(True)
        self.convert_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)

        total = converted + skipped + failed
        self.log(
            f"Done. Total: {total}, Converted: {converted}, Skipped: {skipped}, "
            f"Failed: {failed}",
            "SUCCESS" if failed == 0 else "WARNING",
        )
        self.emit_status("Quad-to-stereo conversion complete")
