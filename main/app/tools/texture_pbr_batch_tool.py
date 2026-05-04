"""
Texture PBR Batch Tool - Parse Source 2 texture folders into Fake PBR or Exo PBR sets

This tool scans a root folder recursively for PNG textures, groups them by a
sanitized base asset name, resolves texture roles via keyword matching, and
converts each asset into either Fake PBR or Exo PBR outputs.

Keyword rules (case-insensitive, tokenized by separators _ - . space):
- ORM packed maps are detected FIRST if any token == "orm".
  Files claimed as ORM are excluded from normal/emissive matching.
- Emissive keywords: emissive, emit, emission, illum, illumin, glow
- Normal keywords: normal, nrm, nor
- Color/Albedo keywords: color, albedo, diffuse, basecolor, base_color, base

Asset key derivation removes role tokens and common extras (psd, tga, png, etc.).
Ambiguous matches are warned and skipped for that role.
"""

from __future__ import annotations

import os
import re
import shutil
import json
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFileDialog,
    QLineEdit, QGroupBox, QDoubleSpinBox, QCheckBox,
    QProgressBar, QFormLayout, QWidget, QComboBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView
)
from PySide6.QtCore import Qt, QThread, Signal

from .base_tool import BaseTool
from .fake_pbr_tool import FakePBRProcessor, ProcessingOptions, PBRInputs
from .exo_pbr_tool import ExoPBRProcessor, ExoPBROptions, ExoPBRInputs
from ..utils.image_processing import load_image, to_uint8
from ..utils.helpers import get_config_dir


@dataclass
class TextureFile:
    path: Path
    tokens: List[str]
    tokens_original: List[str]
    base_key: str
    pretty_name: str

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def directory(self) -> Path:
        return self.path.parent


