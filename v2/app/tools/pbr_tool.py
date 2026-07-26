"""
Combined PBR Tool - switch between Exo PBR and Fake PBR processing

This tool centralizes Exo PBR and Fake PBR into a single UI with a simple
dropdown to choose the processing mode. Default mode is Fake PBR.
"""

from PySide6.QtWidgets import QComboBox, QLabel, QStackedWidget

from .base_tool import BaseTool
from .exo_pbr_tool import ExoPBRTool
from .fake_pbr_tool import FakePBRTool
from app.ui import widgets


class PBRTool(BaseTool):
    """Centralized PBR tool with selectable processing mode."""

    def __init__(self):
        super().__init__("PBR Tool")
        self._build_ui()
        # Child tools provide their own log panes, so hide the shared one.
        self.set_log_visible(False)

    def _build_ui(self):
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Fake PBR")
        self.mode_combo.addItem("Exo PBR")
        self.mode_combo.setCurrentIndex(0)
        self.content_layout.addLayout(
            widgets.hbox(QLabel("PBR Mode:"), self.mode_combo, stretch_after=True))

        self.stack = QStackedWidget()
        self.fake_tool = FakePBRTool()
        self.exo_tool = ExoPBRTool()
        self.stack.addWidget(self.fake_tool)
        self.stack.addWidget(self.exo_tool)
        self.content_layout.addWidget(self.stack)

        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        # Forward status messages from child tools
        self.exo_tool.status_message.connect(self.status_message.emit)
        self.fake_tool.status_message.connect(self.status_message.emit)

        # Default to Fake PBR
        self._on_mode_changed(0)

    def _on_mode_changed(self, index: int):
        self.stack.setCurrentIndex(index)
        mode_name = "Fake PBR" if index == 0 else "Exo PBR"
        self.emit_status(f"{mode_name} mode selected")
