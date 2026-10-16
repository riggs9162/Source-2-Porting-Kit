"""
Source 2 Porting Kit v3 - bootstrapper.

The single entry point for the Flet rebuild. Run it with a bare interpreter
(`python v3/run.py`) and it takes care of everything the app needs before the
first window appears: interpreter version check, import path wiring, a
dependency audit (with optional self-install), file + console logging, global
exception hooks, then it hands control to the Flet UI.

Useful flags:
    --check         print the environment report and exit (no window)
    --install       pip-install any missing required dependencies first
    --headless      start the app with a hidden window (used by smoke tests)
    --web           serve the UI in a browser instead of a desktop window
    --tool <id>     open straight into a tool page
    --log-level     DEBUG / INFO / WARNING / ERROR (default INFO for console)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# The package lives next to this file; make it importable no matter what the
# working directory is. Done before any s2kit import.
V3_ROOT = Path(__file__).resolve().parent
if str(V3_ROOT) not in sys.path:
    sys.path.insert(0, str(V3_ROOT))

MIN_PYTHON = (3, 10)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Source 2 Porting Kit v3 (Flet)",
    )
    parser.add_argument("--check", action="store_true",
                        help="print the environment report and exit")
    parser.add_argument("--install", action="store_true",
                        help="pip-install missing required dependencies without asking")
    parser.add_argument("--install-optional", action="store_true",
                        help="also install the optional dependencies")
    parser.add_argument("--headless", action="store_true",
                        help="run with a hidden window (smoke tests)")
    interface = parser.add_mutually_exclusive_group()
    interface.add_argument("--desktop", action="store_true",
                           help="open the desktop app without the startup chooser")
    interface.add_argument("--web", action="store_true",
                        help="serve the UI in a web browser instead of a desktop window")
    parser.add_argument("--port", type=int, default=0,
                        help="port for --web (0 = auto)")
    parser.add_argument("--tool", default="",
                        help="tool id to open on startup")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="console log level (the log file always gets DEBUG)")
    parser.add_argument("--self-test", action="store_true",
                        help="build the UI headlessly, assert it mounted, then exit")
    return parser.parse_args(argv)


def _ensure_python() -> None:
    if sys.version_info < MIN_PYTHON:
        have = ".".join(str(p) for p in sys.version_info[:3])
        want = ".".join(str(p) for p in MIN_PYTHON)
        raise SystemExit(f"Python {want}+ is required to run the kit (found {have}).")


def _choose_console_interface(reason: Exception) -> str | None:
    """Offer a console chooser when Tk is unavailable, defaulting to desktop without a terminal."""
    print(f"Startup chooser unavailable: {reason}", file=sys.stderr)
    print("Use --desktop or --web to skip the startup chooser.", file=sys.stderr)
    if sys.stdin is None or not sys.stdin.isatty():
        print("No interactive console; opening Desktop.", file=sys.stderr)
        return "desktop"

    while True:
        try:
            answer = input("Open [1] Desktop (default), [2] Browser (localhost), "
                           "or [q] Cancel: ").strip().lower()
        except EOFError:
            return "desktop"
        except KeyboardInterrupt:
            return None
        if answer in ("", "1", "desktop", "d"):
            return "desktop"
        if answer in ("2", "browser", "web", "b", "w"):
            return "web"
        if answer in ("q", "quit", "cancel"):
            return None
        print("Choose 1 for Desktop, 2 for Browser, or q to cancel.")


def choose_interface() -> str | None:
    """Choose a local interface, falling back to the console if optional Tk support is unavailable."""
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError as exc:
        return _choose_console_interface(exc)

    choice = None
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        return _choose_console_interface(exc)
    root.title("Source 2 Porting Kit")
    root.resizable(False, False)
    frame = ttk.Frame(root, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="Open Source 2 Porting Kit", font=("Segoe UI", 15, "bold")).pack(anchor="w")
    ttk.Label(frame, text="Choose how you want to use the tools.").pack(anchor="w", pady=(8, 18))

    def select(value: str) -> None:
        nonlocal choice
        choice = value
        root.destroy()

    desktop = ttk.Button(frame, text="Desktop", command=lambda: select("desktop"))
    desktop.pack(fill="x", pady=4)
    browser = ttk.Button(frame, text="Browser (localhost)", command=lambda: select("web"))
    browser.pack(fill="x", pady=4)
    ttk.Label(frame, text="Browser mode runs on this PC. Keep the launcher running.").pack(anchor="w", pady=(14, 0))
    root.bind("<Escape>", lambda _e: root.destroy())
    desktop.bind("<Return>", lambda _e: select("desktop"))
    browser.bind("<Return>", lambda _e: select("web"))
    desktop.focus_set()
    root.mainloop()
    return choice


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    _ensure_python()

    # Imported after the path fix-up; bootstrap itself only uses the stdlib so
    # it stays importable on a bare interpreter with nothing installed.
    from s2kit import bootstrap

    report = bootstrap.probe()

    if args.check:
        print(bootstrap.format_report(report))
        return 0 if report.satisfied else 1

    if not (args.desktop or args.web or args.headless or args.self_test):
        selected = choose_interface()
        if selected is None:
            return 0
        args.web = selected == "web"

    if not report.satisfied:
        missing = [d.package for d in report.missing_required]
        print(bootstrap.format_report(report))
        should_install = args.install or bootstrap.confirm_install(missing)
        if not should_install:
            print("\nCannot start without the required dependencies. "
                  "Install them with:\n    "
                  f"{sys.executable} -m pip install {' '.join(missing)}")
            return 1
        if not bootstrap.pip_install(missing):
            return 1
        report = bootstrap.probe()
        if not report.satisfied:
            print("Dependencies are still missing after the install attempt.")
            return 1

    if args.install_optional:
        optional = [d.package for d in report.missing_optional]
        if optional:
            bootstrap.pip_install(optional)

    log_file = bootstrap.setup_logging(console_level=args.log_level)
    bootstrap.install_exception_hooks()
    bootstrap.log_environment(report)

    from s2kit.app import launch

    return launch(
        headless=args.headless,
        self_test=args.self_test,
        web=args.web,
        port=args.port,
        startup_tool=args.tool,
        log_file=log_file,
    )


if __name__ == "__main__":
    raise SystemExit(main())
