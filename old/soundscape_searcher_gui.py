import os
import re
import json
import logging
import tkinter as tk
from tkinter import filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

# ─── Logger setup ─────────────────────────────────────────────────────────────
logger = logging.getLogger('SoundscapeSearcher')
logger.setLevel(logging.INFO)
fh = logging.FileHandler('search.log', encoding='utf-8')
fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
logger.addHandler(fh)

# ─── Text widget log handler ──────────────────────────────────────────────────
class TextHandler(logging.Handler):
    def __init__(self, widget):
        super().__init__()
        self.widget = widget
    def emit(self, record):
        msg = self.format(record)
        self.widget.configure(state='normal')
        self.widget.insert(tk.END, msg + '\n')
        self.widget.configure(state='disabled')
        self.widget.yview(tk.END)

# ─── Block extraction ──────────────────────────────────────────────────────────
def extract_block(text, start):
    depth = 0
    for i, ch in enumerate(text[start:], start=start):
        if ch == '{': depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[start:i+1]
    return ''

def extract_bracket_block(text, start):
    depth = 0
    for i, ch in enumerate(text[start:], start=start):
        if ch == '[': depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0:
                return text[start:i+1]
    return ''

# ─── Parse soundscape for its playevent names ────────────────────────────────
def parse_soundscape_block(name, filepath):
    data = open(filepath, 'r', encoding='utf-8', errors='ignore').read()
    m = re.search(rf'"{re.escape(name)}"', data)
    if not m:
        raise ValueError(f"Soundscape '{name}' not found in {filepath}")
    brace = data.find('{', m.end())
    if brace < 0:
        raise ValueError(f"No '{{' after '{name}' in {filepath}")
    block = extract_block(data, brace)
    events = re.findall(r'"event"\s*"([^"]+)"', block)
    return list(dict.fromkeys(events))

# ─── Scan & merge across all .vsndevts, honoring base= aliases ──────────────
def find_event_definition(event, search_dir, visited=None):
    if visited is None:
        visited = set()
    if event in visited:
        return None
    visited.add(event)

    all_vsnd = []
    nested = {}
    randoms = {}
    files_seen = []

    # Walk every .vsndevts in the directory
    for root, _, files in os.walk(search_dir):
        for fn in files:
            if not fn.lower().endswith('.vsndevts'):
                continue
            path = os.path.join(root, fn)
            try:
                text = open(path, 'r', encoding='utf-8', errors='ignore').read()
            except:
                continue

            # Does this file contain a definition for our event?
            if not re.search(rf'\b{re.escape(event)}\s*=', text):
                continue

            files_seen.append(path)
            logger.info(f"Found stub or full def of '{event}' in {path}")

            # Extract the {...} block for this event
            brace = text.find('{', text.find(event))
            block = extract_block(text, brace)

            # 1) direct vsnd_file_NN
            all_vsnd += re.findall(r'vsnd_file_\d+\s*=\s*"([^"]+\.vsnd)"', block)

            # 2) array-style vsnd_files = [ ... ]
            arr = re.search(r'vsnd_files\s*=', block, flags=re.IGNORECASE)
            if arr:
                idx = block.find('[', arr.end())
                if idx >= 0:
                    arr_blk = extract_bracket_block(block, idx)
                    all_vsnd += re.findall(r'"([^"]+\.vsnd)"', arr_blk)

            # 3) nested soundevent_NN
            for se in re.findall(r'soundevent_\d+\s*=\s*"([^"]+)"', block):
                sub = find_event_definition(se, search_dir, visited)
                if sub:
                    nested[se] = sub

            # 4) random_soundevent_NN_name
            for rn in re.findall(r'random_soundevent_\d+_name\s*=\s*"([^"]+)"', block):
                rsub = find_event_definition(rn, search_dir, visited)
                if rsub:
                    randoms[rn] = rsub

            # 5) base = "OtherEventName" alias support
            for b in re.findall(r'base\s*=\s*"([^"]+)"', block):
                bdef = find_event_definition(b, search_dir, visited)
                if bdef:
                    nested[b] = bdef

    # Dedupe
    vsnd_files = list(dict.fromkeys(all_vsnd))

    # Compute total_volume across all seen files
    vols = []
    for path in files_seen:
        t = open(path, 'r', encoding='utf-8', errors='ignore').read()
        vols += [float(v) for v in re.findall(r'vsnd_vol_\d+\s*=\s*"?([-\d\.]+)"?', t)]
        mv = re.search(r'volume\s*=\s*([-\d\.]+)', t)
        if not vols and mv:
            vols = [float(mv.group(1))]
    total_volume = sum(vols) if vols else 0.0

    return {
        'files': files_seen,
        'vsnd_files': vsnd_files,
        'nested': nested,
        'random': randoms,
        'total_volume': total_volume
    }

