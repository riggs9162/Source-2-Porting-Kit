import os
import re
import logging
import warnings
import shutil
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from pydub import AudioSegment

# suppress pydub RuntimeWarnings about ffmpeg/ffprobe
warnings.filterwarnings("ignore", category=RuntimeWarning)

# --- Logging handler that writes to a Tkinter Text widget ---
class TextHandler(logging.Handler):
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record):
        msg = self.format(record)
        self.text_widget.configure(state='normal')
        self.text_widget.insert(tk.END, msg + '\n')
        self.text_widget.configure(state='disabled')
        self.text_widget.yview(tk.END)

# --- Find all files with '_lp' before '.mp3' ---
def find_loop_files(root):
    pattern = re.compile(r'_lp.*\.mp3$', re.IGNORECASE)
    results = []
    for dirpath, _, files in os.walk(root):
        for fn in files:
            if fn.lower().endswith('.mp3') and pattern.search(fn):
                results.append(os.path.join(dirpath, fn))
    return results

# --- Process one loop file: convert to wav, loop start/end, preserve stereo ---
def process_loop_file(root, path, output_root, logger, crossfade_ms=1000):
    # relative output directory
    rel_dir = os.path.relpath(os.path.dirname(path), root)
    out_dir = (os.path.join(output_root, rel_dir)
               if output_root else os.path.dirname(path))
    os.makedirs(out_dir, exist_ok=True)

    base = os.path.splitext(os.path.basename(path))[0]
    out_path = os.path.join(out_dir, base + '.wav')

    # skip if already converted
    if os.path.exists(out_path):
        logger.info("Skipping already-converted: %s", out_path)
        return

    try:
        audio = AudioSegment.from_file(path)
        if audio.channels == 2:
            left, right = audio.split_to_mono()
            l_loop = left.append(left, crossfade=crossfade_ms)
            r_loop = right.append(right, crossfade=crossfade_ms)
            # pad to same length
            max_len = max(len(l_loop), len(r_loop))
            l_loop += AudioSegment.silent(duration=max_len - len(l_loop))
            r_loop += AudioSegment.silent(duration=max_len - len(r_loop))
            looped = AudioSegment.from_mono_audiosegments(l_loop, r_loop)
        else:
            looped = audio.append(audio, crossfade=crossfade_ms)

        looped.export(out_path, format='wav')
        logger.info("Exported looped WAV: %s", out_path)

        # if in-place conversion (output folder == original file folder), delete original mp3
        if os.path.abspath(out_dir) == os.path.abspath(os.path.dirname(path)):
            try:
                os.remove(path)
                logger.info("Deleted original MP3: %s", path)
            except Exception as e:
                logger.exception("Failed to delete original MP3 %s: %s", path, e)

    except Exception as e:
        logger.exception("Error processing %s: %s", path, e)

# --- Tkinter GUI Application ---
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Loop Sound Converter")
        self.geometry("700x450")

        # Root directory
        tk.Label(self, text="Root directory:").grid(row=0, column=0,
                                                    sticky='e', padx=5, pady=5)
        self.root_var = tk.StringVar()
        tk.Entry(self, textvariable=self.root_var, width=50).grid(row=0, column=1,
                                                                  padx=5)
        tk.Button(self, text="Browse...", command=self.browse_root).grid(
            row=0, column=2, padx=5)

        # Output directory (optional)
        tk.Label(self, text="Output directory:").grid(row=1, column=0,
                                                      sticky='e', padx=5, pady=5)
        self.output_var = tk.StringVar()
        tk.Entry(self, textvariable=self.output_var, width=50).grid(row=1,
                                                                    column=1,
                                                                    padx=5)
        tk.Button(self, text="Browse...", command=self.browse_output).grid(
            row=1, column=2, padx=5)

        # Start button
        tk.Button(self, text="Start Conversion", command=self.run).grid(
            row=2, column=1, pady=10)

        # Log display
        self.log_widget = ScrolledText(self, state='disabled', wrap='word',
                                       height=15)
        self.log_widget.grid(row=3, column=0, columnspan=3,
                             padx=5, pady=5, sticky='nsew')
        self.grid_rowconfigure(3, weight=1)
        self.grid_columnconfigure(1, weight=1)

        self.setup_logging()

    def setup_logging(self):
        self.logger = logging.getLogger('Looper')
        self.logger.setLevel(logging.INFO)

        fh = logging.FileHandler('loop_converter.log', encoding='utf-8')
        fh.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s: %(message)s'))
        self.logger.addHandler(fh)

        th = TextHandler(self.log_widget)
        th.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s: %(message)s'))
        self.logger.addHandler(th)

    def browse_root(self):
        path = filedialog.askdirectory(title="Select root directory")
        if path:
            self.root_var.set(path)

    def browse_output(self):
        path = filedialog.askdirectory(title="Select output directory")
        if path:
            self.output_var.set(path)

    def run(self):
        raw_root = self.root_var.get().strip()
        if not raw_root:
            messagebox.showerror("Error", "Please select a root directory.")
            return

        root = os.path.abspath(os.path.normpath(raw_root))
        if not os.path.isdir(root):
            self.logger.error("Root not found: %s", root)
            messagebox.showerror("Error", f"Directory not found:\n{root}")
            return

        if shutil.which("ffmpeg") is None:
            self.logger.error("ffmpeg not on PATH")
            messagebox.showerror("Error", "ffmpeg not found on PATH.")
            return

        raw_out = self.output_var.get().strip()
        output = None
        if raw_out:
            output = os.path.abspath(os.path.normpath(raw_out))
            try:
                os.makedirs(output, exist_ok=True)
            except Exception as e:
                self.logger.error("Cannot create output dir: %s", e)
                messagebox.showerror("Error", f"Cannot create output directory:\n{output}")
                return

        self.logger.info("Scanning for loop MP3s under: %s", root)
        files = find_loop_files(root)
        total = len(files)
        self.logger.info("Found %d loop file(s).", total)
        if total == 0:
            messagebox.showwarning("No loop files",
                                   f"No files with '_lp' found under:\n{root}")
            return

        for path in files:
            process_loop_file(root, path, output, self.logger)

        self.logger.info("Conversion complete.")
        messagebox.showinfo("Done",
                            "All loop files converted. See loop_converter.log for details.")

if __name__ == '__main__':
    App().mainloop()
