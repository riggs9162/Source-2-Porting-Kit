"""
VRF Batch Export Tool

GUI front-end for ValveResourceFormat's Source2Viewer-CLI: decompile a single
.vmdl_c or a folder of compiled Source 2 assets to GLTF/GLB plus a
`materials/` tree (VMATs + texture PNGs), stripping the stray PNGs VRF drops
next to the .gltf.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
)

from app.core.settings import Settings
from app.tools.base_tool import BaseTool
from app.utils.vrf_runner import (
    VrfRunnerError,
    is_vpk,
    resolve_vrf_executable,
    run_vrf_export,
)


class VrfExportWorker(QThread):
    """Background worker that runs VRF and post-cleans stray images."""

    progress = Signal(str, str)   # message, level
    finished_with = Signal(int)   # process return code (0 == success)

    def __init__(
        self,
        vrf_exe: Path,
        input_path: Path,
        output_dir: Path,
        gltf_format: str,
        recursive: bool,
        threads: int,
        keep_stray_images: bool,
        vpk_filepath: Optional[str] = None,
    ):
        super().__init__()
        self.vrf_exe = vrf_exe
        self.input_path = input_path
        self.output_dir = output_dir
        self.gltf_format = gltf_format
        self.recursive = recursive
        self.threads = threads
        self.keep_stray_images = keep_stray_images
        self.vpk_filepath = vpk_filepath

    def run(self):
        try:
            rc = run_vrf_export(
                vrf_exe=self.vrf_exe,
                input_path=self.input_path,
                output_dir=self.output_dir,
                gltf_format=self.gltf_format,
                recursive=self.recursive,
                threads=self.threads,
                keep_stray_images=self.keep_stray_images,
                vpk_filepath=self.vpk_filepath,
                on_log=lambda msg: self.progress.emit(msg, "INFO"),
            )
            if rc == 0:
                self.progress.emit("Export complete.", "SUCCESS")
            else:
                self.progress.emit(f"VRF returned non-zero exit code: {rc}", "ERROR")
            self.finished_with.emit(rc)
        except VrfRunnerError as e:
            self.progress.emit(str(e), "ERROR")
            self.finished_with.emit(2)
        except Exception as e:  # noqa: BLE001
            self.progress.emit(f"Unexpected error: {e}", "ERROR")
            self.finished_with.emit(1)


class VrfBatchExportTool(BaseTool):
    """GUI tool: VRF batch export for GLTF + materials/ tree."""

    def __init__(self):
        super().__init__("VRF Batch Export")
        self.worker: Optional[VrfExportWorker] = None
        self.settings = Settings()
        self._setup_tool_ui()

    def _setup_tool_ui(self):
        layout: QVBoxLayout = self.content_layout

        # --- Recent runs -------------------------------------------------
        recent_group = QGroupBox("Recent runs")
        recent_row = QHBoxLayout(recent_group)
        self.recent_combo = QComboBox()
        self.recent_combo.activated.connect(self._on_recent_selected)
        clear_recent_btn = QPushButton("Clear")
        clear_recent_btn.clicked.connect(self._on_clear_recent)
        recent_row.addWidget(self.recent_combo, 1)
        recent_row.addWidget(clear_recent_btn)
        layout.addWidget(recent_group)
        self._refresh_recent_combo()

        # --- VRF binary --------------------------------------------------
        vrf_group = QGroupBox("Source2Viewer-CLI executable")
        vrf_form = QFormLayout(vrf_group)
        self.vrf_path_edit = QLineEdit(self.settings.get_vrf_cli_path())
        self.vrf_path_edit.editingFinished.connect(self._save_vrf_path)
        vrf_browse_btn = QPushButton("Browse…")
        vrf_browse_btn.clicked.connect(self._browse_vrf)
        vrf_row = QHBoxLayout()
        vrf_row.addWidget(self.vrf_path_edit)
        vrf_row.addWidget(vrf_browse_btn)
        vrf_form.addRow("Path:", vrf_row)
        layout.addWidget(vrf_group)

        # --- Input -------------------------------------------------------
        input_group = QGroupBox("Input")
        input_form = QFormLayout(input_group)
        self.input_edit = QLineEdit()
        self.input_edit.textChanged.connect(self._update_vpk_field_state)
        input_file_btn = QPushButton("Pick file…")
        input_file_btn.clicked.connect(self._browse_input_file)
        input_dir_btn = QPushButton("Pick folder…")
        input_dir_btn.clicked.connect(self._browse_input_folder)
        input_vpk_btn = QPushButton("Pick VPK…")
        input_vpk_btn.clicked.connect(self._browse_input_vpk)
        input_row = QHBoxLayout()
        input_row.addWidget(self.input_edit)
        input_row.addWidget(input_file_btn)
        input_row.addWidget(input_dir_btn)
        input_row.addWidget(input_vpk_btn)
        input_form.addRow("Path:", input_row)

        # VPK internal path filter (e.g. "models/props_c17/")
        self.vpk_path_edit = QLineEdit()
        self.vpk_path_edit.setPlaceholderText("e.g. models/props_c17/  (only used when input is a .vpk)")
        input_form.addRow("Path inside VPK:", self.vpk_path_edit)

        layout.addWidget(input_group)
        self._update_vpk_field_state(self.input_edit.text())

        # --- Output ------------------------------------------------------
        output_group = QGroupBox("Output (project root)")
        output_form = QFormLayout(output_group)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText(
            "Project/addon folder; modelsrc/ and materialsrc/ will be created here"
        )
        output_btn = QPushButton("Browse…")
        output_btn.clicked.connect(self._browse_output)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_edit)
        output_row.addWidget(output_btn)
        output_form.addRow("Folder:", output_row)
        layout.addWidget(output_group)

        # --- Options -----------------------------------------------------
        opts_group = QGroupBox("Options")
        opts_form = QFormLayout(opts_group)

        self.format_combo = QComboBox()
        self.format_combo.addItems(["glb", "gltf"])
        opts_form.addRow("Format:", self.format_combo)

        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(1, 64)
        self.threads_spin.setValue(4)
        opts_form.addRow("Threads:", self.threads_spin)

        self.recursive_check = QCheckBox("Recurse into subfolders")
        self.recursive_check.setChecked(True)
        opts_form.addRow(self.recursive_check)

        self.keep_images_check = QCheckBox("Keep stray images next to GLTF")
        self.keep_images_check.setChecked(False)
        opts_form.addRow(self.keep_images_check)

        layout.addWidget(opts_group)

        # --- Run button --------------------------------------------------
        self.run_btn = QPushButton("Export")
        self.run_btn.clicked.connect(self._on_export_clicked)
        layout.addWidget(self.run_btn)

        layout.addStretch()

    # ------------------------------------------------------------------
    # File browsing helpers
    # ------------------------------------------------------------------

    def _browse_vrf(self):
        start = self.vrf_path_edit.text() or ""
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Source2Viewer-CLI.exe",
            start,
            "Executables (*.exe);;All files (*)",
        )
        if path:
            self.vrf_path_edit.setText(path)
            self._save_vrf_path()

    def _save_vrf_path(self):
        self.settings.set_vrf_cli_path(self.vrf_path_edit.text().strip())
        self.settings.save()

    def _browse_input_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select compiled model",
            self.input_edit.text() or "",
            "Source 2 models (*.vmdl_c);;All files (*)",
        )
        if path:
            self.input_edit.setText(path)

    def _browse_input_folder(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select folder of compiled assets", self.input_edit.text() or ""
        )
        if path:
            self.input_edit.setText(path)

    def _browse_input_vpk(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select VPK archive",
            self.input_edit.text() or "",
            "VPK archives (*.vpk);;All files (*)",
        )
        if path:
            self.input_edit.setText(path)

    def _update_vpk_field_state(self, text: str):
        """Enable the VPK path field only when the input looks like a .vpk."""
        is_vpk_input = bool(text) and is_vpk(Path(text))
        self.vpk_path_edit.setEnabled(is_vpk_input)

    # ------------------------------------------------------------------
    # Recent runs
    # ------------------------------------------------------------------

    def _refresh_recent_combo(self):
        """Rebuild the dropdown from saved settings."""
        self.recent_combo.blockSignals(True)
        self.recent_combo.clear()
        self.recent_combo.addItem("— Select a recent run —", None)
        for run in self.settings.get_vrf_recent_runs():
            self.recent_combo.addItem(self._format_recent_label(run), run)
        self.recent_combo.blockSignals(False)

    @staticmethod
    def _format_recent_label(run: dict) -> str:
        input_path = run.get('input', '')
        vpk_path = run.get('vpk_path', '')
        output_path = run.get('output', '')
        in_label = Path(input_path).name if input_path else "?"
        out_label = Path(output_path).name if output_path else "?"
        if vpk_path:
            return f"{in_label} @ {vpk_path} → {out_label}"
        return f"{in_label} → {out_label}"

    def _on_recent_selected(self, index: int):
        run = self.recent_combo.itemData(index)
        if not run:
            return
        self.input_edit.setText(run.get('input', ''))
        self.output_edit.setText(run.get('output', ''))
        self.vpk_path_edit.setText(run.get('vpk_path', ''))
        fmt = run.get('format', 'glb')
        idx = self.format_combo.findText(fmt)
        if idx >= 0:
            self.format_combo.setCurrentIndex(idx)
        if 'threads' in run:
            self.threads_spin.setValue(int(run['threads']))
        if 'recursive' in run:
            self.recursive_check.setChecked(bool(run['recursive']))
        if 'keep_stray_images' in run:
            self.keep_images_check.setChecked(bool(run['keep_stray_images']))

    def _on_clear_recent(self):
        self.settings.clear_vrf_recent_runs()
        self.settings.save()
        self._refresh_recent_combo()

    def _save_run_to_recent(self):
        """Persist the current form state as the most-recent run."""
        run = {
            'input': self.input_edit.text().strip(),
            'output': self.output_edit.text().strip(),
            'vpk_path': self.vpk_path_edit.text().strip(),
            'format': self.format_combo.currentText(),
            'threads': self.threads_spin.value(),
            'recursive': self.recursive_check.isChecked(),
            'keep_stray_images': self.keep_images_check.isChecked(),
        }
        self.settings.add_vrf_recent_run(run)
        self.settings.save()
        self._refresh_recent_combo()

    def _browse_output(self):
        path = QFileDialog.getExistingDirectory(
            self, "Select output folder", self.output_edit.text() or ""
        )
        if path:
            self.output_edit.setText(path)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _on_export_clicked(self):
        if self.worker and self.worker.isRunning():
            return

        input_text = self.input_edit.text().strip()
        output_text = self.output_edit.text().strip()
        if not input_text or not output_text:
            QMessageBox.warning(self, "Missing input", "Set both input and output paths.")
            return

        try:
            vrf_exe = resolve_vrf_executable(
                explicit=self.vrf_path_edit.text().strip() or None,
                settings_path=self.settings.get_vrf_cli_path() or None,
            )
        except VrfRunnerError as e:
            QMessageBox.critical(self, "VRF not found", str(e))
            return

        input_path = Path(input_text)
        output_dir = Path(output_text)

        self.clear_log()
        self.log(f"VRF: {vrf_exe}", "INFO")
        self.log(f"Input: {input_path}", "INFO")
        self.log(f"Output: {output_dir}", "INFO")

        self.run_btn.setEnabled(False)
        self.run_btn.setText("Exporting…")

        vpk_filepath = self.vpk_path_edit.text().strip() or None
        if vpk_filepath and not is_vpk(input_path):
            self.log("Path inside VPK ignored (input is not a .vpk).", "WARNING")
            vpk_filepath = None

        self.worker = VrfExportWorker(
            vrf_exe=vrf_exe,
            input_path=input_path,
            output_dir=output_dir,
            gltf_format=self.format_combo.currentText(),
            recursive=self.recursive_check.isChecked(),
            threads=self.threads_spin.value(),
            keep_stray_images=self.keep_images_check.isChecked(),
            vpk_filepath=vpk_filepath,
        )
        self.worker.progress.connect(self.log)
        self.worker.finished_with.connect(self._on_export_finished)
        self.worker.start()

    def _on_export_finished(self, rc: int):
        self.run_btn.setEnabled(True)
        self.run_btn.setText("Export")
        # Save the run regardless of rc — VRF returns non-zero whenever any
        # single file fails to decompile (common in multi-file VPK extracts),
        # but the form state is still useful to recall and retry. The log
        # tells the user whether it succeeded.
        self._save_run_to_recent()
        if rc == 0:
            self.emit_status("VRF export finished")
        else:
            self.emit_status(f"VRF export failed (rc={rc})")
