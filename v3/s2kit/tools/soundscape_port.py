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

from s2kit.definitions.soundscape_port import FORMATS, MANIFESTS, SOUNDLEVELS

import shutil
import tempfile
import threading
from pathlib import Path, PurePosixPath
from typing import Any, Optional


from s2kit.core.jobs import JobContext
from s2kit.core.params import options
from s2kit.core.process import ProcessError, run_streamed
from s2kit.media.probe import human_size
from s2kit.pipelines import audio_ops as audio
from s2kit.pipelines import soundscape as ss
from s2kit.pipelines import vrf
from s2kit.definitions.soundscape_port import SoundscapePorter as _SoundscapePorterDefinition

#: Longest `-f` filter list to hand one Source2Viewer-CLI call. The command
#: line is the limit, not the tool, so this is deliberately conservative.
FILTER_CHUNK = 120

#: Where Garry's Mod usually lives, for finding the stock manifest to merge
#: with. Only a fallback - the path is a parameter.
GMOD_GUESS = Path(r"C:/Program Files (x86)/Steam/steamapps/common/GarrysMod"
                  r"/garrysmod")


def _spec_for(target_format: str) -> "audio.EncodeSpec":
    """The encode settings for a target container.

    WAV is PCM because that is the only thing Source will loop; Vorbis quality
    4 matches what the Sound Converter calls default.
    """
    if target_format == "wav":
        return audio.EncodeSpec(codec="pcm_s16le", quality=None)
    return audio.EncodeSpec(codec="libvorbis", quality=4.0)


class SoundscapePorter(_SoundscapePorterDefinition):
    """Resolve, select and port soundscapes without page state."""

    def __init__(self):
        self._scratch = None
        self._scripts = None
        self._prefs = {}
        self._cancel = None
        self._log = None

    def validate(self, values: dict) -> dict[str, str]:
        """Reject incomplete or unsafe explicit selection requests."""
        errors = {}
        source = str(values.get("source") or "")
        vpk = str(values.get("vpk") or "")
        listing = values.get("action") == "list"
        if not source and not vpk:
            errors["source"] = "Set a content folder or a VPK"
        if source and not Path(source).is_dir():
            errors["source"] = "Content folder does not exist"
        if vpk and not Path(vpk).is_file():
            errors["vpk"] = "VPK does not exist"
        if not listing:
            if values.get("extract_audio", True) and not vpk:
                errors["vpk"] = "Extracting audio needs a VPK"
            if not str(values.get("soundscapes") or "").strip():
                errors["soundscapes"] = "Specify one soundscape name per line"
            for key in ("namespace", "script_name"):
                value = str(values.get(key) or "")
                path = PurePosixPath(value.replace("\\", "/"))
                if not value.strip() or path.is_absolute() or ".." in path.parts or ":" in value:
                    errors[key] = "Use a nonempty relative name without parent references"
        return errors

    def run_job(self, ctx: JobContext) -> None:
        """List or port explicitly named soundscapes from independently resolved inputs."""
        with tempfile.TemporaryDirectory(prefix="s2kit-scapes-") as temporary:
            self._scratch = Path(temporary)
            self._prefs = ctx.prefs
            self._cancel = ctx.cancel_event
            self._log = ctx.debug
            params = ctx.params
            vpk = Path(params["vpk"]) if params.get("vpk") else None
            self._scripts, events = self._resolve_trees(
                Path(params["source"]) if params.get("source") else None, vpk,
                scripts_only=params.get("action") == "list")
            entries = []
            for path in sorted(self._scripts.glob("*.txt")):
                ctx.raise_if_cancelled()
                if "manifest" not in path.name.lower():
                    entries.extend(ss.index_soundscapes(path.read_text(encoding="utf-8", errors="ignore"), path.name))
            if params.get("action") == "list":
                ctx.table("Soundscapes", ["Name", "Source", "Kind"],
                          [[entry.name, entry.source, entry.kind] for entry in entries])
                ctx.metric("Soundscapes", len(entries))
                ctx.summary(f"{len(entries)} soundscapes available")
                return
            picked = {name.strip() for name in str(params["soundscapes"]).splitlines() if name.strip()}
            unknown = picked - {entry.name for entry in entries}
            if unknown:
                raise ValueError("Unknown soundscapes: " + ", ".join(sorted(unknown)))
            output = Path(params["output"])
            output.mkdir(parents=True, exist_ok=True)
            table = self._load_events(ctx, events)
            extension = "." + ("wav" if params["format"] == "keep" else params["format"])
            options = ss.ConvertOptions(
                extension=extension, soundlevel=str(params.get("soundlevel") or ""),
                quad_fold=bool(params["quad_fold"]), max_stems=int(params["max_stems"]),
                keep_source1=bool(params["keep_source1"]), wave_prefix=params["namespace"],
                name_prefix=params["name_prefix"], only=picked)
            script, sources, looping = self._convert(ctx, picked, table, options, output, params)
            self._write_manifest(ctx, params, script, output)
            if params["extract_audio"] and vpk:
                self._bring_audio(ctx, params, vpk, sources, looping, output, options)
            ctx.summary(f"Ported {len(picked)} soundscapes")

    def _resolve_trees(self, source: Optional[Path],
                       vpk: Optional[Path], *, scripts_only: bool = False) -> tuple[Path, Path]:
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
        if scripts is not None and (events is not None or scripts_only):
            return scripts, events or scripts / "soundevents"

        if vpk is None or not vpk.is_file():
            missing = "scripts" if scripts is None else "soundevents"
            raise ValueError(f"no {missing}/ in the content folder, and no VPK "
                             f"to read it from")
        if self._scratch is None:
            self._scratch = Path(tempfile.mkdtemp(prefix="s2kit-scapes-"))
        wanted: list[str] = []
        if scripts is None:
            wanted.append("scripts/soundscapes")
        if events is None and not scripts_only:
            wanted.append("soundevents/")
        self._extract(vpk, wanted, self._scratch, log=self._log, cancel=self._cancel)
        return (scripts or self._scratch / "scripts",
                events or self._scratch / "soundevents")

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

    def _extract(self, vpk: Path, filters: list[str], destination: Path, *,
                 log: Any = None,
                 cancel: Optional[threading.Event] = None) -> None:
        """Pull a set of internal VPK paths out, in as few calls as fit."""
        exe = vrf.resolve_vrf_executable(
            settings_path=str(self._prefs.get("vrf_cli_path", "") or ""))
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
