"""
Alpha Mask.

Applies a black-and-white mask to a texture's alpha channel: white keeps the
pixel, black makes it transparent. Source 2 often ships opacity as its own map
while Source 1 wants it packed into the basetexture's alpha, and this is the
one-step way to do that merge before feeding the result to a material tool.

Existing transparency is preserved rather than replaced - a pixel that was
already half-transparent stays half-transparent where the mask says keep, so a
mask can be applied on top of authored alpha without flattening it.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from s2kit.core.jobs import JobContext
from s2kit.core.params import (
    BoolParam, ChoiceParam, FloatParam, PathParam, options,
)
from s2kit.core.registry import tool
from s2kit.pipelines import textures
from s2kit.ui.tool_page import ToolPage

IMAGES = tuple(ext.lstrip(".") for ext in textures.IMAGE_EXTENSIONS)


@tool(
    id="alpha-mask",
    title="Alpha Mask",
    category="Materials",
    icon="OPACITY",
    description="Bake a black/white mask into a texture's alpha channel.",
    keywords=("alpha", "mask", "opacity", "transparency", "cutout", "bake"),
    order=30,
)
class AlphaMask(ToolPage):
    """Merge a mask into a texture's alpha."""

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

    def run_job(self, ctx: JobContext) -> None:
        params = ctx.params
        color_path = Path(str(params["color"]))
        mask_path = Path(str(params["mask"]))

        color = textures.load_rgba(color_path)
        if color is None:
            raise ValueError(f"Could not read {color_path}")
        height, width = color.shape[:2]
        ctx.info(f"Texture {width}x{height}")

        mask = textures.load_rgba(mask_path)
        if mask is None:
            raise ValueError(f"Could not read {mask_path}")
        if mask.shape[:2] != (height, width):
            ctx.info(f"Resizing the mask from {mask.shape[1]}x{mask.shape[0]}")
            mask = textures.fit(mask, height, width)

        luminance = textures.luminance(mask)
        if bool(params["invert"]):
            luminance = 1.0 - luminance

        if str(params["mode"]) == "threshold":
            threshold = float(params["threshold"])
            keep = (luminance >= threshold).astype(np.float32)
            opaque = int(np.count_nonzero(keep))
            ctx.info(f"Cutout at {threshold:.2f}: {opaque:,} of "
                     f"{keep.size:,} pixels kept "
                     f"({100.0 * opaque / keep.size:.1f}%)")
        else:
            keep = luminance
            ctx.info(f"Soft mask, mean opacity {float(keep.mean()):.3f}")

        alpha = keep
        if bool(params["keep_existing_alpha"]):
            existing = np.clip(color[:, :, 3], 0.0, 1.0)
            alpha = existing * keep
            if float(existing.min()) < 1.0:
                ctx.info("Multiplied with the texture's existing alpha")

        target = self._target(params, color_path)
        textures.save_png(
            textures.to_uint8(np.dstack([color[:, :, :3], alpha])), target)

        transparent = int(np.count_nonzero(alpha <= 0.0))
        ctx.metric("Size", f"{width}x{height}")
        ctx.metric("Transparent", f"{100.0 * transparent / alpha.size:.1f}%",
                   tone="good" if transparent else "warn")
        ctx.metric("Mean alpha", f"{float(alpha.mean()):.3f}")
        ctx.table("Output", ["File", "Size", "Path"],
                  [[target.name, f"{width}x{height}", str(target)]],
                  path_column=2)

        if bool(ctx.prefs.get("preview_result", True)):
            ctx.artifact(target, label=target.name, kind="image")
        ctx.success(f"wrote {target.name}")
        ctx.summary(f"{target.name} ({100.0 * transparent / alpha.size:.1f}% "
                    f"transparent)")

    @staticmethod
    def _target(params: dict, color_path: Path) -> Path:
        raw = str(params["output"] or "").strip()
        if not raw:
            return color_path.with_name(f"{color_path.stem}_masked.png")
        target = Path(raw.split(os.pathsep)[0])
        return target if target.suffix else target.with_suffix(".png")
