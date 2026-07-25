"""
Base tool widget that all tools inherit from.

Provides the shared tool chrome (content area over a log pane and a progress
bar) plus the plumbing every tool repeats: a shared Settings instance, path
pickers with last-directory memory, group/action-row shortcuts, and standard
QThread worker wiring (button busy-state, progress -> log, progress_value ->
progress bar, finished -> re-enable).
"""

from datetime import datetime
from typing import Callable, Optional, Sequence

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTextEdit, QLabel, QSplitter, QPushButton,
    QProgressBar
)
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QTextCursor

from app.core.settings import Settings
from app.ui import widgets


class BaseTool(QWidget):
    """Base class for all tool widgets"""

    # Signal emitted when tool wants to update status bar
    status_message = Signal(str)

    def __init__(self, tool_name: str):
        super().__init__()
        self.tool_name = tool_name
        # One Settings instance per tool; used for last-dir memory and any
        # tool-specific persisted keys. Subclasses must not create their own.
        self.settings = Settings()
        # The currently-running QThread worker (see start_worker). Tools with
        # several workers may keep extra references, but this one is what
        # request_cancel() targets.
        self.worker: Optional[QThread] = None
        self.setup_ui()

    def setup_ui(self):
        """Setup the base UI with content area and logger"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Create splitter for content and logger
        self._splitter = QSplitter(Qt.Vertical)

        # Content area (to be populated by subclasses)
        self.content_widget = QWidget()
        self.content_layout = QVBoxLayout(self.content_widget)
        self._splitter.addWidget(self.content_widget)

        # Logger section
        self._logger_widget = QWidget()
        logger_layout = QVBoxLayout(self._logger_widget)
        logger_layout.setContentsMargins(4, 4, 4, 4)

        logger_label = QLabel("Log")
        logger_label.setStyleSheet("font-weight: bold;")
        logger_layout.addWidget(logger_label)

        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumHeight(150)
        self.log_output.setMinimumHeight(80)
        logger_layout.addWidget(self.log_output)

        self._splitter.addWidget(self._logger_widget)

        # Set splitter sizes (content gets more space)
        self._splitter.setStretchFactor(0, 3)
        self._splitter.setStretchFactor(1, 1)

        layout.addWidget(self._splitter)

        # Shared progress bar, pinned under the log pane so it stays visible
        # even in wrapper tools that hide the log. start_worker() shows it
        # (indeterminate) for the duration of a run and hides it on finish;
        # workers with a `progress_value(done, total)` signal drive real
        # percentages through set_progress().
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setMaximumHeight(14)
        layout.addWidget(self.progress_bar)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def log(self, message: str, level: str = "INFO"):
        """
        Add a message to the log

        Args:
            message: Message to log
            level: Log level (INFO, WARNING, ERROR, SUCCESS)
        """
        import html
        timestamp = datetime.now().strftime("%H:%M:%S")

        # Color based on level
        color_map = {
            "INFO": "#d4d4d4",
            "WARNING": "#ffcc00",
            "ERROR": "#ff6b6b",
            "SUCCESS": "#4ec9b0"
        }
        color = color_map.get(level, "#d4d4d4")

        # Escape HTML entities in the message
        escaped_message = html.escape(message)

        formatted_message = f'<span style="color: #808080;">[{timestamp}]</span> ' \
                          f'<span style="color: {color};">[{level}]</span> {escaped_message}'

        self.log_output.append(formatted_message)

        # Auto-scroll to bottom
        cursor = self.log_output.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.log_output.setTextCursor(cursor)

    def clear_log(self):
        """Clear the log output"""
        self.log_output.clear()

    def emit_status(self, message: str):
        """Emit a status bar message"""
        self.status_message.emit(message)

    def set_progress(self, done: int, total: int):
        """Update the shared progress bar (slot for worker `progress_value`).

        ``total <= 0`` switches the bar to indeterminate "busy" mode — the
        state every run starts in until a worker reports a real count.
        """
        if total <= 0:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(max(0, min(done, total)))

    def set_log_visible(self, visible: bool):
        """Show or hide the shared log pane (wrapper tools hide it)."""
        self._logger_widget.setVisible(visible)

    # ------------------------------------------------------------------
    # UI construction helpers
    # ------------------------------------------------------------------

    def create_picker(self, mode: str, field: str, **kwargs) -> widgets.PathPickerRow:
        """A PathPickerRow bound to this tool: last-dir memory + pick logging.

        ``field`` is a short identifier for the input ("input", "output",
        "mask", ...) — it both keys the remembered directory
        ("<tool name>/<field>") and labels the log line on pick.
        """
        kwargs.setdefault("on_picked",
                          lambda path, f=field: self.log(f"Selected {f}: {path}", "INFO"))
        return widgets.PathPickerRow(
            mode,
            settings=self.settings,
            remember_key=f"{self.tool_name}/{field}",
            **kwargs,
        )

    def add_group(self, title: str, *items) -> None:
        """Append a titled group box of widgets/layouts to the content area."""
        self.content_layout.addWidget(widgets.make_group(title, *items))

    def add_action_row(self, *buttons: QPushButton) -> None:
        """Append the standard right-aligned action-button row."""
        self.content_layout.addLayout(widgets.action_row(*buttons))

    def make_run_button(self, text: str, on_click: Callable, *,
                        min_width: int = 120) -> QPushButton:
        """The tool's primary action button (start disabled via start_worker)."""
        button = QPushButton(text)
        button.setMinimumWidth(min_width)
        button.clicked.connect(on_click)
        return button

    def make_cancel_button(self, on_click: Callable = None) -> QPushButton:
        """A Cancel button, disabled until a worker runs (see start_worker)."""
        button = QPushButton("Cancel")
        button.setEnabled(False)
        button.clicked.connect(on_click or self.request_cancel)
        return button

    # ------------------------------------------------------------------
    # Worker wiring
    # ------------------------------------------------------------------

    def start_worker(self, worker: QThread, on_finished: Callable = None, *,
                     progress_slot: Callable = None,
                     finished_signal=None,
                     busy_widgets: Sequence[QWidget] = (),
                     cancel_button: QWidget = None,
                     status: str = None,
                     clear_log: bool = True,
                     progress_total: int = 0):
        """Start a QThread worker with the standard wiring.

        Keeps the reference (``self.worker``), connects the worker's
        ``progress`` signal to the tool log (or ``progress_slot``), disables
        ``busy_widgets`` and enables ``cancel_button`` for the duration, and
        calls ``on_finished(*signal_args)`` after the UI is re-enabled.

        The shared progress bar is shown while the worker runs and hidden
        again on finish — indeterminate by default, or seeded at
        ``0/progress_total`` when the item count is known up front. Workers
        with a ``progress_value(done, total)`` signal get it auto-connected
        to ``set_progress`` so the bar shows real percentages; tools whose
        workers report ``(idx, total, message)`` instead call
        ``set_progress`` from their ``progress_slot``.

        ``finished_signal`` overrides ``worker.finished`` for workers with a
        differently-named completion signal.
        """
        self.worker = worker
        if clear_log:
            self.clear_log()
        if status:
            self.emit_status(status)

        # Workers with a differently-named progress signal connect it
        # themselves before calling start_worker.
        if hasattr(worker, "progress"):
            worker.progress.connect(progress_slot or self.log)
        if hasattr(worker, "progress_value"):
            worker.progress_value.connect(self.set_progress)

        self.set_progress(0, progress_total)
        self.progress_bar.setVisible(True)

        for widget in busy_widgets:
            widget.setEnabled(False)
        if cancel_button is not None:
            cancel_button.setEnabled(True)

        def _finish(*args):
            self.progress_bar.setVisible(False)
            for widget in busy_widgets:
                widget.setEnabled(True)
            if cancel_button is not None:
                cancel_button.setEnabled(False)
            if on_finished is not None:
                on_finished(*args)

        signal = finished_signal if finished_signal is not None else worker.finished
        signal.connect(_finish)
        worker.start()

    def request_cancel(self, worker: QThread = None):
        """Ask the running worker to stop at its next interruption check."""
        worker = worker or self.worker
        if worker is not None and worker.isRunning():
            worker.requestInterruption()
            self.log("Cancellation requested...", "WARNING")
