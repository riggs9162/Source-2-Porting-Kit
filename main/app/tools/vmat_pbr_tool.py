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
    QTableWidgetItem, QHeaderView, QAbstractItemView, QSlider, QStackedWidget,
    QSplitter, QScrollArea, QGridLayout, QSizePolicy,
)
from PySide6.QtCore import Qt, QThread, Signal, QEvent

from .base_tool import BaseTool
from .fake_pbr_tool import FakePBRProcessor, ProcessingOptions, PBRInputs, _paint_table_row
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
    # When the source vmat declared a Texture* value as a vector literal
    # (e.g. `"TextureColor" "[1.0 1.0 1.0 0.0]"`) instead of a path, the
    # parsed RGBA components live here as a 4-tuple. The processors
    # materialise these as flat uniform images at process time when no
    # actual texture file resolves for that role. Only populated for roles
    # where a uniform tint is meaningful (color/ao/emissive/translucency).
    color_constant: Optional[Tuple[float, float, float, float]] = None
    ao_constant: Optional[Tuple[float, float, float, float]] = None
    emissive_constant: Optional[Tuple[float, float, float, float]] = None
    translucency_constant: Optional[Tuple[float, float, float, float]] = None


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
    # Self-illumination from F_SELF_ILLUM + g_vSelfIllumTint + g_flSelfIllumBrightness.
    # selfillum is the master toggle: when True, the FakePBR/ExoPBR processor
    # emits $selfillum 1 with the resolved emissive texture as $selfillummask.
    # Tint is folded with brightness into $selfillumtint at VMT-write time.
    selfillum: bool = False
    selfillum_tint: Optional[Tuple[float, float, float]] = None
    selfillum_brightness: Optional[float] = None


class VmatParser:
    """Parse VMAT text files and resolve texture paths."""

    KEY_PATTERN = re.compile(r'"(?P<key>Texture[A-Za-z0-9_]+)"\s+"(?P<value>[^"]+)"')
    SHADER_PATTERN = re.compile(r'"shader"\s+"(?P<value>[^"]+)"', re.IGNORECASE)
    FLAG_PATTERN = re.compile(r'"(?P<key>F_[A-Z_]+)"\s+"(?P<value>[01])"')
    SCALAR_PATTERN = re.compile(r'"(?P<key>g_fl[A-Za-z0-9_]+)"\s+"(?P<value>-?[0-9.]+)"')
    # Vector literals look like:  "g_vSelfIllumTint" "[1.000000 1.000000 1.000000 0.000000]"
    # We capture the bracketed value verbatim so the body can split it.
    VECTOR_PATTERN = re.compile(r'"(?P<key>g_v[A-Za-z0-9_]+)"\s+"(?P<value>\[[^"]+\])"')
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

    @staticmethod
    def _parse_literal_rgba(raw_val: str) -> Optional[Tuple[float, float, float, float]]:
        """Parse a bracketed vmat literal like `[1.0 1.0 1.0 0.0]` into RGBA.

        VRF emits Texture* keys with vector literals when the source vmdl used
        a solid colour input rather than a texture map. Three-component
        literals are right-padded with alpha=1.0; anything beyond four
        components is truncated.
        """
        inner = raw_val.strip().lstrip("[").rstrip("]")
        parts = [p for p in inner.replace(",", " ").split() if p]
        if len(parts) < 3:
            return None
        try:
            floats = [float(p) for p in parts[:4]]
        except ValueError:
            return None
        while len(floats) < 4:
            floats.append(1.0)
        return (floats[0], floats[1], floats[2], floats[3])

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

        vectors: Dict[str, str] = dict(self.VECTOR_PATTERN.findall(text))

        def _parse_vec3(key: str) -> Optional[Tuple[float, float, float]]:
            raw_val = vectors.get(key)
            if raw_val is None:
                return None
            inner = raw_val.strip().lstrip("[").rstrip("]")
            parts = [p for p in inner.replace(",", " ").split() if p]
            if len(parts) < 3:
                return None
            try:
                return (float(parts[0]), float(parts[1]), float(parts[2]))
            except ValueError:
                return None

        selfillum_tint = _parse_vec3("g_vSelfIllumTint")
        selfillum_brightness = _parse_scalar("g_flSelfIllumBrightness")

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

        # Roles that carry a meaningful uniform tint when the vmat declares a
        # Texture* value as a vector literal instead of a path. Normal /
        # roughness / metallic intentionally aren't here — there's no useful
        # interpretation of an RGBA literal as a flat normal, and roughness /
        # metallic already have dedicated `g_fl*` scalar fallbacks.
        roles_with_constant = ("color", "ao", "emissive", "translucency")

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
            # When the vmat declared a literal RGBA and no texture (path or
            # sibling) was found, capture the literal as a uniform tint so the
            # processor can synthesise a flat image instead of reporting the
            # role as missing. This is what VRF emits when the source vmdl
            # used a solid colour input rather than a texture map — e.g.
            # `"TextureColor" "[1.0 1.0 1.0 0.0]"`.
            constant: Optional[Tuple[float, float, float, float]] = None
            if raw_was_literal and resolved is None and role in roles_with_constant:
                constant = self._parse_literal_rgba(raw_val)
                if constant is not None:
                    sources[role] = (
                        f"VMAT literal "
                        f"[{constant[0]:.3f} {constant[1]:.3f} {constant[2]:.3f} {constant[3]:.3f}]"
                    )
            if role == "color":
                textures.color = resolved
                textures.color_constant = constant
            elif role == "normal":
                textures.normal = resolved
            elif role == "ao":
                textures.ao = resolved
                textures.ao_constant = constant
            elif role == "roughness":
                textures.roughness = resolved
            elif role == "metallic":
                textures.metallic = resolved
            elif role == "emissive":
                textures.emissive = resolved
                textures.emissive_constant = constant
            elif role == "translucency":
                textures.translucency = resolved
                textures.translucency_constant = constant
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

        # Self-illumination is enabled when either F_SELF_ILLUM is set OR a
        # selfillum mask was discovered (TextureSelfIllumMask, or a
        # *_selfillum sibling texture). The latter handles vmats that didn't
        # bother flipping the flag but still ship a glow mask.
        selfillum = flags.get("F_SELF_ILLUM") == "1"
        if textures.emissive is not None and not selfillum:
            selfillum = True
        if selfillum and textures.emissive is None:
            warnings.append("F_SELF_ILLUM set but no selfillum mask found")

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
            selfillum=selfillum,
            selfillum_tint=selfillum_tint,
            selfillum_brightness=selfillum_brightness,
        )


