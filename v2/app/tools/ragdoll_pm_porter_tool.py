"""Ragdoll & Playermodel Porter — one-click Source 2 GLB -> GMod sources.

Two modes over the verified porting recipes:
  - Playermodel: donor-exact ValveBiped conform (utils/playermodel_port),
    now with weight-derived $collisionjoints for the death ragdoll.
  - Ragdoll: native-skeleton PHYS-exact build (utils/ragdoll_port) — real
    Rubikon hulls/limits, distal mass bias, hitboxes.

Both with an optional StudioMDL CE compile step (-game folder validated up
front — the CE root's csgo subfolder is resolved automatically).
"""

from pathlib import Path

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox,
                               QFormLayout, QGroupBox, QLineEdit)
from PySide6.QtCore import QThread, Signal

from app.tools.base_tool import BaseTool
from app.core import ragdoll_presets
from app.utils.playermodel_port import port_playermodel
from app.utils.ragdoll_port import discover_sidecars, port_ragdoll
from app.utils.studiomdl_run import resolve_game_dir

_SKELETON_NAMING = ("native", "auto", "valvebiped")
_CONSTRAINT_SOURCE = ("s2", "valvebiped_donor")


class PortWorker(QThread):
    progress = Signal(str, str)
    finished = Signal(bool, str)          # ok, qc_path

    def __init__(self, mode: str, kwargs: dict, compile_requested: bool):
        super().__init__()
        self.mode = mode
        self.kwargs = kwargs
        self.compile_requested = compile_requested

    def run(self):
        try:
            fn = port_playermodel if self.mode == "playermodel" else port_ragdoll
            res = fn(on_log=lambda m, l="INFO": self.progress.emit(m, l),
                     **self.kwargs)
            ok = (not self.compile_requested) or bool(res.get("compiled"))
            if not ok:
                self.progress.emit("StudioMDL compile failed - see the "
                                   "output above", "ERROR")
            self.finished.emit(ok, res.get("qc", ""))
        except Exception as exc:
            self.progress.emit(f"port failed: {exc}", "ERROR")
            self.finished.emit(False, "")