@dataclass
class AssetGroup:
    key: str
    pretty_name: str
    base_dir: Path
    files: List[TextureFile] = field(default_factory=list)
    resolved: Dict[str, Optional[TextureFile]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


class TextureScanner:
    """Scan folders and build asset groups based on sanitized base keys."""

    IMAGE_EXTS = {".png"}

    ROLE_TOKENS = {
        "color", "albedo", "diffuse", "basecolor", "base", "normal", "nrm", "nor",
        "ao", "rough", "roughness", "metal", "metallic", "metalness",
        "emissive", "emit", "emission", "illum", "illumin", "glow",
        "orm"
    }

    EXTRA_TOKENS = {"psd", "tga", "png", "jpg", "jpeg", "tif", "tiff"}

    def __init__(self, root: Path):
        self.root = Path(root)

    @staticmethod
    def _tokenize(stem: str) -> Tuple[List[str], List[str]]:
        original_tokens = [t for t in re.split(r"[\s._-]+", stem) if t]
        lower_tokens = [t.lower() for t in original_tokens]
        return lower_tokens, original_tokens

    def _derive_key(self, stem: str) -> Tuple[str, str]:
        lower_tokens, original_tokens = self._tokenize(stem)
        filtered = []
        filtered_original = []
        for t_lower, t_orig in zip(lower_tokens, original_tokens):
            if t_lower in self.ROLE_TOKENS or t_lower in self.EXTRA_TOKENS:
                continue
            filtered.append(t_lower)
            filtered_original.append(t_orig)
        if not filtered:
            return stem.lower(), stem
        return "_".join(filtered), "_".join(filtered_original)

    def scan(self) -> Dict[Tuple[Path, str], AssetGroup]:
        groups: Dict[Tuple[Path, str], AssetGroup] = {}
        for base, _, files in os.walk(self.root):
            base_path = Path(base)
            for fn in files:
                ext = Path(fn).suffix.lower()
                if ext not in self.IMAGE_EXTS:
                    continue
                path = base_path / fn
                stem = path.stem
                base_key, pretty = self._derive_key(stem)
                lower_tokens, original_tokens = self._tokenize(stem)

                group_id = (base_path, base_key)
                group = groups.get(group_id)
                if group is None:
                    group = AssetGroup(key=base_key, pretty_name=pretty, base_dir=base_path)
                    groups[group_id] = group

                group.files.append(TextureFile(
                    path=path,
                    tokens=lower_tokens,
                    tokens_original=original_tokens,
                    base_key=base_key,
                    pretty_name=pretty
                ))
        return groups


class RoleResolver:
    """Resolve role assignments with ORM-first logic and conflict handling."""

    ORM_TOKENS = {"orm"}
    EMISSIVE_TOKENS = {"emissive", "emit", "emission", "illum", "illumin", "glow"}
    NORMAL_TOKENS = {"normal", "nrm", "nor"}
    COLOR_TOKENS = {"color", "albedo", "diffuse", "basecolor", "base_color", "base"}

    WEAK_TOKENS = {"base"}

    ROLE_ORDER = ["orm", "emissive", "normal", "color"]

    def resolve(self, group: AssetGroup, include_emissive: bool) -> AssetGroup:
        claimed: set[Path] = set()
        orm_file = self._select_best(group.files, self.ORM_TOKENS, None, "orm")
        if orm_file:
            group.resolved["orm"] = orm_file
            claimed.add(orm_file.path)
        else:
            group.resolved["orm"] = None

        orm_dir = orm_file.directory if orm_file else None

        for role in self.ROLE_ORDER:
            if role == "orm":
                continue
            if role == "emissive" and not include_emissive:
                group.resolved["emissive"] = None
                continue
            tokens = self._tokens_for_role(role)
            candidates = [f for f in group.files if f.path not in claimed]
            selected = self._select_best(candidates, tokens, orm_dir, role, group)
            group.resolved[role] = selected
            if selected:
                claimed.add(selected.path)
        return group

    @staticmethod
    def _tokens_for_role(role: str) -> set[str]:
        if role == "emissive":
            return RoleResolver.EMISSIVE_TOKENS
        if role == "normal":
            return RoleResolver.NORMAL_TOKENS
        if role == "color":
            return RoleResolver.COLOR_TOKENS
        return RoleResolver.ORM_TOKENS

    def _select_best(
        self,
        candidates: List[TextureFile],
        tokens: set[str],
        orm_dir: Optional[Path],
        role: str,
        group: Optional[AssetGroup] = None
    ) -> Optional[TextureFile]:
        scored: List[Tuple[float, TextureFile]] = []
        for f in candidates:
            token_matches = tokens.intersection(set(f.tokens))
            if not token_matches:
                continue
            score = 0.0
            for match in token_matches:
                if match in self.WEAK_TOKENS:
                    score = max(score, 50.0)
                else:
                    score = max(score, 100.0)
            if orm_dir and f.directory == orm_dir:
                score += 10.0
            score -= len(f.stem) * 0.01
            scored.append((score, f))

        if not scored:
            return None

        scored.sort(key=lambda x: x[0], reverse=True)
        best_score = scored[0][0]
        best = [f for s, f in scored if abs(s - best_score) < 1e-6]
        if len(best) > 1 and group is not None:
            names = ", ".join([b.path.name for b in best[:5]])
            group.warnings.append(f"Ambiguous {role}: {names}")
            return None
        return scored[0][1]


class Converter:
    """Convert resolved groups into Fake PBR or Exo PBR outputs."""

    def __init__(
        self,
        mode: str,
        include_emissive: bool,
        overwrite: bool,
        material_path: str,
        generate_mipmaps: bool
    ):
        self.mode = mode
        self.include_emissive = include_emissive
        self.overwrite = overwrite
        self.material_path = material_path
        self.generate_mipmaps = generate_mipmaps

    @staticmethod
    def _save_channel(channel: np.ndarray, out_path: Path) -> None:
        img = Image.fromarray((channel * 255.0).astype(np.uint8), mode="L")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path)

    def _split_orm(self, orm_path: Path, temp_dir: Path) -> Tuple[Optional[Path], Optional[Path], Optional[Path]]:
        data = load_image(str(orm_path))
        if data is None or data.ndim < 3 or data.shape[2] < 3:
            return None, None, None
        ao = np.clip(data[:, :, 0], 0.0, 1.0)
        rough = np.clip(data[:, :, 1], 0.0, 1.0)
        metal = np.clip(data[:, :, 2], 0.0, 1.0)
        ao_path = temp_dir / f"{orm_path.stem}_ao.png"
        rough_path = temp_dir / f"{orm_path.stem}_rough.png"
        metal_path = temp_dir / f"{orm_path.stem}_metal.png"
        self._save_channel(ao, ao_path)
        self._save_channel(rough, rough_path)
        self._save_channel(metal, metal_path)
        return ao_path, rough_path, metal_path

    def _expected_outputs(self, output_dir: Path, name: str, has_emissive: bool) -> List[Path]:
        if self.mode == "Fake PBR":
            return [
                output_dir / f"{name}_color.vtf",
                output_dir / f"{name}_normal.vtf",
                output_dir / f"{name}_phong.vtf",
                output_dir / f"{name}_envmask.vtf",
                output_dir / f"{name}.vmt",
            ]
        outputs = [
            output_dir / f"{name}_base.vtf",
            output_dir / f"{name}_arm.vtf",
            output_dir / f"{name}_normal.vtf",
            output_dir / f"{name}.vmt",
        ]
        if has_emissive:
            outputs.append(output_dir / f"{name}_emissive.vtf")
        return outputs

    def convert(self, group: AssetGroup, output_dir: Path) -> Tuple[bool, str]:
        name = group.pretty_name or group.key
        color = group.resolved.get("color")
        normal = group.resolved.get("normal")
        orm = group.resolved.get("orm")
        emissive = group.resolved.get("emissive") if self.include_emissive else None

        if color is None or normal is None:
            return False, "Missing required color/normal"

        output_dir.mkdir(parents=True, exist_ok=True)
        if not self.overwrite:
            expected = self._expected_outputs(output_dir, name, emissive is not None)
            if any(p.exists() for p in expected):
                return False, "Outputs already exist"

        temp_dir = output_dir / "__tmp_orm"
        ao_path = rough_path = metal_path = None
        if orm is not None:
            ao_path, rough_path, metal_path = self._split_orm(orm.path, temp_dir)
        try:
            if self.mode == "Fake PBR":
                options = ProcessingOptions(generate_mipmaps=self.generate_mipmaps)
                processor = FakePBRProcessor(options)
                inputs = PBRInputs(
                    color=str(color.path),
                    normal=str(normal.path),
                    ao=str(ao_path) if ao_path else None,
                    roughness=str(rough_path) if rough_path else None,
                    metallic=str(metal_path) if metal_path else None
                )
                try:
                    success, msg = processor.process_material(inputs, str(output_dir), name, self.material_path)
                finally:
                    processor.shutdown()
                return success, msg

            options = ExoPBROptions(generate_mipmaps=self.generate_mipmaps)
            processor = ExoPBRProcessor(options)
            inputs = ExoPBRInputs(
                color=str(color.path),
                normal=str(normal.path),
                ao=str(ao_path) if ao_path else None,
                roughness=str(rough_path) if rough_path else None,
                metallic=str(metal_path) if metal_path else None,
                selfillum=str(emissive.path) if emissive else None
            )
            try:
                success, msg = processor.process_material(inputs, str(output_dir), name, self.material_path)
            finally:
                processor.shutdown()
            return success, msg
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir, ignore_errors=True)


