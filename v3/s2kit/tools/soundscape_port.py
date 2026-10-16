"""
Soundscape Porter.

Browse the soundscapes a Source 2 game ships, tick the ones you want, and get a
Garry's Mod addon out: the converted script, the audio it names, and a manifest
that loads it without trampling anything.

A soundscape is only half a deliverable on its own - its rules name waves that
have to exist - so the port is one job, not three:

* **Sound events are a graph.** A `playevent` names an event, the event inherits
  through `base`, and its children hold the audio. Resolving that is where the
  volumes and file lists live.
* **Source 1 cannot layer inside a rule.** A Source 2 ambience is often three
  stems at different trims, so it becomes three `playlooping` rules whose
  volumes are the products.
* **Everything must be namespaced.** Half-Life 2 already owns `sound/ambient/`
  and soundscape names are global, so an unprefixed port overwrites the base
  game's ambience for everyone who installs the addon.

The format layer is in `pipelines/soundscape.py` and is Flet-free; this module
is the picker, the VPK extraction and the audio stage.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

import flet as ft

from s2kit.core.jobs import JobContext
from s2kit.core.params import (
    BoolParam, ChoiceParam, IntParam, PathParam, TextParam, options,
)
from s2kit.core.process import ProcessError, run_streamed
from s2kit.core.registry import tool
from s2kit.media.probe import human_size
from s2kit.pipelines import audio_ops as audio
from s2kit.pipelines import soundscape as ss
from s2kit.pipelines import vrf
from s2kit.ui import common
from s2kit.ui.tool_page import ToolPage

#: Longest `-f` filter list to hand one Source2Viewer-CLI call. The command
#: line is the limit, not the tool, so this is deliberately conservative.
FILTER_CHUNK = 120

#: Where Garry's Mod usually lives, for finding the stock manifest to merge
#: with. Only a fallback - the path is a parameter.
GMOD_GUESS = Path(r"C:/Program Files (x86)/Steam/steamapps/common/GarrysMod"
                  r"/garrysmod")

SOUNDLEVELS = options(
    ("", "None - a 2D bed that does not fall off (recommended)"),
    ("SNDLVL_NORM", "SNDLVL_NORM"),
    ("SNDLVL_80dB", "SNDLVL_80dB"),
    ("SNDLVL_85dB", "SNDLVL_85dB"),
)

FORMATS = options(
    ("wav", "WAV - required for anything that loops"),
    ("ogg", "OGG Vorbis - smaller, one-shots only"),
    ("keep", "Keep what VRF decoded (usually MP3)"),
)

MANIFESTS = options(
    ("merge", "Merge with Garry's Mod's - keeps the stock soundscapes"),
    ("own", "Ours only - replaces the stock manifest"),
    ("none", "Do not write one - I will add the line myself"),
)


def _spec_for(target_format: str) -> "audio.EncodeSpec":
    """The encode settings for a target container.

    WAV is PCM because that is the only thing Source will loop; Vorbis quality
    4 matches what the Sound Converter calls default.
    """
    if target_format == "wav":
        return audio.EncodeSpec(codec="pcm_s16le", quality=None)
    return audio.EncodeSpec(codec="libvorbis", quality=4.0)


@tool(
    id="soundscape-port",
    title="Soundscape Porter",
    category="Sounds",
    icon="GRAPHIC_EQ",
    description="Pick Source 2 soundscapes and get a Garry's Mod addon with "
                "their audio.",
    keywords=("soundscape", "ambient", "ambience", "playevent", "vsndevts",
              "soundevent", "playlooping", "playrandom", "hlvr", "alyx",
              "scripts", "dsp", "manifest"),
    order=50,
)
class SoundscapePorter(ToolPage):
    """Pick soundscapes by name, port them, and bring their sounds along."""

    RUN_LABEL = "Port selected"
    RUN_ICON = "PLAY_ARROW"

    PARAMS = [
        PathParam("source", "Game content folder", mode="directory",
                  must_exist=False, group="Input",
                  help="A folder holding `scripts/` and `soundevents/`. Leave "
                       "empty to read both straight from the VPK."),
        PathParam("vpk", "Game VPK", mode="open_file", extensions=("vpk",),
                  must_exist=False, group="Input",
                  help="`hlvr/pak01_dir.vpk`. Needed for the audio, and for "
                       "anything the folder above is missing."),

        PathParam("output", "Addon folder", mode="directory", must_exist=False,
                  required=True, group="Output",
                  help="The addon root. `scripts/` and `sound/` are written "
                       "inside it."),
        TextParam("namespace", "Sound folder", default="hlvr", group="Output",
                  help="Audio goes to `sound/<this>/...`. Without it the port "
                       "overwrites Half-Life 2's ambience."),
        TextParam("name_prefix", "Soundscape name prefix", default="hlvr.",
                  group="Output",
                  help="Soundscape names are global. `hlvr.` turns "
                       "`a2_pistol_basement` into `hlvr.a2_pistol_basement`."),
        TextParam("script_name", "Script file name",
                  default="soundscapes_hlvr", group="Output",
                  help="Written as `scripts/<this>.txt`."),
        ChoiceParam("manifest", "Manifest", default="merge", choices=MANIFESTS,
                    group="Output",
                    help="Garry's Mod loads soundscapes from one manifest. "
                         "Shipping your own replaces it and silently disables "
                         "the stock files."),
        PathParam("gmod_dir", "Garry's Mod folder", mode="directory",
                  must_exist=False, group="Output", advanced=True,
                  help="The `garrysmod` folder, for reading the stock "
                       "manifest to merge with. Found automatically if it is "
                       "in the usual place."),

        BoolParam("quad_fold", "Fold surround beds to stereo", default=True,
                  group="Conversion",
                  help="Source 1 has no quad ambience. Off keeps the rear "
                       "channels as extra looping layers."),
        BoolParam("keep_source1", "Keep rules that are already Source 1",
                  default=True, group="Conversion",
                  help="Valve mix both formats in one file."),
        IntParam("max_stems", "Layer limit per event", default=0, min=0, max=16,
                 group="Conversion", advanced=True,
                 help="0 keeps every layer."),
        ChoiceParam("soundlevel", "Soundlevel", default="", choices=SOUNDLEVELS,
                    group="Conversion", advanced=True),

        BoolParam("extract_audio", "Extract and convert the sounds",
                  default=True, group="Audio",
                  help="Off writes only the script, and lists what it needs."),
        ChoiceParam("format", "Audio format", default="wav", choices=FORMATS,
                    group="Audio", depends_on=("extract_audio", True)),
        BoolParam("loop_points", "Write loop points on looping sounds",
                  default=True, group="Audio",
                  depends_on=("extract_audio", True),
                  help="A `playlooping` wave without a `smpl` chunk plays once "
                       "and stops."),
    ]

    PREFERENCES = [
        BoolParam("report_warnings", "List every conversion warning",
                  default=False, group="Reporting",
                  help="Off summarises them by kind."),
        IntParam("preview_limit", "Sounds to offer for preview", default=6,
                 min=0, max=50, group="Reporting"),
    ]

    HELP = """
