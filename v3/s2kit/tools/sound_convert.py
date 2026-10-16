"""
Sound Converter and Loop Point Converter.

Two focused tools that sit either side of the Sound Optimizer:

* **Sound Converter** - plain format/rate/channel conversion when you know
  exactly what you want, without the optimiser's opinions.
* **Loop Point Converter** - writes the RIFF `smpl` chunk that tells Source
  where an ambience loops. This is the tool whose output the optimiser
  deliberately refuses to touch.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from s2kit.core.jobs import JobContext
from s2kit.core.params import (
    BoolParam, ChoiceParam, FloatParam, IntParam, PathParam, options,
)
from s2kit.core.registry import tool
from s2kit.media.probe import human_size
from s2kit.pipelines import audio_ops as audio
from s2kit.tools._sounds_shared import sound_filter_params, sound_filter_values
from s2kit.ui.tool_page import ToolPage

FORMATS = options(
    ("ogg", "OGG Vorbis"),
    ("wav", "WAV (PCM 16-bit)"),
    ("mp3", "MP3"),
)

RATES = options(
    (0, "Keep the source rate"),
    (44100, "44.1 kHz"),
    (22050, "22.05 kHz"),
    (11025, "11.025 kHz"),
)

CHANNELS = options(
    (0, "Keep"),
    (1, "Mono"),
    (2, "Stereo"),
)


@tool(
    id="sound-convert",
    title="Sound Converter",
    category="Sounds",
    icon="MULTITRACK_AUDIO",
    description="Convert sounds between OGG, WAV and MP3 with explicit rate "
                "and channel control.",
    keywords=("ogg", "wav", "mp3", "convert", "vorbis", "resample", "mono",
              "stereo", "audio"),
    order=20,
)
class SoundConverter(ToolPage):
    """Straight format conversion, no optimisation heuristics."""

    RUN_LABEL = "Convert"
    RUN_ICON = "MULTITRACK_AUDIO"

    PARAMS = [
        PathParam("folder", "Sound folder", mode="directory", required=True,
                  group="Input"),
        BoolParam("recursive", "Include subfolders", default=True,
                  group="Input"),
        *sound_filter_params(),
        ChoiceParam("format", "Convert to", default="ogg", choices=FORMATS,
                    style="segmented", group="Output"),
        PathParam("output", "Output folder", mode="directory", must_exist=False,
                  group="Output",
                  help="Empty writes next to each source file."),
        BoolParam("keep_source", "Keep the source files", default=True,
                  group="Output"),
        BoolParam("overwrite", "Overwrite existing outputs", default=False,
                  group="Output"),

        FloatParam("quality", "Vorbis quality", default=5.0, min=-1.0, max=10.0,
                   step=1.0, decimals=1, group="Encoding",
                   depends_on=("format", "ogg")),
        IntParam("bitrate", "MP3 bitrate", default=128, min=32, max=320,
                 step=16, suffix="kbps", group="Encoding",
                 depends_on=("format", "mp3")),
        ChoiceParam("rate", "Sample rate", default=0, choices=RATES,
                    group="Encoding"),
        ChoiceParam("channels", "Channels", default=0, choices=CHANNELS,
                    group="Encoding"),
        BoolParam("reconvert", "Re-convert files already in this format",
                  default=False, group="Encoding",
                  help="Off, a file that already has the target extension is "
                       "left alone. On, it is re-encoded too - which is how "
                       "you apply a new bitrate, sample rate or channel count "
                       "to an existing set of OGGs, WAVs or MP3s. Note that "
                       "re-encoding a lossy file (OGG, MP3) loses a little "
                       "more quality each time."),
        BoolParam("skip_looping", "Skip sounds with loop points", default=True,
                  group="Encoding",
                  help="Converting a looping WAV to OGG drops the loop."),
    ]

    PREFERENCES = [
        IntParam("workers", "Files converted in parallel", default=4, min=1,
                 max=32, group="Performance"),
        BoolParam("preview_results", "Add results to Preview", default=True,
                  group="Report"),
    ]

    HELP = """
### Sound Converter

Format conversion with the settings you pick, applied verbatim - no size
heuristics, no skipping of files that would grow. Use the **Sound Optimizer**
when you want the size decisions made for you.

Vorbis quality maps roughly: `q0` ~64 kbps, `q4` ~128, `q6` ~192, `q10` ~500.
GMod plays `.ogg`, `.wav` and `.mp3`; OGG is the sane default for anything
that is not a short SFX.

#### Files already in the target format

By default they are left alone - converting OGG to OGG normally just loses
quality. Turn on **Re-convert files already in this format** when that is the
point: dropping a folder of 320 kbps MP3s to 128, forcing a set of OGGs to
22.05 kHz mono, or re-encoding WAVs to 16-bit PCM at a lower rate. Each pass
over a lossy format costs a little more quality, so do it from the original
where you can.

Re-converting writes in place through a temporary file and swaps it in, so a
failed or cancelled encode leaves the original untouched.

