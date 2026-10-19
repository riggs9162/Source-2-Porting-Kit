"""Shared quad to stereo schema and metadata."""
from __future__ import annotations

from s2kit.core.registry import tool
from s2kit.core.tool_definition import ToolDefinition
from s2kit.core.params import BoolParam, ChoiceParam, FloatParam, IntParam, PathParam, options

from s2kit.tools._sounds_shared import sound_filter_params


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
class QuadToStereo(ToolDefinition):
    """Shared declaration for QuadToStereo."""
    EXECUTOR = "s2kit.tools.quad_to_stereo:QuadToStereo"
    CUSTOM_VALIDATION = False
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