### Soundscape Porter

Half-Life: Alyx keeps its soundscapes in `scripts/soundscapes_*.txt` - the same
KeyValues container Source 1 uses, and never compiled. What changed is the body:
where Source 1 names a `.wav` inside a `playlooping` rule, Source 2 writes a
`playevent` naming a *sound event*, and that event lives in a KV3 `.vsndevts`
file which resolves - usually through a `base` chain and a set of children - to
the actual `.vsnd` files.

Point the tool at a game, press **Load**, tick the soundscapes you want, and
port. You get an addon folder you can drop into `garrysmod/addons/`.

#### What comes out

| Source 2 | Source 1 |
| --- | --- |
| A looping event with layered stems | one `playlooping` per stem, volumes multiplied |
| A random ambience | one `playrandom` with the takes in an `rndwave` block |
| A quad or surround bed | the front pair only |
| Rules already in Source 1 form | passed through untouched |

Everything picked lands in one script file, because an addon wants one entry in
the manifest rather than twenty. Volumes multiply down the graph: an event at
0.9 whose stem is trimmed to 0.4, played by a `playevent` at 0.5, comes out at
0.18.

#### Three things that fail silently if you get them wrong

* **Namespacing.** Half-Life 2 already owns `sound/ambient/`, and Alyx reuses
  many of the same names. Audio is written to `sound/<namespace>/...` and every
  soundscape gets a name prefix, because soundscape names are global too.
