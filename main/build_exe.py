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

# Pinned UPX version. UPX 4.x compresses considerably better than 3.x and is
# stable on modern Windows. The single-file Win64 zip is ~580 KB.
UPX_VERSION = "4.2.4"
UPX_DIR = SCRIPT_DIR / "build" / "upx"

# DLLs that must NOT be UPX-compressed. UPXing system runtime DLLs (vcruntime,
# ucrt, api-ms-win-*) breaks Windows DLL signing checks; UPXing python3*.dll
# breaks PyInstaller's bootloader on some Windows builds; Qt's WebEngine and
# the Vulkan loader contain self-checks that fail when compressed.
UPX_EXCLUDES = [
    "vcruntime140.dll", "vcruntime140_1.dll",
    "msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll",
    "ucrtbase.dll", "ucrtbased.dll",
    "python3.dll", "python313.dll",
    # Qt WebEngine + Vulkan loader sometimes refuse to load when compressed.
    "qwindowsvistastyle.dll", "vulkan-1.dll",
]


def _pyinstaller_data_arg(src: Path, dest: str) -> str:
    sep = ";" if os.name == "nt" else ":"
    return f"{src}{sep}{dest}"


def _upx_executable_name() -> str:
    return "upx.exe" if os.name == "nt" else "upx"


