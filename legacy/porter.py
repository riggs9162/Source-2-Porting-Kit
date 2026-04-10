"""
Source 2 Porting Kit

This is the main application that loads tools from the tools package dynamically.
Each tool is a separate module that can be developed and maintained independently.
"""

import os
import sys
import tkinter as tk
from tkinter import ttk, messagebox
import importlib
import pkgutil
from discordrp import Presence
import time
import threading
from typing import Dict, List

# Add the current directory to the Python path for importing tools
current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from tools import tool_registry
from tools.utils import load_config, save_config

# Check for drag and drop support
try:
    from tkinterdnd2 import TkinterDnD
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False

RPC_STATE = 'Browsing Tools'
RPC_DETAILS = 'Source 2 Porting Kit'
RPC_CLIENT_ID = '1400667977854226505'

# Global variables for RPC
presence = None
rpc_start_time = int(time.time())
last_rpc_update = 0
rpc_update_queue = None
rpc_thread = None
RPC_ENABLED = True

def init_discord_rpc():
    """Initialize Discord RPC connection."""
    global presence, rpc_start_time
    if not RPC_ENABLED:
        return False
    try:
        presence = Presence(RPC_CLIENT_ID)
        rpc_start_time = int(time.time())
        print("Discord RPC Connected")
        # Initial update
        threading.Thread(target=update_rpc_presence, daemon=True).start()
        return True
    except Exception as e:
        print(f"Failed to connect to Discord RPC: {e}")
        presence = None
        return False

def update_rpc_presence():
    """Update the Discord RPC presence with current state and details."""
    if presence:
        try:
            presence.set({
                "state": RPC_STATE,
                "details": RPC_DETAILS,
                "timestamps": {"start": rpc_start_time}
            })
        except Exception as e:
            print(f"Failed to update Discord RPC: {e}")

def threaded_rpc_update():
    """Update RPC in a separate thread to avoid blocking UI."""
    threading.Thread(target=update_rpc_presence, daemon=True).start()

def update_rpc_with_cooldown():
    """Update the Discord RPC presence with cooldown for non-interactive updates."""
    global last_rpc_update
    current_time = time.time()

    if current_time - last_rpc_update < 0.5:
        return

    threaded_rpc_update()
    last_rpc_update = current_time

def SetRPCState(state):
    """Set the Discord RPC state."""
    global RPC_STATE
    RPC_STATE = state
    threaded_rpc_update()

def SetRPCDetails(details):
    """Set the Discord RPC details."""
    global RPC_DETAILS
    RPC_DETAILS = details
    threaded_rpc_update()

def cleanup_discord_rpc():
    """Cleanup Discord RPC connection."""
    global presence
    if presence:
        try:
            presence.close()
        except:
            pass
        presence = None

