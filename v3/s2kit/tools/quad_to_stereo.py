"""
Quad to Stereo.

Source ships some ambience and vehicle sounds as four mono files - `_l`, `_ls`,
`_r`, `_rs` - that the engine mixes into a surround bed. Anything downstream
that expects one stereo file (a soundscape port, a music replacement) needs
them folded down first.

One ffmpeg run per group: four inputs, `amerge`, then a `pan` matrix that also
carries the gain, so no separate volume pass is needed.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Optional

from s2kit.core.jobs import JobContext
from s2kit.core.params import (
    BoolParam, ChoiceParam, FloatParam, IntParam, PathParam, options,
)
from s2kit.core.process import ProcessError, run_streamed
from s2kit.core.registry import tool
from s2kit.media.probe import human_size
from s2kit.pipelines import audio_ops as audio
from s2kit.tools._sounds_shared import sound_filter_params, sound_filter_values
from s2kit.ui.tool_page import ToolPage

#: `<name>_l.wav`, `<name>_rs.ogg`, ... - the Source quad naming convention.
QUAD_SUFFIX = re.compile(r"^(?P<stem>.+?)_(?P<channel>l|ls|r|rs)$",
                         re.IGNORECASE)
QUAD_CHANNELS = ("l", "ls", "r", "rs")


@tool(
    id="quad-to-stereo",
    title="Quad to Stereo",
    category="Sounds",
    icon="SURROUND_SOUND",
    description="Fold Source's four-file quad sounds (_l/_ls/_r/_rs) down to "
                "one stereo file.",
    keywords=("quad", "surround", "stereo", "downmix", "ambient", "4 channel"),
    order=40,
)
class QuadToStereo(ToolPage):
    """Group quad sets by name and mix each to stereo."""

    RUN_LABEL = "Mix down"
    RUN_ICON = "SURROUND_SOUND"

    PARAMS = [
        PathParam("folder", "Sound folder", mode="directory", required=True,
                  group="Input"),
        BoolParam("recursive", "Include subfolders", default=True,
                  group="Input"),
        *sound_filter_params(quad=True),
        PathParam("output", "Output folder", mode="directory", must_exist=False,
                  group="Output",
                  help="Empty writes the stereo file beside the group."),
        ChoiceParam("format", "Output format", default="wav",
                    choices=options(("wav", "WAV"), ("ogg", "OGG Vorbis")),
                    style="segmented", group="Output"),
        BoolParam("overwrite", "Overwrite existing outputs", default=False,
                  group="Output"),
        BoolParam("delete_sources", "Delete the four source files afterwards",
                  default=False, group="Output", advanced=True),

        ChoiceParam("mix", "Mix mode", default="balance", group="Mixing",
                    choices=options(
                        ("balance", "Keep the stage: L+LS left, R+RS right"),
                        ("downmix", "Fold everything into both channels")),
                    help="Balance preserves the left/right image; downmix is "
                         "safer when the rear pair carries most of the sound."),
        FloatParam("gain", "Gain", default=0.5, min=0.05, max=2.0, step=0.05,
                   decimals=2, group="Mixing",
                   help="Applied inside the pan matrix. 0.5 avoids clipping "
                        "when two channels are summed."),
        FloatParam("quality", "Vorbis quality", default=5.0, min=-1.0, max=10.0,
                   step=1.0, decimals=1, group="Mixing",
                   depends_on=("format", "ogg")),
    ]

    PREFERENCES = [
        IntParam("workers", "Groups mixed in parallel", default=4, min=1,
                 max=32, group="Performance"),
        BoolParam("preview_results", "Add results to Preview", default=True,
                  group="Report"),
    ]

    HELP = """
### Quad to Stereo

Finds groups of four files sharing a stem:

```text
ambient_wind_l.wav   ambient_wind_ls.wav
ambient_wind_r.wav   ambient_wind_rs.wav
        -> ambient_wind.wav  (stereo)
```

Both mix modes run as a single `pan` matrix with the gain folded into the
coefficients:

* **Balance** - `c0 = g*L + g*LS`, `c1 = g*R + g*RS`. The front/rear pairs
  collapse but the left/right image survives, which is what you want for
  directional ambience.
* **Downmix** - every input feeds both output channels. Use it when the rear
  pair carries content the front pair does not, and you would rather have
  everything than a correct stage.

