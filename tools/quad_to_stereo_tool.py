"""
Quad to Stereo Audio Tool - Convert quad audio files to stereo format.
"""

import os
import re
import logging
import warnings
import shutil
import math
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from .base_tool import BaseTool, register_tool
from .utils import PlaceholderEntry, browse_folder

# Suppress pydub RuntimeWarnings about ffmpeg/ffprobe
warnings.filterwarnings("ignore", category=RuntimeWarning)

@register_tool
class QuadToStereoTool(BaseTool):
    @property
    def name(self) -> str:
        return "Quad to Stereo"

    @property
    def description(self) -> str:
        return "Convert quad audio files (L, LS, R, RS) to stereo format"

    @property
    def dependencies(self) -> list:
        return ["pydub"]

    def create_tab(self, parent) -> ttk.Frame:
        return QuadToStereoTab(parent, self.config)

class TextHandler(logging.Handler):
    """Logging handler for Tkinter Text widget."""
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record):
        msg = self.format(record)
        self.text_widget.configure(state='normal')
        self.text_widget.insert(tk.END, msg + '\n')
        self.text_widget.configure(state='disabled')
        self.text_widget.see(tk.END)

class QuadToStereoTab(ttk.Frame):
    def __init__(self, parent, config):
        super().__init__(parent)
        self.config = config
        self.setup_ui()
        self.setup_logging()

    def setup_ui(self):
        """Set up the user interface."""
        main_frame = ttk.Frame(self)
        main_frame.pack(fill="both", expand=True, padx=10, pady=10)

        # Input section
        input_frame = ttk.LabelFrame(main_frame, text="Input", padding=10)
        input_frame.pack(fill="x", pady=(0, 10))

        # Audio folder row
        audio_row = ttk.Frame(input_frame)
        audio_row.pack(fill="x", pady=2)
        ttk.Label(audio_row, text="Audio Folder:").pack(side="left")
        self.input_folder = PlaceholderEntry(audio_row, placeholder="Select folder containing quad audio files...")
        self.input_folder.pack(side="left", fill="x", expand=True, padx=(5, 0))
        ttk.Button(audio_row, text="Browse",
                command=self.browse_input_folder).pack(side="right", padx=(5, 0))

        # Output folder row
        output_row = ttk.Frame(input_frame)
        output_row.pack(fill="x", pady=2)
        ttk.Label(output_row, text="Output Folder:").pack(side="left")
        self.output_folder = PlaceholderEntry(output_row, placeholder="Select output folder for stereo files...")
        self.output_folder.pack(side="left", fill="x", expand=True, padx=(5, 0))
        ttk.Button(output_row, text="Browse",
                command=self.browse_output_folder).pack(side="right", padx=(5, 0))

        # File pattern section
        pattern_frame = ttk.LabelFrame(main_frame, text="File Pattern", padding=10)
        pattern_frame.pack(fill="x", pady=(0, 10))

        # Pattern explanation
        pattern_info = ttk.Label(pattern_frame,
                            text="Quad audio files should be named: basename_L.mp3, basename_LS.mp3, basename_R.mp3, basename_RS.mp3")
        pattern_info.pack(anchor="w", pady=(0, 5))

        # Pattern input
        pattern_row = ttk.Frame(pattern_frame)
        pattern_row.pack(fill="x", pady=2)
        ttk.Label(pattern_row, text="File Pattern:").pack(side="left")
        self.file_pattern = tk.StringVar(value="*_(l|ls|r|rs).mp3")
        pattern_entry = ttk.Entry(pattern_row, textvariable=self.file_pattern, width=30)
        pattern_entry.pack(side="left", fill="x", expand=True, padx=(5, 0))
        ttk.Label(pattern_row, text="(regex pattern)").pack(side="right", padx=(5, 0))

        pattern_frame.columnconfigure(1, weight=1)

        # Options section
        options_frame = ttk.LabelFrame(main_frame, text="Options", padding=10)
        options_frame.pack(fill="x", pady=(0, 10))

        # Mixing options
        mix_frame = ttk.Frame(options_frame)
        mix_frame.pack(fill="x", pady=(0, 5))

        ttk.Label(mix_frame, text="Mix Mode:").pack(side="left")

        self.mix_mode = tk.StringVar(value="balance")
        ttk.Radiobutton(mix_frame, text="Balance (L+LS → Left, R+RS → Right)",
                    variable=self.mix_mode, value="balance").pack(side="left", padx=(10, 0))
        ttk.Radiobutton(mix_frame, text="Downmix (All → Stereo)",
                    variable=self.mix_mode, value="downmix").pack(side="left", padx=(10, 0))

        # Volume options
        volume_frame = ttk.Frame(options_frame)
        volume_frame.pack(fill="x", pady=(5, 0))

        ttk.Label(volume_frame, text="Volume Adjustment:").grid(row=0, column=0, sticky="w", pady=2)
        self.volume_adjustment = tk.DoubleVar(value=1.0)
        volume_scale = ttk.Scale(volume_frame, from_=0.1, to=2.0, orient="horizontal",
                                variable=self.volume_adjustment, command=self.update_volume_label)
        volume_scale.grid(row=0, column=1, sticky="ew", padx=(5, 0), pady=2)
        self.volume_label = ttk.Label(volume_frame, text="1.0x")
        self.volume_label.grid(row=0, column=2, padx=(5, 0), pady=2)

        volume_frame.columnconfigure(1, weight=1)

        # File options
        file_options_frame = ttk.Frame(options_frame)
        file_options_frame.pack(fill="x", pady=(10, 0))

        self.overwrite_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(file_options_frame, text="Overwrite existing files",
                    variable=self.overwrite_var).pack(side="left")

        self.delete_originals_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(file_options_frame, text="Delete original quad files after conversion",
                    variable=self.delete_originals_var).pack(side="left", padx=(10, 0))

        # Output format
        format_frame = ttk.Frame(options_frame)
        format_frame.pack(fill="x", pady=(10, 0))

        ttk.Label(format_frame, text="Output Format:").pack(side="left")
        self.output_format = tk.StringVar(value="mp3")
        format_combo = ttk.Combobox(format_frame, textvariable=self.output_format,
                                values=["mp3", "wav", "ogg"], state="readonly", width=10)
        format_combo.pack(side="left", padx=(5, 0))

        # Quality settings
        ttk.Label(format_frame, text="Quality:").pack(side="left", padx=(20, 5))
        self.quality = tk.StringVar(value="192k")
        quality_combo = ttk.Combobox(format_frame, textvariable=self.quality,
                                    values=["128k", "192k", "256k", "320k"], state="readonly", width=10)
        quality_combo.pack(side="left")

        # Action buttons
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill="x", pady=(0, 10))

        ttk.Button(button_frame, text="Scan for Quad Groups",
                command=self.scan_quad_groups).pack(side="left")
        ttk.Button(button_frame, text="Convert to Stereo",
                command=self.convert_to_stereo).pack(side="left", padx=(10, 0))
        ttk.Button(button_frame, text="Clear Log",
                command=self.clear_log).pack(side="right")

        # Log section
        log_frame = ttk.LabelFrame(main_frame, text="Log", padding=10)
        log_frame.pack(fill="both", expand=True)

        self.log_text = ScrolledText(log_frame, height=12, width=70, state='disabled')
        self.log_text.pack(fill="both", expand=True)

        # Status label
        self.status_label = ttk.Label(main_frame, text="Ready", foreground="green")
        self.status_label.pack(pady=(10, 0))

    def setup_logging(self):
        """Set up logging to the text widget."""
        self.logger = logging.getLogger('QuadToStereo')
        self.logger.setLevel(logging.INFO)

        # Clear any existing handlers
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)

        # Add text widget handler
        text_handler = TextHandler(self.log_text)
        text_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s', '%H:%M:%S'))
        self.logger.addHandler(text_handler)

    def update_volume_label(self, value=None):
        """Update the volume label."""
        volume = round(self.volume_adjustment.get(), 1)
        self.volume_label.config(text=f"{volume}x")

    def browse_input_folder(self):
        """Browse for input folder."""
        path = browse_folder(title="Select folder containing quad audio files")
        if path:
            self.input_folder.set_text(path)

    def browse_output_folder(self):
        """Browse for output folder."""
        path = browse_folder(title="Select output folder for stereo files")
        if path:
            self.output_folder.set_text(path)

    def clear_log(self):
        """Clear the log text."""
        self.log_text.configure(state='normal')
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state='disabled')

    def find_quad_groups(self, root_path):
        """Find all quad audio groups under root path."""
        pattern = re.compile(r'(.+?)_(l|ls|r|rs)\.mp3$', re.IGNORECASE)
        groups = {}

        if not os.path.exists(root_path):
            return groups

        for root, dirs, files in os.walk(root_path):
            for file in files:
                match = pattern.match(file)
                if match:
                    base_name = match.group(1)
                    channel = match.group(2).lower()
                    file_path = os.path.join(root, file)

                    if base_name not in groups:
                        groups[base_name] = {'root': root}

                    groups[base_name][channel] = file_path

        # Filter complete groups (must have all 4 channels)
        complete_groups = {}
        for base_name, files in groups.items():
            if all(ch in files for ch in ['l', 'ls', 'r', 'rs']):
                complete_groups[base_name] = files

        return complete_groups

    def scan_quad_groups(self):
        """Scan for quad audio groups and display results."""
        input_folder = self.input_folder.get()
        if not input_folder:
            messagebox.showerror("Error", "Please select an input folder first.")
            return

        self.logger.info(f"Scanning for quad audio groups in: {input_folder}")

        groups = self.find_quad_groups(input_folder)

        if not groups:
            self.logger.info("No complete quad audio groups found.")
            self.status_label.config(text="No quad groups found", foreground="orange")
            return

        self.logger.info(f"Found {len(groups)} complete quad audio groups:")

        for base_name, files in groups.items():
            self.logger.info(f"  {base_name}:")
            for channel in ['l', 'ls', 'r', 'rs']:
                if channel in files:
                    rel_path = os.path.relpath(files[channel], input_folder)
                    self.logger.info(f"    {channel.upper()}: {rel_path}")

        self.status_label.config(text=f"Found {len(groups)} quad groups", foreground="green")

    def convert_to_stereo(self):
        """Convert quad audio files to stereo."""
        input_folder = self.input_folder.get()
        output_folder = self.output_folder.get()

        if not input_folder:
            messagebox.showerror("Error", "Please select an input folder first.")
            return

        if not output_folder:
            messagebox.showerror("Error", "Please select an output folder first.")
            return

        # Check if pydub is available
        try:
            from pydub import AudioSegment
        except ImportError:
            messagebox.showerror("Error", "pydub library is required for audio processing.\n"
                                "Please install it with: pip install pydub")
            return

        groups = self.find_quad_groups(input_folder)

        if not groups:
            messagebox.showinfo("No Files", "No complete quad audio groups found.")
            return

        # Confirm conversion
        result = messagebox.askyesno("Confirm Conversion",
                                    f"Convert {len(groups)} quad audio groups to stereo?")
        if not result:
            return

        # Create output directory if it doesn't exist
        os.makedirs(output_folder, exist_ok=True)

        mix_mode = self.mix_mode.get()
        volume_adjustment = self.volume_adjustment.get()
        output_format = self.output_format.get()
        quality = self.quality.get()
        overwrite = self.overwrite_var.get()
        delete_originals = self.delete_originals_var.get()

        converted = 0
        skipped = 0
        errors = 0

        self.logger.info(f"Starting conversion of {len(groups)} quad groups...")
        self.logger.info(f"Mix mode: {mix_mode}")
        self.logger.info(f"Volume adjustment: {volume_adjustment}x")
        self.logger.info(f"Output format: {output_format} ({quality})")

        for base_name, files in groups.items():
            try:
                self.logger.info(f"Processing: {base_name}")

                # Determine output filename
                output_filename = f"{base_name}_stereo.{output_format}"
                output_filename = f"{base_name}_stereo.{output_format}"
                output_path = os.path.join(output_folder, output_filename)

                # Check if output exists
                if os.path.exists(output_path) and not overwrite:
                    self.logger.info(f"  Skipped (file exists): {output_filename}")
                    skipped += 1
                    continue

                # Load audio files
                l_audio = AudioSegment.from_mp3(files['l'])
                ls_audio = AudioSegment.from_mp3(files['ls'])
                r_audio = AudioSegment.from_mp3(files['r'])
                rs_audio = AudioSegment.from_mp3(files['rs'])

                # Ensure all files have the same length
                min_length = min(len(l_audio), len(ls_audio), len(r_audio), len(rs_audio))
                l_audio = l_audio[:min_length]
                ls_audio = ls_audio[:min_length]
                r_audio = r_audio[:min_length]
                rs_audio = rs_audio[:min_length]

                # Mix channels based on mode
                if mix_mode == "balance":
                    # L+LS to left channel, R+RS to right channel
                    left_channel = l_audio.overlay(ls_audio)
                    right_channel = r_audio.overlay(rs_audio)
                else:  # downmix
                    # Mix all channels to stereo
                    mono_mix = l_audio.overlay(ls_audio).overlay(r_audio).overlay(rs_audio)
                    left_channel = mono_mix
                    right_channel = mono_mix

                # Apply volume adjustment
                if volume_adjustment != 1.0:
                    left_channel = left_channel + (20 * math.log10(volume_adjustment))
                    right_channel = right_channel + (20 * math.log10(volume_adjustment))

                # Create stereo audio
                stereo_audio = AudioSegment.from_mono_audiosegments(left_channel, right_channel)

                # Export based on format
                export_params = {}
                if output_format == "mp3":
                    export_params["bitrate"] = quality
                elif output_format == "ogg":
                    export_params["bitrate"] = quality

                stereo_audio.export(output_path, format=output_format, **export_params)

                self.logger.info(f"  Converted: {output_filename}")
                converted += 1

                # Delete originals if requested
                if delete_originals:
                    for channel_file in [files['l'], files['ls'], files['r'], files['rs']]:
                        try:
                            os.remove(channel_file)
                            self.logger.info(f"  Deleted: {os.path.basename(channel_file)}")
                        except Exception as e:
                            self.logger.warning(f"  Failed to delete {channel_file}: {e}")

            except Exception as e:
                self.logger.error(f"  Error processing {base_name}: {e}")
                errors += 1

        # Summary
        self.logger.info(f"Conversion complete!")
        self.logger.info(f"Converted: {converted}")
        self.logger.info(f"Skipped: {skipped}")
        self.logger.info(f"Errors: {errors}")

        messagebox.showinfo("Conversion Complete",
                            f"Conversion finished!\n\n"
                            f"Converted: {converted}\n"
                            f"Skipped: {skipped}\n"
                            f"Errors: {errors}")

        self.status_label.config(
            text=f"Complete: {converted} converted, {skipped} skipped, {errors} errors",
            foreground="green" if errors == 0 else "orange"
        )
