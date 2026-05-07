"""
Alpha Mask Tool

Apply a black/white transparency mask to a color texture while preserving
existing transparency: White = keep original opacity, Black = fully transparent.

Outputs a new RGBA texture (PNG) with the mask applied.
"""

from typing import Optional
from pathlib import Path

import numpy as np
from PIL import Image
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QVBoxLayout, QFileDialog, QCheckBox
)

from app.tools.base_tool import BaseTool
from app.utils.image_processing import load_image, resize_to_match, extract_channel, to_uint8
from app.core.settings import Settings


class AlphaMaskWorker(QThread):
    """Background worker that applies a transparency mask to a color texture."""

    progress = Signal(str, str)  # message, level
    finished = Signal(bool, str)  # success, output_path

    def __init__(self, color_path: str, mask_path: str, output_path: str, threshold: float):
        super().__init__()
        self.color_path = color_path
        self.mask_path = mask_path
        self.output_path = output_path
        self.threshold = threshold

    def run(self):
        try:
            self.progress.emit(f"Loading color texture: {self.color_path}", "INFO")
            color = load_image(self.color_path)
            if color is None:
                raise RuntimeError("Failed to load color texture")

            h, w = color.shape[:2]

            self.progress.emit(f"Loading mask: {self.mask_path}", "INFO")
            mask = load_image(self.mask_path)
            if mask is None:
                raise RuntimeError("Failed to load mask")

            # Resize mask to match color texture if needed
            mask = resize_to_match(mask, h, w, name="mask")

            # Use luminance of mask as alpha selector; threshold to binary keep/remove
            mask_gray = self._to_grayscale(mask)
            keep = (mask_gray >= self.threshold).astype(np.float32)  # 1.0 where keep, 0.0 where transparent

            # Preserve existing alpha where keep==1, force 0 where keep==0
            orig_alpha = extract_channel(color, 3) if color.shape[2] == 4 else np.ones((h, w), dtype=np.float32)
            new_alpha = orig_alpha * keep

            # Combine channels
            rgb = color[:, :, :3]
            rgba = np.dstack([rgb, new_alpha])

            # Save as PNG
            out_path = Path(self.output_path)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            img_uint8 = to_uint8(rgba, clip=True)
            Image.fromarray(img_uint8, mode='RGBA').save(out_path)

            self.progress.emit(f"Saved output: {out_path}", "SUCCESS")
            self.finished.emit(True, str(out_path))
        except Exception as e:
            self.progress.emit(f"Mask application failed: {e}", "ERROR")
            self.finished.emit(False, "")

    @staticmethod
    def _to_grayscale(img: np.ndarray) -> np.ndarray:
        """
        Convert RGBA image to grayscale [0,1] using perceptual weights on RGB.
        Ignores alpha channel.
        """
        rgb = img[:, :, :3]
        # Perceptual luminance
        gray = 0.2126 * rgb[:, :, 0] + 0.7152 * rgb[:, :, 1] + 0.0722 * rgb[:, :, 2]
        return gray


