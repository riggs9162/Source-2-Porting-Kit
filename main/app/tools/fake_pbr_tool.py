"""
FakePBR Tool - Convert Source 2 PBR textures to Source 1 materials

This tool converts up to 5 Source 2 textures into a full Source 1-compatible
material set with proper Phong, envmap masking, and VTF encoding.
"""

import os
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass
import numpy as np
from pathlib import Path
import json
from datetime import datetime

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFileDialog,
    QLineEdit, QGroupBox, QSlider, QDoubleSpinBox, QCheckBox,
    QProgressBar, QFormLayout, QWidget, QComboBox, QTabWidget,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QSpinBox, QSplitter, QScrollArea, QSizePolicy, QGridLayout,
)
from PySide6.QtCore import Qt, QThread, Signal, QEvent
from PySide6.QtGui import QColor, QBrush
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Event


_ROW_OK_BRUSH = QBrush(QColor(76, 175, 80, 90))
_ROW_FAIL_BRUSH = QBrush(QColor(244, 67, 54, 90))


def _paint_table_row(table, row: int, success: bool) -> None:
    """Tint every QTableWidgetItem in `row` to indicate completion status."""
    if row < 0 or row >= table.rowCount():
        return
    brush = _ROW_OK_BRUSH if success else _ROW_FAIL_BRUSH
    for col in range(table.columnCount()):
        item = table.item(row, col)
        if item is not None:
            item.setBackground(brush)

from .base_tool import BaseTool
from ..utils.vtf_encoder import VTFEncoder, VTFEncoderError
from ..utils.helpers import get_config_dir
from ..utils.image_processing import load_image, resize_to_match, to_uint8
from ..utils.pbr_processing import (
    process_fakepbr_base_texture,
    pack_normal_with_phong_mask,
    create_phong_exponent_texture,
    create_colored_envmap_mask,
    compute_fakepbr_material_stats
)
from ..utils.vmt_generator import generate_fakepbr_vmt


class ProcessingCancelled(Exception):
    """Internal signal to abort processing early when user cancels."""
    pass


@dataclass
class PBRInputs:
    """Container for PBR input textures"""
    color: Optional[str] = None
    normal: Optional[str] = None
    ao: Optional[str] = None
    roughness: Optional[str] = None
    metallic: Optional[str] = None
    # Optional opacity / alpha map. When present, the alpha channel is
    # baked into the basetexture so $translucent / $alphatest takes effect.
    translucency: Optional[str] = None
    # Uniform PBR scalars used as fallbacks when no metallic/roughness texture
    # is provided. These come from Source 2 vmat fields (g_flMetalness /
    # g_flRoughness) and apply to the entire material. When set, the processor
    # synthesises a flat single-value array so downstream math behaves the same
    # as if a uniform texture had been authored.
    metallic_constant: Optional[float] = None
    roughness_constant: Optional[float] = None
    # Self-illumination mask. RGB-as-color-map: non-black pixels glow at their
    # authored color, scaled by selfillum_tint × selfillum_brightness in the
    # VMT. Encoded as a separate ``_selfillum.vtf`` so the basetexture's alpha
    # stays free for $translucent / $alphatest baking.
    selfillum: Optional[str] = None
    selfillum_tint: Optional[Tuple[float, float, float]] = None
    selfillum_brightness: Optional[float] = None
    # Uniform RGBA tints for roles where the source vmat declared a Texture*
    # value as a vector literal (e.g. `"TextureColor" "[1.0 1.0 1.0 0.0]"`)
    # rather than a path. Only used when the corresponding texture path is
    # absent — actual textures always win. Materialised as a 4×4 flat image
    # at the same float32 RGBA layout as `load_image` returns.
    color_constant: Optional[Tuple[float, float, float, float]] = None
    ao_constant: Optional[Tuple[float, float, float, float]] = None
    translucency_constant: Optional[Tuple[float, float, float, float]] = None
    selfillum_constant: Optional[Tuple[float, float, float, float]] = None


@dataclass
class ProcessingOptions:
    """Options for PBR processing"""
    ao_strength: float = 0.7
    gloss_gamma: float = 2.0
    generate_vtf: bool = True
    generate_vmt: bool = True
    generate_mipmaps: bool = True
    target_branch: str = "gmod"
    shader: str = "VertexLitGeneric"
    envmap: str = "env_cubemap"
    metal_diffuse_suppression: float = 0.7
    envmask_gamma: float = 1.5
    invert_green: bool = False
    # Scales the phong mask (bump alpha) and phong exponent map.
    # 0.5 halves the prior unscaled output, which read too hot in-engine.
    phong_strength: float = 0.5
    # How the phong mask compensates for $phongalbedotint at runtime.
    # "off"        — original behaviour, no compensation.
    # "selective"  — colored-metal pixels are boosted via divide-by-luminance,
    #                dielectric pixels are suppressed (envmap handles their spec).
    # "blanket"    — divide-by-luminance everywhere.
    phong_tint_mode: str = "selective"
    # Per-pixel relief on metal_diffuse_suppression for chromatic metals
    # (gold/copper/brass) so they retain saturation in the basetexture and
    # provide a brighter source for $phongalbedotint to multiply.
    colored_metal_relief: float = 0.5
    # Transparency mode. translucent → $translucent 1; alphatest → $alphatest 1
    # with $alphatestreference. Mutually exclusive (translucent wins if both).
    translucent: bool = False
    alphatest: bool = False
    # Glow technique used when a selfillum mask is supplied via PBRInputs.
    # "selfillum"     — emit $selfillum 1 / $selfillummask. Standard, but
    #                   incompatible with $translucent on some branches and
    #                   may collide with $phong on bumped materials.
    # "emissiveblend" — emit $EmissiveBlend* family. Plays nicely with
    #                   $translucent and $phong and is the recommended
    #                   technique on L4D2 / Alyx-port targets.
    glow_mode: str = "selfillum"
    # When True, missing normal/roughness/metallic maps are filled in with
    # blank "neutral" data instead of failing the conversion. Useful for
    # quick-and-dirty ports of vmats that ship only a colour map.
    #   Normal     → flat tangent-space (0.5, 0.5, 1.0)
    #   Roughness  → uniform 0.5 (mid)
    #   Metallic   → uniform 0.0 (dielectric)
    # The vmat's g_flMetalness / g_flRoughness scalars still take priority
    # over the blank fallback when present.
    synthesize_missing_maps: bool = False


