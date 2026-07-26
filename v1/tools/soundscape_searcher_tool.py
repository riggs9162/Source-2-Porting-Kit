"""
Soundscape Searcher Tool - Search and analyze soundscape files for sound references.
"""

import os
import re
import json
import logging
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from .base_tool import BaseTool, register_tool
from .utils import PlaceholderEntry, browse_folder, browse_folder_with_context

@register_tool
class SoundscapeSearcherTool(BaseTool):
    @property
    def name(self) -> str:
        return "Soundscape Searcher"
    
    @property
    def description(self) -> str:
        return "Search and analyze soundscape files for sound references and extract sound blocks"
    
    @property
    def dependencies(self) -> list:
        return []  # No external dependencies
    
    def create_tab(self, parent) -> ttk.Frame:
        return SoundscapeSearcherTab(parent, self.config)

class TextHandler(logging.Handler):
    """Logging handler for Tkinter Text widget."""
    def __init__(self, widget):
        super().__init__()
        self.widget = widget
        
    def emit(self, record):
        msg = self.format(record)
        self.widget.configure(state='normal')
        self.widget.insert(tk.END, msg + '\n')
        self.widget.configure(state='disabled')
        self.widget.see(tk.END)

class SoundscapeSearcherTab(ttk.Frame):
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
        input_frame = ttk.LabelFrame(main_frame, text="Search Parameters", padding=10)
        input_frame.pack(fill="x", pady=(0, 10))
        
        # Root folder
        ttk.Label(input_frame, text="Root Folder:").grid(row=0, column=0, sticky="w", pady=2)
        self.root_folder = PlaceholderEntry(input_frame, placeholder="Select folder to search in...")
        self.root_folder.grid(row=0, column=1, sticky="ew", padx=(5, 0), pady=2)
        ttk.Button(input_frame, text="Browse", 
                  command=self.browse_root_folder).grid(row=0, column=2, padx=(5, 0), pady=2)
        
        # Search term
        ttk.Label(input_frame, text="Search Term:").grid(row=1, column=0, sticky="w", pady=2)
        self.search_term = tk.StringVar()
        search_entry = ttk.Entry(input_frame, textvariable=self.search_term, width=30)
        search_entry.grid(row=1, column=1, sticky="ew", padx=(5, 0), pady=2)
        
        # File extensions
        ttk.Label(input_frame, text="File Extensions:").grid(row=2, column=0, sticky="w", pady=2)
        self.file_extensions = tk.StringVar(value="*.txt, *.cfg, *.res")
        ext_entry = ttk.Entry(input_frame, textvariable=self.file_extensions, width=30)
        ext_entry.grid(row=2, column=1, sticky="ew", padx=(5, 0), pady=2)
        
        input_frame.columnconfigure(1, weight=1)
        
        # Options section
        options_frame = ttk.LabelFrame(main_frame, text="Search Options", padding=10)
        options_frame.pack(fill="x", pady=(0, 10))
        
        options_row1 = ttk.Frame(options_frame)
        options_row1.pack(fill="x", pady=(0, 5))
        
        self.case_sensitive_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(options_row1, text="Case sensitive", 
                       variable=self.case_sensitive_var).pack(side="left")
        
        self.regex_search_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(options_row1, text="Use regex", 
                       variable=self.regex_search_var).pack(side="left", padx=(10, 0))
        
        self.whole_words_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(options_row1, text="Whole words only", 
                       variable=self.whole_words_var).pack(side="left", padx=(10, 0))
        
        options_row2 = ttk.Frame(options_frame)
        options_row2.pack(fill="x")
        
        self.extract_blocks_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options_row2, text="Extract sound blocks", 
                       variable=self.extract_blocks_var).pack(side="left")
        
        self.save_results_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options_row2, text="Save results to file", 
                       variable=self.save_results_var).pack(side="left", padx=(10, 0))
        
        # Action buttons
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill="x", pady=(0, 10))
        
        ttk.Button(button_frame, text="Search Files", 
                  command=self.search_files).pack(side="left")
        ttk.Button(button_frame, text="Extract Soundscape Blocks", 
                  command=self.extract_soundscape_blocks).pack(side="left", padx=(10, 0))
        ttk.Button(button_frame, text="Clear Log", 
                  command=self.clear_log).pack(side="right")
        
        # Results section
        results_frame = ttk.LabelFrame(main_frame, text="Results", padding=10)
        results_frame.pack(fill="both", expand=True)
        
        self.results_text = ScrolledText(results_frame, height=15, width=70, state='disabled')
        self.results_text.pack(fill="both", expand=True)
        
        # Status label
        self.status_label = ttk.Label(main_frame, text="Ready", foreground="green")
        self.status_label.pack(pady=(10, 0))
    
    def setup_logging(self):
        """Set up logging to the text widget."""
        self.logger = logging.getLogger('SoundscapeSearcher')
        self.logger.setLevel(logging.INFO)
        
        # Clear any existing handlers
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)
        
        # Add text widget handler
        text_handler = TextHandler(self.results_text)
        text_handler.setFormatter(logging.Formatter('%(asctime)s - %(message)s', '%H:%M:%S'))
        self.logger.addHandler(text_handler)
    
    def browse_root_folder(self):
        """Browse for root folder."""
        path = browse_folder_with_context(self.root_folder, context_key="soundscape_searcher_root_folder",
                                        title="Select folder to search in")
    
    def clear_log(self):
        """Clear the log text."""
        self.results_text.configure(state='normal')
        self.results_text.delete("1.0", "end")
        self.results_text.configure(state='disabled')
    
    def get_file_extensions(self):
        """Parse file extensions from the input field."""
        ext_text = self.file_extensions.get().strip()
        if not ext_text:
            return ["*.txt"]  # Default
        
        extensions = []
        for ext in ext_text.split(','):
            ext = ext.strip()
            if ext:
                if not ext.startswith('*.'):
                    ext = '*.' + ext.lstrip('.')
                extensions.append(ext.lower())
        
        return extensions
    
    def find_files(self, root_path, extensions):
        """Find files matching the specified extensions."""
        matching_files = []
        
        if not os.path.exists(root_path):
            return matching_files
        
        for root, dirs, files in os.walk(root_path):
            for file in files:
                file_path = os.path.join(root, file)
                
                # Check if file matches any extension
                for ext in extensions:
                    ext_pattern = ext[1:]  # Remove *
                    if file.lower().endswith(ext_pattern):
                        matching_files.append(file_path)
                        break
        
        return matching_files
    
    def search_in_file(self, file_path, search_term, case_sensitive, regex_search, whole_words):
        """Search for term in a file and return matches."""
        matches = []
        
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                for line_num, line in enumerate(f, 1):
                    if self.line_matches(line, search_term, case_sensitive, regex_search, whole_words):
                        matches.append((line_num, line.strip()))
        
        except Exception as e:
            self.logger.error(f"Error reading {file_path}: {e}")
        
        return matches
    
    def line_matches(self, line, search_term, case_sensitive, regex_search, whole_words):
        """Check if a line matches the search criteria."""
        if regex_search:
            try:
                flags = 0 if case_sensitive else re.IGNORECASE
                return bool(re.search(search_term, line, flags))
            except re.error:
                return False
        else:
            search_target = line if case_sensitive else line.lower()
            search_pattern = search_term if case_sensitive else search_term.lower()
            
            if whole_words:
                pattern = r'\\b' + re.escape(search_pattern) + r'\\b'
                flags = 0 if case_sensitive else re.IGNORECASE
                return bool(re.search(pattern, search_target, flags))
            else:
                return search_pattern in search_target
    
    def search_files(self):
        """Search for the specified term in files."""
        root_folder = self.root_folder.get()
        search_term = self.search_term.get().strip()
        
        if not root_folder:
            messagebox.showerror("Error", "Please select a root folder first.")
            return
        
        if not search_term:
            messagebox.showerror("Error", "Please enter a search term.")
            return
        
        extensions = self.get_file_extensions()
        case_sensitive = self.case_sensitive_var.get()
        regex_search = self.regex_search_var.get()
        whole_words = self.whole_words_var.get()
        
        self.logger.info(f"Searching for '{search_term}' in {root_folder}")
        self.logger.info(f"File extensions: {', '.join(extensions)}")
        self.logger.info(f"Options: Case sensitive={case_sensitive}, Regex={regex_search}, Whole words={whole_words}")
        
        # Find matching files
        matching_files = self.find_files(root_folder, extensions)
        
        if not matching_files:
            self.logger.info("No files found matching the specified extensions.")
            self.status_label.config(text="No files found", foreground="orange")
            return
        
        self.logger.info(f"Scanning {len(matching_files)} files...")
        
        total_matches = 0
        files_with_matches = 0
        search_results = {}
        
        for file_path in matching_files:
            matches = self.search_in_file(file_path, search_term, case_sensitive, regex_search, whole_words)
            
            if matches:
                files_with_matches += 1
                total_matches += len(matches)
                search_results[file_path] = matches
                
                rel_path = os.path.relpath(file_path, root_folder)
                self.logger.info(f"\\n{rel_path} ({len(matches)} matches):")
                
                for line_num, line_content in matches[:5]:  # Show first 5 matches per file
                    self.logger.info(f"  Line {line_num}: {line_content[:100]}{'...' if len(line_content) > 100 else ''}")
                
                if len(matches) > 5:
                    self.logger.info(f"  ... and {len(matches) - 5} more matches")
        
        self.logger.info(f"\\nSearch complete!")
        self.logger.info(f"Found {total_matches} matches in {files_with_matches} files.")
        
        # Save results if requested
        if self.save_results_var.get() and search_results:
            self.save_search_results(search_results, search_term, root_folder)
        
        self.status_label.config(text=f"Search complete: {total_matches} matches in {files_with_matches} files", 
                                foreground="green")
    
    def save_search_results(self, results, search_term, root_folder):
        """Save search results to a file."""
        try:
            timestamp = __import__('datetime').datetime.now().strftime("%Y%m%d_%H%M%S")
            output_filename = f"search_results_{search_term.replace(' ', '_')}_{timestamp}.json"
            output_path = os.path.join(root_folder, output_filename)
            
            # Prepare data for JSON
            json_data = {
                "search_term": search_term,
                "root_folder": root_folder,
                "timestamp": timestamp,
                "total_matches": sum(len(matches) for matches in results.values()),
                "files_with_matches": len(results),
                "results": {}
            }
            
            for file_path, matches in results.items():
                rel_path = os.path.relpath(file_path, root_folder)
                json_data["results"][rel_path] = {
                    "match_count": len(matches),
                    "matches": [{"line": line_num, "content": content} for line_num, content in matches]
                }
            
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, indent=2, ensure_ascii=False)
            
            self.logger.info(f"Results saved to: {output_filename}")
            
        except Exception as e:
            self.logger.error(f"Failed to save results: {e}")
    
    def extract_block(self, text, start_pos):
        """Extract a block from text starting at a position."""
        depth = 0
        i = start_pos
        
        # Find the opening brace
        while i < len(text) and text[i] != '{':
            i += 1
        
        if i >= len(text):
            return None, i
        
        block_start = i
        i += 1
        depth = 1
        
        # Find the matching closing brace
        while i < len(text) and depth > 0:
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
            i += 1
        
        if depth == 0:
            return text[block_start:i], i
        else:
            return None, i
    
    def extract_soundscape_blocks(self):
        """Extract soundscape blocks from files."""
        root_folder = self.root_folder.get()
        
        if not root_folder:
            messagebox.showerror("Error", "Please select a root folder first.")
            return
        
        # Look for soundscape files
        soundscape_extensions = ["*.txt"]
        soundscape_files = self.find_files(root_folder, soundscape_extensions)
        
        # Filter for likely soundscape files
        soundscape_files = [f for f in soundscape_files if 
                           any(keyword in os.path.basename(f).lower() 
                               for keyword in ['soundscape', 'sound', 'ambient'])]
        
        if not soundscape_files:
            self.logger.info("No soundscape files found. Searching all text files...")
            soundscape_files = self.find_files(root_folder, ["*.txt"])
        
        if not soundscape_files:
            messagebox.showinfo("No Files", "No text files found to analyze.")
            return
        
        self.logger.info(f"Analyzing {len(soundscape_files)} files for soundscape blocks...")
        
        total_blocks = 0
        output_data = {}
        
        for file_path in soundscape_files:
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                
                rel_path = os.path.relpath(file_path, root_folder)
                file_blocks = []
                
                # Look for soundscape block patterns
                # Common patterns: "soundscape_name" { ... }
                pattern = r'(["\']?\\w+["\']?)\\s*{'
                matches = list(re.finditer(pattern, content, re.MULTILINE))
                
                for match in matches:
                    block_name = match.group(1).strip('\'"')
                    block_content, end_pos = self.extract_block(content, match.start())
                    
                    if block_content:
                        file_blocks.append({
                            "name": block_name,
                            "content": block_content,
                            "start_pos": match.start(),
                            "end_pos": end_pos
                        })
                        total_blocks += 1
                
                if file_blocks:
                    output_data[rel_path] = file_blocks
                    self.logger.info(f"{rel_path}: Found {len(file_blocks)} blocks")
                
            except Exception as e:
                self.logger.error(f"Error processing {file_path}: {e}")
        
        self.logger.info(f"\\nExtraction complete! Found {total_blocks} soundscape blocks.")
        
        # Save extracted blocks if requested
        if self.save_results_var.get() and output_data:
            try:
                timestamp = __import__('datetime').datetime.now().strftime("%Y%m%d_%H%M%S")
                output_filename = f"soundscape_blocks_{timestamp}.json"
                output_path = os.path.join(root_folder, output_filename)
                
                json_output = {
                    "extraction_timestamp": timestamp,
                    "root_folder": root_folder,
                    "total_blocks": total_blocks,
                    "files_processed": len(soundscape_files),
                    "blocks": output_data
                }
                
                with open(output_path, 'w', encoding='utf-8') as f:
                    json.dump(json_output, f, indent=2, ensure_ascii=False)
                
                self.logger.info(f"Blocks saved to: {output_filename}")
                
            except Exception as e:
                self.logger.error(f"Failed to save blocks: {e}")
        
        self.status_label.config(text=f"Extraction complete: {total_blocks} blocks found", 
                                foreground="green")
