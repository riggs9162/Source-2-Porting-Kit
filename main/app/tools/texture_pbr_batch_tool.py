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
    QLineEdit, QGroupBox, QDoubleSpinBox, QCheckBox, QSlider,
    QProgressBar, QFormLayout, QWidget, QComboBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView,
    QSplitter, QScrollArea, QSizePolicy, QGridLayout,
)
from PySide6.QtCore import Qt, QThread, Signal, QEvent

from .base_tool import BaseTool
from .fake_pbr_tool import FakePBRProcessor, ProcessingOptions, PBRInputs, _paint_table_row
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
    force_metal_mode: str = "off"


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

    def __init__(self, root: Path, recursive: bool = True):
        self.root = Path(root)
        self.recursive = recursive

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

    def _iter_dirs(self):
        if self.recursive:
            yield from os.walk(self.root)
            return
        if not self.root.is_dir():
            return
        files = [p.name for p in self.root.iterdir() if p.is_file()]
        yield str(self.root), [], files

    def scan(self) -> Dict[Tuple[Path, str], AssetGroup]:
        groups: Dict[Tuple[Path, str], AssetGroup] = {}
        for base, _, files in self._iter_dirs():
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
        generate_mipmaps: bool,
        metal_diffuse_suppression: float = 0.7,
        phong_strength: float = 0.5,
        phong_tint_mode: str = "selective",
        colored_metal_relief: float = 0.5,
    ):
        self.mode = mode
        self.include_emissive = include_emissive
        self.overwrite = overwrite
        self.material_path = material_path
        self.generate_mipmaps = generate_mipmaps
        self.metal_diffuse_suppression = metal_diffuse_suppression
        self.phong_strength = phong_strength
        self.phong_tint_mode = phong_tint_mode
        self.colored_metal_relief = colored_metal_relief

    @staticmethod
    def _save_channel(channel: np.ndarray, out_path: Path) -> None:
        img = Image.fromarray((channel * 255.0).astype(np.uint8), mode="L")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path)

    @staticmethod
    def _build_force_metal_channel(color_path: Path, mode: str) -> np.ndarray:
        with Image.open(color_path) as color_img:
            w, h = color_img.size
            if mode == "full":
                return np.ones((h, w), dtype=np.float32)
            channel_index = {"albedo_r": 0, "albedo_g": 1, "albedo_b": 2}.get(mode)
            if channel_index is None:
                return np.ones((h, w), dtype=np.float32)
            rgb = color_img.convert("RGB")
            arr = np.asarray(rgb, dtype=np.float32) / 255.0
        return np.clip(arr[:, :, channel_index], 0.0, 1.0)

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
        if group.force_metal_mode != "off":
            temp_dir.mkdir(parents=True, exist_ok=True)
            metal_path = temp_dir / f"{name}_force_metal.png"
            channel = self._build_force_metal_channel(color.path, group.force_metal_mode)
            self._save_channel(channel, metal_path)
        try:
            if self.mode == "Fake PBR":
                options = ProcessingOptions(
                    generate_mipmaps=self.generate_mipmaps,
                    metal_diffuse_suppression=self.metal_diffuse_suppression,
                    phong_strength=self.phong_strength,
                    phong_tint_mode=self.phong_tint_mode,
                    colored_metal_relief=self.colored_metal_relief,
                )
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
    row_finished = Signal(int, bool)

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
        generate_mipmaps: bool,
        metal_diffuse_suppression: float = 0.7,
        phong_strength: float = 0.5,
        phong_tint_mode: str = "selective",
        colored_metal_relief: float = 0.5,
        row_indices: Optional[List[int]] = None,
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
        self.metal_diffuse_suppression = metal_diffuse_suppression
        self.phong_strength = phong_strength
        self.phong_tint_mode = phong_tint_mode
        self.colored_metal_relief = colored_metal_relief
        # Parallel list of QTableWidget row indices, one per group, used to
        # report per-row completion back to the UI. -1 means "no row".
        self.row_indices = list(row_indices) if row_indices else [-1] * len(groups)

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
            self.generate_mipmaps,
            self.metal_diffuse_suppression,
            self.phong_strength,
            self.phong_tint_mode,
            self.colored_metal_relief,
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
            row = self.row_indices[idx - 1] if idx - 1 < len(self.row_indices) else -1
            if row >= 0:
                self.row_finished.emit(row, success)
        self.finished.emit(True, f"Processed {ok_count}/{total} assets")


