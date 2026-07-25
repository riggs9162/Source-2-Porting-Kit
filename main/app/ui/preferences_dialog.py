"""
Preferences dialog for application-wide settings.

Tabbed dialog reachable via Settings → Preferences… in the main menu. Edits
the global `Settings` JSON file. Per-tool defaults (Default Material Path,
Max Parallel, Generate Mipmaps) only affect *newly created* tool instances —
already-open tools keep their current values until the user reopens them.

FakePBR per-output clamps, by contrast, are read by FakePBRProcessor on
every conversion, so the change takes effect on the very next run without
needing the tool to be recreated.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QTabWidget,
    QWidget,
    QFormLayout,
    QGroupBox,
    QLabel,
    QComboBox,
    QCheckBox,
    QPushButton,
    QLineEdit,
    QSpinBox,
    QDialogButtonBox,
)

from app.core.settings import Settings


# Same canonical size set the per-tool combos already use, so users don't
# have to learn a new ladder.
_CLAMP_CHOICES = (
    ("Off (use working resolution)", 0),
    ("32", 32),
    ("64", 64),
    ("128", 128),
    ("256", 256),
    ("512", 512),
    ("1024", 1024),
    ("2048", 2048),
    ("4096", 4096),
    ("8192", 8192),
)


def _make_clamp_combo() -> QComboBox:
    combo = QComboBox()
    for label, value in _CLAMP_CHOICES:
        combo.addItem(label, value)
    return combo


def _select_combo_value(combo: QComboBox, value: int) -> None:
    idx = combo.findData(int(value or 0))
    combo.setCurrentIndex(idx if idx >= 0 else 0)


class PreferencesDialog(QDialog):
    """Tabbed preferences editor."""

    def __init__(self, settings: Settings, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Preferences")
        self.setModal(True)
        self.resize(560, 480)

        self._build_ui()
        self._load_from_settings()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        layout = QVBoxLayout(self)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_fakepbr_tab(), "FakePBR Outputs")
        self.tabs.addTab(self._build_general_tab(), "General Defaults")
        layout.addWidget(self.tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok
            | QDialogButtonBox.Cancel
            | QDialogButtonBox.RestoreDefaults
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.RestoreDefaults).clicked.connect(
            self._restore_defaults
        )
        layout.addWidget(buttons)

    def _build_fakepbr_tab(self) -> QWidget:
        widget = QWidget()
        outer = QVBoxLayout(widget)

        intro = QLabel(
            "Cap the final resolution of individual FakePBR output VTFs. "
            "Each cap downsizes that output if its longest edge exceeds the "
            "value (aspect preserved); textures already at or below the cap "
            "pass through unchanged. The selfillum / emissive mask has its "
            "own independent cap.\n\n"
            "Enabled by default as a safety net: sourcepp's VTF encoder has "
            "an internal downscale path that corrupts data (most visibly on "
            "normal maps — flat areas come out black) when fed buffers far "
            "larger than the target. Pre-resizing in Python forces sourcepp "
            "to take the no-resize fast path. Turn off only if you've "
            "specifically tested your source resolutions and know they're "
            "safe.\n\n"
            "When enabled, these overrides take precedence over the per-tool "
            "Clamp Size combo on a per-output basis. Outputs with a cap of "
            "\"Off\" still use the per-tool working resolution."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #cccccc; padding-bottom: 8px;")
        outer.addWidget(intro)

        self.fakepbr_enabled = QCheckBox(
            "Enable per-output resolution overrides"
        )
        self.fakepbr_enabled.setToolTip(
            "When off, FakePBR uses the per-tool Clamp Size combo as before. "
            "When on, each output VTF is downscaled independently using the "
            "caps below."
        )
        self.fakepbr_enabled.toggled.connect(self._sync_fakepbr_enabled)
        outer.addWidget(self.fakepbr_enabled)

        self.fakepbr_group = QGroupBox("Per-output Resolution Caps")
        form = QFormLayout(self.fakepbr_group)

        self.color_combo = _make_clamp_combo()
        self.color_combo.setToolTip(
            "Cap for `_color.vtf` (basetexture)."
        )
        form.addRow("Color:", self.color_combo)

        self.normal_combo = _make_clamp_combo()
        self.normal_combo.setToolTip(
            "Cap for `_normal.vtf` (bumpmap). Halving the normal map is the "
            "single biggest disk-space win for materials that don't need "
            "fine bump detail."
        )
        form.addRow("Normal:", self.normal_combo)

        self.phong_combo = _make_clamp_combo()
        self.phong_combo.setToolTip(
            "Cap for `_phong.vtf` (phong exponent texture). Phong response "
            "is low-frequency by nature; 1024 or even 512 usually looks the "
            "same as 4096."
        )
        form.addRow("Phong:", self.phong_combo)

        self.envmask_combo = _make_clamp_combo()
        self.envmask_combo.setToolTip(
            "Cap for `_envmask.vtf` (envmap mask). The cheapest texture to "
            "shrink — environment reflections blur heavily at runtime."
        )
        form.addRow("Envmask:", self.envmask_combo)

        self.selfillum_combo = _make_clamp_combo()
        self.selfillum_combo.setToolTip(
            "Cap for `_selfillum.vtf` (self-illum / emissive mask). Glow masks "
            "are usually mostly black with a few bright details, so they "
            "tolerate aggressive shrinking."
        )
        form.addRow("Selfillum:", self.selfillum_combo)

        outer.addWidget(self.fakepbr_group)
        outer.addStretch()
        return widget

    def _build_general_tab(self) -> QWidget:
        widget = QWidget()
        outer = QVBoxLayout(widget)

        intro = QLabel(
            "Defaults applied when a tool is first opened. Changing these "
            "does not affect tools that are already open — close and reopen "
            "the tool (or restart the app) to pick up new defaults."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #cccccc; padding-bottom: 8px;")
        outer.addWidget(intro)

        defaults_group = QGroupBox("Tool Defaults")
        form = QFormLayout(defaults_group)

        self.material_path_input = QLineEdit()
        self.material_path_input.setPlaceholderText("e.g. models/ports")
        self.material_path_input.setToolTip(
            "Default `Material path` value seeded into FakePBR, ExoPBR, "
            "vmat-batch, and texture-batch tools when they're opened."
        )
        form.addRow("Default Material Path:", self.material_path_input)

        # 0 = auto (use cpu-derived value the tool computes itself); cap at
        # 64 so the spinbox isn't a footgun on machines with very high
        # core counts that the user may not actually want to saturate.
        self.max_parallel_spin = QSpinBox()
        self.max_parallel_spin.setRange(0, 64)
        self.max_parallel_spin.setSpecialValueText("Auto (use CPU count)")
        self.max_parallel_spin.setToolTip(
            "Default 'Max parallel' value for batch tools. 0 = the tool "
            "picks a sensible default from os.cpu_count() as before."
        )
        form.addRow("Default Max Parallel:", self.max_parallel_spin)

        self.generate_mipmaps_default = QCheckBox(
            "Generate Mipmaps checkbox starts ticked"
        )
        self.generate_mipmaps_default.setToolTip(
            "When unchecked here, the 'Generate Mipmaps' checkbox in every "
            "tool will default to off. Mipmaps are usually worth keeping; "
            "turn this off only if you're chasing a specific encoder bug."
        )
        form.addRow("", self.generate_mipmaps_default)

        outer.addWidget(defaults_group)

        session_group = QGroupBox("Session")
        session_form = QFormLayout(session_group)

        self.remember_tool = QCheckBox("Restore last opened tool on startup")
        self.remember_tool.setToolTip(
            "When on, the tool you had selected when the app last closed "
            "will be reopened on startup. The welcome screen is shown if "
            "the saved tool no longer exists."
        )
        session_form.addRow("", self.remember_tool)

        outer.addWidget(session_group)
        outer.addStretch()
        return widget

    # ------------------------------------------------------------- behaviour
    def _sync_fakepbr_enabled(self, enabled: bool) -> None:
        self.fakepbr_group.setEnabled(enabled)

    def _load_from_settings(self) -> None:
        s = self.settings

        self.fakepbr_enabled.setChecked(bool(s.get('fakepbr_overrides_enabled', False)))
        _select_combo_value(self.color_combo, int(s.get('fakepbr_color_clamp', 0) or 0))
        _select_combo_value(self.normal_combo, int(s.get('fakepbr_normal_clamp', 0) or 0))
        _select_combo_value(self.phong_combo, int(s.get('fakepbr_phong_clamp', 0) or 0))
        _select_combo_value(self.envmask_combo, int(s.get('fakepbr_envmask_clamp', 0) or 0))
        _select_combo_value(self.selfillum_combo, int(s.get('fakepbr_selfillum_clamp', 0) or 0))
        self._sync_fakepbr_enabled(self.fakepbr_enabled.isChecked())

        self.material_path_input.setText(str(s.get('default_material_path', 'models/ports') or ''))
        self.max_parallel_spin.setValue(int(s.get('default_max_parallel', 0) or 0))
        self.generate_mipmaps_default.setChecked(bool(s.get('default_generate_mipmaps', True)))
        self.remember_tool.setChecked(bool(s.get('remember_last_tool', True)))

    def _restore_defaults(self) -> None:
        """Reset only the keys this dialog manages."""
        managed_keys = {
            'fakepbr_overrides_enabled',
            'fakepbr_color_clamp',
            'fakepbr_normal_clamp',
            'fakepbr_phong_clamp',
            'fakepbr_envmask_clamp',
            'fakepbr_selfillum_clamp',
            'default_material_path',
            'default_max_parallel',
            'default_generate_mipmaps',
            'remember_last_tool',
        }
        for key in managed_keys:
            self.settings.set(key, Settings.DEFAULT_SETTINGS[key])
        self._load_from_settings()

    def _on_accept(self) -> None:
        s = self.settings
        s.set('fakepbr_overrides_enabled', self.fakepbr_enabled.isChecked())
        s.set('fakepbr_color_clamp', int(self.color_combo.currentData() or 0))
        s.set('fakepbr_normal_clamp', int(self.normal_combo.currentData() or 0))
        s.set('fakepbr_phong_clamp', int(self.phong_combo.currentData() or 0))
        s.set('fakepbr_envmask_clamp', int(self.envmask_combo.currentData() or 0))
        s.set('fakepbr_selfillum_clamp', int(self.selfillum_combo.currentData() or 0))

        s.set('default_material_path', self.material_path_input.text().strip() or 'models/ports')
        s.set('default_max_parallel', int(self.max_parallel_spin.value()))
        s.set('default_generate_mipmaps', self.generate_mipmaps_default.isChecked())
        s.set('remember_last_tool', self.remember_tool.isChecked())

        s.save()
        self.accept()
