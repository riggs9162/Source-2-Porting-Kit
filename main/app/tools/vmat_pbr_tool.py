"""
VMAT PBR Tool - Parse VMAT files and convert to Fake or Exo PBR outputs.

This tool scans a directory for VMAT files, reads texture references from
plain-text VMATs, resolves those paths against a texture root, and feeds the
textures into the existing FakePBR / ExoPBR processors to output VTF/VMT files.
"""

from __future__ import annotations

import os
import re
import json
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFileDialog,
    QLineEdit, QGroupBox, QDoubleSpinBox, QCheckBox,
    QProgressBar, QFormLayout, QWidget, QComboBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QSlider, QStackedWidget
)
from PySide6.QtCore import Qt, QThread, Signal

from .base_tool import BaseTool
from .fake_pbr_tool import FakePBRProcessor, ProcessingOptions, PBRInputs
from .exo_pbr_tool import ExoPBRProcessor, ExoPBROptions, ExoPBRInputs
from ..utils.helpers import get_config_dir


@dataclass
class VmatTextures:
    color: Optional[Path] = None
    normal: Optional[Path] = None
    ao: Optional[Path] = None
    roughness: Optional[Path] = None
    metallic: Optional[Path] = None
    emissive: Optional[Path] = None
    translucency: Optional[Path] = None


@dataclass
class VmatEntry:
    vmat_path: Path
    name: str
    rel_dir: Path
    textures: VmatTextures
    raw_paths: Dict[str, str] = field(default_factory=dict)
    sources: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    # Transparency mode derived from the vmat shader/feature flags. translucent
    # implies $translucent 1 (alpha-blend); alphatest implies $alphatest 1.
    # Both default off — only set when the source vmat asks for transparency.
    translucent: bool = False
    alphatest: bool = False
    # Scalar PBR constants from the vmat (g_flMetalness, g_flRoughness). These
    # apply uniformly across the surface when the corresponding TextureXxx is a
    # vector literal or absent — Source 2's way of saying "no map, use this
    # constant value." Carried separately from textures so the runner can fill
    # missing maps with synthesised uniform images at process time.
    metallic_constant: Optional[float] = None
    roughness_constant: Optional[float] = None