class VmatBatchRunner(QThread):
    progress = Signal(int, int, str)
    finished = Signal(bool, str)
    row_finished = Signal(int, bool)

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
        fake_phong_tint_mode: str,
        fake_colored_metal_relief: float,
        exo_emission: float,
        exo_parallax: float,
        exo_alphablend: bool,
        generate_vtf: bool,
        generate_vmt: bool,
        generate_mipmaps: bool,
        glow_mode: str = "selfillum",
        transparency_mode: str = "auto",
        skip_existing: bool = False,
        synthesize_missing_maps: bool = False,
        row_indices: Optional[List[int]] = None,
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
        # When the user types a path-shaped prefix (with slashes), the leading
        # path portion overrides the Material path used inside generated VMTs
        # and the trailing component is treated as a filename prefix. See
        # `_split_prefix` for the rule.
        self._prefix_path, self._prefix_tail = self._split_prefix(prefix)
        self.fake_ao_strength = fake_ao_strength
        self.fake_gloss_gamma = fake_gloss_gamma
        self.fake_metal_diffuse_suppression = fake_metal_diffuse_suppression
        self.fake_phong_strength = fake_phong_strength
        self.fake_phong_tint_mode = fake_phong_tint_mode
        self.fake_colored_metal_relief = fake_colored_metal_relief
        self.exo_emission = exo_emission
        self.exo_parallax = exo_parallax
        self.exo_alphablend = exo_alphablend
        self.generate_vtf = generate_vtf
        self.generate_vmt = generate_vmt
        self.generate_mipmaps = generate_mipmaps
        self.glow_mode = glow_mode
        self.transparency_mode = transparency_mode
        self.skip_existing = skip_existing
        self.synthesize_missing_maps = synthesize_missing_maps
        # Parallel list of QTableWidget row indices (-1 = no row), used to
        # report per-row completion back to the UI.
        self.row_indices = list(row_indices) if row_indices else [-1] * len(entries)

    def _output_dir_for(self, entry: VmatEntry) -> Path:
        # When the user supplied a path-shaped prefix, inject its path part
        # between the output root and any rel_dir so the .vmt lands at the
        # same place its own $basetexture path points to. Source 1 needs the
        # file at `<game>/materials/<material_path>/<name>.vmt` to find it.
        base = self.output_root
        if self._prefix_path:
            base = base / self._prefix_path
        if self.preserve_structure and entry.rel_dir is not None:
            return base / entry.rel_dir
        return base

    @staticmethod
    def _split_prefix(prefix: str) -> Tuple[str, str]:
        """Split the user's prefix into (material_path_part, filename_tail).

        A prefix containing a forward or back slash is treated as path-shaped:
        everything up to the LAST slash becomes a Material-path override; the
        remainder (which may be empty) is used as the filename prefix. A plain
        prefix with no slashes is returned as ('', prefix) — i.e. filename
        prefix only, no path override.

        Examples:
            ''                          -> ('', '')
            'foo_'                      -> ('', 'foo_')
            'models/riggs9162/hlvr/'    -> ('models/riggs9162/hlvr', '')
            'models/riggs9162/hlvr'     -> ('models/riggs9162', 'hlvr')
            'subdir/foo_'               -> ('subdir', 'foo_')
            'models\\foo\\'             -> ('models/foo', '')
        """
        if "/" not in prefix and "\\" not in prefix:
            return ("", prefix)
        norm = prefix.replace("\\", "/")
        head, _, tail = norm.rpartition("/")
        return (head, tail)

    def _material_path_for(self, entry: VmatEntry) -> str:
        # Path-shaped prefix overrides the Material-path field entirely.
        base = self._prefix_path if self._prefix_path else self.material_path
        if not self.append_material_subfolders:
            return base
        rel = entry.rel_dir.as_posix().strip("./")
        if rel:
            return f"{base}/{rel}"
        return base

    def _resolve_transparency(self, entry: VmatEntry) -> Tuple[bool, bool]:
        """Resolve the (translucent, alphatest) pair to use for one entry.

        ``transparency_mode`` of ``"auto"`` keeps whatever the parser detected
        from F_TRANSLUCENT / F_ALPHA_TEST / shader name. Forced modes only
        kick in when the entry already had *some* form of transparency
        intent — we don't add transparency to materials that were authored
        opaque, since that would silently break their basetexture alpha
        channel. ``"opaque"`` strips both flags so the material renders solid
        regardless of what the source vmat asked for.
        """
        translucent = entry.translucent
        alphatest = entry.alphatest
        mode = (self.transparency_mode or "auto").lower()
        if mode == "auto":
            return translucent, alphatest
        had_transparency = translucent or alphatest or entry.textures.translucency is not None
        if mode == "opaque":
            return False, False
        if not had_transparency:
            return translucent, alphatest
        if mode == "translucent":
            return True, False
        if mode == "alphatest":
            return False, True
        return translucent, alphatest

    def _should_skip(self, output_dir: Path, material_name: str) -> bool:
        """Decide whether to skip an entry because its outputs already exist.

        Treat the .vmt as the canonical "done" marker when VMT generation is
        on — it is the file the user actually edits, so we shouldn't clobber
        it. When VMT generation is off, fall back to the basetexture .vtf
        since that's the only reliable artefact the run produces.
        """
        if not self.skip_existing:
            return False
        if self.generate_vmt:
            if (output_dir / f"{material_name}.vmt").exists():
                return True
        if self.generate_vtf:
            base_vtf = "_color.vtf" if self.mode == "Fake PBR" else "_base.vtf"
            if (output_dir / f"{material_name}{base_vtf}").exists():
                return True
        return False

    def run(self):
        total = len(self.entries)
        if total == 0:
            self.finished.emit(False, "No VMAT files selected")
            return

        ok_count = 0
        skipped_count = 0
        for idx, entry in enumerate(self.entries, start=1):
            if self.isInterruptionRequested():
                self.finished.emit(False, f"Cancelled after {ok_count}/{total}")
                return

            row = self.row_indices[idx - 1] if idx - 1 < len(self.row_indices) else -1
            tex = entry.textures
            # Color is always required (can't invent the diffuse) — but a
            # vmat-declared RGBA literal counts: the processor materialises it
            # as a flat uniform image. Normal is required UNLESS the user
            # opted into blank-map synthesis, in which case the processor
            # will materialise a flat tangent normal.
            has_color = tex.color is not None or tex.color_constant is not None
            missing_required = (not has_color) or (
                tex.normal is None and not self.synthesize_missing_maps
            )
            if missing_required:
                if not has_color:
                    reason = "missing color"
                else:
                    reason = "missing normal (enable 'Synthesize missing maps' to bypass)"
                self.progress.emit(idx, total, f"✗ {entry.name}: {reason}")
                if row >= 0:
                    self.row_finished.emit(row, False)
                continue

            output_dir = self._output_dir_for(entry)
            material_name = f"{self._prefix_tail}{entry.name}{self.suffix}"
            mat_path = self._material_path_for(entry)

            if self._should_skip(output_dir, material_name):
                skipped_count += 1
                self.progress.emit(idx, total, f"↷ {entry.name}: skipped (already exists)")
                # Don't paint the row green — it's not a fresh success — but
                # also don't leave it red. row_finished emits its own status,
                # so we just skip the signal and let the row stay neutral.
                continue

            os.makedirs(output_dir, exist_ok=True)

            translucent, alphatest = self._resolve_transparency(entry)

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
                        phong_tint_mode=self.fake_phong_tint_mode,
                        colored_metal_relief=self.fake_colored_metal_relief,
                        translucent=translucent,
                        alphatest=alphatest,
                        glow_mode=self.glow_mode,
                        synthesize_missing_maps=self.synthesize_missing_maps,
                    )
                    processor = FakePBRProcessor(options)
                    # Selfillum mask is only fed in when the vmat actually
                    # asked for it (F_SELF_ILLUM or an emissive sibling). The
                    # mask file alone without entry.selfillum is dropped on the
                    # floor — Source 2's emission textures don't always mean
                    # Source 1 selfillum is appropriate.
                    selfillum_path = (
                        str(tex.emissive) if entry.selfillum and tex.emissive else None
                    )
                    selfillum_constant = (
                        tex.emissive_constant if entry.selfillum and not tex.emissive else None
                    )
                    inputs = PBRInputs(
                        color=str(tex.color) if tex.color else None,
                        normal=str(tex.normal),
                        ao=str(tex.ao) if tex.ao else None,
                        roughness=str(tex.roughness) if tex.roughness else None,
                        metallic=str(tex.metallic) if tex.metallic else None,
                        translucency=str(tex.translucency) if tex.translucency else None,
                        metallic_constant=entry.metallic_constant,
                        roughness_constant=entry.roughness_constant,
                        selfillum=selfillum_path,
                        selfillum_tint=entry.selfillum_tint,
                        selfillum_brightness=entry.selfillum_brightness,
                        color_constant=tex.color_constant,
                        ao_constant=tex.ao_constant,
                        translucency_constant=tex.translucency_constant,
                        selfillum_constant=selfillum_constant,
                    )
                    try:
                        success, msg = processor.process_material(inputs, str(output_dir), material_name, mat_path)
                    finally:
                        processor.shutdown()
                else:
                    # Force alphablend on when the source vmat declares translucency,
                    # regardless of UI checkbox — the vmat is authoritative,
                    # subject to the user's transparency-mode override above.
                    alphablend = self.exo_alphablend or translucent or alphatest
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
                        color=str(tex.color) if tex.color else None,
                        normal=str(tex.normal),
                        ao=str(tex.ao) if tex.ao else None,
                        roughness=str(tex.roughness) if tex.roughness else None,
                        metallic=str(tex.metallic) if tex.metallic else None,
                        selfillum=str(tex.emissive) if tex.emissive else None,
                        transparency_mask=str(tex.translucency) if tex.translucency else None,
                        metallic_constant=entry.metallic_constant,
                        roughness_constant=entry.roughness_constant,
                        color_constant=tex.color_constant,
                        ao_constant=tex.ao_constant,
                        selfillum_constant=tex.emissive_constant,
                        transparency_mask_constant=tex.translucency_constant,
                    )
                    try:
                        success, msg = processor.process_material(inputs, str(output_dir), material_name, mat_path)
                    finally:
                        processor.shutdown()
            except Exception as exc:
                self.progress.emit(idx, total, f"✗ {entry.name}: {exc}")
                if row >= 0:
                    self.row_finished.emit(row, False)
                continue

            if success:
                ok_count += 1
                self.progress.emit(idx, total, f"✓ {entry.name} done")
            else:
                self.progress.emit(idx, total, f"✗ {entry.name}: {msg}")
            if row >= 0:
                self.row_finished.emit(row, success)

        summary = f"Processed {ok_count}/{total} materials"
        if skipped_count:
            summary += f" ({skipped_count} skipped, already exists)"
        self.finished.emit(True, summary)


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
        """Two-pane layout: settings on the left (scrollable), results on
        the right (always visible). The splitter handle lets the user grow
        either side; default 1:2 ratio keeps the results table primary."""
        root = QHBoxLayout()
        self.content_layout.addLayout(root)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_settings_pane())
        splitter.addWidget(self._build_results_pane())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([420, 760])
        root.addWidget(splitter)

        # Apply mode-driven defaults once the widgets are wired.
        self._sync_mode_defaults(self.mode_combo.currentText())

    # ------------------------------------------------------------------
    # Pane builders
    # ------------------------------------------------------------------

    def _build_settings_pane(self) -> QWidget:
        """Left pane: scrollable column of settings groupboxes."""
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
        col.addWidget(self._build_conversion_group())
        col.addWidget(self._build_mode_settings_group())
        col.addWidget(self._build_requirements_group())
        col.addStretch()

        scroll.setWidget(container)
        scroll.setMinimumWidth(380)
        return scroll

    def _build_folders_group(self) -> QGroupBox:
        """History dropdown + the three required folder paths, stacked."""
        group = QGroupBox("Folders & History")
        form = QFormLayout()

        self.history_dropdown = QComboBox()
        self.history_dropdown.addItem("-- Recent runs --")
        self.history_dropdown.currentIndexChanged.connect(self.on_history_selected)
        form.addRow("Recent run:", self.history_dropdown)
        self._refresh_history_dropdown()

        self.vmat_root = QLineEdit()
        self.vmat_root.textChanged.connect(self._on_vmat_root_changed)
        vmat_btn = QPushButton("Browse...")
        vmat_btn.clicked.connect(lambda: self._browse_dir_into(self.vmat_root))
        form.addRow("VMAT root:", self._row(self.vmat_root, vmat_btn))

        self.texture_root = QLineEdit()
        self.texture_root.setPlaceholderText("Optional; same-folder textures auto-detected")
        tex_btn = QPushButton("Browse...")
        tex_btn.clicked.connect(lambda: self._browse_dir_into(self.texture_root))
        form.addRow("Texture root:", self._row(self.texture_root, tex_btn))

        self.output_root = QLineEdit()
        out_btn = QPushButton("Browse...")
        out_btn.clicked.connect(lambda: self._browse_dir_into(self.output_root))
        form.addRow("Output root:", self._row(self.output_root, out_btn))

        group.setLayout(form)
        return group

    def _build_output_group(self) -> QGroupBox:
        """Mode + naming + which artefact types to write."""
        group = QGroupBox("Output")
        form = QFormLayout()

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Fake PBR", "Exo PBR"])
        self.mode_combo.currentTextChanged.connect(self._sync_mode_defaults)
        form.addRow("Mode:", self.mode_combo)

        self.material_path = QLineEdit()
        self.material_path.setText("models/ports")
        self.material_path.setPlaceholderText("models/ports (Fake) or exopbr (Exo)")
        self.material_path.setToolTip(
            "Source 1 material reference path — what comes after `materials/` "
            "when the engine looks up a material.\n\n"
            "Used in two places:\n"
            "  1. As the $basetexture/$bumpmap prefix inside generated .vmt files. "
            "A material path of 'models/props/hazmat' produces e.g. "
            "$basetexture \"models/props/hazmat/foo_color\".\n"
            "  2. As the output subdirectory under the chosen output root.\n\n"
            "Convention: this is the path tail under your materialsrc/materials/ "
            "tree. For materialsrc/materials/models/props/hazmat/, set this to "
            "'models/props/hazmat'. The Auto button does this for you.\n\n"
            "Auto-fill: when you set the VMAT root, the field updates "
            "automatically as long as it still holds a default ('', 'models/ports', "
            "'exopbr') or a previously auto-derived value. Once you type your own "
            "value, manual edits are sticky — the field stops auto-changing."
        )
        # Track the most recent auto-derived value so that auto-fill on VMAT
        # root change can update freely until the user manually edits — at which
        # point we stop overwriting and let the manual value stick.
        self._last_auto_material_path: Optional[str] = None
        auto_btn = QPushButton("Auto")
        auto_btn.setToolTip(
            "Force-derive the material path from VMAT root: take everything "
            "after the last 'materials' component.\n\n"
            "Examples:\n"
            "  <addon>/materialsrc/materials/models/props/hazmat → models/props/hazmat\n"
            "  <addon>/materialsrc/materials/cable → cable\n"
            "  <addon>/materialsrc/materials → (no change — too generic)\n\n"
            "Overwrites the field even if you've manually edited it."
        )
        auto_btn.clicked.connect(lambda: self._auto_fill_material_path(force=True))
        form.addRow("Material path:", self._row(self.material_path, auto_btn))

        name_row = QHBoxLayout()
        self.prefix_input = QLineEdit()
        self.prefix_input.setPlaceholderText("Prefix (e.g. 'pre_' or 'models/riggs9162/hlvr/')")
        self.prefix_input.setToolTip(
            "Prepended to each generated material name.\n\n"
            "Plain prefix (no slashes): used as a filename prefix only.\n"
            "  'pre_' + 'cable.vmat' -> 'pre_cable.vmt'\n\n"
            "Path-shaped prefix (contains '/' or '\\\\'): the part up to the "
            "LAST slash overrides the Material path field AND the on-disk "
            "output directory for this batch; the remainder (if any) is the "
            "filename prefix.\n"
            "  'models/riggs9162/hlvr/' -> Material path = "
            "'models/riggs9162/hlvr', filename prefix = (none)\n"
            "  'subdir/foo_'            -> Material path = 'subdir', "
            "filename prefix = 'foo_'\n\n"
            "Both effects fire together: the .vmt lands at "
            "<output>/<prefix-path>/<rel_dir>/<name>.vmt, matching its own "
            "$basetexture reference so Source 1 can find the file at runtime."
        )
        self.suffix_input = QLineEdit()
        self.suffix_input.setPlaceholderText("Suffix")
        self.suffix_input.setToolTip(
            "Appended to each generated material name (filename only). "
            "Slash-shaped suffixes are not interpreted as paths."
        )
        name_row.addWidget(self.prefix_input)
        name_row.addWidget(self.suffix_input)
        form.addRow("Prefix / Suffix:", self._row_widget(name_row))

        self.append_material_subfolders = QCheckBox("Append VMAT subfolders to material path")
        self.append_material_subfolders.setChecked(True)
        self.append_material_subfolders.setToolTip(
            "When enabled, each VMAT's location relative to the VMAT root is "
            "appended to the material path used for that file's output. Lets a "
            "single batch run cover several material subfolders with their "
            "structure preserved.\n\n"
            "Example — VMAT root = materialsrc/materials/, material path = "
            "models/props:\n"
            "  materials/models/props/hazmat/foo.vmat → output material path "
            "'models/props/hazmat'\n"
            "  materials/models/props/cable/bar.vmat → output material path "
            "'models/props/cable'\n\n"
            "Disable when your VMAT root already points at the exact target "
            "folder and you want every VMT to share the same material path "
            "verbatim."
        )
        form.addRow("", self.append_material_subfolders)

        self.preserve_structure = QCheckBox("Preserve VMAT folder structure in output")
        self.preserve_structure.setChecked(True)
        form.addRow("", self.preserve_structure)

        # Generation toggles in a grid — they're peers, no reason to waste
        # full-width form rows on them.
        gen_grid = QGridLayout()
        gen_grid.setContentsMargins(0, 0, 0, 0)
        gen_grid.setHorizontalSpacing(12)
        self.generate_vtf = QCheckBox("Generate VTF")
        self.generate_vtf.setChecked(True)
        self.generate_vmt = QCheckBox("Generate VMT")
        self.generate_vmt.setChecked(True)
        self.generate_mipmaps = QCheckBox("Generate Mipmaps")
        self.generate_mipmaps.setChecked(True)
        self.skip_existing = QCheckBox("Skip already-processed files")
        self.skip_existing.setChecked(False)
        self.skip_existing.setToolTip(
            "Skip any VMAT whose output .vmt already exists in the destination "
            "folder. Useful when re-running a large batch after fixing a few "
            "inputs — leaves your hand-edited VMTs untouched. Falls back to "
            "checking the basetexture .vtf when 'Generate VMT' is off."
        )
        self.synthesize_missing = QCheckBox("Synthesize missing maps")
        self.synthesize_missing.setChecked(False)
        self.synthesize_missing.setToolTip(
            "When enabled, fills in missing maps with neutral defaults instead "
            "of failing the conversion:\n"
            "• Normal → flat tangent-space (no bump)\n"
            "• Roughness → uniform 0.5\n"
            "• Metallic → uniform 0.0 (dielectric)\n"
            "Useful for quick ports of vmats that ship only a colour map. "
            "g_flMetalness / g_flRoughness scalars in the vmat still take "
            "priority over the blank fallback when present."
        )
        gen_grid.addWidget(self.generate_vtf, 0, 0)
        gen_grid.addWidget(self.generate_vmt, 0, 1)
        gen_grid.addWidget(self.generate_mipmaps, 1, 0)
        gen_grid.addWidget(self.skip_existing, 1, 1)
        gen_grid.addWidget(self.synthesize_missing, 2, 0, 1, 2)
        form.addRow("Generate:", self._wrap_layout(gen_grid))

        group.setLayout(form)
        return group

    def _build_conversion_group(self) -> QGroupBox:
        """Glow + transparency overrides — apply to either Fake or Exo."""
        group = QGroupBox("Conversion Overrides")
        form = QFormLayout()

        self.glow_mode_combo = QComboBox()
        self.glow_mode_combo.addItem("Self-illum ($selfillum)", "selfillum")
        self.glow_mode_combo.addItem("Emissive Blend ($EmissiveBlend*)", "emissiveblend")
        self.glow_mode_combo.setToolTip(
            "Technique used to emit glow VMT params when a vmat declares "
            "F_SELF_ILLUM or ships a *_selfillum mask.\n"
            "• Self-illum: classic $selfillum + $selfillummask. Breaks with "
            "$translucent / $alphatest on some branches.\n"
            "• Emissive Blend: $EmissiveBlend* family. Plays nicely with "
            "$translucent and $phong; recommended for L4D2 / Alyx-port targets."
        )
        form.addRow("Glow mode:", self.glow_mode_combo)

        self.transparency_mode_combo = QComboBox()
        self.transparency_mode_combo.addItem("Auto (use vmat flags)", "auto")
        self.transparency_mode_combo.addItem("Force translucent ($translucent)", "translucent")
        self.transparency_mode_combo.addItem("Force alphatest ($alphatest)", "alphatest")
        self.transparency_mode_combo.addItem("Force opaque (no transparency)", "opaque")
        self.transparency_mode_combo.setToolTip(
            "Override the transparency technique chosen for materials whose "
            "source vmat declares F_TRANSLUCENT / F_ALPHA_TEST or ships a "
            "translucency mask.\n"
            "• Auto: trust the vmat's own flags.\n"
            "• Force translucent: emit $translucent 1 + $nocull.\n"
            "• Force alphatest: emit $alphatest 1 + $alphatestreference 0.5.\n"
            "• Force opaque: strip both flags entirely.\n"
            "Force translucent / alphatest only apply to materials that were "
            "already detected as transparent — opaque materials are left alone."
        )
        form.addRow("Transparency:", self.transparency_mode_combo)

        group.setLayout(form)
        return group

    def _build_mode_settings_group(self) -> QGroupBox:
        """Stacked mode-specific knobs (Fake PBR sliders OR Exo PBR fields).
        The QStackedWidget is swapped by ``_sync_mode_defaults`` whenever
        the Mode combo in the Output group changes."""
        group = QGroupBox("Mode Settings")
        layout = QVBoxLayout()
        self.mode_stack = QStackedWidget()
        self.mode_stack.addWidget(self._build_fake_pbr_widget())
        self.mode_stack.addWidget(self._build_exo_pbr_widget())
        layout.addWidget(self.mode_stack)
        group.setLayout(layout)
        return group

    def _build_fake_pbr_widget(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)

        self.fake_ao_slider, self.fake_ao_value = self._make_slider(0, 200, 50, 0.01)
        form.addRow("AO Strength:", self._slider_row(self.fake_ao_slider, self.fake_ao_value))

        self.fake_gamma_slider, self.fake_gamma_value = self._make_slider(10, 40, 22, 0.1)
        form.addRow("Gloss Gamma:", self._slider_row(self.fake_gamma_slider, self.fake_gamma_value))

        self.fake_metal_suppression_slider, self.fake_metal_suppression_value = self._make_slider(
            0, 100, 70, 0.01,
        )
        self.fake_metal_suppression_slider.setToolTip(
            "How much to darken albedo on metal pixels. "
            "0.00 = no darkening, 1.00 = fully darkened."
        )
        form.addRow(
            "Metal Diffuse Suppression:",
            self._slider_row(self.fake_metal_suppression_slider, self.fake_metal_suppression_value),
        )

        self.fake_phong_strength_slider, self.fake_phong_strength_value = self._make_slider(
            0, 200, 50, 0.01,
        )
        self.fake_phong_strength_slider.setToolTip(
            "Scales the phong mask (bump alpha) and phong exponent map. "
            "0.00 = no phong, 0.50 = halved (default), 1.00 = original strength."
        )
        form.addRow(
            "Phong Strength:",
            self._slider_row(self.fake_phong_strength_slider, self.fake_phong_strength_value),
        )

        self.fake_phong_tint_mode_combo = QComboBox()
        self.fake_phong_tint_mode_combo.addItem("Off", "off")
        self.fake_phong_tint_mode_combo.addItem("Selective (recommended)", "selective")
        self.fake_phong_tint_mode_combo.addItem("Blanket", "blanket")
        self.fake_phong_tint_mode_combo.setCurrentIndex(1)
        self.fake_phong_tint_mode_combo.setToolTip(
            "Compensates the phong mask for $phongalbedotint runtime tinting. "
            "Selective: colored metals are boosted, dielectric phong is suppressed. "
            "Blanket: divide-by-luminance compensation everywhere. "
            "No effect on targets without $phongalbedotint."
        )
        form.addRow("Phong Tint Mode:", self.fake_phong_tint_mode_combo)

        self.fake_colored_metal_relief_slider, self.fake_colored_metal_relief_value = self._make_slider(
            0, 100, 50, 0.01,
        )
        self.fake_colored_metal_relief_slider.setToolTip(
            "Per-pixel relief on Metal Diffuse Suppression for chromatic metals. "
            "Only applied when Phong Tint Mode is not Off."
        )
        form.addRow(
            "Colored Metal Relief:",
            self._slider_row(self.fake_colored_metal_relief_slider, self.fake_colored_metal_relief_value),
        )

        return widget

    def _build_exo_pbr_widget(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)

        self.exo_emission = QDoubleSpinBox()
        self.exo_emission.setRange(0.0, 10.0)
        self.exo_emission.setDecimals(2)
        self.exo_emission.setSingleStep(0.1)
        self.exo_emission.setValue(0.0)
        form.addRow("$emissionscale:", self.exo_emission)

        self.exo_parallax = QDoubleSpinBox()
        self.exo_parallax.setRange(0.0, 1.0)
        self.exo_parallax.setDecimals(3)
        self.exo_parallax.setSingleStep(0.01)
        self.exo_parallax.setValue(0.0)
        form.addRow("$parallaxscale:", self.exo_parallax)

        self.exo_alphablend = QCheckBox("Enable partial opacity")
        self.exo_alphablend.setChecked(False)
        form.addRow("$alphablend:", self.exo_alphablend)

        return widget

    def _build_requirements_group(self) -> QGroupBox:
        """Texture-role filters laid out as a compact 2-column grid so they
        don't run off the side of the narrow settings pane.
        Color/Normal default on (matching the runner's hard validation);
        the rest default off so existing scans behave as before. Metallic /
        Roughness count as 'present' when the VMAT provides a g_flMetalness /
        g_flRoughness scalar even without a texture — those cases produce a
        synthesised uniform map at processing time."""
        group = QGroupBox("Required maps (filter)")
        grid = QGridLayout()
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setHorizontalSpacing(12)
        self.req_checkboxes: Dict[str, QCheckBox] = {}
        roles = (
            ("Color", "color", True),
            ("Normal", "normal", True),
            ("AO", "ao", False),
            ("Roughness", "roughness", False),
            ("Metallic", "metallic", False),
            ("Emissive", "emissive", False),
            ("Translucency", "translucency", False),
        )
        for idx, (label, key, default) in enumerate(roles):
            cb = QCheckBox(label)
            cb.setChecked(default)
            cb.setToolTip(
                f"Require a {label.lower()} map for a VMAT to be processed. "
                f"Rows missing this role will be auto-unchecked in the table."
            )
            cb.stateChanged.connect(self._apply_requirements_filter)
            grid.addWidget(cb, idx // 2, idx % 2)
            self.req_checkboxes[key] = cb
        group.setLayout(grid)
        return group

    def _build_results_pane(self) -> QWidget:
        """Right pane: scan controls, selection bar, results table, run buttons."""
        pane = QWidget()
        col = QVBoxLayout(pane)
        col.setContentsMargins(6, 0, 0, 0)
        col.setSpacing(6)

        # Scan + selection bar on a single line so the table stays as tall
        # as possible — these are the one-shot "before each batch" controls.
        action_row = QHBoxLayout()
        self.scan_btn = QPushButton("Scan VMATs")
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
        # "Selected" here means the row(s) highlighted via click / shift-click /
        # ctrl-click — same selection model as Windows Explorer. These buttons
        # apply the action to the highlighted rows only, leaving the rest alone.
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

        # Results table — set to expand into all available space.
        self.results_table = QTableWidget(0, 10)
        self.results_table.setHorizontalHeaderLabels([
            "Include", "VMAT", "Color", "Normal", "AO", "Rough", "Metal", "Emissive", "Trans", "Notes"
        ])
        header = self.results_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        # Enable Explorer-style multi-row selection: click → single, Shift+Click
        # → contiguous range, Ctrl+Click → toggle individual rows. This is Qt's
        # default for QTableWidget but we set it explicitly so it stays robust
        # if a stylesheet or future change ever flips it.
        self.results_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.results_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.results_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # Capture Space key on the table to toggle Include for *all* highlighted
        # rows. Default Qt behavior toggles only the focused cell's checkbox.
        self.results_table.installEventFilter(self)
        col.addWidget(self.results_table, 1)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        col.addWidget(self.progress)

        # Run controls live next to the table where the user actually clicks
        # them, instead of being buried below all the settings.
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

    def _make_slider(
        self, lo: int, hi: int, initial: int, step: float,
    ) -> Tuple[QSlider, QLabel]:
        """Build a horizontal slider + value label, wired so the label
        always shows the current scaled value."""
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
        return self._row_widget(row)

    @staticmethod
    def _wrap_layout(layout) -> QWidget:
        w = QWidget()
        w.setLayout(layout)
        return w

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
            "fake_phong_tint_mode": self.fake_phong_tint_mode_combo.currentData() or "selective",
            "fake_colored_metal_relief": self.fake_colored_metal_relief_slider.value() / 100.0,
            "exo_emission": float(self.exo_emission.value()),
            "exo_parallax": float(self.exo_parallax.value()),
            "exo_alphablend": self.exo_alphablend.isChecked(),
            "glow_mode": self.glow_mode_combo.currentData() or "selfillum",
            "transparency_mode": self.transparency_mode_combo.currentData() or "auto",
            "skip_existing": self.skip_existing.isChecked(),
            "synthesize_missing_maps": self.synthesize_missing.isChecked(),
            "requirements": {key: cb.isChecked() for key, cb in self.req_checkboxes.items()},
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
            "fake_phong_tint_mode", "fake_colored_metal_relief",
            "exo_emission",
            "exo_parallax", "exo_alphablend", "glow_mode", "transparency_mode",
            "skip_existing", "synthesize_missing_maps", "requirements"
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
        self.fake_colored_metal_relief_slider.setValue(
            int(float(entry.get("fake_colored_metal_relief", 0.5)) * 100)
        )
        tint_mode_val = str(entry.get("fake_phong_tint_mode", "selective"))
        tint_idx = self.fake_phong_tint_mode_combo.findData(tint_mode_val)
        if tint_idx >= 0:
            self.fake_phong_tint_mode_combo.setCurrentIndex(tint_idx)
        self.exo_emission.setValue(float(entry.get("exo_emission", 0.0)))
        self.exo_parallax.setValue(float(entry.get("exo_parallax", 0.0)))
        self.exo_alphablend.setChecked(bool(entry.get("exo_alphablend", False)))
        glow_mode_val = str(entry.get("glow_mode", "selfillum"))
        glow_idx = self.glow_mode_combo.findData(glow_mode_val)
        if glow_idx >= 0:
            self.glow_mode_combo.setCurrentIndex(glow_idx)
        transparency_mode_val = str(entry.get("transparency_mode", "auto"))
        transparency_idx = self.transparency_mode_combo.findData(transparency_mode_val)
        if transparency_idx >= 0:
            self.transparency_mode_combo.setCurrentIndex(transparency_idx)
        self.skip_existing.setChecked(bool(entry.get("skip_existing", False)))
        self.synthesize_missing.setChecked(bool(entry.get("synthesize_missing_maps", False)))
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

    def _row_widget(self, layout: QHBoxLayout) -> QWidget:
        w = QWidget()
        layout.setContentsMargins(0, 0, 0, 0)
        w.setLayout(layout)
        return w

    def _browse_dir_into(self, line_edit: QLineEdit):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder", "")
        if folder:
            line_edit.setText(folder)

    # ------------------------------------------------------------------
    # Auto-derive material path from VMAT root
    # ------------------------------------------------------------------

    # Values the auto-fill is willing to overwrite without explicit user
    # confirmation. "" / mode defaults / a previously auto-derived value all
    # count as "not manually edited."
    _DEFAULT_MATERIAL_PATHS = {"", "models/ports", "exopbr"}

    @staticmethod
    def _derive_material_path_from(vmat_root: str) -> str:
        """Find the last `materials` component in `vmat_root` and return the
        path tail after it, joined with forward slashes.

        Examples:
            <addon>/materialsrc/materials/models/props/hazmat -> models/props/hazmat
            <addon>/materialsrc/materials/cable               -> cable
            <addon>/materialsrc/materials                     -> ""
            <addon>/random/folder                             -> ""
        """
        if not vmat_root:
            return ""
        try:
            parts = Path(vmat_root).parts
        except (TypeError, ValueError):
            return ""
        idxs = [i for i, p in enumerate(parts) if p.lower() == "materials"]
        if not idxs:
            return ""
        tail = parts[idxs[-1] + 1:]
        return "/".join(tail)

    def _on_vmat_root_changed(self, _text: str):
        self._auto_fill_material_path(force=False)

    def _auto_fill_material_path(self, force: bool = False):
        """Populate Material path from VMAT root.

        `force=True` (the Auto button) overwrites whatever's there.
        `force=False` (text-changed signal) only overwrites if the field still
        looks default — empty string, a mode-default, or the previously
        auto-derived value. This keeps manual edits sticky.
        """
        derived = self._derive_material_path_from(self.vmat_root.text().strip())
        if not derived:
            return
        current = self.material_path.text().strip()
        if not force:
            looks_default = (
                current in self._DEFAULT_MATERIAL_PATHS
                or current == self._last_auto_material_path
            )
            if not looks_default:
                return
        self.material_path.setText(derived)
        self._last_auto_material_path = derived

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
        excluded = self._apply_requirements_filter()
        msg = f"Scan complete: {len(entries)} VMAT files detected"
        if excluded:
            msg += f" ({excluded} excluded by requirements)"
        self.log(msg, "INFO")

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
                # Vmat literal constants count as "available" — the processor
                # synthesises a uniform image from them at runtime, so the
                # cell shouldn't read "No" the way a truly missing role does.
                constant = getattr(entry.textures, f"{raw_key}_constant", None)
                if path:
                    item = QTableWidgetItem("Yes")
                    tooltip = str(path)
                    if source:
                        tooltip = f"{tooltip}\nSource: {source}"
                    item.setToolTip(tooltip)
                elif constant is not None:
                    item = QTableWidgetItem("Const")
                    tooltip = (
                        f"VMAT literal: "
                        f"[{constant[0]:.3f} {constant[1]:.3f} {constant[2]:.3f} {constant[3]:.3f}]"
                    )
                    if source:
                        tooltip = f"{tooltip}\nSource: {source}"
                    item.setToolTip(tooltip)
                else:
                    item = QTableWidgetItem("No")
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
            if entry.selfillum:
                mode_notes.append("selfillum")
            warn_text = "; ".join(mode_notes + entry.warnings)
            warn_item = QTableWidgetItem(warn_text)
            self.results_table.setItem(row, 9, warn_item)

    def _selected_entries(self) -> Tuple[List[VmatEntry], List[int]]:
        selected: List[VmatEntry] = []
        rows: List[int] = []
        entries = self.sorted_entries or self.entries
        for row in range(self.results_table.rowCount()):
            if self.results_table.item(row, 0).checkState() != Qt.Checked:
                continue
            selected.append(entries[row])
            rows.append(row)
        return selected, rows

    def _set_all_results_selected(self, checked: bool):
        """Bulk toggle every Include checkbox in the results table."""
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self.results_table.rowCount()):
            item = self.results_table.item(row, 0)
            if item is not None:
                item.setCheckState(state)

    def _invert_results_selection(self):
        """Flip every Include checkbox in the results table."""
        for row in range(self.results_table.rowCount()):
            item = self.results_table.item(row, 0)
            if item is None:
                continue
            item.setCheckState(
                Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked
            )

    def _highlighted_rows(self) -> List[int]:
        """Return distinct row indices highlighted in the results table.

        Uses ``selectedIndexes`` rather than ``selectedRows`` so a partial
        selection (e.g. just one cell) still returns its row — matches how
        Explorer treats a single-cell click as selecting the row.
        """
        sel_model = self.results_table.selectionModel()
        if sel_model is None:
            return []
        rows = sorted({idx.row() for idx in sel_model.selectedIndexes()})
        return rows

    def _set_selected_rows_checked(self, checked: bool):
        """Set Include for every highlighted row (Shift/Ctrl-click selection)."""
        state = Qt.Checked if checked else Qt.Unchecked
        for row in self._highlighted_rows():
            item = self.results_table.item(row, 0)
            if item is not None:
                item.setCheckState(state)

    def _toggle_selected_rows(self):
        """Flip Include for every highlighted row.

        When the highlighted rows have mixed Include states, normalise the
        toggle by treating ``majority checked`` as ``all → unchecked`` and
        vice-versa — the same fence-sit behaviour Explorer's checkbox uses
        when you bulk-toggle a multi-selection."""
        rows = self._highlighted_rows()
        if not rows:
            return
        checked_count = sum(
            1 for row in rows
            if self.results_table.item(row, 0) is not None
            and self.results_table.item(row, 0).checkState() == Qt.Checked
        )
        # If at least half are checked, uncheck them all; otherwise check
        # them all. Predictable single-tap behaviour for a mixed selection.
        new_state = Qt.Unchecked if checked_count * 2 >= len(rows) else Qt.Checked
        for row in rows:
            item = self.results_table.item(row, 0)
            if item is not None:
                item.setCheckState(new_state)

    def eventFilter(self, obj, event):
        """Intercept Space on the results table to bulk-toggle highlighted rows.

        Without this, Qt's default Space handler only toggles the *focused*
        cell's checkbox even when many rows are highlighted, which feels wrong
        coming from Explorer where Space ticks the whole multi-selection.
        """
        if obj is self.results_table and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Space, Qt.Key_Select):
                self._toggle_selected_rows()
                return True
        return super().eventFilter(obj, event)

    def convert(self):
        vmat_root = self.vmat_root.text().strip()
        out_root = self.output_root.text().strip()
        if not vmat_root or not out_root:
            self.log("VMAT root and output root are required", "ERROR")
            return

        entries, row_indices = self._selected_entries()
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
            fake_phong_tint_mode=self.fake_phong_tint_mode_combo.currentData() or "selective",
            fake_colored_metal_relief=self.fake_colored_metal_relief_slider.value() / 100.0,
            exo_emission=float(self.exo_emission.value()),
            exo_parallax=float(self.exo_parallax.value()),
            exo_alphablend=self.exo_alphablend.isChecked(),
            generate_vtf=self.generate_vtf.isChecked(),
            generate_vmt=self.generate_vmt.isChecked(),
            generate_mipmaps=self.generate_mipmaps.isChecked(),
            glow_mode=self.glow_mode_combo.currentData() or "selfillum",
            transparency_mode=self.transparency_mode_combo.currentData() or "auto",
            skip_existing=self.skip_existing.isChecked(),
            synthesize_missing_maps=self.synthesize_missing.isChecked(),
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
        """Texture roles currently required for a VMAT to be eligible."""
        if not hasattr(self, "req_checkboxes"):
            return []
        return [key for key, cb in self.req_checkboxes.items() if cb.isChecked()]

    @staticmethod
    def _entry_has_role(entry: VmatEntry, role: str) -> bool:
        """Whether `entry` satisfies a given role requirement.

        Metallic/Roughness count as present when the VMAT carries a uniform
        scalar (g_flMetalness / g_flRoughness) since the processor synthesises
        a flat map for those at runtime. Color/AO/Emissive/Translucency count
        as present when the vmat declared a Texture* vector literal — the
        processor materialises those into uniform images the same way.
        """
        tex = entry.textures
        path = getattr(tex, role, None)
        if path is not None:
            return True
        if role == "metallic" and entry.metallic_constant is not None:
            return True
        if role == "roughness" and entry.roughness_constant is not None:
            return True
        constant = getattr(tex, f"{role}_constant", None)
        if constant is not None:
            return True
        return False

    def _apply_requirements_filter(self) -> int:
        """Auto-uncheck rows whose underlying entry is missing any required role.

        Returns the count of rows that ended up unchecked because they failed
        the current requirement set. Re-checking a row manually is still
        allowed; the filter only fires on requirement-toggle and post-scan.
        """
        if not hasattr(self, "results_table"):
            return 0
        entries = self.sorted_entries or self.entries
        if not entries:
            return 0
        required = self._required_roles()
        if not required:
            for row in range(self.results_table.rowCount()):
                include_item = self.results_table.item(row, 0)
                if include_item is not None:
                    include_item.setToolTip("")
            return 0
        excluded = 0
        for row in range(self.results_table.rowCount()):
            include_item = self.results_table.item(row, 0)
            if include_item is None or row >= len(entries):
                continue
            entry = entries[row]
            missing = [r for r in required if not self._entry_has_role(entry, r)]
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
