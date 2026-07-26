"""
Application styling and theme management
"""

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPalette, QColor, QFont
from PySide6.QtCore import Qt
from enum import Enum


class Theme(Enum):
    """Available application themes"""
    DARK = "dark"
    LIGHT = "light"
    SYSTEM = "system"


class StyleManager:
    """Manages application styling and themes"""
    
    # Color schemes
    DARK_PALETTE = {
        'window': '#1e1e1e',
        'windowText': '#d4d4d4',
        'base': '#252526',
        'alternateBase': '#2d2d30',
        'toolTipBase': '#252526',
        'toolTipText': '#d4d4d4',
        'text': '#d4d4d4',
        'button': '#2d2d30',
        'buttonText': '#d4d4d4',
        'brightText': '#ffffff',
        'link': '#4a9eff',
        'highlight': '#094771',
        'highlightedText': '#ffffff',
    }
    
    LIGHT_PALETTE = {
        'window': '#f3f3f3',
        'windowText': '#000000',
        'base': '#ffffff',
        'alternateBase': '#f0f0f0',
        'toolTipBase': '#ffffdc',
        'toolTipText': '#000000',
        'text': '#000000',
        'button': '#f0f0f0',
        'buttonText': '#000000',
        'brightText': '#ffffff',
        'link': '#0066cc',
        'highlight': '#0078d4',
        'highlightedText': '#ffffff',
    }
    
    @staticmethod
    def set_font(app: QApplication, font_family: str = "Segoe UI", font_size: int = 10):
        """
        Set application font
        
        Args:
            app: QApplication instance
            font_family: Font family name
            font_size: Font size in points
        """
        font = QFont(font_family, font_size)
        app.setFont(font)
    
    @staticmethod
    def apply_theme(app: QApplication, theme: Theme = Theme.DARK):
        """
        Apply a theme to the application
        
        Args:
            app: QApplication instance
            theme: Theme to apply
        """
        if theme == Theme.DARK:
            StyleManager._apply_dark_theme(app)
        elif theme == Theme.LIGHT:
            StyleManager._apply_light_theme(app)
        else:
            # System theme - use default
            app.setPalette(app.style().standardPalette())
    
    @staticmethod
    def _apply_dark_theme(app: QApplication):
        """Apply dark theme"""
        palette = QPalette()
        colors = StyleManager.DARK_PALETTE
        
        palette.setColor(QPalette.Window, QColor(colors['window']))
        palette.setColor(QPalette.WindowText, QColor(colors['windowText']))
        palette.setColor(QPalette.Base, QColor(colors['base']))
        palette.setColor(QPalette.AlternateBase, QColor(colors['alternateBase']))
        palette.setColor(QPalette.ToolTipBase, QColor(colors['toolTipBase']))
        palette.setColor(QPalette.ToolTipText, QColor(colors['toolTipText']))
        palette.setColor(QPalette.Text, QColor(colors['text']))
        palette.setColor(QPalette.Button, QColor(colors['button']))
        palette.setColor(QPalette.ButtonText, QColor(colors['buttonText']))
        palette.setColor(QPalette.BrightText, QColor(colors['brightText']))
        palette.setColor(QPalette.Link, QColor(colors['link']))
        palette.setColor(QPalette.Highlight, QColor(colors['highlight']))
        palette.setColor(QPalette.HighlightedText, QColor(colors['highlightedText']))
        
        # Disabled colors
        palette.setColor(QPalette.Disabled, QPalette.WindowText, QColor('#808080'))
        palette.setColor(QPalette.Disabled, QPalette.Text, QColor('#808080'))
        palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor('#808080'))
        
        app.setPalette(palette)
        
        # Apply additional stylesheet
        app.setStyleSheet("""
            QToolTip {
                color: #d4d4d4;
                background-color: #252526;
                border: 1px solid #454545;
                padding: 4px;
            }
            QMenuBar {
                background-color: #2d2d30;
                color: #d4d4d4;
            }
            QMenuBar::item {
                background-color: transparent;
                color: #d4d4d4;
                padding: 4px 8px;
            }
            QMenuBar::item:selected {
                background-color: #094771;
                color: #d4d4d4;
            }
            QMenuBar::item:pressed {
                background-color: #0e639c;
                color: #d4d4d4;
            }
            QMenu {
                background-color: #252526;
                color: #d4d4d4;
                border: 1px solid #454545;
            }
            QMenu::item {
                padding: 4px 20px 4px 8px;
                color: #d4d4d4;
            }
            QMenu::item:selected {
                background-color: #094771;
                color: #d4d4d4;
            }
            QComboBox {
                background-color: #252526;
                color: #d4d4d4;
                border: 1px solid #454545;
                padding: 4px;
            }
            QComboBox:hover {
                border: 1px solid #007acc;
            }
            QComboBox::drop-down {
                border: none;
                background-color: transparent;
            }
            QComboBox QAbstractItemView {
                background-color: #252526;
                color: #d4d4d4;
                selection-background-color: #094771;
                selection-color: #d4d4d4;
                border: 1px solid #454545;
            }
            QPushButton {
                background-color: #2d2d30;
                color: #d4d4d4;
                border: 1px solid #454545;
                padding: 4px 12px;
            }
            QPushButton:hover {
                background-color: #3e3e42;
                border: 1px solid #007acc;
                color: #d4d4d4;
            }
            QPushButton:pressed {
                background-color: #094771;
                color: #d4d4d4;
            }
            QPushButton:disabled {
                background-color: #2d2d30;
                color: #808080;
                border: 1px solid #3e3e42;
            }
            QStatusBar {
                background-color: #007acc;
                color: #ffffff;
            }
        """)
    
    @staticmethod
    def _apply_light_theme(app: QApplication):
        """Apply light theme"""
        palette = QPalette()
        colors = StyleManager.LIGHT_PALETTE
        
        palette.setColor(QPalette.Window, QColor(colors['window']))
        palette.setColor(QPalette.WindowText, QColor(colors['windowText']))
        palette.setColor(QPalette.Base, QColor(colors['base']))
        palette.setColor(QPalette.AlternateBase, QColor(colors['alternateBase']))
        palette.setColor(QPalette.ToolTipBase, QColor(colors['toolTipBase']))
        palette.setColor(QPalette.ToolTipText, QColor(colors['toolTipText']))
        palette.setColor(QPalette.Text, QColor(colors['text']))
        palette.setColor(QPalette.Button, QColor(colors['button']))
        palette.setColor(QPalette.ButtonText, QColor(colors['buttonText']))
        palette.setColor(QPalette.BrightText, QColor(colors['brightText']))
        palette.setColor(QPalette.Link, QColor(colors['link']))
        palette.setColor(QPalette.Highlight, QColor(colors['highlight']))
        palette.setColor(QPalette.HighlightedText, QColor(colors['highlightedText']))
        
        # Disabled colors
        palette.setColor(QPalette.Disabled, QPalette.WindowText, QColor('#a0a0a0'))
        palette.setColor(QPalette.Disabled, QPalette.Text, QColor('#a0a0a0'))
        palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor('#a0a0a0'))
        
        app.setPalette(palette)
        
        # Apply additional stylesheet
        app.setStyleSheet("""
            QToolTip {
                color: #000000;
                background-color: #ffffdc;
                border: 1px solid #c0c0c0;
                padding: 4px;
            }
            QMenuBar {
                background-color: #f3f3f3;
                color: #000000;
            }
            QMenuBar::item {
                background-color: transparent;
                color: #000000;
                padding: 4px 8px;
            }
            QMenuBar::item:selected {
                background-color: #e5f3ff;
                color: #000000;
            }
            QMenuBar::item:pressed {
                background-color: #cce8ff;
                color: #000000;
            }
            QMenu {
                background-color: #ffffff;
                color: #000000;
                border: 1px solid #c0c0c0;
            }
            QMenu::item {
                padding: 4px 20px 4px 8px;
                color: #000000;
            }
            QMenu::item:selected {
                background-color: #e5f3ff;
                color: #000000;
            }
            QComboBox {
                background-color: #ffffff;
                color: #000000;
                border: 1px solid #c0c0c0;
                padding: 4px;
            }
            QComboBox:hover {
                border: 1px solid #0078d4;
            }
            QComboBox::drop-down {
                border: none;
                background-color: transparent;
            }
            QComboBox QAbstractItemView {
                background-color: #ffffff;
                color: #000000;
                selection-background-color: #e5f3ff;
                selection-color: #000000;
                border: 1px solid #c0c0c0;
            }
            QPushButton {
                background-color: #f0f0f0;
                color: #000000;
                border: 1px solid #c0c0c0;
                padding: 4px 12px;
            }
            QPushButton:hover {
                background-color: #e5f3ff;
                border: 1px solid #0078d4;
                color: #000000;
            }
            QPushButton:pressed {
                background-color: #cce8ff;
                color: #000000;
            }
            QPushButton:disabled {
                background-color: #f3f3f3;
                color: #a0a0a0;
                border: 1px solid #d0d0d0;
            }
            QStatusBar {
                background-color: #0078d4;
                color: #ffffff;
            }
        """)
