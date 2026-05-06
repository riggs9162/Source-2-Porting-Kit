"""
Fake PBR Reverse Tool

Decodes Fake-PBR VTF outputs (`_color.vtf`, `_normal.vtf`, `_phong.vtf`,
optionally `_envmask.vtf`) back into per-channel PNGs (color, normal,
roughness, metallic). Inverts the formulas applied by FakePBRProcessor
in fake_pbr_tool.py / pbr_processing.py:

  _phong.vtf  → R = (1 - roughness)^gloss_gamma * phong_strength,  G = metallic
  _normal.vtf → RGB = tangent normal, A = phong mask (ignored on reverse —
                AO is multiplied through it and is not cleanly separable
                from the color)
  _color.vtf  → RGB has AO + metal-darkening baked in (we dump it as-is by
                default; optional metal-suppression undo is available but
                cannot recover the original AO-free albedo because AO is
                already baked into the color).
                A = opacity (translucency / alphatest), if applied

`_envmask.vtf` is also ignored — its content is redundant.

Two scan modes:
  - **By VTF filenames** — match `*_color.vtf` / `*_normal.vtf` / `*_phong.vtf`
    in the input root. Fast but breaks if VTF filenames are inconsistent.
  - **By VMT files** — for each `*.vmt`, parse `$basetexture`, `$bumpmap`,
    `$phongexponenttexture`, `$envmapmask` and resolve them to VTF files.
    The VMT stem becomes the output base name, so the resulting PNGs are
    clean even when the VTFs themselves are named oddly.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from PIL import Image
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from sourcepp import vtfpp

from app.tools.base_tool import BaseTool
from app.utils.helpers import get_config_dir


# Material entries are (output_name, source_folder_for_preserve_structure, {role: vtf_path}).
Material = Tuple[str, Path, Dict[str, Path]]


# ---------------------------------------------------------------- VTF/VMT IO


def _decode_vtf_rgba(path: Path) -> np.ndarray:
    """Decode a VTF (mip 0, frame 0, face 0, slice 0) as HxWx4 uint8 RGBA."""
    vtf = vtfpp.VTF(str(path))
    width = int(vtf.width)
    height = int(vtf.height)
    rgba_bytes = vtf.get_image_data_as_rgba8888(0, 0, 0, 0)
    expected = width * height * 4
    if len(rgba_bytes) != expected:
        raise RuntimeError(
            f"{path.name}: decoded byte count {len(rgba_bytes)} does not match "
            f"{width}x{height}x4 = {expected}"
        )
    return np.frombuffer(rgba_bytes, dtype=np.uint8).reshape((height, width, 4)).copy()


def _save_gray(arr01: np.ndarray, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pixels = np.clip(arr01, 0.0, 1.0)
    Image.fromarray((pixels * 255.0 + 0.5).astype(np.uint8), mode="L").save(out_path)


def _resize_gray(arr01: np.ndarray, target_hw: Tuple[int, int]) -> np.ndarray:
    if arr01.shape[:2] == target_hw:
        return arr01
    h, w = target_hw
    img = Image.fromarray(arr01.astype(np.float32), mode="F")
    img = img.resize((w, h), Image.Resampling.BILINEAR)
    return np.asarray(img, dtype=np.float32)


def _save_rgb(rgb_u8: np.ndarray, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb_u8, mode="RGB").save(out_path)


# ---------------------------------------------------------------- VMT parsing


_VMT_KEY_TO_ROLE = {
    "$basetexture": "color",
    "$bumpmap": "normal",
    "$phongexponenttexture": "phong",
    "$envmapmask": "envmask",
}

# Matches:    "$key" "value"   |   "$key" value   |   $key "value"   |   $key value
# `\s*` around the key tolerates trailing whitespace inside the quoted key
# (real-world VMTs sometimes have things like `"$PhongExponentTexture "`).
_VMT_KV_RE = re.compile(
    r'"?\s*(\$[A-Za-z_][A-Za-z_0-9]*)\s*"?\s+"?([^"\s\{\}]+)"?',
    flags=re.IGNORECASE,
)


def _parse_vmt_texture_refs(vmt_path: Path) -> Dict[str, str]:
    """Read a VMT and pull out the texture-path values keyed by role.

    Returns a dict like ``{"color": "models/foo/bar_color", ...}``. Comments
    (`//`) are stripped per-line and only the first value seen for a key wins,
    which mirrors how Source itself reads VMTs.
    """
    refs: Dict[str, str] = {}
    try:
        text = vmt_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return refs
    for raw_line in text.splitlines():
        line = raw_line.split("//", 1)[0].strip()
        if not line or line in ("{", "}"):
            continue
        match = _VMT_KV_RE.match(line)
        if not match:
            continue
        key = match.group(1).lower()
        role = _VMT_KEY_TO_ROLE.get(key)
        if role is None or role in refs:
            continue
        refs[role] = match.group(2)
    return refs


def _find_materials_root(start: Path) -> Optional[Path]:
    """Walk up from ``start`` looking for a ``materials`` directory."""
    for parent in [start] + list(start.parents):
        if parent.name.lower() == "materials":
            return parent
        candidate = parent / "materials"
        if candidate.is_dir():
            return candidate
    return None


def _resolve_vtf_ref(ref: str, vmt_dir: Path, input_root: Path) -> Optional[Path]:
    """Resolve a VMT texture reference (e.g. ``models/foo/bar_color``) to a file."""
    ref = ref.replace("\\", "/").strip()
    if not ref:
        return None
    # Strip any extension already present and re-add .vtf so we don't double up.
    ref_no_ext = ref[:-4] if ref.lower().endswith(".vtf") else ref
    ref_basename = Path(ref_no_ext).name + ".vtf"

    candidates = [
        vmt_dir / ref_basename,
        vmt_dir / (ref_no_ext + ".vtf"),
        input_root / (ref_no_ext + ".vtf"),
    ]
    materials_root = _find_materials_root(vmt_dir)
    if materials_root is not None:
        candidates.append(materials_root / (ref_no_ext + ".vtf"))
    for cand in candidates:
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


# ---------------------------------------------------------------- worker


class FakePBRReverseWorker(QThread):
    """Background worker that reverses Fake PBR VTFs into PNGs."""

    progress = Signal(str, str)  # message, level
    finished = Signal(int, int, int)  # processed, skipped, failed

    def __init__(
        self,
        materials: List[Material],
        output_root: Path,
        input_root: Path,
        preserve_structure: bool,
        gloss_gamma: float,
        phong_strength: float,
        metal_suppression: float,
        invert_green: bool,
        recover_albedo: bool,
        write_opacity: bool,
        overwrite: bool,
    ):
        super().__init__()
        self.materials = materials
        self.output_root = output_root
        self.input_root = input_root
        self.preserve_structure = preserve_structure
        self.gloss_gamma = max(0.01, float(gloss_gamma))
        self.phong_strength = max(1e-4, float(phong_strength))
        self.metal_suppression = float(metal_suppression)
        self.invert_green = bool(invert_green)
        self.recover_albedo = bool(recover_albedo)
        self.write_opacity = bool(write_opacity)
        self.overwrite = bool(overwrite)

    def run(self):
        processed = 0
        skipped = 0
        failed = 0
        for name, folder, paths in self.materials:
            if self.isInterruptionRequested():
                self.progress.emit("Cancelled by user", "WARNING")
                break
            try:
                wrote = self._reverse_one(name, folder, paths)
                if wrote:
                    processed += 1
                else:
                    skipped += 1
            except Exception as exc:
                failed += 1
                self.progress.emit(f"✗ {name}: {exc}", "ERROR")
        self.finished.emit(processed, skipped, failed)

    def _output_dir_for(self, folder: Path) -> Path:
        if not self.preserve_structure:
            return self.output_root
        try:
            rel = folder.relative_to(self.input_root)
        except ValueError:
            return self.output_root
        return self.output_root / rel

    def _reverse_one(self, name: str, folder: Path, paths: Dict[str, Path]) -> bool:
        out_dir = self._output_dir_for(folder)
        out_dir.mkdir(parents=True, exist_ok=True)

        targets = [out_dir / f"{name}_color.png", out_dir / f"{name}_normal.png"]
        if "phong" in paths:
            targets.extend([
                out_dir / f"{name}_roughness.png",
                out_dir / f"{name}_metallic.png",
            ])
        if not self.overwrite and all(p.exists() for p in targets):
            self.progress.emit(f"- {name}: outputs exist (skipped)", "INFO")
            return False

        color_rgba = _decode_vtf_rgba(paths["color"]) if "color" in paths else None
        normal_rgba = _decode_vtf_rgba(paths["normal"]) if "normal" in paths else None
        phong_rgba = _decode_vtf_rgba(paths["phong"]) if "phong" in paths else None

        roughness: Optional[np.ndarray] = None
        metallic: Optional[np.ndarray] = None
        if phong_rgba is not None:
            phong_r = phong_rgba[:, :, 0].astype(np.float32) / 255.0
            metal = phong_rgba[:, :, 1].astype(np.float32) / 255.0
            gloss = np.clip(phong_r / self.phong_strength, 0.0, 1.0)
            roughness = np.clip(1.0 - np.power(gloss, 1.0 / self.gloss_gamma), 0.0, 1.0)
            metallic = np.clip(metal, 0.0, 1.0)
            _save_gray(roughness, out_dir / f"{name}_roughness.png")
            _save_gray(metallic, out_dir / f"{name}_metallic.png")

        if normal_rgba is not None:
            normal_rgb = normal_rgba[:, :, :3].copy()
            if self.invert_green:
                normal_rgb[:, :, 1] = 255 - normal_rgb[:, :, 1]
            _save_rgb(normal_rgb, out_dir / f"{name}_normal.png")

        if color_rgba is not None:
            color_rgb = color_rgba[:, :, :3].astype(np.float32) / 255.0
            alpha = color_rgba[:, :, 3]
            if self.recover_albedo and metallic is not None and self.metal_suppression > 0.0:
                metal_c = _resize_gray(metallic, color_rgb.shape[:2])
                denom = np.maximum(1.0 - self.metal_suppression * metal_c, 1e-3)
                albedo = np.clip(color_rgb / denom[:, :, np.newaxis], 0.0, 1.0)
                albedo_u8 = (albedo * 255.0 + 0.5).astype(np.uint8)
            else:
                albedo_u8 = color_rgba[:, :, :3]

            _save_rgb(albedo_u8, out_dir / f"{name}_color.png")
            if self.write_opacity and alpha.min() < 255:
                _save_gray(alpha.astype(np.float32) / 255.0, out_dir / f"{name}_opacity.png")

        self.progress.emit(f"✓ {name}", "SUCCESS")
        return True


# ---------------------------------------------------------------- tool


class FakePBRReverseTool(BaseTool):
    """GUI tool for reversing Fake-PBR VTFs back into PBR-style PNGs."""

    SCAN_MODE_VTF = "By VTF filenames"
    SCAN_MODE_VMT = "By VMT files"

    SUFFIX_MAP = {
        "_color": "color",
        "_normal": "normal",
        "_phong": "phong",
        "_envmask": "envmask",
    }

    def __init__(self):
        super().__init__("Fake PBR Reverse")
        self.worker: Optional[FakePBRReverseWorker] = None
        self.scanned_materials: List[Material] = []
        self.history: List[dict] = []
        try:
            self._history_file = get_config_dir() / "fake_pbr_reverse_history.json"
        except Exception:
            self._history_file = Path(__file__).parent.parent / "config" / "fake_pbr_reverse_history.json"
        self._load_history()
        self.setup_tool_ui()
        self._refresh_history_dropdown()

    # ------------------------------------------------------------------ UI

    def setup_tool_ui(self):
        # Previous runs
        history_group = QGroupBox("Previous Runs")
        history_form = QFormLayout()
        self.history_dropdown = QComboBox()
        self.history_dropdown.addItem("-- Recent runs --")
        self.history_dropdown.currentIndexChanged.connect(self.on_history_selected)
        history_form.addRow("Select Run:", self.history_dropdown)
        history_group.setLayout(history_form)
        self.content_layout.addWidget(history_group)

        # Folders
        io_group = QGroupBox("Folders")
        io_form = QFormLayout()
        self.input_root = QLineEdit()
        in_btn = QPushButton("Browse...")
        in_btn.clicked.connect(lambda: self._browse_dir_into(self.input_root))
        io_form.addRow("Input Root:", self._row(self.input_root, in_btn))

        self.output_root = QLineEdit()
        out_btn = QPushButton("Browse...")
        out_btn.clicked.connect(lambda: self._browse_dir_into(self.output_root))
        io_form.addRow("Output Root:", self._row(self.output_root, out_btn))
        io_group.setLayout(io_form)
        self.content_layout.addWidget(io_group)

        # Options
        opt_group = QGroupBox("Options")
        opt_form = QFormLayout()

        self.scan_mode = QComboBox()
        self.scan_mode.addItems([self.SCAN_MODE_VTF, self.SCAN_MODE_VMT])
        self.scan_mode.currentIndexChanged.connect(lambda _i: self._on_scan_mode_changed())
        self.scan_mode.setToolTip(
            "By VTF filenames: pair *_color.vtf / *_normal.vtf / *_phong.vtf "
            "by stem. Fast but unreliable when VTFs aren't consistently named.\n"
            "By VMT files: read each .vmt's $basetexture / $bumpmap / "
            "$phongexponenttexture and use the VMT name as the output base. "
            "Robust against messy VTF filenames."
        )
        opt_form.addRow("Scan Mode:", self.scan_mode)

        self.recursive = QCheckBox("Scan recursively")
        self.recursive.setChecked(True)
        opt_form.addRow("", self.recursive)

        self.preserve_structure = QCheckBox("Preserve folder structure")
        self.preserve_structure.setChecked(True)
        opt_form.addRow("", self.preserve_structure)

        self.invert_green = QCheckBox("Invert normal green channel (Y-flip)")
        self.invert_green.setChecked(False)
        self.invert_green.setToolTip(
            "Enable if the source PBR pipeline used OpenGL (+Y up) normals "
            "and you need DirectX (-Y up) on output, or vice versa."
        )
        opt_form.addRow("", self.invert_green)

        self.recover_albedo = QCheckBox("Attempt metal-suppression undo on color")
        self.recover_albedo.setChecked(False)
        self.recover_albedo.setToolTip(
            "Divides the encoded color by (1 - metal_diffuse_suppression * metal) "
            "to back out the suppression Fake PBR applied to metal pixels. "
            "Off by default: AO is also baked into the color so this only "
            "partially recovers the original albedo and can over-brighten "
            "metal areas."
        )
        opt_form.addRow("", self.recover_albedo)

        self.write_opacity = QCheckBox("Write opacity PNG when color alpha is non-trivial")
        self.write_opacity.setChecked(True)
        opt_form.addRow("", self.write_opacity)

        self.overwrite = QCheckBox("Overwrite existing PNGs")
        self.overwrite.setChecked(False)
        opt_form.addRow("", self.overwrite)

        self.gloss_gamma_slider, gamma_widget, self.gloss_gamma_label = self._make_slider(
            10, 40, 20, 10.0, "{:.2f}",
            "Gloss gamma originally used when encoding (default 2.00). "
            "Used to invert (1 - roughness)^gamma when recovering roughness."
        )
        opt_form.addRow("Gloss Gamma:", gamma_widget)

        self.phong_strength_slider, phong_widget, self.phong_strength_label = self._make_slider(
            1, 200, 100, 100.0, "{:.2f}",
            "Phong strength originally used when encoding. The phong R channel "
            "is divided by this to invert the scaling.\n"
            "Use 1.00 (default) for legacy/in-the-wild Fake PBR VTFs — they have "
            "no strength scaling.\n"
            "Use 0.50 for VTFs you just made with this app's current Fake PBR "
            "default (which halves the phong R channel)."
        )
        opt_form.addRow("Phong Strength:", phong_widget)

        self.metal_supp_slider, metal_widget, self.metal_supp_label = self._make_slider(
            0, 100, 70, 100.0, "{:.2f}",
            "Metal Diffuse Suppression originally used when encoding (default 0.70). "
            "Only applied when 'Attempt metal-suppression undo on color' is enabled."
        )
        opt_form.addRow("Metal Diffuse Suppression:", metal_widget)

        opt_group.setLayout(opt_form)
        self.content_layout.addWidget(opt_group)

        # Scan results table — populated by Scan, consumed by Reverse.
        # Columns adapt to the scan mode (VMT scans show the VMT path).
        self.results_table = QTableWidget(0, 7)
        self._set_results_headers(self.SCAN_MODE_VTF)
        header = self.results_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.results_table.setMinimumHeight(180)
        self.content_layout.addWidget(self.results_table)

        # Buttons: Scan → enables Reverse; Cancel
        btn_row = QHBoxLayout()
        self.scan_button = QPushButton("Scan")
        self.scan_button.setMinimumHeight(36)
        self.scan_button.clicked.connect(self.scan)
        self.run_button = QPushButton("Reverse to PNGs")
        self.run_button.setMinimumHeight(36)
        self.run_button.setEnabled(False)
        self.run_button.clicked.connect(self.start_reverse)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_worker)
        btn_row.addWidget(self.scan_button)
        btn_row.addStretch()
        btn_row.addWidget(self.cancel_button)
        btn_row.addWidget(self.run_button)
        self.content_layout.addLayout(btn_row)
        self.content_layout.addStretch()

    def _row(self, line_edit: QLineEdit, button: QPushButton) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(line_edit)
        row.addWidget(button)
        return w

    def _make_slider(self, lo: int, hi: int, value: int, divisor: float, fmt: str, tooltip: str):
        slider = QSlider(Qt.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(value)
        slider.setToolTip(tooltip)
        label = QLabel(fmt.format(value / divisor))
        slider.valueChanged.connect(lambda v: label.setText(fmt.format(v / divisor)))
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(slider)
        layout.addWidget(label)
        return slider, widget, label

    def _browse_dir_into(self, line_edit: QLineEdit):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder", "")
        if folder:
            line_edit.setText(folder)

    # ------------------------------------------------------------------ results table

    def _set_results_headers(self, mode: str) -> None:
        """Set column headers — VMT mode shows the VMT path instead of the VTF stem."""
        first_col = "VMT" if mode == self.SCAN_MODE_VMT else "Material"
        self.results_table.setHorizontalHeaderLabels(
            [first_col, "Color", "Normal", "Phong", "Envmask", "Folder", "Output Name"]
        )

    def _populate_results_table(self, materials: List[Material], mode: str) -> None:
        self._set_results_headers(mode)
        self.results_table.setRowCount(0)
        for output_name, folder, paths in materials:
            row = self.results_table.rowCount()
            self.results_table.insertRow(row)
            if mode == self.SCAN_MODE_VMT:
                vmt_label = f"{output_name}.vmt"
            else:
                vmt_label = output_name
            first_item = QTableWidgetItem(vmt_label)
            first_item.setToolTip(str(folder))
            self.results_table.setItem(row, 0, first_item)
            for col, role in enumerate(("color", "normal", "phong", "envmask"), start=1):
                p = paths.get(role)
                cell = QTableWidgetItem(p.name if p is not None else "—")
                if p is not None:
                    cell.setToolTip(str(p))
                self.results_table.setItem(row, col, cell)
            folder_item = QTableWidgetItem(str(folder))
            folder_item.setToolTip(str(folder))
            self.results_table.setItem(row, 5, folder_item)
            self.results_table.setItem(row, 6, QTableWidgetItem(output_name))

    def _on_scan_mode_changed(self) -> None:
        self._set_results_headers(self.scan_mode.currentText())
        # Selecting a new mode invalidates the previous scan results.
        self.scanned_materials = []
        self.results_table.setRowCount(0)
        self.run_button.setEnabled(False)

    # ------------------------------------------------------------------ scan

    def _scan_by_vtf(self, root: Path, recursive: bool) -> List[Material]:
        groups: Dict[Tuple[str, str], Dict[str, Path]] = {}
        iterator = root.rglob("*.vtf") if recursive else root.glob("*.vtf")
        for vtf_path in iterator:
            if not vtf_path.is_file():
                continue
            stem = vtf_path.stem
            for suffix, role in self.SUFFIX_MAP.items():
                if stem.endswith(suffix):
                    name = stem[: -len(suffix)]
                    key = (str(vtf_path.parent), name)
                    groups.setdefault(key, {})[role] = vtf_path
                    break

        materials: List[Material] = []
        for (folder, name), paths in sorted(groups.items()):
            if not any(role in paths for role in ("color", "normal", "phong")):
                continue
            materials.append((name, Path(folder), paths))
        return materials

    def _scan_by_vmt(self, root: Path, recursive: bool) -> List[Material]:
        materials: List[Material] = []
        iterator = root.rglob("*.vmt") if recursive else root.glob("*.vmt")
        for vmt_path in sorted(iterator):
            if not vmt_path.is_file():
                continue
            refs = _parse_vmt_texture_refs(vmt_path)
            if not refs:
                self.log(f"- {vmt_path.name}: no texture refs found", "INFO")
                continue
            resolved: Dict[str, Path] = {}
            missing: List[str] = []
            for role, ref in refs.items():
                vtf = _resolve_vtf_ref(ref, vmt_path.parent, root)
                if vtf is not None:
                    resolved[role] = vtf
                else:
                    missing.append(f"{role}={ref}")
            if missing:
                self.log(
                    f"- {vmt_path.name}: unresolved {', '.join(missing)}",
                    "WARNING",
                )
            if not any(r in resolved for r in ("color", "normal", "phong")):
                continue
            materials.append((vmt_path.stem, vmt_path.parent, resolved))
        return materials

    def scan(self):
        in_text = self.input_root.text().strip()
        if not in_text:
            self.log("Input root is required", "ERROR")
            return
        in_root = Path(in_text)
        if not in_root.is_dir():
            self.log(f"Input root does not exist: {in_root}", "ERROR")
            return

        self.clear_log()
        mode = self.scan_mode.currentText()
        recursive = self.recursive.isChecked()
        self.emit_status(f"Scanning ({mode})...")
        self.log(f"Scanning '{in_root}' [{mode}, recursive={recursive}]", "INFO")

        if mode == self.SCAN_MODE_VMT:
            materials = self._scan_by_vmt(in_root, recursive)
        else:
            materials = self._scan_by_vtf(in_root, recursive)

        self.scanned_materials = materials
        self._populate_results_table(materials, mode)
        if not materials:
            self.log(
                "No material sets found "
                "(VTF mode needs *_color/_normal/_phong.vtf; "
                "VMT mode needs .vmt files referencing those textures).",
                "WARNING",
            )
            self.run_button.setEnabled(False)
            self.emit_status("Scan: 0 sets")
            return

        self.log(f"Scan complete: {len(materials)} material set(s) ready to reverse", "INFO")
        self.run_button.setEnabled(True)
        self.emit_status(f"Scan: {len(materials)} sets")

    # ------------------------------------------------------------------ run

    def start_reverse(self):
        if not self.scanned_materials:
            self.log("Nothing to reverse — run Scan first.", "ERROR")
            return
        out_text = self.output_root.text().strip()
        if not out_text:
            self.log("Output root is required", "ERROR")
            return
        in_root = Path(self.input_root.text().strip())
        out_root = Path(out_text)

        self._save_current_run_to_history()

        self.scan_button.setEnabled(False)
        self.run_button.setEnabled(False)
        self.cancel_button.setEnabled(True)

        self.worker = FakePBRReverseWorker(
            materials=self.scanned_materials,
            output_root=out_root,
            input_root=in_root,
            preserve_structure=self.preserve_structure.isChecked(),
            gloss_gamma=self.gloss_gamma_slider.value() / 10.0,
            phong_strength=self.phong_strength_slider.value() / 100.0,
            metal_suppression=self.metal_supp_slider.value() / 100.0,
            invert_green=self.invert_green.isChecked(),
            recover_albedo=self.recover_albedo.isChecked(),
            write_opacity=self.write_opacity.isChecked(),
            overwrite=self.overwrite.isChecked(),
        )
        self.worker.progress.connect(self.log)
        self.worker.finished.connect(self.on_finished)
        self.worker.start()

    def cancel_worker(self):
        if self.worker and self.worker.isRunning():
            self.worker.requestInterruption()
            self.log("Cancellation requested...", "WARNING")

    def on_finished(self, processed: int, skipped: int, failed: int):
        self.scan_button.setEnabled(True)
        self.run_button.setEnabled(bool(self.scanned_materials))
        self.cancel_button.setEnabled(False)
        if failed:
            self.log(
                f"Finished with errors: {processed} processed, {skipped} skipped, {failed} failed",
                "WARNING",
            )
            self.emit_status("Finished with errors")
        else:
            self.log(f"Finished: {processed} processed, {skipped} skipped", "SUCCESS")
            self.emit_status("Done")

    # ------------------------------------------------------------------ history

    def _load_history(self):
        try:
            if self._history_file.exists():
                with open(self._history_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self.history = data
        except Exception:
            self.history = []

    def _save_history_file(self):
        try:
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._history_file, "w", encoding="utf-8") as f:
                json.dump(self.history, f, indent=2)
        except Exception:
            pass

    def _refresh_history_dropdown(self):
        if not hasattr(self, "history_dropdown"):
            return
        self.history_dropdown.blockSignals(True)
        self.history_dropdown.clear()
        self.history_dropdown.addItem("-- Recent runs --")
        for entry in self.history:
            ts = entry.get("timestamp", "")
            label = entry.get("label") or entry.get("input_root") or entry.get("output_root") or ts
            self.history_dropdown.addItem(f"{ts} — {label}" if ts else label)
        self.history_dropdown.blockSignals(False)

    def _make_history_entry(self) -> dict:
        return {
            "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "label": Path(self.input_root.text().strip()).name or self.output_root.text().strip(),
            "input_root": self.input_root.text().strip(),
            "output_root": self.output_root.text().strip(),
            "scan_mode": self.scan_mode.currentText(),
            "recursive": self.recursive.isChecked(),
            "preserve_structure": self.preserve_structure.isChecked(),
            "invert_green": self.invert_green.isChecked(),
            "recover_albedo": self.recover_albedo.isChecked(),
            "write_opacity": self.write_opacity.isChecked(),
            "overwrite": self.overwrite.isChecked(),
            "gloss_gamma": self.gloss_gamma_slider.value() / 10.0,
            "phong_strength": self.phong_strength_slider.value() / 100.0,
            "metal_suppression": self.metal_supp_slider.value() / 100.0,
        }

    def _save_current_run_to_history(self):
        entry = self._make_history_entry()
        if not entry.get("input_root") and not entry.get("output_root"):
            return

        compare_keys = [
            "input_root", "output_root", "scan_mode", "recursive",
            "preserve_structure", "invert_green", "recover_albedo",
            "write_opacity", "overwrite", "gloss_gamma", "phong_strength",
            "metal_suppression",
        ]

        def _same(a: dict, b: dict) -> bool:
            return all(a.get(k) == b.get(k) for k in compare_keys)

        if self.history and _same(self.history[0], entry):
            return
        self.history = [h for h in self.history if not _same(h, entry)]
        self.history.insert(0, entry)
        self.history = self.history[:20]
        self._save_history_file()
        self._refresh_history_dropdown()

    def on_history_selected(self, index: int):
        if index <= 0:
            return
        try:
            entry = self.history[index - 1]
        except Exception:
            return

        self.input_root.setText(entry.get("input_root") or "")
        self.output_root.setText(entry.get("output_root") or "")
        mode = entry.get("scan_mode") or self.SCAN_MODE_VTF
        idx = self.scan_mode.findText(mode)
        if idx >= 0:
            self.scan_mode.setCurrentIndex(idx)
        self.recursive.setChecked(bool(entry.get("recursive", True)))
        self.preserve_structure.setChecked(bool(entry.get("preserve_structure", True)))
        self.invert_green.setChecked(bool(entry.get("invert_green", False)))
        self.recover_albedo.setChecked(bool(entry.get("recover_albedo", False)))
        self.write_opacity.setChecked(bool(entry.get("write_opacity", True)))
        self.overwrite.setChecked(bool(entry.get("overwrite", False)))
        try:
            self.gloss_gamma_slider.setValue(int(float(entry.get("gloss_gamma", 2.0)) * 10))
        except (TypeError, ValueError):
            pass
        try:
            self.phong_strength_slider.setValue(int(float(entry.get("phong_strength", 1.0)) * 100))
        except (TypeError, ValueError):
            pass
        try:
            self.metal_supp_slider.setValue(int(float(entry.get("metal_suppression", 0.7)) * 100))
        except (TypeError, ValueError):
            pass
        # Selecting a previous run resets the scan state; user can re-scan.
        self.scanned_materials = []
        self.run_button.setEnabled(False)
