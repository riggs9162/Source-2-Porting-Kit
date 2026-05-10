"""
ExoPBR Tool - Convert PBR textures to ExoPBR screenspace shader materials

This tool converts Source 2 PBR textures into ExoPBR-compatible materials with
proper base color, ARM map, and normal map packing for the screenspace shader.
"""

import os
from typing import Optional, Dict, Tuple, Callable
from dataclasses import dataclass
import numpy as np
from pathlib import Path
import json
from datetime import datetime

from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFileDialog,
    QLineEdit, QGroupBox, QDoubleSpinBox, QCheckBox,
    QProgressBar, QFormLayout, QWidget, QComboBox, QTabWidget,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QSpinBox, QSplitter, QScrollArea, QSizePolicy, QGridLayout,
)
from PySide6.QtCore import Qt, QThread, Signal, QEvent
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Event

from .base_tool import BaseTool
from ..utils.vtf_encoder import VTFEncoder, VTFEncoderError
from ..utils.helpers import get_config_dir
from ..utils.image_processing import load_image, resize_to_match, to_uint8
from ..utils.vmt_generator import generate_exopbr_vmt
from sourcepp import vtfpp


class ProcessingCancelled(Exception):
    """Internal signal to abort processing early when user cancels."""
    pass


@dataclass
class ExoPBRInputs:
    """Container for ExoPBR input textures"""
    color: Optional[str] = None
    normal: Optional[str] = None
    ao: Optional[str] = None
    roughness: Optional[str] = None
    metallic: Optional[str] = None
    selfillum: Optional[str] = None
    height: Optional[str] = None
    transparency_mask: Optional[str] = None
    # Uniform PBR scalars from Source 2 vmat fields (g_flMetalness /
    # g_flRoughness). When set without a corresponding texture, the processor
    # synthesises a flat single-value array.
    metallic_constant: Optional[float] = None
    roughness_constant: Optional[float] = None
    # Uniform RGBA tints for roles where the source vmat declared a Texture*
    # value as a vector literal (e.g. `"TextureColor" "[1.0 1.0 1.0 0.0]"`).
    # Only applied when no texture path resolves for the role; actual
    # textures always win.
    color_constant: Optional[Tuple[float, float, float, float]] = None
    ao_constant: Optional[Tuple[float, float, float, float]] = None
    selfillum_constant: Optional[Tuple[float, float, float, float]] = None
    transparency_mask_constant: Optional[Tuple[float, float, float, float]] = None


@dataclass
class ExoPBROptions:
    """Options for ExoPBR processing"""
    generate_vtf: bool = True
    generate_vmt: bool = True
    generate_mipmaps: bool = True
    emissionscale: float = 0.0
    parallaxscale: float = 0.0
    alphablend: bool = False


