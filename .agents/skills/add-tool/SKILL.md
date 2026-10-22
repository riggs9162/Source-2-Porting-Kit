---
name: add-tool
description: How to add a new tool to the Source 2 Porting Kit — BaseTool subclass, QThread worker, sidebar registration, settings persistence. Use whenever creating a new tool or restructuring an existing one.
---

# Adding a tool to the porting kit

Every tool is a PySide6 widget registered in the sidebar. Follow the existing pattern exactly — `v2/app/tools/alpha_mask_tool.py` is the canonical small example.

## 1. Structure the code

- **Processing logic is Qt-free** and lives in `v2/app/utils/` (or a Qt-free module in `tools/`/`core/` like `ragdoll_physics.py` / `ragdoll_presets.py`). The tool file only wires UI to that logic. This keeps it testable and reusable from the CLIs.
- Tool file: `v2/app/tools/<snake_name>_tool.py` containing:
  - a **worker `QThread`** with `progress = Signal(str, str)` (message, level) and `finished = Signal(bool, ...)`; all file I/O and computation happens in `run()`, wrapped in try/except that emits `("...", "ERROR")` then `finished(False, ...)` — never raise out of the thread.
  - batch workers additionally declare `progress_value = Signal(int, int)` (done, total) and emit it per item — `start_worker` auto-connects it to the shared progress bar under the log pane. Workers without it get an indeterminate busy bar for free; never add a per-tool `QProgressBar`. Throttle emissions (~200 steps) when items are cheap (renames, text edits).
  - the tool class subclassing `app.tools.base_tool.BaseTool`, calling `super().__init__("Display Name")` and building its UI in the inherited **`self.content_layout`** (a log pane already exists below it).

## 2. UI conventions

- Group inputs in `QGroupBox` sections: inputs → options → output → an action-button row (`addStretch()` before the button, `content_layout.addStretch()` last).
- Browse buttons remember the last-used directory via `Settings`: read `settings.get('<tool>_last_<field>_dir', '')`, and `settings.set(...)` + `settings.save()` after a successful pick.
- Log through `self.log(msg, level)` with levels `INFO` / `WARNING` / `ERROR` / `SUCCESS`; status bar via `self.emit_status(msg)`. Connect the worker: `worker.progress.connect(self.log)`.
- Start workers via `self.start_worker(...)` — it wires progress → log, `progress_value` → progress bar (pass `progress_total=len(items)` to seed the bar when the worker reports `(idx, total, message)` through a custom `progress_slot` instead; call `self.set_progress(idx, total)` there), busy-disables widgets, and shows/hides the bar around the run.
- Validate inputs in the start handler with early returns + `self.log(..., "ERROR")` before spawning the worker; keep a reference (`self.worker = ...`) or the thread gets garbage-collected mid-run.
- Seed defaults from **`app.core.tool_defaults`** (`default_material_path()`, `default_max_parallel()`, `default_generate_mipmaps()`) rather than hard-coding, so Preferences → General applies. These are read once at construction.

## 3. Register it

In [main_window.py](v2/app/ui/main_window.py):

1. Add the import at the top with the other tool imports.
2. Add `("Display Name", ToolClass)` to the right category in the `tool_categories` dict in `create_sidebar()` (Models / Materials / Materials / PBR / Sounds / Utility). Categories self-sort alphabetically; escape `&` as `&&` in labels.

That's all — the button, catalogue entry, and `remember_last_tool` restore come for free.

## 4. Finish up

- New persistent settings keys get a default in `Settings.DEFAULT_SETTINGS` ([settings.py](v2/app/core/settings.py)) with a comment explaining semantics; sentinels are `''`/`0` = "use tool default".
- Add a `test_<name>.py` under `v2/tests/` exercising the Qt-free logic (no QApplication needed).
- Verify: `python -m py_compile` on touched files, import `app.ui.main_window`, then launch the GUI and run the tool once (see the `run` skill).
- Add the tool to the feature table in [README.md](README.md).
