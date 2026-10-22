---
name: repo-conventions
description: Module layout, settings/config model, and code conventions for the Source 2 Porting Kit codebase. Use when editing or reviewing any code in this repo.
---

# Source 2 Porting Kit — repo conventions

## Layout

- `v2/main.py` — entry point (logging + excepthook + QApplication). `v1/` is old code — never extend it.
- `v2/app/core/` — Qt-free app state: `settings.py` (Settings), `tool_defaults.py` (Preferences-seeded defaults), domain data tables (`bone_maps.py`, `ragdoll_presets.py`).
- `v2/app/tools/` — one file per sidebar tool (`*_tool.py`, BaseTool subclass + QThread worker) plus Qt-free pipeline modules (`ragdoll_physics.py`, `smd_export*.py`, `gltf_animation.py`).
- `v2/app/utils/` — Qt-free shared logic: `vrf_runner.py`, `vtf_encoder.py`, `pbr_processing.py`, `image_processing.py`, `vmt_generator.py`, `audio_runner.py`, `helpers.py`.
- `v2/app/ui/` — `main_window.py` (sidebar/tool registry), `preferences_dialog.py`, `styling.py` (themes/fonts).
- `v2/app/cli/` — headless entry points (`vrf_export_cli.py`, `fakepbr_cli.py`); they `sys.path.insert` their way to `app.*`.
- `v2/manuals/` — in-repo reference docs (QC, collision, fake-PBR, soundscapes, VTF). Check here before researching Source engine questions externally.
- `v2/tests/` — pytest files + standalone `debug_*.py` diagnostics. `v2/config/` — runtime user settings/history JSON (generated; don't commit meaningful state). `v2/logs/app.log` — runtime log.

## Hard rules

- **Qt-free processing**: computation, file I/O, and format parsing never import Qt. Tool files only orchestrate. This is what keeps logic reachable from CLIs and tests.
- **No blocking work on the UI thread**: long operations run in a `QThread` worker with `progress(str, str)` / `finished(bool, ...)` signals; exceptions are caught inside `run()` and reported via signals.
- **Settings discipline**: every persisted key has a default in `Settings.DEFAULT_SETTINGS` (loaded settings are merged over defaults). Sentinels `''` / `0` mean "unset — use the tool's hard-coded default". Global tool defaults go through `core/tool_defaults.py` accessors (read-once-at-open, wrapped in try/except with a fallback).
- **VTF encoding**: always pre-resize images in Python before handing buffers to sourcepp — its internal downscale corrupts normal maps (see the clamp settings in `settings.py`).

## Style

- Python 3.10+, 4-space indent. Module docstring at the top of every file stating what it does; docstrings on classes and non-trivial functions (plain prose or Args/Returns sections, matching neighbors).
- Type hints on public function signatures (`Optional[...]`, `from __future__ import annotations` in newer modules); `pathlib.Path` over string paths.
- Comments explain *why* and non-obvious semantics (see the clamp-settings block in `settings.py` for the house style) — not what the next line does.
- Log levels: `INFO` progress, `WARNING` recoverable, `ERROR` failures, `SUCCESS` completions.
- Commit messages: conventional-commit style scoped to the tool, e.g. `feat(vmat-pbr): add support for vmat literal rgba constants`.
