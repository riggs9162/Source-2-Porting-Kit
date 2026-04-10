"""
Main Window for Source 2 Porting Kit
"""

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QMenuBar, QMenu, QStatusBar, QMessageBox,
    QPushButton, QScrollArea, QStackedWidget, QSplitter, QApplication
)
from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QAction, QActionGroup, QScreen
from app.core.settings import Settings
from app.ui.styling import StyleManager, Theme
from app.tools.search_replace_tool import SearchReplaceTool
from app.tools.filename_sanitizer_tool import FilenameSanitizerTool
from app.tools.pbr_tool import PBRTool
from app.tools.bone_backport_tool import BoneBackportTool
from app.tools.soundscape_porter_tool import SoundscapePorterTool
from app.tools.loop_point_tool import LoopPointTool
from app.tools.quad_to_stereo_tool import QuadToStereoTool
from app.tools.ogg_converter_tool import OggConverterTool
from app.tools.alpha_mask_tool import AlphaMaskTool
from app.tools.folder_search_replace_tool import FolderSearchReplaceTool
from app.tools.texture_pbr_batch_tool import TexturePBRBatchTool
from app.tools.vmat_pbr_tool import VmatPBRTool
from app.tools.gltf_smd_batch_tool import GltfSmdBatchTool
from app.tools.hotspot_editor_tool import HotspotEditorTool


