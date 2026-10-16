"""
Sound Optimizer.

Shrinks an addon's sound folder using the passes GMod content optimisers have
converged on - Vorbis re-encode, sample-rate capping, mono downmix, loudness
normalisation, metadata stripping, trailing-silence trimming - plus duplicate
detection and reference rewriting so the addon still works afterwards.

Techniques taken from fOptimizer, gm_content_optimizer and
GMod-Optimization-Tool; see :mod:`s2kit.pipelines.audio_ops` for the settings
and the reasoning. The one rule this tool adds on top: a WAV carrying loop or
cue metadata is left alone by default, because that metadata is what makes
Source loop an ambience and Vorbis cannot carry it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from s2kit.core.jobs import JobCancelled, JobContext
from s2kit.core.params import (
    BoolParam, ChoiceParam, FloatParam, IntParam, PathParam, options,
)
from s2kit.core.registry import tool
from s2kit.media.probe import human_size
from s2kit.pipelines import audio_ops as audio
from s2kit.tools._sounds_shared import sound_filter_params, sound_filter_values
from s2kit.ui.tool_page import ToolPage

#: Nominal stereo bitrates from the Xiph quality table.
QUALITY_CHOICES = options(
    (0.0, "q0 - smallest (~64 kbps)"),
    (2.0, "q2 - small (~96 kbps)"),
    (3.0, "q3 - lean (~112 kbps)"),
    (4.0, "q4 - balanced (~128 kbps)"),
    (6.0, "q6 - high (~192 kbps)"),
    (8.0, "q8 - near-transparent (~256 kbps)"),
)

RATE_CHOICES = options(
    (44100, "44.1 kHz (keep CD quality)"),
    (22050, "22.05 kHz (half - fine for most SFX)"),
    (11025, "11.025 kHz (radio chatter, UI blips)"),
    (0, "Leave alone"),
)


@tool(
    id="sound-optimizer",
    title="Sound Optimizer",
    category="Sounds",
    icon="COMPRESS",
    description="Shrink an addon's sounds: Vorbis re-encode, downsample, mono, "
                "normalise, trim silence, de-duplicate.",
    keywords=("optimise", "optimize", "compress", "ogg", "vorbis", "shrink",
              "size", "addon", "workshop", "duplicate", "loudnorm"),
    order=10,
)
class SoundOptimizer(ToolPage):
    """Batch sound optimisation with a dry run that reports the savings."""

    RUN_LABEL = "Optimize"
    RUN_ICON = "COMPRESS"

    PARAMS = [
        PathParam("folder", "Sound folder", mode="directory", required=True,
                  group="Input",
                  help="Usually your addon's sound/ folder. Every file "
                       "underneath is considered."),
        BoolParam("recursive", "Include subfolders", default=True,
                  group="Input"),
        *sound_filter_params(),
        BoolParam("dry_run", "Dry run (report only, change nothing)",
                  default=True, group="Input",
                  help="Leave this on for the first pass - the report tells "
                       "you exactly what each file would become."),

        ChoiceParam("quality", "Vorbis quality", default=4.0,
                    choices=QUALITY_CHOICES, group="Encode",
                    help="q4 is where the GMod optimisers settled: about a "
                         "tenth the size of a WAV with no audible loss in "
                         "game. Drop to q2 for chatter and UI blips."),
        ChoiceParam("max_rate", "Cap the sample rate at", default=44100,
                    choices=RATE_CHOICES, group="Encode",
                    help="Source resamples anything unusual at load; these "
                         "are the rates it handles natively."),
        BoolParam("force_mono", "Force everything to mono", default=False,
                  group="Encode",
                  help="Halves the size. 3D-positioned sounds are played mono "
                       "by the engine anyway."),
        FloatParam("mono_below", "Mono for clips shorter than", default=0.0,
                   min=0.0, max=30.0, step=0.5, decimals=1, suffix="seconds",
                   group="Encode", depends_on=("force_mono", False),
                   help="0 disables. A good middle ground: short SFX go mono, "
                        "music and ambience stay stereo."),

        BoolParam("reencode_ogg", "Re-encode files that are already OGG",
                  default=False, group="Encode",
                  help="Vorbis to Vorbis is a generational transcode - it "
                       "decodes lossy audio and throws more away. Off, OGGs "
                       "still get the lossless metadata strip."),
        IntParam("ogg_floor", "...but only above", default=160, min=0, max=500,
                 step=16, suffix="kbps", group="Encode",
                 depends_on=("reencode_ogg", True),
                 help="Catches bloated music while leaving already-lean "
                      "effects alone. 0 re-encodes every OGG."),

        BoolParam("normalise", "Normalise loudness", default=False,
                  group="Clean up",
                  help="loudnorm to -14 LUFS, so one sound is not far louder "
                       "than the rest of the pack."),
        BoolParam("strip_metadata", "Strip tags and cover art", default=True,
                  group="Clean up",
                  help="Album art embedded in an MP3 can be most of its size."),
        BoolParam("trim_silence", "Trim trailing silence", default=False,
                  group="Clean up",
                  help="Cuts silence at the end (below -55 dB) with a short "
                       "fade. Leading silence is never touched - it is often "
                       "deliberate timing."),

        BoolParam("skip_looping", "Never touch looping sounds", default=True,
                  group="Safety",
                  help="WAVs with smpl/cue chunks drive Source's looping "
                       "ambience. Vorbis cannot carry those points, so "
                       "re-encoding silently breaks the loop."),
        BoolParam("keep_names", "Keep the original filenames", default=False,
                  group="Safety",
                  help="Writes OGG data into the existing .wav/.mp3 name. "
                       "Nothing else needs updating, and GMod reads the file "
                       "by content - but it is confusing to look at."),
        BoolParam("rewrite_refs", "Update references in Lua/text files",
                  default=True, group="Safety",
                  depends_on=("keep_names", False),
                  help="Rewrites sound paths in .lua/.txt/.json/.res so "
                       "renamed files still resolve."),
        PathParam("refs_root", "Search for references in", mode="directory",
                  must_exist=False, group="Safety",
                  depends_on=("keep_names", False),
                  help="Defaults to the addon root above the sound folder."),
        FloatParam("min_saving", "Only keep results that save at least",
                   default=2.0, min=0.0, max=90.0, step=1.0, decimals=0,
                   suffix="%", group="Safety", advanced=True,
                   help="Stops a short clip getting bigger when its source "
                        "was already well compressed."),
    ]

    PREFERENCES = [
        IntParam("workers", "Files processed in parallel", default=4, min=1,
                 max=32, group="Performance",
                 help="Each file is one ffmpeg process."),
        BoolParam("report_duplicates", "Report byte-identical duplicates",
                  default=True, group="Report",
                  help="The same footstep copied into six folders is usually "
                       "the biggest single win in a scraped addon."),
        BoolParam("list_files", "List every file in a table", default=True,
                  group="Report"),
        BoolParam("preview_results", "Add optimised sounds to Preview",
                  default=True, group="Report"),
        IntParam("preview_limit", "Preview at most", default=6, min=0, max=50,
                 suffix="files", group="Report"),
    ]

    HELP = """