def ensure_upx() -> Path | None:
    """Locate UPX, downloading a pinned Windows build into build/upx/ if needed.

    UPX compression cuts ~30-50% off native DLLs and EXEs, with a small
    one-time decompression cost on launch. Returns the directory containing
    the UPX binary (what PyInstaller wants), or None if UPX could not be
    obtained on this platform.
    """
    # 1. Already vendored in build/upx/.
    vendored = UPX_DIR / _upx_executable_name()
    if vendored.exists():
        return UPX_DIR

    # 2. On PATH.
    on_path = shutil.which("upx")
    if on_path:
        return Path(on_path).parent

    # 3. Download a pinned Windows build. Skip on non-Windows where users
    # can apt/brew install upx themselves.
    if os.name != "nt":
        print("UPX not found on PATH; install it via your package manager (e.g. `apt install upx`).")
        return None

    print(f"UPX not found. Downloading UPX {UPX_VERSION} ...")
    url = (
        f"https://github.com/upx/upx/releases/download/v{UPX_VERSION}/"
        f"upx-{UPX_VERSION}-win64.zip"
    )
    try:
        import io
        import urllib.request
        import zipfile

        with urllib.request.urlopen(url, timeout=60) as resp:
            payload = resp.read()
        UPX_DIR.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            for member in zf.namelist():
                if member.endswith("/upx.exe") or member.endswith("\\upx.exe"):
                    with zf.open(member) as src, open(vendored, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    break
        if not vendored.exists():
            print("UPX archive did not contain upx.exe; skipping compression.")
            return None
        print(f"UPX installed to {vendored}")
        return UPX_DIR
    except Exception as exc:  # noqa: BLE001
        print(f"UPX download failed ({exc}); building without compression.")
        return None


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


BUILD_VENV_DIR = SCRIPT_DIR / "build" / "build_venv"
BUILD_VENV_MARKER_ENV = "S2PK_BUILD_VENV_ACTIVE"


def _build_venv_python() -> Path:
    if os.name == "nt":
        return BUILD_VENV_DIR / "Scripts" / "python.exe"
    return BUILD_VENV_DIR / "bin" / "python"


def ensure_build_venv() -> Path:
    """Create (if needed) and refresh the isolated build venv.

    Returns the path to its python executable. Building inside this venv
    is what keeps unrelated globally-installed packages (torch, lightning,
    pandas, etc.) from being dragged into the bundle by PyInstaller's
    static analysis.
    """
    venv_python = _build_venv_python()
    if not venv_python.exists():
        print(f"Creating isolated build venv at {BUILD_VENV_DIR} ...")
        BUILD_VENV_DIR.parent.mkdir(parents=True, exist_ok=True)
        # Use the stdlib `venv` module to avoid a dependency on virtualenv.
        # `with_pip=True` gives us pip; `clear=False` is harmless on a
        # missing dir but explicit.
        import venv  # local import to keep top-of-file lean

        venv.EnvBuilder(with_pip=True, clear=False, upgrade_deps=False).create(BUILD_VENV_DIR)

    # Always make sure pip + PyInstaller + project requirements are present.
    # pip-install is idempotent so subsequent runs are fast (~few seconds).
    print("Refreshing build venv packages ...")
    _run([str(venv_python), "-m", "pip", "install", "--upgrade", "--quiet", "pip"])
    _run([str(venv_python), "-m", "pip", "install", "--quiet", "pyinstaller"])
    requirements_file = SCRIPT_DIR / "requirements.txt"
    if requirements_file.exists():
        _run([str(venv_python), "-m", "pip", "install", "--quiet", "-r", str(requirements_file)])

    return venv_python


def reexec_in_build_venv(extra_args: list[str]) -> int:
    """Re-run this script using the build venv's python interpreter."""
    venv_python = ensure_build_venv()
    env = os.environ.copy()
    env[BUILD_VENV_MARKER_ENV] = "1"
    cmd = [str(venv_python), str(Path(__file__).resolve()), *extra_args]
    print(f"Re-launching build inside venv: {venv_python}")
    result = subprocess.run(cmd, env=env)
    return result.returncode


def already_in_build_venv() -> bool:
    return os.environ.get(BUILD_VENV_MARKER_ENV) == "1"


def clean_build_outputs() -> None:
    # Preserve the build venv even on a full --clean: rebuilding it costs
    # 1-2 minutes for ~1 GB of pip downloads. The venv lives under build/
    # but is independent of PyInstaller's analysis cache.
    preserve = {BUILD_VENV_DIR.resolve()}
    for path in (SCRIPT_DIR / "build", SCRIPT_DIR / "dist"):
        if not path.exists():
            continue
        if path.resolve() == (SCRIPT_DIR / "build").resolve():
            # Wipe build/ contents but keep build/build_venv/.
            for child in path.iterdir():
                if child.resolve() in preserve:
                    continue
                print(f"Removing {child} ...")
                if child.is_dir():
                    _robust_rmtree(child)
                else:
                    try:
                        child.unlink()
                    except OSError:
                        pass
        else:
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
    else:
        # Strip docstrings + assert statements from bundled .pyc files.
        # Saves a few MB and is the standard release-mode optimization.
        cmd.extend(["--optimize", "2"])

    # UPX compression — biggest single win on bundle size after venv isolation.
    if not args.no_upx:
        upx_dir = ensure_upx()
        if upx_dir is not None:
            cmd.extend(["--upx-dir", str(upx_dir)])
            for excl in UPX_EXCLUDES:
                cmd.extend(["--upx-exclude", excl])
        else:
            cmd.append("--noupx")
    else:
        cmd.append("--noupx")

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
    ]
    for package_name in collect_all:
        if _has_module(package_name):
            cmd.extend(["--collect-all", package_name])

    # The project only uses QtCore + QtGui + QtWidgets. Excluding the rest
    # of the PySide6 module set strips ~80-150 MB of Qt6 plugins and
    # binaries that would otherwise be bundled "just in case".
    pyside6_excludes = [
        "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
        "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
        "PySide6.QtBluetooth", "PySide6.QtCharts", "PySide6.QtConcurrent",
        "PySide6.QtDataVisualization", "PySide6.QtDBus", "PySide6.QtDesigner",
        "PySide6.QtHelp", "PySide6.QtHttpServer", "PySide6.QtLocation",
        "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
        "PySide6.QtNetwork", "PySide6.QtNetworkAuth", "PySide6.QtNfc",
        "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtPdf",
        "PySide6.QtPdfWidgets", "PySide6.QtPositioning",
        "PySide6.QtPrintSupport", "PySide6.QtQml", "PySide6.QtQuick",
        "PySide6.QtQuick3D", "PySide6.QtQuickControls2",
        "PySide6.QtQuickWidgets", "PySide6.QtRemoteObjects",
        "PySide6.QtScxml", "PySide6.QtSensors", "PySide6.QtSerialBus",
        "PySide6.QtSerialPort", "PySide6.QtSpatialAudio", "PySide6.QtSql",
        "PySide6.QtStateMachine", "PySide6.QtSvg", "PySide6.QtSvgWidgets",
        "PySide6.QtTest", "PySide6.QtTextToSpeech", "PySide6.QtUiTools",
        "PySide6.QtVirtualKeyboard", "PySide6.QtWebChannel",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineQuick",
        "PySide6.QtWebEngineWidgets", "PySide6.QtWebSockets",
        "PySide6.QtXml",
    ]
    # Plus a sweep of common bloat sources that PyInstaller occasionally
    # picks up via transitive imports.
    extra_excludes = [
        "tkinter", "test", "unittest", "pydoc_data",
        # Scientific Python stragglers — none of these are used by this
        # project, but if a build venv ever has them they'd be huge.
        "scipy", "pymeshlab", "matplotlib", "pandas", "torch", "lightning",
        "torchaudio", "torchvision", "tensorflow", "sklearn",
    ]
    for module_name in pyside6_excludes + extra_excludes:
        cmd.extend(["--exclude-module", module_name])

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


