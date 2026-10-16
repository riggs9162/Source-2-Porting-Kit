"""
Asset Inspector.

Walks a folder, classifies everything it finds and reports it as tables plus
previewable artefacts. Read-only, so it doubles as the reference example of the
tool contract: a `PARAMS` schema, a `PREFERENCES` block and one `run_job`.
"""

from __future__ import annotations

from pathlib import Path

from s2kit.core.jobs import JobContext
from s2kit.core.params import BoolParam, ChoiceParam, IntParam, PathParam, options
from s2kit.core.registry import tool
from s2kit.media import probe
from s2kit.media.probe import MediaKind
from s2kit.pipelines.studiomdl import check_compiled_model
from s2kit.ui.tool_page import ToolPage

KIND_ORDER = (MediaKind.MODEL, MediaKind.IMAGE, MediaKind.AUDIO,
              MediaKind.TEXT, MediaKind.DATA, MediaKind.ARCHIVE,
              MediaKind.BINARY)


@tool(
    id="asset-inspector",
    title="Asset Inspector",
    category="Inspect",
    icon="TRAVEL_EXPLORE",
    description="Scan a folder, classify every asset and preview any of them.",
    keywords=("browse", "scan", "audit", "textures", "models", "sounds"),
)
class AssetInspector(ToolPage):
    """Folder scanner that feeds the Preview tab."""

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

    def run_job(self, ctx: JobContext) -> None:
        folder = Path(str(ctx.params["folder"]))
        recursive = bool(ctx.params["recursive"])
        wanted = str(ctx.params["kind"])
        limit = int(ctx.params["max_files"])
        preview_all = bool(ctx.params["preview_all"])
        seed_count = int(ctx.prefs.get("preview_seed_count", 4))

        if not folder.is_dir():
            raise ValueError(f"Not a folder: {folder}")

        ctx.status(f"Listing {folder}...")
        ctx.info(f"Scanning {folder} ({'recursive' if recursive else 'top level'})")

        files = probe.find_files(folder, recursive=recursive, limit=limit)
        if not files:
            ctx.warn("No files found.")
            ctx.summary("Nothing to inspect")
            return
        ctx.info(f"Found {len(files):,} file(s)")

        buckets: dict[MediaKind, list[tuple[Path, int]]] = {}
        total_bytes = 0
        seeded: dict[MediaKind, int] = {}

        for index, path in enumerate(files, start=1):
            ctx.raise_if_cancelled()
            if index % 200 == 0 or index == len(files):
                ctx.progress(index, len(files))
                ctx.status(f"{index:,} / {len(files):,}  -  {path.name}")

            description = probe.describe(path)
            kind = description.kind
            if wanted != "all" and kind.value != wanted:
                continue

            buckets.setdefault(kind, []).append((path, description.size))
            total_bytes += description.size

            count = seeded.get(kind, 0)
            if preview_all or count < seed_count:
                seeded[kind] = count + 1
                ctx.artifact(path, label=path.name, kind=kind.value,
                             note=f"{kind.value}  -  {description.size_text()}")

        matched = sum(len(entries) for entries in buckets.values())
        if not matched:
            ctx.warn(f"No files of kind '{wanted}' under {folder}")
            ctx.summary(f"0 of {len(files):,} files matched")
            return

        # -- metrics ---------------------------------------------------
        ctx.metric("Files", f"{matched:,}")
        ctx.metric("Total size", probe.human_size(total_bytes))
        ctx.metric("Kinds", len(buckets))
        ctx.metric("Models", f"{len(buckets.get(MediaKind.MODEL, [])):,}",
                   tone="good" if buckets.get(MediaKind.MODEL) else "neutral")
        ctx.metric("Textures", f"{len(buckets.get(MediaKind.IMAGE, [])):,}",
                   tone="good" if buckets.get(MediaKind.IMAGE) else "neutral")

        self._report_compiled_models(ctx, buckets.get(MediaKind.MODEL, []))

        # -- summary table ---------------------------------------------
        summary_rows = []
        for kind in KIND_ORDER:
            entries = buckets.get(kind)
            if not entries:
                continue
            size = sum(item[1] for item in entries)
            summary_rows.append([
                kind.value, len(entries), probe.human_size(size),
                probe.human_size(size / len(entries)),
                ", ".join(sorted({item[0].suffix.lower() or "-"
                                  for item in entries})[:8]),
            ])
        ctx.table("Assets by kind",
                  ["Kind", "Files", "Total size", "Average", "Extensions"],
                  summary_rows, numeric_columns=(1,), sort_column=1,
                  sort_ascending=False)

        # -- extension breakdown ---------------------------------------
        extensions: dict[str, tuple[int, int]] = {}
        for entries in buckets.values():
            for path, size in entries:
                suffix = path.suffix.lower() or "(none)"
                count, total = extensions.get(suffix, (0, 0))
                extensions[suffix] = (count + 1, total + size)
        ctx.table("Extensions",
                  ["Extension", "Files", "Total size"],
                  [[suffix, count, probe.human_size(total)]
                   for suffix, (count, total) in extensions.items()],
                  numeric_columns=(1,), sort_column=1, sort_ascending=False)

        # -- largest files ---------------------------------------------
        if bool(ctx.prefs.get("report_largest", True)):
            top = int(ctx.prefs.get("largest_count", 15))
            everything = [(path, size) for entries in buckets.values()
                          for path, size in entries]
            everything.sort(key=lambda item: item[1], reverse=True)
            ctx.table(f"Largest {min(top, len(everything))} files",
                      ["File", "Size", "Kind", "Path"],
                      [[path.name, probe.human_size(size),
                        probe.kind_for(path).value, str(path)]
                       for path, size in everything[:top]],
                      path_column=3)

        ctx.success(f"Classified {matched:,} file(s), "
                    f"{probe.human_size(total_bytes)} total")
        ctx.summary(f"{matched:,} files  -  {probe.human_size(total_bytes)}  -  "
                    f"{len(buckets)} kinds")

    @staticmethod
    def _report_compiled_models(ctx: JobContext, models) -> None:
        """Flag compiled models whose .mdl, .vvd and .vtx are from different compiles.

        The engine refuses one of these outright - `Error Vertex File ...
        checksum X should be Y`, then `Bad geometry` - and a map compile is
        usually where it is first noticed. It happens when the compiler cannot
        overwrite one of the three, most often because the game had the model
        loaded at the time, and nothing about the compile says so.
        """
        compiled = [path for path, _size in models
                    if path.suffix.lower() == ".mdl"]
        if not compiled:
            return

        rows = []
        for path in compiled:
            ctx.raise_if_cancelled()
            model = check_compiled_model(path)
            if not model.ok:
                rows.append([path.stem, "; ".join(model.problems()), str(path)])

        ctx.metric("Broken models", len(rows),
                   tone="bad" if rows else "good")
        if rows:
            ctx.table("Models the engine will refuse",
                      ["Model", "Why", "Path"], rows, path_column=2)
            ctx.warn(f"{len(rows)} of {len(compiled)} compiled model(s) mix "
                     f"files from different compiles - recompile them with "
                     f"the game closed")