### Sound Optimizer

Sound is usually the cheapest size win in a Garry's Mod addon: a folder of
44.1 kHz stereo WAVs routinely drops by 80-90% with nothing audible changing
in game.

#### The passes

| Pass | What it does | Typical saving |
| --- | --- | --- |
| **Vorbis re-encode** | WAV/MP3 to OGG at `-q:a 4` (~128 kbps) | 85-92% off a WAV |
| **Sample-rate cap** | 48 kHz down to 44.1 (or lower) | 8% per halving |
| **Mono downmix** | Two channels to one | ~50% |
| **Strip metadata** | `-map_metadata -1` | whatever the cover art weighed |
| **OGG re-encode** | only above the bitrate floor, opt-in | varies |
| **Trim silence** | Cuts trailing silence below -55 dB | varies wildly |
| **Loudness** | `loudnorm=I=-14:TP=-1.5:LRA=11` | none - it evens levels out |

#### Files that are already OGG

Re-encoding Vorbis to Vorbis is a *generational* transcode: it decodes audio
that has already been through a lossy encoder and throws more away. Doing that
to a whole sound folder on every run quietly degrades it.

So OGGs are stream-copied by default - `-c:a copy` with `-map_metadata -1`,
which drops the tags without touching a single audio byte. Turn
**Re-encode files that are already OGG** on when a pack ships oversized OGGs,
and the bitrate floor keeps it to the ones actually worth re-encoding: at the
default of 160 kbps a 320 kbps music track is re-encoded and a 96 kbps effect
is left alone. Set the floor to 0 to re-encode every OGG regardless.

Note that a stream copy cannot run filters, so loudness normalisation and the
silence trim do not apply to files taking that path.

#### What it refuses to break

