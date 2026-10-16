"""
Model viewer.

Flet has no 3D surface, so :mod:`s2kit.media.meshes` rasterises the mesh with
numpy and this control drives it: orbit and zoom sliders, shading mode,
wireframe overlay, up-axis switch, and an info panel with the geometry stats
plus the bone and material lists an SMD carries.

Loads SMD natively (bones, materials) and GLB/OBJ/STL/PLY/DAE through the
optional trimesh dependency.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import flet as ft

from s2kit.media import meshes
from s2kit.ui import common
from s2kit.ui.controls import state
from s2kit.ui.viewers.base import BaseViewer


@ft.control
class ModelViewer(BaseViewer):
    """Orbitable software-rendered mesh preview."""

    display_name: str = state("Model")

    def init(self):
        super().init()
        settings = self.ctx.settings if self.ctx else None
        self._yaw = 35.0
        self._pitch = 20.0
        self._zoom = 1.0
        self._shading = (str(settings.get("model_viewer_shading", "shaded"))
                         if settings else "shaded")
        self._wireframe = (bool(settings.get("model_show_wireframe", False))
                           if settings else False)
        self._up_axis = "z"
        self._mesh: Optional[meshes.MeshData] = None
        self._rendering = False
        self._render_pending = False

    def build_toolbar(self) -> list[ft.Control]:
        self._shading_button = ft.SegmentedButton(
            segments=[ft.Segment(value=mode, label=ft.Text(mode.title(), size=11))
                      for mode in meshes.SHADING_MODES],
            selected=[self._shading],
            allow_empty_selection=False,
            show_selected_icon=False,
            on_change=self._on_shading,
        )
        self._wire_button = ft.IconButton(
            ft.Icons.GRID_ON, tooltip="Wireframe overlay", icon_size=17,
            icon_color=ft.Colors.PRIMARY if self._wireframe else None,
            on_click=self._toggle_wireframe,
        )
        self._up_button = ft.IconButton(
            ft.Icons.SWAP_VERT, tooltip="Toggle up axis (Z-up / Y-up)",
            icon_size=17, on_click=self._toggle_up,
        )
        return [
            self._shading_button,
            self._wire_button,
            self._up_button,
            ft.IconButton(ft.Icons.CENTER_FOCUS_STRONG, tooltip="Reset view",
                          icon_size=17, on_click=self._reset_view),
            ft.IconButton(ft.Icons.REFRESH, tooltip="Reload", icon_size=17,
                          on_click=lambda _e: self.reload()),
        ]

    # ------------------------------------------------------------------
    # Decode / present
    # ------------------------------------------------------------------

    def decode(self, path: Path) -> dict[str, Any]:
        settings = self.ctx.settings
        budget = int(settings.get("model_viewer_max_faces", 60000))
        mesh = meshes.load_mesh(path, max_faces=budget)
        # SMD is Z-up; anything trimesh loaded is usually Y-up already.
        self._up_axis = "z" if path.suffix.lower() == ".smd" else "y"
        png = self._render(mesh)
        return {"mesh": mesh, "png": png}

    def _render(self, mesh: meshes.MeshData) -> bytes:
        settings = self.ctx.settings
        size = int(settings.get("model_viewer_size", 512))
        return meshes.render(
            mesh,
            size=size,
            camera=meshes.Camera(yaw=self._yaw, pitch=self._pitch,
                                 zoom=self._zoom, up_axis=self._up_axis),
            shading=self._shading,
            wireframe=self._wireframe,
        )

    def present(self, payload: dict[str, Any]) -> ft.Control:
        self._mesh = payload["mesh"]
        settings = self.ctx.settings

        self._image = ft.Image(src=payload["png"], fit=ft.BoxFit.CONTAIN,
                              expand=True, gapless_playback=True)

        self._yaw_slider = ft.Slider(
            value=self._yaw, min=-180, max=180, label="Yaw {value}",
            expand=True, on_change=self._on_yaw)
        self._pitch_slider = ft.Slider(
            value=self._pitch, min=-89, max=89, label="Pitch {value}",
            expand=True, on_change=self._on_pitch)
        self._zoom_slider = ft.Slider(
            value=self._zoom, min=0.3, max=4.0, label="Zoom {value}",
            expand=True, on_change=self._on_zoom)

        orbit = ft.Column(
            spacing=0,
            tight=True,
            controls=[
                _slider_row("Yaw", self._yaw_slider),
                _slider_row("Pitch", self._pitch_slider),
                _slider_row("Zoom", self._zoom_slider),
            ],
        )

        side: list[ft.Control] = [
            common.section("Geometry",
                           common.info_table(self._mesh.rows(), settings),
                           icon_name="VIEW_IN_AR"),
            common.section("File", common.info_table(self.file_rows(), settings),
                           icon_name="DESCRIPTION"),
        ]
        if self._mesh.bones:
            side.append(common.collapsible(
                f"Bones ({len(self._mesh.bones)})",
                ft.Column([common.mono("\n".join(self._mesh.bones), settings)],
                          tight=True),
                icon_name="ACCOUNT_TREE",
            ))
        if self._mesh.materials:
            side.append(common.collapsible(
                f"Materials ({len(self._mesh.materials)})",
                ft.Column([common.mono("\n".join(self._mesh.materials), settings)],
                          tight=True),
                icon_name="TEXTURE",
            ))

        return ft.Row(
            expand=True,
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.START,
            controls=[
                ft.Column(
                    expand=True,
                    spacing=6,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                    controls=[
                        ft.Container(
                            expand=True,
                            padding=6,
                            border_radius=ft.BorderRadius.all(8),
                            bgcolor=ft.Colors.with_opacity(
                                0.5, ft.Colors.SURFACE_CONTAINER_LOWEST),
                            border=ft.Border.all(
                                1, ft.Colors.with_opacity(0.12, ft.Colors.OUTLINE)),
                            content=ft.InteractiveViewer(
                                expand=True, min_scale=0.5, max_scale=8,
                                content=self._image),
                        ),
                        orbit,
                    ],
                ),
                ft.Container(
                    width=300,
                    content=ft.Column(side, spacing=10, tight=True,
                                      scroll=ft.ScrollMode.AUTO),
                ),
            ],
        )

    # ------------------------------------------------------------------
    # Re-render (cheap: no re-parse, just the rasteriser)
    # ------------------------------------------------------------------

    def _rerender(self) -> None:
        """Redraw from the cached mesh, coalescing bursts of slider events.

        Dragging a slider emits a change event per pixel of travel. Spawning a
        render for each would queue dozens of frames and deliver them out of
        order; instead one render runs at a time and only the most recent
        request is honoured when it finishes.
        """
        if self._mesh is None:
            return
        if self._rendering:
            self._render_pending = True
            return

        mesh = self._mesh
        self._rendering = True

        def work() -> None:
            try:
                while True:
                    self._render_pending = False
                    try:
                        png = self._render(mesh)
                    except Exception as exc:
                        self._show_error(str(exc))
                        return
                    self._image.src = png
                    _safe_update(self._image)
                    if not self._render_pending:
                        return
            finally:
                self._rendering = False

        self.ctx.run_thread(work)

    def _on_yaw(self, event: ft.ControlEvent) -> None:
        self._yaw = float(event.control.value)
        self._rerender()

    def _on_pitch(self, event: ft.ControlEvent) -> None:
        self._pitch = float(event.control.value)
        self._rerender()

    def _on_zoom(self, event: ft.ControlEvent) -> None:
        self._zoom = float(event.control.value)
        self._rerender()

    def _on_shading(self, event: ft.ControlEvent) -> None:
        for value in event.control.selected or ():
            self._shading = str(value)
            break
        self.ctx.settings.set("model_viewer_shading", self._shading)
        self._rerender()

    def _toggle_wireframe(self, _event: ft.ControlEvent) -> None:
        self._wireframe = not self._wireframe
        self._wire_button.icon_color = ft.Colors.PRIMARY if self._wireframe else None
        _safe_update(self._wire_button)
        self.ctx.settings.set("model_show_wireframe", self._wireframe)
        self._rerender()

    def _toggle_up(self, _event: ft.ControlEvent) -> None:
        self._up_axis = "y" if self._up_axis == "z" else "z"
        self.ctx.toast(f"Up axis: {self._up_axis.upper()}", duration=1500)
        self._rerender()

    def _reset_view(self, _event: ft.ControlEvent) -> None:
        self._yaw, self._pitch, self._zoom = 35.0, 20.0, 1.0
        for slider, value in ((self._yaw_slider, self._yaw),
                              (self._pitch_slider, self._pitch),
                              (self._zoom_slider, self._zoom)):
            slider.value = value
            _safe_update(slider)
        self._rerender()


def _slider_row(label: str, slider: ft.Slider) -> ft.Control:
    return ft.Row(
        spacing=8,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        controls=[
            ft.Container(width=44, content=common.caption(label)),
            slider,
        ],
    )


def _safe_update(control: object) -> None:
    try:
        control.update()  # type: ignore[attr-defined]
    except Exception:
        pass
