"""
VTF Clamp Tool

Batch-clamps existing VTF textures to power-of-two dimensions with a user-selected
maximum size, such as 512, 1024, or 2048 pixels.
"""

import math
import shutil
import struct
from pathlib import Path
from typing import List, Tuple

import numpy as np
from PIL import Image
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QLabel,
    QPushButton,
)

from sourcepp import vtfpp

from app.tools.base_tool import BaseTool
from app.ui import widgets


MAX_TEXTURE_SIZES = [32, 64, 128, 256, 512, 1024, 2048, 4096]


class VtfClampWorker(QThread):
    """Background worker that clamps VTF files to power-of-two dimensions."""

    progress = Signal(str, str)  # message, level
    progress_value = Signal(int, int)  # files done, total (drives the progress bar)
    finished = Signal(int, int, int, int)  # total, clamped, skipped, failed

    def __init__(
        self,
        files: List[Path],
        max_size: int,
        recursive: bool,
        overwrite_existing: bool,
        create_backups: bool,
        preserve_aspect: bool,
    ):
        super().__init__()
        self.files = files
        self.max_size = max_size
        self.recursive = recursive
        self.overwrite_existing = overwrite_existing
        self.create_backups = create_backups
        self.preserve_aspect = preserve_aspect

    def run(self):
        clamped = 0
        skipped = 0
        failed = 0
        total = len(self.files)

        self.progress_value.emit(0, total)
        for idx, file_path in enumerate(self.files, start=1):
            if self.isInterruptionRequested():
                self.progress.emit("Clamp cancelled by user", "WARNING")
                break

            try:
                changed, reason = self._clamp_file(file_path)
                if changed:
                    clamped += 1
                    self.progress.emit(reason, "SUCCESS")
                else:
                    skipped += 1
                    self.progress.emit(reason, "INFO")
            except Exception as exc:
                failed += 1
                self.progress.emit(f"Failed {file_path.name}: {exc}", "ERROR")
            self.progress_value.emit(idx, total)

        self.finished.emit(total, clamped, skipped, failed)

    @staticmethod
    def _floor_power_of_two(value: int) -> int:
        """Return the largest power of two less than or equal to value."""
        if value <= 1:
            return 1

        return 2 ** int(math.floor(math.log2(value)))

    @staticmethod
    def _read_header_dimensions(file_path: Path) -> Tuple[int, int]:
        """Read width and height directly from the VTF header."""
        with file_path.open("rb") as file:
            header = file.read(20)

        if len(header) < 20 or header[0:4] != b"VTF\x00":
            raise RuntimeError("Invalid VTF header")

        width, height = struct.unpack_from("<HH", header, 16)
        if width <= 0 or height <= 0:
            raise RuntimeError(f"Invalid VTF dimensions in header: {width}x{height}")

        return int(width), int(height)

    def _target_dimensions(self, width: int, height: int) -> Tuple[int, int]:
        """Calculate target power-of-two dimensions capped by max_size."""
        if width <= self.max_size and height <= self.max_size:
            return width, height

        if self.preserve_aspect:
            scale = self.max_size / max(width, height)
            scaled_width = max(1, int(round(width * scale)))
            scaled_height = max(1, int(round(height * scale)))
            return (
                min(self.max_size, self._floor_power_of_two(scaled_width)),
                min(self.max_size, self._floor_power_of_two(scaled_height)),
            )

        return (
            min(self.max_size, self._floor_power_of_two(width)),
            min(self.max_size, self._floor_power_of_two(height)),
        )

    def _clamp_file(self, file_path: Path) -> Tuple[bool, str]:
        """Clamp a single VTF file if it exceeds the configured maximum size."""
        header_width, header_height = self._read_header_dimensions(file_path)
        vtf = vtfpp.VTF(str(file_path))
        sourcepp_width = int(vtf.width)
        sourcepp_height = int(vtf.height)
        width = header_width
        height = header_height
        target_width, target_height = self._target_dimensions(width, height)

        if target_width == width and target_height == height:
            return False, f"Skipped {file_path.name} ({width}x{height} <= {self.max_size})"

        if not self.overwrite_existing:
            return False, f"Skipped {file_path.name} ({width}x{height}; overwrite disabled)"

        if self.create_backups:
            backup_path = file_path.with_suffix(file_path.suffix + ".bak")
            if not backup_path.exists():
                shutil.copy2(file_path, backup_path)

        rgba_bytes = vtf.get_image_data_as_rgba8888(0, 0, 0, 0)
        expected_size = width * height * 4
        actual_size = len(rgba_bytes)
        if actual_size != expected_size:
            sourcepp_size = sourcepp_width * sourcepp_height * 4
            if actual_size == sourcepp_size:
                width = sourcepp_width
                height = sourcepp_height
                target_width, target_height = self._target_dimensions(header_width, header_height)
            else:
                raise RuntimeError(
                    f"Decoded image size mismatch: header={header_width}x{header_height}, "
                    f"sourcepp={sourcepp_width}x{sourcepp_height}, bytes={actual_size}"
                )

        pixels = np.frombuffer(rgba_bytes, dtype=np.uint8).reshape((height, width, 4))
        image = Image.fromarray(pixels, mode="RGBA")
        image = image.resize((target_width, target_height), Image.Resampling.LANCZOS)

        output_pixels = np.asarray(image, dtype=np.uint8)
        output_pixels = np.ascontiguousarray(output_pixels)

        options = vtfpp.VTF.CreationOptions()
        options.version = 4
        options.output_format = vtf.format
        options.flags = int(vtf.flags)
        options.compute_mips = True
        options.compute_thumbnail = False
        options.compute_reflectivity = False
        options.compute_transparency_flags = False

        new_vtf = vtfpp.VTF.create(
            output_pixels.tobytes(),
            vtfpp.ImageFormat.RGBA8888,
            target_width,
            target_height,
            options,
        )
        if not new_vtf.bake_to_file(str(file_path)):
            raise RuntimeError("sourcepp failed to bake resized VTF")

        return True, f"Clamped {file_path.name}: {width}x{height} -> {target_width}x{target_height}"


