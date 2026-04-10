"""
Application settings management
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional
from app.utils.helpers import get_config_dir
from app.ui.styling import Theme


class Settings:
    """Manages application settings"""
    
    DEFAULT_SETTINGS = {
        'theme': Theme.DARK.value,
        'font_family': 'Segoe UI',
        'font_size': 10,
        'window_width': 800,
        'window_height': 600,
        'window_maximized': False,
    }
    
    def __init__(self):
        self.settings_file = get_config_dir() / 'settings.json'
        self.settings = self._load_settings()
    
    def _load_settings(self) -> Dict[str, Any]:
        """Load settings from file"""
        if self.settings_file.exists():
            try:
                with open(self.settings_file, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    # Merge with defaults to ensure all keys exist
                    return {**self.DEFAULT_SETTINGS, **loaded}
            except (json.JSONDecodeError, OSError):
                pass
        return self.DEFAULT_SETTINGS.copy()
    
    def save(self):
        """Save settings to file"""
        try:
            self.settings_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f, indent=4)
        except OSError:
            pass
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get a setting value"""
        return self.settings.get(key, default)
    
    def set(self, key: str, value: Any):
        """Set a setting value"""
        self.settings[key] = value
    
    def get_theme(self) -> Theme:
        """Get the current theme"""
        theme_value = self.get('theme', Theme.DARK.value)
        try:
            return Theme(theme_value)
        except ValueError:
            return Theme.DARK
    
    def set_theme(self, theme: Theme):
        """Set the current theme"""
        self.set('theme', theme.value)
