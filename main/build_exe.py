#!/usr/bin/env python3
"""
PyInstaller build script for the current Source 2 Porting Kit app.

Builds `main.py` into a Windows executable and bundles runtime resources.
"""

from __future__ import annotations

import argparse
import errno
import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import time
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


def _on_rmtree_error(func, path, _exc):
    """rmtree onexc handler that clears the read-only bit and retries.

    PyInstaller's bundled `.pyd` and `.dll` files are sometimes written
    read-only, which makes Windows refuse to delete them on the first try.
    """
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass
    func(path)


def _robust_rmtree(path: Path, *, retries: int = 5, delay: float = 0.5) -> None:
    """Delete a directory tree on Windows, retrying transient lock errors.

    File handles released by another process (or AV scanners) are typically
    cleared within a second or two. If the path is still locked after
    `retries` attempts, raise a clear error pointing at the locked file —
    the most common cause is that a previous build of the EXE is still
    running.
    """
    if not path.exists():
        return

    last_error: BaseException | None = None
    for attempt in range(1, retries + 1):
        try:
            shutil.rmtree(path, onexc=_on_rmtree_error)
            return
        except PermissionError as exc:
            last_error = exc
            locked = exc.filename or path
            print(
                f"  Retry {attempt}/{retries}: could not delete {locked} "
                f"(locked or in use). Waiting {delay:.1f}s..."
            )
            time.sleep(delay)
            delay *= 2  # exponential backoff
        except OSError as exc:
            # ENOTEMPTY / similar — give the same retry treatment.
            if exc.errno not in (errno.EACCES, errno.EBUSY, errno.ENOTEMPTY):
                raise
            last_error = exc
            time.sleep(delay)
            delay *= 2

    locked = getattr(last_error, "filename", None) or path
    raise RuntimeError(
        f"Failed to delete {locked} after {retries} attempts.\n"
        "This usually means a previous build of the app is still running, "
        "or an antivirus is scanning the dist folder. Close any running "
        f"'{APP_NAME}.exe' instances and try again."
    ) from last_error


def clean_build_outputs() -> None:
    for path in (SCRIPT_DIR / "build", SCRIPT_DIR / "dist"):
        if path.exists():
            print(f"Removing {path} ...")
            _robust_rmtree(path)


def clean_dist_only() -> None:
    """Remove only the dist/ tree so PyInstaller can repopulate it.

    Preserves build/work/ (PyInstaller's analysis cache), which makes
    re-runs an order of magnitude faster.
    """
    dist = SCRIPT_DIR / "dist"
    if dist.exists():
        print(f"Removing {dist} ...")
        _robust_rmtree(dist)


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

    if args.clean:
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

    if shutil.which("ffmpeg") is None:
        warnings.append(
            "ffmpeg executable was not found on PATH. Audio conversion tools (OGG converter, "
            "loop point, quad-to-stereo) will fail at runtime."
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
    parser = argparse.ArgumentParser(
        description=(
            "Build Source 2 Porting Kit executable. By default the build is "
            "incremental: dependencies are NOT reinstalled and PyInstaller's "
            "analysis cache is reused, so re-runs are ~10x faster than a full "
            "rebuild. Pass --clean for a full rebuild, --install-deps to refresh "
            "Python packages."
        )
    )
    parser.add_argument("--onefile", action="store_true", help="Build a single-file executable (slower startup)")
    parser.add_argument("--console", action="store_true", help="Show console window for debugging")
    parser.add_argument("--debug", action="store_true", help="Enable PyInstaller debug mode")
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Wipe build/ and dist/ before building and pass --clean to PyInstaller (forces full rebuild)",
    )
    parser.add_argument(
        "--install-deps",
        action="store_true",
        help="Run pip install for PyInstaller and requirements.txt before building",
    )
    # Back-compat aliases (silently accepted; current defaults already match
    # what these flags used to request).
    parser.add_argument("--no-clean", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-deps", action="store_true", help=argparse.SUPPRESS)
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
        # PyInstaller is required either way; quietly verify the import
        # without invoking pip when the package is already present.
        if args.install_deps:
            ensure_pyinstaller_installed()
            install_requirements()
        elif not _has_module("PyInstaller"):
            ensure_pyinstaller_installed()

        if args.clean:
            # Full rebuild: wipe everything and let PyInstaller redo analysis.
            clean_build_outputs()
        else:
            # Incremental rebuild: only the previous app folder needs to go;
            # keep build/work/ so PyInstaller can reuse its analysis cache.
            clean_dist_only()

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
