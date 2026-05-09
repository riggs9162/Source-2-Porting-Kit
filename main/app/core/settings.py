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
        'vrf_cli_path': '',
        'vrf_recent_runs': [],
    }

    VRF_RECENT_RUNS_LIMIT = 10
    
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

    def get_vrf_cli_path(self) -> str:
        """Path to the user's Source2Viewer-CLI.exe ('' if unset)."""
        return self.get('vrf_cli_path', '') or ''

    def set_vrf_cli_path(self, path: str):
        """Persist the path to Source2Viewer-CLI.exe."""
        self.set('vrf_cli_path', path)

    def get_vrf_recent_runs(self) -> list:
        """List of recent VRF export runs, most-recent first. May be empty."""
        runs = self.get('vrf_recent_runs', [])
        return runs if isinstance(runs, list) else []

    def add_vrf_recent_run(self, run: dict):
        """LRU-insert a run into recents. Dedupes by (input, output, vpk_path)."""
        key = (run.get('input', ''), run.get('output', ''), run.get('vpk_path', ''))
        runs = [
            r for r in self.get_vrf_recent_runs()
            if (r.get('input', ''), r.get('output', ''), r.get('vpk_path', '')) != key
        ]
        runs.insert(0, run)
        self.set('vrf_recent_runs', runs[:self.VRF_RECENT_RUNS_LIMIT])

    def clear_vrf_recent_runs(self):
        self.set('vrf_recent_runs', [])
