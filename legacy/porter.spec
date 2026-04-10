# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from pathlib import Path

# Get the current directory (spec file location)
current_dir = Path(os.getcwd()).absolute()

# Define data files to include
datas = []

# Include the tools package and all its modules
tools_dir = current_dir / 'tools'
if tools_dir.exists():
    for py_file in tools_dir.rglob('*.py'):
        rel_path = py_file.relative_to(current_dir)
        datas.append((str(py_file), str(rel_path.parent)))

# Include VTFLibWrapper and its binary files
vtflib_dir = current_dir / 'VTFLibWrapper'
if vtflib_dir.exists():
    # Include Python files
    for py_file in vtflib_dir.rglob('*.py'):
        rel_path = py_file.relative_to(current_dir)
        datas.append((str(py_file), str(rel_path.parent)))
    
    # Include binary files (DLLs)
    bin_dir = vtflib_dir / 'bin'
    if bin_dir.exists():
        for dll_file in bin_dir.glob('*.dll'):
            datas.append((str(dll_file), 'VTFLibWrapper/bin'))

# Include config.json if it exists
config_file = current_dir / 'config.json'
if config_file.exists():
    datas.append((str(config_file), '.'))

# Include any additional resource files
resource_files = [
    'LICENSE',
    'README.md'
]

for resource in resource_files:
    resource_path = current_dir / resource
    if resource_path.exists():
        datas.append((str(resource_path), '.'))

# Hidden imports for dynamically loaded modules
hiddenimports = [
    'tools.bone_backport_tool',
    'tools.brightness_to_alpha_tool',
    'tools.color_transparency_tool',
    'tools.fake_pbr_baker_tool',
    'tools.loop_sound_converter_tool',
    'tools.metal_transparency_tool',
    'tools.qc_generation_tool',
    'tools.qc_smd_prefix_tool',
    'tools.quad_to_stereo_tool',
    'tools.search_replace_tool',
    'tools.soundscape_searcher_tool',
    'tools.subtexture_extraction_tool',
    'tools.texture_tool',
    'tools.vmat_to_vmt_tool',
    'tools.vmt_generator_tool',
    'tools.filename_sanitizer_tool',
    'tools.base_tool',
    'tools.utils',
    'VTFLibWrapper.VTFLib',
    'VTFLibWrapper.VTFLibEnums',
    'VTFLibWrapper.VTFLibStructures',
    'VTFLibWrapper.VTFLibConstants',
    'PIL',
    'PIL.Image',
    'PIL.ImageTk',
    'PIL.ImageChops',
    'PIL.ImageEnhance',
    'PIL.ImageOps',
    'tkinterdnd2',
    'pydub',
    'discordrp'
]

# Exclusions to reduce file size
excludes = [
    'matplotlib',
    'scipy',
    'numpy.distutils',
    'numpy.testing',
    'setuptools',
    'distutils'
]

block_cipher = None

a = Analysis(
    ['porter.py'],
    pathex=[str(current_dir)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Remove duplicate files
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Source 2 Porting Kit',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='hlvr.ico',
    version=None,
)
