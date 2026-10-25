#!/usr/bin/env python3
"""
Build Source 2 Porting Kit v3 into a Windows executable with PyInstaller.

The desktop window is the Flet client, a separate Flutter program. The build
copies the client this interpreter already uses, gives it the kit's icon and
version information, and bundles it as the archive `flet_desktop` unpacks on
first launch, so the executable needs no download. Browser mode's web client
is bundled too. Every tool is registered lazily by module name, so the whole
`s2kit` package is collected explicitly.

    python v3/build_exe.py            one self-contained .exe in v3/dist/
    python v3/build_exe.py --onedir   a folder build with faster startup
    python v3/build_exe.py --console  keep a console window for debugging

Settings, history and logs are written beside the executable, as when running
from source they are written beside run.py.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import zipfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from s2kit import APP_NAME, __version__  # noqa: E402

ENTRYPOINT = SCRIPT_DIR / "run.py"
ICON_PATH = SCRIPT_DIR / "assets" / "icon.ico"
BUILD_DIR = SCRIPT_DIR / "build"
DIST_DIR = SCRIPT_DIR / "dist"
DATA_DIRS = [
    (SCRIPT_DIR / "assets", "assets"),
    (SCRIPT_DIR / "s2kit" / "native", "s2kit/native"),
]
COLLECT_ALL = ["sourcepp", "trimesh"]
COLLECT_DATA = ["flet", "flet_web", "srctools"]
EXCLUDES = [
    "pytest", "IPython", "jupyter", "notebook", "matplotlib", "pandas", "torch",
    "torchvision", "torchaudio", "tensorflow", "sklearn", "lightning", "pymeshlab",
    "PySide6", "PyQt5", "PyQt6",
]


def file_version() -> str:
    """The `n.n.n.n` Windows version resource for the kit's release number."""
    numbers = [int(part) for part in __version__.split("-", 1)[0].split(".")]
    return ".".join(str(number) for number in [*numbers, 0, 0, 0][:4])


def prepare_flet_client(work: Path) -> Path:
    """Copy the desktop client, brand it, and pack it as the archive PyInstaller ships.

    Returns the directory holding `flet-windows.zip` and the version-info file
    the main executable reuses.
    """
    from flet_desktop import ensure_client_cached
    from flet_cli.__pyinstaller.win_utils import update_flet_view_icon, update_flet_view_version_info

    client = ensure_client_cached()
    staging = work / "flet-client"
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(client, staging)
    daemon = staging / "fletd.exe"
    if daemon.exists():
        daemon.unlink()
    viewer = staging / "flet" / "flet.exe"
    if ICON_PATH.is_file():
        update_flet_view_icon(str(viewer), str(ICON_PATH))
    version_file = update_flet_view_version_info(
        exe_path=str(viewer), product_name=APP_NAME, file_description=APP_NAME,
        product_version=__version__, file_version=file_version(), company_name="Riggs",
        copyright="Riggs")
    archive = staging / "flet-windows.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted((staging / "flet").rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(staging).as_posix())
    shutil.rmtree(staging / "flet")
    shutil.copy2(version_file, work / "version_info.txt")
    return staging


def pyinstaller_arguments(args: argparse.Namespace, version_file: Path) -> list[str]:
    """Assemble the PyInstaller command line for the selected build layout."""
    import flet_cli.__pyinstaller as flet_hooks

    hooks = Path(flet_hooks.__file__).parent
    separator = ";" if os.name == "nt" else ":"
    command = [
        str(ENTRYPOINT), "--noconfirm", "--name", args.name,
        "--distpath", str(DIST_DIR), "--workpath", str(BUILD_DIR / "work"),
        "--specpath", str(BUILD_DIR / "spec"), "--paths", str(SCRIPT_DIR),
        "--additional-hooks-dir", str(hooks),
        "--runtime-hook", str(hooks / "rthooks" / "pyi_rth_localhost_fletd.py"),
        "--collect-submodules", "s2kit", "--version-file", str(version_file),
        "--onedir" if args.onedir else "--onefile",
        "--console" if args.console else "--windowed",
    ]
    if args.clean:
        command.append("--clean")
    if ICON_PATH.is_file():
        command.extend(["--icon", str(ICON_PATH)])
    for source, target in DATA_DIRS:
        if source.is_dir():
            command.extend(["--add-data", f"{source}{separator}{target}"])
    for package in COLLECT_ALL:
        command.extend(["--collect-all", package])
    for package in COLLECT_DATA:
        command.extend(["--collect-data", package])
    for module in EXCLUDES:
        command.extend(["--exclude-module", module])
    return command


def parse_args() -> argparse.Namespace:
    """Read the build layout and debugging switches."""
    parser = argparse.ArgumentParser(description=f"Build {APP_NAME} v{__version__} into an executable.")
    parser.add_argument("--onedir", action="store_true",
                        help="build a folder with faster startup instead of one self-contained .exe")
    parser.add_argument("--console", action="store_true", help="keep a console window for log output")
    parser.add_argument("--clean", action="store_true", help="discard PyInstaller's analysis cache first")
    parser.add_argument("--name", default=APP_NAME, help=f"executable name (default: {APP_NAME})")
    return parser.parse_args()


def main() -> int:
    """Prepare the bundled client, then run PyInstaller in this process so its Flet hook sees it."""
    if os.name != "nt":
        print("The v3 executable build targets Windows.")
        return 1
    args = parse_args()
    import flet_cli.__pyinstaller.config as hook_config
    import PyInstaller.__main__

    work = BUILD_DIR / "flet"
    work.mkdir(parents=True, exist_ok=True)
    print(f"Building {APP_NAME} v{__version__} ({'onedir' if args.onedir else 'onefile'})")
    hook_config.temp_bin_dir = str(prepare_flet_client(work))
    PyInstaller.__main__.run(pyinstaller_arguments(args, work / "version_info.txt"))
    executable = DIST_DIR / (f"{args.name}/{args.name}.exe" if args.onedir else f"{args.name}.exe")
    if not executable.is_file():
        print(f"PyInstaller finished without producing {executable}")
        return 1
    size = executable.stat().st_size if not args.onedir else sum(
        path.stat().st_size for path in executable.parent.rglob("*") if path.is_file())
    print(f"\nBuilt {executable} ({size / 1024 / 1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
