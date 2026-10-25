#!/usr/bin/env python3
"""
Build Source 2 Porting Kit v3 into a Windows executable with PyInstaller.

The desktop window is the Flet client, a separate Flutter program. Flet's
stock client links every Flet extension, and the geolocator plugin among them
creates Windows location objects at startup, so Windows lists any Flet app as
using the location. The build therefore compiles its own client from the
matching Flet source release with only the extensions the kit uses, gives it
the kit's icon and version information, and bundles it. The packaged app
unpacks that client into local app data and points Flet at it, so it never
starts the stock client and needs no download.

Compiling the client needs git, the Visual Studio C++ desktop workload and the
Flutter release Flet pins; that Flutter SDK is installed into ~/flutter on
first use. The compiled client is reused until the Flet version changes.

    python v3/build_exe.py                 one self-contained .exe in v3/dist/
    python v3/build_exe.py --onedir        a folder build with faster startup
    python v3/build_exe.py --console       keep a console window for debugging
    python v3/build_exe.py --rebuild-client  compile the window client again

Settings, history and logs are written beside the executable, as when running
from source they are written beside run.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
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
CLIENT_DIR = BUILD_DIR / "flet-client"
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

#: Flet extensions the kit's controls need in the window client; every other
#: extension, including the geolocator, camera and recorder plugins, is left out.
CLIENT_EXTENSIONS = {"flet_audio"}


def file_version() -> str:
    """The `n.n.n.n` Windows version resource for the kit's release number."""
    numbers = [int(part) for part in __version__.split("-", 1)[0].split(".")]
    return ".".join(str(number) for number in [*numbers, 0, 0, 0][:4])


def _run(command: list[str], cwd: Path) -> None:
    """Run one build step, echoing it and stopping the build when it fails."""
    print(f"> {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def restrict_client_extensions(client: Path) -> list[str]:
    """Drop every Flet extension the kit does not use from the client's pubspec and entry point.

    Returns the extensions removed.
    """
    pubspec = client / "pubspec.yaml"
    text = pubspec.read_text(encoding="utf-8")
    removed = []

    def drop_dependency(match: re.Match) -> str:
        name = match.group(1)
        if name in CLIENT_EXTENSIONS or name == "flet_integration_test":
            return match.group(0)
        removed.append(name)
        return ""

    text = re.sub(r"(?m)^  (flet_[a-z0-9_]+):\n    path: [^\n]+\n", drop_dependency, text)
    pubspec.write_text(text, encoding="utf-8")
    main = client / "lib" / "main.dart"
    source = main.read_text(encoding="utf-8")
    aliases = []
    for package in removed:
        pattern = rf"""import\s+['"]package:{package}/[^;]*?as\s+(\w+)\s*;\n"""
        match = re.search(pattern, source)
        if match is None:
            raise RuntimeError(f"Could not find the {package} import in {main}")
        aliases.append(match.group(1))
        source = source[:match.start()] + source[match.end():]
    for alias in aliases:
        source, count = re.subn(rf"(?m)^\s*{alias}\.Extension\(\),\n", "", source)
        if count != 1:
            raise RuntimeError(f"Could not find the {alias} extension registration in {main}")
    main.write_text(source, encoding="utf-8")
    return removed


def copy_msvc_runtime(release: Path) -> None:
    """Ship the C++ runtime DLLs the client links against, so it starts without the VC++ redistributable."""
    vswhere = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / \
        "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    install = subprocess.run([str(vswhere), "-latest", "-products", "*", "-requires",
                              "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                              "-property", "installationPath"],
                             capture_output=True, text=True, check=True).stdout.strip()
    folders = sorted(Path(install).glob("VC/Redist/MSVC/*/x64/Microsoft.VC*.CRT"))
    if not folders:
        raise RuntimeError(f"No Visual C++ runtime redistributable under {install}")
    for name in ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        shutil.copy2(folders[-1] / name, release / name)


