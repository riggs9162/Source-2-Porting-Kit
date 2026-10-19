"""Shared texture set convert schema and metadata."""
from __future__ import annotations

from s2kit.core.registry import tool
from s2kit.core.tool_definition import ToolDefinition
from s2kit.core.params import BoolParam, ChoiceParam, PathParam, TextParam, options

from s2kit.core.asset_defaults import MISSING_METAL_MODES

from s2kit.definitions import _materials_shared as shared


@tool(
    id="texture-set-convert",
    title="Texture Set Converter",
    category="Materials / PBR",
    icon="GRID_VIEW",
    description="Group a folder of PBR textures into material sets and convert "
                "each one to VMT + VTF.",
    keywords=("pbr", "texture", "folder", "batch", "orm", "albedo", "roughness",
              "metallic", "material", "convert"),
    order=20,
)
class TextureSetConverter(ToolDefinition):
    """Shared declaration for TextureSetConverter."""
    EXECUTOR = "s2kit.tools.texture_set_convert:TextureSetConverter"
    CUSTOM_VALIDATION = False
    RUN_LABEL = "Convert"
    RUN_ICON = "GRID_VIEW"
    PARAMS = [
        PathParam("input_root", "Texture folder", mode="directory",
                  required=True, group="Input"),
        BoolParam("recursive", "Include subfolders", default=True, group="Input"),
        TextParam("name_filter", "Only names containing", group="Input",
                  help="Case-insensitive substring filter. Empty converts all."),
        ChoiceParam("requires", "Only convert sets with", default="color_normal",
                    group="Input",
                    choices=options(("color", "A colour map only"),
                                    ("color_normal", "Colour and normal"),
                                    ("full", "Colour, normal and a specular map"))),

        PathParam("output_root", "Output folder", mode="directory",
                  required=True, must_exist=False, group="Destination"),
        BoolParam("preserve_structure", "Mirror the input folder tree",
                  default=True, group="Destination"),
        BoolParam("append_subfolders", "Append subfolders to the material path",
                  default=False, group="Destination"),

        *shared.naming_params(),
        *shared.conversion_params(),

        ChoiceParam("missing_metal", "When there is no metallic map",
                    default="off", group="Source intent",
                    choices=options(*MISSING_METAL_MODES),
                    depends_on=("mode", "fake"),
                    help="Reusing an albedo channel works on painted-metal props "
                         "where the artist already separated metal by hue."),
        ChoiceParam("transparency", "Transparency", default="opaque",
                    group="Source intent",
                    choices=options(("opaque", "Opaque"),
                                    ("translucent", "$translucent"),
                                    ("alphatest", "$alphatest")),
                    help="Applies to every set in the run - filenames carry no "
                         "transparency intent to read."),
        BoolParam("port_selfillum", "Use emissive maps as glow masks",
                  default=True, group="Source intent"),

        *shared.fake_pbr_params(),
        *shared.exo_pbr_params(),
        *shared.output_params(),
        BoolParam("dry_run", "Scan only (report, write nothing)", default=False,
                  group="Output"),
    ]
    PREFERENCES = shared.preferences()
    HELP = """
### Texture Set Converter

Groups textures by asset name and resolves each file to a role.

```text
crate_01_BaseColor.png  crate_01_Normal.png
crate_01_Roughness.png  crate_01_Metallic.png  crate_01_AO.png
        -> crate_01.vmt + four VTFs
```

#### How the grouping works

1. Split the filename on `_`, `-`, `.` and space.
2. Strip role words (`basecolor`, `normal`, `orm`, ...) and noise (`2k`, `png`,
   `tile`) to get the asset key. Whatever is left is the material's name.
3. Claim a packed **ORM** first, before any other role is matched - otherwise
   `foo_orm` scores on the `r` and `m` aliases and gets taken for roughness.
4. Score the rest per role. Exact role words beat weak ones (`base` appears in
   `base_color` but also in `concrete_base`), and files sitting beside the ORM
   are preferred.

#### Ambiguity is reported, not resolved

Two files claiming one role as strongly as each other - `foo_normal.png` next to
`foo_nrm.png` - is a real question about which map the artist meant. The set
records a warning and leaves the role empty rather than picking by filename
length and baking a coin flip into the material.

#### Packed ORM

A `_ORM` map is split into its three channels in memory, so there are no
temporary files. An explicit single-channel map beside an ORM wins the role -
authoring one deliberately is the stronger signal.

#### The normal's green channel is flipped

Source 2 authors tangent normals green-up and Source 1 wants them green-down, so
the green channel is inverted by default. This is the one place that assumption
is worth checking: a set scraped from somewhere other than Source 2 may already
be in Source 1's convention. Name the file `_normal_dx` and it is left alone, or
set **Normal green channel** to *Never flip* for the whole run.

#### Baking normal detail into the base texture

For static props, turn **Ship a normal map** off and enable **Bake normal detail
into base texture**. The source normal then adds fixed diffuse relief to the
colour texture without shipping a bumpmap or Phong block. **Baked detail
strength** defaults to a subtle 0.25; 0 makes no change. The bake honours
**Normal green channel**, preserves alpha, and fades out under emissive pixels.
Missing or flat normals are skipped, and **Write VTF textures** must be on.

The shading cannot follow moving lights and may reveal rotated UV islands.
The bake is off by default and only applies to Fake PBR with normal output off.

#### What it cannot know

Filenames say nothing about translucency, glow or entity tint, so those are set
once for the whole run. If the material came from Source 2 and you still have
the `.vmat`, use the **VMAT Converter** instead - it reads all of that from the
source.
"""
