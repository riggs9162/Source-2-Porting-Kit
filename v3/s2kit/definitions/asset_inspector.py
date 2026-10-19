"""Shared asset inspector schema and metadata."""
from __future__ import annotations

from s2kit.core.registry import tool
from s2kit.core.tool_definition import ToolDefinition
from s2kit.core.params import BoolParam, ChoiceParam, IntParam, PathParam, options


@tool(
    id="asset-inspector",
    title="Asset Inspector",
    category="Inspect",
    icon="TRAVEL_EXPLORE",
    description="Scan a folder, classify every asset and preview any of them.",
    keywords=("browse", "scan", "audit", "textures", "models", "sounds"),
)
class AssetInspector(ToolDefinition):
    """Shared declaration for AssetInspector."""
    EXECUTOR = "s2kit.tools.asset_inspector:AssetInspector"
    CUSTOM_VALIDATION = False
    RUN_LABEL = "Scan"
    RUN_ICON = "SEARCH"
    WRITES_FILES = False
    PARAMS = [
        PathParam("folder", "Folder to scan", mode="directory", required=True,
                  group="Input",
                  help="Every file underneath is classified by extension and "
                       "magic bytes."),
        BoolParam("recursive", "Include subfolders", default=True, group="Input"),
        ChoiceParam("kind", "Only this kind", default="all", group="Filter",
                    choices=options(("all", "Everything"), ("model", "Models"),
                                    ("image", "Textures"), ("audio", "Sounds"),
                                    ("text", "Text"), ("binary", "Other"))),
        IntParam("max_files", "Stop after", default=5000, min=10, max=200_000,
                 suffix="files", group="Filter", advanced=True),
        BoolParam("preview_all", "Add every hit to the Preview list",
                  default=False, group="Filter", advanced=True,
                  help="Off adds the first few of each kind, which is usually "
                       "what you want on a big folder."),
    ]
    PREFERENCES = [
        IntParam("preview_seed_count", "Files seeded into Preview per kind",
                 default=4, min=0, max=50, group="Preview"),
        BoolParam("report_largest", "Report the largest files", default=True,
                  group="Report"),
        IntParam("largest_count", "How many of the largest to list", default=15,
                 min=1, max=200, group="Report"),
    ]
    HELP = """
### Asset Inspector

Points at a folder and tells you what is in it.

* **Summary** table - one row per asset kind with counts and total size.
* **Largest files** table - what is actually costing you disk.
* **Preview** tab - the first few of each kind are seeded there, so a scan
  immediately gives you a texture, a model and a sound to look at.

Nothing is written or modified. Use it to sanity-check a VRF export before
running a conversion.

**Kinds** are decided by extension first, then by magic bytes for files whose
suffix lies (a `.vtf` that is really a PNG, for example).
"""
