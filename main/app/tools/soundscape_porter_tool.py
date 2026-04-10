"""
Soundscape Porter Tool
Converts Source 2 soundscapes to Source 1 format
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFileDialog, QTextEdit, QGroupBox, QComboBox,
    QProgressBar, QCheckBox
)
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont
from pathlib import Path
from app.tools.base_tool import BaseTool
import re
import json
from typing import Dict, List, Tuple, Optional
from datetime import datetime


class SoundscapeParser:
    """Parse Source 2 soundscape files"""
    
    @staticmethod
    def parse_soundscape(content: str, soundscape_name: str) -> Optional[Dict]:
        """
        Extract a specific soundscape block from Source 2 format
        
        Args:
            content: File content as string
            soundscape_name: Name of the soundscape to extract
            
        Returns:
            Dictionary with soundscape data, or None if not found
        """
        # Find the soundscape block using a more lenient pattern
        pattern = rf'"{re.escape(soundscape_name)}"\s*{{(.*?)\n}}'
        match = re.search(pattern, content, re.DOTALL)
        
        if not match:
            return None
        
        block_content = match.group(1)
        soundscape_data = {
            'name': soundscape_name,
            'playevents': []
        }
        
        # Extract playevent blocks more robustly
        # Look for "playevent" followed by a block { ... }
        playevent_pattern = r'"playevent"\s*\{([^}]*)\}'
        for playevent_match in re.finditer(playevent_pattern, block_content, re.DOTALL):
            playevent_content = playevent_match.group(1)
            playevent = SoundscapeParser._parse_playevent(playevent_content)
            if playevent:
                soundscape_data['playevents'].append(playevent)
        
        return soundscape_data
    
    @staticmethod
    def _parse_playevent(content: str) -> Optional[Dict]:
        """Parse a single playevent block"""
        playevent = {}
        
        # Extract key-value pairs (handle both quoted and unquoted values, with optional tabs)
        # Pattern: "key" optional_whitespace "value" or "key" optional_whitespace "value"
        kvpair_pattern = r'"([^"]+)"\s+"([^"]+)"'
        for match in re.finditer(kvpair_pattern, content):
            key = match.group(1)
            value = match.group(2)
            playevent[key] = value
        
        return playevent if playevent else None


class SoundEventResolver:
    """Resolve and find sound events referenced in soundscapes"""
    
    def __init__(self, soundevents_dir: Path):
        self.soundevents_dir = soundevents_dir
        self._cache: Dict[str, Dict] = {}
    
    def find_sound_event(self, event_name: str) -> Tuple[Optional[Dict], Optional[Path]]:
        """
        Find a sound event definition across all soundevent files
        
        Args:
            event_name: Name of the sound event (e.g., "AmbientA5Vault.LargeRoomLp01")
            
        Returns:
            Tuple of (sound_event_data, file_path) or (None, None) if not found
        """
        if event_name in self._cache:
            return self._cache[event_name]
        
        # Search through all .vsndevts files
        for vsndevts_file in sorted(self.soundevents_dir.glob("*.vsndevts")):
            try:
                content = vsndevts_file.read_text(encoding='utf-8', errors='ignore')
                
                # Look for the event definition in Source 2 format: EventName = { ... }
                # The pattern matches: word characters, dots, equals sign, opening brace
                pattern = f'{re.escape(event_name)}\\s*=\\s*{{(.*?)^\\t?}}'
                match = re.search(pattern, content, re.DOTALL | re.MULTILINE)
                
                if match:
                    event_content = match.group(1)
                    event_data = self._parse_sound_event(event_content)
                    event_data['name'] = event_name
                    result = (event_data, vsndevts_file)
                    self._cache[event_name] = result
                    return result
            except Exception:
                continue
        
        self._cache[event_name] = (None, None)
        return None, None
    
    @staticmethod
    def _parse_sound_event(content: str) -> Dict:
        """Parse a sound event block"""
        event = {}
        
        # First, handle array values like vsnd_files = [ ... ]
        array_pattern = r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*\[(.*?)\]'
        for match in re.finditer(array_pattern, content, re.DOTALL):
            key = match.group(1).strip()
            array_content = match.group(2)
            # Extract quoted strings from the array
            items = re.findall(r'"([^"]*)"', array_content)
            event[key] = items if items else [array_content.strip()]
        
        # Then handle regular key-value pairs
        # Formats: key = "value" or key = value or key = number
        # Skip lines that are already part of arrays
        lines = content.split('\n')
        for line in lines:
            # Skip lines that look like array content
            if line.strip().startswith('[') or line.strip().startswith(']') or line.strip().startswith('"'):
                continue
            
            kvpair_pattern = r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*([^[\n]*?)(?:\s*[,\n]|$)'
            for match in re.finditer(kvpair_pattern, line):
                key = match.group(1).strip()
                if key in event:  # Skip if already parsed as array
                    continue
                value = match.group(2).strip()
                # Remove quotes if present
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                event[key] = value
        
        return event
    
    def resolve_dependencies(self, event_name: str) -> List[str]:
        """
        Recursively resolve all sound event dependencies
        
        Args:
            event_name: Name of the sound event
            
        Returns:
            List of all event names needed
        """
        dependencies = set()
        to_process = [event_name]
        processed = set()
        
        while to_process:
            current = to_process.pop(0)
            if current in processed:
                continue
            processed.add(current)
            dependencies.add(current)
            
            event_data, _ = self.find_sound_event(current)
            if not event_data:
                continue
            
            # Check for referenced soundevents
            for key, value in event_data.items():
                if key.startswith('soundevent_'):
                    to_process.append(value)
                elif key == 'base':
                    to_process.append(value)
        
        return list(dependencies)


class SoundscapeConverter:
    """Convert Source 2 soundscapes to Source 1 format"""
    
    def __init__(self, hla_dir_or_resolver):
        """Initialize the converter with either a Path or SoundEventResolver"""
        if isinstance(hla_dir_or_resolver, Path):
            # Create resolver from path
            soundevents_dir = hla_dir_or_resolver / 'soundevents'
            self.event_resolver = SoundEventResolver(soundevents_dir)
        else:
            # Assume it's already a resolver
            self.event_resolver = hla_dir_or_resolver
    
    def convert(self, soundscape_data: Dict, include_sound_events: bool = True) -> Dict:
        """
        Convert a Source 2 soundscape to Source 1 format
        
        Args:
            soundscape_data: Parsed Source 2 soundscape
            include_sound_events: Whether to include sound event definitions
            
        Returns:
            Dictionary with Source 1 soundscape format
        """
        result = {
            'name': soundscape_data['name'],
            'rules': []
        }
        
        # Process each playevent
        for playevent in soundscape_data.get('playevents', []):
            event_name = playevent.get('event', '')
            if not event_name:
                continue
            
            # Find the sound event
            event_data, source_file = self.event_resolver.find_sound_event(event_name)
            
            if not event_data:
                # If not found, create a placeholder
                rule = {
                    'type': 'playlooping',
                    'comment': f'Sound event not found: {event_name}',
                    'volume': playevent.get('volume', '1.0'),
                    'wave': f'sounds/placeholder_{event_name.lower()}.wav'
                }
            else:
                # Convert the sound event to a Source 1 rule
                rule = self._convert_sound_event_to_rule(
                    event_name,
                    event_data,
                    playevent
                )
            
            result['rules'].append(rule)
        
        return result
    
    def _determine_rule_type(self, event_data: Dict) -> Tuple[str, Optional[str]]:
        """
        Determine if a sound is looping or random based on Source 2 event type and properties
        
        Returns:
            Tuple of (rule_type, time_range) where rule_type is 'playlooping' or 'playrandom'
        """
        event_type = event_data.get('type', '')
        
        # Check for random event indicators
        has_random_timer = any(
            k.startswith('random_soundevent_') and 'timer_' in k
            for k in event_data.keys()
        )
        
        # Check for delay parameters (non-zero rand_delay indicates random)
        rand_delay_min = float(event_data.get('rand_delay_min', 0) or 0)
        rand_delay_max = float(event_data.get('rand_delay_max', 0) or 0)
        has_delay = rand_delay_min != 0 or rand_delay_max != 0
        
        # Determine based on event type
        if 'hlvr_ambient_rand' in event_type:
            # Random event type - extract timer values
            timer_min = event_data.get('random_soundevent_01_timer_min', '4.0')
            timer_max = event_data.get('random_soundevent_01_timer_max', '8.0')
            try:
                t_min = float(timer_min or 4.0)
                t_max = float(timer_max or 8.0)
                time_range = f'{int(t_min)},{int(t_max)}'
            except (ValueError, TypeError):
                time_range = '4,8'
            return 'playrandom', time_range
        
        elif 'hlvr_start' in event_type:
            # Start/looping event type
            if has_delay:
                # Has delay - use random with delay
                return 'playrandom', f'{int(rand_delay_min)},{int(rand_delay_max)}'
            else:
                # No delay - continuous loop
                return 'playlooping', None
        
        elif 'hlvr_ambient_fixed_rotation' in event_type or 'hlvr_default_3d' in event_type:
            # Fixed rotation or 3D sounds are typically looping
            return 'playlooping', None
        
        else:
            # Default to looping for unknown types
            return 'playlooping', None
    
    def _convert_sound_event_to_rule(self, event_name: str, event_data: Dict, playevent: Dict) -> Dict:
        """Convert a Source 2 sound event to a Source 1 rule"""
        rule = {}
        
        # Determine rule type based on Source 2 event type and properties
        rule_type, time_range = self._determine_rule_type(event_data)
        rule['type'] = rule_type
        
        # Add time range for playrandom rules
        if time_range:
            rule['time'] = time_range
        
        # Add volume from playevent
        volume = playevent.get('volume', event_data.get('volume', '1.0'))
        try:
            rule['volume'] = str(float(volume))
        except (ValueError, TypeError):
            rule['volume'] = '1.0'
        
        # Add pitch if present
        if 'pitch' in event_data:
            try:
                pitch_val = float(event_data['pitch'])
                # Convert pitch scale if needed
                rule['pitch'] = str(int(pitch_val * 100))
            except (ValueError, TypeError):
                pass
        
        # Extract sound file paths - collect all wave files from this event and referenced events
        all_wave_files = []
        
        # Check for direct vsnd_file_XX entries
        direct_vsnd_files = [v for k, v in event_data.items() if k.startswith('vsnd_file_') and isinstance(v, str)]
        all_wave_files.extend([v for v in direct_vsnd_files if v and not v.startswith('!')])
        
        # Check for vsnd_files array (e.g., in child random events)
        if 'vsnd_files' in event_data:
            vsnd_array = event_data['vsnd_files']
            if isinstance(vsnd_array, list):
                all_wave_files.extend([v for v in vsnd_array if v and not v.startswith('!')])
        
        # Resolve referenced sound events to find their vsnd files
        soundevent_refs = [v for k, v in event_data.items() if k.startswith('soundevent_') and isinstance(v, str)]
        random_soundevent_refs = [v for k, v in event_data.items() if k.startswith('random_soundevent_') and isinstance(v, str)]
        
        for ref_event in soundevent_refs + random_soundevent_refs:
            if ref_event and not ref_event.startswith('!'):
                resolved_event_data, _ = self.event_resolver.find_sound_event(ref_event)
                if resolved_event_data:
                    # Check for vsnd_file entries in resolved event
                    ref_vsnds = [v for k, v in resolved_event_data.items() if k.startswith('vsnd_file_') and isinstance(v, str)]
                    all_wave_files.extend([v for v in ref_vsnds if v and not v.startswith('!')])
                    
                    # Also check for vsnd_files array
                    if 'vsnd_files' in resolved_event_data:
                        ref_array = resolved_event_data['vsnd_files']
                        if isinstance(ref_array, list):
                            all_wave_files.extend([v for v in ref_array if v and not v.startswith('!')])
        
        # Convert vsnd paths to wav paths
        wave_paths = []
        for vsnd_path in all_wave_files:
            if vsnd_path.endswith('.vsnd'):
                wave_path = vsnd_path[:-5] + '.wav'
            else:
                wave_path = vsnd_path + '.wav'
            wave_paths.append(wave_path)
        
        # For playrandom with multiple files, add all as separate wave entries
        # For playlooping, just use the first one
        if wave_paths:
            if rule_type == 'playrandom' and len(wave_paths) > 1:
                # For multiple random sounds, store them as a list
                rule['waves'] = wave_paths
                # Also set single wave for compatibility
                rule['wave'] = wave_paths[0]
            else:
                rule['wave'] = wave_paths[0]
        else:
            # Fallback: check for base sound path
            if 'wave' in event_data:
                rule['wave'] = event_data['wave']
            else:
                # Use placeholder
                rule['wave'] = f'ambient/{event_name.lower().replace(".", "_")}.wav'
        
        # Add attenuation/soundlevel
        rule['soundlevel'] = 'SNDLVL_80dB'  # Default
        
        # Add traveler/position info if available
        traveler = playevent.get('traveler', '')
        if traveler:
            rule['comment'] = f'Traveler: {traveler}'
        
        # Add any additional settings from event_data
        if 'dsp_preset' in event_data:
            rule['dsp'] = event_data['dsp_preset']
        
        return rule
    
    def generate_source1_text(self, soundscape_data: Dict) -> str:
        """Generate Source 1 soundscape text format"""
        lines = []
        
        # Soundscape header
        lines.append(f'"{soundscape_data["name"]}"')
        lines.append('{')
        
        # Add rules
        for rule in soundscape_data.get('rules', []):
            rule_type = rule.get('type', 'playlooping')
            
            lines.append(f'\t"{rule_type}"')
            lines.append('\t{')
            
            # Add comment if present
            if 'comment' in rule:
                lines.append(f'\t\t// {rule["comment"]}')
            
            # Add volume
            if 'volume' in rule:
                lines.append(f'\t\t"volume"\t"{rule["volume"]}"')
            
            # Add pitch if present
            if 'pitch' in rule:
                lines.append(f'\t\t"pitch"\t"{rule["pitch"]}"')
            
            # Add time for playrandom
            if rule_type == 'playrandom' and 'time' in rule:
                lines.append(f'\t\t"time"\t"{rule["time"]}"')
            
            # Add soundlevel
            if 'soundlevel' in rule:
                lines.append(f'\t\t"soundlevel"\t"{rule["soundlevel"]}"')
            
            # Add DSP if present
            if 'dsp' in rule:
                lines.append(f'\t\t"dsp"\t"{rule["dsp"]}"')
            
            # Add wave/sound files based on rule type
            if rule_type == 'playrandom':
                # For playrandom, waves must be inside rndwave block
                lines.append('\t\t"rndwave"')
                lines.append('\t\t{')
                
                if 'waves' in rule:
                    # Multiple waves
                    for wave in rule['waves']:
                        lines.append(f'\t\t\t"wave"\t"{wave}"')
                elif 'wave' in rule:
                    # Single wave still needs rndwave block
                    lines.append(f'\t\t\t"wave"\t"{rule["wave"]}"')
                
                lines.append('\t\t}')
            else:
                # For playlooping, wave goes directly in rule
                if 'wave' in rule:
                    lines.append(f'\t\t"wave"\t"{rule["wave"]}"')
            
            lines.append('\t}')
            lines.append('')
        
        lines.append('}')
        
        return '\n'.join(lines)


class SoundscapePorterWorker(QThread):
    """Worker thread for soundscape porting"""
    
    progress = Signal(str, str)  # message, level
    finished = Signal(bool, str)  # success, message
    soundscape_list = Signal(list)  # List of available soundscapes
    
    def __init__(self, hla_dir: Path, soundscape_name: str, mode: str = 'info'):
        super().__init__()
        self.hla_dir = hla_dir
        self.soundscape_name = soundscape_name
        self.mode = mode  # 'info', 'convert', 'export'
        self.result = None
    
    def run(self):
        """Execute the porting operation"""
        try:
            if self.mode == 'list':
                self._list_soundscapes()
            elif self.mode == 'info':
                self._get_soundscape_info()
            elif self.mode == 'convert':
                self._convert_soundscape()
        except Exception as e:
            self.finished.emit(False, f"Error: {str(e)}")
    
    def _list_soundscapes(self):
        """List all available soundscapes"""
        soundscapes = []
        scripts_dir = self.hla_dir / 'scripts'
        
        if not scripts_dir.exists():
            self.finished.emit(False, "Scripts directory not found")
            return
        
        for soundscape_file in sorted(scripts_dir.glob('soundscapes_*.txt')):
            try:
                content = soundscape_file.read_text(encoding='utf-8', errors='ignore')
                # Find all soundscape names in the file
                pattern = r'"([a-zA-Z0-9_]+)"\\s*\\{'
                matches = re.findall(pattern, content)
                
                for match in matches:
                    soundscapes.append(match)
                
            except Exception as e:
                self.progress.emit(f"Error reading {soundscape_file.name}: {e}", "ERROR")
        
        self.soundscape_list.emit(sorted(set(soundscapes)))
        self.finished.emit(True, f"Found {len(set(soundscapes))} soundscapes")
    
    def _get_soundscape_info(self):
        """Get information about a specific soundscape"""
        self.progress.emit(f"Searching for soundscape: {self.soundscape_name}", "INFO")
        
        scripts_dir = self.hla_dir / 'scripts'
        if not scripts_dir.exists():
            self.finished.emit(False, "Scripts directory not found")
            return
        
        for soundscape_file in scripts_dir.glob('soundscapes_*.txt'):
            try:
                content = soundscape_file.read_text(encoding='utf-8', errors='ignore')
                soundscape_data = SoundscapeParser.parse_soundscape(
                    content, self.soundscape_name
                )
                
                if soundscape_data:
                    self.progress.emit(f"Found in: {soundscape_file.name}", "SUCCESS")
                    self.progress.emit(
                        f"Play events: {len(soundscape_data.get('playevents', []))}",
                        "INFO"
                    )
                    
                    # List events
                    for i, pe in enumerate(soundscape_data.get('playevents', []), 1):
                        event = pe.get('event', 'Unknown')
                        volume = pe.get('volume', 'N/A')
                        self.progress.emit(f"  {i}. {event} (vol: {volume})", "INFO")
                    
                    self.result = soundscape_data
                    self.finished.emit(True, "Soundscape found")
                    return
                    
            except Exception as e:
                self.progress.emit(f"Error reading {soundscape_file.name}: {e}", "ERROR")
        
        self.finished.emit(False, f"Soundscape '{self.soundscape_name}' not found")
    
    def _convert_soundscape(self):
        """Convert a soundscape from Source 2 to Source 1"""
        self.progress.emit("Initializing conversion...", "INFO")
        
        scripts_dir = self.hla_dir / 'scripts'
        soundevents_dir = self.hla_dir / 'soundevents'
        
        if not scripts_dir.exists():
            self.finished.emit(False, "Scripts directory not found")
            return
        
        if not soundevents_dir.exists():
            self.finished.emit(False, "Soundevents directory not found")
            return
        
        # Find and parse soundscape
        self.progress.emit(f"Searching for soundscape: {self.soundscape_name}", "INFO")
        soundscape_data = None
        source_file = None
        
        for soundscape_file in sorted(scripts_dir.glob('soundscapes_*.txt')):
            try:
                content = soundscape_file.read_text(encoding='utf-8', errors='ignore')
                soundscape_data = SoundscapeParser.parse_soundscape(
                    content, self.soundscape_name
                )
                
                if soundscape_data:
                    source_file = soundscape_file
                    break
                    
            except Exception as e:
                self.progress.emit(f"Error reading {soundscape_file.name}: {e}", "ERROR")
        
        if not soundscape_data:
            self.finished.emit(False, f"Soundscape '{self.soundscape_name}' not found")
            return
        
        self.progress.emit(f"Found in: {source_file.name}", "SUCCESS")
        
        # Resolve sound events
        self.progress.emit("Resolving sound events...", "INFO")
        event_resolver = SoundEventResolver(soundevents_dir)
        
        event_names = set()
        for playevent in soundscape_data.get('playevents', []):
            event_name = playevent.get('event', '')
            if event_name:
                try:
                    deps = event_resolver.resolve_dependencies(event_name)
                    event_names.update(deps)
                    self.progress.emit(f"  Resolved: {event_name}", "INFO")
                except Exception as e:
                    self.progress.emit(f"  Warning: Could not resolve {event_name}: {e}", "WARNING")
        
        self.progress.emit(f"Found {len(event_names)} sound events", "SUCCESS")
        
        # Convert
        self.progress.emit("Converting to Source 1 format...", "INFO")
        converter = SoundscapeConverter(event_resolver)
        converted = converter.convert(soundscape_data, include_sound_events=True)
        
        self.result = converted
        self.finished.emit(True, f"Converted {len(event_names)} sound events")


class SoundscapePorterTab(QWidget):
    """GUI tab for soundscape porter"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.hla_dir = Path()
        self.current_soundscape = None
        self.current_conversion = None
        self.setup_ui()
    
    def setup_ui(self):
        """Setup the user interface"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)
        
        # Directory selection section
        dir_group = QGroupBox("Half-Life Alyx Directory")
        dir_layout = QHBoxLayout(dir_group)
        
        self.dir_input = QLineEdit()
        self.dir_input.setPlaceholderText("Select Half-Life Alyx directory...")
        dir_layout.addWidget(self.dir_input)
        
        self.dir_button = QPushButton("Browse...")
        self.dir_button.clicked.connect(self.browse_directory)
        dir_layout.addWidget(self.dir_button)
        
        layout.addWidget(dir_group)
        
        # Soundscape selection section
        soundscape_group = QGroupBox("Soundscape Selection")
        soundscape_layout = QHBoxLayout(soundscape_group)
        
        soundscape_layout.addWidget(QLabel("Soundscape:"))
        
        self.soundscape_combo = QComboBox()
        self.soundscape_combo.setEditable(True)
        self.soundscape_combo.currentTextChanged.connect(self.on_soundscape_changed)
        soundscape_layout.addWidget(self.soundscape_combo)
        
        self.refresh_button = QPushButton("Refresh List")
        self.refresh_button.clicked.connect(self.refresh_soundscape_list)
        self.refresh_button.setMaximumWidth(120)
        soundscape_layout.addWidget(self.refresh_button)
        
        layout.addWidget(soundscape_group)
        
        # Info button
        self.info_button = QPushButton("Get Info")
        self.info_button.clicked.connect(self.get_soundscape_info)
        layout.addWidget(self.info_button)
        
        # Options section
        options_group = QGroupBox("Conversion Options")
        options_layout = QVBoxLayout(options_group)
        
        self.include_events_checkbox = QCheckBox("Include sound events in output")
        self.include_events_checkbox.setChecked(True)
        options_layout.addWidget(self.include_events_checkbox)
        
        layout.addWidget(options_group)
        
        # Conversion button
        self.convert_button = QPushButton("Convert to Source 1 Format")
        self.convert_button.setStyleSheet("font-weight: bold;")
        self.convert_button.clicked.connect(self.convert_soundscape)
        layout.addWidget(self.convert_button)
        
        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)
        
        # Output section
        output_group = QGroupBox("Output Preview")
        output_layout = QVBoxLayout(output_group)
        
        self.output_text = QTextEdit()
        self.output_text.setReadOnly(True)
        self.output_text.setFont(QFont("Courier", 9))
        self.output_text.setMaximumHeight(300)
        output_layout.addWidget(self.output_text)
        
        # Export button
        export_layout = QHBoxLayout()
        
        self.export_json_button = QPushButton("Export as JSON")
        self.export_json_button.clicked.connect(self.export_json)
        export_layout.addWidget(self.export_json_button)
        
        self.export_text_button = QPushButton("Export as Text")
        self.export_text_button.clicked.connect(self.export_text)
        export_layout.addWidget(self.export_text_button)
        
        export_layout.addStretch()
        output_layout.addLayout(export_layout)
        
        layout.addWidget(output_group)
        layout.addStretch()
    
    def browse_directory(self):
        """Browse for Half-Life Alyx directory"""
        dir_path = QFileDialog.getExistingDirectory(
            self,
            "Select Half-Life Alyx Directory",
            str(Path.home())
        )
        
        if dir_path:
            self.hla_dir = Path(dir_path)
            self.dir_input.setText(dir_path)
            self.refresh_soundscape_list()
    
    def refresh_soundscape_list(self):
        """Refresh the list of available soundscapes"""
        if not self.hla_dir.exists():
            self.show_message("Please select a valid directory first", "WARNING")
            return
        
        self.soundscape_combo.clear()
        self.soundscape_combo.addItem("Loading soundscapes...")
        
        self.worker = SoundscapePorterWorker(self.hla_dir, "", mode='list')
        self.worker.soundscape_list.connect(self.on_soundscape_list_received)
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.progress.connect(self.on_worker_progress)
        self.worker.start()
    
    def on_soundscape_list_received(self, soundscapes: List[str]):
        """Handle received soundscape list"""
        self.soundscape_combo.clear()
        self.soundscape_combo.addItems(soundscapes)
    
    def on_soundscape_changed(self, text: str):
        """Handle soundscape selection change"""
        self.current_soundscape = text
    
    def get_soundscape_info(self):
        """Get information about the selected soundscape"""
        if not self.hla_dir.exists():
            self.show_message("Please select a directory first", "WARNING")
            return
        
        if not self.current_soundscape:
            self.show_message("Please select a soundscape first", "WARNING")
            return
        
        self.output_text.clear()
        self.info_button.setEnabled(False)
        
        self.worker = SoundscapePorterWorker(
            self.hla_dir,
            self.current_soundscape,
            mode='info'
        )
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.progress.connect(self.on_worker_progress)
        self.worker.start()
    
    def convert_soundscape(self):
        """Convert the selected soundscape"""
        if not self.hla_dir.exists():
            self.show_message("Please select a directory first", "WARNING")
            return
        
        if not self.current_soundscape:
            self.show_message("Please select a soundscape first", "WARNING")
            return
        
        self.output_text.clear()
        self.convert_button.setEnabled(False)
        self.progress_bar.setVisible(True)
        
        self.worker = SoundscapePorterWorker(
            self.hla_dir,
            self.current_soundscape,
            mode='convert'
        )
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.progress.connect(self.on_worker_progress)
        self.worker.start()
    
    def on_worker_progress(self, message: str, level: str):
        """Handle worker progress messages"""
        color_map = {
            "INFO": "#d4d4d4",
            "WARNING": "#ffcc00",
            "ERROR": "#ff6b6b",
            "SUCCESS": "#4ec9b0"
        }
        color = color_map.get(level, "#d4d4d4")
        
        formatted_message = f'<span style="color: {color};">[{level}]</span> {message}'
        self.output_text.append(formatted_message)
    
    def on_worker_finished(self, success: bool, message: str):
        """Handle worker completion"""
        self.info_button.setEnabled(True)
        self.convert_button.setEnabled(True)
        self.progress_bar.setVisible(False)
        
        if success and hasattr(self.worker, 'result'):
            self.current_conversion = self.worker.result
            self.show_conversion_preview()
        
        self.show_message(message, "SUCCESS" if success else "ERROR")
    
    def show_conversion_preview(self):
        """Show a preview of the converted soundscape"""
        if not self.current_conversion:
            return
        
        preview = "\n=== CONVERTED SOUNDSCAPE (Source 1 Format) ===\n\n"
        soundscape = self.current_conversion
        
        preview += f"Name: {soundscape.get('name', 'Unknown')}\n\n"
        preview += "Rules:\n"
        
        for i, rule in enumerate(soundscape.get('rules', []), 1):
            rule_type = rule.get('type', 'unknown')
            wave = rule.get('wave', 'unknown')
            volume = rule.get('volume', 'N/A')
            preview += f"\n{i}. {rule_type.upper()}\n"
            preview += f"   Wave: {wave}\n"
            preview += f"   Volume: {volume}\n"
            if 'pitch' in rule:
                preview += f"   Pitch: {rule['pitch']}\n"
            if 'comment' in rule:
                preview += f"   {rule['comment']}\n"
        
        self.output_text.append(preview)
    
    def export_json(self):
        """Export converted soundscape as JSON"""
        if not self.current_conversion:
            self.show_message("No converted soundscape to export", "WARNING")
            return
        
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export as JSON",
            str(Path.home() / f"{self.current_soundscape}_converted.json"),
            "JSON Files (*.json)"
        )
        
        if file_path:
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(self.current_conversion, f, indent=2)
                self.show_message(f"Exported to {file_path}", "SUCCESS")
            except Exception as e:
                self.show_message(f"Export failed: {e}", "ERROR")
    def export_text(self):
        """Export converted soundscape as text (Source 1 format)"""
        if not self.current_conversion:
            self.show_message("No converted soundscape to export", "WARNING")
            return
        
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export as Text",
            str(Path.home() / f"{self.current_soundscape}_converted.txt"),
            "Text Files (*.txt)"
        )
        
        if file_path:
            try:
                content = self._generate_source1_format()
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                self.show_message(f"Exported to {file_path}", "SUCCESS")
            except Exception as e:
                self.show_message(f"Export failed: {e}", "ERROR")
    
    def _generate_source1_format(self) -> str:
        """Generate Source 1 soundscape format from conversion"""
        if not self.current_conversion:
            return ""
        
        converter = SoundscapeConverter(self.hla_dir)
        return converter.generate_source1_text(self.current_conversion)
    
    def show_message(self, message: str, level: str = "INFO"):
        """Show a message in the output"""
        color_map = {
            "INFO": "#d4d4d4",
            "WARNING": "#ffcc00",
            "ERROR": "#ff6b6b",
            "SUCCESS": "#4ec9b0"
        }
        color = color_map.get(level, "#d4d4d4")
        
        formatted_message = f'<span style="color: {color};">[{level}]</span> {message}'
        self.output_text.append(formatted_message)


class SoundscapePorterTool(BaseTool):
    """Soundscape Porter Tool"""
    
    def __init__(self):
        super().__init__("Soundscape Porter")
        # Add the soundscape porter tab to the content layout
        tab = SoundscapePorterTab()
        self.content_layout.addWidget(tab)