class BatchRunner(QThread):
    progress = Signal(int, int, str)
    finished = Signal(bool, str)

    def __init__(
        self,
        groups: List[AssetGroup],
        input_root: Path,
        output_root: Path,
        preserve_folders: bool,
        mode: str,
        include_emissive: bool,
        overwrite: bool,
        material_path: str,
        generate_mipmaps: bool
    ):
        super().__init__()
        self.groups = groups
        self.input_root = input_root
        self.output_root = output_root
        self.preserve_folders = preserve_folders
        self.mode = mode
        self.include_emissive = include_emissive
        self.overwrite = overwrite
        self.material_path = material_path
        self.generate_mipmaps = generate_mipmaps

    def _output_dir_for(self, group: AssetGroup) -> Path:
        if self.preserve_folders:
            rel = group.base_dir.relative_to(self.input_root)
            return self.output_root / rel
        return self.output_root

    def run(self):
        total = len(self.groups)
        if total == 0:
            self.finished.emit(False, "No assets to process")
            return

        converter = Converter(
            self.mode,
            self.include_emissive,
            self.overwrite,
            self.material_path,
            self.generate_mipmaps
        )
        ok_count = 0
        for idx, group in enumerate(self.groups, start=1):
            if self.isInterruptionRequested():
                self.finished.emit(False, f"Cancelled after {ok_count}/{total}")
                return
            out_dir = self._output_dir_for(group)
            success, msg = converter.convert(group, out_dir)
            name = group.pretty_name or group.key
            if success:
                ok_count += 1
                self.progress.emit(idx, total, f"✓ {name} done")
            else:
                self.progress.emit(idx, total, f"✗ {name}: {msg}")
        self.finished.emit(True, f"Processed {ok_count}/{total} assets")