Sounds carrying loop points are skipped by default - see the **Loop Point
Converter** for what that metadata is.
"""

    def run_job(self, ctx: JobContext) -> None:
        params = ctx.params
        folder = Path(str(params["folder"]))
        if not folder.is_dir():
            raise ValueError(f"Not a folder: {folder}")
        override = str(ctx.prefs.get("ffmpeg_path", "") or "")
        audio.ffmpeg_binary(override)

        target_format = str(params["format"])
        found = audio.find_sounds(folder, recursive=bool(params["recursive"]),
                                  **sound_filter_values(params))
        if not found:
            ctx.warn("No sound files match the input filters.")
            ctx.summary("Nothing to convert")
            return
        sounds = [path for path in found
                  if path.suffix.lower() != f".{target_format}"
                  or bool(params["reconvert"])]
        if not sounds:
            already = len(found)
            ctx.warn(f"Nothing to convert - all {already} file(s) are already "
                     f"{target_format.upper()}. Turn on 'Re-convert files "
                     "already in this format' to re-encode them.")
            ctx.summary(f"No files to convert ({already} already "
                        f"{target_format.upper()})")
            return
        if len(sounds) < len(found):
            ctx.info(f"Skipping {len(found) - len(sounds)} file(s) already in "
                     f"{target_format.upper()}")
        ctx.info(f"Converting {len(sounds)} file(s) to {target_format.upper()}")

        output_root = Path(str(params["output"])) if params["output"] else None
        spec = audio.EncodeSpec(
            codec={"ogg": "libvorbis", "wav": "pcm_s16le",
                   "mp3": "libmp3lame"}[target_format],
            quality=(float(params["quality"]) if target_format == "ogg"
                     else None),
            bitrate=(int(params["bitrate"]) if target_format == "mp3" else None),
            sample_rate=int(params["rate"]) or None,
            channels=int(params["channels"]) or None,
            strip_metadata=True,
        )

        def convert(path: Path) -> dict:
            info = audio.probe(path, ffmpeg_override=override,
                               cancel=ctx.cancel_event)
            if bool(params["skip_looping"]) and info.protected:
                return {"path": path, "status": "skipped",
                        "reason": "has loop/cue points", "before": info.size,
                        "after": 0, "output": None}

            destination = _destination(path, folder, output_root, target_format)
            in_place = destination == path
            if destination.exists() and not in_place \
                    and not bool(params["overwrite"]):
                return {"path": path, "status": "skipped",
                        "reason": "output exists", "before": info.size,
                        "after": destination.stat().st_size,
                        "output": destination}

            # ffmpeg cannot read and write the same file, which is exactly what
            # re-converting in place asks for. Encode beside it, then swap.
            written = (destination.with_name(f"{destination.stem}.s2kit-tmp"
                                             f"{destination.suffix}")
                       if in_place else destination)
            audio.encode(path, written, spec, ffmpeg_override=override,
                         cancel=ctx.cancel_event)
            if in_place:
                os.replace(written, destination)
            elif not bool(params["keep_source"]):
                path.unlink(missing_ok=True)
            return {"path": path, "status": "converted", "reason": "",
                    "before": info.size, "after": destination.stat().st_size,
                    "output": destination}

        outcome = ctx.map(sounds, convert,
                          workers=int(ctx.prefs.get("workers", 4)),
                          label=lambda path: path.name, unit="sound",
                          log_each=False)
        rows = [row for row in outcome.results if row is not None]

        converted = [row for row in rows if row["status"] == "converted"]
        before = sum(row["before"] for row in rows)
        after = sum(row["after"] or row["before"] for row in rows)
        ctx.metric("Converted", len(converted),
                   tone="good" if converted else "neutral")
        ctx.metric("Before", human_size(before))
        ctx.metric("After", human_size(after))
        if outcome.failed:
            ctx.metric("Failed", outcome.failed, tone="bad")

        ctx.table("Conversions", ["File", "Result", "Before", "After", "Output"],
                  [[row["path"].name, row["status"], human_size(row["before"]),
                    human_size(row["after"]) if row["after"] else "",
                    str(row["output"] or row["reason"])] for row in rows],
                  path_column=4)

        if bool(ctx.prefs.get("preview_results", True)):
            for row in converted[:6]:
                if row["output"]:
                    ctx.artifact(row["output"], label=row["output"].name)

        summary = (f"converted {len(converted)}/{len(rows)} to "
                   f"{target_format.upper()}, {human_size(before)} -> "
                   f"{human_size(after)}")
        ctx.success(summary)
        ctx.summary(summary)


def _destination(path: Path, root: Path, output_root: Optional[Path],
                 target_format: str) -> Path:
    """Mirror the source tree under the output root, or write in place."""
    if output_root is None:
        return path.with_suffix(f".{target_format}")
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = Path(path.name)
    return (output_root / relative).with_suffix(f".{target_format}")


@tool(
    id="loop-points",
    title="Loop Point Converter",
    category="Sounds",
    icon="LOOP",
    description="Write the RIFF smpl chunk that makes Source loop a sound "
                "seamlessly.",
    keywords=("loop", "smpl", "cue", "ambient", "ambience", "wav", "seamless",
              "music"),
    order=30,
)
class LoopPointConverter(ToolPage):
    """Convert to WAV and embed Source loop points."""

    RUN_LABEL = "Add loop points"
    RUN_ICON = "LOOP"

    PARAMS = [
        PathParam("folder", "Sound folder", mode="directory", required=True,
                  group="Input"),
        BoolParam("recursive", "Include subfolders", default=True,
                  group="Input"),
        *sound_filter_params(),
        PathParam("output", "Output folder", mode="directory", must_exist=False,
                  group="Output",
                  help="Empty writes the WAVs next to their sources."),
        BoolParam("overwrite", "Overwrite existing WAVs", default=True,
                  group="Output"),
        BoolParam("keep_source", "Keep the source files", default=True,
                  group="Output"),

        ChoiceParam("span", "Loop the", default="whole", group="Loop",
                    style="segmented",
                    choices=options(("whole", "Whole file"),
                                    ("custom", "Sample range"))),
        IntParam("loop_start", "Loop start", default=0, min=0, max=1 << 30,
                 suffix="samples", group="Loop",
                 depends_on=("span", "custom")),
        IntParam("loop_end", "Loop end", default=0, min=0, max=1 << 30,
                 suffix="samples", group="Loop", depends_on=("span", "custom"),
                 help="0 means the last sample."),
        ChoiceParam("rate", "Resample to", default=0, choices=RATES,
                    group="Loop", advanced=True),
    ]

    PREFERENCES = [
        IntParam("workers", "Files processed in parallel", default=4, min=1,
                 max=32, group="Performance"),
    ]

    HELP = """