class VtfClampTool(BaseTool):
    """GUI tool for clamping VTF texture sizes."""

    def __init__(self):
        super().__init__("VTF Clamp")
        self.selected_files: List[Path] = []
        self.setup_tool_ui()

    def setup_tool_ui(self):
        self.folder_picker = self.create_picker(
            "dir", "input folder", label="VTF Folder:",
            placeholder="Select folder containing .vtf files...",
            dialog_title="Select VTF Folder")
        # Picking a folder switches the tool back to folder mode.
        self.folder_picker.changed.connect(self._folder_picked)

        self.files_label = QLabel("No files selected")
        self.files_label.setStyleSheet("color: #808080;")
        files_button = QPushButton("Select Files...")
        files_button.clicked.connect(self.select_files)
        files_row = widgets.hbox(QLabel("Or select files:"), self.files_label, files_button)

        self.add_group("Input", self.folder_picker, files_row)

        self.max_size_combo = QComboBox()
        for size in MAX_TEXTURE_SIZES:
            label = f"{size}x{size} ({size // 1024}K)" if size >= 1024 else f"{size}x{size}"
            self.max_size_combo.addItem(label, size)
        self.max_size_combo.setCurrentIndex(self.max_size_combo.findData(1024))  # 1024 default
        max_row = widgets.hbox(QLabel("Clamp anything above:"), self.max_size_combo,
                               stretch_after=True)

        self.recursive_check = QCheckBox("Recursive (include subfolders)")
        self.recursive_check.setChecked(True)
        self.recursive_check.setToolTip(
            "When on, scan all subfolders of the input. When off, scan only "
            "the input folder itself."
        )

        self.preserve_aspect_check = QCheckBox("Preserve aspect ratio")
        self.preserve_aspect_check.setChecked(True)
        self.preserve_aspect_check.setToolTip("Keeps rectangular textures rectangular, e.g. 2048x1024 → 1024x512.")

        self.overwrite_check = QCheckBox("Replace existing outputs")
        self.overwrite_check.setChecked(True)
        self.overwrite_check.setToolTip(
            "Overwrite outputs that already exist in the destination folder."
        )

        self.backup_check = QCheckBox("Create backups (.bak)")
        self.backup_check.setChecked(True)
        self.backup_check.setToolTip(
            "Save the original alongside the modified copy."
        )

        self.add_group("Settings", max_row, self.recursive_check,
                       self.preserve_aspect_check, self.overwrite_check, self.backup_check)

        self.content_layout.addWidget(widgets.note_label(
            "Clamps oversized VTF textures to power-of-two dimensions for Source engine budgets.\n"
            "Examples: 4096x4096 -> 1024x1024, 2048x1024 -> 1024x512 when preserving aspect ratio."
        ))

        self.cancel_button = self.make_cancel_button()
        self.clamp_button = self.make_run_button("Process", self.start_clamp)
        self.add_action_row(self.cancel_button, self.clamp_button)
        self.content_layout.addStretch()

    def _folder_picked(self, folder: str):
        self.selected_files = []
        self.files_label.setText("No files selected")

    def select_files(self):
        files = widgets.browse_open_files(
            self, "Select VTF Files", file_filter="VTF Textures (*.vtf)",
            settings=self.settings, remember_key=f"{self.tool_name}/input folder")
        if files:
            self.selected_files = [Path(file) for file in files]
            self.folder_picker.set_path("")
            self.files_label.setText(f"{len(self.selected_files)} file(s) selected")
            self.log(f"Selected {len(self.selected_files)} VTF file(s)", "INFO")

    def _collect_files(self) -> List[Path]:
        if self.selected_files:
            return [path for path in self.selected_files if path.exists() and path.suffix.lower() == ".vtf"]

        folder = Path(self.folder_picker.path())
        if not folder.exists() or not folder.is_dir():
            return []

        pattern = "**/*.vtf" if self.recursive_check.isChecked() else "*.vtf"
        return sorted(folder.glob(pattern))

    def start_clamp(self):
        files = self._collect_files()
        if not files:
            self.log("No VTF files found. Select files or a valid VTF folder first.", "ERROR")
            return

        max_size = int(self.max_size_combo.currentData())
        self.start_worker(
            VtfClampWorker(
                files,
                max_size,
                self.recursive_check.isChecked(),
                self.overwrite_check.isChecked(),
                self.backup_check.isChecked(),
                self.preserve_aspect_check.isChecked(),
            ),
            self.on_finished,
            busy_widgets=(self.clamp_button,),
            cancel_button=self.cancel_button,
            status="Clamping VTF textures...",
        )
        self.log(f"Started VTF clamp for {len(files)} file(s) with max {max_size}x{max_size}", "INFO")

    def on_finished(self, total: int, clamped: int, skipped: int, failed: int):
        if failed > 0:
            self.log(f"Finished with errors: {clamped} clamped, {skipped} skipped, {failed} failed out of {total}", "WARNING")
            self.emit_status("Finished with errors")
        else:
            self.log(f"Finished: {clamped} clamped, {skipped} skipped out of {total}", "SUCCESS")
            self.emit_status("Done")
