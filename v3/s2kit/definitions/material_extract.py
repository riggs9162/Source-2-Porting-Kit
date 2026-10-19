"""Shared material extract schema and metadata."""
from __future__ import annotations

from s2kit.core.registry import tool
from s2kit.core.tool_definition import ToolDefinition
from s2kit.core.params import BoolParam, ChoiceParam, FloatParam, IntParam, PathParam, options

from s2kit.core.asset_defaults import METAL_DIFFUSE_SUPPRESSION

from s2kit.definitions._materials_shared import CLAMP_CHOICES


@tool(
    id="material-extract",
    title="Material Extractor",
    category="Materials / PBR",
    icon="UNDO",
    description="Decode built Source 1 materials back into PBR maps as PNG.",
    keywords=("reverse", "extract", "decode", "vtf", "vmt", "pbr", "roughness",
              "metallic", "unbake"),
    order=40,
)
class MaterialExtractor(ToolDefinition):
    """Shared declaration for MaterialExtractor."""
    EXECUTOR = "s2kit.tools.material_extract:MaterialExtractor"
    CUSTOM_VALIDATION = False
    RUN_LABEL = "Extract"
    RUN_ICON = "UNDO"
    PARAMS = [
        PathParam("input_root", "Materials folder", mode="directory",
                  required=True, group="Input"),
        BoolParam("recursive", "Include subfolders", default=True, group="Input"),
        ChoiceParam("scan_mode", "Find materials by", default="vmt",
                    group="Input", style="segmented",
                    choices=options(("vmt", "VMT files"),
                                    ("vtf", "VTF filenames")),
                    help="VMT is the reliable one - it names its own textures, "
                         "so the output is clean even when the VTFs are not."),
        PathParam("output_root", "Output folder", mode="directory",
                  required=True, must_exist=False, group="Output"),
        BoolParam("preserve_structure", "Mirror the input folder tree",
                  default=True, group="Output"),
        BoolParam("overwrite", "Overwrite existing PNGs", default=False,
                  group="Output"),
        ChoiceParam("clamp_size", "Clamp resolution", default=0, group="Output",
                    choices=CLAMP_CHOICES),

        FloatParam("phong_strength", "Phong strength used", default=0.5,
                   min=0.01, max=1.0, step=0.05, decimals=2, group="Bake settings",
                   help="Must match what built the material, or roughness comes "
                        "back on the wrong curve."),
        FloatParam("gloss_gamma", "Gloss gamma used", default=2.0, min=0.5,
                   max=4.0, step=0.1, decimals=2, group="Bake settings"),
        FloatParam("metal_diffuse_suppression", "Metal suppression used",
                   default=METAL_DIFFUSE_SUPPRESSION, min=0.0, max=1.0, step=0.01, decimals=2,
                   group="Bake settings"),
        BoolParam("recover_albedo", "Undo the metal darkening", default=True,
                  group="Bake settings",
                  help="Needs the Phong map, which carries the metallic channel."),
        BoolParam("invert_green", "Flip the normal's green channel back",
                  default=True, group="Bake settings",
                  help="On by default so the maps come back the way Source 2 "
                       "authored them, and feed straight into a converter "
                       "again. Turn it off to keep Source 1's convention."),
        BoolParam("write_opacity", "Write an opacity map when alpha is used",
                  default=True, group="Bake settings"),
    ]
    PREFERENCES = [
        IntParam("workers", "Materials extracted in parallel", default=4, min=1,
                 max=32, group="Performance"),
        BoolParam("preview_results", "Add extracted maps to Preview",
                  default=True, group="Report"),
        IntParam("preview_limit", "Preview at most", default=8, min=0, max=100,
                 suffix="materials", group="Report"),
    ]
    HELP = """
### Material Extractor

Decodes `_color.vtf`, `_normal.vtf` and `_phong.vtf` back into
`_color.png`, `_normal.png`, `_roughness.png` and `_metallic.png`.

#### The two scan modes

**By VMT** reads each `.vmt`, follows its `$basetexture`, `$bumpmap` and
`$phongexponenttexture` to real files, and names the output after the VMT. This
works even when the VTFs are named inconsistently, and it is what you want for
someone else's addon.

**By VTF filenames** matches the `_color` / `_normal` / `_phong` suffix
convention directly. Faster, and correct for output this kit produced.

#### What inverts and what does not

`_phong.vtf` stores `R = (1 - roughness) ^ gamma * strength` and `G = metallic`.
Given the same two numbers the bake used, roughness inverts exactly - which is
why the **Bake settings** group exists. Leave them at the defaults for materials
this kit built; change them if you know the material came from something else.

The basetexture is the lossy direction. AO was multiplied into the albedo before
encoding, and nothing recorded it separately, so the AO cannot be pulled back
out. Undoing the metal darkening is possible because the metallic channel
survived in the Phong map, and that is what **Undo the metal darkening** does.

The bumpmap's alpha is skipped entirely. It holds the Phong mask, which is a
product of roughness, metallic and AO all at once - already recovered more
cleanly from the Phong map.

#### The green channel comes back flipped

The converters invert the normal's green on the way in, because Source 2 authors
it green-up and Source 1 wants green-down. So the extractor inverts it back, and
what lands on disk is the map the way Source 2 had it - which is what you want if
it is going into a PBR tool or straight back through a converter. Turn **Flip the
normal's green channel back** off to keep Source 1's convention instead.
"""
