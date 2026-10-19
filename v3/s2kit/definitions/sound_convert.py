"""Shared sound convert schema and metadata."""
from __future__ import annotations

from s2kit.core.registry import tool
from s2kit.core.tool_definition import ToolDefinition
from s2kit.core.params import BoolParam, ChoiceParam, FloatParam, IntParam, PathParam, options

from s2kit.tools._sounds_shared import sound_filter_params

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
class SoundConverter(ToolDefinition):
    """Shared declaration for SoundConverter."""
    EXECUTOR = "s2kit.tools.sound_convert:SoundConverter"
    CUSTOM_VALIDATION = False
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
class LoopPointConverter(ToolDefinition):
    """Shared declaration for LoopPointConverter."""
    EXECUTOR = "s2kit.tools.sound_convert:LoopPointConverter"
    CUSTOM_VALIDATION = False
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
