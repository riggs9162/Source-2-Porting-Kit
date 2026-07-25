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
        # Ragdoll & Playermodel Porter: saved run configurations (list of
        # field dicts, newest last, capped at 10). Runs saved by the retired
        # Playermodel Porter tool ('pm_porter_recent_runs') are still read as
        # a fallback and load with mode defaulting to playermodel.
        'porter_recent_runs': [],
        # FakePBR per-output VTF resolution caps. When `fakepbr_overrides_enabled`
        # is True, each non-zero value forces that specific output VTF to be
        # downscaled in Python before the buffer is handed to sourcepp. The
        # selfillum mask has its own independent cap. 0 = no override for that
        # output; uses
        # the processor's working resolution as before. These take precedence
        # over the per-tool Clamp Size combo when enabled.
        #
        # Defaults are ON: sourcepp's internal downscale path (used when the
        # input texture is larger than the format-natural size for that VTF)
        # produces corrupted normals and other artefacts on high-res source
        # PNGs. By pre-resizing in Python we guarantee sourcepp receives a
        # buffer that matches the final VTF dimensions exactly — no internal
        # downsample, no corruption. Values chosen to match typical
        # game-ready resolutions; users with reason to ship higher can raise
        # them in Preferences (or disable overrides entirely).
        'fakepbr_overrides_enabled': True,
        'fakepbr_color_clamp': 2048,
        'fakepbr_normal_clamp': 1024,
        'fakepbr_phong_clamp': 1024,
        'fakepbr_envmask_clamp': 1024,
        # Selfillum / emissive mask gets its own cap (defaults to the color
        # value so existing projects see no change). Glow masks are often
        # mostly black with a few bright details, so users may want to shrink
        # them more aggressively than the basetexture.
        'fakepbr_selfillum_clamp': 2048,
        # Global defaults that tools read at construction time. Changes don't
        # affect already-open tool instances — the user has to reopen the tool
        # (or restart) to pick up a new default. Empty string / 0 sentinels
        # mean "use the tool's hard-coded default".
        'default_material_path': 'models/ports',
        'default_max_parallel': 0,
        'default_generate_mipmaps': True,
        # Session continuity. When True, MainWindow restores whichever tool
        # was active at last close on startup. Tool name stored verbatim
        # (matches the sidebar button label).
        'remember_last_tool': True,
        'last_opened_tool': '',
        # Last-used browse directories for every file dialog in the app,
        # keyed "<Tool Name>/<field>" (see BaseTool.create_picker). One dict
        # instead of one ad-hoc top-level key per picker.
        'last_dirs': {},
    }

    VRF_RECENT_RUNS_LIMIT = 25
    
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
    
    def get_last_dir(self, key: str) -> str:
        """Last-used browse directory for a picker key ('' if none yet)."""
        dirs = self.get('last_dirs', {})
        if not isinstance(dirs, dict):
            return ''
        return str(dirs.get(key, '') or '')

    def set_last_dir(self, key: str, directory: str):
        """Remember (and immediately persist) a picker's last-used directory."""
        dirs = self.get('last_dirs', {})
        if not isinstance(dirs, dict):
            dirs = {}
        dirs[key] = directory
        self.set('last_dirs', dirs)
        self.save()

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
        """LRU-insert a run into recents. Dedupes by (input, output, vpk_path,
        batch_list) so distinct pasted model lists keep separate entries."""
        def _key(r: dict) -> tuple:
            return (
                r.get('input', ''), r.get('output', ''),
                r.get('vpk_path', ''), r.get('batch_list', ''),
            )
        key = _key(run)
        runs = [r for r in self.get_vrf_recent_runs() if _key(r) != key]
        runs.insert(0, run)
        self.set('vrf_recent_runs', runs[:self.VRF_RECENT_RUNS_LIMIT])

    def clear_vrf_recent_runs(self):
        self.set('vrf_recent_runs', [])

    # ------------------------------------------------------------------
    # FakePBR per-output resolution overrides
    # ------------------------------------------------------------------

    _FAKEPBR_CLAMP_KEYS = ("color", "normal", "phong", "envmask", "selfillum")

    def get_fakepbr_overrides(self) -> Dict[str, int]:
        """Return per-output clamp values when the override toggle is on.

        Empty dict means "overrides are off — fall back to the per-tool
        Clamp Size combo." Otherwise, keys are 'color', 'normal', 'phong',
        'envmask', 'selfillum' and values are integer max-edge caps in pixels
        (0 = no cap for that specific output).
        """
        if not bool(self.get('fakepbr_overrides_enabled', False)):
            return {}
        return {
            key: int(self.get(f'fakepbr_{key}_clamp', 0) or 0)
            for key in self._FAKEPBR_CLAMP_KEYS
        }