class VmatParser:
    """Parse VMAT text files and resolve texture paths."""

    KEY_PATTERN = re.compile(r'"(?P<key>Texture[A-Za-z0-9_]+)"\s+"(?P<value>[^"]+)"')
    SHADER_PATTERN = re.compile(r'"shader"\s+"(?P<value>[^"]+)"', re.IGNORECASE)
    FLAG_PATTERN = re.compile(r'"(?P<key>F_[A-Z_]+)"\s+"(?P<value>[01])"')
    SCALAR_PATTERN = re.compile(r'"(?P<key>g_fl[A-Za-z0-9_]+)"\s+"(?P<value>-?[0-9.]+)"')
    IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tga", ".bmp", ".tif", ".tiff"}

    # Source 2 shader names that imply transparent rendering even without an
    # explicit F_TRANSLUCENT flag.
    TRANSLUCENT_SHADERS = ("vr_glass.vfx", "vr_unlit.vfx", "vr_monitor.vfx")

    # Role tokens used for same-folder sibling auto-detect when the vmat doesn't
    # name a texture explicitly (or names a vector literal). Includes Source 2
    # Viewer's "g_tXxx" engine-internal naming that surfaces in compiled-texture
    # filenames as suffixes like "_g_tnormal_<hash>" or "_g_tcolor_<hash>".
    ROLE_TOKENS: Dict[str, Tuple[str, ...]] = {
        "color": (
            "color", "albedo", "basecolor", "base_color", "basecolour", "base_colour",
            "diffuse", "base", "col", "bc", "diff", "d",
            "tcolor", "tdiffuse", "talbedo", "tbasecolor",
        ),
        "normal": (
            "normal_opengl", "normal", "normalmap", "normal_map", "nrm", "normals",
            "normalgl", "normal_dx", "nrml", "nor", "n",
            "tnormal", "tnormalmap",
        ),
        "ao": (
            "ao", "ambientocclusion", "ambient_occlusion", "occlusion", "occ", "aoc", "mixed_ao",
            "tao", "tambientocclusion", "tocclusion",
        ),
        "roughness": (
            "roughness", "rough", "rgh", "r", "rghness",
            "troughness", "trough",
        ),
        "metallic": (
            "metallic", "metal", "metalness", "mtl", "m",
            "tmetalness", "tmetal", "tmetallic",
        ),
        "emissive": (
            "emissive", "emit", "emission", "selfillum", "self_illum", "illum", "illumin", "glow",
            "temission", "temissive", "tselfillum",
        ),
        "translucency": (
            "trans", "translucency", "translucent", "opacity", "alpha", "opac",
            "ttranslucency", "topacity",
        ),
    }

    KEY_MAP: Dict[str, Tuple[str, ...]] = {
        "color": (
            "TextureColor",
            "TextureDiffuse",
            "TextureAlbedo",
            "TextureBaseColor",
            "TextureBasecolor",
            "TextureBase",
        ),
        "normal": (
            "TextureNormal",
            "TextureNormalMap",
        ),
        "ao": (
            "TextureAmbientOcclusion",
            "TextureAO",
            "TextureOcclusion",
        ),
        "roughness": (
            "TextureRoughness",
            "TextureRough",
        ),
        "metallic": (
            "TextureMetalness",
            "TextureMetallic",
            "TextureMetal",
        ),
        "emissive": (
            "TextureSelfIllumMask",
            "TextureSelfIllum",
            "TextureEmissive",
            "TextureEmission",
            "TextureGlow",
        ),
        "translucency": (
            "TextureTranslucency",
            "TextureOpacity",
            "TextureAlpha",
        ),
    }

    def __init__(self, texture_root: Path):
        self.texture_root = texture_root

    @staticmethod
    def _tokenize(stem: str) -> List[str]:
        return [token.lower() for token in re.split(r"[\s._-]+", stem) if token]

    @classmethod
    def _strip_role_tokens(cls, stem: str) -> str:
        role_tokens = {token for tokens in cls.ROLE_TOKENS.values() for token in tokens}
        tokens = [token for token in cls._tokenize(stem) if token not in role_tokens]
        return "_".join(tokens) if tokens else stem.lower()

    def _resolve_from_siblings(self, vmat_path: Path, role: str) -> Optional[Path]:
        role_tokens = set(self.ROLE_TOKENS.get(role, ()))
        if not role_tokens:
            return None

        # Use the raw vmat stem as the identity. Don't strip role tokens from
        # it — names like "combine_metal_base" legitimately contain "metal"
        # and "base" as descriptors, and stripping leaves just "combine"
        # which prefix-matches any other combine_* sibling.
        vmat_base = vmat_path.stem.lower()
        scored: List[Tuple[float, Path]] = []
        for candidate in vmat_path.parent.iterdir():
            if not candidate.is_file():
                continue
            if candidate.suffix.lower() not in self.IMAGE_EXTS:
                continue

            tokens = self._tokenize(candidate.stem)
            if not role_tokens.intersection(tokens):
                continue

            candidate_base = self._strip_role_tokens(candidate.stem)

            # The candidate must be tied to *this* vmat. Require a prefix match
            # in either direction; matching on shared individual tokens picks
            # up generic words like "box" or "main" and grabs textures from
            # unrelated materials.
            if not (
                candidate_base.startswith(vmat_base)
                or vmat_base.startswith(candidate_base)
            ):
                continue

            score = 100.0
            if candidate_base == vmat_base:
                score += 50.0
            else:
                score += 20.0
            score -= len(candidate.name) * 0.01
            scored.append((score, candidate))

        if not scored:
            return None

        scored.sort(key=lambda item: item[0], reverse=True)
        best_score = scored[0][0]
        best = [path for score, path in scored if abs(score - best_score) < 1e-6]
        if len(best) > 1:
            return None
        return scored[0][1]

    def parse_file(self, vmat_path: Path, vmat_root: Path) -> VmatEntry:
        text = vmat_path.read_text(encoding="utf-8", errors="ignore")
        matches = self.KEY_PATTERN.findall(text)
        raw_map: Dict[str, str] = {}
        for key, value in matches:
            if key not in raw_map:
                raw_map[key] = value

        # Detect transparency intent from the shader name + feature flags.
        # Source 2 has many flag variants (F_TRANSLUCENT, F_ADDITIVE_BLEND,
        # F_ADDITIVE_BLEND_OVER_ALPHA, F_BLEND_MODE, ...). Treat any "set" flag
        # whose name implies blending as translucent.
        flags: Dict[str, str] = dict(self.FLAG_PATTERN.findall(text))
        scalars: Dict[str, str] = dict(self.SCALAR_PATTERN.findall(text))

        def _parse_scalar(key: str) -> Optional[float]:
            raw_val = scalars.get(key)
            if raw_val is None:
                return None
            try:
                return float(raw_val)
            except ValueError:
                return None

        # Source 2 stores per-material scalar overrides for metalness/roughness
        # (g_flMetalness, g_flRoughness). When the matching TextureXxx is a
        # vector literal or absent, the scalar is the authoritative value —
        # the runner will materialise it as a uniform map.
        metallic_constant = _parse_scalar("g_flMetalness")
        roughness_constant = _parse_scalar("g_flRoughness")

        shader_match = self.SHADER_PATTERN.search(text)
        shader_name = (shader_match.group("value").lower() if shader_match else "")
        translucent = (
            any(
                value == "1" and ("TRANSLUCENT" in key or "BLEND" in key)
                for key, value in flags.items()
            )
            or any(s in shader_name for s in self.TRANSLUCENT_SHADERS)
        )
        alphatest = flags.get("F_ALPHA_TEST") == "1"

        textures = VmatTextures()
        warnings: List[str] = []
        sources: Dict[str, str] = {}

        def _resolve(raw: Optional[str]) -> Optional[Path]:
            if not raw:
                return None
            stripped = raw.strip()
            # Source 2 Viewer emits vector-literal constants (e.g.
            # "TextureColor" "[1.0 1.0 1.0 0.0]") when the source vmdl used a
            # solid color instead of a texture map. Treat as "no texture" so
            # the sibling auto-detect can run without spurious "Missing"
            # warnings cluttering the results table.
            if stripped.startswith("[") or stripped.startswith("{"):
                return None
            raw_clean = stripped.replace("\\", "/")
            raw_path = Path(raw_clean)
            if raw_path.is_absolute():
                candidate = raw_path
            else:
                candidate = self.texture_root / raw_path
            if candidate.exists():
                return candidate
            # Source 2 Viewer's TextureXxx values often point at game-relative
            # paths like "models/props_combine/.../foo_color.png" while the
            # actual exported PNG sits next to the vmat. Try that filename in
            # the vmat's own folder before declaring it missing — the vmat
            # author specified the basename intentionally.
            local_candidate = vmat_path.parent / raw_path.name
            if local_candidate.exists():
                return local_candidate
            warnings.append(f"Missing: {raw}")
            return None

        for role, keys in self.KEY_MAP.items():
            raw_val = None
            for key in keys:
                if key in raw_map:
                    raw_val = raw_map[key]
                    break
            # Vector literals indicate "no texture, use a constant" — they're
            # expected, not a missing path, so suppress the warn-on-fallback.
            raw_was_literal = bool(raw_val) and raw_val.strip().startswith(("[", "{"))
            resolved = _resolve(raw_val)
            if resolved is not None:
                sources[role] = "VMAT definition"
            else:
                sibling = self._resolve_from_siblings(vmat_path, role)
                if sibling is not None:
                    resolved = sibling
                    sources[role] = "Same-folder auto-detect"
                    if raw_val and not raw_was_literal:
                        warnings.append(f"Using sibling {sibling.name} for {role}")
            if role == "color":
                textures.color = resolved
            elif role == "normal":
                textures.normal = resolved
            elif role == "ao":
                textures.ao = resolved
            elif role == "roughness":
                textures.roughness = resolved
            elif role == "metallic":
                textures.metallic = resolved
            elif role == "emissive":
                textures.emissive = resolved
            elif role == "translucency":
                textures.translucency = resolved
            if raw_val:
                raw_map[role] = raw_val

        # If translucent but no opacity map declared, fall back to the color
        # texture's own alpha channel if it carries one (read at process time).
        if (translucent or alphatest) and textures.translucency is None:
            warnings.append("Translucent but no opacity map; will use color alpha if present")

        # Conversely, when a translucency *texture* is present (sibling auto-detect
        # picked up a "_trans" file or the vmat referenced one explicitly) but no
        # blend flag was set, treat the material as translucent. The texture's
        # existence is the user-authored signal — without flipping the flag here,
        # the alpha gets baked into the basetexture but the VMT never enables
        # $translucent, so the alpha is silently ignored at runtime.
        if textures.translucency is not None and not translucent and not alphatest:
            translucent = True

        rel_dir = vmat_path.parent
        if vmat_root in vmat_path.parents:
            rel_dir = vmat_path.parent.relative_to(vmat_root)
        return VmatEntry(
            vmat_path=vmat_path,
            name=vmat_path.stem,
            rel_dir=rel_dir,
            textures=textures,
            raw_paths=raw_map,
            sources=sources,
            warnings=warnings,
            translucent=translucent,
            alphatest=alphatest,
            metallic_constant=metallic_constant,
            roughness_constant=roughness_constant,
        )


