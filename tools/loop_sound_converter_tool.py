"""
Loop Sound Converter Tool - Convert looping sounds with proper crossfading.
"""

import os
import re
import logging
import warnings
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

from .base_tool import BaseTool, register_tool
from .utils import save_config

# Try to import pydub for audio processing
try:
    from pydub import AudioSegment
    PYDUB_AVAILABLE = True
    # Suppress pydub RuntimeWarnings about ffmpeg/ffprobe
    warnings.filterwarnings("ignore", category=RuntimeWarning)
except ImportError:
    PYDUB_AVAILABLE = False


class TextHandler(logging.Handler):
    """Logging handler that writes to a Tkinter Text widget."""
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record):
        msg = self.format(record)
        self.text_widget.configure(state='normal')
        self.text_widget.insert(tk.END, msg + '\n')
        self.text_widget.configure(state='disabled')
        self.text_widget.yview(tk.END)


def find_loop_files(root):
    """Find all files with '_lp' before '.mp3'."""
    if not PYDUB_AVAILABLE:
        return []
        
    pattern = re.compile(r'_lp.*\.mp3$', re.IGNORECASE)
    results = []
    for dirpath, _, files in os.walk(root):
        for fn in files:
            if fn.lower().endswith('.mp3') and pattern.search(fn):
                results.append(os.path.join(dirpath, fn))
    return results


def process_loop_sound(path, output_root=None, crossfade_ms=1000, logger=None):
    """
    Process a single loop sound file, converting it to WAV with proper loop point handling.
    
    Args:
        path: Path to the loop MP3 file
        output_root: Optional output directory root
        crossfade_ms: Crossfade duration in milliseconds
        logger: Optional logger for output messages
    
    Returns:
        Path to the created output file, or None if failed
    """
    if not PYDUB_AVAILABLE:
        if logger:
            logger.error("pydub library is required for sound processing")
        return None
        
    # Determine output path
    out_dir = output_root if output_root else os.path.dirname(path)
    os.makedirs(out_dir, exist_ok=True)
    
    base = os.path.splitext(os.path.basename(path))[0]
    out_path = os.path.join(out_dir, base + '.wav')
    
    # Skip if already converted
    if os.path.exists(out_path):
        if logger:
            logger.info("Skipping already-converted: %s", out_path)
        return out_path
    
    try:
        audio = AudioSegment.from_file(path)
        if audio.channels == 2:
            # Process stereo file
            left, right = audio.split_to_mono()
            l_loop = left.append(left, crossfade=crossfade_ms)
            r_loop = right.append(right, crossfade=crossfade_ms)
            # Pad to same length
            max_len = max(len(l_loop), len(r_loop))
            l_loop += AudioSegment.silent(duration=max_len - len(l_loop))
            r_loop += AudioSegment.silent(duration=max_len - len(r_loop))
            looped = AudioSegment.from_mono_audiosegments(l_loop, r_loop)
        else:
            # Process mono file
            looped = audio.append(audio, crossfade=crossfade_ms)
        
        # Export to WAV
        looped.export(out_path, format='wav')
        if logger:
            logger.info("Exported looped WAV: %s", out_path)
        
        return out_path
        
    except Exception as e:
        if logger:
            logger.exception("Error processing %s: %s", path, e)
        return None


