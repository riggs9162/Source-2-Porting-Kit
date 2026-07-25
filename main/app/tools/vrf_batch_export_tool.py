"""
VRF Batch Export Tool

GUI front-end for ValveResourceFormat's Source2Viewer-CLI. Two port types,
selectable via tabs:

  * Models (default) — decompile a single .vmdl_c or a folder/VPK of compiled
    Source 2 assets to GLTF/GLB plus a `materials/` tree (VMATs + texture PNGs),
    stripping the stray PNGs VRF drops next to the .gltf.
  * VMATs — decompile .vmat_c materials plus their textures only (no models),
    landing them in `materialsrc/`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
    QTabWidget, QVBoxLayout, QWidget,
)

from app.tools.base_tool import BaseTool
from app.ui import widgets
from app.utils.vrf_runner import (
    ExportJob,
    VrfRunnerError,
    build_export_jobs,
    is_vpk,
    resolve_vrf_executable,
    run_vrf_export,
)


class VrfExportWorker(QThread):
    """Background worker that runs VRF (once per job) and post-cleans stray images."""

    progress = Signal(str, str)       # message, level
    progress_value = Signal(int, int) # units done, total units (drives the progress bar)
    finished_with = Signal(int)       # worst process return code (0 == all ok)

    # VRF prints one of these per output file; the primary-suffix ones
    # (.glb/.gltf per model, .vmat per material) anchor the progress bar.
    _DUMP_LINE_RE = re.compile(r'^--- Dump written to "(.+)"\s*$')

    def __init__(
        self,
        vrf_exe: Path,
        jobs: List[ExportJob],
        output_dir: Path,
        gltf_format: str,
        recursive: bool,
        threads: int,
        keep_stray_images: bool,
        flatten_materials_root: bool,
        mode: str,
    ):
        super().__init__()
        self.vrf_exe = vrf_exe
        self.jobs = jobs
        self.output_dir = output_dir
        self.gltf_format = gltf_format
        self.recursive = recursive
        self.threads = threads
        self.keep_stray_images = keep_stray_images
        self.flatten_materials_root = flatten_materials_root
        self.mode = mode
        # Progress-bar unit accounting (see run/_on_job_log). `_unit_total`
        # is 0 when the primary-output counts can't be known up front.
        self._primary_suffixes = (".glb", ".gltf") if mode == "models" else (".vmat",)
        self._unit_total = 0
        self._unit_cap = 0
        self._done_units = 0

    @staticmethod
    def _job_label(job: ExportJob) -> str:
        name = job.input_path.name
        if not job.vpk_filepaths:
            return name
        if len(job.vpk_filepaths) == 1:
            return f"{name} : {job.vpk_filepaths[0]}"
        return f"{name} : {len(job.vpk_filepaths)} paths"

    def _expected_units(self, job: ExportJob) -> Optional[int]:
        """Expected number of primary outputs (models or vmats) for a job.

        Known when every VPK filter names an explicit file, when the input is
        a loose file, or when it's a folder we can scan. Returns None for
        opaque inputs (whole archives, prefix filters) — those can't drive a
        percentage bar honestly.
        """
        suffix = ".vmdl_c" if self.mode == "models" else ".vmat_c"
        if job.vpk_filepaths:
            if all(f.lower().endswith(suffix) for f in job.vpk_filepaths):
                return len(job.vpk_filepaths)
            return None
        p = job.input_path
        if is_vpk(p):
            return None
        if p.is_dir():
            globber = p.rglob if self.recursive else p.glob
            return sum(1 for _ in globber(f"*{suffix}")) or None
        return 1

    def _on_job_log(self, msg: str):
        """Forward a runner log line; count primary-output dump lines toward
        the unit-based progress bar."""
        self.progress.emit(msg, "INFO")
        if not self._unit_total or self._done_units >= self._unit_cap:
            return
        m = self._DUMP_LINE_RE.match(msg)
        if m and m.group(1).lower().endswith(self._primary_suffixes):
            self._done_units += 1
            self.progress_value.emit(self._done_units, self._unit_total)

    def _snap_progress(self, jobs_done: int, total_jobs: int):
        """Snap the bar to the finished job's boundary — failed or partial
        jobs would otherwise leave it stuck short of their slice."""
        if self._unit_total:
            self._done_units = self._unit_cap
            self.progress_value.emit(self._done_units, self._unit_total)
        elif total_jobs > 1:
            self.progress_value.emit(jobs_done, total_jobs)

    def run(self):
        total = len(self.jobs)
        worst_rc = 0
        try:
            # Progress accounting: with per-job primary counts known up front
            # the bar advances per model/material as VRF's "Dump written"
            # lines stream by (a pasted batch list merges into ONE VPK job,
            # so job-level steps alone would never move). Unknown counts fall
            # back to whole-job steps, or busy mode for a single opaque job.
            expected = [self._expected_units(job) for job in self.jobs]
            if all(e is not None for e in expected):
                self._unit_total = sum(expected)  # type: ignore[arg-type]
            self._done_units = 0
            self._unit_cap = 0
            if self._unit_total:
                self.progress_value.emit(0, self._unit_total)
            elif total > 1:
                self.progress_value.emit(0, total)

            for i, job in enumerate(self.jobs, start=1):
                label = self._job_label(job)
                self.progress.emit(f"[{i}/{total}] {label}", "INFO")
                if self._unit_total:
                    self._unit_cap += expected[i - 1]  # type: ignore[operator]
                try:
                    rc = run_vrf_export(
                        vrf_exe=self.vrf_exe,
                        input_path=job.input_path,
                        output_dir=self.output_dir,
                        gltf_format=self.gltf_format,
                        recursive=self.recursive,
                        threads=self.threads,
                        keep_stray_images=self.keep_stray_images,
                        flatten_materials_root=self.flatten_materials_root,
                        mode=self.mode,
                        vpk_filepath=job.vpk_filepaths or None,
                        on_log=self._on_job_log,
                    )
                except VrfRunnerError as e:
                    self.progress.emit(f"{label}: {e}", "ERROR")
                    worst_rc = worst_rc or 2
                    self._snap_progress(i, total)
                    continue
                if rc != 0:
                    self.progress.emit(f"{label}: VRF returned non-zero exit code {rc}", "ERROR")
                    worst_rc = rc
                else:
                    self.progress.emit(f"{label}: done", "SUCCESS")
                self._snap_progress(i, total)

            if worst_rc == 0:
                self.progress.emit(
                    f"Export complete ({total} job{'s' if total != 1 else ''}).", "SUCCESS"
                )
            self.finished_with.emit(worst_rc)
        except Exception as e:  # noqa: BLE001
            self.progress.emit(f"Unexpected error: {e}", "ERROR")
            self.finished_with.emit(1)


# Per-tab placeholder text for the multi-entry batch box.
_MODELS_BATCH_PLACEHOLDER = (
    "Optional — one model per line. Examples:\n"
    "vpk:S:/.../hlvr/pak01_dir.vpk:models/props/foo.vmdl_c   (VRF right-click → Copy URL)\n"
    "models/props_combine/combine_consoles/combine_monitor_large.vmdl_c   (path inside the VPK above)\n"
    r"C:\game\models\prop.vmdl_c   (a loose file or folder on disk)"
)
_VMATS_BATCH_PLACEHOLDER = (
    "Optional — one material per line. Examples:\n"
    "vpk:S:/.../hlvr/pak01_dir.vpk:materials/models/props/foo.vmat_c   (VRF right-click → Copy URL)\n"
    "materials/models/props_combine/combine_monitor_large.vmat_c   (path inside the VPK above)\n"
    r"C:\game\materials\prop.vmat_c   (a loose file or folder on disk)"
)


class VrfBatchExportTool(BaseTool):
    """GUI tool: VRF batch export for models (GLTF + materials) or VMATs."""

    # Tab index → mode string.
    _MODES = ("models", "vmats")

    def __init__(self):
        super().__init__("VRF Batch Export")
        self.worker: Optional[VrfExportWorker] = None
        # Per-kind input widgets, filled by _build_input_group.
        self.inputs: dict = {}
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

        # --- Port type tabs (Models / VMATs) -----------------------------
        # The tab index is the "port type": index 0 exports models (default),
        # index 1 exports vmats. Each tab owns its own Input group; everything
        # below (output, options, run) is shared and dispatches on the active
        # tab via _current_mode().
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_input_group("models"), "Models")
        self.tabs.addTab(self._build_input_group("vmats"), "VMATs")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tabs)

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
        self.format_label = QLabel("Format:")
        opts_form.addRow(self.format_label, self.format_combo)

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

        self.flatten_materials_check = QCheckBox(
            "Move all images && VMATs to materialsrc/ root (overrides stray-image option)"
        )
        self.flatten_materials_check.setChecked(False)
        self.flatten_materials_check.toggled.connect(self._on_flatten_toggled)
        opts_form.addRow(self.flatten_materials_check)

        layout.addWidget(opts_group)

        # --- Run button --------------------------------------------------
        self.run_btn = QPushButton("Export")
        self.run_btn.clicked.connect(self._on_export_clicked)
        layout.addWidget(self.run_btn)

        layout.addStretch()

        # Sync option enablement to the initial (Models) tab.
        self._on_tab_changed(self.tabs.currentIndex())

    def _build_input_group(self, kind: str) -> QWidget:
        """Build the per-tab Input group for `kind` ("models" | "vmats") and
        register its widgets in `self.inputs[kind]`. Returns the tab widget."""
        is_models = kind == "models"
        noun = "model" if is_models else "material"

        group = QGroupBox("Input")
        form = QFormLayout(group)

        input_edit = QLineEdit()
        input_edit.textChanged.connect(lambda text, k=kind: self._update_vpk_field_state(k, text))
        file_btn = QPushButton("Pick file…")
        file_btn.clicked.connect(lambda _=False, k=kind: self._browse_input_file(k))
        dir_btn = QPushButton("Pick folder…")
        dir_btn.clicked.connect(lambda _=False, k=kind: self._browse_input_folder(k))
        vpk_btn = QPushButton("Pick VPK…")
        vpk_btn.clicked.connect(lambda _=False, k=kind: self._browse_input_vpk(k))
        input_row = QHBoxLayout()
        input_row.addWidget(input_edit)
        input_row.addWidget(file_btn)
        input_row.addWidget(dir_btn)
        input_row.addWidget(vpk_btn)
        form.addRow("Path:", input_row)

        # VPK internal path filter (e.g. "models/props_c17/").
        vpk_path_edit = QLineEdit()
        vpk_path_edit.setPlaceholderText(
            "e.g. models/props_c17/  (only used when input is a .vpk)" if is_models
            else "e.g. materials/models/props_c17/  (only used when input is a .vpk)"
        )
        form.addRow("Path inside VPK:", vpk_path_edit)

        # Multi-entry batch list: one entry per line. Accepts VRF "Copy URL"
        # strings, loose paths, or bare internal paths paired with the .vpk above.
        batch_edit = QPlainTextEdit()
        batch_edit.setPlaceholderText(
            _MODELS_BATCH_PLACEHOLDER if is_models else _VMATS_BATCH_PLACEHOLDER
        )
        batch_edit.setMaximumHeight(120)
        form.addRow(f"{noun.capitalize()}s (list):", batch_edit)

        self.inputs[kind] = {
            "input": input_edit,
            "vpk": vpk_path_edit,
            "batch": batch_edit,
        }
        self._update_vpk_field_state(kind, "")
        return group

    # ------------------------------------------------------------------
    # Tab / mode helpers
    # ------------------------------------------------------------------

    def _current_mode(self) -> str:
        idx = self.tabs.currentIndex()
        return self._MODES[idx] if 0 <= idx < len(self._MODES) else "models"

    def _on_tab_changed(self, index: int):
        """Model-only options (GLTF format, keep-stray-images) don't apply to a
        VMAT-only port — grey them out on the VMATs tab."""
        is_models = index == 0
        self.format_label.setEnabled(is_models)
        self.format_combo.setEnabled(is_models)
        # keep_images_check is also gated by the flatten override; combine both.
        self.keep_images_check.setEnabled(
            is_models and not self.flatten_materials_check.isChecked()
        )

    # ------------------------------------------------------------------
    # File browsing helpers
    # ------------------------------------------------------------------

    def _browse_vrf(self):
        path = widgets.browse_open_file(
            self, "Select Source2Viewer-CLI",
            file_filter="Executables (*.exe);;All files (*)",
            settings=self.settings, remember_key=f"{self.tool_name}/cli",
            fallback=self.vrf_path_edit.text().strip(),
        )
        if path:
            self.vrf_path_edit.setText(path)
            self._save_vrf_path()

    def _save_vrf_path(self):
        self.settings.set_vrf_cli_path(self.vrf_path_edit.text().strip())
        self.settings.save()

    def _browse_input_file(self, kind: str):
        edit = self.inputs[kind]["input"]
        if kind == "models":
            title, filt = "Select compiled model", "Source 2 models (*.vmdl_c);;All files (*)"
        else:
            title, filt = "Select compiled material", "Source 2 materials (*.vmat_c);;All files (*)"
        path = widgets.browse_open_file(
            self, title, file_filter=filt,
            settings=self.settings, remember_key=f"{self.tool_name}/input",
            fallback=edit.text().strip(),
        )
        if path:
            edit.setText(path)

    def _browse_input_folder(self, kind: str):
        edit = self.inputs[kind]["input"]
        path = widgets.browse_dir(
            self, "Select folder of compiled assets",
            settings=self.settings, remember_key=f"{self.tool_name}/input",
            fallback=edit.text().strip(),
        )
        if path:
            edit.setText(path)

    def _browse_input_vpk(self, kind: str):
        edit = self.inputs[kind]["input"]
        path = widgets.browse_open_file(
            self, "Select VPK archive",
            file_filter="VPK archives (*.vpk);;All files (*)",
            settings=self.settings, remember_key=f"{self.tool_name}/input",
            fallback=edit.text().strip(),
        )
        if path:
            edit.setText(path)

    def _update_vpk_field_state(self, kind: str, text: str):
        """Enable the VPK path field only when the input looks like a .vpk."""
        is_vpk_input = bool(text) and is_vpk(Path(text))
        self.inputs[kind]["vpk"].setEnabled(is_vpk_input)

    def _on_flatten_toggled(self, checked: bool):
        """Flatten-to-root supersedes the stray-image handling — grey out the
        keep-stray toggle so the override is visible (models tab only)."""
        self.keep_images_check.setEnabled(
            self._current_mode() == "models" and not checked
        )

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
        batch_list = run.get('batch_list', '')
        mode = run.get('mode', 'models')
        out_label = Path(output_path).name if output_path else "?"
        n_lines = len([ln for ln in batch_list.splitlines() if ln.strip()])
        if input_path:
            in_label = Path(input_path).name
            head = f"{in_label} @ {vpk_path}" if vpk_path else in_label
            if n_lines:
                head += f" (+{n_lines} listed)"
        elif n_lines:
            head = f"{n_lines} item{'s' if n_lines != 1 else ''}"
        else:
            head = "?"
        prefix = "[VMAT] " if mode == "vmats" else ""
        return f"{prefix}{head} → {out_label}"

    def _on_recent_selected(self, index: int):
        run = self.recent_combo.itemData(index)
        if not run:
            return
        mode = run.get('mode', 'models')
        kind = mode if mode in self._MODES else 'models'
        self.tabs.setCurrentIndex(self._MODES.index(kind))
        w = self.inputs[kind]
        w["input"].setText(run.get('input', ''))
        w["vpk"].setText(run.get('vpk_path', ''))
        w["batch"].setPlainText(run.get('batch_list', ''))
        self.output_edit.setText(run.get('output', ''))
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
        if 'flatten_materials_root' in run:
            self.flatten_materials_check.setChecked(bool(run['flatten_materials_root']))

    def _on_clear_recent(self):
        self.settings.clear_vrf_recent_runs()
        self.settings.save()
        self._refresh_recent_combo()

    def _save_run_to_recent(self, mode: str):
        """Persist the current form state as the most-recent run."""
        w = self.inputs[mode]
        run = {
            'mode': mode,
            'input': w["input"].text().strip(),
            'output': self.output_edit.text().strip(),
            'vpk_path': w["vpk"].text().strip(),
            'batch_list': w["batch"].toPlainText().strip(),
            'format': self.format_combo.currentText(),
            'threads': self.threads_spin.value(),
            'recursive': self.recursive_check.isChecked(),
            'keep_stray_images': self.keep_images_check.isChecked(),
            'flatten_materials_root': self.flatten_materials_check.isChecked(),
        }
        self.settings.add_vrf_recent_run(run)
        self.settings.save()
        self._refresh_recent_combo()

    def _browse_output(self):
        path = widgets.browse_dir(
            self, "Select output folder",
            settings=self.settings, remember_key=f"{self.tool_name}/output",
            fallback=self.output_edit.text().strip(),
        )
        if path:
            self.output_edit.setText(path)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _on_export_clicked(self):
        if self.worker and self.worker.isRunning():
            return

        mode = self._current_mode()
        w = self.inputs[mode]

        output_text = self.output_edit.text().strip()
        if not output_text:
            QMessageBox.warning(self, "Missing output", "Set an output (project root) folder.")
            return

        input_text = w["input"].text().strip()
        batch_text = w["batch"].toPlainText()
        if not input_text and not batch_text.strip():
            QMessageBox.warning(
                self, "Missing input",
                "Set an input path or paste one or more lines.",
            )
            return

        # Assemble the entry list: the multi-line batch box, plus the single
        # Input field folded in as one more entry so nothing the user typed is
        # ignored. A .vpk Input also becomes the context for bare internal
        # lines in the box.
        entries: list = batch_text.splitlines()
        vpk_filter = w["vpk"].text().strip()
        default_input: Optional[Path] = None
        pre_warnings: list = []
        if input_text:
            input_path = Path(input_text)
            if is_vpk(input_path):
                default_input = input_path
                entries.append(vpk_filter if vpk_filter else input_text)
            else:
                if vpk_filter:
                    pre_warnings.append("Path inside VPK ignored (input is not a .vpk).")
                entries.append(input_text)

        jobs, warnings = build_export_jobs(entries, default_input=default_input)

        self.clear_log()
        for msg in pre_warnings + warnings:
            self.log(msg, "WARNING")
        if not jobs:
            QMessageBox.warning(
                self, "Nothing to export",
                "No valid entries resolved from the input/list. See the log for details.",
            )
            return

        try:
            vrf_exe = resolve_vrf_executable(
                explicit=self.vrf_path_edit.text().strip() or None,
                settings_path=self.settings.get_vrf_cli_path() or None,
            )
        except VrfRunnerError as e:
            QMessageBox.critical(self, "VRF not found", str(e))
            return

        output_dir = Path(output_text)

        self.log(f"Port type: {'VMATs' if mode == 'vmats' else 'Models'}", "INFO")
        self.log(f"VRF: {vrf_exe}", "INFO")
        self.log(f"Output: {output_dir}", "INFO")
        self.log(f"Resolved {len(jobs)} export job(s):", "INFO")
        for job in jobs:
            self.log(f"  - {VrfExportWorker._job_label(job)}", "INFO")

        # busy_widgets only toggles enabled state; swap the label text by hand.
        self.run_btn.setText("Exporting…")

        worker = VrfExportWorker(
            vrf_exe=vrf_exe,
            jobs=jobs,
            output_dir=output_dir,
            gltf_format=self.format_combo.currentText(),
            recursive=self.recursive_check.isChecked(),
            threads=self.threads_spin.value(),
            keep_stray_images=self.keep_images_check.isChecked(),
            flatten_materials_root=self.flatten_materials_check.isChecked(),
            mode=mode,
        )
        self._active_mode = mode
        # clear_log=False: the pre-run summary logged just above must survive.
        self.start_worker(
            worker,
            self._on_export_finished,
            finished_signal=worker.finished_with,
            busy_widgets=(self.run_btn,),
            status="Running VRF export…",
            clear_log=False,
        )

    def _on_export_finished(self, rc: int):
        self.run_btn.setText("Export")
        # Save the run regardless of rc — VRF returns non-zero whenever any
        # single file fails to decompile (common in multi-file VPK extracts),
        # but the form state is still useful to recall and retry. The log
        # tells the user whether it succeeded.
        self._save_run_to_recent(getattr(self, "_active_mode", self._current_mode()))
        if rc == 0:
            self.emit_status("VRF export finished")
        else:
            self.emit_status(f"VRF export failed (rc={rc})")
