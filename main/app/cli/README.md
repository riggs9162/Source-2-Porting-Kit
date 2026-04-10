# CLI Tools

Command-line interfaces for Source 2 Porting Kit tools.

## FakePBR CLI

Convert Source 2 PBR textures to Source 1 materials via command line.

### Quick Usage

```bash
# Basic usage
python fakepbr_cli.py --color base.png --normal norm.png --out ./materials --stem my_mat

# Auto-detect inputs
python fakepbr_cli.py --in ./textures --out ./materials --stem metal_panel

# Batch process folder
python fakepbr_cli.py --in ./textures --out ./materials --batch
```

### Full Documentation

See: `docs/FAKEPBR_TOOL.md` and `docs/FAKEPBR_QUICKSTART.md`

### Requirements

- Python 3.8+
- PySide6, Pillow, numpy (see `requirements.txt`)
- VTFLib DLLs in `thirdparty/bin/`

### Help

```bash
python fakepbr_cli.py --help
```