def build_flet_client(rebuild: bool) -> Path:
    """Compile the trimmed window client for the installed Flet version and return its folder."""
    import flet.version
    from flet_cli.utils.flutter import install_flutter

    flet_version = flet.version.flet_version
    stamp = CLIENT_DIR / "client.json"
    release = CLIENT_DIR / "flet"
    expected = {"flet": flet_version, "extensions": sorted(CLIENT_EXTENSIONS)}
    if not rebuild and stamp.is_file() and json.loads(stamp.read_text()) == expected:
        print(f"Reusing the window client in {release}")
        return release
    source = BUILD_DIR / f"flet-src-{flet_version}"
    if not source.is_dir():
        _run(["git", "clone", "--depth", "1", "--branch", f"v{flet_version}",
              "https://github.com/flet-dev/flet.git", str(source)], BUILD_DIR)
    client = source / "client"
    _run(["git", "checkout", "--", "client/pubspec.yaml", "client/lib/main.dart"], source)
    removed = restrict_client_extensions(client)
    print(f"Window client without: {', '.join(removed)}")
    flutter = Path(install_flutter(flet.version.flutter_version, print)) / "bin" / "flutter.bat"
    _run([str(flutter), "config", "--no-analytics"], client)
    _run([str(flutter), "build", "windows", "--release"], client)
    built = client / "build" / "windows" / "x64" / "runner" / "Release"
    if not (built / "flet.exe").is_file():
        raise RuntimeError(f"Flutter finished without producing {built / 'flet.exe'}")
    if release.exists():
        shutil.rmtree(release)
    shutil.copytree(built, release)
    copy_msvc_runtime(release)
    stamp.write_text(json.dumps(expected), encoding="utf-8")
    return release


def package_flet_client(release: Path, work: Path) -> Path:
    """Brand the window client and pack it beside a stamp the packaged app unpacks by.

    Returns the folder holding `flet-windows.zip` and `client.json`; the
    version-info file the main executable reuses is written into `work`.
    """
    from flet_cli.__pyinstaller.win_utils import update_flet_view_icon, update_flet_view_version_info

    staging = work / "client"
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(release, staging / "flet")
    viewer = staging / "flet" / "flet.exe"
    if ICON_PATH.is_file():
        update_flet_view_icon(str(viewer), str(ICON_PATH))
    version_file = update_flet_view_version_info(
        exe_path=str(viewer), product_name=APP_NAME, file_description=APP_NAME,
        product_version=__version__, file_version=file_version(), company_name="Riggs",
        copyright="Riggs")
    shutil.copy2(version_file, work / "version_info.txt")
    package = work / "client-package"
    if package.exists():
        shutil.rmtree(package)
    package.mkdir()
    with zipfile.ZipFile(package / "flet-windows.zip", "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted((staging / "flet").rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(staging).as_posix())
    digest = hashlib.sha256((package / "flet-windows.zip").read_bytes()).hexdigest()
    (package / "client.json").write_text(json.dumps({"version": __version__, "sha256": digest}),
                                         encoding="utf-8")
    shutil.rmtree(staging)
    return package


def pyinstaller_arguments(args: argparse.Namespace, version_file: Path) -> list[str]:
    """Assemble the PyInstaller command line for the selected build layout."""
    import flet_cli.__pyinstaller as flet_hooks

    hooks = Path(flet_hooks.__file__).parent
    separator = ";" if os.name == "nt" else ":"
    command = [
        str(ENTRYPOINT), "--noconfirm", "--name", args.name,
        "--distpath", str(DIST_DIR), "--workpath", str(BUILD_DIR / "work"),
        "--specpath", str(BUILD_DIR / "spec"), "--paths", str(SCRIPT_DIR),
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
    parser.add_argument("--rebuild-client", action="store_true",
                        help="compile the window client again even when a matching one exists")
    parser.add_argument("--name", default=APP_NAME, help=f"executable name (default: {APP_NAME})")
    return parser.parse_args()


def main() -> int:
    """Compile and package the window client, then run PyInstaller in this process.

    Flet registers a PyInstaller hook that ships its stock client unless the
    in-process hook configuration names another folder, so the packaged
    client is handed over that way and lands in `flet_desktop/app`.
    """
    if os.name != "nt":
        print("The v3 executable build targets Windows.")
        return 1
    args = parse_args()
    import flet_cli.__pyinstaller.config as hook_config
    import PyInstaller.__main__

    work = BUILD_DIR / "flet"
    work.mkdir(parents=True, exist_ok=True)
    print(f"Building {APP_NAME} v{__version__} ({'onedir' if args.onedir else 'onefile'})")
    client = package_flet_client(build_flet_client(args.rebuild_client), work)
    hook_config.temp_bin_dir = str(client)
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