class VmatBatchRunner(QThread):
    progress = Signal(int, int, str)
    finished = Signal(bool, str)

    def __init__(
        self,
        entries: List[VmatEntry],
        vmat_root: Path,
        output_root: Path,
        preserve_structure: bool,
        mode: str,
        material_path: str,
        append_material_subfolders: bool,
        prefix: str,
        suffix: str,
        fake_ao_strength: float,
        fake_gloss_gamma: float,
        fake_metal_diffuse_suppression: float,
        fake_phong_strength: float,
        exo_emission: float,
        exo_parallax: float,
        exo_alphablend: bool,
        generate_vtf: bool,
        generate_vmt: bool,
        generate_mipmaps: bool
    ):
        super().__init__()
        self.entries = entries
        self.vmat_root = vmat_root
        self.output_root = output_root
        self.preserve_structure = preserve_structure
        self.mode = mode
        self.material_path = material_path
        self.append_material_subfolders = append_material_subfolders
        self.prefix = prefix
        self.suffix = suffix
        self.fake_ao_strength = fake_ao_strength
        self.fake_gloss_gamma = fake_gloss_gamma
        self.fake_metal_diffuse_suppression = fake_metal_diffuse_suppression
        self.fake_phong_strength = fake_phong_strength
        self.exo_emission = exo_emission
        self.exo_parallax = exo_parallax
        self.exo_alphablend = exo_alphablend
        self.generate_vtf = generate_vtf
        self.generate_vmt = generate_vmt
        self.generate_mipmaps = generate_mipmaps

    def _output_dir_for(self, entry: VmatEntry) -> Path:
        if self.preserve_structure and entry.rel_dir is not None:
            return self.output_root / entry.rel_dir
        return self.output_root

    def _material_path_for(self, entry: VmatEntry) -> str:
        if not self.append_material_subfolders:
            return self.material_path
        rel = entry.rel_dir.as_posix().strip("./")
        if rel:
            return f"{self.material_path}/{rel}"
        return self.material_path

    def run(self):
        total = len(self.entries)
        if total == 0:
            self.finished.emit(False, "No VMAT files selected")
            return

        ok_count = 0
        for idx, entry in enumerate(self.entries, start=1):
            if self.isInterruptionRequested():
                self.finished.emit(False, f"Cancelled after {ok_count}/{total}")
                return

            tex = entry.textures
            if tex.color is None or tex.normal is None:
                self.progress.emit(idx, total, f"✗ {entry.name}: missing color or normal")
                continue

            output_dir = self._output_dir_for(entry)
            os.makedirs(output_dir, exist_ok=True)
            material_name = f"{self.prefix}{entry.name}{self.suffix}"
            mat_path = self._material_path_for(entry)

            try:
                if self.mode == "Fake PBR":
                    options = ProcessingOptions(
                        ao_strength=self.fake_ao_strength,
                        gloss_gamma=self.fake_gloss_gamma,
                        generate_vtf=self.generate_vtf,
                        generate_vmt=self.generate_vmt,
                        generate_mipmaps=self.generate_mipmaps,
                        metal_diffuse_suppression=self.fake_metal_diffuse_suppression,
                        phong_strength=self.fake_phong_strength,
                        translucent=entry.translucent,
                        alphatest=entry.alphatest,
                    )
                    processor = FakePBRProcessor(options)
                    inputs = PBRInputs(
                        color=str(tex.color),
                        normal=str(tex.normal),
                        ao=str(tex.ao) if tex.ao else None,
                        roughness=str(tex.roughness) if tex.roughness else None,
                        metallic=str(tex.metallic) if tex.metallic else None,
                        translucency=str(tex.translucency) if tex.translucency else None,
                        metallic_constant=entry.metallic_constant,
                        roughness_constant=entry.roughness_constant,
                    )
                    try:
                        success, msg = processor.process_material(inputs, str(output_dir), material_name, mat_path)
                    finally:
                        processor.shutdown()
                else:
                    # Force alphablend on when the source vmat declares translucency,
                    # regardless of UI checkbox — the vmat is authoritative.
                    alphablend = self.exo_alphablend or entry.translucent or entry.alphatest
                    options = ExoPBROptions(
                        generate_vtf=self.generate_vtf,
                        generate_vmt=self.generate_vmt,
                        generate_mipmaps=self.generate_mipmaps,
                        emissionscale=self.exo_emission,
                        parallaxscale=self.exo_parallax,
                        alphablend=alphablend,
                    )
                    processor = ExoPBRProcessor(options)
                    inputs = ExoPBRInputs(
                        color=str(tex.color),
                        normal=str(tex.normal),
                        ao=str(tex.ao) if tex.ao else None,
                        roughness=str(tex.roughness) if tex.roughness else None,
                        metallic=str(tex.metallic) if tex.metallic else None,
                        selfillum=str(tex.emissive) if tex.emissive else None,
                        transparency_mask=str(tex.translucency) if tex.translucency else None,
                        metallic_constant=entry.metallic_constant,
                        roughness_constant=entry.roughness_constant,
                    )
                    try:
                        success, msg = processor.process_material(inputs, str(output_dir), material_name, mat_path)
                    finally:
                        processor.shutdown()
            except Exception as exc:
                self.progress.emit(idx, total, f"✗ {entry.name}: {exc}")
                continue

            if success:
                ok_count += 1
                self.progress.emit(idx, total, f"✓ {entry.name} done")
            else:
                self.progress.emit(idx, total, f"✗ {entry.name}: {msg}")

        self.finished.emit(True, f"Processed {ok_count}/{total} materials")