class FakePBRProcessor:
    """
    Core processing engine for converting PBR textures to Source 1 format
    """
    
    def __init__(self, options: ProcessingOptions):
        self.options = options
        self.encoder = VTFEncoder()
        self.log_callback = None  # Set by ProcessingThread for GUI logging
        self._cancel_cb: Optional[Callable[[], bool]] = None

    def set_canceller(self, cb: Optional[Callable[[], bool]]):
        """Provide a callback that returns True when a cancellation is requested."""
        self._cancel_cb = cb

    def _check_cancel(self):
        if self._cancel_cb and self._cancel_cb():
            raise ProcessingCancelled()
    
    def log(self, message: str):
        """Log message to console and GUI if callback is set"""
        print(message)
        if self.log_callback:
            self.log_callback(message)

    @staticmethod
    def _uniform_rgba_image(rgba: Tuple[float, float, float, float]) -> np.ndarray:
        """Build a 4×4 RGBA float32 image filled with `rgba` (values in [0, 1]).

        Same layout as `load_image` returns, so downstream resize / processing
        treats a synthesised uniform identically to a 1×1 PNG that had been
        decoded.
        """
        clamped = [float(np.clip(c, 0.0, 1.0)) for c in rgba]
        return np.tile(
            np.array([[clamped]], dtype=np.float32),
            (4, 4, 1),
        )
    
    def process_material(
        self,
        inputs: PBRInputs,
        output_folder: str,
        material_name: str,
        material_path: str = "models/ports"
    ) -> Tuple[bool, str]:
        """
        Process a complete material set
        
        Args:
            inputs: PBRInputs with paths to source textures
            output_folder: Folder to save output files
            material_name: Base name for output files
            material_path: Relative path in materials folder
            
        Returns:
            (success, message)
        """
        try:
            self._check_cancel()
            # Load all inputs
            self.log("[FakePBR] Loading input textures...")
            color_data = load_image(inputs.color)
            if color_data is not None:
                self.log(f"  ✓ Loaded color map: {os.path.basename(inputs.color)}")
            elif inputs.color_constant is not None:
                # VRF emits `"TextureColor" "[r g b a]"` when the source vmdl
                # used a solid colour input. Materialise the literal as a
                # uniform image so downstream processing runs unchanged.
                color_data = self._uniform_rgba_image(inputs.color_constant)
                rgba = inputs.color_constant
                self.log(
                    f"  ✓ Synthesised uniform color from TextureColor literal "
                    f"[{rgba[0]:.3f} {rgba[1]:.3f} {rgba[2]:.3f} {rgba[3]:.3f}]"
                )
            self._check_cancel()
            
            normal_data = load_image(inputs.normal)
            if normal_data is not None:
                self.log(f"  ✓ Loaded normal map: {os.path.basename(inputs.normal)}")
            elif self.options.synthesize_missing_maps:
                # Tangent-space "no bump": (X=0, Y=0, Z=1) packed as RGB
                # (0.5, 0.5, 1.0) in [0, 1]. Float32 here, resized + uint8'd
                # downstream by the same pipeline as authored normals.
                normal_data = np.tile(
                    np.array([[[0.5, 0.5, 1.0, 1.0]]], dtype=np.float32),
                    (4, 4, 1),
                )
                self.log("  ✓ Synthesised blank flat normal (no bump, full opacity)")
            self._check_cancel()

            ao_data = load_image(inputs.ao)
            if ao_data is not None:
                self.log(f"  ✓ Loaded AO map: {os.path.basename(inputs.ao)}")
            elif inputs.ao_constant is not None:
                ao_data = self._uniform_rgba_image(inputs.ao_constant)
                self.log(
                    f"  ✓ Synthesised uniform AO from TextureAmbientOcclusion literal "
                    f"= {inputs.ao_constant[0]:.3f}"
                )
            else:
                self.log("  ℹ No AO map provided (will use default)")
            self._check_cancel()

            roughness_data = load_image(inputs.roughness)
            if roughness_data is not None:
                self.log(f"  ✓ Loaded roughness map: {os.path.basename(inputs.roughness)}")
            elif inputs.roughness_constant is not None:
                value = float(np.clip(inputs.roughness_constant, 0.0, 1.0))
                roughness_data = np.full((4, 4, 1), value, dtype=np.float32)
                self.log(f"  ✓ Synthesised uniform roughness from g_flRoughness = {value:.3f}")
            elif self.options.synthesize_missing_maps:
                roughness_data = np.full((4, 4, 1), 0.5, dtype=np.float32)
                self.log("  ✓ Synthesised blank roughness (uniform 0.5)")
            else:
                self.log("  ℹ No roughness map provided (will use default: 0.5)")
            self._check_cancel()

            metallic_data = load_image(inputs.metallic)
            if metallic_data is not None:
                self.log(f"  ✓ Loaded metallic map: {os.path.basename(inputs.metallic)}")
            elif inputs.metallic_constant is not None:
                value = float(np.clip(inputs.metallic_constant, 0.0, 1.0))
                metallic_data = np.full((4, 4, 1), value, dtype=np.float32)
                self.log(f"  ✓ Synthesised uniform metallic from g_flMetalness = {value:.3f}")
            elif self.options.synthesize_missing_maps:
                metallic_data = np.full((4, 4, 1), 0.0, dtype=np.float32)
                self.log("  ✓ Synthesised blank metallic (uniform 0.0 dielectric)")
            else:
                self.log("  ℹ No metallic map provided (will use dielectric)")
            self._check_cancel()

            translucency_data = load_image(inputs.translucency)
            if translucency_data is not None:
                self.log(f"  ✓ Loaded translucency map: {os.path.basename(inputs.translucency)}")
            elif inputs.translucency_constant is not None:
                translucency_data = self._uniform_rgba_image(inputs.translucency_constant)
                rgba = inputs.translucency_constant
                self.log(
                    f"  ✓ Synthesised uniform translucency from TextureTranslucency literal "
                    f"(alpha = {rgba[3]:.3f})"
                )
            self._check_cancel()

            selfillum_data = load_image(inputs.selfillum)
            if selfillum_data is not None:
                self.log(f"  ✓ Loaded selfillum mask: {os.path.basename(inputs.selfillum)}")
            elif inputs.selfillum_constant is not None:
                selfillum_data = self._uniform_rgba_image(inputs.selfillum_constant)
                rgba = inputs.selfillum_constant
                self.log(
                    f"  ✓ Synthesised uniform selfillum from TextureSelfIllumMask literal "
                    f"[{rgba[0]:.3f} {rgba[1]:.3f} {rgba[2]:.3f}]"
                )
            self._check_cancel()

            # Validate required inputs
            if color_data is None:
                return False, "Color map is required"

            if normal_data is None:
                return False, "Normal map is required"

            # Determine target resolution: use LARGEST dimension from all inputs
            # This prevents downsampling high-res textures
            all_inputs = [color_data, normal_data, ao_data, roughness_data, metallic_data, translucency_data, selfillum_data]
            max_height = max(img.shape[0] for img in all_inputs if img is not None)
            max_width = max(img.shape[1] for img in all_inputs if img is not None)
            height, width = max_height, max_width
            
            self.log(f"[FakePBR] Processing at resolution: {width}x{height}")
            self.log(f"[FakePBR] AO Strength: {self.options.ao_strength:.2f}")
            self.log(f"[FakePBR] Gloss Gamma: {self.options.gloss_gamma:.2f}")
            self.log(f"[FakePBR] Target Branch: {self.options.target_branch}")
            self._check_cancel()
            
            # Resize all inputs to match target resolution
            color_data = resize_to_match(color_data, height, width, "color")
            normal_data = resize_to_match(normal_data, height, width, "normal")
            ao_data = resize_to_match(ao_data, height, width, "AO")
            roughness_data = resize_to_match(roughness_data, height, width, "roughness")
            metallic_data = resize_to_match(metallic_data, height, width, "metallic")
            translucency_data = resize_to_match(translucency_data, height, width, "translucency")
            selfillum_data = resize_to_match(selfillum_data, height, width, "selfillum")
            self._check_cancel()

            stats = compute_fakepbr_material_stats(roughness_data, metallic_data, height, width)

            # Process base texture
            self.log(f"[FakePBR] Processing base texture...")
            self.log(f"  → Baking AO with power curve (strength: {self.options.ao_strength:.2f})")
            self.log(f"  → Darkening metallic diffuse regions")
            if translucency_data is not None:
                self.log(f"  → Baking opacity into base texture alpha")
            # Determine whether the target VMT will emit $phongalbedotint, in
            # which case we run the matching tint-aware mask compensation.
            from ..utils.vmt_generator import SOURCE1_TARGET_CAPABILITIES as _S1_CAPS
            target_caps = _S1_CAPS.get(self.options.target_branch, _S1_CAPS.get("hl2", {}))
            tint_mode_active = self.options.phong_tint_mode if target_caps.get("phong_albedo_tint", False) else "off"
            if self.options.phong_tint_mode != "off" and tint_mode_active == "off":
                self.log(
                    f"  ℹ Target '{self.options.target_branch}' lacks $phongalbedotint; "
                    f"phong tint mode forced off."
                )

            base_texture = process_fakepbr_base_texture(
                color_data,
                ao_data,
                metallic_data,
                self.options.ao_strength,
                self.options.metal_diffuse_suppression,
                translucency=translucency_data,
                colored_metal_relief=self.options.colored_metal_relief if tint_mode_active != "off" else 0.0,
            )
            self.log(f"  ✓ Base texture processed")
            self._check_cancel()

            # Process normal map
            self.log(f"[FakePBR] Packing normal map...")
            self.log(f"  → Preserving RGB normal channels")
            self.log(f"  → Computing bump alpha Phong mask (tint mode: {tint_mode_active})")
            normal_texture = pack_normal_with_phong_mask(
                normal_data,
                ao_data,
                metallic_data,
                roughness_data,
                self.options.invert_green,
                phong_strength=self.options.phong_strength,
                color=color_data if tint_mode_active != "off" else None,
                tint_mode=tint_mode_active,
                metal_diffuse_suppression=self.options.metal_diffuse_suppression,
                colored_metal_relief=self.options.colored_metal_relief if tint_mode_active != "off" else 0.0,
            )
            self.log(f"  ✓ Normal map packed with Phong mask in alpha")
            self._check_cancel()

            # Process phong/gloss map
            self.log(f"[FakePBR] Computing phong exponent texture...")
            self.log(f"  → Converting roughness to gloss: (1-r)^{self.options.gloss_gamma:.2f}")
            self.log(f"  → Packing R=exponent, G=metallic, A=rim")
            phong_texture = create_phong_exponent_texture(
                roughness_data, metallic_data, ao_data, self.options.gloss_gamma, height, width,
                phong_strength=self.options.phong_strength
            )
            self.log(f"  ✓ Phong exponent texture computed")
            self._check_cancel()

            # Process colored envmap mask
            self.log(f"[FakePBR] Computing colored envmap mask...")
            self.log(f"  → Packing RGB colored metal tint / roughness intensity")
            envmask_texture = create_colored_envmap_mask(
                color_data,
                ao_data,
                metallic_data,
                roughness_data,
                self.options.envmask_gamma
            )
            self.log(f"  ✓ Colored envmap mask computed")
            self._check_cancel()
            
            # Conditionally encode to VTF
            vtf_generated = False
            selfillum_vtf_emitted = False
            if self.options.generate_vtf:
                self.log(f"[FakePBR] Encoding textures to VTF...")

                base_path = os.path.join(output_folder, f"{material_name}_color.vtf")
                normal_path = os.path.join(output_folder, f"{material_name}_normal.vtf")
                phong_path = os.path.join(output_folder, f"{material_name}_phong.vtf")
                envmask_path = os.path.join(output_folder, f"{material_name}_envmask.vtf")
                self._check_cancel()
                self.encoder.encode_base_texture(base_texture, base_path, generate_mipmaps=self.options.generate_mipmaps)
                self.log(f"  ✓ Encoded {material_name}_color.vtf")
                self._check_cancel()
                self.encoder.encode_normal_map(normal_texture, normal_path, generate_mipmaps=self.options.generate_mipmaps)
                self.log(f"  ✓ Encoded {material_name}_normal.vtf")
                self._check_cancel()
                self.encoder.encode_phong_map(phong_texture, phong_path, generate_mipmaps=self.options.generate_mipmaps)
                self.log(f"  ✓ Encoded {material_name}_phong.vtf")
                self._check_cancel()
                self.encoder.encode_envmap_mask(envmask_texture, envmask_path, generate_mipmaps=self.options.generate_mipmaps)
                self.log(f"  ✓ Encoded {material_name}_envmask.vtf")
                if selfillum_data is not None:
                    self._check_cancel()
                    selfillum_path = os.path.join(output_folder, f"{material_name}_selfillum.vtf")
                    selfillum_rgba = to_uint8(selfillum_data, clip=True)
                    self.encoder.encode_selfillum_mask(
                        selfillum_rgba, selfillum_path,
                        generate_mipmaps=self.options.generate_mipmaps,
                    )
                    self.log(f"  ✓ Encoded {material_name}_selfillum.vtf")
                    selfillum_vtf_emitted = True
                vtf_generated = True
            else:
                self.log(f"[FakePBR] Skipping VTF generation (option disabled)")
            
            # Optionally generate VMT
            vmt_path = os.path.join(output_folder, f"{material_name}.vmt")
            if self.options.generate_vmt:
                self._check_cancel()
                self.log(f"[FakePBR] Generating VMT...")
                # Transparency: $translucent and $alphatest both rely on the base
                # texture's alpha channel, which we bake above when a translucency
                # input is supplied. The VMT also needs to disable the
                # $normalmapalphaenvmapmask path because that conflicts with the
                # alpha-blended draw — Source 1 cannot use both at once.
                custom_params: Dict[str, Any] = {}
                if self.options.translucent:
                    custom_params['"$translucent"'] = "1"
                elif self.options.alphatest:
                    custom_params['"$alphatest"'] = "1"
                    custom_params['"$alphatestreference"'] = "0.5"
                if self.options.translucent or self.options.alphatest:
                    custom_params['"$nocull"'] = "0"
                # Glow params. Two techniques, selected by options.glow_mode:
                #   selfillum     — classic $selfillum + $selfillummask; brightness
                #                   is folded into $selfillumtint (older Source
                #                   branches lack $selfillummaskscale).
                #   emissiveblend — $EmissiveBlend* family, recommended when the
                #                   material also uses $translucent / $phong /
                #                   $alphatest, where $selfillum tends to break.
                if inputs.selfillum is not None or inputs.selfillum_constant is not None:
                    glow_path = f"{material_path}/{material_name}_selfillum"
                    tint = inputs.selfillum_tint or (1.0, 1.0, 1.0)
                    brightness = inputs.selfillum_brightness if inputs.selfillum_brightness is not None else 1.0
                    glow_mode = (self.options.glow_mode or "selfillum").lower()
                    if glow_mode == "emissiveblend":
                        custom_params['"$EmissiveBlendEnabled"'] = "1"
                        # $EmissiveBlendStrength clamps to [0, 1] in the
                        # shader, so anything brighter has to ride along in
                        # the tint. Split brightness into the two slots:
                        # strength absorbs everything up to 1.0, the rest
                        # multiplies through the per-channel tint.
                        b_val = max(0.0, float(brightness))
                        strength = min(1.0, b_val)
                        tint_scale = max(1.0, b_val)
                        custom_params['"$EmissiveBlendStrength"'] = f"{strength:.3f}"
                        # $EmissiveBlendTexture is required even when static; the
                        # wiki recommends a stock white as a placeholder.
                        custom_params['"$EmissiveBlendTexture"'] = "vgui/white"
                        custom_params['"$EmissiveBlendBaseTexture"'] = glow_path
                        custom_params['"$EmissiveBlendFlowTexture"'] = "vgui/white"
                        custom_params['"$EmissiveBlendTint"'] = (
                            f"[{max(0.0, float(tint[0]) * tint_scale):.3f} "
                            f"{max(0.0, float(tint[1]) * tint_scale):.3f} "
                            f"{max(0.0, float(tint[2]) * tint_scale):.3f}]"
                        )
                        custom_params['"$EmissiveBlendScrollVector"'] = "[0 0]"
                    else:
                        custom_params['"$selfillum"'] = "1"
                        custom_params['"$selfillummask"'] = glow_path
                        scaled_tint = (
                            max(0.0, float(tint[0]) * brightness),
                            max(0.0, float(tint[1]) * brightness),
                            max(0.0, float(tint[2]) * brightness),
                        )
                        if scaled_tint != (1.0, 1.0, 1.0):
                            custom_params['"$selfillumtint"'] = (
                                f"[{scaled_tint[0]:.3f} {scaled_tint[1]:.3f} {scaled_tint[2]:.3f}]"
                            )
                generate_fakepbr_vmt(
                    vmt_path,
                    material_name,
                    material_path,
                    shader=self.options.shader,
                    target_branch=self.options.target_branch,
                    envmap=self.options.envmap,
                    stats=stats,
                    has_envmap_mask=True,
                    custom_params=custom_params or None,
                    tint_mode_used=tint_mode_active,
                )
                self.log(f"  ✓ Generated {material_name}.vmt")
            else:
                self.log(f"[FakePBR] Skipping VMT generation (option disabled)")
            
            # Build success message depending on what was generated
            files = []
            if self.options.generate_vmt:
                files.append(f" - {material_name}.vmt")
            if vtf_generated:
                files.extend([
                    f" - {material_name}_color.vtf",
                    f" - {material_name}_normal.vtf",
                    f" - {material_name}_phong.vtf",
                    f" - {material_name}_envmask.vtf"
                ])
                if selfillum_vtf_emitted:
                    files.append(f" - {material_name}_selfillum.vtf")
            success_msg = "[SUCCESS] Created files:\n" + "\n".join(files)
            
            return True, success_msg
            
        except ProcessingCancelled:
            return False, "Cancelled by user"
        except VTFEncoderError as e:
            return False, f"VTF Encoding Error: {str(e)}"
        except Exception as e:
            return False, f"Processing Error: {str(e)}"
    
    def shutdown(self):
        """Clean up encoder"""
        if self.encoder:
            self.encoder.shutdown()