### Loop Point Converter

Source loops a sound when its WAV carries a RIFF **`smpl`** chunk naming the
first and last sample of the loop. Without it, an ambience restarts with an
audible gap, and `SND_CHANGEVOL`-style looping entities will not behave.

This tool converts each input to 16-bit PCM WAV (the only container that can
carry the chunk) and writes a single forward loop with an infinite play count.
Any existing `smpl` chunk is replaced rather than appended, because Source
reads the first one it finds.

**Ogg Vorbis cannot carry loop points**, which is why the Sound Optimizer
refuses to re-encode files that have them. If you want both small files and
looping ambience, keep the looping sounds as WAV and optimise everything else.
"""

    def run_job(self, ctx: JobContext) -> None:
        params = ctx.params
        folder = Path(str(params["folder"]))
        if not folder.is_dir():
            raise ValueError(f"Not a folder: {folder}")
        override = str(ctx.prefs.get("ffmpeg_path", "") or "")
        audio.ffmpeg_binary(override)

        sounds = audio.find_sounds(folder, recursive=bool(params["recursive"]),
                                   **sound_filter_values(params))
        if not sounds:
            ctx.warn("No sound files found.")
            ctx.summary("Nothing to loop")
            return

        output_root = Path(str(params["output"])) if params["output"] else None
        custom = str(params["span"]) == "custom"
        spec = audio.EncodeSpec(codec="pcm_s16le", quality=None,
                                sample_rate=int(params["rate"]) or None,
                                strip_metadata=False)

        def process(path: Path) -> dict:
            destination = _destination(path, folder, output_root, "wav")
            if (destination.exists() and destination != path
                    and not bool(params["overwrite"])):
                return {"path": path, "status": "skipped",
                        "reason": "output exists", "start": "", "end": "",
                        "output": destination}

            if path.suffix.lower() == ".wav" and destination == path:
                working = path
            else:
                audio.encode(path, destination, spec, ffmpeg_override=override,
                             cancel=ctx.cancel_event)
                working = destination

            start, end = audio.write_wav_loop(
                working,
                loop_start=int(params["loop_start"]) if custom else 0,
                loop_end=(int(params["loop_end"]) or None) if custom else None,
            )
            if not bool(params["keep_source"]) and working != path:
                path.unlink(missing_ok=True)
            return {"path": path, "status": "looped", "reason": "",
                    "start": start, "end": end, "output": working}

        outcome = ctx.map(sounds, process,
                          workers=int(ctx.prefs.get("workers", 4)),
                          label=lambda path: path.name, unit="sound",
                          log_each=False)
        rows = [row for row in outcome.results if row is not None]
        looped = [row for row in rows if row["status"] == "looped"]

        ctx.metric("Looped", len(looped), tone="good" if looped else "neutral")
        if outcome.failed:
            ctx.metric("Failed", outcome.failed, tone="bad")
        ctx.table("Loop points",
                  ["File", "Result", "Loop start", "Loop end", "Output"],
                  [[row["path"].name, row["status"], row["start"], row["end"],
                    str(row["output"] or row["reason"])] for row in rows],
                  numeric_columns=(2, 3), path_column=4)

        for row in looped[:6]:
            ctx.artifact(row["output"], label=row["output"].name)

        summary = f"wrote loop points into {len(looped)}/{len(rows)} file(s)"
        ctx.success(summary)
        ctx.summary(summary)