class AlphaMaskTool(BaseTool):
    """GUI tool to apply a black/white transparency mask to a color texture."""

    def __init__(self):
        super().__init__("Alpha Mask")
        self.worker: Optional[AlphaMaskWorker] = None
        self.settings = Settings()
        self._setup_tool_ui()

    def _setup_tool_ui(self):
        # Color texture input
        color_group = QGroupBox("Color Texture")
        color_layout = QHBoxLayout()
        self.color_input = QLineEdit()
        self.color_input.setPlaceholderText("Select color texture image (PNG/TGA/JPG)...")
        color_layout.addWidget(self.color_input)
        color_browse = QPushButton("Browse...")
        color_browse.clicked.connect(self._browse_color)
        color_layout.addWidget(color_browse)
        color_group.setLayout(color_layout)
        self.content_layout.addWidget(color_group)

        # Mask input
        mask_group = QGroupBox("Transparency Mask")
        mask_layout = QHBoxLayout()
        self.mask_input = QLineEdit()
        self.mask_input.setPlaceholderText("Select black/white mask image (white=keep, black=transparent)...")
        mask_layout.addWidget(self.mask_input)
        mask_browse = QPushButton("Browse...")
        mask_browse.clicked.connect(self._browse_mask)
        mask_layout.addWidget(mask_browse)
        mask_group.setLayout(mask_layout)
        self.content_layout.addWidget(mask_group)

        # Options
        options_group = QGroupBox("Options")
        options_layout = QVBoxLayout()
        self.threshold_check = QCheckBox("Use 50% threshold (white >= 128 keeps, black < 128 removes)")
        self.threshold_check.setChecked(True)
        options_layout.addWidget(self.threshold_check)
        options_group.setLayout(options_layout)
        self.content_layout.addWidget(options_group)

        # Output
        out_group = QGroupBox("Output")
        out_layout = QHBoxLayout()
        self.output_input = QLineEdit()
        self.output_input.setPlaceholderText("Output file (.png), default: <color>_masked.png next to source")
        out_layout.addWidget(self.output_input)
        out_browse = QPushButton("Browse...")
        out_browse.clicked.connect(self._browse_output)
        out_layout.addWidget(out_browse)
        out_group.setLayout(out_layout)
        self.content_layout.addWidget(out_group)

        # Action buttons
        buttons = QHBoxLayout()
        buttons.addStretch()
        run_btn = QPushButton("Process")
        run_btn.setMinimumWidth(140)
        run_btn.clicked.connect(self._start)
        buttons.addWidget(run_btn)
        self.content_layout.addLayout(buttons)
        self.content_layout.addStretch()

    def _browse_color(self):
        start_dir = self.settings.get('alpha_mask_last_color_dir', '') or ''
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Color Texture",
            start_dir,
            "Images (*.png *.tga *.jpg *.jpeg *.bmp)"
        )
        if path:
            self.color_input.setText(path)
            self.settings.set('alpha_mask_last_color_dir', str(Path(path).parent))
            self.settings.save()
            self.log(f"Selected color: {path}", "INFO")

    def _browse_mask(self):
        start_dir = self.settings.get('alpha_mask_last_mask_dir', '') or ''
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Transparency Mask",
            start_dir,
            "Images (*.png *.tga *.jpg *.jpeg *.bmp)"
        )
        if path:
            self.mask_input.setText(path)
            self.settings.set('alpha_mask_last_mask_dir', str(Path(path).parent))
            self.settings.save()
            self.log(f"Selected mask: {path}", "INFO")

    def _browse_output(self):
        start_dir = str(Path(self.color_input.text()).parent) if self.color_input.text() else ''
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Select Output PNG",
            start_dir,
            "PNG Image (*.png)"
        )
        if path:
            if not path.lower().endswith('.png'):
                path += '.png'
            self.output_input.setText(path)
            self.log(f"Output path set: {path}", "INFO")

    def _start(self):
        color_path = self.color_input.text().strip()
        mask_path = self.mask_input.text().strip()
        if not color_path:
            self.log("Please select a color texture", "ERROR")
            return
        if not mask_path:
            self.log("Please select a transparency mask", "ERROR")
            return

        color_p = Path(color_path)
        mask_p = Path(mask_path)
        if not color_p.exists():
            self.log("Color texture does not exist", "ERROR")
            return
        if not mask_p.exists():
            self.log("Mask file does not exist", "ERROR")
            return

        # Default output path
        output_path = self.output_input.text().strip()
        if not output_path:
            output_path = str(color_p.with_name(color_p.stem + "_masked.png"))
            self.output_input.setText(output_path)

        threshold = 0.5 if self.threshold_check.isChecked() else 0.0

        self.clear_log()
        self.emit_status("Applying mask...")
        self.worker = AlphaMaskWorker(color_path, mask_path, output_path, threshold)
        self.worker.progress.connect(self.log)
        self.worker.finished.connect(self._finished)
        self.worker.start()
        self.log("Processing started...", "INFO")

    def _finished(self, success: bool, output_path: str):
        if success:
            self.log("Mask applied successfully", "SUCCESS")
            self.emit_status("Done")
        else:
            self.log("Operation failed", "ERROR")
            self.emit_status("Failed")
