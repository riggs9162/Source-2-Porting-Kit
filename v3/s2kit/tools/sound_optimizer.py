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

from s2kit.definitions.sound_optimizer import QUALITY_CHOICES, RATE_CHOICES

from pathlib import Path
from typing import Optional

from s2kit.core.jobs import JobCancelled, JobContext
from s2kit.core.params import options
from s2kit.media.probe import human_size
from s2kit.pipelines import audio_ops as audio
from s2kit.tools._sounds_shared import sound_filter_params, sound_filter_values
from s2kit.definitions.sound_optimizer import SoundOptimizer as _SoundOptimizerDefinition

#: Nominal stereo bitrates from the Xiph quality table.


class SoundOptimizer(_SoundOptimizerDefinition):
    """Batch sound optimisation with a dry run that reports the savings."""


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
