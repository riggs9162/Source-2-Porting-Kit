"""
Environment bootstrap: dependency audit, logging, crash handlers.

Stdlib only. This module has to be importable on a bare interpreter that has
none of the app's dependencies installed, because it is what discovers and
installs them.
"""

from __future__ import annotations

import importlib.util
import logging
import logging.handlers
import os
import shutil
import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from s2kit.core import paths

logger = logging.getLogger("s2kit.bootstrap")


# ----------------------------------------------------------------------
# Dependency catalogue
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Dependency:
    """A Python package the kit imports, and why."""

    module: str
    package: str
    purpose: str
    required: bool = True


DEPENDENCIES: tuple[Dependency, ...] = (
    Dependency("flet", "flet>=0.86", "the UI framework itself"),
    Dependency("flet_desktop", "flet-desktop>=0.86", "native desktop window host"),
    Dependency("flet_web", "flet-web>=0.86", "local browser interface"),
    Dependency("PIL", "Pillow>=10", "image decode/encode for texture previews"),
    Dependency("numpy", "numpy>=1.24", "pixel + mesh math, waveform peaks"),
    Dependency("sourcepp", "sourcepp>=2025.11.5",
               "VTF read/write (texture viewer, encoders)", required=False),
    Dependency("trimesh", "trimesh>=4.0",
               "GLB/OBJ/STL loading for the model viewer", required=False),
    Dependency("scipy", "scipy>=1.10",
               "convex hulls for collision meshes (trimesh/qhull)",
               required=False),
    Dependency("srctools", "srctools>=2.6",
               "Source 1 formats (VMT/KeyValues/MDL)", required=False),
)

# Command-line programs the pipelines shell out to. Absence is never fatal at
# startup - the tools that need them report it themselves - but the report
# tells the user up front what is wired up.
EXTERNAL_TOOLS: tuple[tuple[str, str], ...] = (
    ("ffmpeg", "audio decode/convert (waveforms, OGG/WAV conversion)"),
    ("ffprobe", "audio metadata probing"),
    ("studiomdl", "Source 1 model compiles"),
    ("Source2Viewer-CLI", "Source 2 asset extraction (VRF)"),
)


@dataclass
class DependencyStatus:
    """Result of probing one :class:`Dependency`."""

    dependency: Dependency
    found: bool
    version: str = ""


@dataclass
class EnvironmentReport:
    """Everything the bootstrapper learned about this machine."""

    python: str = field(default_factory=lambda: sys.version.split()[0])
    executable: str = field(default_factory=lambda: sys.executable)
    statuses: list[DependencyStatus] = field(default_factory=list)
    external: dict[str, str] = field(default_factory=dict)

    @property
    def missing_required(self) -> list[Dependency]:
        return [s.dependency for s in self.statuses
                if s.dependency.required and not s.found]

    @property
    def missing_optional(self) -> list[Dependency]:
        return [s.dependency for s in self.statuses
                if not s.dependency.required and not s.found]

    @property
    def satisfied(self) -> bool:
        return not self.missing_required


def _module_version(module_name: str) -> str:
    """Best-effort version string for an installed module, '' if unknown."""
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - Python < 3.8
        return ""
    # Distribution names rarely match import names, so try both spellings.
    candidates = {module_name, module_name.replace("_", "-")}
    if module_name == "PIL":
        candidates.add("Pillow")
    for name in candidates:
        try:
            return version(name)
        except PackageNotFoundError:
            continue
        except Exception:
            return ""
    return ""


def probe() -> EnvironmentReport:
    """Audit the interpreter: which dependencies and external tools exist."""
    report = EnvironmentReport()
    for dep in DEPENDENCIES:
        found = importlib.util.find_spec(dep.module) is not None
        report.statuses.append(
            DependencyStatus(dep, found, _module_version(dep.module) if found else "")
        )
    for name, _purpose in EXTERNAL_TOOLS:
        report.external[name] = shutil.which(name) or ""
    return report


def format_report(report: EnvironmentReport) -> str:
    """Render the audit as a human-readable block for the console."""
    lines: list[str] = []
    lines.append("Source 2 Porting Kit v3 - environment")
    lines.append("=" * 68)
    lines.append(f"Python      {report.python}")
    lines.append(f"Interpreter {report.executable}")
    lines.append(f"App root    {paths.app_root()}")
    lines.append(f"Config      {paths.config_dir()}")
    lines.append(f"Logs        {paths.logs_dir()}")
    lines.append("")

    lines.append("Python packages")
    lines.append("-" * 68)
    for status in report.statuses:
        dep = status.dependency
        mark = "OK  " if status.found else ("MISS" if dep.required else "opt ")
        version = f" {status.version}" if status.version else ""
        tag = "" if dep.required else " (optional)"
        lines.append(f"  [{mark}] {dep.module}{version}{tag}")
        if not status.found:
            lines.append(f"         needed for: {dep.purpose}")
    lines.append("")

    lines.append("External programs (PATH)")
    lines.append("-" * 68)
    for name, purpose in EXTERNAL_TOOLS:
        found = report.external.get(name, "")
        mark = "OK  " if found else "----"
        lines.append(f"  [{mark}] {name:<20} {found or purpose}")
    lines.append("")

    if report.satisfied:
        lines.append("Status: ready to launch.")
    else:
        names = ", ".join(d.package for d in report.missing_required)
        lines.append(f"Status: MISSING REQUIRED PACKAGES -> {names}")
    return "\n".join(lines)


