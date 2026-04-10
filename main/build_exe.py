#!/usr/bin/env python3
"""
PyInstaller build script for the current Source 2 Porting Kit app.

Builds `main.py` into a Windows executable and bundles runtime resources.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_NAME = "Source 2 Porting Kit"
SCRIPT_DIR = Path(__file__).resolve().parent
ENTRYPOINT = SCRIPT_DIR / "main.py"
ICON_PATH = SCRIPT_DIR / "app" / "resources" / "icon.ico"
DATA_DIRS = [
    (SCRIPT_DIR / "app" / "resources", "app/resources"),
    (SCRIPT_DIR / "manuals", "manuals"),
]


def _pyinstaller_data_arg(src: Path, dest: str) -> str:
    sep = ";" if os.name == "nt" else ":"
    return f"{src}{sep}{dest}"


def _has_module(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _run(cmd: list[str]) -> None:
    print(f"> {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def ensure_pyinstaller_installed() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not found. Installing...")
        _run([sys.executable, "-m", "pip", "install", "pyinstaller"])


def install_requirements() -> None:
    requirements_file = SCRIPT_DIR / "requirements.txt"
    if not requirements_file.exists():
        print("requirements.txt not found, skipping dependency install.")
        return
    print("Installing project requirements...")
    _run([sys.executable, "-m", "pip", "install", "-r", str(requirements_file)])


def clean_build_outputs() -> None:
    for path in (SCRIPT_DIR / "build", SCRIPT_DIR / "dist"):
        if path.exists():
            print(f"Removing {path} ...")
            shutil.rmtree(path)


def ensure_build_dirs() -> None:
    (SCRIPT_DIR / "build" / "work").mkdir(parents=True, exist_ok=True)
    (SCRIPT_DIR / "build" / "spec").mkdir(parents=True, exist_ok=True)


def build_command(args: argparse.Namespace) -> list[str]:
    cmd: list[str] = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--name",
        args.name,
        "--paths",
        str(SCRIPT_DIR),
        "--distpath",
        str(SCRIPT_DIR / "dist"),
        "--workpath",
        str(SCRIPT_DIR / "build" / "work"),
        "--specpath",
        str(SCRIPT_DIR / "build" / "spec"),
    ]

    if not args.no_clean:
        cmd.append("--clean")

    cmd.append("--onefile" if args.onefile else "--onedir")
    cmd.append("--console" if args.console else "--windowed")

    if args.debug:
        cmd.append("--debug=all")

    if ICON_PATH.exists():
        cmd.extend(["--icon", str(ICON_PATH)])

    for src, dest in DATA_DIRS:
        if src.exists():
            cmd.extend(["--add-data", _pyinstaller_data_arg(src, dest)])

    # Dependencies that are frequently missed by static analysis or require
    # packaged resources/binaries.
    hidden_imports = [
        "ffmpeg",         # ffmpeg-python (imported inside methods)
        "pydub",          # imported lazily in audio tools
        "sourcepp.vtfpp", # native-backed module used for VTF work
    ]
    for module_name in hidden_imports:
        if _has_module(module_name):
            cmd.extend(["--hidden-import", module_name])

    collect_all = [
        "sourcepp",
        "trimesh",
        "pymeshlab",
    ]
    for package_name in collect_all:
        if _has_module(package_name):
            cmd.extend(["--collect-all", package_name])

    cmd.append(str(ENTRYPOINT))
    return cmd


def print_runtime_warnings() -> None:
    warnings: list[str] = []

    if not _has_module("pydub"):
        warnings.append(
            "Optional package 'pydub' is not installed. OGG/Quad audio tools will be unavailable in the built app."
        )

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        warnings.append(
            "ffmpeg/ffprobe executables were not found on PATH. Audio conversion tools may fail at runtime."
        )

    if warnings:
        print()
        print("Warnings:")
        for item in warnings:
            print(f"- {item}")


def output_exe_path(app_name: str, onefile: bool) -> Path:
    if onefile:
        return SCRIPT_DIR / "dist" / f"{app_name}.exe"
    return SCRIPT_DIR / "dist" / app_name / f"{app_name}.exe"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Source 2 Porting Kit executable")
    parser.add_argument("--onefile", action="store_true", help="Build a single-file executable (slower startup)")
    parser.add_argument("--console", action="store_true", help="Show console window for debugging")
    parser.add_argument("--debug", action="store_true", help="Enable PyInstaller debug mode")
    parser.add_argument("--no-clean", action="store_true", help="Do not remove previous build/dist output")
    parser.add_argument("--no-deps", action="store_true", help="Skip pip install for requirements and PyInstaller")
    parser.add_argument("--name", default=APP_NAME, help=f"Executable name (default: {APP_NAME})")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not ENTRYPOINT.exists():
        print(f"Entrypoint not found: {ENTRYPOINT}")
        return 1

    os.chdir(SCRIPT_DIR)
    print(f"Working directory: {SCRIPT_DIR}")
    print(f"Entrypoint: {ENTRYPOINT.name}")
    print(f"Mode: {'onefile' if args.onefile else 'onedir'}")

    try:
        if not args.no_deps:
            ensure_pyinstaller_installed()
            install_requirements()

        if not args.no_clean:
            clean_build_outputs()

        ensure_build_dirs()

        cmd = build_command(args)
        print("Running PyInstaller...")
        subprocess.run(cmd, check=True)

        exe_path = output_exe_path(args.name, args.onefile)
        print()
        print("Build completed successfully.")
        print(f"Executable: {exe_path}")
        if not exe_path.exists():
            print("Note: expected executable path not found yet. Check the dist folder output.")

        print_runtime_warnings()
        return 0

    except subprocess.CalledProcessError as exc:
        print()
        print(f"Build failed with exit code {exc.returncode}.")
        return exc.returncode
    except Exception as exc:  # pragma: no cover - defensive build-script handling
        print()
        print(f"Unexpected error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
