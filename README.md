<div align="center">

# Source 2 Porting Kit

**A desktop toolkit for porting assets from Source 2 games to Source 1.**

Port models, materials, sounds, and more from games like Half-Life: Alyx back to the Source 1 engine.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-yellow.svg)](https://www.python.org/)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)](#)
[![Discord](https://img.shields.io/badge/Discord-Riggs'_Bike_Club-5865F2?logo=discord&logoColor=white)](https://discord.gg/MpWHkcqzyB)

</div>

---

## Features

<table>
<tr>
<td width="50%" valign="top">

### Models
- **Bone Backport** — Backport bone data for Source 1 compatibility
- **GLTF Batch SMD** — Batch-convert GLTF models to SMD, with QC scaffolding and `$texturegroup` skin support
- **VRF Batch Export** — Batch-extract `.vmdl_c` / `.vmat_c` / `.vtex_c` from VPKs via Source2Viewer-CLI, staged into a Source 1 sibling layout (`modelsrc/`, `materialsrc/`)

### Materials
- **Alpha Mask** — Apply alpha mask operations to textures
- **Hotspot Editor** — Edit `.rect` hotspot files for textures
- **VTF Clamp** — Clamp / resize VTF textures to Source 1-friendly dimensions

### Materials / PBR
- **Fake PBR Reverse** — Reverse Source 1 "fake PBR" composites back into separate maps
- **Manual PBR Converter** — Hands-on PBR-to-Source 1 conversion for individual materials
- **Texture Folder PBR Batch** — Recursively batch-process PBR textures across a folder tree
- **VMAT PBR Batch Converter** — Convert Source 2 `.vmat` PBR materials to Source 1 `.vmt`

</td>
<td width="50%" valign="top">

### Sounds
- **Loop Point Converter** — Set loop points in sound files
- **OGG Converter** — Convert audio files to OGG format
- **Quad to Stereo** — Convert quad-channel audio to stereo
- **Soundscape Porter** — Port Source 2 soundscapes to Source 1

### Utility
- **Filename Sanitizer** — Clean up file names for Source 1 compatibility
- **Search & Replace (Files)** — Search and replace text within files
- **Search & Replace (Folder)** — Batch rename files and folders

</td>
</tr>
</table>

---

## Getting Started

There are three ways to use the Source 2 Porting Kit:

### Option 1 — Download a Release (Recommended)

The easiest way to get started. Download the latest `.exe` from the [Releases](https://github.com/riggs9162/Source-2-Porting-Kit/releases) page and run it — no Python installation required.

### Option 2 — Run from Source

1. Clone the repository:
   ```bash
   git clone https://github.com/riggs9162/Source-2-Porting-Kit.git
   cd Source-2-Porting-Kit
   ```

2. Install dependencies:
   ```bash
   pip install -r main/requirements.txt
   ```

3. Run the application:
   ```bash
   python main/main.py
   ```

### Option 3 — Build the Executable Yourself

Follow the same clone and install steps as Option 2, then build a standalone `.exe` with PyInstaller:

```bash
cd main
python build_exe.py
```

The output will be placed in `main/dist/`.

> **Note:** Some audio tools require [FFmpeg](https://ffmpeg.org/) to be available on your system PATH.

---

## License

This project is licensed under the [MIT License](LICENSE).

---

<div align="center">

### Support the Project

[![GitHub Sponsors](https://img.shields.io/badge/GitHub_Sponsors-Support-ea4aaa?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/riggs9162)
[![Patreon](https://img.shields.io/badge/Patreon-Support-f96854?logo=patreon&logoColor=white)](https://patreon.com/riggs9162)
[![Ko-fi](https://img.shields.io/badge/Ko--fi-Support-ff5e5b?logo=kofi&logoColor=white)](https://ko-fi.com/riggs9162)

</div>
