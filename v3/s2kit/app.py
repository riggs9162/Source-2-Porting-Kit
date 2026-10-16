"""
Flet application entry point.

`launch()` is what the bootstrapper calls once the environment is known good.
It wires the page (title, theme, window geometry, keyboard shortcuts, close
handling), imports the tool package so the registry populates, then mounts the
shell.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import flet as ft

from s2kit import APP_NAME, __version__
from s2kit.core import paths, registry
from s2kit.core.settings import Settings
from s2kit.ui import theme
from s2kit.ui.context import AppContext
from s2kit.ui.shell import HOME_ID, PREFERENCES_ID, AppShell

logger = logging.getLogger(__name__)


def _load_tools() -> int:
    """Import the tool package so every @tool decorator runs."""
    from s2kit import tools

    count = tools.discover()
    logger.info("Registered %d tool(s)", count)
    return count


def build(page: ft.Page, *, startup_tool: str = "",
          self_test: bool = False) -> AppShell:
    """Construct the whole UI on a page. Returns the mounted shell."""
    settings = Settings()

    page.title = APP_NAME
    theme.apply(page, settings)
    page.padding = 0
    page.spacing = 0

    _restore_window(page, settings)
    _load_tools()

    ctx = AppContext(page, settings)
    shell = AppShell(ctx=ctx)
    page.add(shell)

    # Preferences that other pages must re-read live are pushed out here; the
    # theme itself is applied by the preferences page directly.
    def on_setting_changed(key: str, _value: object) -> None:
        if key in _VIEWER_KEYS or key in _LOG_KEYS:
            current = shell.current_page()
            if current is not None and hasattr(current, "on_preferences_changed"):
                current.on_preferences_changed()
        if key in ("compact_density", "show_tool_icons", "sidebar_sort",
                   "favourite_tools"):
            shell.refresh_chrome()

    settings.subscribe(on_setting_changed)

    page.on_keyboard_event = shell.handle_key
    page.on_close = lambda _event: _on_close(page, settings, ctx)
    if hasattr(page.window, "on_event"):
        page.window.on_event = lambda event: _on_window_event(event, page, settings)

    target = _startup_target(startup_tool, settings)
    shell.navigate(target)
    logger.info("UI ready (%d tools, startup page %s)", registry.count(), target)

    if self_test:
        _self_test(page, shell)
    return shell


def launch(*, headless: bool = False, self_test: bool = False,
           web: bool = False, port: int = 0, startup_tool: str = "",
           log_file: Optional[Path] = None) -> int:
    """Run the app. Returns a process exit code."""
    logger.info("%s v%s starting (headless=%s web=%s)", APP_NAME, __version__,
                headless, web)

    view = ft.AppView.FLET_APP
    if web:
        view = ft.AppView.WEB_BROWSER
    elif headless or self_test:
        view = ft.AppView.FLET_APP_HIDDEN

    def main(page: ft.Page) -> None:
        try:
            build(page, startup_tool=startup_tool, self_test=self_test)
        except Exception as exc:
            logger.critical("UI failed to build", exc_info=True)
            _show_startup_error(page, exc, log_file)

    try:
        ft.run(main, view=view, host="127.0.0.1", port=port,
               assets_dir=str(paths.assets_dir()))
    except KeyboardInterrupt:
        logger.info("Interrupted")
        return 130
    except Exception:
        logger.critical("Fatal error running the app", exc_info=True)
        return 1
    return 0


# ----------------------------------------------------------------------
# Window state
# ----------------------------------------------------------------------

_VIEWER_KEYS = {
    "viewer_bg", "image_smooth", "show_alpha_channel", "model_viewer_size",
    "model_viewer_shading", "model_show_wireframe", "model_viewer_max_faces",
    "waveform_height", "waveform_rms", "text_viewer_tab_width",
    "text_viewer_line_numbers", "auto_preview", "preview_max_mb",
}

_LOG_KEYS = {
    "log_buffer_lines", "log_autoscroll", "log_show_timestamps", "log_wrap",
    "log_min_level", "mono_font", "text_scale",
}


def _restore_window(page: ft.Page, settings: Settings) -> None:
    if not bool(settings.get("remember_window_size", True)):
        page.window.width = 1440
        page.window.height = 900
        page.window.min_width = 1024
        page.window.min_height = 680
        return
    page.window.width = float(settings.get("window_width", 1440))
    page.window.height = float(settings.get("window_height", 900))
    page.window.min_width = 1024
    page.window.min_height = 680
    top = settings.get("window_top")
    left = settings.get("window_left")
    if isinstance(top, (int, float)) and isinstance(left, (int, float)):
        page.window.top = float(top)
        page.window.left = float(left)
    if bool(settings.get("window_maximized", False)):
        page.window.maximized = True


def _on_window_event(event: ft.WindowEvent, page: ft.Page,
                     settings: Settings) -> None:
    """Persist geometry as the user moves and resizes the window."""
    if not bool(settings.get("remember_window_size", True)):
        return
    if event.type in (ft.WindowEventType.RESIZED, ft.WindowEventType.MOVED,
                      ft.WindowEventType.MAXIMIZE, ft.WindowEventType.UNMAXIMIZE):
        settings.update({
            "window_width": float(page.window.width or 1440),
            "window_height": float(page.window.height or 900),
            "window_top": float(page.window.top or 0),
            "window_left": float(page.window.left or 0),
            "window_maximized": bool(page.window.maximized),
        })


def _on_close(page: ft.Page, settings: Settings, ctx: AppContext) -> None:
    logger.info("Window closing; cancelling %d running job(s)",
                ctx.jobs.active_count())
    ctx.jobs.cancel_all()
    settings.save()


def _startup_target(startup_tool: str, settings: Settings) -> str:
    if startup_tool:
        if registry.get(startup_tool) is not None:
            return startup_tool
        logger.warning("Unknown --tool id %r; opening home instead", startup_tool)
        return HOME_ID
    if bool(settings.get("remember_last_tool", True)):
        last = str(settings.get("last_tool", ""))
        if last and registry.get(last) is not None:
            return last
    return HOME_ID


# ----------------------------------------------------------------------
# Diagnostics
# ----------------------------------------------------------------------

def _show_startup_error(page: ft.Page, exc: Exception,
                        log_file: Optional[Path]) -> None:
    """Last-resort UI when the shell itself could not be built."""
    import traceback

    page.add(
        ft.Container(
            padding=24,
            content=ft.Column(
                spacing=12,
                controls=[
                    ft.Text("The kit could not start", size=20,
                            weight=ft.FontWeight.W_700, color=ft.Colors.RED_400),
                    ft.Text(f"{type(exc).__name__}: {exc}", size=13,
                            selectable=True),
                    ft.Text(f"Log: {log_file}" if log_file else "", size=11,
                            color=ft.Colors.ON_SURFACE_VARIANT),
                    ft.Container(
                        padding=10,
                        bgcolor=ft.Colors.with_opacity(0.4, ft.Colors.BLACK),
                        border_radius=ft.BorderRadius.all(8),
                        content=ft.Text(traceback.format_exc(), size=11,
                                        font_family="Consolas", selectable=True),
                    ),
                ],
            ),
        )
    )


async def _self_test_hotspot(page: ft.Page, shell: AppShell,
                             record: Any, settle: Any) -> None:
    """Detect regions on a synthetic trim sheet and save the `.rect`.

    The Hotspot Editor is the one page whose Run tab is a canvas rather than a
    form, so this covers the parts a page-mount check cannot: the texture
    decodes at native size, detection finds the panels, and Save writes a file
    Hammer would accept plus the VMT line pointing at it.
    """
    import asyncio
    import shutil
    import tempfile

    import numpy as np
    from PIL import Image

    scratch = Path(tempfile.mkdtemp(prefix="s2kit-hotspot-"))
    try:
        folder = scratch / "materials" / "selftest"
        folder.mkdir(parents=True)
        sheet = np.full((256, 512, 3), 24, dtype=np.uint8)
        sheet[8:120, 8:248] = (170, 96, 64)
        sheet[8:120, 264:504] = (64, 140, 170)
        sheet[136:248, 8:504] = (118, 158, 96)
        texture = folder / "trim.png"
        Image.fromarray(sheet, "RGB").save(texture)
        (folder / "trim.vmt").write_text(
            '"LightmappedGeneric"\n{\n\t"$basetexture" "selftest/trim"\n}\n',
            encoding="utf-8")

        shell.navigate("hotspot-editor")
        await settle(0.4)
        tool = shell.current_page()

        tool._load_texture(texture)
        await settle(0.3)
        record(tool.canvas.sheet.size == (512, 256),
               "trim sheet loads at native size",
               f"{tool.canvas.sheet.size}")

        tool._detect()
        await settle(0.3)
        found = len(tool.canvas.sheet.rects)
        record(found == 3, "panels detected", f"{found} region(s)")

        # Grid appearance is applied live from the tool's own settings, so a
        # broken pref name would only show up as a canvas that never changes.
        store = shell.ctx.settings.tool_pref_store(tool.tool_id)
        store.set("grid_style", "dots")
        store.set("grid_color", "black")
        store.set("grid_opacity", 40)
        tool.on_preferences_changed()
        await settle(0.3)
        record(tool.canvas._grid_style == "dots"
               and tool.canvas._grid_color == "black"
               and abs(tool.canvas._grid_opacity - 0.4) < 1e-6,
               "grid appearance applies live",
               f"{tool.canvas._grid_style}/{tool.canvas._grid_color}/"
               f"{tool.canvas._grid_opacity}")
        store.set("grid_style", "lines")
        store.set("grid_color", "white")
        store.set("grid_opacity", 6)
        tool.on_preferences_changed()

        # Editing shortcuts. These are routed from the shell, so this also
        # covers the page getting first refusal on keys at all.
        def press(key: str, **mods: bool) -> None:
            event = ft.KeyboardEvent(
                name="keyboard", control=page, key=key,
                shift=mods.get("shift", False), ctrl=mods.get("ctrl", False),
                alt=False, meta=False)
            shell.handle_key(event)

        tool.canvas.select([0])
        press("Delete")
        await settle(0.2)
        record(len(tool.canvas.sheet.rects) == found - 1,
               "Delete removes the selection",
               f"{len(tool.canvas.sheet.rects)} left of {found}")
        press("Z", ctrl=True)
        await settle(0.2)
        record(len(tool.canvas.sheet.rects) == found, "Ctrl+Z restores it",
               f"{len(tool.canvas.sheet.rects)} of {found}")

        tool.canvas.select([0])
        before = tool.canvas.sheet.rects[0].x
        press("Arrow Right")
        await settle(0.2)
        record(tool.canvas.sheet.rects[0].x == before + 1,
               "arrows nudge by a pixel",
               f"{before} -> {tool.canvas.sheet.rects[0].x}")
        press("Z", ctrl=True)
        await settle(0.2)

        # A focused text field must keep the editing keys out of the way.
        tool._typing = True
        press("Delete")
        await settle(0.1)
        record(len(tool.canvas.sheet.rects) == found,
               "typing in a field suppresses Delete",
               f"{len(tool.canvas.sheet.rects)} of {found}")
        tool._typing = False
        tool.canvas.select([])

        tool.start_run()
        for _ in range(300):
            if not tool.is_running:
                break
            await asyncio.sleep(0.1)
        handle = tool.last_handle
        record(handle is not None and handle.status == "success",
               "hotspot-editor save",
               f"status={getattr(handle, 'status', '?')} "
               f"{getattr(handle, 'error', '')}")

        rect_file = texture.with_suffix(".rect")
        text = rect_file.read_text(encoding="utf-8") if rect_file.is_file() else ""
        record(text.startswith("Rectangles") and text.count("rectangle") == found,
               "rect file written", f"{len(text)} bytes")
        vmt_text = (folder / "trim.vmt").read_text(encoding="utf-8")
        record('"%rectanglemap" "selftest/trim"' in vmt_text,
               "VMT rectanglemap wired up")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


async def _self_test_soundscape(page: ft.Page, shell: AppShell,
                                record: Any, settle: Any) -> None:
    """Pick a soundscape and port it, without touching a game install.

    Audio extraction needs a VPK and cannot be assumed, so this covers the half
    that is pure logic: the picker indexes what it loaded, selection drives the
    port, the event graph resolves through `base`, the surround pair is folded,
    volumes multiply, and the output is namespaced so it cannot collide with
    Half-Life 2's.
    """
    import asyncio
    import shutil
    import tempfile

    from s2kit.pipelines import soundscape as ss

    kv3_header = ("<!-- kv3 encoding:text:version"
                  "{e21c7f3c-8a33-41c5-9977-a76d3a32aa0d} -->")
    events_text = "\n".join([
        kv3_header,
        "{",
        '\tSelf.Bed = { type = "hlvr_start_multi" volume = 0.5',
        '\t\tsoundevent_01 = "Self.Bed_L" soundevent_02 = "Self.Bed_R"',
        '\t\tsoundevent_03 = "Self.Bed_LS" soundevent_04 = "Self.Bed_RS" }',
        '\tSelf.Bed_L = { volume = 0.8 vsnd_file_01 = "sounds/amb/a_l.vsnd" }',
        '\tSelf.Bed_R = { base = "Self.Bed_L" vsnd_file_01 = "sounds/amb/a_r.vsnd" }',
        '\tSelf.Bed_LS = { base = "Self.Bed_L" vsnd_file_01 = "sounds/amb/a_ls.vsnd" }',
        '\tSelf.Bed_RS = { base = "Self.Bed_L" vsnd_file_01 = "sounds/amb/a_rs.vsnd" }',
        "}",
        "",
    ])
    # A live playevent, a commented-out one, a rule that is already Source 1,
    # and a second soundscape that must stay behind because it is not ticked.
    script_text = "\n".join([
        '"selftest_room"',
        "{",
        '\t"playevent"',
        "\t{",
        '\t\t"event" "Self.Bed"',
        '\t\t"volume" "0.5"',
        "\t}",
        '\t// "playevent"',
        "\t// {",
        '\t// "event" "Self.Dead"',
        "\t// }",
        '\t"playlooping"',
        "\t{",
        '\t\t"volume" "1"',
        '\t\t"wave" "ambient/legacy.wav"',
        "\t}",
        "}",
        '"selftest_unwanted"',
        "{",
        '\t"playevent"',
        "\t{",
        '\t\t"event" "Self.Bed"',
        "\t}",
        "}",
        "",
    ])

    scratch = Path(tempfile.mkdtemp(prefix="s2kit-selftest-scape-"))
    try:
        events = scratch / "soundevents"
        events.mkdir(parents=True)
        (events / "selftest.vsndevts").write_text(events_text, encoding="utf-8")
        scripts = scratch / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "soundscapes_selftest.txt").write_text(script_text,
                                                          encoding="utf-8")

        # The fixture is checked first, so a typo here cannot look like a tool
        # bug when the run fails further down.
        try:
            probe = ss.parse_vsndevts(events_text)
            record(len(probe) == 5, "soundscape fixture parses",
                   f"{len(probe)} event(s)")
        except Exception as exc:
            record(False, "soundscape fixture parses", str(exc))

        output = scratch / "addon"
        shell.navigate("soundscape-port")
        await settle(0.3)
        tool = shell.current_page()
        record(tool is not None, "soundscape-port page opened")
        if tool is None:
            return

        tool.form.set_values({
            "source": str(scratch), "vpk": "", "output": str(output),
            "namespace": "selftest", "name_prefix": "st.",
            "script_name": "soundscapes_selftest", "manifest": "none",
            "gmod_dir": "", "quad_fold": True, "keep_source1": True,
            "max_stems": 0, "soundlevel": "", "extract_audio": False,
            "format": "wav", "loop_points": True,
        })
        await settle(0.2)

        # Drive the picker the way a person would: load, then tick one.
        tool._load()
        for _ in range(100):
            if not tool._loading and tool._entries:
                break
            await asyncio.sleep(0.05)
        names = [entry.name for entry in tool._entries]
        record(names == ["selftest_room", "selftest_unwanted"],
               "picker indexed the soundscapes", str(names))
        tool._toggle("selftest_room", True)
        record(tool._picked == {"selftest_room"}, "picker selection")

        tool.start_run()
        handle = None
        for _ in range(600):
            handle = tool.last_handle
            if handle is not None and not handle.running:
                break
            await asyncio.sleep(0.1)
        ok = handle is not None and handle.status == "success"
        record(ok, "soundscape-port run",
               "" if ok else (getattr(handle, "error", "") or "no handle"))
        if not ok:
            return

        written = output / "scripts" / "soundscapes_selftest.txt"
        record(written.is_file(), "soundscape script written")
        if not written.is_file():
            return
        root = ss.parse_kv(written.read_text(encoding="utf-8"))
        ported = [name for name, _ in root]
        record(ported == ["st.selftest_room"],
               "only the selection was ported, and prefixed", str(ported))

        room = root.block("st.selftest_room")
        loops = room.all("playlooping")
        waves = [rule.get("wave") for rule in loops]
        record(waves == ["selftest/amb/a_l.wav", "selftest/amb/a_r.wav",
                         "ambient/legacy.wav"],
               "surrounds folded and audio namespaced", str(waves))
        # 0.5 (playevent) x 0.5 (parent) x 0.8 (inherited child) = 0.20
        record(loops[0].get("volume") == "0.20",
               "volumes multiply through the event graph",
               loops[0].get("volume"))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


async def _self_test_materials(page: ft.Page, shell: AppShell,
                               record: Any, settle: Any) -> None:
    """Build one material from synthesised maps and check what came out."""
    import asyncio
    import shutil
    import tempfile

    import numpy as np
    from PIL import Image

    scratch = Path(tempfile.mkdtemp(prefix="s2kit-selftest-"))
    try:
        size = 32
        ramp = np.tile(np.linspace(0.0, 1.0, size, dtype=np.float32), (size, 1))
        Image.fromarray(
            (np.dstack([ramp, ramp * 0.5 + 0.3, np.full_like(ramp, 0.2)])
             * 255).astype(np.uint8), "RGB").save(scratch / "probe_color.png")
        Image.fromarray(
            (np.dstack([ramp * 0.3 + 0.35, ramp.T * 0.3 + 0.35,
                        np.ones_like(ramp)]) * 255).astype(np.uint8),
            "RGB").save(scratch / "probe_normal.png")
        Image.fromarray((ramp * 255).astype(np.uint8),
                        "L").save(scratch / "probe_roughness.png")
        Image.fromarray(((1.0 - ramp) * 255).astype(np.uint8),
                        "L").save(scratch / "probe_metallic.png")

        shell.navigate("material-builder")
        await settle(0.4)
        tool = shell.current_page()
        tool.form.set_values({
            "color": str(scratch / "probe_color.png"),
            "normal": str(scratch / "probe_normal.png"),
            "roughness": str(scratch / "probe_roughness.png"),
            "metallic": str(scratch / "probe_metallic.png"),
            "output_dir": str(scratch / "out"),
            "name": "probe",
            "material_path": "models/selftest",
            "mode": "fake",
            "branch": "gmod",
        })
        tool.start_run()
        for _ in range(600):                        # up to ~60 s
            if not tool.is_running:
                break
            await asyncio.sleep(0.1)

        handle = tool.last_handle
        ok = handle is not None and handle.status == "success"
        record(ok, "material-builder run",
               f"status={getattr(handle, 'status', '?')} "
               f"{getattr(handle, 'error', '')}")

        written = sorted(path.name for path in (scratch / "out").glob("*"))
        expected = ["probe.vmt", "probe_color.vtf", "probe_envmask.vtf",
                    "probe_normal.vtf", "probe_phong.vtf"]
        record(written == expected, "material outputs written",
               ", ".join(written) or "nothing")

        vmt_text = (scratch / "out" / "probe.vmt").read_text(encoding="utf-8") \
            if (scratch / "out" / "probe.vmt").exists() else ""
        record('"$phong" "1"' in vmt_text and '"$envmap"' in vmt_text
               and '"$basetexture" "models/selftest/probe_color"' in vmt_text,
               "VMT has the expected parameters")

        # The Preview tab has to be able to open what the run just encoded,
        # which is the only check that the VTFs are actually well formed.
        vtf = scratch / "out" / "probe_color.vtf"
        if vtf.exists():
            tool.preview.show(vtf)
            await settle(0.1)
            for _ in range(80):
                viewer = tool.preview.current_viewer()
                if viewer is not None and not viewer.busy:
                    break
                await asyncio.sleep(0.1)
            viewer = tool.preview.current_viewer()
            record(viewer is not None and not viewer.has_error,
                   "encoded VTF opens in the viewer", vtf.name)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _self_test(page: ft.Page, shell: AppShell) -> None:
    """Headless integration test.

    Mounts every page, then runs a real job end to end (the read-only Asset
    Inspector, pointed at the kit's own source tree), opens each artefact it
    produced in the viewer that claims it, and checks a history record was
    written. Optionally saves a screenshot with `S2KIT_SELFTEST_SHOT=<path>`.
    """
    page.run_task(_self_test_async, page, shell)


async def _self_test_async(page: ft.Page, shell: AppShell) -> None:
    import asyncio
    import os
    import traceback

    results: list[str] = []

    def record(ok: bool, label: str, detail: str = "") -> None:
        results.append(f"  {'OK  ' if ok else 'FAIL'}  {label}"
                       + (f"\n         {detail}" if detail else ""))

    async def settle(seconds: float = 0.25) -> None:
        page.update()
        await asyncio.sleep(seconds)

    # -- 1. every page mounts -----------------------------------------
    targets = [HOME_ID, PREFERENCES_ID] + [spec.id for spec in registry.all_tools()]
    for target in targets:
        try:
            shell.navigate(target)
            await settle(0.1)
            record(True, f"page {target}")
        except Exception:
            record(False, f"page {target}", traceback.format_exc())

    # -- 2. a real run, end to end ------------------------------------
    tool = None
    try:
        shell.navigate("asset-inspector")
        await settle(0.4)
        tool = shell.current_page()
        # Point this at a real asset folder (S2KIT_SELFTEST_DIR) to exercise the
        # texture, model and sound viewers as well as the text one.
        scan_dir = os.environ.get("S2KIT_SELFTEST_DIR",
                                  str(paths.app_root() / "s2kit"))
        tool.form.set_values({
            "folder": scan_dir,
            "recursive": True,
            "kind": "all",
            "max_files": 600,
            "preview_all": False,
        })
        tool.start_run()
        for _ in range(600):                    # up to ~60 s
            if not tool.is_running:
                break
            await asyncio.sleep(0.1)
        handle = tool.last_handle
        ok = handle is not None and handle.status == "success"
        record(ok, "asset-inspector run",
               f"status={getattr(handle, 'status', '?')} "
               f"summary={getattr(handle, 'summary', '')}")

        metrics = tool.output.metrics()
        record(bool(metrics), "run reported metrics", f"{len(metrics)} metric(s)")
        artifacts = tool.output.artifacts()
        record(bool(artifacts), "run reported artifacts",
               f"{len(artifacts)} artifact(s)")
        history = shell.ctx.history.list("asset-inspector")
        record(bool(history), "history record written",
               f"{len(history)} record(s)")
        log_lines = len(tool.log_view.lines())
        record(log_lines > 0, "log captured", f"{log_lines} line(s)")
    except Exception:
        record(False, "asset-inspector run", traceback.format_exc())

    # -- 2b. a material conversion, end to end ------------------------
    # The PBR path has no read-only equivalent of the Asset Inspector, so it
    # gets a synthesised input set and a scratch output folder. It is worth the
    # extra seconds: this exercises decode, bake, VTF encode and VMT write in
    # one go, which is most of what the materials tools are.
    try:
        await _self_test_materials(page, shell, record, settle)
    except Exception:
        record(False, "material-builder run", traceback.format_exc())

    # -- 2c. the hotspot canvas, end to end ---------------------------
    try:
        await _self_test_hotspot(page, shell, record, settle)
    except Exception:
        record(False, "hotspot-editor run", traceback.format_exc())

    # -- 2d. a soundscape port, without needing a game install --------
    try:
        await _self_test_soundscape(page, shell, record, settle)
    except Exception:
        record(False, "soundscape-port run", traceback.format_exc())

    # -- 3. viewers ----------------------------------------------------
    if tool is not None:
        shell.navigate("asset-inspector")
        await settle(0.2)
        seen: set[str] = set()
        for artifact in tool.output.artifacts():
            kind = artifact.kind or ""
            if kind in seen:
                continue
            seen.add(kind)
            try:
                tool.preview.show(artifact.path)
                await settle(0.1)
                for _ in range(80):             # decode happens on a thread
                    viewer = tool.preview.current_viewer()
                    if viewer is not None and not viewer.busy:
                        break
                    await asyncio.sleep(0.1)
                viewer = tool.preview.current_viewer()
                failed = viewer is None or viewer.has_error
                record(not failed, f"viewer {kind or 'unknown'}",
                       f"{artifact.path.name}"
                       + ("  (error shown)" if failed else ""))
            except Exception:
                record(False, f"viewer {kind}", traceback.format_exc())

    # -- 4. optional screenshot ---------------------------------------
    shot = os.environ.get("S2KIT_SELFTEST_SHOT", "")
    if shot:
        try:
            if tool is not None:
                tool.select_tab("Preview")
            page.enable_screenshots = True
            await settle(0.3)
            data = await page.take_screenshot(pixel_ratio=1)
            Path(shot).write_bytes(data)
            record(True, "screenshot", f"{len(data):,} bytes -> {shot}")
        except Exception:
            record(False, "screenshot", traceback.format_exc())

    print("SELF-TEST", flush=True)
    for line in results:
        print(line, flush=True)
    failures = [line for line in results if line.strip().startswith("FAIL")]
    print(f"SELF-TEST: {len(results) - len(failures)}/{len(results)} checks OK",
          flush=True)
    await page.window.close()
