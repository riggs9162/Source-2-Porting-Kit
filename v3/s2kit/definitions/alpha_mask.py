"""Shared alpha mask schema and metadata."""
from __future__ import annotations

from s2kit.core.registry import tool
from s2kit.core.tool_definition import ToolDefinition
from s2kit.core.params import BoolParam, ChoiceParam, FloatParam, PathParam, options

from s2kit.core.asset_defaults import IMAGE_EXTENSIONS

IMAGES = tuple(ext.lstrip(".") for ext in IMAGE_EXTENSIONS)


@tool(
    id="alpha-mask",
    title="Alpha Mask",
    category="Materials",
    icon="OPACITY",
    description="Bake a black/white mask into a texture's alpha channel.",
    keywords=("alpha", "mask", "opacity", "transparency", "cutout", "bake"),
    order=30,
)
class AlphaMask(ToolDefinition):
    """Shared declaration for AlphaMask."""
    EXECUTOR = "s2kit.tools.alpha_mask:AlphaMask"
    CUSTOM_VALIDATION = False
    RUN_LABEL = "Apply"
    RUN_ICON = "OPACITY"
    PARAMS = [
        PathParam("color", "Texture", mode="open_file", required=True,
                  group="Input", extensions=IMAGES),
        PathParam("mask", "Mask", mode="open_file", required=True, group="Input",
                  extensions=IMAGES,
                  help="White keeps the pixel, black makes it transparent."),
        ChoiceParam("mode", "Mask mode", default="threshold", group="Settings",
                    style="segmented",
                    choices=options(("threshold", "Hard cutout"),
                                    ("smooth", "Soft gradient"))),
        FloatParam("threshold", "Cutout threshold", default=0.5, min=0.0,
                   max=1.0, step=0.05, decimals=2, group="Settings",
                   depends_on=("mode", "threshold"),
                   help="Mask luminance at or above this keeps the pixel."),
        BoolParam("invert", "Invert the mask", default=False, group="Settings",
                  help="For a mask authored the other way round - black keeps."),
        BoolParam("keep_existing_alpha", "Multiply with the existing alpha",
                  default=True, group="Settings",
                  help="Off replaces the alpha channel outright."),

        PathParam("output", "Output file", mode="save_file", must_exist=False,
                  group="Output", extensions=("png",),
                  help="Empty writes <texture>_masked.png beside the source."),
    ]
    PREFERENCES = [
        BoolParam("preview_result", "Open the result in Preview", default=True,
                  group="Report"),
    ]
    HELP = """
### Alpha Mask

Reads the mask's luminance and writes it into the texture's alpha channel.

#### The two modes

**Hard cutout** thresholds the mask: anything at or above the threshold is fully
kept, anything below is fully transparent. This is what `$alphatest` materials
want - the engine does its own binary cut anyway, and a soft edge just wastes
bits.

**Soft gradient** uses the mask's luminance directly, so a grey pixel becomes a
half-transparent one. This is for `$translucent` materials, where the blend is
the point.

#### Preserving what is already there

**Multiply with the existing alpha** is on by default: a texture that already had
transparency keeps it, and the mask can only make pixels *more* transparent,
never less. Turn it off when the mask is meant to be the complete answer - for
instance after extracting an opacity map that already accounts for everything.

#### Output

Always PNG, because the point is to feed the result into a material tool that
will encode the VTF itself. Alpha survives the round trip intact.
"""
