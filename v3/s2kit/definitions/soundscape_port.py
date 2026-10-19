"""Shared soundscape port schema and metadata."""
from __future__ import annotations

from s2kit.core.registry import tool
from s2kit.core.tool_definition import ToolDefinition
from s2kit.core.params import BoolParam, ChoiceParam, IntParam, PathParam, TextParam, options

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
class SoundscapePorter(ToolDefinition):
    """Shared declaration for SoundscapePorter."""
    EXECUTOR = "s2kit.tools.soundscape_port:SoundscapePorter"
    CUSTOM_VALIDATION = True
    UI_CLASS = "s2kit.ui.soundscape_page:SoundscapePorterPage"
    RUN_LABEL = "Port selected"
    RUN_ICON = "PLAY_ARROW"
    PARAMS = [
        ChoiceParam("action", "Action", default="port", group="Input",
                    choices=options(("port", "Port named soundscapes"), ("list", "List soundscapes"))),
        TextParam("soundscapes", "Soundscape names", lines=4, group="Input",
                  depends_on=("action", "port"), help="One exact name per line; use List to discover names."),
        PathParam("source", "Game content folder", mode="directory",
                  must_exist=False, group="Input",
                  help="A folder holding `scripts/` and `soundevents/`. Leave "
                       "empty to read both straight from the VPK."),
        PathParam("vpk", "Game VPK", mode="open_file", extensions=("vpk",),
                  must_exist=False, group="Input",
                  help="`hlvr/pak01_dir.vpk`. Needed for the audio, and for "
                       "anything the folder above is missing."),

        PathParam("output", "Addon folder", mode="directory", must_exist=False,
                  required=True, group="Output", depends_on=("action", "port"),
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