* **Looping sounds.** A WAV with a `smpl` or `cue ` chunk is how Source knows
  where an ambience loops. Ogg Vorbis has no equivalent, so those files are
  reported and skipped. Turn the safety off only if you know the sound is not
  looped in game.
* **References.** Renaming `foo.wav` to `foo.ogg` breaks every
  `EmitSound`/`sound.Add` that names it, so the matching paths in your
  `.lua`, `.txt`, `.json` and `.res` files are rewritten to match.
* **Files that would grow.** A short clip re-encoded from an already-small
  source can come out bigger; those are left as they were.

#### Suggested first run

Leave **Dry run** on and press Optimize. The Output tab then shows the
per-file before/after and the total saving, and you can decide whether to
turn the aggressive passes on before doing it for real.

*Techniques adapted from [fOptimizer](https://github.com/fxington/fOptimizer),
[gm_content_optimizer](https://github.com/wrefgtzweve/gm_content_optimizer) and
[GMod-Optimization-Tool](https://github.com/bleonheart/GMod-Optimization-Tool).*
"""

    # ------------------------------------------------------------------

    def run_job(self, ctx: JobContext) -> None:
        params = ctx.params
        folder = Path(str(params["folder"]))
        if not folder.is_dir():
            raise ValueError(f"Not a folder: {folder}")

        audio.ffmpeg_binary(str(ctx.prefs.get("ffmpeg_path", "") or ""))

        ctx.status(f"Scanning {folder}...")
        sounds = audio.find_sounds(folder, recursive=bool(params["recursive"]),
                                   **sound_filter_values(params))
        if not sounds:
            ctx.warn("No sound files found.")
            ctx.summary("Nothing to optimise")
            return
        ctx.info(f"Found {len(sounds)} sound file(s)")

        duplicates: list = []
        if bool(ctx.prefs.get("report_duplicates", True)):
            ctx.status("Hashing for duplicates...")
            duplicates = audio.find_duplicates(sounds)

        options = self._options(ctx)
        if options.dry_run:
            ctx.warn("Dry run - nothing will be changed.")

        ffmpeg_override = str(ctx.prefs.get("ffmpeg_path", "") or "")

        def optimise(path: Path) -> audio.OptimiseResult:
            return audio.optimise_file(path, options,
                                       ffmpeg_override=ffmpeg_override,
                                       on_log=ctx.debug,
                                       cancel=ctx.cancel_event)

        try:
            outcome = ctx.map(sounds, optimise,
                              workers=int(ctx.prefs.get("workers", 4)),
                              label=lambda path: path.name, unit="sound",
                              log_each=False)
        except audio.AudioError as exc:
            raise RuntimeError(str(exc)) from exc

        results = [r for r in outcome.results if r is not None]
        renames = {r.path.name: r.output.name for r in results
                   if r.output and r.output.name != r.path.name}
        self._rewrite_references(ctx, folder, renames, options.dry_run)
        self._report(ctx, results, duplicates, options)

    # ------------------------------------------------------------------

    def _options(self, ctx: JobContext) -> audio.OptimiseOptions:
        params = ctx.params
        return audio.OptimiseOptions(
            to_ogg=True,
            quality=float(params["quality"]),
            max_sample_rate=int(params["max_rate"]),
            force_mono=bool(params["force_mono"]),
            mono_below_seconds=float(params["mono_below"]),
            normalise=bool(params["normalise"]),
            strip_metadata=bool(params["strip_metadata"]),
            trim_silence=bool(params["trim_silence"]),
            keep_original_name=bool(params["keep_names"]),
            skip_looping=bool(params["skip_looping"]),
            reencode_ogg=bool(params["reencode_ogg"]),
            ogg_bitrate_floor=int(params["ogg_floor"]),
            minimum_saving=float(params["min_saving"]) / 100.0,
            dry_run=bool(params["dry_run"]),
        )

    def _rewrite_references(self, ctx: JobContext, folder: Path,
                            renames: dict[str, str], dry_run: bool) -> None:
        params = ctx.params
        if bool(params["keep_names"]) or not bool(params["rewrite_refs"]):
            return
        if not renames and not dry_run:
            return

        root = Path(str(params["refs_root"] or "")) if params["refs_root"] \
            else folder.parent
        if not root.is_dir():
            ctx.warn(f"Reference root {root} does not exist; skipping rewrite.")
            return

        if dry_run:
            renames = {path.name: f"{path.stem}.ogg"
                       for path in audio.find_sounds(
                           folder, recursive=bool(params["recursive"]),
                           **sound_filter_values(params))
                       if path.suffix.lower() != ".ogg"}
        ctx.status("Updating references...")
        files, edits = audio.rewrite_references(root, renames, dry_run=dry_run,
                                                on_log=ctx.info)
        if edits:
            verb = "would update" if dry_run else "updated"
            ctx.success(f"{verb} {edits} reference(s) in {files} file(s) "
                        f"under {root}")
            ctx.metric("References", edits, tone="good")
        else:
            ctx.info(f"No sound references found under {root}")

    def _report(self, ctx: JobContext, results: list, duplicates: dict,
                options: audio.OptimiseOptions) -> None:
        done = [r for r in results if r.status == "optimised"]
        skipped = [r for r in results if r.status == "skipped"]
        unchanged = [r for r in results if r.status == "unchanged"]
        failed = [r for r in results if r.status == "failed"]

        before = sum(r.before for r in results)
        after = sum((r.after or r.before) for r in results)
        saved = max(0, before - after)
        percent = (saved / before * 100.0) if before else 0.0

        ctx.metric("Sounds", len(results))
        lossless = [r for r in done if r.lossless]
        ctx.metric("Optimised", len(done), tone="good" if done else "neutral")
        if lossless:
            ctx.metric("Copied", len(lossless),
                       tone="neutral")
        ctx.metric("Saved", human_size(saved),
                   tone="good" if saved else "neutral")
        ctx.metric("Reduction", f"{percent:.0f}%",
                   tone="good" if percent >= 25 else "neutral")
        if skipped:
            ctx.metric("Protected", len(skipped), tone="warn")
        if failed:
            ctx.metric("Failed", len(failed), tone="bad")

        if bool(ctx.prefs.get("list_files", True)):
            ctx.table(
                "Sounds",
                ["File", "Result", "Before", "After", "Saved", "Rate", "Ch",
                 "Length", "What changed", "Path"],
                [[r.path.name, r.status, human_size(r.before),
                  human_size(r.after) if r.after else "",
                  f"{(1 - r.ratio) * 100:.0f}%" if r.after and r.before else "",
                  r.info.sample_rate if r.info else "",
                  r.info.channels if r.info else "",
                  f"{r.info.duration:.2f}s" if r.info else "",
                  ", ".join(r.actions) or r.reason, str(r.path)]
                 for r in sorted(results, key=lambda r: -r.saved)],
                numeric_columns=(5, 6), path_column=9,
            )

        if skipped:
            ctx.table(
                f"Protected sounds ({len(skipped)})",
                ["File", "Why", "Loop start", "Loop end", "Path"],
                [[r.path.name, r.reason,
                  r.info.loop_start if r.info else "",
                  r.info.loop_end if r.info else "", str(r.path)]
                 for r in skipped],
                path_column=4,
            )

        if duplicates:
            wasted = sum(group.wasted for group in duplicates)
            ctx.metric("Duplicate waste", human_size(wasted), tone="warn")
            ctx.table(
                f"Byte-identical duplicates ({len(duplicates)} group(s))",
                ["Copies", "Size each", "Wasted", "Files"],
                [[len(group.paths), human_size(group.size),
                  human_size(group.wasted),
                  "  |  ".join(str(path) for path in group.paths)]
                 for group in sorted(duplicates, key=lambda g: -g.wasted)],
                numeric_columns=(0,),
            )
            ctx.warn(f"{len(duplicates)} duplicate group(s) wasting "
                     f"{human_size(wasted)} - deleting the copies and pointing "
                     "the references at one file is the cheapest win here.")

        for result in failed:
            ctx.error(f"{result.path.name}: {result.reason}")
        for result in skipped:
            ctx.warn(f"{result.path.name}: {result.reason}")

        if bool(ctx.prefs.get("preview_results", True)) and not options.dry_run:
            limit = int(ctx.prefs.get("preview_limit", 6))
            for result in done[:limit]:
                if result.output:
                    ctx.artifact(result.output, label=result.output.name)

        verb = "would save" if options.dry_run else "saved"
        summary = (f"{verb} {human_size(saved)} of {human_size(before)} "
                   f"({percent:.0f}%) across {len(done)} file(s)")
        if skipped:
            summary += f", {len(skipped)} protected"
        if failed:
            summary += f", {len(failed)} failed"
        (ctx.error if failed and not done else ctx.success)(summary)
        ctx.summary(summary)


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