class LoopSoundConverterTab(ttk.Frame):
    def __init__(self, parent, config):
        super().__init__(parent)
        self.config = config
        
        # Root directory selection
        ttk.Label(self, text="Root directory:").grid(row=0, column=0, sticky="e", padx=5, pady=5)
        self.root_var = tk.StringVar(value=config.get("loop_sound_root", ""))
        ttk.Entry(self, textvariable=self.root_var, width=50).grid(row=0, column=1, padx=5, pady=5, sticky="ew")
        ttk.Button(self, text="Browse...", command=self.browse_root).grid(row=0, column=2, padx=5, pady=5)
        
        # Output directory
        ttk.Label(self, text="Output directory:").grid(row=1, column=0, sticky="e", padx=5, pady=5)
        self.output_var = tk.StringVar(value=config.get("loop_sound_output", ""))
        ttk.Entry(self, textvariable=self.output_var, width=50).grid(row=1, column=1, padx=5, pady=5, sticky="ew")
        ttk.Button(self, text="Browse...", command=self.browse_output).grid(row=1, column=2, padx=5, pady=5)
        
        # Crossfade setting
        ttk.Label(self, text="Crossfade (ms):").grid(row=2, column=0, sticky="e", padx=5, pady=5)
        self.crossfade_var = tk.StringVar(value=config.get("loop_sound_crossfade", "1000"))
        ttk.Entry(self, textvariable=self.crossfade_var, width=10).grid(row=2, column=1, sticky="w", padx=5, pady=5)
        
        # Delete originals checkbox
        self.delete_var = tk.BooleanVar(value=config.get("loop_sound_delete", False))
        ttk.Checkbutton(self, text="Delete original MP3 files after conversion", variable=self.delete_var).grid(
            row=3, column=0, columnspan=3, padx=5, pady=5, sticky="w")
        
        # Convert button
        ttk.Button(self, text="Convert Loop Sounds", command=self.on_convert).grid(
            row=4, column=0, columnspan=3, padx=5, pady=10)
        
        # Log area
        ttk.Label(self, text="Log:").grid(row=5, column=0, sticky="w", padx=5)
        self.log_text = ScrolledText(self, height=10, width=70)
        self.log_text.grid(row=6, column=0, columnspan=3, padx=5, pady=5, sticky="nsew")
        
        # Configure logging
        self.logger = logging.getLogger("loop_converter")
        self.logger.setLevel(logging.INFO)
        # Clear existing handlers to avoid duplication
        self.logger.handlers.clear()
        handler = TextHandler(self.log_text)
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', '%H:%M:%S'))
        self.logger.addHandler(handler)
        
        # Configure grid weights
        self.columnconfigure(1, weight=1)
        self.rowconfigure(6, weight=1)
        
        # Check for pydub availability
        if not PYDUB_AVAILABLE:
            self.logger.error("pydub library not found! Please install it with: pip install pydub")
    
    def browse_root(self):
        p = filedialog.askdirectory()
        if p:
            self.root_var.set(p)
    
    def browse_output(self):
        p = filedialog.askdirectory()
        if p:
            self.output_var.set(p)
    
    def on_convert(self):
        if not PYDUB_AVAILABLE:
            messagebox.showerror("Missing Dependency", 
                                "The pydub library is required for sound processing.\n"
                                "Please install it with: pip install pydub")
            return
            
        root_dir = self.root_var.get()
        output_dir = self.output_var.get()
        
        # If output dir is empty, use in-place conversion
        if not output_dir:
            output_dir = None
            
        try:
            crossfade = int(self.crossfade_var.get())
        except ValueError:
            self.logger.error("Invalid crossfade value, using default 1000ms")
            crossfade = 1000
        
        # Save settings to config
        self.config["loop_sound_root"] = root_dir
        self.config["loop_sound_output"] = output_dir or ""
        self.config["loop_sound_crossfade"] = crossfade
        self.config["loop_sound_delete"] = self.delete_var.get()
        save_config(self.config)
        
        if not root_dir:
            messagebox.showerror("Error", "Please select a root directory.")
            return
            
        self.logger.info(f"Searching for loop files in {root_dir}...")
        loop_files = find_loop_files(root_dir)
        
        if not loop_files:
            self.logger.info("No loop files found.")
            return
            
        self.logger.info(f"Found {len(loop_files)} loop files. Processing...")
        
        count = 0
        for file_path in loop_files:
            rel_dir = os.path.relpath(os.path.dirname(file_path), root_dir)
            out_dir = os.path.join(output_dir, rel_dir) if output_dir else None
            
            if process_loop_sound(file_path, out_dir, crossfade, self.logger):
                count += 1
                
                # Delete original if requested and using separate output directory
                if self.delete_var.get() and output_dir and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        self.logger.info(f"Deleted original: {os.path.basename(file_path)}")
                    except Exception as e:
                        self.logger.error(f"Failed to delete original: {str(e)}")
        
        self.logger.info(f"Conversion complete. Processed {count} of {len(loop_files)} files.")


@register_tool
class LoopSoundConverterTool(BaseTool):
    @property
    def name(self) -> str:
        return "Loop Sound Converter"
    
    @property
    def description(self) -> str:
        return "Convert looping sounds with proper crossfading"
    
    @property
    def dependencies(self) -> list:
        return ["pydub"]
    
    def create_tab(self, parent) -> ttk.Frame:
        return LoopSoundConverterTab(parent, self.config)
