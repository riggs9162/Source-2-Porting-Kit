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
from PySide6.QtWidgets import QCheckBox

from app.tools.base_tool import BaseTool
from app.utils.image_processing import load_image, resize_to_match, extract_channel, to_uint8

IMAGE_FILTER = "Images (*.png *.tga *.jpg *.jpeg *.bmp)"


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
        self._setup_tool_ui()

    def _setup_tool_ui(self):
        self.color_picker = self.create_picker(
            "open_file", "color texture",
            placeholder="Select color texture image (PNG/TGA/JPG)...",
            dialog_title="Select Color Texture", file_filter=IMAGE_FILTER)
        self.add_group("Color Texture", self.color_picker)

        self.mask_picker = self.create_picker(
            "open_file", "mask",
            placeholder="Select black/white mask image (white=keep, black=transparent)...",
            dialog_title="Select Transparency Mask", file_filter=IMAGE_FILTER)
        self.add_group("Transparency Mask", self.mask_picker)

        self.threshold_check = QCheckBox("Use 50% threshold (white >= 128 keeps, black < 128 removes)")
        self.threshold_check.setChecked(True)
        self.add_group("Options", self.threshold_check)

        self.output_picker = self.create_picker(
            "save_file", "output",
            placeholder="Output file (.png), default: <color>_masked.png next to source",
            dialog_title="Select Output PNG", file_filter="PNG Image (*.png)",
            default_suffix=".png")
        self.add_group("Output", self.output_picker)

        self.run_btn = self.make_run_button("Process", self._start, min_width=140)
        self.add_action_row(self.run_btn)
        self.content_layout.addStretch()

    def _start(self):
        color_path = self.color_picker.path()
        mask_path = self.mask_picker.path()
        if not color_path:
            self.log("Please select a color texture", "ERROR")
            return
        if not mask_path:
            self.log("Please select a transparency mask", "ERROR")
            return

        color_p = Path(color_path)
        if not color_p.exists():
            self.log("Color texture does not exist", "ERROR")
            return
        if not Path(mask_path).exists():
            self.log("Mask file does not exist", "ERROR")
            return

        # Default output path
        output_path = self.output_picker.path()
        if not output_path:
            output_path = str(color_p.with_name(color_p.stem + "_masked.png"))
            self.output_picker.set_path(output_path)

        threshold = 0.5 if self.threshold_check.isChecked() else 0.0

        self.start_worker(
            AlphaMaskWorker(color_path, mask_path, output_path, threshold),
            self._finished,
            busy_widgets=(self.run_btn,),
            status="Applying mask...",
        )
        self.log("Processing started...", "INFO")

    def _finished(self, success: bool, output_path: str):
        if success:
            self.log("Mask applied successfully", "SUCCESS")
            self.emit_status("Done")
        else:
            self.log("Operation failed", "ERROR")
            self.emit_status("Failed")
