"""
Combined PBR Tool - switch between Exo PBR and Fake PBR processing

This tool centralizes Exo PBR and Fake PBR into a single UI with a simple
dropdown to choose the processing mode. Default mode is Exo PBR.
"""

from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QComboBox, QStackedWidget, QSplitter
)

from .base_tool import BaseTool
from .exo_pbr_tool import ExoPBRTool
from .fake_pbr_tool import FakePBRTool


class PBRTool(BaseTool):
    """Centralized PBR tool with selectable processing mode."""

    def __init__(self):
        super().__init__("PBR Tool")
        self._build_ui()
        self._hide_base_log()

    def _build_ui(self):
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)

        label = QLabel("PBR Mode:")
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Exo PBR")
        self.mode_combo.addItem("Fake PBR")
        self.mode_combo.setCurrentIndex(0)

        header_layout.addWidget(label)
        header_layout.addWidget(self.mode_combo)
        header_layout.addStretch()

        self.content_layout.addWidget(header)

        self.stack = QStackedWidget()
        self.exo_tool = ExoPBRTool()
        self.fake_tool = FakePBRTool()
        self.stack.addWidget(self.exo_tool)
        self.stack.addWidget(self.fake_tool)

        self.content_layout.addWidget(self.stack)

        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        # Forward status messages from child tools
        self.exo_tool.status_message.connect(self.status_message.emit)
        self.fake_tool.status_message.connect(self.status_message.emit)

        # Default to Exo PBR
        self._on_mode_changed(0)

    def _hide_base_log(self):
        """Hide BaseTool logger since child tools provide their own logs."""
        try:
            logger_widget = self.log_output.parentWidget()
            if logger_widget:
                logger_widget.setVisible(False)
            splitter = self.findChild(QSplitter)
            if splitter:
                splitter.setSizes([1, 0])
        except Exception:
            pass

    def _on_mode_changed(self, index: int):
        self.stack.setCurrentIndex(index)
        mode_name = "Exo PBR" if index == 0 else "Fake PBR"
        self.emit_status(f"{mode_name} mode selected")