class ProcessingThread(QThread):
    """Background thread for material processing"""
    
    progress = Signal(str)
    finished = Signal(bool, str)
    
    def __init__(self, processor, inputs, output_folder, material_name, material_path):
        super().__init__()
        self.processor = processor
        self.inputs = inputs
        self.output_folder = output_folder
        self.material_name = material_name
        self.material_path = material_path
        # Store reference to emit progress
        self.processor.log_callback = self.log_progress
        # Provide cancellation callback to processor
        try:
            self.processor.set_canceller(self.isInterruptionRequested)
        except Exception:
            pass
    
    def log_progress(self, message: str):
        """Emit progress message"""
        self.progress.emit(message)
    
    def run(self):
        """Run processing in background"""
        success, message = self.processor.process_material(
            self.inputs,
            self.output_folder,
            self.material_name,
            self.material_path
        )
        self.finished.emit(success, message)


class AutomationThread(QThread):
    """Background thread to process multiple detected materials automatically"""

    progress = Signal(str)
    finished = Signal(bool, str)
    row_finished = Signal(int, bool)

    def __init__(self, processor_factory, tasks, max_workers: int = 2):
        super().__init__()
        self.processor_factory = processor_factory  # function to create a FakePBRProcessor
        # Each task is a dict that may include a 'row' key carrying the
        # originating QTableWidget row index so the UI can paint per-row
        # completion status without depending on submission order.
        self.tasks = tasks
        self.max_workers = max(1, int(max_workers))

    def run(self):
        total = len(self.tasks)
        ok_count = 0
        cancelled = False

        def worker(idx: int, task: dict):
            if self.isInterruptionRequested():
                return (idx, task['material_name'], False, 'Cancelled', task.get('row', -1))
            proc = self.processor_factory()
            # Prefix all logs from this task with its index/total
            proc.log_callback = lambda m, i=idx, t=total: self.progress.emit(f"[{i}/{t}] {m}")
            try:
                proc.set_canceller(self.isInterruptionRequested)
            except Exception:
                pass
            try:
                success, msg = proc.process_material(
                    task['inputs'],
                    task['output_folder'],
                    task['material_name'],
                    task.get('material_path', 'models/ports')
                )
            finally:
                try:
                    proc.shutdown()
                except Exception:
                    pass
            return (idx, task['material_name'], success, msg, task.get('row', -1))

        # Submit tasks to a thread pool with a bounded number of workers
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {}
            for idx, task in enumerate(self.tasks, start=1):
                if self.isInterruptionRequested():
                    cancelled = True
                    break
                futures[executor.submit(worker, idx, task)] = (idx, task['material_name'], task.get('row', -1))

            # Process completions as they finish
            for future in as_completed(futures):
                if self.isInterruptionRequested():
                    cancelled = True
                    break
                try:
                    idx, name, success, msg, row = future.result()
                except Exception as e:
                    idx, name, row = futures[future]
                    success = False
                    msg = f"Processing Error: {e}"
                if success:
                    ok_count += 1
                    self.progress.emit(f"[{idx}/{total}] ✓ {name} done")
                else:
                    # If the processor cooperatively cancelled
                    if isinstance(msg, str) and msg.lower().startswith("cancelled"):
                        cancelled = True
                    self.progress.emit(f"[{idx}/{total}] ✗ {name} failed: {msg}")
                if row >= 0:
                    self.row_finished.emit(row, success)

        if cancelled or self.isInterruptionRequested():
            self.finished.emit(False, f"Cancelled after {ok_count}/{total} materials")
        else:
            all_ok = ok_count == total
            self.finished.emit(all_ok, f"Processed {ok_count}/{total} materials")