class TexturePBRBatchTool(BaseTool):
    """GUI tool for batch converting texture folders into Fake/Exo PBR outputs."""

    METAL_OVERRIDE_OPTIONS = [
        ("Off", "off"),
        ("Fully Metal", "full"),
        ("Albedo Red", "albedo_r"),
        ("Albedo Green", "albedo_g"),
        ("Albedo Blue", "albedo_b"),
    ]

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
        """Two-pane layout: scrollable settings left, results table right —
        same shape as vmat_pbr_tool / gltf_smd_batch_tool."""
        root = QHBoxLayout()
        self.content_layout.addLayout(root)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_settings_pane())
        splitter.addWidget(self._build_results_pane())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([460, 880])
        root.addWidget(splitter)

        self._refresh_history_dropdown()

    # ------------------------------------------------------------------
    # Pane builders
    # ------------------------------------------------------------------

    def _build_settings_pane(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        container = QWidget()
        col = QVBoxLayout(container)
        col.setContentsMargins(0, 0, 6, 0)
        col.setSpacing(8)

        col.addWidget(self._build_folders_group())
        col.addWidget(self._build_output_group())
        col.addWidget(self._build_processing_group())
        col.addWidget(self._build_requirements_group())
        col.addStretch()

        scroll.setWidget(container)
        scroll.setMinimumWidth(420)
        return scroll

    def _build_folders_group(self) -> QGroupBox:
        group = QGroupBox("Folders & Recent Runs")
        form = QFormLayout()

        self.history_dropdown = QComboBox()
        self.history_dropdown.addItem("-- Recent runs --")
        self.history_dropdown.currentIndexChanged.connect(self.on_history_selected)
        form.addRow("Recent run:", self.history_dropdown)

        self.input_root = QLineEdit()
        in_btn = QPushButton("Browse...")
        in_btn.clicked.connect(lambda: self._browse_dir_into(self.input_root))
        form.addRow("Input root:", self._row(self.input_root, in_btn))

        self.output_root = QLineEdit()
        out_btn = QPushButton("Browse...")
        out_btn.clicked.connect(lambda: self._browse_dir_into(self.output_root))
        form.addRow("Output root:", self._row(self.output_root, out_btn))

        self.material_path = QLineEdit()
        self.material_path.setText("models/ports")
        self.material_path.setPlaceholderText("models/ports (Fake) or exopbr (Exo)")
        form.addRow("Material path:", self.material_path)

        group.setLayout(form)
        return group

    def _build_output_group(self) -> QGroupBox:
        group = QGroupBox("Output")
        form = QFormLayout()

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Fake PBR", "Exo PBR"])
        form.addRow("Mode:", self.mode_combo)

        # Boolean toggles in a 2x2 grid.
        gen_grid = QGridLayout()
        gen_grid.setContentsMargins(0, 0, 0, 0)
        gen_grid.setHorizontalSpacing(12)
        self.recursive_scan = QCheckBox("Recursive (include subfolders)")
        self.recursive_scan.setChecked(True)
        self.recursive_scan.setToolTip(
            "When on, scan all subfolders of the input. When off, scan only "
            "the input folder itself."
        )
        self.preserve_structure = QCheckBox("Preserve folder structure")
        self.preserve_structure.setChecked(True)
        self.preserve_structure.setToolTip(
            "Mirror the input folder tree under the output root."
        )
        self.overwrite = QCheckBox("Replace existing outputs")
        self.overwrite.setChecked(False)
        self.overwrite.setToolTip(
            "Overwrite outputs that already exist in the destination folder."
        )
        self.include_emissive = QCheckBox("Include emissive if present")
        self.include_emissive.setChecked(True)
        self.generate_mipmaps = QCheckBox("Generate Mipmaps")
        self.generate_mipmaps.setChecked(True)
        gen_grid.addWidget(self.recursive_scan, 0, 0)
        gen_grid.addWidget(self.preserve_structure, 0, 1)
        gen_grid.addWidget(self.overwrite, 1, 0)
        gen_grid.addWidget(self.include_emissive, 1, 1)
        gen_grid.addWidget(self.generate_mipmaps, 2, 0)
        form.addRow("Run mode:", self._wrap_layout(gen_grid))

        group.setLayout(form)
        return group

    def _build_processing_group(self) -> QGroupBox:
        """Sliders + tint mode (Fake PBR only — Exo ignores these)."""
        group = QGroupBox("Processing Options")
        form = QFormLayout()

        self.metal_suppression_slider, self.metal_suppression_value = self._make_slider(
            0, 100, 70, 0.01,
        )
        self.metal_suppression_slider.setToolTip(
            "Fake PBR only. How much to darken albedo on metal pixels. "
            "0.00 = no darkening, 1.00 = fully darkened (metal becomes black diffuse)."
        )
        form.addRow(
            "Metal Diffuse Suppression:",
            self._slider_row(self.metal_suppression_slider, self.metal_suppression_value),
        )

        self.phong_strength_slider, self.phong_strength_value = self._make_slider(
            0, 200, 50, 0.01,
        )
        self.phong_strength_slider.setToolTip(
            "Fake PBR only. Scales the phong mask and phong exponent map. "
            "0.00 = no phong, 0.50 = halved (default), 1.00 = original strength."
        )
        form.addRow(
            "Phong Strength:",
            self._slider_row(self.phong_strength_slider, self.phong_strength_value),
        )

        self.phong_tint_mode_combo = QComboBox()
        self.phong_tint_mode_combo.addItem("Off", "off")
        self.phong_tint_mode_combo.addItem("Selective (recommended)", "selective")
        self.phong_tint_mode_combo.addItem("Blanket", "blanket")
        self.phong_tint_mode_combo.setCurrentIndex(1)
        self.phong_tint_mode_combo.setToolTip(
            "Fake PBR only. Compensates the phong mask for $phongalbedotint runtime tinting. "
            "Selective: colored metals are boosted, dielectric phong is suppressed (envmap handles spec). "
            "Blanket: divide-by-luminance compensation everywhere. "
            "No effect on targets without $phongalbedotint."
        )
        form.addRow("Phong Tint Mode:", self.phong_tint_mode_combo)

        self.colored_metal_relief_slider, self.colored_metal_relief_value = self._make_slider(
            0, 100, 50, 0.01,
        )
        self.colored_metal_relief_slider.setToolTip(
            "Fake PBR only. Per-pixel relief on Metal Diffuse Suppression for chromatic metals. "
            "Only applied when Phong Tint Mode is not Off."
        )
        form.addRow(
            "Colored Metal Relief:",
            self._slider_row(self.colored_metal_relief_slider, self.colored_metal_relief_value),
        )

        group.setLayout(form)
        return group

    def _build_requirements_group(self) -> QGroupBox:
        """Texture-role filter — 2-column grid so it fits in the narrow pane."""
        group = QGroupBox("Required maps (filter)")
        grid = QGridLayout()
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setHorizontalSpacing(12)
        self.req_checkboxes: Dict[str, QCheckBox] = {}
        roles = (
            ("Color", "color", True),
            ("Normal", "normal", True),
            ("ORM", "orm", False),
            ("Emissive", "emissive", False),
        )
        for idx, (label, key, default) in enumerate(roles):
            cb = QCheckBox(label)
            cb.setChecked(default)
            cb.setToolTip(
                f"Require a {label.lower()} map for an asset to be processed. "
                f"Rows missing this role will be auto-unchecked in the table."
            )
            cb.stateChanged.connect(self._apply_requirements_filter)
            grid.addWidget(cb, idx // 2, idx % 2)
            self.req_checkboxes[key] = cb
        group.setLayout(grid)
        return group

    def _build_results_pane(self) -> QWidget:
        pane = QWidget()
        col = QVBoxLayout(pane)
        col.setContentsMargins(6, 0, 0, 0)
        col.setSpacing(6)

        action_row = QHBoxLayout()
        self.scan_btn = QPushButton("Scan")
        self.scan_btn.clicked.connect(self.scan)
        action_row.addWidget(self.scan_btn)

        action_row.addSpacing(12)
        action_row.addWidget(QLabel("All:"))
        self.select_all_btn = QPushButton("Check")
        self.select_all_btn.clicked.connect(lambda: self._set_all_results_selected(True))
        self.select_none_btn = QPushButton("Uncheck")
        self.select_none_btn.clicked.connect(lambda: self._set_all_results_selected(False))
        self.select_invert_btn = QPushButton("Invert")
        self.select_invert_btn.clicked.connect(self._invert_results_selection)
        for b in (self.select_all_btn, self.select_none_btn, self.select_invert_btn):
            action_row.addWidget(b)

        action_row.addSpacing(12)
        action_row.addWidget(QLabel("Selected:"))
        sel_tooltip = (
            "Click a row, then Shift+Click (range) or Ctrl+Click (toggle) more "
            "rows like in Explorer.\n"
            "These buttons toggle the Include checkbox for the highlighted rows "
            "only. Pressing Space while the table has focus does the same."
        )
        self.check_selected_btn = QPushButton("Check")
        self.check_selected_btn.setToolTip(sel_tooltip)
        self.check_selected_btn.clicked.connect(lambda: self._set_selected_rows_checked(True))
        self.uncheck_selected_btn = QPushButton("Uncheck")
        self.uncheck_selected_btn.setToolTip(sel_tooltip)
        self.uncheck_selected_btn.clicked.connect(lambda: self._set_selected_rows_checked(False))
        self.toggle_selected_btn = QPushButton("Toggle")
        self.toggle_selected_btn.setToolTip(sel_tooltip)
        self.toggle_selected_btn.clicked.connect(self._toggle_selected_rows)
        for b in (self.check_selected_btn, self.uncheck_selected_btn, self.toggle_selected_btn):
            action_row.addWidget(b)

        action_row.addStretch()
        col.addLayout(action_row)

        # Results table
        self.results_table = QTableWidget(0, 8)
        self.results_table.setHorizontalHeaderLabels([
            "Include", "Asset", "Color", "Normal", "ORM", "Emissive",
            "Metal Override", "Warnings",
        ])
        header = self.results_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.results_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.results_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.results_table.installEventFilter(self)
        col.addWidget(self.results_table, 1)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        col.addWidget(self.progress)

        run_row = QHBoxLayout()
        run_row.addStretch()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel)
        self.convert_btn = QPushButton("Convert")
        self.convert_btn.setEnabled(False)
        self.convert_btn.clicked.connect(self.convert)
        run_row.addWidget(self.cancel_btn)
        run_row.addWidget(self.convert_btn)
        col.addLayout(run_row)

        return pane

    # ------------------------------------------------------------------
    # Small layout helpers
    # ------------------------------------------------------------------

    def _make_slider(self, lo: int, hi: int, initial: int, step: float):
        slider = QSlider(Qt.Horizontal)
        slider.setRange(lo, hi)
        slider.setValue(initial)
        label = QLabel(f"{initial * step:.2f}")
        slider.valueChanged.connect(lambda v, _s=step: label.setText(f"{v * _s:.2f}"))
        return slider, label

    def _slider_row(self, slider: QSlider, label: QLabel) -> QWidget:
        row = QHBoxLayout()
        row.addWidget(slider, 1)
        row.addWidget(label)
        wrap = QWidget()
        row.setContentsMargins(0, 0, 0, 0)
        wrap.setLayout(row)
        return wrap

    @staticmethod
    def _wrap_layout(layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w

    # ------------------------------------------------------------------
    # Selection helpers + Spacebar event filter (Explorer-style)
    # ------------------------------------------------------------------

    def _set_all_results_selected(self, checked: bool):
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self.results_table.rowCount()):
            item = self.results_table.item(row, 0)
            if item is not None:
                item.setCheckState(state)

    def _invert_results_selection(self):
        for row in range(self.results_table.rowCount()):
            item = self.results_table.item(row, 0)
            if item is None:
                continue
            item.setCheckState(
                Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked
            )

    def _highlighted_rows(self) -> List[int]:
        sm = self.results_table.selectionModel()
        if sm is None:
            return []
        return sorted({idx.row() for idx in sm.selectedIndexes()})

    def _set_selected_rows_checked(self, checked: bool):
        state = Qt.Checked if checked else Qt.Unchecked
        for row in self._highlighted_rows():
            item = self.results_table.item(row, 0)
            if item is not None:
                item.setCheckState(state)

    def _toggle_selected_rows(self):
        rows = self._highlighted_rows()
        if not rows:
            return
        checked_count = sum(
            1 for row in rows
            if self.results_table.item(row, 0) is not None
            and self.results_table.item(row, 0).checkState() == Qt.Checked
        )
        new_state = Qt.Unchecked if checked_count * 2 >= len(rows) else Qt.Checked
        for row in rows:
            item = self.results_table.item(row, 0)
            if item is not None:
                item.setCheckState(new_state)

    def eventFilter(self, obj, event):
        """Intercept Space on the results table to bulk-toggle highlighted rows."""
        if obj is self.results_table and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Space, Qt.Key_Select):
                self._toggle_selected_rows()
                return True
        return super().eventFilter(obj, event)

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
        input_root = self.input_root.text().strip()
        output_root = self.output_root.text().strip()
        return {
            "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "label": Path(input_root).name if input_root else output_root,
            "input_root": input_root,
            "output_root": output_root,
            "mode": self.mode_combo.currentText(),
            "recursive": self.recursive_scan.isChecked(),
            "preserve_structure": self.preserve_structure.isChecked(),
            "overwrite": self.overwrite.isChecked(),
            "include_emissive": self.include_emissive.isChecked(),
            "generate_mipmaps": self.generate_mipmaps.isChecked(),
            "metal_diffuse_suppression": self.metal_suppression_slider.value() / 100.0,
            "phong_strength": self.phong_strength_slider.value() / 100.0,
            "phong_tint_mode": self.phong_tint_mode_combo.currentData() or "selective",
            "colored_metal_relief": self.colored_metal_relief_slider.value() / 100.0,
            "material_path": self.material_path.text().strip(),
            "requirements": {key: cb.isChecked() for key, cb in self.req_checkboxes.items()},
        }

    def _save_current_run_to_history(self):
        entry = self._make_history_entry()
        if not entry.get("input_root") and not entry.get("output_root"):
            return

        def _same(a: dict, b: dict) -> bool:
            keys = [
                "input_root", "output_root", "mode", "recursive", "preserve_structure",
                "overwrite", "include_emissive", "generate_mipmaps",
                "metal_diffuse_suppression", "phong_strength", "phong_tint_mode",
                "colored_metal_relief", "material_path", "requirements"
            ]
            return all(a.get(k) == b.get(k) for k in keys)

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
        mode = entry.get("mode") or "Fake PBR"
        mode_index = self.mode_combo.findText(mode)
        if mode_index >= 0:
            self.mode_combo.setCurrentIndex(mode_index)
        self.recursive_scan.setChecked(bool(entry.get("recursive", True)))
        self.preserve_structure.setChecked(bool(entry.get("preserve_structure", True)))
        self.overwrite.setChecked(bool(entry.get("overwrite", False)))
        self.include_emissive.setChecked(bool(entry.get("include_emissive", True)))
        self.generate_mipmaps.setChecked(bool(entry.get("generate_mipmaps", True)))
        try:
            metal_supp_val = float(entry.get("metal_diffuse_suppression", 0.7))
        except (TypeError, ValueError):
            metal_supp_val = 0.7
        self.metal_suppression_slider.setValue(int(metal_supp_val * 100))
        try:
            phong_val = float(entry.get("phong_strength", 0.5))
        except (TypeError, ValueError):
            phong_val = 0.5
        self.phong_strength_slider.setValue(int(phong_val * 100))
        try:
            relief_val = float(entry.get("colored_metal_relief", 0.5))
        except (TypeError, ValueError):
            relief_val = 0.5
        self.colored_metal_relief_slider.setValue(int(relief_val * 100))
        tint_mode_val = str(entry.get("phong_tint_mode", "selective"))
        tint_idx = self.phong_tint_mode_combo.findData(tint_mode_val)
        if tint_idx >= 0:
            self.phong_tint_mode_combo.setCurrentIndex(tint_idx)
        self.material_path.setText(entry.get("material_path") or "models/ports")
        reqs = entry.get("requirements") or {}
        for key, cb in self.req_checkboxes.items():
            if key in reqs:
                cb.setChecked(bool(reqs[key]))

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

        scanner = TextureScanner(Path(root), recursive=self.recursive_scan.isChecked())
        groups = scanner.scan()

        resolver = RoleResolver()
        resolved_groups: List[AssetGroup] = []
        for group in groups.values():
            resolver.resolve(group, include_emissive=self.include_emissive.isChecked())
            resolved_groups.append(group)

        self._populate_table(resolved_groups)
        self.scanned_groups = resolved_groups
        self.convert_btn.setEnabled(len(resolved_groups) > 0)
        excluded = self._apply_requirements_filter()
        msg = f"Scan complete: {len(resolved_groups)} assets detected"
        if excluded:
            msg += f" ({excluded} excluded by requirements)"
        self.log(msg, "INFO")

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

            force_combo = QComboBox()
            for label, value in self.METAL_OVERRIDE_OPTIONS:
                force_combo.addItem(label, value)
            current_idx = next(
                (i for i, (_, v) in enumerate(self.METAL_OVERRIDE_OPTIONS)
                 if v == group.force_metal_mode),
                0
            )
            force_combo.setCurrentIndex(current_idx)
            force_combo.setToolTip(
                "Override the metalness mask. Use when the material has no metal map "
                "but is fully metal, or when a single albedo channel encodes metalness."
            )
            self.results_table.setCellWidget(row, 6, force_combo)

            warn_text = "; ".join(group.warnings)
            warn_item = QTableWidgetItem(warn_text)
            self.results_table.setItem(row, 7, warn_item)

    def _selected_groups(self) -> Tuple[List[AssetGroup], List[int]]:
        sorted_groups = sorted(
            self.scanned_groups,
            key=lambda g: (str(g.base_dir), g.pretty_name)
        )
        selected: List[AssetGroup] = []
        rows: List[int] = []
        for row in range(self.results_table.rowCount()):
            if self.results_table.item(row, 0).checkState() != Qt.Checked:
                continue
            group = sorted_groups[row]
            force_widget = self.results_table.cellWidget(row, 6)
            if isinstance(force_widget, QComboBox):
                mode = force_widget.currentData()
                group.force_metal_mode = mode if isinstance(mode, str) else "off"
            else:
                group.force_metal_mode = "off"
            selected.append(group)
            rows.append(row)
        return selected, rows

    def convert(self):
        root = self.input_root.text().strip()
        out_root = self.output_root.text().strip()
        if not root or not out_root:
            self.log("Input/Output roots are required", "ERROR")
            return

        groups, row_indices = self._selected_groups()
        if not groups:
            self.log("No assets selected", "WARNING")
            return

        self._save_current_run_to_history()

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
            generate_mipmaps=self.generate_mipmaps.isChecked(),
            metal_diffuse_suppression=self.metal_suppression_slider.value() / 100.0,
            phong_strength=self.phong_strength_slider.value() / 100.0,
            phong_tint_mode=self.phong_tint_mode_combo.currentData() or "selective",
            colored_metal_relief=self.colored_metal_relief_slider.value() / 100.0,
            row_indices=row_indices,
        )
        self.thread.progress.connect(self.on_progress)
        self.thread.row_finished.connect(self._on_row_finished)
        self.thread.finished.connect(self.on_finished)
        self.thread.start()

    def _on_row_finished(self, row: int, success: bool):
        """Tint the corresponding results_table row green/red as each task completes."""
        _paint_table_row(self.results_table, row, success)

    def _required_roles(self) -> List[str]:
        """Texture roles currently required for an asset to be eligible."""
        if not hasattr(self, "req_checkboxes"):
            return []
        return [key for key, cb in self.req_checkboxes.items() if cb.isChecked()]

    def _apply_requirements_filter(self) -> int:
        """Auto-uncheck rows whose underlying group is missing any required role.

        Returns the count of rows that ended up unchecked because they failed
        the current requirement set. Re-checking a row manually is still
        allowed; the filter only fires on requirement-toggle and post-scan.
        """
        if not hasattr(self, "results_table") or not self.scanned_groups:
            return 0
        required = self._required_roles()
        if not required:
            for row in range(self.results_table.rowCount()):
                include_item = self.results_table.item(row, 0)
                if include_item is not None:
                    include_item.setToolTip("")
            return 0
        sorted_groups = sorted(
            self.scanned_groups,
            key=lambda g: (str(g.base_dir), g.pretty_name)
        )
        excluded = 0
        for row in range(self.results_table.rowCount()):
            include_item = self.results_table.item(row, 0)
            if include_item is None or row >= len(sorted_groups):
                continue
            group = sorted_groups[row]
            missing = [r for r in required if group.resolved.get(r) is None]
            if missing:
                if include_item.checkState() == Qt.Checked:
                    include_item.setCheckState(Qt.Unchecked)
                include_item.setToolTip("Missing required: " + ", ".join(missing))
                excluded += 1
            else:
                include_item.setToolTip("")
        return excluded

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
