"""Shared texture convert schema and metadata."""
from __future__ import annotations

from s2kit.core.registry import tool
from s2kit.core.tool_definition import ToolDefinition
from s2kit.core.params import BoolParam, ChoiceParam, IntParam, PathParam, options

from s2kit.media import probe

SUPPORTED = tuple(sorted(ext.lstrip(".") for ext in probe.IMAGE_EXTENSIONS))


@tool(
    id="texture-convert",
    title="Texture Converter",
    category="Materials",
    icon="IMAGE",
    description="Convert textures (including VTF) to PNG or TGA, with clamping "
                "and channel export.",
    keywords=("vtf", "png", "tga", "resize", "clamp", "channel", "alpha"),
)
class TextureConverter(ToolDefinition):
    """Shared declaration for TextureConverter."""
    EXECUTOR = "s2kit.tools.texture_convert:TextureConverter"
    CUSTOM_VALIDATION = False
    RUN_LABEL = "Convert"
    RUN_ICON = "TRANSFORM"
    PARAMS = [
        ChoiceParam("source", "Source", default="folder", group="Input",
                    style="segmented",
                    choices=options(("folder", "Folder"), ("files", "Files"))),
        PathParam("input_folder", "Input folder", mode="directory",
                  group="Input", depends_on=("source", "folder"),
                  help="Every supported texture in the folder is converted."),
        BoolParam("recursive", "Include subfolders", default=False,
                  group="Input", depends_on=("source", "folder")),
        PathParam("input_files", "Input files", mode="open_files",
                  group="Input", depends_on=("source", "files"),
                  extensions=SUPPORTED),
        PathParam("output_folder", "Output folder", mode="directory",
                  required=True, must_exist=False, group="Output"),
        ChoiceParam("format", "Output format", default="png", group="Output",
                    choices=options(("png", "PNG"), ("tga", "TGA"),
                                    ("bmp", "BMP"))),
        IntParam("clamp", "Clamp longest edge", default=0, min=0, max=8192,
                 step=256, suffix="px", group="Output",
                 help="0 keeps the source resolution."),
        ChoiceParam("channel", "Channels", default="rgba", group="Output",
                    choices=options(("rgba", "RGBA (as-is)"), ("rgb", "RGB only"),
                                    ("a", "Alpha as greyscale"),
                                    ("luminance", "Luminance"))),
        BoolParam("split_channels", "Also write each channel separately",
                  default=False, group="Output", advanced=True),
        BoolParam("overwrite", "Overwrite existing files", default=False,
                  group="Output", advanced=True),
        BoolParam("dry_run", "Dry run (report only, write nothing)",
                  default=False, group="Output"),
    ]
    PREFERENCES = [
        BoolParam("flatten_output", "Flatten the folder structure", default=True,
                  group="Naming",
                  help="Off mirrors the input folder tree under the output."),
        ChoiceParam("naming", "Name collisions", default="suffix",
                    group="Naming",
                    choices=options(("suffix", "Add _1, _2, ..."),
                                    ("skip", "Skip the file"),
                                    ("overwrite", "Overwrite"))),
        BoolParam("preview_results", "Add converted files to Preview",
                  default=True, group="Preview"),
        IntParam("preview_limit", "Preview at most", default=12, min=0, max=200,
                 suffix="files", group="Preview"),
    ]
    HELP = """
### Texture Converter

Reads anything Pillow understands, plus **VTF** through the optional `sourcepp`
package (mip 0, frame 0). Writes PNG, TGA or BMP.

* **Clamp longest edge** downsizes with nearest-neighbour, which keeps texels
  crisp - the right choice when you are inspecting a normal or mask map rather
  than producing final art.
* **Channels** isolates a single channel as greyscale. Handy for pulling an
  alpha mask out of a Source 1 basetexture.
* **Split channels** additionally writes `_r`, `_g`, `_b`, `_a` files.
* **Dry run** reports exactly what would be written without touching disk.

Converted files land on the Preview tab, so you can flip through the results in
the texture viewer without leaving the app.
"""