class FakePBRTool(BaseTool):
    """
    FakePBR Tool GUI Widget
    """
    
    def __init__(self):
        super().__init__("FakePBR Tool")
        self.processor = None
        self.processing_thread = None
        self.automation_thread = None
        # History file locations
        try:
            cfg_dir = get_config_dir()
            self._history_file = cfg_dir / 'fakepbr_history.json'
            self._auto_history_file = cfg_dir / 'fakepbr_auto_history.json'
        except Exception:
            # Fallback: use local config path
            cfg = Path(__file__).parent.parent / 'config'
            self._history_file = cfg / 'fakepbr_history.json'
            self._auto_history_file = cfg / 'fakepbr_auto_history.json'

        # Separate history lists for manual and automation tabs
        self.history = []        # manual runs
        self.auto_history = []   # automation runs

        self._load_history()
        self._load_auto_history()
        self.setup_content()

        # Inbuilt keyword config for scanning
        self._keyword_config = {
            # Common suffixes/keywords found at the end of filenames for each texture type
            # Keep most-common first; we'll prefer the earliest match when needed
            'color': [
                "color", "albedo", "basecolor", "base_color", "basecolour", "base_colour",
                "diffuse", "base", "col", "bc", "basecol", "c", "diff", "d"
            ],
            'normal': [
                "normal_opengl", "normal", "nrm", "normals", "normalgl", "normal_dx", "nrml", "nor", "n"
            ],
            'ao': [
                "ao", "ambientocclusion", "ambient_occlusion", "occlusion", "occ", "aoc", "ao_map", "mixed_ao"
            ],
            'roughness': [
                "roughness", "rough", "rgh", "r", "rghness"
            ],
            'metallic': [
                "metallic", "metal", "metalness", "mtl", "m"
            ],
        }
        # Ensure all keywords are lowercased
        for typ in self._keyword_config:
            self._keyword_config[typ] = [k.lower() for k in self._keyword_config[typ]]
        self._image_exts = {".png", ".jpg", ".jpeg", ".tga", ".bmp", ".tif", ".tiff"}
        # Map each suffix keyword back to its canonical type for quick lookup
        self._suffix_to_type: Dict[str, str] = {}
        for _typ, _keys in self._keyword_config.items():
            for _k in _keys:
                self._suffix_to_type[_k] = _typ
        # Common trailing tokens that are not part of texture-type suffixes (e.g., resolutions)
        self._trailing_ignore_tokens = {
            "1k", "2k", "4k", "8k", "16k", "32k", "512", "1024", "2048", "4096", "8192",
            "hi", "lo", "low", "high", "tile", "tiling"
        }
    
    def setup_content(self):
        """Setup the tool's UI content with Manual and Automate tabs"""
        tabs = QTabWidget()
        self.content_layout.addWidget(tabs)

        # ---------- Manual Tab (existing UI) ----------
        manual_tab = QWidget()
        manual_layout = QVBoxLayout(manual_tab)

        # History dropdown (previous runs) - moved to top of manual tab
        history_group = QGroupBox("Previous Runs")
        history_layout = QFormLayout()
        self.history_dropdown = QComboBox()
        self.history_dropdown.addItem("-- Recent runs --")
        self.history_dropdown.currentIndexChanged.connect(self.on_history_selected)
        history_layout.addRow("Select Run:", self.history_dropdown)
        history_group.setLayout(history_layout)
        manual_layout.addWidget(history_group)

        # Input files group
        input_group = QGroupBox("Input Textures")
        input_layout = QFormLayout()
        
        self.color_input = self.create_file_input()
        self.normal_input = self.create_file_input()
        self.ao_input = self.create_file_input()
        self.roughness_input = self.create_file_input()
        self.metallic_input = self.create_file_input()
        
        input_layout.addRow("Color (Required):", self.color_input)
        input_layout.addRow("Normal (Required):", self.normal_input)
        input_layout.addRow("AO (Optional):", self.ao_input)
        input_layout.addRow("Roughness (Optional):", self.roughness_input)
        input_layout.addRow("Metallic (Optional):", self.metallic_input)
        
        input_group.setLayout(input_layout)
        manual_layout.addWidget(input_group)
        
        # Options group
        options_group = QGroupBox("Processing Options")
        options_layout = QFormLayout()
        
        # AO Strength
        ao_layout = QHBoxLayout()
        self.ao_strength_slider = QSlider(Qt.Horizontal)
        # Range 0..200 to allow AO strength up to 2.00 (value displayed as value/100)
        self.ao_strength_slider.setRange(0, 200)
        self.ao_strength_slider.setValue(50)
        self.ao_strength_value = QLabel("0.50")
        self.ao_strength_slider.valueChanged.connect(
            lambda v: self.ao_strength_value.setText(f"{v/100:.2f}")
        )
        ao_layout.addWidget(self.ao_strength_slider)
        ao_layout.addWidget(self.ao_strength_value)
        options_layout.addRow("AO Strength:", ao_layout)
        
        # Gloss Gamma
        gamma_layout = QHBoxLayout()
        self.gloss_gamma_slider = QSlider(Qt.Horizontal)
        self.gloss_gamma_slider.setRange(10, 40)
        self.gloss_gamma_slider.setValue(22)
        self.gloss_gamma_value = QLabel("2.20")
        self.gloss_gamma_slider.valueChanged.connect(
            lambda v: self.gloss_gamma_value.setText(f"{v/10:.2f}")
        )
        gamma_layout.addWidget(self.gloss_gamma_slider)
        gamma_layout.addWidget(self.gloss_gamma_value)
        options_layout.addRow("Gloss Gamma:", gamma_layout)

        # Metal Diffuse Suppression
        metal_layout = QHBoxLayout()
        self.metal_suppression_slider = QSlider(Qt.Horizontal)
        self.metal_suppression_slider.setRange(0, 100)
        self.metal_suppression_slider.setValue(70)
        self.metal_suppression_slider.setToolTip(
            "How much to darken albedo on metal pixels. "
            "0.00 = no darkening (metal keeps full albedo), "
            "1.00 = fully darkened (metal becomes black diffuse)."
        )
        self.metal_suppression_value = QLabel("0.70")
        self.metal_suppression_slider.valueChanged.connect(
            lambda v: self.metal_suppression_value.setText(f"{v/100:.2f}")
        )
        metal_layout.addWidget(self.metal_suppression_slider)
        metal_layout.addWidget(self.metal_suppression_value)
        options_layout.addRow("Metal Diffuse Suppression:", metal_layout)

        # Phong Strength
        phong_layout = QHBoxLayout()
        self.phong_strength_slider = QSlider(Qt.Horizontal)
        self.phong_strength_slider.setRange(0, 200)
        self.phong_strength_slider.setValue(50)
        self.phong_strength_slider.setToolTip(
            "Scales the phong mask (bump alpha) and phong exponent map. "
            "0.00 = no phong, 0.50 = halved (default), 1.00 = original strength, "
            "up to 2.00 to push specular harder."
        )
        self.phong_strength_value = QLabel("0.50")
        self.phong_strength_slider.valueChanged.connect(
            lambda v: self.phong_strength_value.setText(f"{v/100:.2f}")
        )
        phong_layout.addWidget(self.phong_strength_slider)
        phong_layout.addWidget(self.phong_strength_value)
        options_layout.addRow("Phong Strength:", phong_layout)

        # Phong Tint Mode
        self.phong_tint_mode_combo = QComboBox()
        self.phong_tint_mode_combo.addItem("Off", "off")
        self.phong_tint_mode_combo.addItem("Selective (recommended)", "selective")
        self.phong_tint_mode_combo.addItem("Blanket", "blanket")
        self.phong_tint_mode_combo.setCurrentIndex(1)
        self.phong_tint_mode_combo.setToolTip(
            "Compensates the phong mask for $phongalbedotint runtime tinting. "
            "Off: original behaviour. Selective: only colored metals are boosted, "
            "dielectric phong is suppressed (envmap handles their spec). "
            "Blanket: divide-by-luminance compensation everywhere. "
            "Has no effect on targets that don't support $phongalbedotint (hl2, source2013_sp)."
        )
        options_layout.addRow("Phong Tint Mode:", self.phong_tint_mode_combo)

        # Colored Metal Relief
        relief_layout = QHBoxLayout()
        self.colored_metal_relief_slider = QSlider(Qt.Horizontal)
        self.colored_metal_relief_slider.setRange(0, 100)
        self.colored_metal_relief_slider.setValue(50)
        self.colored_metal_relief_slider.setToolTip(
            "Per-pixel relief on Metal Diffuse Suppression for chromatic metals. "
            "0.00 = uniform suppression (current behaviour). "
            "1.00 = fully-saturated metals receive no diffuse darkening, so colors "
            "like gold/copper/brass stay bright for $phongalbedotint to multiply. "
            "Only applied when Phong Tint Mode is not Off."
        )
        self.colored_metal_relief_value = QLabel("0.50")
        self.colored_metal_relief_slider.valueChanged.connect(
            lambda v: self.colored_metal_relief_value.setText(f"{v/100:.2f}")
        )
        relief_layout.addWidget(self.colored_metal_relief_slider)
        relief_layout.addWidget(self.colored_metal_relief_value)
        options_layout.addRow("Colored Metal Relief:", relief_layout)

        # Generate VTF checkbox
        self.generate_vtf_checkbox = QCheckBox("Generate VTF")
        self.generate_vtf_checkbox.setChecked(True)
        options_layout.addRow("Generate VTF:", self.generate_vtf_checkbox)

        # Generate mipmaps checkbox
        self.generate_mipmaps_checkbox = QCheckBox("Generate Mipmaps")
        self.generate_mipmaps_checkbox.setChecked(True)
        options_layout.addRow("Generate Mipmaps:", self.generate_mipmaps_checkbox)

        # Generate VMT checkbox
        self.generate_vmt_checkbox = QCheckBox("Generate VMT")
        self.generate_vmt_checkbox.setChecked(True)
        options_layout.addRow("Generate VMT:", self.generate_vmt_checkbox)
        
        options_group.setLayout(options_layout)
        manual_layout.addWidget(options_group)
        
        # Output group
        output_group = QGroupBox("Output")
        output_layout = QFormLayout()
        # Output folder
        folder_layout = QHBoxLayout()
        self.output_folder_input = QLineEdit()
        self.output_folder_button = QPushButton("Browse...")
        self.output_folder_button.clicked.connect(self.browse_output_folder)
        folder_layout.addWidget(self.output_folder_input)
        folder_layout.addWidget(self.output_folder_button)
        output_layout.addRow("Output Folder:", folder_layout)
        
        # Material name
        self.material_name_input = QLineEdit()
        self.material_name_input.setPlaceholderText("e.g., combine_grunt_base")
        output_layout.addRow("Material Base Name:", self.material_name_input)
        
        # Material path
        self.material_path_input = QLineEdit()
        self.material_path_input.setPlaceholderText("e.g., models/ports")
        self.material_path_input.setText("models/ports")
        output_layout.addRow("Material Path:", self.material_path_input)
        
        output_group.setLayout(output_layout)
        manual_layout.addWidget(output_group)
        
        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        manual_layout.addWidget(self.progress_bar)
        
        # Process button
        self.process_button = QPushButton("Generate Material")
        self.process_button.clicked.connect(self.process_material)
        self.process_button.setMinimumHeight(40)
        manual_layout.addWidget(self.process_button)

        # Cancel button (manual)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_processing)
        manual_layout.addWidget(self.cancel_button)
        
        manual_layout.addStretch()

        tabs.addTab(manual_tab, "Manual")

        # ---------- Automate Tab (two-pane batch UI) ----------
        # Settings on the left (scrollable), results table + run controls on
        # the right — same shape as vmat_pbr_tool / gltf_smd_batch_tool.
        tabs.addTab(self._build_automate_tab(), "Automate")

        # Populate history dropdowns now that UI exists
        try:
            self._refresh_history_dropdown()
            self._refresh_auto_history_dropdown()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Automate tab — two-pane builders (scrollable settings + results)
    # ------------------------------------------------------------------

    def _build_automate_tab(self) -> QWidget:
        """Construct the Automate tab as a horizontal splitter with
        scrollable settings on the left and the scan/results table on
        the right. Mirrors the shape used by vmat_pbr_tool /
        gltf_smd_batch_tool so users find the same controls in the
        same places across batch tools."""
        tab = QWidget()
        outer = QHBoxLayout(tab)
        outer.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_auto_settings_pane())
        splitter.addWidget(self._build_auto_results_pane())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([460, 880])
        outer.addWidget(splitter)
        return tab

    def _build_auto_settings_pane(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        container = QWidget()
        col = QVBoxLayout(container)
        col.setContentsMargins(0, 0, 6, 0)
        col.setSpacing(8)

        col.addWidget(self._build_auto_folders_group())
        col.addWidget(self._build_auto_output_group())
        col.addWidget(self._build_auto_processing_group())
        col.addWidget(self._build_auto_requirements_group())
        col.addStretch()

        scroll.setWidget(container)
        scroll.setMinimumWidth(420)
        return scroll

    def _build_auto_folders_group(self) -> QGroupBox:
        """History dropdown + input/output folders + naming, stacked."""
        group = QGroupBox("Folders & Recent Runs")
        form = QFormLayout()

        self.auto_history_dropdown = QComboBox()
        self.auto_history_dropdown.addItem("-- Recent runs --")
        self.auto_history_dropdown.currentIndexChanged.connect(self.on_auto_history_selected)
        form.addRow("Recent run:", self.auto_history_dropdown)

        self.auto_input_folder = QLineEdit()
        in_btn = QPushButton("Browse...")
        in_btn.clicked.connect(lambda: self._browse_dir_into(self.auto_input_folder))
        in_row = QHBoxLayout()
        in_row.addWidget(self.auto_input_folder)
        in_row.addWidget(in_btn)
        form.addRow("Input root:", self._row_widget(in_row))

        self.auto_output_folder = QLineEdit()
        out_btn = QPushButton("Browse...")
        out_btn.clicked.connect(lambda: self._browse_dir_into(self.auto_output_folder))
        out_row = QHBoxLayout()
        out_row.addWidget(self.auto_output_folder)
        out_row.addWidget(out_btn)
        form.addRow("Output root:", self._row_widget(out_row))

        self.auto_material_path = QLineEdit()
        self.auto_material_path.setPlaceholderText("e.g., models/ports")
        self.auto_material_path.setText("models/ports")
        form.addRow("Material path:", self.auto_material_path)

        name_row = QHBoxLayout()
        self.auto_prefix = QLineEdit()
        self.auto_prefix.setPlaceholderText("Prefix")
        self.auto_suffix = QLineEdit()
        self.auto_suffix.setPlaceholderText("Suffix")
        name_row.addWidget(self.auto_prefix)
        name_row.addWidget(self.auto_suffix)
        form.addRow("Prefix / Suffix:", self._row_widget(name_row))

        group.setLayout(form)
        return group

    def _build_auto_output_group(self) -> QGroupBox:
        """Generation toggles + run mode (recursive / skip / parallelism)."""
        group = QGroupBox("Output")
        form = QFormLayout()

        gen_grid = QGridLayout()
        gen_grid.setContentsMargins(0, 0, 0, 0)
        gen_grid.setHorizontalSpacing(12)
        self.auto_generate_vtf = QCheckBox("Generate VTF")
        self.auto_generate_vtf.setChecked(True)
        self.auto_generate_vmt = QCheckBox("Generate VMT")
        self.auto_generate_vmt.setChecked(True)
        self.auto_generate_mipmaps = QCheckBox("Generate Mipmaps")
        self.auto_generate_mipmaps.setChecked(True)
        self.auto_skip_existing = QCheckBox("Skip already-processed files")
        self.auto_skip_existing.setChecked(False)
        self.auto_skip_existing.setToolTip(
            "Skip any input whose .vmt and enabled .vtf outputs already exist "
            "in the destination folder."
        )
        gen_grid.addWidget(self.auto_generate_vtf, 0, 0)
        gen_grid.addWidget(self.auto_generate_vmt, 0, 1)
        gen_grid.addWidget(self.auto_generate_mipmaps, 1, 0)
        gen_grid.addWidget(self.auto_skip_existing, 1, 1)
        form.addRow("Generate:", self._wrap_layout(gen_grid))

        self.auto_recursive = QCheckBox("Recursive (include subfolders)")
        self.auto_recursive.setChecked(True)
        self.auto_recursive.setToolTip(
            "When on, scan all subfolders of the input. When off, scan only "
            "the input folder itself."
        )
        form.addRow("", self.auto_recursive)

        max_cpu = os.cpu_count() or 4
        self.auto_max_parallel = QSpinBox()
        self.auto_max_parallel.setRange(1, max(2, max_cpu))
        self.auto_max_parallel.setValue(min(4, max_cpu))
        form.addRow("Max parallel:", self.auto_max_parallel)

        group.setLayout(form)
        return group

    def _build_auto_processing_group(self) -> QGroupBox:
        """Sliders + tint mode — same content as before, just regrouped."""
        group = QGroupBox("Processing Options")
        form = QFormLayout()

        self.auto_ao_slider, self.auto_ao_value = self._make_auto_slider(0, 200, 50, 0.01)
        form.addRow("AO Strength:", self._slider_row(self.auto_ao_slider, self.auto_ao_value))

        self.auto_gamma_slider, self.auto_gamma_value = self._make_auto_slider(10, 40, 22, 0.1)
        form.addRow("Gloss Gamma:", self._slider_row(self.auto_gamma_slider, self.auto_gamma_value))

        self.auto_metal_suppression_slider, self.auto_metal_suppression_value = self._make_auto_slider(
            0, 100, 70, 0.01,
        )
        self.auto_metal_suppression_slider.setToolTip(
            "How much to darken albedo on metal pixels. "
            "0.00 = no darkening, 1.00 = fully darkened."
        )
        form.addRow(
            "Metal Diffuse Suppression:",
            self._slider_row(self.auto_metal_suppression_slider, self.auto_metal_suppression_value),
        )

        self.auto_phong_strength_slider, self.auto_phong_strength_value = self._make_auto_slider(
            0, 200, 50, 0.01,
        )
        self.auto_phong_strength_slider.setToolTip(
            "Scales the phong mask (bump alpha) and phong exponent map. "
            "0.00 = no phong, 0.50 = halved (default), 1.00 = original strength."
        )
        form.addRow(
            "Phong Strength:",
            self._slider_row(self.auto_phong_strength_slider, self.auto_phong_strength_value),
        )

        self.auto_phong_tint_mode_combo = QComboBox()
        self.auto_phong_tint_mode_combo.addItem("Off", "off")
        self.auto_phong_tint_mode_combo.addItem("Selective (recommended)", "selective")
        self.auto_phong_tint_mode_combo.addItem("Blanket", "blanket")
        self.auto_phong_tint_mode_combo.setCurrentIndex(1)
        self.auto_phong_tint_mode_combo.setToolTip(
            "Compensates the phong mask for $phongalbedotint runtime tinting. "
            "Selective is recommended for mixed metal/dielectric materials."
        )
        form.addRow("Phong Tint Mode:", self.auto_phong_tint_mode_combo)

        self.auto_colored_metal_relief_slider, self.auto_colored_metal_relief_value = self._make_auto_slider(
            0, 100, 50, 0.01,
        )
        self.auto_colored_metal_relief_slider.setToolTip(
            "Per-pixel relief on Metal Diffuse Suppression for chromatic metals. "
            "Only applied when Phong Tint Mode is not Off."
        )
        form.addRow(
            "Colored Metal Relief:",
            self._slider_row(self.auto_colored_metal_relief_slider, self.auto_colored_metal_relief_value),
        )

        group.setLayout(form)
        return group

    def _build_auto_requirements_group(self) -> QGroupBox:
        """Texture-role filter — 2-column grid so it fits in the narrow pane.
        Color/Normal default on (matching today's hardcoded validation);
        AO/Roughness/Metallic default off so existing scans behave as before.
        Toggling any checkbox re-applies live to the results table — rows
        missing a required type get auto-unchecked."""
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
        )
        for idx, (label, key, default) in enumerate(roles):
            cb = QCheckBox(label)
            cb.setChecked(default)
            cb.setToolTip(
                f"Require a {label.lower()} map for a material to be processed. "
                f"Rows missing this map will be auto-unchecked in the table."
            )
            cb.stateChanged.connect(self._apply_requirements_filter)
            grid.addWidget(cb, idx // 2, idx % 2)
            self.req_checkboxes[key] = cb
        group.setLayout(grid)
        return group

    def _build_auto_results_pane(self) -> QWidget:
        """Right pane: scan + selection bar, results table, run buttons."""
        pane = QWidget()
        col = QVBoxLayout(pane)
        col.setContentsMargins(6, 0, 0, 0)
        col.setSpacing(6)

        action_row = QHBoxLayout()
        self.scan_button = QPushButton("Scan")
        self.scan_button.clicked.connect(self.scan_folder_for_materials)
        action_row.addWidget(self.scan_button)

        action_row.addSpacing(12)
        action_row.addWidget(QLabel("All:"))
        self.scan_select_all_btn = QPushButton("Check")
        self.scan_select_all_btn.clicked.connect(lambda: self._set_all_scan_selected(True))
        self.scan_select_none_btn = QPushButton("Uncheck")
        self.scan_select_none_btn.clicked.connect(lambda: self._set_all_scan_selected(False))
        self.scan_select_invert_btn = QPushButton("Invert")
        self.scan_select_invert_btn.clicked.connect(self._invert_scan_selection)
        for b in (self.scan_select_all_btn, self.scan_select_none_btn, self.scan_select_invert_btn):
            action_row.addWidget(b)

        action_row.addSpacing(12)
        action_row.addWidget(QLabel("Selected:"))
        sel_tooltip = (
            "Click a row, then Shift+Click (range) or Ctrl+Click (toggle) more "
            "rows like in Explorer.\n"
            "These buttons toggle the Include checkbox for the highlighted rows "
            "only. Pressing Space while the table has focus does the same."
        )
        self.scan_check_selected_btn = QPushButton("Check")
        self.scan_check_selected_btn.setToolTip(sel_tooltip)
        self.scan_check_selected_btn.clicked.connect(lambda: self._set_selected_scan_rows_checked(True))
        self.scan_uncheck_selected_btn = QPushButton("Uncheck")
        self.scan_uncheck_selected_btn.setToolTip(sel_tooltip)
        self.scan_uncheck_selected_btn.clicked.connect(lambda: self._set_selected_scan_rows_checked(False))
        self.scan_toggle_selected_btn = QPushButton("Toggle")
        self.scan_toggle_selected_btn.setToolTip(sel_tooltip)
        self.scan_toggle_selected_btn.clicked.connect(self._toggle_selected_scan_rows)
        for b in (self.scan_check_selected_btn, self.scan_uncheck_selected_btn, self.scan_toggle_selected_btn):
            action_row.addWidget(b)

        action_row.addStretch()
        col.addLayout(action_row)

        # Results table
        self.scan_table = QTableWidget(0, 7)
        self.scan_table.setHorizontalHeaderLabels([
            "Include", "Material", "Color", "Normal", "AO", "Roughness", "Metallic"
        ])
        header = self.scan_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        self.scan_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        # Explorer-style multi-select.
        self.scan_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.scan_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.scan_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.scan_table.installEventFilter(self)
        col.addWidget(self.scan_table, 1)

        # Run controls — Cancel + Convert at the bottom-right.
        run_row = QHBoxLayout()
        run_row.addStretch()
        self.cancel_all_button = QPushButton("Cancel")
        self.cancel_all_button.setEnabled(False)
        self.cancel_all_button.clicked.connect(self.cancel_automation)
        self.process_all_button = QPushButton("Convert")
        self.process_all_button.setEnabled(False)
        self.process_all_button.clicked.connect(self.process_all_materials)
        run_row.addWidget(self.cancel_all_button)
        run_row.addWidget(self.process_all_button)
        col.addLayout(run_row)

        return pane

    # --- Small shared helpers for the Automate tab -------------------

    def _make_auto_slider(self, lo: int, hi: int, initial: int, step: float):
        """Build a horizontal slider + value label, wired so the label shows
        the scaled value live."""
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

    # --- Selection helpers + Spacebar event filter (Explorer-style) ---

    def _set_all_scan_selected(self, checked: bool):
        state = Qt.Checked if checked else Qt.Unchecked
        for row in range(self.scan_table.rowCount()):
            item = self.scan_table.item(row, 0)
            if item is not None:
                item.setCheckState(state)

    def _invert_scan_selection(self):
        for row in range(self.scan_table.rowCount()):
            item = self.scan_table.item(row, 0)
            if item is None:
                continue
            item.setCheckState(
                Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked
            )

    def _highlighted_scan_rows(self) -> list:
        sm = self.scan_table.selectionModel()
        if sm is None:
            return []
        return sorted({idx.row() for idx in sm.selectedIndexes()})

    def _set_selected_scan_rows_checked(self, checked: bool):
        state = Qt.Checked if checked else Qt.Unchecked
        for row in self._highlighted_scan_rows():
            item = self.scan_table.item(row, 0)
            if item is not None:
                item.setCheckState(state)

    def _toggle_selected_scan_rows(self):
        rows = self._highlighted_scan_rows()
        if not rows:
            return
        checked_count = sum(
            1 for row in rows
            if self.scan_table.item(row, 0) is not None
            and self.scan_table.item(row, 0).checkState() == Qt.Checked
        )
        new_state = Qt.Unchecked if checked_count * 2 >= len(rows) else Qt.Checked
        for row in rows:
            item = self.scan_table.item(row, 0)
            if item is not None:
                item.setCheckState(new_state)

    def eventFilter(self, obj, event):
        """Intercept Space on the scan table to bulk-toggle highlighted rows."""
        if obj is self.scan_table and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Space, Qt.Key_Select):
                self._toggle_selected_scan_rows()
                return True
        return super().eventFilter(obj, event)

    def create_file_input(self) -> QWidget:
        """Create a file input row with browse button"""
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        
        line_edit = QLineEdit()
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(lambda: self.browse_file(line_edit))
        
        layout.addWidget(line_edit)
        layout.addWidget(browse_btn)
        
        # Store line edit as property of container for easy access
        container.line_edit = line_edit
        
        return container

    def _row_widget(self, layout: QHBoxLayout) -> QWidget:
        """Helper to wrap a row layout in a QWidget for FormLayout rows"""
        w = QWidget()
        w.setLayout(layout)
        return w

    def _browse_dir_into(self, line_edit: QLineEdit):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder", "")
        if folder:
            line_edit.setText(folder)
    
    def browse_file(self, line_edit: QLineEdit):
        """Browse for an image file"""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Image",
            "",
            "Images (*.png *.jpg *.jpeg *.tga *.bmp);;All Files (*.*)"
        )
        if file_path:
            line_edit.setText(file_path)

    def _load_history(self):
        """Load history list from JSON file"""
        try:
            if self._history_file.exists():
                with open(self._history_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self.history = data
        except Exception:
            # ignore errors, leave history empty
            self.history = []
        # Populate dropdown
        try:
            self._refresh_history_dropdown()
        except Exception:
            pass

    def _load_auto_history(self):
        """Load automation history list from JSON file"""
        try:
            if self._auto_history_file.exists():
                with open(self._auto_history_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self.auto_history = data
        except Exception:
            self.auto_history = []
        try:
            self._refresh_auto_history_dropdown()
        except Exception:
            pass

    def _save_history_file(self):
        """Write history list to JSON file"""
        try:
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._history_file, 'w', encoding='utf-8') as f:
                json.dump(self.history, f, indent=2)
        except Exception:
            pass

    def _save_auto_history_file(self):
        """Write automation history list to JSON file"""
        try:
            self._auto_history_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._auto_history_file, 'w', encoding='utf-8') as f:
                json.dump(self.auto_history, f, indent=2)
        except Exception:
            pass

    def _refresh_history_dropdown(self):
        """Refresh the dropdown widget from self.history"""
        # If dropdowns don't exist yet (during init), skip
        if not hasattr(self, 'history_dropdown'):
            return
        # Block signals while updating
        self.history_dropdown.blockSignals(True)
        self.history_dropdown.clear()
        self.history_dropdown.addItem("-- Recent runs --")
        for entry in self.history:
            # Friendly label: timestamp - material_name
            ts = entry.get('timestamp', '')
            name = entry.get('material_name') or entry.get('material_name_input') or ''
            label = f"{ts} — {name}"
            self.history_dropdown.addItem(label)
        self.history_dropdown.blockSignals(False)

    def _refresh_auto_history_dropdown(self):
        """Refresh the automation history dropdown from self.auto_history"""
        if not hasattr(self, 'auto_history_dropdown'):
            return
        self.auto_history_dropdown.blockSignals(True)
        self.auto_history_dropdown.clear()
        self.auto_history_dropdown.addItem("-- Recent runs --")
        for entry in self.auto_history:
            ts = entry.get('timestamp', '')
            label = entry.get('label') or ts
            self.auto_history_dropdown.addItem(label)
        self.auto_history_dropdown.blockSignals(False)

    def on_auto_history_selected(self, index: int):
        """Populate automation fields when a previous automation run is selected"""
        if index <= 0:
            return
        history_index = index - 1
        try:
            entry = self.auto_history[history_index]
        except Exception:
            return

        try:
            self.auto_input_folder.setText(entry.get('input_root') or '')
            self.auto_output_folder.setText(entry.get('output_folder') or '')
            self.auto_material_path.setText(entry.get('material_path') or 'models/ports')
            self.auto_prefix.setText(entry.get('prefix') or '')
            self.auto_suffix.setText(entry.get('suffix') or '')

            opts = entry.get('options', {})
            ao_val = float(opts.get('ao_strength', 0.5))
            gamma_val = float(opts.get('gloss_gamma', 2.2))
            metal_supp_val = float(opts.get('metal_diffuse_suppression', 0.7))
            relief_val = float(opts.get('colored_metal_relief', 0.5))
            self.auto_ao_slider.setValue(int(ao_val * 100))
            self.auto_gamma_slider.setValue(int(gamma_val * 10))
            self.auto_metal_suppression_slider.setValue(int(metal_supp_val * 100))
            self.auto_colored_metal_relief_slider.setValue(int(relief_val * 100))
            tint_mode_val = str(opts.get('phong_tint_mode', 'selective'))
            tint_idx = self.auto_phong_tint_mode_combo.findData(tint_mode_val)
            if tint_idx >= 0:
                self.auto_phong_tint_mode_combo.setCurrentIndex(tint_idx)
            self.auto_generate_vtf.setChecked(bool(opts.get('generate_vtf', True)))
            self.auto_generate_vmt.setChecked(bool(opts.get('generate_vmt', True)))
            self.auto_generate_mipmaps.setChecked(bool(opts.get('generate_mipmaps', True)))
            reqs = opts.get('requirements') or {}
            for key, cb in self.req_checkboxes.items():
                if key in reqs:
                    cb.setChecked(bool(reqs[key]))
        except Exception:
            pass

    def on_history_selected(self, index: int):
        """Populate UI fields when a previous run is selected"""
        # index 0 is placeholder
        if index <= 0:
            return
        # History is stored as list; dropdown index maps to history index
        history_index = index - 1
        try:
            entry = self.history[history_index]
        except Exception:
            return

        # Populate inputs
        inputs = entry.get('inputs', {})
        # Each file input is container with .line_edit
        try:
            self.color_input.line_edit.setText(inputs.get('color') or '')
            self.normal_input.line_edit.setText(inputs.get('normal') or '')
            self.ao_input.line_edit.setText(inputs.get('ao') or '')
            self.roughness_input.line_edit.setText(inputs.get('roughness') or '')
            self.metallic_input.line_edit.setText(inputs.get('metallic') or '')
        except Exception:
            pass

        # Populate options
        options = entry.get('options', {})
        try:
            ao_val = float(options.get('ao_strength', 0.5))
            gamma_val = float(options.get('gloss_gamma', 2.2))
            metal_supp_val = float(options.get('metal_diffuse_suppression', 0.7))
            relief_val = float(options.get('colored_metal_relief', 0.5))
            self.ao_strength_slider.setValue(int(ao_val * 100))
            self.gloss_gamma_slider.setValue(int(gamma_val * 10))
            self.metal_suppression_slider.setValue(int(metal_supp_val * 100))
            self.colored_metal_relief_slider.setValue(int(relief_val * 100))
            tint_mode_val = str(options.get('phong_tint_mode', 'selective'))
            tint_idx = self.phong_tint_mode_combo.findData(tint_mode_val)
            if tint_idx >= 0:
                self.phong_tint_mode_combo.setCurrentIndex(tint_idx)
            self.generate_vtf_checkbox.setChecked(bool(options.get('generate_vtf', True)))
            self.generate_mipmaps_checkbox.setChecked(bool(options.get('generate_mipmaps', True)))
            self.generate_vmt_checkbox.setChecked(bool(options.get('generate_vmt', True)))
        except Exception:
            pass

        # Populate outputs
        try:
            self.output_folder_input.setText(entry.get('output_folder') or '')
            self.material_name_input.setText(entry.get('material_name') or '')
            self.material_path_input.setText(entry.get('material_path') or 'models/ports')
        except Exception:
            pass

    def _make_history_entry(self):
        """Construct a history entry dict from current UI state"""
        inputs = self.get_inputs()
        options = self.get_processing_options()
        entry = {
            'timestamp': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
            'material_name': self.material_name_input.text() or '',
            'material_path': self.material_path_input.text() or '',
            'output_folder': self.output_folder_input.text() or '',
            'inputs': {
                'color': inputs.color or '',
                'normal': inputs.normal or '',
                'ao': inputs.ao or '',
                'roughness': inputs.roughness or '',
                'metallic': inputs.metallic or ''
            },
            'options': {
                'ao_strength': options.ao_strength,
                'gloss_gamma': options.gloss_gamma,
                'metal_diffuse_suppression': options.metal_diffuse_suppression,
                'phong_tint_mode': options.phong_tint_mode,
                'colored_metal_relief': options.colored_metal_relief,
                'generate_vtf': options.generate_vtf,
                'generate_vmt': options.generate_vmt,
                'generate_mipmaps': options.generate_mipmaps
            }
        }
        return entry

    def _save_current_run_to_history(self):
        """Save current UI state to history list and write to disk"""
        try:
            entry = self._make_history_entry()
            # Avoid duplicates: if identical to most recent, skip
            if len(self.history) > 0 and self.history[0].get('inputs') == entry.get('inputs') and self.history[0].get('output_folder') == entry.get('output_folder') and self.history[0].get('material_name') == entry.get('material_name'):
                return
            # Remove any identical older entries
            self.history = [h for h in self.history if not (
                h.get('inputs') == entry.get('inputs') and
                h.get('output_folder') == entry.get('output_folder') and
                h.get('material_name') == entry.get('material_name')
            )]
            # Insert new entry at front
            self.history.insert(0, entry)
            # Limit history length
            MAX = 20
            if len(self.history) > MAX:
                self.history = self.history[:MAX]
            # Persist
            self._save_history_file()
            # Refresh dropdown
            self._refresh_history_dropdown()
        except Exception:
            pass

    def _make_auto_history_entry(self):
        """Construct an automation history entry from current automation UI state"""
        opts = {
            'ao_strength': self.auto_ao_slider.value() / 100.0,
            'gloss_gamma': self.auto_gamma_slider.value() / 10.0,
            'metal_diffuse_suppression': self.auto_metal_suppression_slider.value() / 100.0,
            'phong_tint_mode': self.auto_phong_tint_mode_combo.currentData() or 'selective',
            'colored_metal_relief': self.auto_colored_metal_relief_slider.value() / 100.0,
            'generate_vtf': self.auto_generate_vtf.isChecked(),
            'generate_vmt': self.auto_generate_vmt.isChecked(),
            'generate_mipmaps': self.auto_generate_mipmaps.isChecked(),
            'requirements': {key: cb.isChecked() for key, cb in self.req_checkboxes.items()},
        }
        label = self.auto_input_folder.text() or self.auto_output_folder.text() or ''
        return {
            'timestamp': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
            'label': label,
            'input_root': self.auto_input_folder.text() or '',
            'output_folder': self.auto_output_folder.text() or '',
            'material_path': self.auto_material_path.text() or 'models/ports',
            'prefix': self.auto_prefix.text() or '',
            'suffix': self.auto_suffix.text() or '',
            'options': opts,
        }

    def _save_current_auto_run_to_history(self):
        """Save current automation UI state to automation history and persist it"""
        try:
            entry = self._make_auto_history_entry()
            # Avoid duplicates: if identical to most recent, skip
            if (
                len(self.auto_history) > 0 and
                self.auto_history[0].get('input_root') == entry.get('input_root') and
                self.auto_history[0].get('output_folder') == entry.get('output_folder') and
                self.auto_history[0].get('prefix') == entry.get('prefix') and
                self.auto_history[0].get('suffix') == entry.get('suffix')
            ):
                return

            # Remove exact duplicates
            def _same(a, b):
                return (
                    a.get('input_root') == b.get('input_root') and
                    a.get('output_folder') == b.get('output_folder') and
                    a.get('material_path') == b.get('material_path') and
                    a.get('prefix') == b.get('prefix') and
                    a.get('suffix') == b.get('suffix') and
                    a.get('options') == b.get('options')
                )

            self.auto_history = [h for h in self.auto_history if not _same(h, entry)]
            self.auto_history.insert(0, entry)
            MAX = 20
            if len(self.auto_history) > MAX:
                self.auto_history = self.auto_history[:MAX]
            self._save_auto_history_file()
            self._refresh_auto_history_dropdown()
        except Exception:
            pass
    
    def browse_output_folder(self):
        """Browse for output folder"""
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Output Folder",
            ""
        )
        if folder:
            self.output_folder_input.setText(folder)
    
    def get_processing_options(self) -> ProcessingOptions:
        """Get current processing options from UI"""
        return ProcessingOptions(
            ao_strength=self.ao_strength_slider.value() / 100.0,
            gloss_gamma=self.gloss_gamma_slider.value() / 10.0,
            metal_diffuse_suppression=self.metal_suppression_slider.value() / 100.0,
            phong_strength=self.phong_strength_slider.value() / 100.0,
            phong_tint_mode=self.phong_tint_mode_combo.currentData() or "selective",
            colored_metal_relief=self.colored_metal_relief_slider.value() / 100.0,
            generate_vtf=self.generate_vtf_checkbox.isChecked(),
            generate_vmt=self.generate_vmt_checkbox.isChecked(),
            generate_mipmaps=self.generate_mipmaps_checkbox.isChecked()
        )
    
    def get_inputs(self) -> PBRInputs:
        """Get input file paths from UI"""
        return PBRInputs(
            color=self.color_input.line_edit.text() or None,
            normal=self.normal_input.line_edit.text() or None,
            ao=self.ao_input.line_edit.text() or None,
            roughness=self.roughness_input.line_edit.text() or None,
            metallic=self.metallic_input.line_edit.text() or None
        )
    
    def process_material(self):
        """Start material processing"""
        # Validate inputs
        inputs = self.get_inputs()
        if not inputs.color:
            self.log("Error: Color map is required", "error")
            return
        
        if not inputs.normal:
            self.log("Error: Normal map is required", "error")
            return
        
        output_folder = self.output_folder_input.text()
        if not output_folder:
            self.log("Error: Output folder is required", "error")
            return
        
        material_name = self.material_name_input.text()
        if not material_name:
            self.log("Error: Material name is required", "error")
            return
        
        material_path = self.material_path_input.text() or "models/ports"
        
        # Disable UI
        self.process_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # Indeterminate
        
        # Clear log
        self.log_output.clear()
        self.log("Starting material processing...", "info")
        
        # Create processor
        options = self.get_processing_options()
        self.processor = FakePBRProcessor(options)

        # Save run to history before starting
        try:
            self._save_current_run_to_history()
        except Exception:
            pass
        
        # Start processing thread
        self.processing_thread = ProcessingThread(
            self.processor,
            inputs,
            output_folder,
            material_name,
            material_path
        )
        self.processing_thread.finished.connect(self.on_processing_finished)
        self.processing_thread.start()
    
    def on_processing_finished(self, success: bool, message: str):
        """Handle processing completion"""
        # Re-enable UI
        self.process_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress_bar.setVisible(False)
        
        # Log result
        if success:
            self.log(message, "success")
        else:
            self.log(message, "error")
        
        # Clean up
        if self.processor:
            self.processor.shutdown()
            self.processor = None

    # ===================== Automate Tab Logic =====================
    def _tokenize(self, name: str):
        import re
        # split on non-alphanumeric characters
        tokens = re.split(r"[^a-z0-9]+", name.lower())
        return [t for t in tokens if t]

    def _detect_type_and_key(self, filepath: str):
        """Detect texture type from filename and derive a base key using a suffix-first heuristic.

        Strategy:
        - Split the filename stem into tokens.
        - Drop trailing non-type tokens like resolutions (2k, 4k, 1024, etc.).
        - If the last remaining token matches a known type suffix, use it as the type and
          derive the base key from the remaining tokens ("before the suffix").
        - Otherwise, fall back to the older behavior: find any token that matches a known
          keyword and remove it to form the base key.
        """
        from pathlib import Path as _Path
        p = _Path(filepath)
        stem = p.stem.lower()
        tokens = self._tokenize(stem)
        if not tokens:
            return None, None

        # 1) Prefer a trailing suffix match (after stripping ignorable size tokens)
        end_idx = len(tokens) - 1
        while end_idx >= 0 and tokens[end_idx] in self._trailing_ignore_tokens:
            end_idx -= 1
        if end_idx >= 0:
            last_tok = tokens[end_idx]
            if last_tok in self._suffix_to_type:
                ttype = self._suffix_to_type[last_tok]
                base_tokens = tokens[:end_idx]
                key = "_".join(base_tokens) if base_tokens else f"{p.parent.name}_{stem}"
                return ttype, key

        # 2) Fallback: any keyword anywhere (choose the last occurrence for stability)
        found_idx = -1
        found_type = None
        for i in range(len(tokens) - 1, -1, -1):
            tok = tokens[i]
            if tok in self._suffix_to_type:
                found_idx = i
                found_type = self._suffix_to_type[tok]
                break
        if found_type is None:
            return None, None
        filtered = [t for j, t in enumerate(tokens) if j != found_idx and t not in self._trailing_ignore_tokens]
        key = "_".join(filtered) if filtered else f"{p.parent.name}_{stem}"
        return found_type, key

    def scan_folder_for_materials(self):
        root = self.auto_input_folder.text().strip()
        out_dir = self.auto_output_folder.text().strip()
        if not root:
            self.log("Select an Input Root Folder first", "ERROR")
            return
        if not out_dir:
            self.log("Select an Output Folder first", "ERROR")
            return
        import os
        matches: dict = {}
        # Index of detected files by folder and base key to enable a second-pass completion
        folder_index: Dict[str, Dict[str, Dict[str, str]]] = {}
        # Raw file listing per folder (for heuristic rescan even when a file didn't match in first pass)
        folder_files: Dict[str, list] = {}
        count_files = 0
        recursive = self.auto_recursive.isChecked()
        if recursive:
            walker = os.walk(root)
        else:
            try:
                top_files = [f for f in os.listdir(root) if os.path.isfile(os.path.join(root, f))]
            except OSError as e:
                self.log(f"Cannot read input folder: {e}", "ERROR")
                return
            walker = [(root, [], top_files)]
        for base, _, files in walker:
            for fn in files:
                ext = os.path.splitext(fn)[1].lower()
                if ext not in self._image_exts:
                    continue
                count_files += 1
                path = os.path.join(base, fn)
                folder_files.setdefault(base, []).append(path)
                ttype, key = self._detect_type_and_key(path)
                if not ttype or not key:
                    continue
                # Primary matches
                d = matches.setdefault(key, {})
                d.setdefault('material_name', key)
                d.setdefault('folder', base)
                # Only set if not already present to keep first-found preference
                d.setdefault(ttype, path)
                # Build folder index for completion pass
                f_idx = folder_index.setdefault(base, {})
                f_key = f_idx.setdefault(key, {})
                f_key.setdefault(ttype, path)
        # Completion pass: for each match, fill in missing types by scanning siblings with same base key
        for key, data in matches.items():
            folder = data.get('folder')
            if not folder:
                continue
            per_key = folder_index.get(folder, {}).get(key, {})
            if not per_key:
                per_key = {}
            # For each desired type, if missing, try to fill from index
            base_tokens = key.split('_') if key else []
            for typ in ("color", "normal", "ao", "roughness", "metallic"):
                if typ in data:
                    continue
                # 2a) If present in per_key (from first-pass recognitions), use it
                if typ in per_key:
                    data[typ] = per_key[typ]
                    continue
                # 2b) Heuristic rescan in the folder: look for files whose tokens are base + suffix
                for fpath in folder_files.get(folder, []):
                    stem2 = os.path.splitext(os.path.basename(fpath))[0].lower()
                    toks2 = self._tokenize(stem2)
                    # Strip trailing ignorable tokens
                    end_idx2 = len(toks2) - 1
                    while end_idx2 >= 0 and toks2[end_idx2] in self._trailing_ignore_tokens:
                        end_idx2 -= 1
                    if end_idx2 <= 0:
                        continue
                    suffix_tok = toks2[end_idx2]
                    if suffix_tok not in self._keyword_config.get(typ, []):
                        continue
                    base2 = toks2[:end_idx2]
                    if base2 == base_tokens:
                        data[typ] = fpath
                        break
        # Populate table
        self._populate_scan_table(matches)
        excluded = self._apply_requirements_filter()
        self.process_all_button.setEnabled(self.scan_table.rowCount() > 0)
        msg = f"Scan complete: {len(matches)} material sets detected from {count_files} files"
        if excluded:
            msg += f" ({excluded} excluded by requirements)"
        self.log(msg, "INFO")

    def _populate_scan_table(self, matches: dict):
        self.scan_table.setRowCount(0)
        for key, data in sorted(matches.items()):
            row = self.scan_table.rowCount()
            self.scan_table.insertRow(row)
            # Include checkbox
            include_item = QTableWidgetItem()
            include_item.setCheckState(Qt.Checked)
            include_item.setFlags(include_item.flags() | Qt.ItemIsUserCheckable)
            self.scan_table.setItem(row, 0, include_item)
            # Material name
            name_item = QTableWidgetItem(key)
            self.scan_table.setItem(row, 1, name_item)
            # For each map type, show Yes/No with tooltip path
            for col, typ in enumerate(["color", "normal", "ao", "roughness", "metallic"], start=2):
                path = data.get(typ)
                txt = "Yes" if path else "No"
                item = QTableWidgetItem(txt)
                if path:
                    item.setToolTip(path)
                self.scan_table.setItem(row, col, item)
        # store matches for processing
        self._scan_matches = matches

    def _get_override_path(self, enabled_checkbox: QCheckBox, file_container: QWidget) -> Optional[str]:
        try:
            if enabled_checkbox.isChecked():
                p = file_container.line_edit.text().strip()
                return p or None
        except Exception:
            return None
        return None

    def _fakepbr_outputs_exist(self, output_folder: str, material_name: str,
                               gen_vmt: bool, gen_vtf: bool) -> bool:
        """Return True if all enabled outputs for this material already exist."""
        if not output_folder:
            return False
        paths = []
        if gen_vmt:
            paths.append(os.path.join(output_folder, f"{material_name}.vmt"))
        if gen_vtf:
            paths.extend([
                os.path.join(output_folder, f"{material_name}_color.vtf"),
                os.path.join(output_folder, f"{material_name}_normal.vtf"),
                os.path.join(output_folder, f"{material_name}_phong.vtf"),
                os.path.join(output_folder, f"{material_name}_envmask.vtf"),
            ])
        if not paths:
            return False
        return all(os.path.exists(p) for p in paths)

    def process_all_materials(self):
        if not hasattr(self, '_scan_matches') or not self._scan_matches:
            self.log("No materials to process. Run Scan first.", "ERROR")
            return
        out_dir = self.auto_output_folder.text().strip()
        mat_path = self.auto_material_path.text().strip() or "models/ports"
        prefix = self.auto_prefix.text().strip()
        suffix = self.auto_suffix.text().strip()

        # Options
        ao_strength = self.auto_ao_slider.value() / 100.0
        gloss_gamma = self.auto_gamma_slider.value() / 10.0
        metal_suppression = self.auto_metal_suppression_slider.value() / 100.0
        phong_strength = self.auto_phong_strength_slider.value() / 100.0
        phong_tint_mode = self.auto_phong_tint_mode_combo.currentData() or "selective"
        colored_metal_relief = self.auto_colored_metal_relief_slider.value() / 100.0
        gen_vmt = self.auto_generate_vmt.isChecked()

        gen_vtf = self.auto_generate_vtf.isChecked()
        skip_existing = self.auto_skip_existing.isChecked()
        skipped_existing = 0

        # Collect tasks from checked rows
        tasks = []
        for row in range(self.scan_table.rowCount()):
            if self.scan_table.item(row, 0).checkState() != Qt.Checked:
                continue
            key = self.scan_table.item(row, 1).text()
            data = self._scan_matches.get(key, {})
            # Ensure color and normal present
            color = data.get('color')
            normal = data.get('normal')
            if not color or not normal:
                self.log(f"Skipping '{key}': missing color or normal", "WARNING")
                continue
            material_name = f"{prefix}{key}{suffix}"
            if skip_existing and self._fakepbr_outputs_exist(out_dir, material_name, gen_vmt, gen_vtf):
                skipped_existing += 1
                self.log(f"Skipping '{material_name}': outputs already exist", "INFO")
                continue
            inputs = PBRInputs(
                color=color,
                normal=normal,
                ao=data.get('ao'),
                roughness=data.get('roughness'),
                metallic=data.get('metallic')
            )
            tasks.append({
                'inputs': inputs,
                'output_folder': out_dir,
                'material_name': material_name,
                'material_path': mat_path,
                'row': row,
                'options': ProcessingOptions(
                    ao_strength=ao_strength,
                    gloss_gamma=gloss_gamma,
                    metal_diffuse_suppression=metal_suppression,
                    phong_strength=phong_strength,
                    phong_tint_mode=phong_tint_mode,
                    colored_metal_relief=colored_metal_relief,
                    generate_vtf=gen_vtf,
                    generate_vmt=gen_vmt,
                    generate_mipmaps=self.auto_generate_mipmaps.isChecked()
                )
            })
        if skip_existing and skipped_existing:
            self.log(f"Skipped {skipped_existing} material(s) with existing outputs.", "INFO")
        if not tasks:
            self.log("No valid tasks to process.", "ERROR")
            return

        # Save current automation configuration to its own history
        try:
            self._save_current_auto_run_to_history()
        except Exception:
            pass

        # Disable buttons
        self.scan_button.setEnabled(False)
        self.process_all_button.setEnabled(False)
        self.cancel_all_button.setEnabled(True)
        self.clear_log()
        self.log(f"Starting batch processing of {len(tasks)} materials...", "INFO")

        def make_processor():
            proc = FakePBRProcessor(ProcessingOptions(
                ao_strength=ao_strength,
                gloss_gamma=gloss_gamma,
                metal_diffuse_suppression=metal_suppression,
                phong_strength=phong_strength,
                phong_tint_mode=phong_tint_mode,
                colored_metal_relief=colored_metal_relief,
                generate_vtf=gen_vtf,
                generate_vmt=gen_vmt,
                generate_mipmaps=self.auto_generate_mipmaps.isChecked()
            ))
            return proc

        self.automation_thread = AutomationThread(make_processor, tasks, max_workers=self.auto_max_parallel.value())
        self.automation_thread.progress.connect(lambda m: self.log(m, "INFO"))
        self.automation_thread.row_finished.connect(self._on_scan_row_finished)
        self.automation_thread.finished.connect(self.on_automation_finished)
        self.automation_thread.start()

    def _on_scan_row_finished(self, row: int, success: bool):
        """Tint the corresponding scan_table row green/red as each task completes."""
        _paint_table_row(self.scan_table, row, success)

    def _required_types(self) -> List[str]:
        """Texture types currently required for a material to be eligible."""
        if not hasattr(self, "req_checkboxes"):
            return []
        return [key for key, cb in self.req_checkboxes.items() if cb.isChecked()]

    def _apply_requirements_filter(self) -> int:
        """Auto-uncheck rows whose underlying data is missing any required type.

        Returns the count of rows that ended up unchecked because they failed
        the current requirement set. Re-checking a row manually is still
        allowed; the filter only fires on requirement-toggle and post-scan.
        """
        if not hasattr(self, "scan_table") or not getattr(self, "_scan_matches", None):
            return 0
        required = self._required_types()
        if not required:
            return 0
        excluded = 0
        for row in range(self.scan_table.rowCount()):
            name_item = self.scan_table.item(row, 1)
            include_item = self.scan_table.item(row, 0)
            if name_item is None or include_item is None:
                continue
            data = self._scan_matches.get(name_item.text(), {})
            missing = [t for t in required if not data.get(t)]
            if missing:
                if include_item.checkState() == Qt.Checked:
                    include_item.setCheckState(Qt.Unchecked)
                include_item.setToolTip("Missing required: " + ", ".join(missing))
                excluded += 1
            else:
                include_item.setToolTip("")
        return excluded

    def on_automation_finished(self, success: bool, message: str):
        self.log(message, "SUCCESS" if success else "WARNING")
        self.scan_button.setEnabled(True)
        self.process_all_button.setEnabled(True)
        self.cancel_all_button.setEnabled(False)

    def cancel_processing(self):
        """Request cancellation of the current manual process."""
        try:
            if self.processing_thread and self.processing_thread.isRunning():
                self.processing_thread.requestInterruption()
                self.log("Cancel requested for current process...", "INFO")
        except Exception:
            pass

    def cancel_automation(self):
        """Request cancellation of the current automation batch."""
        try:
            if self.automation_thread and self.automation_thread.isRunning():
                self.automation_thread.requestInterruption()
                self.log("Cancel requested for batch processing...", "INFO")
        except Exception:
            pass