* **The manifest.** Garry's Mod loads soundscapes from a single
  `scripts/soundscapes_manifest.txt` listing 89 files. An addon that ships its
  own *replaces* that list and disables every stock soundscape. **Merge** reads
  the stock manifest out of `garrysmod_dir.vpk` and writes it back with your
  file appended.
* **Looping needs a WAV.** Alyx stores audio as MP3, and Source only loops a
  wave carrying a `smpl` chunk. Anything a `playlooping` uses is transcoded and
  given a loop point; one-shots are left as they decoded. Choosing OGG means
  looping ambience plays once and stops.

#### What does not survive

Source 2's dynamic mixing has no Source 1 equivalent: opvar-driven volume
curves, HRTF, occlusion and falloff curves are all dropped, as is anything
positional beyond `position` and `origin`. The result is the bed, not the mix -
expect to set levels by ear afterwards.
"""

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def init(self):
        super().init()
        #: Everything the loaded game offers, in file order.
        self._entries: list[ss.SoundscapeEntry] = []
        #: Names the user has ticked.
        self._picked: set[str] = set()
        #: One row per entry, built once and filtered by toggling `visible`.
        self._rows: list[ft.Control] = []
        #: Where the loaded scripts and sound events live on disk.
        self._scripts: Optional[Path] = None
        self._events: Optional[Path] = None
        #: Kept for the session so a VPK is only unpacked once.
        self._scratch: Optional[Path] = None
        self._loading = False

    # ------------------------------------------------------------------
    # The picker
    # ------------------------------------------------------------------

    def extra_controls(self) -> list[ft.Control]:
        """The soundscape list, under the form: you set a source before you can
        browse it."""
        self._search = ft.TextField(
            label="Search", dense=True, expand=True,
            on_change=lambda _e: self._filter())
        self._count = common.caption("Nothing loaded yet.")
        self._list = ft.Column(spacing=0, scroll=ft.ScrollMode.AUTO, height=300)
        self._only_s2 = ft.Checkbox(
            label="Only Source 2 ones", value=False,
            on_change=lambda _e: self._filter())

        buttons = ft.Row(spacing=8, wrap=True, controls=[
            ft.FilledTonalButton("Load", icon=ft.Icons.REFRESH,
                                 on_click=lambda _e: self._load()),
            ft.OutlinedButton("Select all shown", icon=ft.Icons.DONE_ALL,
                              on_click=lambda _e: self._select_shown(True)),
            ft.OutlinedButton("Clear", icon=ft.Icons.REMOVE_DONE,
                              on_click=lambda _e: self._select_shown(False)),
        ])
        return [common.section(
            "Soundscapes",
            common.caption("Load the game, then tick what you want to port."),
            buttons,
            ft.Row(spacing=12, controls=[self._search, self._only_s2]),
            self._count,
            self._list,
            icon_name="GRAPHIC_EQ",
        )]

    def _load(self) -> None:
        """Index the source, unpacking the VPK once if that is what we have."""
        if self._loading:
            return
        values = self.form.values()
        source = str(values.get("source", "") or "")
        vpk = str(values.get("vpk", "") or "")
        if not source and not vpk:
            self.ctx.toast("Set a content folder or a VPK first",
                           level="WARNING")
            return
        self._loading = True
        self._count.value = "Loading..."
        self._safe_update(self._count)
        threading.Thread(target=self._load_worker, args=(source, vpk),
                         daemon=True).start()

    def _load_worker(self, source: str, vpk: str) -> None:
        try:
            scripts, events = self._resolve_trees(
                Path(source) if source else None, Path(vpk) if vpk else None)
            entries: list[ss.SoundscapeEntry] = []
            for path in sorted(scripts.glob("*.txt")):
                if "manifest" in path.name.lower():
                    continue
                entries.extend(ss.index_soundscapes(
                    path.read_text(encoding="utf-8", errors="ignore"),
                    path.name))
            self._scripts, self._events = scripts, events
            self._entries = entries
            known = {entry.name for entry in entries}
            self._picked = {name for name in self._picked if name in known}
            self._build_rows()
        except Exception as exc:                     # reported in the UI
            self._entries = []
            self._count.value = f"Could not load: {exc}"
            self._safe_update(self._count)
        finally:
            self._loading = False

    def _resolve_trees(self, source: Optional[Path],
                       vpk: Optional[Path]) -> tuple[Path, Path]:
        """Find a `scripts/` and a `soundevents/` folder, unpacking if needed.

        A content folder supplies whatever it has and the VPK fills the gap,
        because an extracted tree is usually partial - sound events without the
        scripts, or the other way round.
        """
        scripts = events = None
        if source is not None and source.is_dir():
            for candidate in (source / "scripts", source):
                if candidate.is_dir() and any(candidate.glob("*.txt")):
                    scripts = candidate
                    break
            for candidate in (source / "soundevents", source):
                if candidate.is_dir() and any(candidate.glob("*.vsndevts")):
                    events = candidate
                    break
        if scripts is not None and events is not None:
            return scripts, events

        if vpk is None or not vpk.is_file():
            missing = "scripts" if scripts is None else "soundevents"
            raise ValueError(f"no {missing}/ in the content folder, and no VPK "
                             f"to read it from")
        if self._scratch is None:
            self._scratch = Path(tempfile.mkdtemp(prefix="s2kit-scapes-"))
        wanted: list[str] = []
        if scripts is None:
            wanted.append("scripts/soundscapes")
        if events is None:
            wanted.append("soundevents/")
        self._extract(vpk, wanted, self._scratch)
        return (scripts or self._scratch / "scripts",
                events or self._scratch / "soundevents")

    def _build_rows(self) -> None:
        """One row per soundscape, created once; searching toggles visibility."""
        self._rows = [self._make_row(entry) for entry in self._entries]
        self._list.controls = self._rows
        self._safe_update(self._list)
        self._filter()

    def _make_row(self, entry: ss.SoundscapeEntry) -> ft.Control:
        tint = {"source2": ft.Colors.GREEN_300, "mixed": ft.Colors.AMBER_300,
                "source1": ft.Colors.BLUE_200}[entry.kind]
        box = ft.Checkbox(
            value=entry.name in self._picked, label="", width=36,
            on_change=lambda e, n=entry.name: self._toggle(n, e.control.value))
        detail = (f"{entry.playevents} event(s)" if entry.playevents
                  else f"{entry.source1_rules} Source 1 rule(s)")
        return ft.Row(spacing=6, controls=[
            box,
            ft.Text(entry.name, size=12, expand=True,
                    overflow=ft.TextOverflow.ELLIPSIS),
            ft.Text(detail, size=11, width=124, color=tint),
            ft.Text(entry.source, size=11, width=168,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                    overflow=ft.TextOverflow.ELLIPSIS),
        ])

    def _toggle(self, name: str, on: bool) -> None:
        if on:
            self._picked.add(name)
        else:
            self._picked.discard(name)
        self._update_count()

    def _filter(self) -> None:
        needle = (self._search.value or "").strip().lower()
        only_s2 = bool(self._only_s2.value)
        for entry, row in zip(self._entries, self._rows):
            shown = (not needle or needle in entry.name.lower()
                     or needle in entry.source.lower())
            if shown and only_s2 and not entry.playevents:
                shown = False
            if row.visible != shown:
                row.visible = shown
        self._safe_update(self._list)
        self._update_count()

    def _select_shown(self, on: bool) -> None:
        for entry, row in zip(self._entries, self._rows):
            if not row.visible:
                continue
            if on:
                self._picked.add(entry.name)
            else:
                self._picked.discard(entry.name)
            row.controls[0].value = on
        self._safe_update(self._list)
        self._update_count()

    def _update_count(self) -> None:
        shown = sum(1 for row in self._rows if row.visible)
        self._count.value = (f"{len(self._picked)} selected of {shown} shown "
                             f"({len(self._entries)} in total)")
        self._safe_update(self._count)

    @staticmethod
    def _safe_update(control: Any) -> None:
        try:
            control.update()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self, values: dict) -> dict[str, str]:
        """Catch the setups that would otherwise fail deep into a long run."""
        errors: dict[str, str] = {}
        source = str(values.get("source", "") or "")
        vpk = str(values.get("vpk", "") or "")
        if not source and not vpk:
            errors["source"] = "Set a content folder or a VPK"
        if vpk and not Path(vpk).is_file():
            errors["vpk"] = "Not found"
        if bool(values.get("extract_audio", True)) and not vpk:
            errors["vpk"] = "Extracting the audio needs the VPK"
        if not self._picked:
            errors["output"] = ("Nothing selected - press Load and tick a "
                                "soundscape below")
        if not str(values.get("namespace", "") or "").strip():
            errors["namespace"] = ("Without a namespace the audio overwrites "
                                   "Half-Life 2's")
        return errors

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    def run_job(self, ctx: JobContext) -> None:
        params = ctx.params
        output = Path(str(params["output"]))
        vpk = Path(str(params["vpk"])) if params.get("vpk") else None
        picked = set(self._picked)
        if not picked:
            raise ValueError("Nothing selected")
        if self._scripts is None or self._events is None:
            raise ValueError("Press Load first")
        output.mkdir(parents=True, exist_ok=True)
        ctx.info(f"{len(picked)} soundscape(s) selected")

        table = self._load_events(ctx, self._events)
        ctx.info(f"{len(table)} sound event(s) available")

        extension = "." + ("wav" if str(params["format"]) == "keep"
                           else str(params["format"]))
        options = ss.ConvertOptions(
            extension=extension,
            soundlevel=str(params.get("soundlevel", "") or ""),
            quad_fold=bool(params.get("quad_fold", True)),
            max_stems=int(params.get("max_stems", 0) or 0),
            keep_source1=bool(params.get("keep_source1", True)),
            wave_prefix=str(params.get("namespace", "") or "").strip("/"),
            name_prefix=str(params.get("name_prefix", "") or ""),
            only=picked,
        )

        script, sources, looping = self._convert(ctx, picked, table, options,
                                                 output, params)
        self._write_manifest(ctx, params, script, output)
        if bool(params.get("extract_audio", True)) and vpk is not None:
            self._bring_audio(ctx, params, vpk, sources, looping, output,
                              options)
        else:
            ctx.info(f"{len(sources)} sound(s) referenced but not extracted.")

    # -- events --------------------------------------------------------

    @staticmethod
    def _load_events(ctx: JobContext, folder: Path) -> ss.EventTable:
        found = sorted(folder.rglob("*.vsndevts"))
        ctx.info(f"Reading {len(found)} sound-event file(s) from {folder}")
        table = ss.EventTable()
        failures = 0
        for path in found:
            try:
                table.add_file(path.name,
                               path.read_text(encoding="utf-8", errors="ignore"))
            except ss.SoundscapeError as exc:
                failures += 1
                ctx.warn(f"{path.name}: {exc}")
        if failures:
            ctx.warn(f"{failures} sound-event file(s) could not be read")
        if not len(table):
            raise ValueError(f"No sound events found in {folder}")
        return table

    # -- conversion ----------------------------------------------------

    def _convert(self, ctx: JobContext, picked: set, table: ss.EventTable,
                 options: ss.ConvertOptions, output: Path,
                 params: dict) -> tuple[Path, list[str], set[str]]:
        """Port every picked soundscape into one script file.

        They come from several of Valve's files but land in one of yours: an
        addon wants a single entry in the manifest, not twenty.
        """
        assert self._scripts is not None
        merged = ss.KVBlock()
        sources: list[str] = []
        looping: set[str] = set()
        warnings: list[str] = []
        rows: list[list[str]] = []
        ported = 0

        files = sorted(self._scripts.glob("*.txt"))
        for index, path in enumerate(files, start=1):
            ctx.raise_if_cancelled()
            ctx.step(index, len(files), path.name)
            try:
                result = ss.convert_file(
                    path.read_text(encoding="utf-8", errors="ignore"),
                    table, options)
            except ss.SoundscapeError as exc:
                ctx.error(f"{path.name}: {exc}")
                continue
            if not result.soundscapes:
                continue
            for name, body in result.block:
                merged.add(name, body)
                rows.append([name, path.name, str(len(body))])
            ported += result.soundscapes
            sources.extend(result.sources)
            looping |= result.looping_sources
            warnings.extend(result.warnings)

        prefix = options.name_prefix
        found = {name[len(prefix):] if prefix and name.startswith(prefix)
                 else name for name, _ in merged}
        for name in sorted(picked - found):
            ctx.warn(f"{name}: not found in any soundscape file")

        script = output / "scripts" / f"{params['script_name']}.txt"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(ss.write_kv(merged) + "\n", encoding="utf-8")
        ctx.artifact(script, label=script.name, kind="text")

        ctx.table("Ported", ["Soundscape", "From", "Rules"], rows)
        self._report_warnings(ctx, warnings)

        ordered: list[str] = []
        seen: set[str] = set()
        for item in sources:
            if item not in seen:
                seen.add(item)
                ordered.append(item)
        ctx.metric("Soundscapes", ported)
        ctx.metric("Distinct sounds", len(ordered))
        return script, ordered, looping

    def _report_warnings(self, ctx: JobContext, warnings: list[str]) -> None:
        if not warnings:
            return
        if bool(ctx.prefs.get("report_warnings", False)):
            for note in warnings:
                ctx.warn(note)
            return
        missing = [w for w in warnings if "not found in any" in w]
        folded = [w for w in warnings if "surround" in w or "quad" in w]
        silent = [w for w in warnings if "nothing to play" in w]
        if missing:
            ctx.warn(f"{len(missing)} event(s) were not in any .vsndevts")
        if folded:
            ctx.info(f"{len(folded)} surround bed(s) folded to their front pair")
        if silent:
            ctx.warn(f"{len(silent)} soundscape(s) came out empty")
        ctx.info("Turn on 'List every conversion warning' in Settings for the "
                 "full list.")

    # -- manifest ------------------------------------------------------

    def _write_manifest(self, ctx: JobContext, params: dict, script: Path,
                        output: Path) -> None:
        """Write the manifest, merging with the stock one where we can.

        Garry's Mod reads exactly one `scripts/soundscapes_manifest.txt`. An
        addon that ships its own replaces the stock list, which silently turns
        off every soundscape the base game and its mounted content provide.
        """
        mode = str(params.get("manifest", "merge"))
        if mode == "none":
            ctx.info(f'Add "file" "scripts/{script.name}" to your soundscapes '
                     f"manifest to load this.")
            return

        entries: list[str] = []
        if mode == "merge":
            entries = self._stock_manifest(ctx, params)
            if not entries:
                ctx.warn("Could not read Garry's Mod's manifest, so this one "
                         "lists only the ported file - it will replace the "
                         "stock list. Set the Garry's Mod folder to merge.")
        line = f"scripts/{script.name}"
        if line not in entries:
            entries.append(line)

        body = ss.KVBlock()
        for entry in entries:
            body.add("file", entry)
        root = ss.KVBlock()
        root.add("soundscapes_manifest", body)
        destination = output / "scripts" / "soundscapes_manifest.txt"
        destination.write_text(ss.write_kv(root) + "\n", encoding="utf-8")
        ctx.artifact(destination, label=destination.name, kind="text")
        ctx.info(f"Manifest lists {len(entries)} file(s)")

    def _stock_manifest(self, ctx: JobContext, params: dict) -> list[str]:
        """The files Garry's Mod's own manifest lists, or [] if unreadable."""
        folder = Path(str(params.get("gmod_dir", "") or "")) \
            if params.get("gmod_dir") else GMOD_GUESS
        if not folder.is_dir():
            return []
        text = ""
        loose = folder / "scripts" / "soundscapes_manifest.txt"
        if loose.is_file():
            text = loose.read_text(encoding="utf-8", errors="ignore")
        else:
            archive = folder / "garrysmod_dir.vpk"
            if not archive.is_file():
                return []
            scratch = Path(tempfile.mkdtemp(prefix="s2kit-manifest-"))
            try:
                self._extract(archive, ["scripts/soundscapes_manifest.txt"],
                              scratch, log=ctx.debug)
                found = scratch / "scripts" / "soundscapes_manifest.txt"
                if found.is_file():
                    text = found.read_text(encoding="utf-8", errors="ignore")
            except Exception as exc:
                ctx.debug(f"stock manifest: {exc}")
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
        if not text:
            return []
        try:
            root = ss.parse_kv(text)
        except ss.SoundscapeError:
            return []
        block = root.block("soundscapes_manifest")
        if block is None:
            return []
        return [value for value in block.all("file") if isinstance(value, str)]

    # -- audio ---------------------------------------------------------

    def _bring_audio(self, ctx: JobContext, params: dict, vpk: Path,
                     sources: list[str], looping: set[str], output: Path,
                     options: ss.ConvertOptions) -> None:
        if not sources:
            ctx.warn("No sounds were referenced - nothing to extract.")
            return
        ctx.status(f"Extracting {len(sources)} sound(s)")
        scratch = Path(tempfile.mkdtemp(prefix="s2kit-audio-"))
        try:
            self._extract(vpk, sources, scratch, log=ctx.debug,
                          cancel=ctx.cancel_event)

            target_format = str(params["format"])
            want_loops = bool(params.get("loop_points", True))
            ffmpeg_override = str(ctx.prefs.get("ffmpeg_path", "") or "")
            sound_root = output / "sound"
            preview_limit = int(ctx.prefs.get("preview_limit", 6) or 0)

            converted = missing = looped = 0
            total_bytes = 0
            for index, source in enumerate(sources, start=1):
                ctx.raise_if_cancelled()
                ctx.progress(index, len(sources))
                decoded = self._find_decoded(scratch, source)
                if decoded is None:
                    missing += 1
                    ctx.warn(f"not in the VPK: {source}")
                    continue
                relative = ss.Layer(path=source).wave(options.extension,
                                                      options.wave_prefix)
                destination = sound_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if target_format == "keep":
                        destination = destination.with_suffix(decoded.suffix)
                        shutil.copy2(decoded, destination)
                    else:
                        audio.encode(decoded, destination,
                                     _spec_for(target_format),
                                     ffmpeg_override=ffmpeg_override,
                                     cancel=ctx.cancel_event)
                    if (want_loops and source in looping
                            and destination.suffix.lower() == ".wav"):
                        audio.write_wav_loop(destination)
                        looped += 1
                except (audio.AudioError, ProcessError, OSError) as exc:
                    ctx.error(f"{source}: {exc}")
                    continue
                converted += 1
                total_bytes += destination.stat().st_size
                if converted <= preview_limit:
                    ctx.artifact(destination, label=destination.name,
                                 kind="audio")

            ctx.metric("Sounds written", converted)
            ctx.metric("Loop points", looped)
            if missing:
                ctx.metric("Not in the VPK", missing)
            ctx.metric("Audio size", human_size(total_bytes))
            ctx.summary(f"{converted} sound(s), {looped} looped")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    @staticmethod
    def _find_decoded(staging: Path, source: str) -> Optional[Path]:
        """VRF names the output by codec, so the extension is not known ahead."""
        relative = source.replace("\\", "/").lstrip("/")
        stem = relative.rsplit(".", 1)[0]
        candidate = staging / stem
        for suffix in (".wav", ".mp3", ".ogg", ".flac"):
            found = candidate.with_suffix(suffix)
            if found.is_file():
                return found
        matches = sorted(candidate.parent.glob(candidate.name + ".*")) \
            if candidate.parent.is_dir() else []
        return matches[0] if matches else None

    # -- VRF -----------------------------------------------------------

    def _extract(self, vpk: Path, filters: list[str], destination: Path, *,
                 log: Any = None,
                 cancel: Optional[threading.Event] = None) -> None:
        """Pull a set of internal VPK paths out, in as few calls as fit."""
        exe = vrf.resolve_vrf_executable(
            settings_path=str(self.ctx.settings.get("vrf_cli_path", "") or ""))
        destination.mkdir(parents=True, exist_ok=True)
        sink = log if log is not None else (lambda _line: None)
        chunks = [filters[i:i + FILTER_CHUNK]
                  for i in range(0, len(filters), FILTER_CHUNK)]
        for chunk in chunks:
            args = [str(exe), "-i", str(vpk), "-o", str(destination),
                    "-f", ",".join(chunk), "-d"]
            code = run_streamed(args, sink, cancel=cancel)
            if code != 0:
                raise ValueError(f"Source2Viewer-CLI exited with {code} while "
                                 f"reading {vpk.name}")