Gain defaults to 0.5 because summing two full-scale channels clips otherwise.
Incomplete groups (three of the four files) are reported and skipped.
"""

    def run_job(self, ctx: JobContext) -> None:
        params = ctx.params
        folder = Path(str(params["folder"]))
        if not folder.is_dir():
            raise ValueError(f"Not a folder: {folder}")
        override = str(ctx.prefs.get("ffmpeg_path", "") or "")
        binary = audio.ffmpeg_binary(override)

        groups, partial = self._scan(folder, bool(params["recursive"]),
                                     **sound_filter_values(params))
        for stem, found in partial.items():
            missing = [c for c in QUAD_CHANNELS if c not in found]
            ctx.warn(f"{stem}: incomplete quad set, missing "
                     f"{', '.join('_' + c for c in missing)}")
        if not groups:
            ctx.warn("No complete quad sets found.")
            ctx.summary("No quad sets")
            return
        ctx.info(f"Found {len(groups)} complete quad set(s)")

        output_root = Path(str(params["output"])) if params["output"] else None
        target_format = str(params["format"])
        filter_complex = ("[0:a][1:a][2:a][3:a]amerge=inputs=4,"
                          + self._pan(str(params["mix"]), float(params["gain"]))
                          + "[out]")

        def mix(entry) -> dict:
            stem, files = entry
            first = files["l"]
            destination = _destination(first, folder, output_root, stem,
                                       target_format)
            before = sum(_size(path) for path in files.values())
            if destination.exists() and not bool(params["overwrite"]):
                return {"stem": stem, "status": "skipped",
                        "reason": "output exists", "before": before,
                        "after": _size(destination), "output": destination}

            destination.parent.mkdir(parents=True, exist_ok=True)
            args = [str(binary), "-hide_banner", "-loglevel", "error",
                    "-nostdin", "-y"]
            for channel in QUAD_CHANNELS:
                args += ["-i", str(files[channel])]
            args += ["-filter_complex", filter_complex, "-map", "[out]"]
            if target_format == "ogg":
                args += ["-c:a", "libvorbis", "-q:a",
                         f"{float(params['quality']):g}"]
            else:
                args += ["-c:a", "pcm_s16le"]
            args.append(str(destination))

            lines: list[str] = []
            try:
                code = run_streamed(args, lines.append, cancel=ctx.cancel_event,
                                    echo_command=False)
            except ProcessError as exc:
                raise audio.AudioError(str(exc)) from exc
            if code != 0:
                raise audio.AudioError("\n".join(lines).strip()
                                       or f"ffmpeg exited with {code}")

            if bool(params["delete_sources"]):
                for path in files.values():
                    path.unlink(missing_ok=True)
            return {"stem": stem, "status": "mixed", "reason": "",
                    "before": before, "after": _size(destination),
                    "output": destination}

        outcome = ctx.map(sorted(groups.items()), mix,
                          workers=int(ctx.prefs.get("workers", 4)),
                          label=lambda entry: entry[0], unit="quad set",
                          log_each=False)
        rows = [row for row in outcome.results if row is not None]
        mixed = [row for row in rows if row["status"] == "mixed"]

        before = sum(row["before"] for row in rows)
        after = sum(row["after"] for row in rows)
        ctx.metric("Quad sets", len(rows))
        ctx.metric("Mixed", len(mixed), tone="good" if mixed else "neutral")
        ctx.metric("Before", human_size(before))
        ctx.metric("After", human_size(after))
        if partial:
            ctx.metric("Incomplete", len(partial), tone="warn")
        if outcome.failed:
            ctx.metric("Failed", outcome.failed, tone="bad")

        ctx.table("Quad sets", ["Set", "Result", "4 files", "Stereo", "Output"],
                  [[row["stem"], row["status"], human_size(row["before"]),
                    human_size(row["after"]) if row["after"] else "",
                    str(row["output"] or row["reason"])] for row in rows],
                  path_column=4)
        if partial:
            ctx.table("Incomplete sets", ["Set", "Found", "Missing"],
                      [[stem, ", ".join(sorted(found)),
                        ", ".join(c for c in QUAD_CHANNELS if c not in found)]
                       for stem, found in sorted(partial.items())])

        if bool(ctx.prefs.get("preview_results", True)):
            for row in mixed[:6]:
                ctx.artifact(row["output"], label=row["output"].name)

        summary = (f"mixed {len(mixed)}/{len(rows)} quad set(s), "
                   f"{human_size(before)} -> {human_size(after)}")
        ctx.success(summary)
        ctx.summary(summary)

    # ------------------------------------------------------------------

    @staticmethod
    def _scan(folder: Path, recursive: bool, *, include: str = "",
              exclude: str = "", input_extensions: str = ""):
        """Group quad channels by stem and filter whole sets by their relative names."""
        found: dict[tuple[Path, str], dict[str, Path]] = defaultdict(dict)
        for path in audio.find_sounds(folder, recursive=recursive,
                                      input_extensions=input_extensions):
            match = QUAD_SUFFIX.match(path.stem)
            if match:
                key = (path.parent, match.group("stem"))
                found[key][match.group("channel").lower()] = path

        complete: dict[str, dict[str, Path]] = {}
        partial: dict[str, dict[str, Path]] = {}
        for (parent, stem), files in found.items():
            label = str((parent / stem).relative_to(folder)) \
                if parent != folder else stem
            if not audio.matches_sound(label, include=include, exclude=exclude):
                continue
            (complete if len(files) == 4 else partial)[label] = files
        return complete, partial

    @staticmethod
    def _pan(mode: str, gain: float) -> str:
        """The `pan` matrix. Channels arrive in L, LS, R, RS order."""
        if mode == "balance":
            return (f"pan=stereo|c0={gain}*c0+{gain}*c1|"
                    f"c1={gain}*c2+{gain}*c3")
        every = "+".join(f"{gain}*c{index}" for index in range(4))
        return f"pan=stereo|c0={every}|c1={every}"


def _destination(sample: Path, root: Path, output_root: Optional[Path],
                 stem: str, target_format: str) -> Path:
    name = Path(stem).name
    if output_root is None:
        return sample.parent / f"{name}.{target_format}"
    try:
        relative = sample.parent.relative_to(root)
    except ValueError:
        relative = Path()
    return output_root / relative / f"{name}.{target_format}"


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
