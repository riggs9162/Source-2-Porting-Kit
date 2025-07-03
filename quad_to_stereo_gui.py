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

# --- Logging handler for Tkinter Text widget ---
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

# --- Find all <base>_(l|ls|r|rs).mp3 groups under root ---
def find_groups(root):
    pattern = re.compile(r'(.+?)_(l|ls|r|rs)\.mp3$', re.IGNORECASE)
    groups = {}
    for dirpath, _, files in os.walk(root):
        for fn in files:
            m = pattern.match(fn)
            if not m:
                continue
            base, suffix = m.group(1), m.group(2).lower()
            key = (dirpath, base)
            groups.setdefault(key, {})[suffix] = os.path.join(dirpath, fn)
    return groups

# --- Mix and export one group; optionally delete sources if writing in-place ---
def process_group(root, dirpath, base, files, output_root, logger):
    required = ['l', 'ls', 'r', 'rs']
    missing = [k for k in required if k not in files]
    if missing:
        logger.info("Skipping %s: missing %s", base, missing)
        return

    try:
        # prepare left channel
        la = AudioSegment.from_file(files['l'])
        lb = AudioSegment.from_file(files['ls'])
        ml = max(len(la), len(lb))
        la += AudioSegment.silent(duration=ml - len(la))
        lb += AudioSegment.silent(duration=ml - len(lb))
        left = la.overlay(lb)

        # prepare right channel
        ra = AudioSegment.from_file(files['r'])
        rb = AudioSegment.from_file(files['rs'])
        mr = max(len(ra), len(rb))
        ra += AudioSegment.silent(duration=mr - len(ra))
        rb += AudioSegment.silent(duration=mr - len(rb))
        right = ra.overlay(rb)

        # pad both channels to same length
        final_len = max(len(left), len(right))
        left += AudioSegment.silent(duration=final_len - len(left))
        right += AudioSegment.silent(duration=final_len - len(right))

        # combine to stereo
        stereo = AudioSegment.from_mono_audiosegments(left, right)

        # determine output folder
        if output_root:
            rel = os.path.relpath(dirpath, root)
            out_dir = os.path.join(output_root, rel)
            os.makedirs(out_dir, exist_ok=True)
        else:
            out_dir = dirpath

        out_path = os.path.join(out_dir, f"{base}.mp3")
        stereo.export(out_path, format="mp3")
        logger.info("Exported: %s", out_path)

        # if writing in-place (out_dir == dirpath), delete the four source files
        if os.path.abspath(out_dir) == os.path.abspath(dirpath):
            for suffix in required:
                src = files[suffix]
                try:
                    os.remove(src)
                    logger.info("Deleted source file: %s", src)
                except Exception as e:
                    logger.exception("Failed to delete %s: %s", src, e)

    except Exception as e:
        logger.exception("Error processing %s: %s", base, e)

# --- Tkinter GUI ---
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MP3 Quad→Stereo Combiner")
        self.geometry("700x450")

        # Root directory input
        tk.Label(self, text="Root directory:").grid(row=0, column=0, sticky='e', padx=5, pady=5)
        self.root_var = tk.StringVar()
        tk.Entry(self, textvariable=self.root_var, width=50).grid(row=0, column=1, padx=5)
        tk.Button(self, text="Browse...", command=self.browse_root).grid(row=0, column=2, padx=5)

        # Output directory input (optional)
        tk.Label(self, text="Output directory:").grid(row=1, column=0, sticky='e', padx=5, pady=5)
        self.output_var = tk.StringVar()
        tk.Entry(self, textvariable=self.output_var, width=50).grid(row=1, column=1, padx=5)
        tk.Button(self, text="Browse...", command=self.browse_output).grid(row=1, column=2, padx=5)

        # Start button
        tk.Button(self, text="Start Processing", command=self.run_processing).grid(
            row=2, column=1, pady=10)

        # Log display
        self.log_widget = ScrolledText(self, state='disabled', wrap='word', height=15)
        self.log_widget.grid(row=3, column=0, columnspan=3, padx=5, pady=5, sticky='nsew')
        self.grid_rowconfigure(3, weight=1)
        self.grid_columnconfigure(1, weight=1)

        self.setup_logging()

    def setup_logging(self):
        self.logger = logging.getLogger('Combiner')
        self.logger.setLevel(logging.INFO)

        fh = logging.FileHandler('combine.log', encoding='utf-8')
        fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
        self.logger.addHandler(fh)

        th = TextHandler(self.log_widget)
        th.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
        self.logger.addHandler(th)

    def browse_root(self):
        path = filedialog.askdirectory(title="Select root directory")
        if path:
            self.root_var.set(path)

    def browse_output(self):
        path = filedialog.askdirectory(title="Select output directory")
        if path:
            self.output_var.set(path)

    def run_processing(self):
        raw_root = self.root_var.get().strip()
        if not raw_root:
            messagebox.showerror("Error", "Please select a root directory.")
            return

        root = os.path.abspath(os.path.normpath(raw_root))
        if not os.path.isdir(root):
            self.logger.error("Root not found: %s", root)
            messagebox.showerror("Error", f"Directory not found:\n{root}")
            return

        # ensure ffmpeg is available
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

        self.logger.info("Scanning %s for MP3 groups...", root)
        groups = find_groups(root)
        total = len(groups)
        self.logger.info("Found %d potential group(s).", total)
        if total == 0:
            messagebox.showwarning("No groups found", f"No matching sets found under:\n{root}")
            return

        for (dirpath, base), files in groups.items():
            process_group(root, dirpath, base, files, output, self.logger)

        self.logger.info("Processing complete.")
        messagebox.showinfo("Finished", "All done. See combine.log for details.")

if __name__ == "__main__":
    App().mainloop()