# ─── GUI Application ─────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Soundscape → JSON Exporter")
        self.geometry("720x600")

        # Soundscapes .txt
        tk.Label(self, text="Soundscapes file:").grid(row=0, column=0, sticky='e')
        self.sc_var = tk.StringVar()
        tk.Entry(self, textvariable=self.sc_var, width=60).grid(row=0, column=1, padx=5)
        tk.Button(self, text="Browse…", command=self.browse_sc).grid(row=0, column=2)

        # Soundevents folder
        tk.Label(self, text="Soundevents dir:").grid(row=1, column=0, sticky='e')
        self.se_var = tk.StringVar()
        tk.Entry(self, textvariable=self.se_var, width=60).grid(row=1, column=1, padx=5)
        tk.Button(self, text="Browse…", command=self.browse_se).grid(row=1, column=2)

        # Soundscape name
        tk.Label(self, text="Soundscape name:").grid(row=2, column=0, sticky='e')
        self.name_var = tk.StringVar()
        tk.Entry(self, textvariable=self.name_var, width=30).grid(row=2, column=1, sticky='w')
        tk.Button(self, text="Search & Export JSON", command=self.run).grid(row=2, column=2)

        # Log output
        self.log = ScrolledText(self, wrap='word')
        self.log.grid(row=3, column=0, columnspan=3, sticky='nsew', padx=5, pady=5)
        self.grid_rowconfigure(3, weight=1)
        self.grid_columnconfigure(1, weight=1)

        th = TextHandler(self.log)
        th.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
        logger.addHandler(th)

    def browse_sc(self):
        p = filedialog.askopenfilename(title="Select soundscapes .txt", filetypes=[("Text","*.txt")])
        if p: self.sc_var.set(p)

    def browse_se(self):
        p = filedialog.askdirectory(title="Select soundevents dir")
        if p: self.se_var.set(p)

    def print_def(self, name, d, indent=0):
        pad = '  ' * indent
        self.log.insert(tk.END, f"{pad}Event: {name}\n")
        self.log.insert(tk.END, f"{pad}  Found in files:\n")
        for f in d['files']:
            self.log.insert(tk.END, f"{pad}    {f}\n")
        self.log.insert(tk.END, f"{pad}  vsnd_files:\n")
        for v in d['vsnd_files']:
            self.log.insert(tk.END, f"{pad}    {v}\n")
        self.log.insert(tk.END, f"{pad}  total_volume: {d['total_volume']}\n")
        if d['random']:
            self.log.insert(tk.END, f"{pad}  random:\n")
            for rn, rd in d['random'].items():
                self.print_def(rn, rd, indent+1)
        if d['nested']:
            self.log.insert(tk.END, f"{pad}  nested:\n")
            for nn, nd in d['nested'].items():
                self.print_def(nn, nd, indent+1)

    def run(self):
        self.log.delete('1.0', tk.END)
        sc = self.sc_var.get().strip()
        se = self.se_var.get().strip()
        nm = self.name_var.get().strip()
        if not (sc and se and nm):
            messagebox.showerror("Error", "Please fill in all three fields.")
            return

        try:
            events = parse_soundscape_block(nm, sc)
            logger.info(f"Found {len(events)} events in '{nm}'")
            self.log.insert(tk.END, f"Found {len(events)} events:\n\n")

            results = {}
            for ev in events:
                d = find_event_definition(ev, se)
                results[ev] = d
                self.print_def(ev, d)
                self.log.insert(tk.END, "\n")

            save = filedialog.asksaveasfilename(defaultextension=".json",
                                                filetypes=[("JSON","*.json")])
            if save:
                with open(save, 'w', encoding='utf-8') as f:
                    json.dump({'soundscape': nm, 'events': results}, f, indent=2)
                messagebox.showinfo("Done", f"Exported JSON to:\n{save}")
            else:
                self.log.insert(tk.END, "Export canceled.\n")

        except Exception as e:
            logger.exception("Error during run")
            messagebox.showerror("Error", str(e))

if __name__ == '__main__':
    App().mainloop()
