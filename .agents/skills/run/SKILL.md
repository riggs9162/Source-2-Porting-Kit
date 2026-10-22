---
name: run
description: How to launch, exercise, and verify the Source 2 Porting Kit app (PySide6 GUI + headless CLIs). Use when asked to run the app, verify a change works, or test a tool end-to-end.
---

# Running the Source 2 Porting Kit

## Launch the GUI

From the repo root:

```powershell
python v2\main.py
```

- Requires Python 3.10+ and `pip install -r v2/requirements.txt` (numpy, Pillow, PySide6, sourcepp, srctools, trimesh).
- Audio tools additionally need **FFmpeg on PATH**.
- It is a Qt desktop app — it opens a window and blocks. Launch with `run_in_background: true` if you need the terminal back.
- All output is logged to **`v2/logs/app.log`** (DEBUG level, plus stdout). Read this file to diagnose startup failures, tool errors, and Qt warnings — uncaught exceptions land here via the global excepthook.
- User settings persist in **`v2/config/settings.json`** (created next to `v2/`); tool history JSONs live in the same folder.

## Headless verification (preferred for automated checks)

The GUI cannot be driven headlessly, but most processing logic is Qt-free in `v2/app/utils/` and two CLIs exercise real pipelines end-to-end:

```powershell
# VRF batch export (vmdl_c/VPK -> GLTF + materials tree); needs Source2Viewer-CLI.exe
python v2\app\cli\vrf_export_cli.py --in <model.vmdl_c|folder|pak01_dir.vpk> --out <dir> [--vrf <Source2Viewer-CLI.exe>] [--vpk-path <subtree>]

# FakePBR conversion (PBR pngs -> Source 1 material)
python v2\app\cli\fakepbr_cli.py --in <textures> --out <materials> --batch
```

The VRF CLI reuses the saved `vrf_cli_path` from `v2/config/settings.json` when `--vrf` is omitted.

Quick sanity checks after edits:

```powershell
python -m py_compile v2\app\tools\<file>.py        # syntax
python -c "import sys; sys.path.insert(0,'v2'); import app.ui.main_window"  # imports/registration
```

## Tests

Tests live in `v2/tests/` (pytest-style `test_*.py` plus ad-hoc `debug_*.py` scripts):

```powershell
cd v2
python -m pytest tests\test_<name>.py -x -q
```

Run the specific test file for the area you touched; the folder also contains standalone diagnostic scripts that are run directly with `python`, not pytest.

## Build the exe

```powershell
cd v2
python build_exe.py   # PyInstaller; output in v2/dist/
```