class MainWindow(QMainWindow):
    """Main application window"""
    
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.current_tool = None
        self.tools = {}
        self.setup_ui()
        self.restore_window_state()
        
    def setup_ui(self):
        """Initialize the user interface"""
        self.setWindowTitle("Source 2 Porting Kit")
        self.setMinimumSize(1000, 700)
        
        # Create central widget with splitter
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        main_layout = QHBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # Create splitter for sidebar and content
        splitter = QSplitter(Qt.Horizontal)
        
        # Sidebar
        sidebar = self.create_sidebar()
        splitter.addWidget(sidebar)
        
        # Content area (stacked widget for different tools)
        self.content_stack = QStackedWidget()
        
        # Welcome page
        welcome_widget = QWidget()
        welcome_layout = QVBoxLayout(welcome_widget)
        welcome_label = QLabel("Source 2 Porting Kit")
        welcome_label.setAlignment(Qt.AlignCenter)
        welcome_label.setStyleSheet("font-size: 24px; font-weight: bold;")
        welcome_layout.addWidget(welcome_label)
        
        instructions = QLabel("Select a tool from the sidebar to get started")
        instructions.setAlignment(Qt.AlignCenter)
        instructions.setStyleSheet("font-size: 12px; color: #808080;")
        welcome_layout.addWidget(instructions)
        
        self.content_stack.addWidget(welcome_widget)
        splitter.addWidget(self.content_stack)
        
        # Set splitter sizes (sidebar smaller)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([200, 800])
        
        main_layout.addWidget(splitter)
        
        # Setup menu bar and status bar
        self.setup_menu_bar()
        self.statusBar().showMessage("Ready")
        
    def create_sidebar(self):
        """Create the sidebar with tool buttons"""
        sidebar_widget = QWidget()
        sidebar_widget.setMinimumWidth(180)
        sidebar_widget.setMaximumWidth(250)
        
        sidebar_layout = QVBoxLayout(sidebar_widget)
        sidebar_layout.setContentsMargins(8, 8, 8, 8)
        sidebar_layout.setSpacing(4)
        
        # Title
        title_label = QLabel("Tools")
        title_label.setStyleSheet("font-size: 14px; font-weight: bold; padding: 4px;")
        sidebar_layout.addWidget(title_label)
        
        # Scrollable area for tools
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        
        tools_widget = QWidget()
        tools_layout = QVBoxLayout(tools_widget)
        tools_layout.setContentsMargins(0, 0, 0, 0)
        tools_layout.setSpacing(4)
        
        # Add tool buttons grouped by category
        tool_categories = {
            "Models": [
                ("Bone Backport", BoneBackportTool),
                ("GLTF Batch SMD", GltfSmdBatchTool),
            ],
            "Materials": [
                ("Alpha Mask", AlphaMaskTool),
                ("Hotspot Editor", HotspotEditorTool),
                ("PBR Tool", PBRTool),
                ("Texture PBR Batch", TexturePBRBatchTool),
                ("VMAT PBR", VmatPBRTool),
            ],
            "Sounds": [
                ("Loop Point Converter", LoopPointTool),
                ("OGG Converter", OggConverterTool),
                ("Quad to Stereo", QuadToStereoTool),
                ("Soundscape Porter", SoundscapePorterTool),
            ],
            "Utility": [
                ("Filename Sanitizer", FilenameSanitizerTool),
                ("Search && Replace (Files)", SearchReplaceTool),
                ("Search && Replace (Folder)", FolderSearchReplaceTool),
            ],
        }
        
        # Add tools by category
        for category_name, tools in tool_categories.items():
            # Category header
            category_label = QLabel(category_name)
            category_label.setStyleSheet("font-size: 12px; font-weight: bold; padding: 8px 4px 4px 4px; color: #cccccc;")
            tools_layout.addWidget(category_label)
            
            # Sort tools alphabetically within category
            tools.sort(key=lambda x: x[0].replace("&&", "&"))
            
            # Add tool buttons
            for tool_name, tool_class in tools:
                self.add_tool_button(tools_layout, tool_name, tool_class)
        
        # Add more tool buttons here in the future
        
        tools_layout.addStretch()
        scroll_area.setWidget(tools_widget)
        sidebar_layout.addWidget(scroll_area)
        
        return sidebar_widget
    
    def add_tool_button(self, layout, tool_name: str, tool_class):
        """Add a tool button to the sidebar"""
        btn = QPushButton(tool_name)
        btn.setCheckable(True)
        btn.setMinimumHeight(36)
        btn.setStyleSheet("""
            QPushButton {
                text-align: left;
                padding: 8px 12px;
                border-radius: 4px;
            }
            QPushButton:checked {
                background-color: #094771;
                font-weight: bold;
            }
        """)
        btn.clicked.connect(lambda: self.show_tool(tool_name, tool_class))
        layout.addWidget(btn)
        
        # Store button reference
        if not hasattr(self, 'tool_buttons'):
            self.tool_buttons = []
        self.tool_buttons.append(btn)
    
    def show_tool(self, tool_name: str, tool_class):
        """Show the selected tool"""
        # Find the button that was clicked
        clicked_button = None
        for btn in self.tool_buttons:
            if btn.text() == tool_name:
                clicked_button = btn
                break
        
        # If the button is already checked, keep it checked (prevent deselection)
        if clicked_button and clicked_button.isChecked():
            # Uncheck all other buttons
            for btn in self.tool_buttons:
                if btn != clicked_button:
                    btn.setChecked(False)
        else:
            # Re-check the clicked button (in case it was unchecked by clicking again)
            if clicked_button:
                clicked_button.setChecked(True)
            
            # Uncheck all other buttons
            for btn in self.tool_buttons:
                if btn.text() != tool_name:
                    btn.setChecked(False)
        
        # Create tool instance if not exists
        if tool_name not in self.tools:
            tool_instance = tool_class()
            tool_instance.status_message.connect(self.update_status)
            self.tools[tool_name] = tool_instance
            self.content_stack.addWidget(tool_instance)
        
        # Show the tool
        self.content_stack.setCurrentWidget(self.tools[tool_name])
        # Replace && with & for display in status bar
        display_name = tool_name.replace("&&", "&")
        self.statusBar().showMessage(f"{display_name} tool active")
        
    def update_status(self, message: str):
        """Update status bar message"""
        self.statusBar().showMessage(message, 5000)
        
    def setup_menu_bar(self):
        """Create the menu bar"""
        menubar = self.menuBar()
        
        # File menu
        file_menu = menubar.addMenu("&File")
        
        exit_action = QAction("E&xit", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        
        # View menu
        view_menu = menubar.addMenu("&View")
        
        # Theme submenu
        theme_menu = view_menu.addMenu("&Theme")
        theme_group = QActionGroup(self)
        theme_group.setExclusive(True)
        
        current_theme = self.settings.get_theme()
        
        dark_action = QAction("&Dark", self)
        dark_action.setCheckable(True)
        dark_action.setChecked(current_theme == Theme.DARK)
        dark_action.triggered.connect(lambda: self.change_theme(Theme.DARK))
        theme_group.addAction(dark_action)
        theme_menu.addAction(dark_action)
        
        light_action = QAction("&Light", self)
        light_action.setCheckable(True)
        light_action.setChecked(current_theme == Theme.LIGHT)
        light_action.triggered.connect(lambda: self.change_theme(Theme.LIGHT))
        theme_group.addAction(light_action)
        theme_menu.addAction(light_action)
        
        system_action = QAction("&System", self)
        system_action.setCheckable(True)
        system_action.setChecked(current_theme == Theme.SYSTEM)
        system_action.triggered.connect(lambda: self.change_theme(Theme.SYSTEM))
        theme_group.addAction(system_action)
        theme_menu.addAction(system_action)
        
        # Help menu
        help_menu = menubar.addMenu("&Help")
        
        about_action = QAction("&About", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)
        
    def change_theme(self, theme: Theme):
        """Change the application theme"""
        from PySide6.QtWidgets import QApplication
        self.settings.set_theme(theme)
        self.settings.save()
        StyleManager.apply_theme(QApplication.instance(), theme)
        self.statusBar().showMessage(f"Theme changed to {theme.value}", 3000)
        
    def restore_window_state(self):
        """Restore window size and position"""
        # Check if we have saved settings
        has_saved_size = 'window_width' in self.settings.settings and \
                        'window_height' in self.settings.settings
        
        if has_saved_size:
            # Use saved dimensions
            width = self.settings.get('window_width', 800)
            height = self.settings.get('window_height', 600)
            self.resize(width, height)
            
            if self.settings.get('window_maximized', False):
                self.showMaximized()
        else:
            # First run - size based on screen with padding
            self.size_to_screen()
    
    def size_to_screen(self, padding_percent: float = 0.10):
        """
        Size the window based on available screen space with padding
        
        Args:
            padding_percent: Percentage of screen size to use as padding (0.10 = 10%)
        """
        # Get the screen that contains the window
        screen = QApplication.primaryScreen()
        if screen:
            screen_geometry = screen.availableGeometry()
            
            # Calculate padding
            h_padding = int(screen_geometry.width() * padding_percent)
            v_padding = int(screen_geometry.height() * padding_percent)
            
            # Calculate window size
            window_width = screen_geometry.width() - (h_padding * 2)
            window_height = screen_geometry.height() - (v_padding * 2)
            
            # Ensure minimum size
            window_width = max(window_width, 1000)
            window_height = max(window_height, 700)
            
            # Set size and center on screen
            self.resize(window_width, window_height)
            
            # Center the window
            x = screen_geometry.x() + (screen_geometry.width() - window_width) // 2
            y = screen_geometry.y() + (screen_geometry.height() - window_height) // 2
            self.move(x, y)
    
    def closeEvent(self, event):
        """Save window state before closing"""
        self.settings.set('window_width', self.width())
        self.settings.set('window_height', self.height())
        self.settings.set('window_maximized', self.isMaximized())
        self.settings.save()
        event.accept()
        
    def show_about(self):
        """Show about dialog"""
        QMessageBox.about(
            self,
            "About Source 2 Porting Kit",
            "Source 2 Porting Kit v2.0\n\n"
            "A tool for porting Source 2 content"
        )