class VmatPBRTool(BaseTool):
    """GUI tool for converting VMAT materials to Fake/Exo PBR outputs."""

    def __init__(self):
        super().__init__("VMAT PBR")
        self.thread: Optional[VmatBatchRunner] = None
        self.entries: List[VmatEntry] = []
        self.sorted_entries: List[VmatEntry] = []
        self.history: List[dict] = []
        try:
            self._history_file = get_config_dir() / "vmat_pbr_history.json"
        except Exception:
            self._history_file = Path(__file__).parent.parent / "config" / "vmat_pbr_history.json"
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

        # Folders
        folder_group = QGroupBox("Folders")
        folder_form = QFormLayout()

        self.vmat_root = QLineEdit()
        vmat_btn = QPushButton("Browse...")
        vmat_btn.clicked.connect(lambda: self._browse_dir_into(self.vmat_root))
        folder_form.addRow("VMAT Root:", self._row(self.vmat_root, vmat_btn))

        self.texture_root = QLineEdit()
        tex_btn = QPushButton("Browse...")
        tex_btn.clicked.connect(lambda: self._browse_dir_into(self.texture_root))
        folder_form.addRow("Texture Root:", self._row(self.texture_root, tex_btn))
        self.texture_root.setPlaceholderText("Optional; same-folder textures are auto-detected")

        self.output_root = QLineEdit()
        out_btn = QPushButton("Browse...")
        out_btn.clicked.connect(lambda: self._browse_dir_into(self.output_root))
        folder_form.addRow("Output Root:", self._row(self.output_root, out_btn))

        folder_group.setLayout(folder_form)
        layout.addWidget(folder_group)

        # Options
        opt_group = QGroupBox("Options")
        opt_form = QFormLayout()

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Fake PBR", "Exo PBR"])
        self.mode_combo.currentTextChanged.connect(self._sync_mode_defaults)
        opt_form.addRow("Mode:", self.mode_combo)

        self.material_path = QLineEdit()
        self.material_path.setText("models/ports")
        self.material_path.setPlaceholderText("models/ports (Fake) or exopbr (Exo)")
        opt_form.addRow("Material Path:", self.material_path)

        self.append_material_subfolders = QCheckBox("Append VMAT subfolders to material path")
        self.append_material_subfolders.setChecked(True)
        opt_form.addRow("", self.append_material_subfolders)

        self.preserve_structure = QCheckBox("Preserve VMAT folder structure in output")
        self.preserve_structure.setChecked(True)
        opt_form.addRow("", self.preserve_structure)

        name_row = QHBoxLayout()
        self.prefix_input = QLineEdit()
        self.prefix_input.setPlaceholderText("Prefix")
        self.suffix_input = QLineEdit()
        self.suffix_input.setPlaceholderText("Suffix")
        name_row.addWidget(self.prefix_input)
        name_row.addWidget(self.suffix_input)
        opt_form.addRow("Name Prefix/Suffix:", self._row_widget(name_row))

        self.generate_vtf = QCheckBox("Generate VTF")
        self.generate_vtf.setChecked(True)
        self.generate_vmt = QCheckBox("Generate VMT")
        self.generate_vmt.setChecked(True)
        self.generate_mipmaps = QCheckBox("Generate Mipmaps")
        self.generate_mipmaps.setChecked(True)
        opt_form.addRow("", self.generate_vtf)
        opt_form.addRow("", self.generate_vmt)
        opt_form.addRow("", self.generate_mipmaps)

        opt_group.setLayout(opt_form)
        layout.addWidget(opt_group)

        # Mode-specific options
        mode_group = QGroupBox("Mode Settings")
        mode_layout = QVBoxLayout()
        self.mode_stack = QStackedWidget()

        # Fake PBR settings
        fake_widget = QWidget()
        fake_form = QFormLayout(fake_widget)

        fake_ao_row = QHBoxLayout()
        self.fake_ao_slider = QSlider(Qt.Horizontal)
        self.fake_ao_slider.setRange(0, 200)
        self.fake_ao_slider.setValue(50)
        self.fake_ao_value = QLabel("0.50")
        self.fake_ao_slider.valueChanged.connect(lambda v: self.fake_ao_value.setText(f"{v/100:.2f}"))
        fake_ao_row.addWidget(self.fake_ao_slider)
        fake_ao_row.addWidget(self.fake_ao_value)
        fake_form.addRow("AO Strength:", self._row_widget(fake_ao_row))

        fake_gamma_row = QHBoxLayout()
        self.fake_gamma_slider = QSlider(Qt.Horizontal)
        self.fake_gamma_slider.setRange(10, 40)
        self.fake_gamma_slider.setValue(22)
        self.fake_gamma_value = QLabel("2.20")
        self.fake_gamma_slider.valueChanged.connect(lambda v: self.fake_gamma_value.setText(f"{v/10:.2f}"))
        fake_gamma_row.addWidget(self.fake_gamma_slider)
        fake_gamma_row.addWidget(self.fake_gamma_value)
        fake_form.addRow("Gloss Gamma:", self._row_widget(fake_gamma_row))

        fake_metal_row = QHBoxLayout()
        self.fake_metal_suppression_slider = QSlider(Qt.Horizontal)
        self.fake_metal_suppression_slider.setRange(0, 100)
        self.fake_metal_suppression_slider.setValue(70)
        self.fake_metal_suppression_slider.setToolTip(
            "How much to darken albedo on metal pixels. "
            "0.00 = no darkening, 1.00 = fully darkened."
        )
        self.fake_metal_suppression_value = QLabel("0.70")
        self.fake_metal_suppression_slider.valueChanged.connect(
            lambda v: self.fake_metal_suppression_value.setText(f"{v/100:.2f}")
        )
        fake_metal_row.addWidget(self.fake_metal_suppression_slider)
        fake_metal_row.addWidget(self.fake_metal_suppression_value)
        fake_form.addRow("Metal Diffuse Suppression:", self._row_widget(fake_metal_row))

        fake_phong_row = QHBoxLayout()
        self.fake_phong_strength_slider = QSlider(Qt.Horizontal)
        self.fake_phong_strength_slider.setRange(0, 200)
        self.fake_phong_strength_slider.setValue(50)
        self.fake_phong_strength_slider.setToolTip(
            "Scales the phong mask (bump alpha) and phong exponent map. "
            "0.00 = no phong, 0.50 = halved (default), 1.00 = original strength."
        )
        self.fake_phong_strength_value = QLabel("0.50")
        self.fake_phong_strength_slider.valueChanged.connect(
            lambda v: self.fake_phong_strength_value.setText(f"{v/100:.2f}")
        )
        fake_phong_row.addWidget(self.fake_phong_strength_slider)
        fake_phong_row.addWidget(self.fake_phong_strength_value)
        fake_form.addRow("Phong Strength:", self._row_widget(fake_phong_row))

        # Exo PBR settings
        exo_widget = QWidget()
        exo_form = QFormLayout(exo_widget)

        self.exo_emission = QDoubleSpinBox()
        self.exo_emission.setRange(0.0, 10.0)
        self.exo_emission.setDecimals(2)
        self.exo_emission.setSingleStep(0.1)
        self.exo_emission.setValue(0.0)
        exo_form.addRow("$emissionscale:", self.exo_emission)

        self.exo_parallax = QDoubleSpinBox()
        self.exo_parallax.setRange(0.0, 1.0)
        self.exo_parallax.setDecimals(3)
        self.exo_parallax.setSingleStep(0.01)
        self.exo_parallax.setValue(0.0)
        exo_form.addRow("$parallaxscale:", self.exo_parallax)

        self.exo_alphablend = QCheckBox("Enable partial opacity")
        self.exo_alphablend.setChecked(False)
        exo_form.addRow("$alphablend:", self.exo_alphablend)

        self.mode_stack.addWidget(fake_widget)
        self.mode_stack.addWidget(exo_widget)

        mode_layout.addWidget(self.mode_stack)
        mode_group.setLayout(mode_layout)
        layout.addWidget(mode_group)

        # Results table
        self.results_table = QTableWidget(0, 10)
        self.results_table.setHorizontalHeaderLabels([
            "Include", "VMAT", "Color", "Normal", "AO", "Rough", "Metal", "Emissive", "Trans", "Notes"
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
        self.scan_btn = QPushButton("Scan VMATs")
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
        self._sync_mode_defaults(self.mode_combo.currentText())

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
            label = entry.get("label") or entry.get("vmat_root") or entry.get("output_root") or ts
            self.history_dropdown.addItem(f"{ts} — {label}" if ts else label)
        self.history_dropdown.blockSignals(False)

    def _make_history_entry(self) -> dict:
        vmat_root = self.vmat_root.text().strip()
        return {
            "timestamp": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "label": Path(vmat_root).name if vmat_root else self.output_root.text().strip(),
            "vmat_root": vmat_root,
            "texture_root": self.texture_root.text().strip(),
            "output_root": self.output_root.text().strip(),
            "mode": self.mode_combo.currentText(),
            "material_path": self.material_path.text().strip(),
            "append_material_subfolders": self.append_material_subfolders.isChecked(),
            "preserve_structure": self.preserve_structure.isChecked(),
            "prefix": self.prefix_input.text().strip(),
            "suffix": self.suffix_input.text().strip(),
            "generate_vtf": self.generate_vtf.isChecked(),
            "generate_vmt": self.generate_vmt.isChecked(),
            "generate_mipmaps": self.generate_mipmaps.isChecked(),
            "fake_ao_strength": self.fake_ao_slider.value() / 100.0,
            "fake_gloss_gamma": self.fake_gamma_slider.value() / 10.0,
            "fake_metal_diffuse_suppression": self.fake_metal_suppression_slider.value() / 100.0,
            "fake_phong_strength": self.fake_phong_strength_slider.value() / 100.0,
            "exo_emission": float(self.exo_emission.value()),
            "exo_parallax": float(self.exo_parallax.value()),
            "exo_alphablend": self.exo_alphablend.isChecked(),
        }

    def _save_current_run_to_history(self):
        entry = self._make_history_entry()
        if not entry.get("vmat_root") and not entry.get("output_root"):
            return

        keys = [
            "vmat_root", "texture_root", "output_root", "mode", "material_path",
            "append_material_subfolders", "preserve_structure", "prefix", "suffix",
            "generate_vtf", "generate_vmt", "generate_mipmaps", "fake_ao_strength",
            "fake_gloss_gamma", "fake_metal_diffuse_suppression", "fake_phong_strength",
            "exo_emission",
            "exo_parallax", "exo_alphablend"
        ]

        def _same(a: dict, b: dict) -> bool:
            return all(a.get(key) == b.get(key) for key in keys)

        if self.history and _same(self.history[0], entry):
            return
        self.history = [history_entry for history_entry in self.history if not _same(history_entry, entry)]
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

        self.vmat_root.setText(entry.get("vmat_root") or "")
        self.texture_root.setText(entry.get("texture_root") or "")
        self.output_root.setText(entry.get("output_root") or "")
        mode = entry.get("mode") or "Fake PBR"
        mode_index = self.mode_combo.findText(mode)
        if mode_index >= 0:
            self.mode_combo.setCurrentIndex(mode_index)
        self.material_path.setText(entry.get("material_path") or "models/ports")
        self.append_material_subfolders.setChecked(bool(entry.get("append_material_subfolders", True)))
        self.preserve_structure.setChecked(bool(entry.get("preserve_structure", True)))
        self.prefix_input.setText(entry.get("prefix") or "")
        self.suffix_input.setText(entry.get("suffix") or "")
        self.generate_vtf.setChecked(bool(entry.get("generate_vtf", True)))
        self.generate_vmt.setChecked(bool(entry.get("generate_vmt", True)))
        self.generate_mipmaps.setChecked(bool(entry.get("generate_mipmaps", True)))
        self.fake_ao_slider.setValue(int(float(entry.get("fake_ao_strength", 0.5)) * 100))
        self.fake_gamma_slider.setValue(int(float(entry.get("fake_gloss_gamma", 2.2)) * 10))
        self.fake_metal_suppression_slider.setValue(
            int(float(entry.get("fake_metal_diffuse_suppression", 0.7)) * 100)
        )
        self.fake_phong_strength_slider.setValue(
            int(float(entry.get("fake_phong_strength", 0.5)) * 100)
        )
        self.exo_emission.setValue(float(entry.get("exo_emission", 0.0)))
        self.exo_parallax.setValue(float(entry.get("exo_parallax", 0.0)))
        self.exo_alphablend.setChecked(bool(entry.get("exo_alphablend", False)))

    def _row(self, line_edit: QLineEdit, button: QPushButton) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(line_edit)
        row.addWidget(button)
        return w

    def _row_widget(self, layout: QHBoxLayout) -> QWidget:
        w = QWidget()
        layout.setContentsMargins(0, 0, 0, 0)
        w.setLayout(layout)
        return w

    def _browse_dir_into(self, line_edit: QLineEdit):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder", "")
        if folder:
            line_edit.setText(folder)

    def _sync_mode_defaults(self, mode: str):
        if mode == "Fake PBR":
            self.mode_stack.setCurrentIndex(0)
            if not self.material_path.text().strip():
                self.material_path.setText("models/ports")
        else:
            self.mode_stack.setCurrentIndex(1)
            if not self.material_path.text().strip():
                self.material_path.setText("exopbr")

    def scan(self):
        vmat_root = self.vmat_root.text().strip()
        texture_root = self.texture_root.text().strip()
        out_root = self.output_root.text().strip()
        if not vmat_root:
            self.log("VMAT root is required", "ERROR")
            return
        if not out_root:
            self.log("Output root is required", "ERROR")
            return

        vmat_root_path = Path(vmat_root)
        texture_root_path = Path(texture_root) if texture_root else vmat_root_path
        if not vmat_root_path.exists():
            self.log("VMAT root does not exist", "ERROR")
            return
        if texture_root and not texture_root_path.exists():
            self.log("Texture root does not exist", "ERROR")
            return

        self.clear_log()
        self.log("Scanning VMAT files...", "INFO")

        parser = VmatParser(texture_root_path)
        entries: List[VmatEntry] = []
        for base, _, files in os.walk(vmat_root_path):
            for fn in files:
                if Path(fn).suffix.lower() != ".vmat":
                    continue
                vmat_path = Path(base) / fn
                try:
                    entry = parser.parse_file(vmat_path, vmat_root_path)
                    entries.append(entry)
                except Exception as exc:
                    self.log(f"Failed to parse {vmat_path.name}: {exc}", "WARNING")

        self.entries = entries
        self._populate_table(entries)
        self.convert_btn.setEnabled(len(entries) > 0)
        self.log(f"Scan complete: {len(entries)} VMAT files detected", "INFO")

    def _populate_table(self, entries: List[VmatEntry]):
        self.results_table.setRowCount(0)
        self.sorted_entries = sorted(entries, key=lambda e: str(e.vmat_path))
        for entry in self.sorted_entries:
            row = self.results_table.rowCount()
            self.results_table.insertRow(row)

            include_item = QTableWidgetItem()
            include_item.setCheckState(Qt.Checked)
            include_item.setFlags(include_item.flags() | Qt.ItemIsUserCheckable)
            self.results_table.setItem(row, 0, include_item)

            name_item = QTableWidgetItem(entry.vmat_path.name)
            name_item.setToolTip(str(entry.vmat_path))
            self.results_table.setItem(row, 1, name_item)

            def _set_cell(col: int, path: Optional[Path], raw_key: str):
                raw = entry.raw_paths.get(raw_key)
                source = entry.sources.get(raw_key)
                if path:
                    txt = "Yes"
                    item = QTableWidgetItem(txt)
                    tooltip = str(path)
                    if source:
                        tooltip = f"{tooltip}\nSource: {source}"
                    item.setToolTip(tooltip)
                else:
                    txt = "No"
                    item = QTableWidgetItem(txt)
                    if raw:
                        item.setToolTip(f"Missing: {raw}")
                self.results_table.setItem(row, col, item)

            _set_cell(2, entry.textures.color, "color")
            _set_cell(3, entry.textures.normal, "normal")
            _set_cell(4, entry.textures.ao, "ao")
            _set_cell(5, entry.textures.roughness, "roughness")
            _set_cell(6, entry.textures.metallic, "metallic")
            _set_cell(7, entry.textures.emissive, "emissive")
            _set_cell(8, entry.textures.translucency, "translucency")

            mode_notes = []
            if entry.translucent:
                mode_notes.append("translucent")
            if entry.alphatest:
                mode_notes.append("alphatest")
            warn_text = "; ".join(mode_notes + entry.warnings)
            warn_item = QTableWidgetItem(warn_text)
            self.results_table.setItem(row, 9, warn_item)

    def _selected_entries(self) -> List[VmatEntry]:
        selected = []
        entries = self.sorted_entries or self.entries
        for row in range(self.results_table.rowCount()):
            if self.results_table.item(row, 0).checkState() != Qt.Checked:
                continue
            selected.append(entries[row])
        return selected

    def convert(self):
        vmat_root = self.vmat_root.text().strip()
        out_root = self.output_root.text().strip()
        if not vmat_root or not out_root:
            self.log("VMAT root and output root are required", "ERROR")
            return

        entries = self._selected_entries()
        if not entries:
            self.log("No VMATs selected", "WARNING")
            return

        self._save_current_run_to_history()

        material_path = self.material_path.text().strip()
        if not material_path:
            material_path = "exopbr" if self.mode_combo.currentText() == "Exo PBR" else "models/ports"

        self.convert_btn.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.progress.setVisible(True)
        self.progress.setRange(0, len(entries))
        self.progress.setValue(0)

        self.thread = VmatBatchRunner(
            entries=entries,
            vmat_root=Path(vmat_root),
            output_root=Path(out_root),
            preserve_structure=self.preserve_structure.isChecked(),
            mode=self.mode_combo.currentText(),
            material_path=material_path,
            append_material_subfolders=self.append_material_subfolders.isChecked(),
            prefix=self.prefix_input.text().strip(),
            suffix=self.suffix_input.text().strip(),
            fake_ao_strength=self.fake_ao_slider.value() / 100.0,
            fake_gloss_gamma=self.fake_gamma_slider.value() / 10.0,
            fake_metal_diffuse_suppression=self.fake_metal_suppression_slider.value() / 100.0,
            fake_phong_strength=self.fake_phong_strength_slider.value() / 100.0,
            exo_emission=float(self.exo_emission.value()),
            exo_parallax=float(self.exo_parallax.value()),
            exo_alphablend=self.exo_alphablend.isChecked(),
            generate_vtf=self.generate_vtf.isChecked(),
            generate_vmt=self.generate_vmt.isChecked(),
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