def _format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} GB"


def _print_size_report(exe_path: Path, onefile: bool) -> None:
    """After a successful build, print the final distribution size."""
    try:
        if onefile:
            size = exe_path.stat().st_size
            print(f"Bundle size: {_format_bytes(size)} (single-file EXE)")
        else:
            dist_root = exe_path.parent
            total = 0
            for p in dist_root.rglob("*"):
                if p.is_file():
                    total += p.stat().st_size
            print(f"Bundle size: {_format_bytes(total)} ({dist_root.name}/)")
    except OSError:
        pass


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
    parser.add_argument(
        "--release",
        action="store_true",
        help=(
            "Produce a distribution-ready single-file build. Implies --onefile, "
            "--clean, and --use-venv. Onefile mode extracts to %%TEMP%% on launch "
            "which gives PyInstaller's bootloader a flat DLL search directory and "
            "avoids the 'Failed to load Python DLL' error seen on some end-user "
            "machines with the onedir layout (GitHub issue #2). Slower startup "
            "(~2-5s) but more robust on clean Windows installs."
        ),
    )
    parser.add_argument(
        "--use-venv",
        action="store_true",
        help=(
            "Build inside an isolated venv at build/build_venv/ that contains only "
            "the packages from requirements.txt. Without this, PyInstaller analyzes "
            "your global Python install and bundles unrelated packages it finds "
            "importable (e.g. torch, lightning, pandas). For this project that "
            "alone has shrunk releases from 3 GB to ~500 MB. First run takes 1-2 "
            "minutes to populate the venv; subsequent runs reuse it."
        ),
    )
    parser.add_argument(
        "--no-upx",
        action="store_true",
        help=(
            "Disable UPX compression. By default, UPX is auto-downloaded into "
            "build/upx/ and applied to native DLLs (typically ~30-50%% smaller "
            "bundle, with a small one-time launch decompression cost)."
        ),
    )
    # Back-compat aliases (silently accepted; current defaults already match
    # what these flags used to request).
    parser.add_argument("--no-clean", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-deps", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--name", default=APP_NAME, help=f"Executable name (default: {APP_NAME})")
    args = parser.parse_args()
    # --release is a convenience preset for distribution builds.
    if args.release:
        args.onefile = True
        args.clean = True
        args.use_venv = True
    return args


def main() -> int:
    args = parse_args()

    if not ENTRYPOINT.exists():
        print(f"Entrypoint not found: {ENTRYPOINT}")
        return 1

    os.chdir(SCRIPT_DIR)
    print(f"Working directory: {SCRIPT_DIR}")
    print(f"Entrypoint: {ENTRYPOINT.name}")
    print(f"Mode: {'onefile' if args.onefile else 'onedir'}")

    # If --use-venv was requested and we're not already inside the build
    # venv, set up the venv and re-launch this script with its python.
    # Strip --use-venv from the forwarded argv so the inner run doesn't
    # recurse (the env-var marker also prevents recursion).
    if args.use_venv and not already_in_build_venv():
        forwarded = [a for a in sys.argv[1:] if a not in ("--use-venv",)]
        return reexec_in_build_venv(forwarded)

    try:
        # PyInstaller is required either way; quietly verify the import
        # without invoking pip when the package is already present. Inside
        # the build venv, ensure_build_venv() already installed it.
        if args.install_deps and not already_in_build_venv():
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
        else:
            _print_size_report(exe_path, args.onefile)

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