def confirm_install(packages: list[str]) -> bool:
    """Ask the user whether to pip-install missing packages (non-tty = no)."""
    if not packages:
        return False
    if not sys.stdin or not sys.stdin.isatty():
        return False
    prompt = f"\nInstall the missing packages now ({', '.join(packages)})? [y/N] "
    try:
        return input(prompt).strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False


def pip_install(packages: list[str]) -> bool:
    """Run `pip install` for the given requirement specs in this interpreter."""
    if not packages:
        return True
    if getattr(sys, "frozen", False):
        print("The packaged app cannot install Python packages; download a complete build.")
        return False
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", *packages]
    print(f"\n> {' '.join(cmd)}\n", flush=True)
    try:
        return subprocess.call(cmd) == 0
    except OSError as exc:
        print(f"pip could not be started: {exc}")
        return False


# ----------------------------------------------------------------------
# Logging
# ----------------------------------------------------------------------

LOG_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"


def setup_logging(console_level: str = "INFO") -> Path:
    """Wire up a rotating file log plus a console log; return the file path."""
    log_file = paths.logs_dir() / "app.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(file_handler)

    if sys.stdout is not None:
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(getattr(logging, console_level.upper(), logging.INFO))
        console.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(console)

    # Flet's transport logs every serialised control patch at DEBUG, which
    # buries our own lines and turns a single session into megabytes of log.
    for name in ("flet", "flet_desktop", "flet_transport", "flet_controls",
                 "flet_runtime", "PIL"):
        logging.getLogger(name).setLevel(logging.INFO)
    return log_file


def log_environment(report: EnvironmentReport) -> None:
    """Record the audit in the log file so crash reports carry it."""
    for line in format_report(report).splitlines():
        logger.debug(line)


def use_bundled_client() -> Optional[Path]:
    """Point Flet at the window client a packaged build ships instead of its shared stock client.

    The archive is unpacked once per build into local app data, keyed by its
    digest. From source, the client `build_exe.py` compiled into `build/` is
    used when present. An existing ``FLET_VIEW_PATH`` is left alone. Returns
    the client folder, or None when Flet's stock client will run.
    """
    import json
    import zipfile

    if os.environ.get("FLET_VIEW_PATH"):
        return None
    bundled = getattr(sys, "_MEIPASS", None)
    if not bundled:
        compiled = paths.app_root() / "build" / "flet-client" / "flet"
        if not (compiled / "flet.exe").is_file():
            return None
        os.environ["FLET_VIEW_PATH"] = str(compiled)
        return compiled
    package = Path(bundled) / "flet_desktop" / "app"
    stamp = package / "client.json"
    if not stamp.is_file():
        return None
    digest = json.loads(stamp.read_text(encoding="utf-8"))["sha256"]
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Source 2 Porting Kit" / "client"
    target = root / digest[:16]
    if not (target / "flet" / "flet.exe").is_file():
        staging = root / f"{digest[:16]}.{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        with zipfile.ZipFile(package / "flet-windows.zip") as archive:
            archive.extractall(staging)
        shutil.rmtree(target, ignore_errors=True)
        try:
            staging.rename(target)
        except OSError:
            shutil.rmtree(staging, ignore_errors=True)
            if not (target / "flet" / "flet.exe").is_file():
                raise
    os.environ["FLET_VIEW_PATH"] = str(target / "flet")
    return target / "flet"


# ----------------------------------------------------------------------
# Crash handling
# ----------------------------------------------------------------------

_LAST_ERROR: list[str] = []


def last_error() -> str:
    """The most recent uncaught traceback, for showing inside the UI."""
    return _LAST_ERROR[-1] if _LAST_ERROR else ""


def _record(kind: str, text: str) -> None:
    _LAST_ERROR.append(text)
    del _LAST_ERROR[:-5]
    logger.critical("%s\n%s", kind, text)
    if sys.stderr is not None:
        sys.stderr.write(f"\n{'=' * 68}\n{kind}\n{'=' * 68}\n{text}\n")


def install_exception_hooks() -> None:
    """Route uncaught exceptions (main thread and workers) into the log."""

    def handle(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        _record("UNCAUGHT EXCEPTION",
                "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))

    def handle_thread(args):
        _record(f"UNCAUGHT EXCEPTION IN THREAD {args.thread.name}",
                "".join(traceback.format_exception(
                    args.exc_type, args.exc_value, args.exc_traceback)))

    sys.excepthook = handle
    threading.excepthook = handle_thread


def open_in_explorer(path: Path) -> bool:
    """Reveal a file or folder in the OS file manager. True if it was opened."""
    target = Path(path)
    if not target.exists():
        return False
    try:
        if sys.platform == "win32":
            if target.is_dir():
                os.startfile(str(target))  # noqa: S606 - intended shell open
            else:
                subprocess.Popen(["explorer", "/select,", str(target)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R" if target.is_file() else "", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target if target.is_dir()
                                              else target.parent)])
        return True
    except OSError as exc:
        logger.warning("Could not reveal %s: %s", target, exc)
        return False


def open_with_default_app(path: Path) -> bool:
    """Hand a file to whatever the OS considers its default application."""
    target = Path(path)
    if not target.exists():
        return False
    try:
        if sys.platform == "win32":
            os.startfile(str(target))  # noqa: S606 - intended shell open
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return True
    except OSError as exc:
        logger.warning("Could not open %s: %s", target, exc)
        return False


def module_available(name: str) -> bool:
    """Whether an optional dependency can be imported right now."""
    return importlib.util.find_spec(name) is not None


def external_tool(name: str) -> Optional[str]:
    """Absolute path to an external program on PATH, or None."""
    return shutil.which(name)
