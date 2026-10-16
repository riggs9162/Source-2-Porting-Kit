"""
The shared Preview pane.

Every tool page has one. It holds a file list - whatever the last run produced,
plus anything the user opens by hand - and shows the selected entry in the
viewer that matches its kind. Because it is part of the base tool page, a new
tool gets a texture viewer, model viewer, sound player and text viewer for
free; all it has to do is call `ctx.artifact(path)` inside its job.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import flet as ft

from s2kit.core.jobs import Artifact
from s2kit.media.probe import MediaKind, kind_for
from s2kit.ui import common, viewers
from s2kit.ui.context import AppContext
from s2kit.ui.controls import is_ready, state


@ft.control
class PreviewPane(ft.Container):
    """File list plus the viewer for the selected file."""

    ctx: Optional[AppContext] = state()
    tool_id: str = state("")

    def init(self):
        self._ready = False
        self._entries: list[Artifact] = []
        self._manual_paths: set[Path] = set()
        self._rail_open = True
        self._rail_width = 260
        self._available_width = 1000
        self._current: Optional[Path] = None
        self._viewer: Optional[viewers.BaseViewer] = None
        self._viewer_cache: dict[MediaKind, viewers.BaseViewer] = {}
        self.expand = True

    def build(self):
        self._list = ft.ListView(expand=True, spacing=2, padding=ft.Padding.all(4))
        self._count = common.caption("")
        self._host = ft.Container(expand=True)
        self._path_field = ft.TextField(
            hint_text="Paste a path, or browse...",
            dense=True, text_size=12, expand=True,
            on_submit=lambda e: self.open_manual(e.control.value),
        )

        left = self._rail = ft.Container(
            width=self._rail_width,
            content=ft.Column(
                expand=True,
                spacing=6,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[
                    ft.Slider(min=180, max=360, value=self._rail_width,
                              label="File list width: {value}px", divisions=18,
                              on_change=self._resize_rail),
                    ft.Row(
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        controls=[
                            common.heading("Files", size=13),
                            ft.Container(expand=True),
                            self._count,
                            ft.IconButton(ft.Icons.ADD, icon_size=15,
                                          tooltip="Open a file to preview",
                                          on_click=lambda _e: self.ctx.page.run_task(
                                              self._browse)),
                            ft.IconButton(ft.Icons.CLEAR_ALL, icon_size=15,
                                          tooltip="Clear the list",
                                          on_click=lambda _e: self.clear()),
                        ],
                    ),
                    ft.Container(
                        expand=True,
                        content=self._list,
                        bgcolor=ft.Colors.with_opacity(
                            0.35, ft.Colors.SURFACE_CONTAINER_HIGHEST),
                        border_radius=ft.BorderRadius.all(8),
                        border=ft.Border.all(
                            1, ft.Colors.with_opacity(0.12, ft.Colors.OUTLINE)),
                    ),
                ],
            ),
        )

        right = ft.Column(
            expand=True,
            spacing=8,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                ft.Row(
                    spacing=4,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    controls=[
                        ft.IconButton(ft.Icons.VIEW_SIDEBAR, tooltip="Show or hide files",
                                      on_click=self._toggle_rail),
                        self._path_field,
                        ft.IconButton(ft.Icons.PLAY_ARROW, icon_size=17,
                                      tooltip="Preview this path",
                                      on_click=lambda _e: self.open_manual(
                                          self._path_field.value)),
                        ft.IconButton(ft.Icons.FOLDER_OPEN, icon_size=17,
                                      tooltip="Reveal in file manager",
                                      on_click=self._reveal),
                        ft.IconButton(ft.Icons.OPEN_IN_NEW, icon_size=17,
                                      tooltip="Open externally",
                                      on_click=self._open_external),
                    ],
                ),
                self._host,
            ],
        )

        self.content = ft.Row(
            expand=True,
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.START,
            controls=[left, right],
        )
        self.on_size_change = self._on_size
        self._ready = True
        self._refresh_list()
        if self._current is not None:
            self.show(self._current)
        else:
            self._show_placeholder()

    # ------------------------------------------------------------------
    # File list
    # ------------------------------------------------------------------

    def add(self, artifact: Artifact, *, select: bool = False,
            generated: bool = False) -> None:
        """Add one file to the list, de-duplicating by path."""
        if not generated:
            self._manual_paths.add(artifact.path)
        if any(entry.path == artifact.path for entry in self._entries):
            self._entries = [artifact if entry.path == artifact.path else entry
                             for entry in self._entries]
            if select or self._current == artifact.path:
                self.show(artifact.path)
            self._refresh_list()
            return
        self._entries.append(artifact)
        self._refresh_list()
        auto = bool(self.ctx.pref("auto_preview", True))
        if select or (auto and self._current is None):
            self.show(artifact.path)

    def begin_run(self) -> None:
        """Discard earlier run outputs while preserving files opened by hand."""
        self._entries = [entry for entry in self._entries
                         if entry.path in self._manual_paths]
        if self._current not in self._manual_paths:
            self._current = None
            self._viewer = None
            if is_ready(self):
                self._path_field.value = ""
                _safe_update(self._path_field)
            self._show_placeholder()
        self._refresh_list()

    def contains(self, path: Path) -> bool:
        """Whether a file is already open, independently of automatic preview."""
        return any(entry.path == path for entry in self._entries)

    def open_manual(self, path: object) -> None:
        """Pin an explicitly opened file independently of generated outputs."""
        if path:
            self.add(Artifact(path=Path(str(path))), select=True)

    def _toggle_rail(self, _event) -> None:
        self._rail_open = not self._rail_open
        self._rail.visible = self._rail_open
        _safe_update(self._rail)

    def _resize_rail(self, event) -> None:
        self._rail_width = float(event.control.value)
        self._rail.width = min(self._rail_width, self._available_width * 0.4)
        _safe_update(self._rail)

    def _on_size(self, event) -> None:
        ft.context.mark_update_called()
        if self._available_width == event.width:
            return
        self._available_width = event.width
        self._rail.width = min(self._rail_width, event.width * 0.4)
        if event.width < 650 and self._rail_open:
            self._rail_open = False
            self._rail.visible = False
        _safe_update(self._rail)

    def set_entries(self, artifacts: list[Artifact]) -> None:
        self._entries = list(artifacts)
        self._refresh_list()
        if self._entries and self._current is None:
            self.show(self._entries[0].path)

    def clear(self) -> None:
        self._entries = []
        self._manual_paths.clear()
        self._current = None
        self._refresh_list()
        self._show_placeholder()

    def _refresh_list(self) -> None:
        if not is_ready(self):
            return
        self._count.value = f"{len(self._entries)}"
        if not self._entries:
            self._list.controls = [
                ft.Container(padding=14,
                             content=common.caption(
                                 "Files a run produces land here. You can also "
                                 "open anything by hand."))
            ]
        else:
            self._list.controls = [self._entry_tile(entry)
                                   for entry in self._entries]
        _safe_update(self._list)
        _safe_update(self._count)

    def _entry_tile(self, entry: Artifact) -> ft.Control:
        kind = kind_for(entry.path)
        selected = self._current == entry.path
        return ft.Container(
            data=str(entry.path),
            padding=ft.Padding.symmetric(horizontal=8, vertical=6),
            border_radius=ft.BorderRadius.all(8),
            bgcolor=(ft.Colors.with_opacity(0.16, ft.Colors.PRIMARY)
                     if selected else None),
            on_click=lambda _e, p=entry.path: self.show(p),
            content=ft.Row(
                spacing=8,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Icon(_icon(kind), size=15,
                            color=ft.Colors.PRIMARY if selected else
                            ft.Colors.ON_SURFACE_VARIANT),
                    ft.Column(
                        spacing=0,
                        tight=True,
                        expand=True,
                        controls=[
                            ft.Text(entry.display_name(), size=12,
                                    weight=ft.FontWeight.W_600, no_wrap=True,
                                    overflow=ft.TextOverflow.ELLIPSIS),
                            # One line, always. A wrapped absolute path turns
                            # every row into a five-line paragraph and the list
                            # stops being scannable.
                            ft.Text(self._entry_note(entry), size=10,
                                    color=ft.Colors.ON_SURFACE_VARIANT,
                                    no_wrap=True,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                    tooltip=str(entry.path)),
                        ],
                    ),
                ],
            ),
        )

    def _entry_note(self, entry: Artifact) -> str:
        """The second line of a row: the tool's note, or where the file lives.

        Paths are shown relative to the shallowest folder the list has in
        common, so a batch of outputs reads as `props/lighting` rather than
        the same 90-character absolute prefix on every row.
        """
        if entry.note:
            return entry.note
        base = self._common_root()
        parent = entry.path.parent
        if base is not None:
            try:
                relative = parent.relative_to(base)
                return str(relative).replace("\\", "/") if str(relative) != "." \
                    else base.name
            except ValueError:
                pass
        return common.truncate_middle(str(parent), 44)

    def _common_root(self) -> Optional[Path]:
        """Deepest folder shared by every entry, if there is more than one."""
        parents = {entry.path.parent for entry in self._entries}
        if len(parents) < 2:
            return None
        try:
            import os

            return Path(os.path.commonpath([str(p) for p in parents]))
        except (ValueError, OSError):
            return None

    # ------------------------------------------------------------------
    # Viewing
    # ------------------------------------------------------------------

    def show(self, path: object) -> None:
        """Display a path, swapping in the viewer that matches its kind."""
        if not path:
            return
        target = Path(str(path))
        self._current = target
        if not is_ready(self):
            return   # build() shows it
        self._path_field.value = str(target)
        _safe_update(self._path_field)

        kind = kind_for(target)
        viewer = self._viewer_cache.get(kind)
        if viewer is None:
            viewer = viewers.create_viewer(self.ctx, target, kind)
            self._viewer_cache[kind] = viewer

        if self._host.content is not viewer:
            self._host.content = viewer
            _safe_update(self._host)
            # A freshly mounted viewer loads from its own build(); an existing
            # one has to be told.
            viewer.path = target
            if getattr(viewer, "_body", None) is not None:
                viewer.load(target)
        else:
            viewer.load(target)

        self._viewer = viewer
        self._refresh_list()

    def current_viewer(self) -> Optional[viewers.BaseViewer]:
        """The viewer currently showing a file, if any."""
        return self._viewer

    def refresh_preferences(self) -> None:
        """Re-decode the current file after a viewer preference changed."""
        if self._viewer is not None and self._current is not None:
            self._viewer.refresh_preferences()

    def _show_placeholder(self) -> None:
        if not is_ready(self):
            return
        self._host.content = common.empty_state(
            "Nothing to preview yet",
            "Run the tool, or open a texture, model, sound or text file to "
            "inspect it here.",
            icon_name="VISIBILITY",
            action=ft.FilledTonalButton(
                "Open a file", icon=ft.Icons.FOLDER_OPEN,
                on_click=lambda _e: self.ctx.page.run_task(self._browse)),
        )
        _safe_update(self._host)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    async def _browse(self) -> None:
        initial = self.ctx.settings.last_dir(self.tool_id, "preview")
        picked = await self.ctx.pick_file(title="Open a file to preview",
                                         initial_directory=initial,
                                         multiple=True)
        for path in picked:
            self.add(Artifact(path=path, label=path.name), select=path == picked[0])
        if picked:
            self.ctx.settings.remember_dir(self.tool_id, "preview",
                                           str(picked[0].parent))

    def _reveal(self, _event: ft.ControlEvent) -> None:
        if self._current is not None:
            self.ctx.reveal(self._current)

    def _open_external(self, _event: ft.ControlEvent) -> None:
        if self._current is not None:
            self.ctx.open_external(self._current)


def _icon(kind: MediaKind):
    from s2kit.ui import theme

    return theme.icon(viewers.kind_icon(kind))


def _safe_update(control: object) -> None:
    try:
        control.update()  # type: ignore[attr-defined]
    except Exception:
        pass