def force_utf8():
    """Force UTF-8 encoding for stdout if available."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def discover_and_load_tools():
    """
    Dynamically discover and load all tools from the tools package.
    This allows new tools to be added simply by placing them in the tools folder.
    """
    tools_package = importlib.import_module('tools')
    tools_path = tools_package.__path__

    # Get all modules in the tools package
    for importer, modname, ispkg in pkgutil.iter_modules(tools_path):
        if modname not in ['base_tool', 'utils', '__init__']:
            try:
                # Import the module to register the tool
                importlib.import_module(f'tools.{modname}')
                print(f"Loaded tool module: {modname}")
            except Exception as e:
                print(f"Failed to load tool module {modname}: {e}")


class PorterApp(tk.Tk if not DND_AVAILABLE else TkinterDnD.Tk):
    """Main application class for the Source 2 Porting Kit."""

    def __init__(self):
        super().__init__()
        self.title("Source 2 Porting Kit")

        # Use app icon if available (hlvr.ico)
        try:
            ico_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hlvr.ico')
            if os.path.exists(ico_path):
                self.iconbitmap(ico_path)
        except Exception:
            pass

        # Size window to 90% of screen while keeping aspect ratio and center
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        w = int(sw * 0.9)
        h = int(sh * 0.9)
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(800, 600)

        force_utf8()

        # Load configuration early
        self.config_data = load_config()

        # Defaults for settings
        settings = self.config_data.setdefault('settings', {})
        self.dark_mode = bool(settings.get('dark_mode', False))
        self.always_on_top = bool(settings.get('always_on_top', False))
        self.enable_rpc = bool(settings.get('enable_rpc', True))
        self.show_status_bar_flag = bool(settings.get('show_status_bar', True))
        self.tab_font_size = int(settings.get('tab_font_size', 10))
        self.ui_font_size = int(settings.get('ui_font_size', 10))
        self.window_scale = float(settings.get('window_scale', 1.0))
        self.start_maximized = bool(settings.get('start_maximized', False))

        # Apply topmost & scaling
        try:
            self.attributes('-topmost', self.always_on_top)
        except Exception:
            pass
        try:
            self.tk.call('tk', 'scaling', self.window_scale)
        except Exception:
            pass
        if self.start_maximized:
            try:
                self.state('zoomed')
            except Exception:
                pass

        # Apply theme and fonts early
        self.apply_theme(self.dark_mode)
        self.apply_fonts(self.ui_font_size)

        # Initialize Discord RPC respecting settings
        global RPC_ENABLED
        RPC_ENABLED = self.enable_rpc
        init_discord_rpc()

        # Initialize tool categories tracking for RPC
        self.tool_categories: Dict[int, Dict[str, str]] = {}

        # Discover and load all available tools
        discover_and_load_tools()

        # Set up the UI
        self.setup_ui()

        # Set up window close handler
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

        # Set up RPC tracking after UI is created
        self.setup_rpc_tracking()

    # ----- Theming -----
    def apply_theme(self, dark: bool):
        style = ttk.Style(self)
        # Use a basic theme as base
        try:
            style.theme_use('clam')
        except Exception:
            pass

        if dark:
            bg = '#1e1f22'
            fg = '#e6e6e6'
            bg2 = '#2b2d31'
            acc = '#3b82f6'
            dis = '#555b66'
        else:
            bg = '#f0f0f0'
            fg = '#000000'
            bg2 = '#ffffff'
            acc = '#0b57d0'
            dis = '#a0a0a0'

        # Root bg
        try:
            self.configure(bg=bg)
        except Exception:
            pass

        # Common styles
        style.configure('TFrame', background=bg)
        style.configure('TLabelframe', background=bg)
        style.configure('TLabelframe.Label', background=bg, foreground=fg)
        style.configure('TLabel', background=bg, foreground=fg)
        style.configure('TButton', padding=6)
        style.map('TButton', foreground=[('disabled', dis), ('!disabled', fg)],
                              background=[('active', bg2), ('!active', bg2)])
        style.configure('TCheckbutton', background=bg, foreground=fg)
        style.configure('TRadiobutton', background=bg, foreground=fg)
        style.configure('TEntry', fieldbackground=bg2, foreground=fg)
        style.configure('TScrollbar', background=bg)
        style.configure('TNotebook', background=bg)
        style.configure('TNotebook.Tab', padding=(12, 6))

        # Category button styles
        style.configure('Category.TButton', background=bg2)
        style.configure('CategorySelected.TButton', background=acc, foreground='#ffffff')

        # Tab font handling
        try:
            import tkinter.font as tkfont
            if not hasattr(self, 'tab_font'):
                self.tab_font = tkfont.Font(family='Segoe UI', size=self.tab_font_size, weight='normal')
            else:
                self.tab_font.configure(size=self.tab_font_size)
            style.configure('TNotebook.Tab', font=self.tab_font)
        except Exception:
            pass

    def apply_fonts(self, ui_size: int):
        try:
            import tkinter.font as tkfont
            base_font = tkfont.nametofont('TkDefaultFont')
            base_font.configure(size=ui_size)
            text_font = tkfont.nametofont('TkTextFont')
            text_font.configure(size=ui_size)
            fixed_font = tkfont.nametofont('TkFixedFont')
            fixed_font.configure(size=ui_size)
            menu_font = tkfont.nametofont('TkMenuFont')
            menu_font.configure(size=ui_size)
            heading_font = tkfont.nametofont('TkHeadingFont')
            heading_font.configure(size=ui_size)
        except Exception:
            pass

    # ----- Settings tab -----
    def create_settings_tab(self):
        frame = ttk.Frame(self.notebook)

        row = 0
        def add_check(text, var_name):
            nonlocal row
            var = tk.BooleanVar(value=getattr(self, var_name))
            chk = ttk.Checkbutton(frame, text=text, variable=var,
                                  command=lambda n=var_name, v=var: self._on_toggle(n, v.get()))
            chk.grid(row=row, column=0, sticky='w', padx=10, pady=6)
            row += 1

        add_check('Enable Dark Mode', 'dark_mode')
        add_check('Enable Discord Rich Presence', 'enable_rpc')
        add_check('Always On Top', 'always_on_top')
        add_check('Show Status Bar', 'show_status_bar_flag')
        add_check('Start Maximized', 'start_maximized')

        # Numeric settings
        controls = ttk.Frame(frame)
        controls.grid(row=row, column=0, sticky='w', padx=10, pady=(10, 6))
        row += 1

        # Tab font size
        ttk.Label(controls, text='Tab Font Size:').grid(row=0, column=0, sticky='w')
        tab_size_var = tk.IntVar(value=self.tab_font_size)
        tab_size = ttk.Spinbox(controls, from_=8, to=18, textvariable=tab_size_var, width=5,
                               command=lambda: self._on_tab_font_size(tab_size_var.get()))
        tab_size.grid(row=0, column=1, padx=(6, 12))

        # UI font size
        ttk.Label(controls, text='UI Font Size:').grid(row=0, column=2, sticky='w')
        ui_size_var = tk.IntVar(value=self.ui_font_size)
        ui_size = ttk.Spinbox(controls, from_=8, to=18, textvariable=ui_size_var, width=5,
                    command=lambda: self._on_ui_font_size(ui_size_var.get()))
        ui_size.grid(row=0, column=3, padx=(6, 12))

        # Window scaling
        ttk.Label(controls, text='Window Scale:').grid(row=0, column=4, sticky='w')
        scale_var = tk.DoubleVar(value=self.window_scale)
        scale = ttk.Spinbox(controls, from_=0.75, to=2.0, increment=0.25, textvariable=scale_var, width=5,
                    command=lambda: self._on_window_scale(scale_var.get()))
        scale.grid(row=0, column=5, padx=(6, 12))

        # Buttons row
        btns = ttk.Frame(frame)
        btns.grid(row=row, column=0, sticky='w', padx=10, pady=(10, 10))
        row += 1

        ttk.Button(btns, text='Save Settings', command=self._save_settings).pack(side='left')
        ttk.Button(btns, text='Apply Now', command=self._apply_settings_now).pack(side='left', padx=(8, 0))

        # Info
        info = ttk.Label(frame, text='Changes to theme and visibility apply immediately.\nSettings are persisted to config.json.')
        info.grid(row=row, column=0, sticky='w', padx=10, pady=(10, 10))

        # Column weight
        frame.grid_columnconfigure(0, weight=1)
        return frame

    def _on_toggle(self, name: str, value: bool):
        setattr(self, name, value)
        # Mirror to config
        self.config_data.setdefault('settings', {})[self._setting_key(name)] = value
        if name == 'dark_mode':
            self.apply_theme(self.dark_mode)
        elif name == 'enable_rpc':
            global RPC_ENABLED
            RPC_ENABLED = self.enable_rpc
            if RPC_ENABLED and presence is None:
                init_discord_rpc()
            if not RPC_ENABLED and presence is not None:
                cleanup_discord_rpc()
        elif name == 'always_on_top':
            try:
                self.attributes('-topmost', self.always_on_top)
            except Exception:
                pass
        elif name == 'show_status_bar_flag':
            self._update_status_bar_visibility()
        elif name == 'start_maximized':
            # Apply on next start; offer immediate maximize/minimize
            try:
                if self.start_maximized:
                    self.state('zoomed')
                else:
                    self.state('normal')
            except Exception:
                pass

    def _on_tab_font_size(self, size: int):
        try:
            self.tab_font_size = int(size)
        except Exception:
            return
        self.config_data.setdefault('settings', {})['tab_font_size'] = self.tab_font_size
        self.apply_theme(self.dark_mode)

    def _on_window_scale(self, val: float):
        try:
            self.window_scale = float(val)
            self.tk.call('tk', 'scaling', self.window_scale)
        except Exception:
            pass
        self.config_data.setdefault('settings', {})['window_scale'] = self.window_scale

    def _on_ui_font_size(self, size: int):
        try:
            self.ui_font_size = int(size)
        except Exception:
            return
        self.config_data.setdefault('settings', {})['ui_font_size'] = self.ui_font_size
        self.apply_fonts(self.ui_font_size)

    def _save_settings(self):
        save_config(self.config_data)

    def _apply_settings_now(self):
        # Re-apply theme and visibility settings
        self.apply_theme(self.dark_mode)
        self._update_status_bar_visibility()

    def _update_status_bar_visibility(self):
        if hasattr(self, 'status_frame'):
            if self.show_status_bar_flag:
                self.status_frame.pack(side="bottom", fill="x", padx=5, pady=2)
            else:
                self.status_frame.pack_forget()


    def _setting_key(self, attr_name: str) -> str:
        # Map attribute names to config keys for consistency
        mapping = {
            'dark_mode': 'dark_mode',
            'enable_rpc': 'enable_rpc',
            'always_on_top': 'always_on_top',
            'show_status_bar_flag': 'show_status_bar',
            'tab_font_size': 'tab_font_size',
            'window_scale': 'window_scale',
            'start_maximized': 'start_maximized',
        }
        return mapping.get(attr_name, attr_name)

    def setup_ui(self):
        """Set up the main user interface."""
        # Create main notebook for tabs
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=5, pady=5)

        # Create Settings tab first so it's leftmost
        settings_frame = self.create_settings_tab()
        self.notebook.add(settings_frame, text="Settings")

        # Get all available tools
        available_tools = tool_registry.get_available_tools(self.config_data)

        if not available_tools:
            # Show error if no tools are available
            error_frame = ttk.Frame(self.notebook)
            self.notebook.add(error_frame, text="Error")

            ttk.Label(error_frame,
                     text="No tools were found!\n\nPlease check that the tools folder exists and contains valid tool modules.",
                     justify="center").pack(expand=True)
            return

        # Categorize and sort tools
        categorized_tools = self.categorize_tools(available_tools)

        group_order = [
            ("Materials", ["Material Conversion"]),
            ("Models", ["Model Processing"]),
            ("Audio", ["Audio Processing"]),
            ("Misc", ["File Management", "Image Processing", "Texture Processing"])
        ]

        for group_name, group_categories in group_order:
            # Collect tools in this group
            has_any = any(c in categorized_tools and categorized_tools[c] for c in group_categories)
            if not has_any:
                continue
            # Add tools under this group
            for category in group_categories:
                tools_in_category = categorized_tools.get(category, [])
                tools_in_category.sort(key=lambda tool: tool.name)
                for tool in tools_in_category:
                    try:
                        if tool.is_available:
                            # Tool is available, create its tab
                            tab_frame = tool.create_tab(self.notebook)
                            tab_name = f"{tool.name}"
                            self.notebook.add(tab_frame, text=tab_name)

                            # Track tool category for RPC
                            tab_index = len(self.notebook.tabs()) - 1
                            self.tool_categories[tab_index] = {
                                'tool_name': tool.name,
                                'category': category,
                                'status': 'available'
                            }
                        else:
                            # Tool is not available, create info tab
                            info_frame = self.create_unavailable_tool_tab(tool)
                            tab_name = f"{tool.name} (Unavailable)"
                            self.notebook.add(info_frame, text=tab_name)

                            # Track tool category for RPC
                            tab_index = len(self.notebook.tabs()) - 1
                            self.tool_categories[tab_index] = {
                                'tool_name': tool.name,
                                'category': category,
                                'status': 'unavailable'
                            }

                    except Exception as e:
                        print(f"Error creating tab for {tool.name}: {e}")
                        # Create error tab
                        error_frame = ttk.Frame(self.notebook)
                        tab_name = f"{tool.name} (Error)"
                        self.notebook.add(error_frame, text=tab_name)
                        ttk.Label(error_frame,
                                text=f"Error loading {tool.name}:\n{str(e)}",
                                justify="center").pack(expand=True)

        # Create status bar
        self.create_status_bar()

    # No category bar

    def create_unavailable_tool_tab(self, tool):
        """Create a tab showing why a tool is unavailable."""
        frame = ttk.Frame(self.notebook)

        # Tool info
        info_text = f"Tool: {tool.name}\n\n"
        info_text += f"Description: {tool.description}\n\n"
        info_text += f"Status: {tool.get_unavailable_reason()}\n\n"

        if tool.dependencies:
            info_text += "Required dependencies:\n"
            for dep in tool.dependencies:
                try:
                    __import__(dep)
                    status = "Available"
                except ImportError:
                    status = "Missing"
                info_text += f"  • {dep}: {status}\n"

            info_text += "\nTo install missing dependencies, run:\n"
            info_text += f"pip install {' '.join(tool.dependencies)}"

        # Display the info
        text_widget = tk.Text(frame, wrap=tk.WORD, state=tk.DISABLED)
        text_widget.pack(fill="both", expand=True, padx=10, pady=10)

        text_widget.config(state=tk.NORMAL)
        text_widget.insert("1.0", info_text)
        text_widget.config(state=tk.DISABLED)

        return frame


    def create_status_bar(self):
        """Create the status bar at the bottom of the window."""
        status_frame = ttk.Frame(self)
        status_frame.pack(side="bottom", fill="x", padx=5, pady=2)

        # Application info
        ttk.Label(status_frame, text="Source 2 Porting Kit").pack(side="left")

        # Tool count
        total_tools = len(tool_registry.tools)
        available_tools = len([tool for tool in tool_registry.get_available_tools(self.config_data) if tool.is_available])
        ttk.Label(status_frame, text=f"| Tools: {available_tools}/{total_tools} available").pack(side="left", padx=(10, 0))

        # Feature availability
        features = []
        if DND_AVAILABLE:
            features.append("Drag & Drop")

        # Check for common dependencies
        try:
            import PIL
            features.append("Image Processing")
        except ImportError:
            pass

        try:
            import pydub
            features.append("Audio Processing")
        except ImportError:
            pass

        if features:
            ttk.Label(status_frame, text=f"| Features: {', '.join(features)}").pack(side="left", padx=(10, 0))

        # Store for toggling visibility
        self.status_frame = status_frame
        if not self.show_status_bar_flag:
            self.status_frame.pack_forget()

    def categorize_tools(self, tools):
        """Categorize tools by their functionality."""
        categories = {
            "Audio Processing": [],
            "File Management": [],
            "Image Processing": [],
            "Material Conversion": [],
            "Model Processing": [],
            "Texture Processing": []
        }

        # Define tool categorization based on tool name/functionality
        tool_categories = {
            # Audio Processing
            "Loop Sound Converter": "Audio Processing",
            "Quad to Stereo": "Audio Processing",

            # File Management
            "Search & Replace": "File Management",
            "VMT Generator": "File Management",
            "VMT Duplicator": "File Management",
            "Soundscape Searcher": "File Management",

            # Image Processing
            "AO Baker": "Image Processing",
            "Brightness to Alpha": "Image Processing",
            "Color Transparency": "Image Processing",
            "Fake PBR Baker": "Image Processing",
            "Hotspot Editor": "Image Processing",
            "Metal Transparency": "Image Processing",
            "Subtexture Extraction": "Image Processing",

            # Material Conversion
            "VMAT to VMT": "Material Conversion",
            "Textures → VTF/VMT": "Material Conversion",

            # Model Processing
            "Bone Backport": "Model Processing",
            "QC Generation": "Model Processing",
            "QC/SMD Prefix": "Model Processing",

            # Texture Processing (legacy, keeping for compatibility)
            "Texture Tool": "Texture Processing"
        }

        # Categorize tools
        for tool in tools:
            category = tool_categories.get(tool.name, "File Management")  # Default category
            if category in categories:
                categories[category].append(tool)
            else:
                categories["File Management"].append(tool)  # Fallback

        # Remove empty categories
        return {k: v for k, v in categories.items() if v}

    # (Category bar removed)

    def setup_rpc_tracking(self):
        """Set up Discord RPC tracking for tab changes."""
        if hasattr(self, 'notebook'):
            # Bind tab change event
            self.notebook.bind("<<NotebookTabChanged>>", self.on_tab_changed)

            # Set initial RPC state
            self.update_rpc_for_current_tab()

    def on_tab_changed(self, event):
        """Handle tab change events and update Discord RPC."""
        self.update_rpc_for_current_tab()
    # no-op

    def update_rpc_for_current_tab(self):
        """Update Discord RPC based on currently selected tab."""
        if not hasattr(self, 'notebook') or not self.tool_categories:
            return

        try:
            current_tab = self.notebook.index(self.notebook.select())
            if current_tab in self.tool_categories:
                tool_info = self.tool_categories[current_tab]

                # Update RPC state and details
                if tool_info['status'] == 'available':
                    SetRPCState(f"Working with {tool_info['category']}")
                    SetRPCDetails(f"Using {tool_info['tool_name']}")
                else:
                    SetRPCState(f"Browsing {tool_info['category']}")
                    SetRPCDetails(f"Viewing {tool_info['tool_name']} (Unavailable)")
            else:
                # Fallback for unknown tabs
                SetRPCState("Browsing Tools")
                SetRPCDetails("Source 2 Porting Kit")
        except Exception as e:
            print(f"Error updating RPC for tab change: {e}")

    def on_closing(self):
        """Handle application closing."""
        # Save configuration
        save_config(self.config_data)

        # Cleanup Discord RPC
        cleanup_discord_rpc()

        # Close the application
        self.destroy()

def main():
    """Main entry point for the application."""
    try:
        app = PorterApp()
        app.mainloop()
    except Exception as e:
        # Show error dialog if the app fails to start
        root = tk.Tk()
        root.withdraw()  # Hide the root window
        messagebox.showerror("Startup Error",
                            f"Failed to start the Source 2 Porting Kit:\n\n{str(e)}\n\n"
                            f"Please check that all required files are present and try again.")
        root.destroy()


if __name__ == "__main__":
    main()
