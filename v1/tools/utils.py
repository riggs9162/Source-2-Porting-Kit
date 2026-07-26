"""
Utility functions and classes shared across tools.
"""

import os
import json
import tkinter as tk
from tkinter import ttk, filedialog

# Check for drag and drop support
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False

CONFIG_FILE = "config.json"

# Surface property keywords for auto-detection
_SURFACE_KEYWORDS = {
    'brick':    'brick',
    'concrete': 'concrete',
    'dirt':     'dirt',
    'glass':    'glass',
    'grass':    'grass',
    'gravel':   'gravel',
    'metal':    'metal',
    'plaster':  'plaster',
    'sand':     'sand',
    'tile':     'tile',
    'water':    'water',
    'wood':     'wood',
}


def determine_surfaceprop(name: str) -> str:
    """Automatically determine surface property based on filename."""
    ln = name.lower()
    for keyword, prop in _SURFACE_KEYWORDS.items():
        if ln.startswith(keyword) or ln.endswith(keyword) or keyword in ln:
            return prop
    return 'default'


def load_config():
    """Load configuration from JSON file."""
    if os.path.isfile(CONFIG_FILE):
        try:
            return json.load(open(CONFIG_FILE, "r", encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_config(cfg):
    """Save configuration to JSON file."""
    try:
        json.dump(cfg, open(CONFIG_FILE, "w", encoding="utf-8"), indent=2)
    except Exception as e:
        print("[WARN] Could not save config:", e)


class PlaceholderEntry(ttk.Entry):
    """
    Enhanced ttk.Entry with drag-and-drop support and placeholder text.
    """
    def __init__(self, master=None, placeholder="", **kwargs):
        # Remove placeholder from kwargs if present to avoid passing it to ttk.Entry
        self.placeholder = placeholder
        super().__init__(master, **kwargs)
        
        # Set up placeholder functionality
        self.placeholder_color = 'grey'
        self.default_color = self['foreground']
        
        if self.placeholder:
            self.put_placeholder()
        
        self.bind("<FocusIn>", self.foc_in)
        self.bind("<FocusOut>", self.foc_out)
        
        # Set up drag and drop support
        if DND_AVAILABLE:
            try:
                self.tk.call('tkdnd::drop_target', 'register', self, DND_FILES)
                self.bind("<<Drop>>", self._on_drop)
            except Exception as e:
                print(f"[WARN] Drag-and-drop initialization failed: {e}")

    def put_placeholder(self):
        """Display the placeholder text."""
        self.insert(0, self.placeholder)
        self['foreground'] = self.placeholder_color

    def foc_in(self, *args):
        """Handle focus in event."""
        if self['foreground'] == self.placeholder_color:
            self.delete('0', 'end')
            self['foreground'] = self.default_color

    def foc_out(self, *args):
        """Handle focus out event."""
        if not self.get():
            self.put_placeholder()

    def get(self):
        """Get the actual text value, excluding placeholder."""
        value = super().get()
        if self['foreground'] == self.placeholder_color:
            return ''
        return value
    
    def set_text(self, text):
        """Set text programmatically."""
        self.delete(0, tk.END)
        if text:
            self.insert(0, text)
            self['foreground'] = self.default_color
        elif self.placeholder:
            self.put_placeholder()

    def get_real(self):
        """Get the actual text value (alias for compatibility)."""
        return self.get()

    def _on_drop(self, event):
        """Handle drag and drop events."""
        if DND_AVAILABLE:
            path = event.data.split()[0].strip("{}")
            self.set_text(path)


def browse_folder(entry: PlaceholderEntry = None, title="Select Folder"):
    """Browse for a folder and set it in the entry, or return the path."""
    p = filedialog.askdirectory(title=title)
    if p and entry:
        entry.set_text(p)
    return p


def browse_file(entry: PlaceholderEntry = None, filetypes=None, title="Select File"):
    """Browse for a file and set it in the entry, or return the path."""
    if filetypes is None:
        filetypes = [("All files", "*.*")]
    p = filedialog.askopenfilename(filetypes=filetypes, title=title)
    if p and entry:
        entry.set_text(p)
    return p


# Context-aware browse functions that remember last used paths
_browse_contexts = {}


def browse_folder_with_context(entry: PlaceholderEntry = None, context_key: str = "default", title="Select Folder"):
    """Browse for a folder with context-aware path memory."""
    initial_dir = _browse_contexts.get(context_key, "")
    if initial_dir and not os.path.exists(initial_dir):
        initial_dir = ""
    
    p = filedialog.askdirectory(title=title, initialdir=initial_dir)
    if p:
        _browse_contexts[context_key] = p
        if entry:
            entry.set_text(p)
    return p


def browse_file_with_context(entry: PlaceholderEntry = None, context_key: str = "default", 
                           filetypes=None, title="Select File"):
    """Browse for a file with context-aware path memory."""
    if filetypes is None:
        filetypes = [("All files", "*.*")]
    
    initial_dir = _browse_contexts.get(context_key, "")
    if initial_dir and not os.path.exists(initial_dir):
        initial_dir = ""
    
    p = filedialog.askopenfilename(filetypes=filetypes, title=title, initialdir=initial_dir)
    if p:
        _browse_contexts[context_key] = os.path.dirname(p)
        if entry:
            entry.set_text(p)
    return p


def save_file_with_context(context_key: str = "default", title="Save File", 
                         defaultextension="", initialvalue="", filetypes=None):
    """Save a file with context-aware path memory."""
    if filetypes is None:
        filetypes = [("All files", "*.*")]
    
    initial_dir = _browse_contexts.get(context_key, "")
    if initial_dir and not os.path.exists(initial_dir):
        initial_dir = ""
    
    p = filedialog.asksaveasfilename(
        title=title, 
        defaultextension=defaultextension,
        initialfile=initialvalue,  # Corrected parameter
        filetypes=filetypes, 
        initialdir=initial_dir
    )
    if p:
        _browse_contexts[context_key] = os.path.dirname(p)
    return p


# Convenience functions for tools that just want to browse without an entry widget
def select_folder(title="Select Folder"):
    """Simple folder selection dialog."""
    return filedialog.askdirectory(title=title)


def select_file(filetypes=None, title="Select File"):
    """Simple file selection dialog."""
    if filetypes is None:
        filetypes = [("All files", "*.*")]
    return filedialog.askopenfilename(filetypes=filetypes, title=title)


def check_dependencies(dependencies):
    """Check if all dependencies are available."""
    missing = []
    for dep in dependencies:
        try:
            __import__(dep)
        except ImportError:
            missing.append(dep)
    return missing


def format_dependency_list(dependencies):
    """Format a list of dependencies for display."""
    return ", ".join(dependencies) if dependencies else "None"
