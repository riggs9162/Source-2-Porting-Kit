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
        self.geometry("1000x750")
        self.minsize(800, 600)
        
        force_utf8()
        
        # Load configuration
        self.config_data = load_config()
        
        # Discover and load all available tools
        discover_and_load_tools()
        
        # Set up the UI
        self.setup_ui()
        
        # Set up window close handler
        self.protocol("WM_DELETE_WINDOW", self.on_closing)
    
    def setup_ui(self):
        """Set up the main user interface."""
        # Create main notebook for tabs
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=5, pady=5)
        
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
        
        # Create tabs for each category in alphabetical order
        for category in sorted(categorized_tools.keys()):
            tools_in_category = categorized_tools[category]
            
            # Sort tools within category alphabetically
            tools_in_category.sort(key=lambda tool: tool.name)
            
            # Create tabs for tools in this category
            for tool in tools_in_category:
                try:
                    if tool.is_available:
                        # Tool is available, create its tab
                        tab_frame = tool.create_tab(self.notebook)
                        tab_name = f"{tool.name}"
                        self.notebook.add(tab_frame, text=tab_name)
                    else:
                        # Tool is not available, create info tab
                        info_frame = self.create_unavailable_tool_tab(tool)
                        tab_name = f"{tool.name} (Unavailable)"
                        self.notebook.add(info_frame, text=tab_name)
                        
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
                    status = "✓ Available"
                except ImportError:
                    status = "✗ Missing"
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
            "Soundscape Searcher": "File Management",
            
            # Image Processing
            "AO Baker": "Image Processing",
            "Brightness to Alpha": "Image Processing",
            "Color Transparency": "Image Processing",
            "Fake PBR Baker": "Image Processing",
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
    
    def on_closing(self):
        """Handle application closing."""
        # Save configuration
        save_config(self.config_data)
        
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
