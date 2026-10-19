"""Shared vrf export schema and metadata."""
from __future__ import annotations

from s2kit.core.registry import tool
from s2kit.core.tool_definition import ToolDefinition
from s2kit.core.params import BoolParam, ChoiceParam, IntParam, PathParam, TextParam, options

_MODELS_BATCH_HINT = (
    "Optional - one model per line:\n"
    "vpk:S:/.../hlvr/pak01_dir.vpk:models/props/foo.vmdl_c   (VRF right-click "
    "> Copy URL)\n"
    "models/props_combine/combine_monitor_large.vmdl_c   (path inside the VPK "
    "above)\n"
    "C:/game/models/prop.vmdl_c   (a loose file or folder on disk)"
)


@tool(
    id="vrf-export",
    title="VRF Batch Export",
    category="Models",
    icon="UNARCHIVE",
    description="Extract Source 2 models or materials from a VPK into a "
                "Source 1 modelsrc/materialsrc project.",
    keywords=("vrf", "source2viewer", "vpk", "extract", "gltf", "glb", "vmat",
              "decompile", "export"),
    order=10,
)
class VrfBatchExport(ToolDefinition):
    """Shared declaration for VrfBatchExport."""
    EXECUTOR = "s2kit.tools.vrf_export:VrfBatchExport"
    CUSTOM_VALIDATION = True
    RUN_LABEL = "Export"
    RUN_ICON = "UNARCHIVE"
    PARAMS = [
        ChoiceParam("mode", "What to export", default="models", group="Mode",
                    style="segmented",
                    choices=options(("models", "Models"), ("vmats", "Materials")),
                    help="Models pull their materials automatically. Materials "
                         "mode extracts VMATs and their textures only, no glTF."),

        ChoiceParam("source", "Input type", default="vpk", group="Input",
                    style="segmented",
                    choices=options(("vpk", "VPK archive"), ("file", "Loose file"),
                                    ("folder", "Folder"))),
        PathParam("vpk", "VPK archive", mode="open_file", extensions=("vpk",),
                  group="Input", depends_on=("source", "vpk"),
                  help="Usually pak01_dir.vpk. Name what to pull from it below."),
        PathParam("input_file", "Input file", mode="open_file", group="Input",
                  depends_on=("source", "file"),
                  extensions=("vmdl_c", "vmat_c", "vmesh_c")),
        PathParam("input_folder", "Input folder", mode="directory", group="Input",
                  depends_on=("source", "folder")),
        BoolParam("recursive", "Recurse into subfolders", default=True,
                  group="Input", depends_on=("source", "folder")),
        TextParam("batch", "Paths to extract", default="", group="Input",
                  lines=6, max_lines=14, hint=_MODELS_BATCH_HINT,
                  help="One per line: a VRF Copy-URL, a path inside the chosen "
                       "VPK, or a file/folder on disk. Blank lines and # "
                       "comments are ignored. Leave empty to export the whole "
                       "input."),

        PathParam("output", "Project folder", mode="directory", required=True,
                  must_exist=False, group="Output",
                  help="The addon root. modelsrc/ and materialsrc/ are written "
                       "inside it; your existing models/ and materials/ trees "
                       "are never touched."),
        ChoiceParam("format", "glTF format", default="glb", group="Output",
                    style="segmented", depends_on=("mode", "models"),
                    choices=options(("glb", "GLB (single file)"),
                                    ("gltf", "glTF (+ .bin)"))),
        IntParam("threads", "VRF threads", default=4, min=1, max=32,
                 group="Output", suffix="threads"),
        BoolParam("flatten_materials", "Flatten materials into one folder",
                  default=False, group="Output",
                  help="Collapses every texture and .vmat into materialsrc/ "
                       "root instead of mirroring the model tree."),
        BoolParam("keep_stray_images", "Keep stray images next to the glTF",
                  default=False, group="Output", advanced=True,
                  help="VRF drops decoded images beside each .gltf; they are "
                       "deleted by default. Ignored when flattening."),
    ]
    PREFERENCES = [
        BoolParam("preview_exports", "Add exported files to Preview",
                  default=True, group="Preview"),
        IntParam("preview_limit", "Preview at most", default=8, min=0, max=100,
                 suffix="files", group="Preview"),
        BoolParam("reveal_when_done", "Open the project folder when finished",
                  default=False, group="After the run"),
        BoolParam("list_outputs", "List every written file in a table",
                  default=True, group="After the run",
                  help="Off keeps the Output tab light on very large exports."),
        IntParam("output_table_limit", "Rows in that table", default=400,
                 min=10, max=5000, group="After the run"),
    ]
    HELP = """
### VRF Batch Export

Wraps **Source2Viewer-CLI** (a separate download from the GUI Source 2 Viewer -
the CLI build is the one with `-CLI` in the name). Point the tool at it in
*Preferences > External programs*, or put it on PATH.

#### What goes in the batch box

One entry per line, mixed freely:

* `vpk:S:/.../pak01_dir.vpk:models/props/foo.vmdl_c` - VRF's right-click
  **Copy URL**. Entries naming the same archive are merged into one pass.
* `models/props/foo.vmdl_c` - a path inside the VPK you picked above.
* `C:/game/models/prop.vmdl_c` - a loose file, or a folder, on disk.

Leave it empty to export everything the input contains.

#### What it does beyond calling VRF

* **Resolves references** with `-b RERL` first, so a model's materials and
  textures come along even when they live outside the folder you asked for.
* **Splits oversized command lines** - a big reference list would otherwise
  blow past the Windows 32k argument limit.
* **Writes sidecars**: `_skins.json` (Source 2 material groups, for
  `$texturegroup`) and `_phys.txt` (the PHYS block, which carries the real
  per-bone hulls and joint limits the merged `_physics.glb` loses).
* **Reorganises into `modelsrc/` + `materialsrc/`**, hoists Alyx-style inner
  material folders, and purges compiled `.vtex`/`.vtex_c` and unreferenced
  textures.

Nothing is written outside the project folder, and the staging directory is
removed when the pass finishes.
"""
