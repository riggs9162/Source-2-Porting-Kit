"""
FakePBR Tool - Convert Source 2 PBR textures to Source 1 materials

This tool converts up to 5 Source 2 textures into a full Source 1-compatible
material set with proper Phong, envmap masking, and VTF encoding.
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
    QLineEdit, QGroupBox, QSlider, QDoubleSpinBox, QCheckBox,
    QProgressBar, QFormLayout, QWidget, QComboBox, QTabWidget,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QSpinBox
)
from PySide6.QtCore import Qt, QThread, Signal
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Event

from .base_tool import BaseTool
from ..utils.vtf_encoder import VTFEncoder, VTFEncoderError
from ..utils.helpers import get_config_dir
from ..utils.image_processing import load_image, resize_to_match
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
    metal_diffuse_suppression: float = 0.85
    envmask_gamma: float = 1.5
    invert_green: bool = False


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
            self._check_cancel()
            
            normal_data = load_image(inputs.normal)
            if normal_data is not None:
                self.log(f"  ✓ Loaded normal map: {os.path.basename(inputs.normal)}")
            self._check_cancel()
            
            ao_data = load_image(inputs.ao)
            if ao_data is not None:
                self.log(f"  ✓ Loaded AO map: {os.path.basename(inputs.ao)}")
            else:
                self.log("  ℹ No AO map provided (will use default)")
            self._check_cancel()
            
            roughness_data = load_image(inputs.roughness)
            if roughness_data is not None:
                self.log(f"  ✓ Loaded roughness map: {os.path.basename(inputs.roughness)}")
            else:
                self.log("  ℹ No roughness map provided (will use default: 0.5)")
            self._check_cancel()
            
            metallic_data = load_image(inputs.metallic)
            if metallic_data is not None:
                self.log(f"  ✓ Loaded metallic map: {os.path.basename(inputs.metallic)}")
            else:
                self.log("  ℹ No metallic map provided (will use dielectric)")
            self._check_cancel()
            
            # Validate required inputs
            if color_data is None:
                return False, "Color map is required"
            
            if normal_data is None:
                return False, "Normal map is required"
            
            # Determine target resolution: use LARGEST dimension from all inputs
            # This prevents downsampling high-res textures
            all_inputs = [color_data, normal_data, ao_data, roughness_data, metallic_data]
            max_height = max(img.shape[0] for img in all_inputs if img is not None)
            max_width = max(img.shape[1] for img in all_inputs if img is not None)
            height, width = max_height, max_width
            
            self.log(f"[FakePBR] Processing at resolution: {width}x{height}")
            self.log(f"[FakePBR] AO Strength: {self.options.ao_strength:.2f}")
            self.log(f"[FakePBR] Gloss Gamma: {self.options.gloss_gamma:.2f}")
            self._check_cancel()
            
            # Resize all inputs to match target resolution
            color_data = resize_to_match(color_data, height, width, "color")
            normal_data = resize_to_match(normal_data, height, width, "normal")
            ao_data = resize_to_match(ao_data, height, width, "AO")
            roughness_data = resize_to_match(roughness_data, height, width, "roughness")
            metallic_data = resize_to_match(metallic_data, height, width, "metallic")
            self._check_cancel()
            
            # Process base texture
            self.log(f"[FakePBR] Processing base texture...")
            self.log(f"  → Converting color to linear space")
            self.log(f"  → Applying AO (strength: {self.options.ao_strength:.2f})")
            self.log(f"  → Adding metallic mask to alpha channel")
            base_texture = process_base_texture(
                color_data, ao_data, metallic_data, self.options.ao_strength
            )
            self.log(f"  ✓ Base texture processed")
            self._check_cancel()
            
            # Process normal map
            self.log(f"[FakePBR] Packing normal map...")
            self.log(f"  → Preserving RGB normal channels")
            self.log(f"  → Computing envmap mask: metal × (1-roughness) × ao")
            normal_texture = pack_normal_with_envmap(
                normal_data, ao_data, metallic_data, roughness_data
            )
            self.log(f"  ✓ Normal map packed with envmap mask in alpha")
            self._check_cancel()
            
            # Process phong/gloss map
            self.log(f"[FakePBR] Computing gloss map...")
            self.log(f"  → Converting roughness to gloss: (1-r)^{self.options.gloss_gamma:.2f}")
            self.log(f"  → Computing rimlight mask: gloss × AO")
            phong_texture = create_phong_texture(
                roughness_data, ao_data, self.options.gloss_gamma, height, width
            )
            self.log(f"  ✓ Gloss map computed with rimlight mask")
            self._check_cancel()
            
            # Conditionally encode to VTF
            vtf_generated = False
            if self.options.generate_vtf:
                self.log(f"[FakePBR] Encoding textures to VTF...")
                
                base_path = os.path.join(output_folder, f"{material_name}_color.vtf")
                normal_path = os.path.join(output_folder, f"{material_name}_normal.vtf")
                phong_path = os.path.join(output_folder, f"{material_name}_phong.vtf")
                self._check_cancel()
                self.encoder.encode_base_texture(base_texture, base_path, generate_mipmaps=self.options.generate_mipmaps)
                self.log(f"  ✓ Encoded {material_name}_color.vtf")
                self._check_cancel()
                self.encoder.encode_normal_map(normal_texture, normal_path, generate_mipmaps=self.options.generate_mipmaps)
                self.log(f"  ✓ Encoded {material_name}_normal.vtf")
                self._check_cancel()
                self.encoder.encode_phong_map(phong_texture, phong_path, generate_mipmaps=self.options.generate_mipmaps)
                self.log(f"  ✓ Encoded {material_name}_phong.vtf")
                vtf_generated = True
            else:
                self.log(f"[FakePBR] Skipping VTF generation (option disabled)")
            
            # Optionally generate VMT
            vmt_path = os.path.join(output_folder, f"{material_name}.vmt")
            if self.options.generate_vmt:
                self._check_cancel()
                self.log(f"[FakePBR] Generating VMT...")
                generate_pbr_vmt(vmt_path, material_name, material_path)
                self.log(f"  ✓ Generated {material_name}.vmt")
            else:
                self.log(f"[FakePBR] Skipping VMT generation (option disabled)")
            
            # Build success message depending on what was generated
            files = []
            if self.options.generate_vmt:
                files.append(f" - {material_name}.vmt")
            if vtf_generated:
                files.extend([f" - {material_name}_color.vtf", f" - {material_name}_normal.vtf", f" - {material_name}_phong.vtf"])
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

    def __init__(self, processor_factory, tasks, max_workers: int = 2):
        super().__init__()
        self.processor_factory = processor_factory  # function to create a FakePBRProcessor
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
                    task.get('material_path', 'models/ports')
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

        # ---------- Automate Tab (new UI) ----------
        automate_tab = QWidget()
        automate_layout = QVBoxLayout(automate_tab)

        # History dropdown (previous runs for automation)
        auto_history_group = QGroupBox("Previous Runs")
        auto_history_form = QFormLayout()
        self.auto_history_dropdown = QComboBox()
        self.auto_history_dropdown.addItem("-- Recent runs --")
        self.auto_history_dropdown.currentIndexChanged.connect(self.on_auto_history_selected)
        auto_history_form.addRow("Select Run:", self.auto_history_dropdown)
        auto_history_group.setLayout(auto_history_form)
        automate_layout.addWidget(auto_history_group)

        # Scan settings
        scan_group = QGroupBox("Automation Scan")
        scan_form = QFormLayout()

        # Input root folder
        self.auto_input_folder = QLineEdit()
        auto_browse_in = QPushButton("Browse...")
        auto_browse_in.clicked.connect(lambda: self._browse_dir_into(self.auto_input_folder))
        in_row = QHBoxLayout()
        in_row.addWidget(self.auto_input_folder)
        in_row.addWidget(auto_browse_in)
        scan_form.addRow("Input Root Folder:", self._row_widget(in_row))

        # Output folder
        self.auto_output_folder = QLineEdit()
        auto_browse_out = QPushButton("Browse...")
        auto_browse_out.clicked.connect(lambda: self._browse_dir_into(self.auto_output_folder))
        out_row = QHBoxLayout()
        out_row.addWidget(self.auto_output_folder)
        out_row.addWidget(auto_browse_out)
        scan_form.addRow("Output Folder:", self._row_widget(out_row))

        # Material path
        self.auto_material_path = QLineEdit()
        self.auto_material_path.setPlaceholderText("e.g., models/ports")
        self.auto_material_path.setText("models/ports")
        scan_form.addRow("Material Path:", self.auto_material_path)

        # Material prefix
        self.auto_prefix = QLineEdit()
        self.auto_prefix.setPlaceholderText("e.g., combine_")
        scan_form.addRow("Material Prefix:", self.auto_prefix)

        # Material suffix
        self.auto_suffix = QLineEdit()
        self.auto_suffix.setPlaceholderText("e.g., _arctic")
        scan_form.addRow("Material Suffix:", self.auto_suffix)

        scan_group.setLayout(scan_form)
        automate_layout.addWidget(scan_group)

        # Options (reuse same sliders)
        auto_options = QGroupBox("Processing Options")
        auto_opt_form = QFormLayout()

        # AO Strength
        auto_ao_row = QHBoxLayout()
        self.auto_ao_slider = QSlider(Qt.Horizontal)
        self.auto_ao_slider.setRange(0, 200)
        self.auto_ao_slider.setValue(50)
        self.auto_ao_value = QLabel("0.50")
        self.auto_ao_slider.valueChanged.connect(lambda v: self.auto_ao_value.setText(f"{v/100:.2f}"))
        auto_ao_row.addWidget(self.auto_ao_slider)
        auto_ao_row.addWidget(self.auto_ao_value)
        auto_opt_form.addRow("AO Strength:", self._row_widget(auto_ao_row))

        # Gloss Gamma
        auto_gamma_row = QHBoxLayout()
        self.auto_gamma_slider = QSlider(Qt.Horizontal)
        self.auto_gamma_slider.setRange(10, 40)
        self.auto_gamma_slider.setValue(22)
        self.auto_gamma_value = QLabel("2.20")
        self.auto_gamma_slider.valueChanged.connect(lambda v: self.auto_gamma_value.setText(f"{v/10:.2f}"))
        auto_gamma_row.addWidget(self.auto_gamma_slider)
        auto_gamma_row.addWidget(self.auto_gamma_value)
        auto_opt_form.addRow("Gloss Gamma:", self._row_widget(auto_gamma_row))

        # Generate VTF
        self.auto_generate_vtf = QCheckBox("Generate VTF")
        self.auto_generate_vtf.setChecked(True)
        auto_opt_form.addRow("Generate VTF:", self.auto_generate_vtf)

        # Generate mipmaps
        self.auto_generate_mipmaps = QCheckBox("Generate Mipmaps")
        self.auto_generate_mipmaps.setChecked(True)
        auto_opt_form.addRow("Generate Mipmaps:", self.auto_generate_mipmaps)

        # Generate VMT
        self.auto_generate_vmt = QCheckBox("Generate VMT")
        self.auto_generate_vmt.setChecked(True)
        auto_opt_form.addRow("Generate VMT:", self.auto_generate_vmt)

        # Max parallel workers
        max_cpu = os.cpu_count() or 4
        self.auto_max_parallel = QSpinBox()
        self.auto_max_parallel.setRange(1, max(2, max_cpu))
        self.auto_max_parallel.setValue(min(4, max_cpu))
        auto_opt_form.addRow("Max Parallel:", self.auto_max_parallel)

        auto_options.setLayout(auto_opt_form)
        automate_layout.addWidget(auto_options)

        # Results table
        self.scan_table = QTableWidget(0, 7)
        self.scan_table.setHorizontalHeaderLabels([
            "Include", "Material", "Color", "Normal", "AO", "Roughness", "Metallic"
        ])
        header = self.scan_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        self.scan_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.scan_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        automate_layout.addWidget(self.scan_table)

        # Buttons
        btn_row = QHBoxLayout()
        self.scan_button = QPushButton("Scan Folder")
        self.scan_button.clicked.connect(self.scan_folder_for_materials)
        self.process_all_button = QPushButton("Process All")
        self.process_all_button.setEnabled(False)
        self.process_all_button.clicked.connect(self.process_all_materials)
        # Cancel button (automate)
        self.cancel_all_button = QPushButton("Cancel")
        self.cancel_all_button.setEnabled(False)
        self.cancel_all_button.clicked.connect(self.cancel_automation)
        btn_row.addWidget(self.scan_button)
        btn_row.addStretch()
        btn_row.addWidget(self.cancel_all_button)
        btn_row.addWidget(self.process_all_button)
        automate_layout.addLayout(btn_row)

        tabs.addTab(automate_tab, "Automate")

        # Populate history dropdowns now that UI exists
        try:
            self._refresh_history_dropdown()
            self._refresh_auto_history_dropdown()
        except Exception:
            pass
    
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
            self.auto_ao_slider.setValue(int(ao_val * 100))
            self.auto_gamma_slider.setValue(int(gamma_val * 10))
            self.auto_generate_vtf.setChecked(bool(opts.get('generate_vtf', True)))
            self.auto_generate_vmt.setChecked(bool(opts.get('generate_vmt', True)))
            self.auto_generate_mipmaps.setChecked(bool(opts.get('generate_mipmaps', True)))
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
            self.ao_strength_slider.setValue(int(ao_val * 100))
            self.gloss_gamma_slider.setValue(int(gamma_val * 10))
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
        for base, _, files in os.walk(root):
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
        self.process_all_button.setEnabled(self.scan_table.rowCount() > 0)
        self.log(f"Scan complete: {len(matches)} material sets detected from {count_files} files", "INFO")

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
        gen_vmt = self.auto_generate_vmt.isChecked()

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
                'material_name': f"{prefix}{key}{suffix}",
                'material_path': mat_path,
                'options': ProcessingOptions(
                    ao_strength=ao_strength,
                    gloss_gamma=gloss_gamma,
                    generate_vtf=self.auto_generate_vtf.isChecked(),
                    generate_vmt=gen_vmt,
                    generate_mipmaps=self.auto_generate_mipmaps.isChecked()
                )
            })
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
                generate_vtf=self.auto_generate_vtf.isChecked(),
                generate_vmt=gen_vmt,
                generate_mipmaps=self.auto_generate_mipmaps.isChecked()
            ))
            return proc

        self.automation_thread = AutomationThread(make_processor, tasks, max_workers=self.auto_max_parallel.value())
        self.automation_thread.progress.connect(lambda m: self.log(m, "INFO"))
        self.automation_thread.finished.connect(self.on_automation_finished)
        self.automation_thread.start()

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
