"""
Base tool widget that all tools inherit from
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTextEdit, QLabel, QSplitter
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QTextCursor
from datetime import datetime


class BaseTool(QWidget):
    """Base class for all tool widgets"""
    
    # Signal emitted when tool wants to update status bar
    status_message = Signal(str)
    
    def __init__(self, tool_name: str):
        super().__init__()
        self.tool_name = tool_name
        self.setup_ui()
        
    def setup_ui(self):
        """Setup the base UI with content area and logger"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # Create splitter for content and logger
        splitter = QSplitter(Qt.Vertical)
        
        # Content area (to be populated by subclasses)
        self.content_widget = QWidget()
        self.content_layout = QVBoxLayout(self.content_widget)
        splitter.addWidget(self.content_widget)
        
        # Logger section
        logger_widget = QWidget()
        logger_layout = QVBoxLayout(logger_widget)
        logger_layout.setContentsMargins(4, 4, 4, 4)
        
        logger_label = QLabel("Log")
        logger_label.setStyleSheet("font-weight: bold;")
        logger_layout.addWidget(logger_label)
        
        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumHeight(150)
        self.log_output.setMinimumHeight(80)
        logger_layout.addWidget(self.log_output)
        
        splitter.addWidget(logger_widget)
        
        # Set splitter sizes (content gets more space)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        
        layout.addWidget(splitter)
        
    def log(self, message: str, level: str = "INFO"):
        """
        Add a message to the log
        
        Args:
            message: Message to log
            level: Log level (INFO, WARNING, ERROR, SUCCESS)
        """
        import html
        timestamp = datetime.now().strftime("%H:%M:%S")
        
        # Color based on level
        color_map = {
            "INFO": "#d4d4d4",
            "WARNING": "#ffcc00",
            "ERROR": "#ff6b6b",
            "SUCCESS": "#4ec9b0"
        }
        color = color_map.get(level, "#d4d4d4")
        
        # Escape HTML entities in the message
        escaped_message = html.escape(message)
        
        formatted_message = f'<span style="color: #808080;">[{timestamp}]</span> ' \
                          f'<span style="color: {color};">[{level}]</span> {escaped_message}'
        
        self.log_output.append(formatted_message)
        
        # Auto-scroll to bottom
        cursor = self.log_output.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.log_output.setTextCursor(cursor)
        
    def clear_log(self):
        """Clear the log output"""
        self.log_output.clear()
        
    def emit_status(self, message: str):
        """Emit a status bar message"""
        self.status_message.emit(message)
