"""
The application context handed to every page.

One object carries the page, the settings, the history store, the job runner
and the shared file picker, plus the handful of operations pages keep needing:
toasts, confirmations, file dialogs, navigation and status-bar updates. Pages
never reach for globals.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import flet as ft

from s2kit import bootstrap
from s2kit.core.history import HistoryStore
from s2kit.core.jobs import JobRunner
from s2kit.core.settings import Settings
from s2kit.ui import theme

logger = logging.getLogger(__name__)


class AppContext:
    """Shared services for the shell and every tool page."""

    def __init__(self, page: ft.Page, settings: Settings):
        self.page = page
        self.settings = settings
        self.history = HistoryStore(limit=int(settings.get("history_limit", 50)))
        self.jobs = JobRunner()
        self.file_picker = ft.FilePicker()
        page.services.append(self.file_picker)

        #: Set by the shell so pages can navigate and post status text.
        self.navigate: Callable[[str], None] = lambda tool_id: None
        self.set_status: Callable[[str], None] = lambda text: None
        self.refresh_chrome: Callable[[], None] = lambda: None

    # ------------------------------------------------------------------
    # Preferences
    # ------------------------------------------------------------------

    @property
    def metrics(self) -> theme.Metrics:
        return theme.Metrics.for_settings(self.settings)

    def pref(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    def apply_theme(self) -> None:
        """Restyle the live page from the current appearance preferences."""
        theme.apply(self.page, self.settings)
        self.history.limit = int(self.settings.get("history_limit", 50))
        self.safe_update(self.page)

    # ------------------------------------------------------------------
    # Feedback
    # ------------------------------------------------------------------

    def toast(self, message: str, *, level: str = "INFO", duration: int = 4000,
              action: str = "", on_action: Optional[Callable[[Any], Any]] = None
              ) -> None:
        """Show a transient message at the bottom of the window."""
        colour = theme.level_colour(level)
        bar = ft.SnackBar(
            content=ft.Row(
                spacing=8,
                controls=[
                    ft.Icon(_toast_icon(level), color=colour, size=18),
                    ft.Text(message, expand=True, no_wrap=False),
                ],
            ),
            duration=ft.Duration(milliseconds=duration),
            show_close_icon=True,
            behavior=ft.SnackBarBehavior.FLOATING,
        )
        if action and on_action is not None:
            bar.action = action
            bar.on_action = on_action
        try:
            self.page.show_dialog(bar)
        except Exception:
            logger.debug("Could not show toast: %s", message)

    async def confirm(self, title: str, message: str, *,
                      confirm_label: str = "Continue",
                      danger: bool = False) -> bool:
        """Modal yes/no. Returns True when the user confirmed."""
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

        def close(result: bool):
            def handler(_event: Any = None) -> None:
                if not future.done():
                    future.set_result(result)
                self.page.pop_dialog()
            return handler

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(title),
            content=ft.Text(message, no_wrap=False),
            actions=[
                ft.TextButton("Cancel", on_click=close(False)),
                ft.FilledButton(
                    confirm_label,
                    on_click=close(True),
                    style=ft.ButtonStyle(bgcolor=ft.Colors.RED_700) if danger else None,
                ),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            on_dismiss=lambda _event: (not future.done()
                                       and future.set_result(False)),
        )
        self.page.show_dialog(dialog)
        return await future

    async def prompt(self, title: str, label: str, *, initial: str = "",
                     confirm_label: str = "Save") -> Optional[str]:
        """Modal single-line input. Returns None when cancelled."""
        future: asyncio.Future[Optional[str]] = (
            asyncio.get_running_loop().create_future())
        field = ft.TextField(label=label, value=initial, autofocus=True,
                             dense=True, text_size=12)

        def finish(accept: bool):
            def handler(_event: Any = None) -> None:
                if not future.done():
                    future.set_result((field.value or "").strip()
                                      if accept else None)
                self.page.pop_dialog()
            return handler

        dialog = ft.AlertDialog(
            modal=True,
            title=ft.Text(title),
            content=ft.Container(width=380, content=field),
            actions=[
                ft.TextButton("Cancel", on_click=finish(False)),
                ft.FilledButton(confirm_label, on_click=finish(True)),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            on_dismiss=lambda _event: (not future.done()
                                       and future.set_result(None)),
        )
        field.on_submit = finish(True)
        self.page.show_dialog(dialog)
        return await future

    def show_dialog(self, dialog: ft.DialogControl) -> None:
        self.page.show_dialog(dialog)

    def close_dialog(self) -> None:
        self.page.pop_dialog()

    # ------------------------------------------------------------------
    # File dialogs
    # ------------------------------------------------------------------

    async def pick_file(self, *, title: str = "Select a file",
                        initial_directory: str = "",
                        extensions: Sequence[str] = (),
                        multiple: bool = False) -> list[Path]:
        """Open a file dialog; returns the picked paths (empty when cancelled)."""
        if self.page.web:
            text = await self.prompt(title, "Local file path" + (" (separate paths with ;)" if multiple else ""),
                                     initial=initial_directory, confirm_label="Use path")
            return [Path(value.strip().strip('"')) for value in (text or "").split(";")
                    if value.strip()][:None if multiple else 1]
        file_type = (ft.FilePickerFileType.CUSTOM if extensions
                     else ft.FilePickerFileType.ANY)
        try:
            picked = await self.file_picker.pick_files(
                dialog_title=title,
                initial_directory=initial_directory or None,
                file_type=file_type,
                allowed_extensions=[e.lstrip(".") for e in extensions] or None,
                allow_multiple=multiple,
            )
        except Exception as exc:
            logger.warning("File dialog failed: %s", exc)
            self.toast(f"File dialog failed: {exc}", level="ERROR")
            return []
        return [Path(item.path) for item in (picked or []) if item.path]

    async def pick_directory(self, *, title: str = "Select a folder",
                             initial_directory: str = "") -> Optional[Path]:
        if self.page.web:
            text = await self.prompt(title, "Folder path on this PC", initial=initial_directory,
                                     confirm_label="Use folder")
            return Path(text.strip('"')) if text else None
        try:
            picked = await self.file_picker.get_directory_path(
                dialog_title=title,
                initial_directory=initial_directory or None,
            )
        except Exception as exc:
            logger.warning("Folder dialog failed: %s", exc)
            self.toast(f"Folder dialog failed: {exc}", level="ERROR")
            return None
        return Path(picked) if picked else None

    async def save_file(self, *, title: str = "Save as",
                        file_name: str = "",
                        initial_directory: str = "",
                        extensions: Sequence[str] = ()) -> Optional[Path]:
        if self.page.web:
            text = await self.prompt(title, "Save to this local path",
                                     initial=(str(Path(initial_directory) / file_name)
                                              if initial_directory or file_name else ""),
                                     confirm_label="Use path")
            return Path(text.strip('"')) if text else None
        file_type = (ft.FilePickerFileType.CUSTOM if extensions
                     else ft.FilePickerFileType.ANY)
        try:
            picked = await self.file_picker.save_file(
                dialog_title=title,
                file_name=file_name or None,
                initial_directory=initial_directory or None,
                file_type=file_type,
                allowed_extensions=[e.lstrip(".") for e in extensions] or None,
            )
        except Exception as exc:
            logger.warning("Save dialog failed: %s", exc)
            self.toast(f"Save dialog failed: {exc}", level="ERROR")
            return None
        return Path(picked) if picked else None

    # ------------------------------------------------------------------
    # OS integration
    # ------------------------------------------------------------------

    def reveal(self, path: Any) -> None:
        """Show a file or folder in the OS file manager."""
        if not bootstrap.open_in_explorer(Path(path)):
            self.toast(f"Could not open {path}", level="WARNING")

    def open_external(self, path: Any) -> None:
        """Hand a file to its default application."""
        if not bootstrap.open_with_default_app(Path(path)):
            self.toast(f"Could not open {path}", level="WARNING")

    def copy(self, text: str, *, notice: str = "Copied to clipboard") -> None:
        try:
            self.page.run_task(self.page.clipboard.set, text)
            self.toast(notice, level="SUCCESS", duration=2000)
        except Exception as exc:
            logger.debug("Clipboard write failed: %s", exc)
            self.toast(f"Clipboard unavailable: {exc}", level="WARNING")

    # ------------------------------------------------------------------
    # Update plumbing
    # ------------------------------------------------------------------

    @staticmethod
    def safe_update(control: Any) -> None:
        """`control.update()` that tolerates a control not being mounted yet.

        Pages update sub-controls from event handlers and from the job pump; a
        tab that has not been built yet, or a page that was just swapped out,
        must not raise into either path.
        """
        try:
            control.update()
        except Exception:
            pass

    def run_thread(self, function: Callable[..., Any], *args: Any) -> None:
        """Run a short background task on the page executor (previews, probes)."""
        self.page.run_thread(function, *args)


def _toast_icon(level: str) -> Any:
    return {
        "SUCCESS": ft.Icons.CHECK_CIRCLE,
        "WARNING": ft.Icons.WARNING_AMBER,
        "ERROR": ft.Icons.ERROR,
    }.get(level.upper(), ft.Icons.INFO)