class TexturePBRBatchTool(BaseTool):
    """GUI tool for batch converting texture folders into Fake/Exo PBR outputs."""

    def __init__(self):
        super().__init__("Texture PBR Batch")
        self.thread: Optional[BatchRunner] = None
        self.scanned_groups: List[AssetGroup] = []
        self.history: List[dict] = []
        try:
            self._history_file = get_config_dir() / "texture_pbr_batch_history.json"
        except Exception:
            self._history_file = Path(__file__).parent.parent / "config" / "texture_pbr_batch_history.json"
        self._load_history()
        self.setup_content()

    def setup_content(self):
        layout = QVBoxLayout()
        self.content_layout.addLayout(layout)

        # Previous runs
        history_group = QGroupBox("Previous Runs")
        history_form = QFormLayout()
        self.history_dropdown = QComboBox()
        self.history_dropdown.addItem("-- Recent runs --")
        self.history_dropdown.currentIndexChanged.connect(self.on_history_selected)
        history_form.addRow("Select Run:", self.history_dropdown)
        history_group.setLayout(history_form)
        layout.addWidget(history_group)

        self._refresh_history_dropdown()

        # Input/Output
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
        layout.addWidget(io_group)

        # Options
        opt_group = QGroupBox("Options")
        opt_form = QFormLayout()

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Fake PBR", "Exo PBR"])
        opt_form.addRow("Mode:", self.mode_combo)

        self.preserve_structure = QCheckBox("Preserve folder structure")
        self.preserve_structure.setChecked(True)
        opt_form.addRow("", self.preserve_structure)

        self.overwrite = QCheckBox("Overwrite existing outputs")
        self.overwrite.setChecked(False)
        opt_form.addRow("", self.overwrite)

        self.include_emissive = QCheckBox("Include emissive if present")
        self.include_emissive.setChecked(True)
        opt_form.addRow("", self.include_emissive)

        self.generate_mipmaps = QCheckBox("Generate Mipmaps")
        self.generate_mipmaps.setChecked(True)
        opt_form.addRow("", self.generate_mipmaps)

        self.material_path = QLineEdit()
        self.material_path.setText("models/ports")
        self.material_path.setPlaceholderText("models/ports (Fake) or exopbr (Exo)")
        opt_form.addRow("Material Path:", self.material_path)

        opt_group.setLayout(opt_form)
        layout.addWidget(opt_group)

        # Results table
        self.results_table = QTableWidget(0, 7)
        self.results_table.setHorizontalHeaderLabels([
            "Include", "Asset", "Color", "Normal", "ORM", "Emissive", "Warnings"
        ])
        header = self.results_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        layout.addWidget(self.results_table)

        # Progress
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        # Buttons
        btn_row = QHBoxLayout()
        self.scan_btn = QPushButton("Scan")
        self.scan_btn.clicked.connect(self.scan)
        self.convert_btn = QPushButton("Convert")
        self.convert_btn.setEnabled(False)
        self.convert_btn.clicked.connect(self.convert)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel)
        btn_row.addWidget(self.scan_btn)
        btn_row.addStretch()
        btn_row.addWidget(self.cancel_btn)
        btn_row.addWidget(self.convert_btn)
        layout.addLayout(btn_row)

        layout.addStretch()

    def _row(self, line_edit: QLineEdit, button: QPushButton) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(line_edit)
        row.addWidget(button)
        return w

    def _browse_dir_into(self, line_edit: QLineEdit):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder", "")
        if folder:
            line_edit.setText(folder)

    def scan(self):
        root = self.input_root.text().strip()
        out_root = self.output_root.text().strip()
        if not root:
            self.log("Input root is required", "ERROR")
            return
        if not out_root:
            self.log("Output root is required", "ERROR")
            return

        self.clear_log()
        self.log("Scanning for textures...", "INFO")

        scanner = TextureScanner(Path(root))
        groups = scanner.scan()

        resolver = RoleResolver()
        resolved_groups: List[AssetGroup] = []
        for group in groups.values():
            resolver.resolve(group, include_emissive=self.include_emissive.isChecked())
            resolved_groups.append(group)

        self._populate_table(resolved_groups)
        self.scanned_groups = resolved_groups
        self.convert_btn.setEnabled(len(resolved_groups) > 0)
        self.log(f"Scan complete: {len(resolved_groups)} assets detected", "INFO")

    def _populate_table(self, groups: List[AssetGroup]):
        self.results_table.setRowCount(0)
        for group in sorted(groups, key=lambda g: (str(g.base_dir), g.pretty_name)):
            row = self.results_table.rowCount()
            self.results_table.insertRow(row)

            include_item = QTableWidgetItem()
            include_item.setCheckState(Qt.Checked)
            include_item.setFlags(include_item.flags() | Qt.ItemIsUserCheckable)
            self.results_table.setItem(row, 0, include_item)

            name_item = QTableWidgetItem(group.pretty_name)
            self.results_table.setItem(row, 1, name_item)

            def _set_cell(col: int, role: str):
                path = group.resolved.get(role)
                txt = "Yes" if path else "No"
                item = QTableWidgetItem(txt)
                if path:
                    item.setToolTip(str(path.path))
                self.results_table.setItem(row, col, item)

            _set_cell(2, "color")
            _set_cell(3, "normal")
            _set_cell(4, "orm")
            _set_cell(5, "emissive")

            warn_text = "; ".join(group.warnings)
            warn_item = QTableWidgetItem(warn_text)
            self.results_table.setItem(row, 6, warn_item)

    def _selected_groups(self) -> List[AssetGroup]:
        selected = []
        for row in range(self.results_table.rowCount()):
            if self.results_table.item(row, 0).checkState() != Qt.Checked:
                continue
            selected.append(self.scanned_groups[row])
        return selected

    def convert(self):
        root = self.input_root.text().strip()
        out_root = self.output_root.text().strip()
        if not root or not out_root:
            self.log("Input/Output roots are required", "ERROR")
            return

        groups = self._selected_groups()
        if not groups:
            self.log("No assets selected", "WARNING")
            return

        self.convert_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress.setVisible(True)
        self.progress.setRange(0, len(groups))
        self.progress.setValue(0)

        material_path = self.material_path.text().strip()
        if not material_path:
            material_path = "exopbr" if self.mode_combo.currentText() == "Exo PBR" else "models/ports"

        self.thread = BatchRunner(
            groups=groups,
            input_root=Path(root),
            output_root=Path(out_root),
            preserve_folders=self.preserve_structure.isChecked(),
            mode=self.mode_combo.currentText(),
            include_emissive=self.include_emissive.isChecked(),
            overwrite=self.overwrite.isChecked(),
            material_path=material_path,
            generate_mipmaps=self.generate_mipmaps.isChecked()
        )
        self.thread.progress.connect(self.on_progress)
        self.thread.finished.connect(self.on_finished)
        self.thread.start()

    def on_progress(self, idx: int, total: int, message: str):
        self.progress.setMaximum(total)
        self.progress.setValue(idx)
        self.log(message, "INFO")

    def on_finished(self, success: bool, message: str):
        self.convert_btn.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.progress.setVisible(False)
        self.log(message, "SUCCESS" if success else "WARNING")

    def cancel(self):
        if self.thread and self.thread.isRunning():
            self.thread.requestInterruption()
            self.log("Cancel requested...", "INFO")