class ExoPBRProcessor:
    """
    Core processing engine for converting PBR textures to ExoPBR format
    """
    
    def __init__(self, options: ExoPBROptions):
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

        Same layout as `load_image` returns — used to materialise vmat
        Texture* vector literals into uniform images when no texture file
        was authored for the role.
        """
        clamped = [float(np.clip(c, 0.0, 1.0)) for c in rgba]
        return np.tile(
            np.array([[clamped]], dtype=np.float32),
            (4, 4, 1),
        )

    def _channel_or_default(self, data: Optional[np.ndarray], default: float, h: int, w: int) -> np.ndarray:
        """Extract single channel or return default-filled array"""
        if data is None:
            return np.full((h, w), default, dtype=np.float32)
        if data.ndim > 2:
            channel = data[:, :, 0]
        else:
            channel = data
        return np.clip(channel, 0.0, 1.0)
    
    def process_material(
        self,
        inputs: ExoPBRInputs,
        output_folder: str,
        material_name: str,
        material_path: str = "exopbr"
    ) -> Tuple[bool, str]:
        """
        Process a complete ExoPBR material set
        
        Args:
            inputs: ExoPBRInputs with paths to source textures
            output_folder: Folder to save output files
            material_name: Base name for output files
            material_path: Relative path in materials folder
            
        Returns:
            (success, message)
        """
        try:
            self._check_cancel()
            # Load all inputs
            self.log("[ExoPBR] Loading input textures...")
            color_data = load_image(inputs.color)
            if color_data is not None:
                self.log(f"  ✓ Loaded color map: {os.path.basename(inputs.color)}")
            elif inputs.color_constant is not None:
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
            self._check_cancel()
            
            ao_data = load_image(inputs.ao)
            if ao_data is not None:
                self.log(f"  ✓ Loaded AO map: {os.path.basename(inputs.ao)}")
            elif inputs.ao_constant is not None:
                ao_data = self._uniform_rgba_image(inputs.ao_constant)
                self.log(
                    f"  ✓ Synthesised uniform AO from literal "
                    f"= {inputs.ao_constant[0]:.3f}"
                )
            else:
                self.log("  ℹ No AO map provided (will use default: 1.0)")
            self._check_cancel()
            
            roughness_data = load_image(inputs.roughness)
            if roughness_data is not None:
                self.log(f"  ✓ Loaded roughness map: {os.path.basename(inputs.roughness)}")
            elif inputs.roughness_constant is not None:
                value = float(np.clip(inputs.roughness_constant, 0.0, 1.0))
                roughness_data = np.full((4, 4, 1), value, dtype=np.float32)
                self.log(f"  ✓ Synthesised uniform roughness from g_flRoughness = {value:.3f}")
            else:
                self.log("  ℹ No roughness map provided (will use default: 1.0)")
            self._check_cancel()

            metallic_data = load_image(inputs.metallic)
            if metallic_data is not None:
                self.log(f"  ✓ Loaded metallic map: {os.path.basename(inputs.metallic)}")
            elif inputs.metallic_constant is not None:
                value = float(np.clip(inputs.metallic_constant, 0.0, 1.0))
                metallic_data = np.full((4, 4, 1), value, dtype=np.float32)
                self.log(f"  ✓ Synthesised uniform metallic from g_flMetalness = {value:.3f}")
            else:
                self.log("  ℹ No metallic map provided (will use dielectric: 0.0)")
            self._check_cancel()

            selfillum_data = load_image(inputs.selfillum)
            if selfillum_data is not None:
                self.log(f"  ✓ Loaded self-illum map: {os.path.basename(inputs.selfillum)}")
            elif inputs.selfillum_constant is not None:
                selfillum_data = self._uniform_rgba_image(inputs.selfillum_constant)
                rgba = inputs.selfillum_constant
                self.log(
                    f"  ✓ Synthesised uniform self-illum from literal "
                    f"[{rgba[0]:.3f} {rgba[1]:.3f} {rgba[2]:.3f}]"
                )
            else:
                self.log("  ℹ No self-illum map provided (base alpha unchanged)")
            self._check_cancel()
            
            height_data = load_image(inputs.height)
            if height_data is not None:
                self.log(f"  ✓ Loaded height map: {os.path.basename(inputs.height)}")
            else:
                self.log("  ℹ No height map provided (ARM alpha will be 1.0)")
            self._check_cancel()
            
            transparency_mask_data = load_image(inputs.transparency_mask)
            if transparency_mask_data is not None:
                self.log(f"  ✓ Loaded transparency mask: {os.path.basename(inputs.transparency_mask)}")
            elif inputs.transparency_mask_constant is not None:
                transparency_mask_data = self._uniform_rgba_image(inputs.transparency_mask_constant)
                rgba = inputs.transparency_mask_constant
                self.log(
                    f"  ✓ Synthesised uniform transparency mask from literal "
                    f"(alpha = {rgba[3]:.3f})"
                )
            else:
                self.log("  ℹ No transparency mask provided (will use color alpha)")
            self._check_cancel()
            
            # Validate required inputs
            if color_data is None:
                return False, "Color map is required"
            
            if normal_data is None:
                return False, "Normal map is required"
            
            # Determine target resolution: use LARGEST dimension from all inputs
            all_inputs = [color_data, normal_data, ao_data, roughness_data, metallic_data, selfillum_data, height_data, transparency_mask_data]
            max_height = max(img.shape[0] for img in all_inputs if img is not None)
            max_width = max(img.shape[1] for img in all_inputs if img is not None)
            height, width = max_height, max_width
            
            self.log(f"[ExoPBR] Processing at resolution: {width}x{height}")
            self._check_cancel()
            
            # Resize all inputs to match target resolution
            color_data = resize_to_match(color_data, height, width, "color")
            normal_data = resize_to_match(normal_data, height, width, "normal")
            ao_data = resize_to_match(ao_data, height, width, "AO")
            roughness_data = resize_to_match(roughness_data, height, width, "roughness")
            metallic_data = resize_to_match(metallic_data, height, width, "metallic")
            selfillum_data = resize_to_match(selfillum_data, height, width, "self-illum")
            height_data = resize_to_match(height_data, height, width, "height")
            transparency_mask_data = resize_to_match(transparency_mask_data, height, width, "transparency mask")
            self._check_cancel()
            
            # Process base texture: RGB=color, Alpha=transparency mask (or color alpha if mask not provided)
            self.log(f"[ExoPBR] Processing base texture...")
            # Ensure color has RGBA
            if color_data.ndim == 3 and color_data.shape[2] >= 3:
                rgb = np.clip(color_data[:, :, :3], 0.0, 1.0)
                if color_data.shape[2] == 4:
                    alpha = np.clip(color_data[:, :, 3], 0.0, 1.0)
                else:
                    alpha = np.ones((height, width), dtype=np.float32)
            else:
                # Fallback: treat as grayscale color, replicate to RGB
                gray = np.clip(color_data[:, :, 0] if color_data.ndim == 3 else color_data, 0.0, 1.0)
                rgb = np.dstack([gray, gray, gray])
                alpha = np.ones((height, width), dtype=np.float32)

            # If transparency mask is provided, use it to override the alpha channel
            if transparency_mask_data is not None:
                if transparency_mask_data.ndim == 3:
                    # Use first channel of mask
                    alpha = np.clip(transparency_mask_data[:, :, 0], 0.0, 1.0)
                else:
                    # Single channel image
                    alpha = np.clip(transparency_mask_data, 0.0, 1.0)
                self.log(f"  → Using transparency mask for alpha channel")
            
            # Alpha now controls opacity
            base_rgba = np.dstack([rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2], alpha])
            base_texture = to_uint8(np.clip(base_rgba, 0.0, 1.0))
            self.log(f"  ✓ Base texture processed (alpha = opacity)")
            self._check_cancel()
            
            # Process ARM map (R=AO, G=Roughness, B=Metallic, A=Height)
            self.log(f"[ExoPBR] Packing ARM map...")
            ao_ch = self._channel_or_default(ao_data, 1.0, height, width)
            rough_ch = self._channel_or_default(roughness_data, 1.0, height, width)
            metal_ch = self._channel_or_default(metallic_data, 0.0, height, width)
            height_ch = self._channel_or_default(height_data, 1.0, height, width)
            
            # Debug: Validate channel shapes and ranges
            for ch_name, ch_data in [("AO", ao_ch), ("Roughness", rough_ch), ("Metallic", metal_ch), ("Height", height_ch)]:
                if ch_data.shape != (height, width):
                    raise ValueError(f"{ch_name} channel has wrong shape: {ch_data.shape}, expected ({height}, {width})")
                if ch_data.min() < 0.0 or ch_data.max() > 1.0:
                    self.log(f"  [WARNING] {ch_name} channel out of range: [{ch_data.min():.3f}, {ch_data.max():.3f}]")
            
            arm = np.dstack([ao_ch, rough_ch, metal_ch, height_ch])
            
            # Final validation before uint8 conversion
            if arm.min() < 0.0 or arm.max() > 1.0:
                self.log(f"  [WARNING] ARM map out of range BEFORE clipping: [{arm.min():.3f}, {arm.max():.3f}]")
            
            arm_texture = to_uint8(np.clip(arm, 0.0, 1.0))
            
            # Validate uint8 output
            if arm_texture.min() < 0 or arm_texture.max() > 255:
                raise ValueError(f"ARM texture uint8 conversion failed: range [{arm_texture.min()}, {arm_texture.max()}]")
            
            self.log(f"  ✓ ARM map packed (R=AO, G=Roughness, B=Metallic, A=Height)")
            self._check_cancel()
            
            # Process emissive texture (optional $texture3)
            emissive_texture = None
            if selfillum_data is not None:
                self.log(f"[ExoPBR] Processing emissive texture...")
                if selfillum_data.ndim == 3 and selfillum_data.shape[2] >= 3:
                    emit_rgb = np.clip(selfillum_data[:, :, :3], 0.0, 1.0)
                    # Use alpha from self-illum if present, otherwise full opacity
                    if selfillum_data.shape[2] == 4:
                        emit_alpha = np.clip(selfillum_data[:, :, 3], 0.0, 1.0)
                    else:
                        emit_alpha = np.ones((height, width), dtype=np.float32)
                else:
                    # Single channel: replicate to RGB
                    ch = selfillum_data[:, :, 0] if selfillum_data.ndim == 3 else selfillum_data
                    emit_gray = np.clip(ch, 0.0, 1.0)
                    emit_rgb = np.dstack([emit_gray, emit_gray, emit_gray])
                    emit_alpha = np.ones((height, width), dtype=np.float32)
                emit_rgba = np.dstack([emit_rgb[:, :, 0], emit_rgb[:, :, 1], emit_rgb[:, :, 2], emit_alpha])
                emissive_texture = to_uint8(np.clip(emit_rgba, 0.0, 1.0))
                self.log(f"  ✓ Emissive texture processed")
                self._check_cancel()
            
            # Process normal map (keep unchanged - don't invert)
            self.log(f"[ExoPBR] Processing normal map...")
            # Ensure we have RGBA
            if normal_data.shape[2] == 4:
                rgb = np.clip(normal_data[:, :, :3], 0.0, 1.0)
                alpha = np.clip(normal_data[:, :, 3], 0.0, 1.0)
            else:
                rgb = np.clip(normal_data[:, :, :3], 0.0, 1.0)
                alpha = np.ones((height, width), dtype=np.float32)

            inv_rgb = 1.0 - rgb
            # Exo screenspace shader only needs XY; drop blue after inversion
            blue_zero = np.zeros((height, width), dtype=np.float32)
            normal_rgba = np.dstack([inv_rgb[:, :, 0], inv_rgb[:, :, 1], blue_zero, alpha])
            normal_texture = to_uint8(normal_rgba, clip=True)
            self.log(f"  ✓ Normal map inverted and blue channel cleared")
            self._check_cancel()
            
            # Conditionally encode to VTF
            vtf_generated = False
            if self.options.generate_vtf:
                self.log(f"[ExoPBR] Encoding textures to VTF...")
                
                os.makedirs(output_folder, exist_ok=True)
                base_path = os.path.join(output_folder, f"{material_name}_base.vtf")
                arm_path = os.path.join(output_folder, f"{material_name}_arm.vtf")
                normal_path = os.path.join(output_folder, f"{material_name}_normal.vtf")
                
                self._check_cancel()
                self.encoder.encode_to_vtf(
                    base_texture,
                    base_path,
                    image_format=vtfpp.ImageFormat.DXT5,
                    generate_mipmaps=self.options.generate_mipmaps
                )
                self.log(f"  ✓ Encoded {material_name}_base.vtf (alpha = opacity)")
                self._check_cancel()
                self.encoder.encode_to_vtf(
                    arm_texture,
                    arm_path,
                    image_format=vtfpp.ImageFormat.DXT5,
                    generate_mipmaps=self.options.generate_mipmaps
                )
                self.log(f"  ✓ Encoded {material_name}_arm.vtf")
                self._check_cancel()
                # Export as DXT5 RGBA to keep the inverted RGB and alpha
                self.encoder.encode_to_vtf(
                    normal_texture,
                    normal_path,
                    image_format=vtfpp.ImageFormat.DXT5,
                    invert_green=False,
                    generate_mipmaps=self.options.generate_mipmaps
                )
                self.log(f"  ✓ Encoded {material_name}_normal.vtf")
                
                # Encode emissive if present
                if emissive_texture is not None:
                    self._check_cancel()
                    emit_path = os.path.join(output_folder, f"{material_name}_emissive.vtf")
                    self.encoder.encode_to_vtf(
                        emissive_texture,
                        emit_path,
                        image_format=vtfpp.ImageFormat.DXT5,
                        generate_mipmaps=self.options.generate_mipmaps
                    )
                    self.log(f"  ✓ Encoded {material_name}_emissive.vtf")
                
                vtf_generated = True
            else:
                os.makedirs(output_folder, exist_ok=True)
                self.log(f"[ExoPBR] Skipping VTF generation (option disabled)")
            
            # Optionally generate VMT
            vmt_path = os.path.join(output_folder, f"{material_name}.vmt")
            if self.options.generate_vmt:
                self._check_cancel()
                self.log(f"[ExoPBR] Generating VMT...")
                
                # Auto-set emissionscale to 1.0 if emissive texture was provided
                final_emissionscale = self.options.emissionscale
                if inputs.selfillum is not None:
                    final_emissionscale = 1.0
                    self.log(f"  → Auto-setting $emissionscale 1.0 (emissive texture detected)")
                
                # Auto-set parallaxscale to 0.03 if height map was provided (unless user overrode)
                final_parallaxscale = self.options.parallaxscale
                if inputs.height is not None and self.options.parallaxscale == 0.0:
                    final_parallaxscale = 0.03
                    self.log(f"  → Auto-setting $parallaxscale 0.03 (height map detected)")
                
                generate_exopbr_vmt(
                    vmt_path,
                    material_name,
                    material_path,
                    basetexture_path=f"{material_path}/{material_name}_base",
                    texture1_path=f"{material_path}/{material_name}_arm",
                    texture2_path=f"{material_path}/{material_name}_normal",
                    texture3_path=f"{material_path}/{material_name}_emissive" if emissive_texture is not None else None,
                    emissionscale=final_emissionscale,
                    parallaxscale=final_parallaxscale
                )
                self.log(f"  ✓ Generated {material_name}.vmt")
            else:
                self.log(f"[ExoPBR] Skipping VMT generation (option disabled)")
            
            # Build success message depending on what was generated
            files = []
            if self.options.generate_vmt:
                files.append(f" - {material_name}.vmt")
            if vtf_generated:
                files.extend([f" - {material_name}_base.vtf", f" - {material_name}_arm.vtf", f" - {material_name}_normal.vtf"])
                if emissive_texture is not None:
                    files.append(f" - {material_name}_emissive.vtf")
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


class ExoProcessingThread(QThread):
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


class ExoAutomationThread(QThread):
    """Background thread to process multiple detected materials automatically"""

    progress = Signal(str)
    finished = Signal(bool, str)

    def __init__(self, processor_factory, tasks, max_workers: int = 2):
        super().__init__()
        self.processor_factory = processor_factory  # function to create an ExoPBRProcessor
        self.tasks = tasks  # list of dicts with inputs, output_folder, material_name, material_path
        self.max_workers = max(1, int(max_workers))

    def run(self):
        total = len(self.tasks)
        ok_count = 0
        cancelled = False

        def worker(idx: int, task: dict):
            if self.isInterruptionRequested():
                return (idx, task['material_name'], False, 'Cancelled')
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
                    task.get('material_path', 'exopbr')
                )
            finally:
                try:
                    proc.shutdown()
                except Exception:
                    pass
            return (idx, task['material_name'], success, msg)

        # Submit tasks to a thread pool with a bounded number of workers
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {}
            for idx, task in enumerate(self.tasks, start=1):
                if self.isInterruptionRequested():
                    cancelled = True
                    break
                futures[executor.submit(worker, idx, task)] = (idx, task['material_name'])

            # Process completions as they finish
            for future in as_completed(futures):
                if self.isInterruptionRequested():
                    cancelled = True
                    break
                try:
                    idx, name, success, msg = future.result()
                except Exception as e:
                    idx, name = futures[future]
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

        if cancelled or self.isInterruptionRequested():
            self.finished.emit(False, f"Cancelled after {ok_count}/{total} materials")
        else:
            all_ok = ok_count == total
            self.finished.emit(all_ok, f"Processed {ok_count}/{total} materials")


class ExoPBRTool(BaseTool):
    """
    ExoPBR Tool GUI Widget
    """
    
    def __init__(self):
        super().__init__("ExoPBR Tool")
        self.processor = None
        self.processing_thread = None
        self.automation_thread = None
        # History file locations
        try:
            cfg_dir = get_config_dir()
            self._history_file = cfg_dir / 'exopbr_history.json'
            self._auto_history_file = cfg_dir / 'exopbr_auto_history.json'
        except Exception:
            # Fallback: use local config path
            cfg = Path(__file__).parent.parent / 'config'
            self._history_file = cfg / 'exopbr_history.json'
            self._auto_history_file = cfg / 'exopbr_auto_history.json'

        # Separate history lists for manual and automation tabs
        self.history = []        # manual runs
        self.auto_history = []   # automation runs

        self._load_history()
        self._load_auto_history()
        self.setup_content()

        # Inbuilt keyword config for scanning
        self._keyword_config = {
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
            'selfillum': [
                "selfillum", "self_illum", "emissive", "emission", "glow", "emit"
            ],
            'height': [
                "height", "displacement", "disp", "heightmap", "height_map", "parallax", "h"
            ],
            'transparency_mask': [
                "transparency", "opacity", "mask", "alpha", "opac", "trans", "transparency_mask", "opacity_mask", "alpha_mask"
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
        # Common trailing tokens that are not part of texture-type suffixes
        self._trailing_ignore_tokens = {
            "1k", "2k", "4k", "8k", "16k", "32k", "512", "1024", "2048", "4096", "8192",
            "hi", "lo", "low", "high", "tile", "tiling"
        }
    
    def setup_content(self):
        """Setup the tool's UI content with Manual and Automate tabs"""
        tabs = QTabWidget()
        self.content_layout.addWidget(tabs)

        # ---------- Manual Tab ----------
        manual_tab = QWidget()
        manual_layout = QVBoxLayout(manual_tab)

        # History dropdown (previous runs)
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
        self.selfillum_input = self.create_file_input()
        self.height_input = self.create_file_input()
        self.transparency_mask_input = self.create_file_input()
        
        input_layout.addRow("Color (Required):", self.color_input)
        input_layout.addRow("Normal (Required):", self.normal_input)
        input_layout.addRow("AO (Optional):", self.ao_input)
        input_layout.addRow("Roughness (Optional):", self.roughness_input)
        input_layout.addRow("Metallic (Optional):", self.metallic_input)
        input_layout.addRow("Self-Illum (Optional):", self.selfillum_input)
        input_layout.addRow("Height (Optional):", self.height_input)
        input_layout.addRow("Transparency Mask (Optional):", self.transparency_mask_input)
        
        input_group.setLayout(input_layout)
        manual_layout.addWidget(input_group)
        
        # Options group
        options_group = QGroupBox("Processing Options")
        options_layout = QFormLayout()
        
        # Emission scale
        self.emission_spin = QDoubleSpinBox()
        self.emission_spin.setRange(0.0, 10.0)
        self.emission_spin.setSingleStep(0.1)
        self.emission_spin.setDecimals(2)
        self.emission_spin.setValue(0.0)
        options_layout.addRow("$emissionscale:", self.emission_spin)
        
        # Parallax scale
        self.parallax_spin = QDoubleSpinBox()
        self.parallax_spin.setRange(0.0, 1.0)
        self.parallax_spin.setSingleStep(0.01)
        self.parallax_spin.setDecimals(3)
        self.parallax_spin.setValue(0.0)
        options_layout.addRow("$parallaxscale:", self.parallax_spin)

        # Alpha blend checkbox
        self.alphablend_checkbox = QCheckBox("Enable partial opacity")
        self.alphablend_checkbox.setChecked(False)
        options_layout.addRow("$alphablend:", self.alphablend_checkbox)

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
        self.material_name_input.setPlaceholderText("e.g., test_material")
        output_layout.addRow("Material Base Name:", self.material_name_input)
        
        # Material path
        self.material_path_input = QLineEdit()
        self.material_path_input.setPlaceholderText("e.g., exopbr/materials")
        self.material_path_input.setText("exopbr")
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
        the right. Mirrors fake_pbr_tool / vmat_pbr_tool / gltf_smd_batch_tool."""
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
        col.addStretch()

        scroll.setWidget(container)
        scroll.setMinimumWidth(420)
        return scroll

    def _build_auto_folders_group(self) -> QGroupBox:
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
        self.auto_material_path.setPlaceholderText("e.g., exopbr")
        self.auto_material_path.setText("exopbr")
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
        group = QGroupBox("Processing Options")
        form = QFormLayout()

        self.auto_emission_spin = QDoubleSpinBox()
        self.auto_emission_spin.setRange(0.0, 10.0)
        self.auto_emission_spin.setSingleStep(0.1)
        self.auto_emission_spin.setDecimals(2)
        self.auto_emission_spin.setValue(0.0)
        form.addRow("$emissionscale:", self.auto_emission_spin)

        self.auto_parallax_spin = QDoubleSpinBox()
        self.auto_parallax_spin.setRange(0.0, 1.0)
        self.auto_parallax_spin.setSingleStep(0.01)
        self.auto_parallax_spin.setDecimals(3)
        self.auto_parallax_spin.setValue(0.0)
        form.addRow("$parallaxscale:", self.auto_parallax_spin)

        self.auto_alphablend = QCheckBox("Enable partial opacity")
        self.auto_alphablend.setChecked(False)
        form.addRow("$alphablend:", self.auto_alphablend)

        group.setLayout(form)
        return group

    def _build_auto_results_pane(self) -> QWidget:
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
        self.scan_table = QTableWidget(0, 10)
        self.scan_table.setHorizontalHeaderLabels([
            "Include", "Material", "Color", "Normal", "AO", "Roughness",
            "Metallic", "SelfIllum", "Height", "Transparency",
        ])
        header = self.scan_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        self.scan_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.scan_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.scan_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.scan_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.scan_table.installEventFilter(self)
        col.addWidget(self.scan_table, 1)

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
            self.history = []
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
        if not hasattr(self, 'history_dropdown'):
            return
        self.history_dropdown.blockSignals(True)
        self.history_dropdown.clear()
        self.history_dropdown.addItem("-- Recent runs --")
        for entry in self.history:
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
            self.auto_material_path.setText(entry.get('material_path') or 'exopbr')
            self.auto_prefix.setText(entry.get('prefix') or '')
            self.auto_suffix.setText(entry.get('suffix') or '')

            opts = entry.get('options', {})
            self.auto_emission_spin.setValue(float(opts.get('emissionscale', opts.get('emissionscale', 0.0))))
            if hasattr(self, 'auto_parallax_spin'):
                self.auto_parallax_spin.setValue(float(opts.get('parallaxscale', 0.0)))
            self.auto_alphablend.setChecked(bool(opts.get('alphablend', False)))
            self.auto_generate_vtf.setChecked(bool(opts.get('generate_vtf', True)))
            self.auto_generate_vmt.setChecked(bool(opts.get('generate_vmt', True)))
            self.auto_generate_mipmaps.setChecked(bool(opts.get('generate_mipmaps', True)))
        except Exception:
            pass

    def on_history_selected(self, index: int):
        """Populate UI fields when a previous run is selected"""
        if index <= 0:
            return
        history_index = index - 1
        try:
            entry = self.history[history_index]
        except Exception:
            return

        # Populate inputs
        inputs = entry.get('inputs', {})
        try:
            self.color_input.line_edit.setText(inputs.get('color') or '')
            self.normal_input.line_edit.setText(inputs.get('normal') or '')
            self.ao_input.line_edit.setText(inputs.get('ao') or '')
            self.roughness_input.line_edit.setText(inputs.get('roughness') or '')
            self.metallic_input.line_edit.setText(inputs.get('metallic') or '')
            if hasattr(self, 'selfillum_input'):
                self.selfillum_input.line_edit.setText(inputs.get('selfillum') or '')
            if hasattr(self, 'height_input'):
                self.height_input.line_edit.setText(inputs.get('height') or '')
            if hasattr(self, 'transparency_mask_input'):
                self.transparency_mask_input.line_edit.setText(inputs.get('transparency_mask') or '')
        except Exception:
            pass

        # Populate options
        options = entry.get('options', {})
        try:
            self.emission_spin.setValue(float(options.get('emissionscale', options.get('emissionscale', 0.0))))
            if hasattr(self, 'parallax_spin'):
                self.parallax_spin.setValue(float(options.get('parallaxscale', 0.0)))
            self.alphablend_checkbox.setChecked(bool(options.get('alphablend', False)))
            self.generate_vtf_checkbox.setChecked(bool(options.get('generate_vtf', True)))
            self.generate_vmt_checkbox.setChecked(bool(options.get('generate_vmt', True)))
            self.generate_mipmaps_checkbox.setChecked(bool(options.get('generate_mipmaps', True)))
        except Exception:
            pass

        # Populate outputs
        try:
            self.output_folder_input.setText(entry.get('output_folder') or '')
            self.material_name_input.setText(entry.get('material_name') or '')
            self.material_path_input.setText(entry.get('material_path') or 'exopbr')
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
                'metallic': inputs.metallic or '',
                'selfillum': inputs.selfillum or '',
                'height': inputs.height or '',
                'transparency_mask': inputs.transparency_mask or ''
            },
            'options': {
                'emissionscale': options.emissionscale,
                'parallaxscale': options.parallaxscale,
                'alphablend': options.alphablend,
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
            # Avoid duplicates
            if len(self.history) > 0 and self.history[0].get('inputs') == entry.get('inputs') and self.history[0].get('output_folder') == entry.get('output_folder') and self.history[0].get('material_name') == entry.get('material_name'):
                return
            self.history = [h for h in self.history if not (
                h.get('inputs') == entry.get('inputs') and
                h.get('output_folder') == entry.get('output_folder') and
                h.get('material_name') == entry.get('material_name')
            )]
            self.history.insert(0, entry)
            MAX = 20
            if len(self.history) > MAX:
                self.history = self.history[:MAX]
            self._save_history_file()
            self._refresh_history_dropdown()
        except Exception:
            pass

    def _make_auto_history_entry(self):
        """Construct an automation history entry from current automation UI state"""
        opts = {
            'emissionscale': float(self.auto_emission_spin.value()),
            'parallaxscale': float(self.auto_parallax_spin.value()) if hasattr(self, 'auto_parallax_spin') else 0.0,
            'alphablend': self.auto_alphablend.isChecked(),
            'generate_vtf': self.auto_generate_vtf.isChecked(),
            'generate_vmt': self.auto_generate_vmt.isChecked(),
            'generate_mipmaps': self.auto_generate_mipmaps.isChecked(),
        }
        label = self.auto_input_folder.text() or self.auto_output_folder.text() or ''
        return {
            'timestamp': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
            'label': label,
            'input_root': self.auto_input_folder.text() or '',
            'output_folder': self.auto_output_folder.text() or '',
            'material_path': self.auto_material_path.text() or 'exopbr',
            'prefix': self.auto_prefix.text() or '',
            'suffix': self.auto_suffix.text() or '',
            'options': opts,
        }

    def _save_current_auto_run_to_history(self):
        """Save current automation UI state to automation history and persist it"""
        try:
            entry = self._make_auto_history_entry()
            if (
                len(self.auto_history) > 0 and
                self.auto_history[0].get('input_root') == entry.get('input_root') and
                self.auto_history[0].get('output_folder') == entry.get('output_folder') and
                self.auto_history[0].get('prefix') == entry.get('prefix') and
                self.auto_history[0].get('suffix') == entry.get('suffix')
            ):
                return

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
    
    def get_processing_options(self) -> ExoPBROptions:
        """Get current processing options from UI"""
        return ExoPBROptions(
            generate_vtf=self.generate_vtf_checkbox.isChecked(),
            generate_vmt=self.generate_vmt_checkbox.isChecked(),
            generate_mipmaps=self.generate_mipmaps_checkbox.isChecked(),
            emissionscale=float(self.emission_spin.value()),
            parallaxscale=float(self.parallax_spin.value()),
            alphablend=self.alphablend_checkbox.isChecked()
        )
    
    def get_inputs(self) -> ExoPBRInputs:
        """Get input file paths from UI"""
        return ExoPBRInputs(
            color=self.color_input.line_edit.text() or None,
            normal=self.normal_input.line_edit.text() or None,
            ao=self.ao_input.line_edit.text() or None,
            roughness=self.roughness_input.line_edit.text() or None,
            metallic=self.metallic_input.line_edit.text() or None,
            selfillum=self.selfillum_input.line_edit.text() or None,
            height=self.height_input.line_edit.text() or None,
            transparency_mask=self.transparency_mask_input.line_edit.text() or None
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
        
        material_path = self.material_path_input.text() or "exopbr"
        
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
        self.processor = ExoPBRProcessor(options)

        # Save run to history before starting
        try:
            self._save_current_run_to_history()
        except Exception:
            pass
        
        # Start processing thread
        self.processing_thread = ExoProcessingThread(
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
        tokens = re.split(r"[^a-z0-9]+", name.lower())
        return [t for t in tokens if t]

    def _detect_type_and_key(self, filepath: str):
        """Detect texture type from filename."""
        from pathlib import Path as _Path
        p = _Path(filepath)
        stem = p.stem.lower()
        tokens = self._tokenize(stem)
        if not tokens:
            return None, None

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
        """Scan a folder for PBR material sets"""
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
        folder_index: Dict[str, Dict[str, Dict[str, str]]] = {}
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
                d = matches.setdefault(key, {})
                d.setdefault('material_name', key)
                d.setdefault('folder', base)
                d.setdefault(ttype, path)
                f_idx = folder_index.setdefault(base, {})
                f_key = f_idx.setdefault(key, {})
                f_key.setdefault(ttype, path)
        # Completion pass
        for key, data in matches.items():
            folder = data.get('folder')
            if not folder:
                continue
            per_key = folder_index.get(folder, {}).get(key, {})
            if not per_key:
                per_key = {}
            base_tokens = key.split('_') if key else []
            for typ in ("color", "normal", "ao", "roughness", "metallic", "selfillum", "height", "transparency_mask"):
                if typ in data:
                    continue
                if typ in per_key:
                    data[typ] = per_key[typ]
                    continue
                for fpath in folder_files.get(folder, []):
                    stem2 = os.path.splitext(os.path.basename(fpath))[0].lower()
                    toks2 = self._tokenize(stem2)
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
        self._populate_scan_table(matches)
        self.process_all_button.setEnabled(self.scan_table.rowCount() > 0)
        self.log(f"Scan complete: {len(matches)} material sets detected from {count_files} files", "INFO")

    def _populate_scan_table(self, matches: dict):
        """Populate the scan results table"""
        self.scan_table.setRowCount(0)
        for key, data in sorted(matches.items()):
            row = self.scan_table.rowCount()
            self.scan_table.insertRow(row)
            include_item = QTableWidgetItem()
            include_item.setCheckState(Qt.Checked)
            include_item.setFlags(include_item.flags() | Qt.ItemIsUserCheckable)
            self.scan_table.setItem(row, 0, include_item)
            name_item = QTableWidgetItem(key)
            self.scan_table.setItem(row, 1, name_item)
            for col, typ in enumerate(["color", "normal", "ao", "roughness", "metallic", "selfillum", "height", "transparency_mask"], start=2):
                path = data.get(typ)
                txt = "Yes" if path else "No"
                item = QTableWidgetItem(txt)
                if path:
                    item.setToolTip(path)
                self.scan_table.setItem(row, col, item)
        self._scan_matches = matches

    def _exopbr_outputs_exist(self, output_folder: str, material_name: str,
                              gen_vmt: bool, gen_vtf: bool, has_selfillum: bool) -> bool:
        """Return True if all enabled outputs for this material already exist."""
        if not output_folder:
            return False
        paths = []
        if gen_vmt:
            paths.append(os.path.join(output_folder, f"{material_name}.vmt"))
        if gen_vtf:
            paths.extend([
                os.path.join(output_folder, f"{material_name}_base.vtf"),
                os.path.join(output_folder, f"{material_name}_arm.vtf"),
                os.path.join(output_folder, f"{material_name}_normal.vtf"),
            ])
            if has_selfillum:
                paths.append(os.path.join(output_folder, f"{material_name}_emissive.vtf"))
        if not paths:
            return False
        return all(os.path.exists(p) for p in paths)

    def process_all_materials(self):
        """Process all selected materials in automation tab"""
        if not hasattr(self, '_scan_matches') or not self._scan_matches:
            self.log("No materials to process. Run Scan first.", "ERROR")
            return
        out_dir = self.auto_output_folder.text().strip()
        mat_path = self.auto_material_path.text().strip() or "exopbr"
        prefix = self.auto_prefix.text().strip()
        suffix = self.auto_suffix.text().strip()

        # Options
        emission = float(self.auto_emission_spin.value())
        parallax = float(self.auto_parallax_spin.value()) if hasattr(self, 'auto_parallax_spin') else 0.0
        alphablend = self.auto_alphablend.isChecked()
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
            color = data.get('color')
            normal = data.get('normal')
            if not color or not normal:
                self.log(f"Skipping '{key}': missing color or normal", "WARNING")
                continue
            material_name = f"{prefix}{key}{suffix}"
            has_selfillum = bool(data.get('selfillum'))
            if skip_existing and self._exopbr_outputs_exist(out_dir, material_name, gen_vmt, gen_vtf, has_selfillum):
                skipped_existing += 1
                self.log(f"Skipping '{material_name}': outputs already exist", "INFO")
                continue
            inputs = ExoPBRInputs(
                color=color,
                normal=normal,
                ao=data.get('ao'),
                roughness=data.get('roughness'),
                metallic=data.get('metallic'),
                selfillum=data.get('selfillum'),
                height=data.get('height'),
                transparency_mask=data.get('transparency_mask')
            )
            tasks.append({
                'inputs': inputs,
                'output_folder': out_dir,
                'material_name': material_name,
                'material_path': mat_path,
                'options': ExoPBROptions(
                    generate_vtf=gen_vtf,
                    generate_vmt=gen_vmt,
                    generate_mipmaps=self.auto_generate_mipmaps.isChecked(),
                    emissionscale=emission,
                    parallaxscale=parallax,
                    alphablend=alphablend
                )
            })
        if skip_existing and skipped_existing:
            self.log(f"Skipped {skipped_existing} material(s) with existing outputs.", "INFO")
        if not tasks:
            self.log("No valid tasks to process.", "ERROR")
            return

        # Save current automation configuration
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
            proc = ExoPBRProcessor(ExoPBROptions(
                generate_vtf=self.auto_generate_vtf.isChecked(),
                generate_vmt=gen_vmt,
                generate_mipmaps=self.auto_generate_mipmaps.isChecked(),
                emissionscale=emission,
                parallaxscale=parallax,
                alphablend=alphablend
            ))
            return proc

        self.automation_thread = ExoAutomationThread(make_processor, tasks, max_workers=self.auto_max_parallel.value())
        self.automation_thread.progress.connect(lambda m: self.log(m, "INFO"))
        self.automation_thread.finished.connect(self.on_automation_finished)
        self.automation_thread.start()

    def on_automation_finished(self, success: bool, message: str):
        """Handle automation completion"""
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
