"""Shared sound optimizer schema and metadata."""
from __future__ import annotations

from s2kit.core.registry import tool
from s2kit.core.tool_definition import ToolDefinition
from s2kit.core.params import BoolParam, ChoiceParam, FloatParam, IntParam, PathParam, options

from s2kit.tools._sounds_shared import sound_filter_params

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
class SoundOptimizer(ToolDefinition):
    """Shared declaration for SoundOptimizer."""
    EXECUTOR = "s2kit.tools.sound_optimizer:SoundOptimizer"
    CUSTOM_VALIDATION = False
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
