"""Shared vtf resize schema and metadata."""
from __future__ import annotations

from s2kit.core.registry import tool
from s2kit.core.tool_definition import ToolDefinition
from s2kit.core.params import BoolParam, ChoiceParam, IntParam, PathParam, options

SIZES = options(*[(size, f"{size} x {size}")
                  for size in (32, 64, 128, 256, 512, 1024, 2048, 4096)])


@tool(
    id="vtf-resize",
    title="VTF Resizer",
    category="Materials",
    icon="PHOTO_SIZE_SELECT_LARGE",
    description="Shrink oversized VTF textures to a power-of-two budget.",
    keywords=("vtf", "clamp", "resize", "downscale", "budget", "memory",
              "optimise", "power of two"),
    order=20,
)
class VtfResizer(ToolDefinition):
    """Shared declaration for VtfResizer."""
    EXECUTOR = "s2kit.tools.vtf_resize:VtfResizer"
    CUSTOM_VALIDATION = False
    RUN_LABEL = "Resize"
    RUN_ICON = "PHOTO_SIZE_SELECT_LARGE"
    PARAMS = [
        ChoiceParam("source", "Source", default="folder", group="Input",
                    style="segmented",
                    choices=options(("folder", "Folder"), ("files", "Files"))),
        PathParam("folder", "VTF folder", mode="directory", group="Input",
                  depends_on=("source", "folder")),
        BoolParam("recursive", "Include subfolders", default=True, group="Input",
                  depends_on=("source", "folder")),
        PathParam("files", "VTF files", mode="open_files", group="Input",
                  extensions=("vtf",), depends_on=("source", "files")),

        ChoiceParam("max_size", "Maximum size", default=1024, group="Settings",
                    choices=SIZES,
                    help="Textures at or below this are left alone."),
        BoolParam("preserve_aspect", "Preserve the aspect ratio", default=True,
                  group="Settings",
                  help="On, 2048x1024 becomes 1024x512. Off squares both edges "
                       "down to the cap independently."),
        BoolParam("backup", "Keep a .bak copy", default=True, group="Settings",
                  help="Resizing rewrites the file in place - this is the only "
                       "way back."),
        BoolParam("dry_run", "Dry run (report, change nothing)", default=False,
                  group="Settings"),
    ]
    PREFERENCES = [
        IntParam("workers", "Files resized in parallel", default=4, min=1,
                 max=32, group="Performance"),
        BoolParam("preview_results", "Add resized textures to Preview",
                  default=True, group="Report"),
        IntParam("preview_limit", "Preview at most", default=8, min=0, max=100,
                 suffix="files", group="Report"),
    ]
    HELP = """
### VTF Resizer

Caps every VTF in a folder at a power-of-two size.

```text
4096x4096 -> 1024x1024
2048x1024 -> 1024x512     (aspect preserved)
512x512   -> unchanged
```

#### Why power of two

Source 1 expects it. A non-power-of-two texture either gets silently resampled by
the engine at load time or fails to mip properly, so the target is always the
largest power of two that fits under the cap.

#### In place, with a backup

The file is rewritten where it sits, because that is what makes this useful -
you point it at a finished `materials/` tree and the references keep working.
The `.bak` copy is written once per file and never overwritten, so running the
tool twice does not destroy the original. Turn the backup off only when the
folder is under version control.

The original image format and flags are preserved, so a DXT5 normal map comes
back as a DXT5 normal map. Mipmaps are regenerated for the new size.

#### Dry run first

On a large tree, run with **Dry run** on. The Output tab lists every file, its
current size and what it would become, without touching anything.
"""