class RagdollPmPorterTool(BaseTool):
    """Port a Source 2 model export (GLB) into GMod ragdoll or playermodel
    sources."""

    def __init__(self):
        super().__init__("Ragdoll & Playermodel Porter")
        self._build_ui()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        runs_form = QFormLayout()
        self.runs_combo = QComboBox()
        self.runs_combo.activated.connect(self._apply_recent_run)
        runs_form.addRow("Previous runs:", self.runs_combo)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Playermodel (donor conform)",
                                  "Ragdoll (native skeleton + PHYS)"])
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        runs_form.addRow("Mode:", self.mode_combo)
        self.add_group("Port", runs_form)

        form = QFormLayout()
        self.glb_row = self.create_picker(
            "open_file", "glb", file_filter="glTF binary (*.glb)",
            dialog_title="Select model GLB", on_picked=self._glb_picked)
        form.addRow("Model GLB:", self.glb_row)
        self.donor_row = self.create_picker(
            "open_file", "donor", file_filter="Source model (*.mdl)",
            dialog_title="Select anim library model",
            placeholder="z_anm.mdl / m_anm.mdl (extracted)")
        self.donor_row.setToolTip(
            "Playermodel mode: REQUIRED - the GMod anim library model the\n"
            "skeleton conforms to. Ragdoll mode: optional, only used when a\n"
            "ValveBiped skeleton naming is selected.")
        form.addRow("Anim donor (.mdl):", self.donor_row)
        self.out_row = self.create_picker(
            "dir", "output", dialog_title="Select output folder")
        form.addRow("Output folder:", self.out_row)
        self.add_group("Inputs", form)

        oform = QFormLayout()
        self.modelname_edit = QLineEdit()
        self.modelname_edit.setPlaceholderText(
            "e.g. myaddon/creatures/thing/thing.mdl")
        oform.addRow("$modelname:", self.modelname_edit)
        self.cdmat_edit = QLineEdit()
        self.cdmat_edit.setPlaceholderText("e.g. models/myaddon/thing/")
        oform.addRow("$cdmaterials:", self.cdmat_edit)
        self.surface_combo = QComboBox()
        self.surface_combo.addItems(["flesh", "zombieflesh", "metal", "wood"])
        self.surface_combo.setEditable(True)
        oform.addRow("$surfaceprop:", self.surface_combo)
        self.mass_spin = QDoubleSpinBox()
        self.mass_spin.setRange(1.0, 2000.0)
        self.mass_spin.setValue(ragdoll_presets.DEFAULT_TOTAL_MASS)
        self.mass_spin.setToolTip("Total ragdoll mass ($mass)")
        oform.addRow("Total mass:", self.mass_spin)
        self.add_group("Options", oform)

        pform = QFormLayout()
        self.prune_check = QCheckBox(
            "ValveBiped-only skeleton (prune + fold unmapped bones)")
        self.prune_check.setChecked(True)
        self.prune_check.setToolTip(
            "Keep only bones that map into the donor; weights fold onto the\n"
            "nearest kept ancestor (stock-style rigid face/extras). Required\n"
            "to stay under 128 compiled bones on dense Source 2 rigs.")
        pform.addRow("", self.prune_check)
        self.pm_collision_check = QCheckBox(
            "Generate ragdoll collision ($collisionjoints)")
        self.pm_collision_check.setChecked(True)
        self.pm_collision_check.setToolTip(
            "Weight-derived per-bone hulls on the conformed skeleton - the\n"
            "playermodel gets a working death ragdoll.")
        pform.addRow("", self.pm_collision_check)
        self.pm_group = QGroupBox("Playermodel options")
        self.pm_group.setLayout(pform)
        self.content_layout.addWidget(self.pm_group)

        rform = QFormLayout()
        self.skeleton_combo = QComboBox()
        self.skeleton_combo.addItems(["Native (keep Source 2 names)",
                                      "Auto (ValveBiped if humanoid)",
                                      "Force ValveBiped"])
        self.skeleton_combo.setToolTip(
            "Native is the verified default: the PHYS sidecar drives exact\n"
            "hulls and joint limits on the model's own skeleton. Pick a\n"
            "ValveBiped option only when the ragdoll must be compatible\n"
            "with HL2 animations/poses.")
        rform.addRow("Skeleton naming:", self.skeleton_combo)
        self.constraint_combo = QComboBox()
        self.constraint_combo.addItems(["Source 2 data (converted)",
                                        "ValveBiped donor (Valve's values)"])
        self.constraint_combo.setToolTip(
            "Joint limits: converted real Rubikon data (any skeleton), or\n"
            "Valve's hand-tuned humanoid values (needs true ValveBiped\n"
            "orientations, i.e. a donor conform).")
        rform.addRow("Constraint source:", self.constraint_combo)
        self.export_anims_check = QCheckBox("Export the GLB's animations")
        self.export_anims_check.setChecked(True)
        self.export_anims_check.setToolTip(
            "Write every animation clip in the GLB as its own SMD and "
            "$sequence. A choreo character can carry hundreds, which is slow "
            "to port and compile - turn this off for a ragdoll that only needs "
            "its idle and death poses.")
        rform.addRow("", self.export_anims_check)
        self.vpk_row = self.create_picker(
            "open_file", "source_vpk",
            file_filter="Source 2 packages (*.vpk *.vmdl_c)",
            dialog_title="Select the model's source VPK",
            placeholder="pak01_dir.vpk (used to auto-dump the PHYS sidecar)")
        self.vpk_row.setToolTip(
            "When no <stem>_phys.txt sidecar sits next to the GLB, the\n"
            "PHYS block is dumped from this VPK (or loose .vmdl_c) via the\n"
            "Source2Viewer CLI and written beside the GLB automatically.")
        rform.addRow("Source VPK:", self.vpk_row)
        self.rd_group = QGroupBox("Ragdoll options")
        self.rd_group.setLayout(rform)
        self.content_layout.addWidget(self.rd_group)

        cform = QFormLayout()
        self.compile_check = QCheckBox("Compile with StudioMDL CE afterwards")
        self.compile_check.setChecked(False)
        cform.addRow("", self.compile_check)
        self.studiomdl_row = self.create_picker(
            "open_file", "studiomdl", file_filter="studiomdl (*.exe)",
            dialog_title="Select studiomdl.exe")
        cform.addRow("studiomdl.exe:", self.studiomdl_row)
        self.game_row = self.create_picker(
            "dir", "game", dialog_title="Select -game folder",
            placeholder="folder containing gameinfo.txt (CE root works too)")
        cform.addRow("-game folder:", self.game_row)
        self.add_group("Compile", cform)

        self.run_btn = self.make_run_button("Port Model", self.start_port)
        self.add_action_row(self.run_btn)
        self.content_layout.addStretch()
        self._on_mode_changed(self.mode_combo.currentIndex())
        self._populate_recent_runs()
        self._prefill_source_vpk()

    def _prefill_source_vpk(self):
        """Seed the Source VPK from the latest VRF Batch Export run."""
        if self.vpk_row.path():
            return
        for r in self.settings.get_vrf_recent_runs():
            inp = str(r.get('input', '') or '')
            if inp.lower().endswith('.vpk') and Path(inp).is_file():
                self.vpk_row.set_path(inp)
                return

    def _mode_value(self) -> str:
        return "playermodel" if self.mode_combo.currentIndex() == 0 else "ragdoll"

    def _on_mode_changed(self, _index: int):
        pm = self._mode_value() == "playermodel"
        self.pm_group.setVisible(pm)
        self.rd_group.setVisible(not pm)

    def _glb_picked(self, path: str):
        self.log(f"Selected glb: {path}", "INFO")
        physics_glb, phys_dump = discover_sidecars(Path(path))
        if phys_dump:
            self.log(f"found PHYS sidecar: {phys_dump.name}", "INFO")
        if physics_glb:
            self.log(f"found physics mesh: {physics_glb.name}", "INFO")
        if not phys_dump and not physics_glb:
            self.log("no physics sidecars next to the GLB (ragdoll mode "
                     "falls back to weight-derived hulls)", "WARNING")

    # ------------------------------------------------------------ recent runs
    def _stored_runs(self) -> list:
        runs = self.settings.get('porter_recent_runs', None)
        if not runs:
            # one-way migration from the retired Playermodel Porter tool
            runs = self.settings.get('pm_porter_recent_runs', []) or []
        return list(runs)

    def _run_snapshot(self) -> dict:
        return {
            'mode': self._mode_value(),
            'glb': self.glb_row.path(),
            'donor': self.donor_row.path(),
            'out': self.out_row.path(),
            'modelname': self.modelname_edit.text().strip(),
            'cdmaterials': self.cdmat_edit.text().strip(),
            'surfaceprop': self.surface_combo.currentText().strip(),
            'total_mass': self.mass_spin.value(),
            'prune': self.prune_check.isChecked(),
            'pm_ragdoll_collision': self.pm_collision_check.isChecked(),
            'skeleton_naming':
                _SKELETON_NAMING[self.skeleton_combo.currentIndex()],
            'constraint_source':
                _CONSTRAINT_SOURCE[self.constraint_combo.currentIndex()],
            'export_animations': self.export_anims_check.isChecked(),
            'source_vpk': self.vpk_row.path(),
            'compile': self.compile_check.isChecked(),
            'studiomdl': self.studiomdl_row.path(),
            'game': self.game_row.path(),
        }

    def _save_recent_run(self):
        snap = self._run_snapshot()
        runs = [r for r in self._stored_runs()
                if not (r.get('glb') == snap['glb']
                        and r.get('out') == snap['out']
                        and r.get('mode', 'playermodel') == snap['mode'])]
        runs.append(snap)
        self.settings.set('porter_recent_runs', runs[-10:])
        self.settings.save()
        self._populate_recent_runs()

    def _populate_recent_runs(self):
        self.runs_combo.blockSignals(True)
        self.runs_combo.clear()
        self.runs_combo.addItem("- select a previous run -")
        for r in reversed(self._stored_runs()):
            label = Path(r.get('glb', '?')).stem or '?'
            tag = "RD" if r.get('mode') == "ragdoll" else "PM"
            self.runs_combo.addItem(
                f"[{tag}] {label}  ({r.get('modelname', '')})", r)
        self.runs_combo.blockSignals(False)

    def _apply_recent_run(self, index: int):
        r = self.runs_combo.itemData(index)
        if not r:
            return
        self.mode_combo.setCurrentIndex(
            1 if r.get('mode', 'playermodel') == 'ragdoll' else 0)
        self.glb_row.set_path(r.get('glb', ''))
        self.donor_row.set_path(r.get('donor', ''))
        self.out_row.set_path(r.get('out', ''))
        self.modelname_edit.setText(r.get('modelname', ''))
        self.cdmat_edit.setText(r.get('cdmaterials', ''))
        self.surface_combo.setCurrentText(r.get('surfaceprop', 'flesh'))
        self.mass_spin.setValue(float(
            r.get('total_mass', ragdoll_presets.DEFAULT_TOTAL_MASS)))
        self.prune_check.setChecked(bool(r.get('prune', True)))
        self.pm_collision_check.setChecked(
            bool(r.get('pm_ragdoll_collision', True)))
        naming = r.get('skeleton_naming', 'native')
        self.skeleton_combo.setCurrentIndex(
            _SKELETON_NAMING.index(naming)
            if naming in _SKELETON_NAMING else 0)
        source = r.get('constraint_source', 's2')
        self.constraint_combo.setCurrentIndex(
            _CONSTRAINT_SOURCE.index(source)
            if source in _CONSTRAINT_SOURCE else 0)
        self.export_anims_check.setChecked(
            bool(r.get('export_animations', True)))
        if r.get('source_vpk'):
            self.vpk_row.set_path(r.get('source_vpk', ''))
        self.compile_check.setChecked(bool(r.get('compile', False)))
        self.studiomdl_row.set_path(r.get('studiomdl', ''))
        self.game_row.set_path(r.get('game', ''))
        self.log("Restored previous run settings", "INFO")

    # -------------------------------------------------------------- actions
    def start_port(self):
        mode = self._mode_value()
        glb = self.glb_row.path()
        donor = self.donor_row.path()
        out = self.out_row.path()
        modelname = self.modelname_edit.text().strip()
        cdmat = self.cdmat_edit.text().strip()
        if not glb or not Path(glb).is_file():
            self.log("Select a valid model .glb", "ERROR")
            return
        if mode == "playermodel" and (not donor or not Path(donor).is_file()):
            self.log("Playermodel mode needs the anim library .mdl "
                     "(e.g. extracted z_anm.mdl / m_anm.mdl)", "ERROR")
            return
        if not out:
            self.log("Select an output folder", "ERROR")
            return
        stem = Path(glb).stem
        if not modelname:
            suffix = "_pm" if mode == "playermodel" else ""
            modelname = f"{stem}/{stem}{suffix}.mdl"
            self.log(f"$modelname defaulted to {modelname}", "WARNING")
        if not cdmat:
            cdmat = f"models/{stem}/"
            self.log(f"$cdmaterials defaulted to {cdmat}", "WARNING")

        kwargs = dict(
            glb_path=Path(glb), out_dir=Path(out), modelname=modelname,
            cdmaterials=cdmat,
            surfaceprop=self.surface_combo.currentText().strip() or "flesh",
            total_mass=self.mass_spin.value(),
        )
        if mode == "playermodel":
            kwargs.update(
                donor_mdl=Path(donor),
                valvebiped_only=self.prune_check.isChecked(),
                ragdoll_collision=self.pm_collision_check.isChecked(),
            )
        else:
            kwargs.update(
                skeleton_naming=
                    _SKELETON_NAMING[self.skeleton_combo.currentIndex()],
                constraint_source=
                    _CONSTRAINT_SOURCE[self.constraint_combo.currentIndex()],
                export_animations=self.export_anims_check.isChecked(),
            )
            if donor and Path(donor).is_file():
                kwargs["donor_mdl"] = Path(donor)
            vpk = self.vpk_row.path()
            vrf = self.settings.get_vrf_cli_path()
            if vpk and Path(vpk).is_file():
                if vrf and Path(vrf).is_file():
                    kwargs["source_vpk"] = Path(vpk)
                    kwargs["vrf_exe"] = Path(vrf)
                else:
                    self.log("Source VPK set but no Source2Viewer-CLI "
                             "configured (Preferences) - PHYS auto-dump "
                             "unavailable", "WARNING")

        compile_requested = self.compile_check.isChecked()
        if compile_requested:
            smdl = self.studiomdl_row.path()
            game = self.game_row.path()
            if not smdl or not Path(smdl).is_file() or not game:
                self.log("Compile enabled: set studiomdl.exe and the -game "
                         "folder", "ERROR")
                return
            resolved = resolve_game_dir(Path(game))
            if resolved is None:
                self.log(f"-game folder invalid: {game} contains no "
                         "gameinfo.txt (and no csgo subfolder with one). "
                         "For StudioMDL CE pick <install>/csgo.", "ERROR")
                return
            if str(resolved) != str(Path(game)):
                self.log(f"-game resolved to {resolved}", "INFO")
            kwargs["studiomdl"] = Path(smdl)
            kwargs["studiomdl_game"] = resolved

        self._save_recent_run()
        worker = PortWorker(mode, kwargs, compile_requested)
        # start_worker auto-connects worker.progress -> self.log; adding a
        # manual connect here would double every log line
        self.start_worker(worker, self._on_finished,
                          busy_widgets=(self.run_btn,),
                          status=f"Porting {stem} ({mode})...")

    def _on_finished(self, ok: bool, qc: str):
        if ok:
            self.emit_status("Port complete")
            self.log(f"Done - QC at {qc}", "SUCCESS")
        else:
            self.emit_status("Port failed")
