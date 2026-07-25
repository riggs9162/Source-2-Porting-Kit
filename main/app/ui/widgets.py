"""
Shared UI building blocks for tool tabs.

Every tool tab is built from the same handful of parts: a path row (line edit +
Browse button), option group boxes, a right-aligned action-button row, and an
informational note. This module owns those parts so individual tools stay
declarative — they say *what* they need, not how to wire a QFileDialog.

All file dialogs opened here remember their last-used directory through
``Settings.get_last_dir()`` / ``set_last_dir()`` when given a ``remember_key``
(convention: ``"<Tool Name>/<field>"``). Tools normally obtain pickers via
``BaseTool.create_picker()``, which fills in the settings object and pick
logging automatically.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Union

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog, QGroupBox, QHBoxLayout, QLabel, QLayout, QLineEdit,
    QPushButton, QVBoxLayout, QWidget,
)


# ---------------------------------------------------------------------------
# File-dialog helpers (usable without a PathPickerRow, e.g. menu actions)
# ---------------------------------------------------------------------------

def _start_dir(settings, remember_key: Optional[str], fallback: str = "") -> str:
    """Resolve the directory a dialog should open in."""
    if settings is not None and remember_key:
        remembered = settings.get_last_dir(remember_key)
        if remembered:
            return remembered
    if fallback:
        p = Path(fallback)
        return str(p if p.is_dir() else p.parent)
    return ""


def _remember(settings, remember_key: Optional[str], picked_path: str):
    """Persist the directory of a picked path for the next dialog."""
    if settings is None or not remember_key or not picked_path:
        return
    p = Path(picked_path)
    settings.set_last_dir(remember_key, str(p if p.is_dir() else p.parent))


def browse_dir(parent, title: str, *, settings=None, remember_key: str = None,
               fallback: str = "") -> str:
    """Open a directory picker. Returns the path or '' if cancelled."""
    path = QFileDialog.getExistingDirectory(
        parent, title, _start_dir(settings, remember_key, fallback))
    if path:
        _remember(settings, remember_key, path)
    return path


def browse_open_file(parent, title: str, *, file_filter: str = "All files (*.*)",
                     settings=None, remember_key: str = None, fallback: str = "") -> str:
    """Open a single-file picker. Returns the path or '' if cancelled."""
    path, _ = QFileDialog.getOpenFileName(
        parent, title, _start_dir(settings, remember_key, fallback), file_filter)
    if path:
        _remember(settings, remember_key, path)
    return path


def browse_open_files(parent, title: str, *, file_filter: str = "All files (*.*)",
                      settings=None, remember_key: str = None, fallback: str = "") -> List[str]:
    """Open a multi-file picker. Returns a (possibly empty) list of paths."""
    paths, _ = QFileDialog.getOpenFileNames(
        parent, title, _start_dir(settings, remember_key, fallback), file_filter)
    if paths:
        _remember(settings, remember_key, paths[0])
    return paths


def browse_save_file(parent, title: str, *, file_filter: str = "All files (*.*)",
                     settings=None, remember_key: str = None, fallback: str = "",
                     default_suffix: str = "") -> str:
    """Open a save-file picker. Appends ``default_suffix`` (e.g. '.png') if the
    chosen name lacks it. Returns the path or '' if cancelled."""
    path, _ = QFileDialog.getSaveFileName(
        parent, title, _start_dir(settings, remember_key, fallback), file_filter)
    if not path:
        return ""
    if default_suffix and not path.lower().endswith(default_suffix.lower()):
        path += default_suffix
    _remember(settings, remember_key, path)
    return path


# ---------------------------------------------------------------------------
# Path picker row
# ---------------------------------------------------------------------------

class PathPickerRow(QWidget):
    """A one-line path input: optional label, QLineEdit, and a Browse button.

    ``mode`` is one of ``'dir'``, ``'open_file'``, ``'save_file'``. The Browse
    dialog seeds from the remembered last directory (via ``remember_key``) or,
    failing that, from the current text. ``on_picked(path)`` fires after a
    successful pick — tools use it to log the selection.
    """

    changed = Signal(str)  # emitted with the new path after a successful pick

    def __init__(self, mode: str, *, placeholder: str = "", label: str = None,
                 dialog_title: str = None, file_filter: str = "All files (*.*)",
                 button_text: str = "Browse...", settings=None,
                 remember_key: str = None, default_suffix: str = "",
                 on_picked: Callable[[str], None] = None):
        super().__init__()
        if mode not in ("dir", "open_file", "save_file"):
            raise ValueError(f"Unsupported picker mode: {mode}")
        self._mode = mode
        self._dialog_title = dialog_title or (label or "Select Path").rstrip(":")
        self._file_filter = file_filter
        self._settings = settings
        self._remember_key = remember_key
        self._default_suffix = default_suffix
        self._on_picked = on_picked

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        if label:
            row.addWidget(QLabel(label))
        self.edit = QLineEdit()
        if placeholder:
            self.edit.setPlaceholderText(placeholder)
        row.addWidget(self.edit)
        self.button = QPushButton(button_text)
        self.button.clicked.connect(self._browse)
        row.addWidget(self.button)

    # -- public API ---------------------------------------------------------

    def path(self) -> str:
        """Current text, stripped."""
        return self.edit.text().strip()

    def set_path(self, path: str):
        self.edit.setText(path)

    def _browse(self):
        kwargs = dict(settings=self._settings, remember_key=self._remember_key,
                      fallback=self.path())
        if self._mode == "dir":
            path = browse_dir(self, self._dialog_title, **kwargs)
        elif self._mode == "open_file":
            path = browse_open_file(self, self._dialog_title,
                                    file_filter=self._file_filter, **kwargs)
        else:
            path = browse_save_file(self, self._dialog_title,
                                    file_filter=self._file_filter,
                                    default_suffix=self._default_suffix, **kwargs)
        if not path:
            return
        self.edit.setText(path)
        if self._on_picked:
            self._on_picked(path)
        self.changed.emit(path)


# ---------------------------------------------------------------------------
# Layout helpers
# ---------------------------------------------------------------------------

_GroupItem = Union[QWidget, QLayout]


def make_group(title: str, *items: _GroupItem) -> QGroupBox:
    """A QGroupBox with a vertical layout holding widgets and/or sub-layouts."""
    group = QGroupBox(title)
    layout = QVBoxLayout()
    for item in items:
        if isinstance(item, QLayout):
            layout.addLayout(item)
        else:
            layout.addWidget(item)
    group.setLayout(layout)
    return group


def hbox(*items: _GroupItem, stretch_after: bool = False) -> QHBoxLayout:
    """A horizontal layout of widgets/layouts, optionally stretch-padded right."""
    row = QHBoxLayout()
    for item in items:
        if isinstance(item, QLayout):
            row.addLayout(item)
        else:
            row.addWidget(item)
    if stretch_after:
        row.addStretch()
    return row


def action_row(*buttons: QWidget) -> QHBoxLayout:
    """The standard right-aligned action-button row (stretch, then buttons)."""
    row = QHBoxLayout()
    row.addStretch()
    for button in buttons:
        row.addWidget(button)
    return row


def note_label(text: str) -> QLabel:
    """A word-wrapped informational note styled as a callout box."""
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet(
        "color: #FFA500; padding: 10px; background-color: #2b2b2b; border-radius: 4px;")
    return label
