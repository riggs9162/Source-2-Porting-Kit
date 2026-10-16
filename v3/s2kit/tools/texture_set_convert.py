"""
Texture Set Converter.

For textures that arrive without a vmat: a marketplace pack, a Substance export,
a scraped addon. It groups a folder of loose maps into material sets by filename
and converts each one.

The trade against the VMAT Converter is that filenames carry no intent. There is
nothing in `crate_01_BaseColor.png` that says the material is translucent, glows,
or takes an entity tint, so those decisions become form fields here instead of
being read from the source.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from s2kit.core.jobs import JobContext
from s2kit.core.params import BoolParam, ChoiceParam, PathParam, TextParam, options
from s2kit.core.registry import tool
from s2kit.pipelines import materials, texture_sets
from s2kit.tools import _materials_shared as shared
from s2kit.ui.tool_page import ToolPage


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
class TextureSetConverter(ToolPage):
    """Filename-driven batch conversion of loose PBR textures."""

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
                    choices=options(*texture_sets.MISSING_METAL_MODES),
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

#### What it cannot know

Filenames say nothing about translucency, glow or entity tint, so those are set
once for the whole run. If the material came from Source 2 and you still have
the `.vmat`, use the **VMAT Converter** instead - it reads all of that from the
source.
"""

    def run_job(self, ctx: JobContext) -> None:
        params = ctx.params
        root = Path(str(params["input_root"]))
        if not root.is_dir():
            raise ValueError(f"Not a folder: {root}")

        sets = texture_sets.scan(root, recursive=bool(params["recursive"]))
        if not sets:
            ctx.warn("No textures found here.")
            ctx.summary("No textures found")
            return
        ctx.info(f"{len(sets)} texture set(s) from "
                 f"{sum(len(item.files) for item in sets)} file(s)")

        needle = str(params["name_filter"] or "").strip().lower()
        if needle:
            sets = [item for item in sets if needle in item.key.lower()]
            ctx.info(f"{len(sets)} match '{needle}'")

        sets, rejected = self._filter(sets, str(params["requires"]))
        for item in sets:
            for warning in item.warnings:
                ctx.warn(f"{item.name}: {warning}")

        if bool(params["dry_run"]):
            self._report_scan(ctx, sets, rejected)
            return
        if not sets:
            ctx.warn("Nothing left to convert.")
            ctx.summary("Nothing to convert")
            return

        output_root = Path(str(params["output_root"]))
        from s2kit.pipelines.vmat import split_prefix
        prefix_path, prefix_tail = split_prefix(str(params["prefix"] or ""))
        suffix = str(params["suffix"] or "")
        base_path = str(params["material_path"] or "models/ports")
        preserve = bool(params["preserve_structure"])
        append = bool(params["append_subfolders"])
        transparency = str(params["transparency"])
        missing_metal = str(params["missing_metal"])
        skip_existing = bool(params["skip_existing"])
        # A list, not a counter: `list.append` is atomic under the GIL while
        # `count += 1` from several workers can interleave and lose increments.
        skipped: list[str] = []

        options = shared.build_options(
            params, ctx.prefs,
            translucent=transparency == "translucent",
            alphatest=transparency == "alphatest")

        def convert(item: texture_sets.TextureSet) -> Optional[materials.MaterialResult]:
            ctx.raise_if_cancelled()
            relative = _relative(item.directory, root)
            folder = output_root
            if prefix_path:
                folder = folder / prefix_path
            if preserve:
                folder = folder / relative
            name = f"{prefix_tail}{item.name}{suffix}"

            if skip_existing and materials.is_already_built(folder, name, options):
                skipped.append(name)
                ctx.debug(f"{name}: already converted")
                return None

            source = item.to_source(
                missing_metal=missing_metal,
                include_selfillum=bool(params["port_selfillum"]))
            result = materials.build_material(
                source, folder, name,
                shared.material_path_for(base_path, prefix_path, relative, append),
                options, log=shared.logger_for(ctx, name),
                cancel=ctx.cancel_event)
            for note in result.notes:
                ctx.warn(f"{name}: {note}")
            return result

        outcome = ctx.map(sets, convert, workers=int(ctx.prefs.get("workers", 4)),
                          label=lambda item: item.name, unit="material",
                          log_each=False)

        summary = shared.report(
            ctx, outcome.results, failed=outcome.failed, skipped=len(skipped),
            unbuildable=[(item.name, why) for item, why in rejected])
        (ctx.error if outcome.failed and not outcome.succeeded else ctx.success)(summary)
        ctx.summary(summary)

    # ------------------------------------------------------------------

    @staticmethod
    def _filter(sets: list[texture_sets.TextureSet], requirement: str):
        keep: list[texture_sets.TextureSet] = []
        rejected: list[tuple[texture_sets.TextureSet, str]] = []
        for item in sets:
            if not item.buildable:
                rejected.append((item, "no colour map"))
                continue
            if requirement in ("color_normal", "full") and not item.has("normal"):
                rejected.append((item, "no normal map"))
                continue
            if requirement == "full" and not (item.has("roughness")
                                              or item.has("metallic")):
                rejected.append((item, "no roughness or metallic"))
                continue
            keep.append(item)
        return keep, rejected

    @staticmethod
    def _report_scan(ctx: JobContext, sets: list[texture_sets.TextureSet],
                     rejected: list[tuple[texture_sets.TextureSet, str]]) -> None:
        ctx.metric("Convertible", len(sets), tone="good" if sets else "warn")
        if rejected:
            ctx.metric("Unbuildable", len(rejected), tone="warn")

        ctx.table("Texture sets", ["Material", "Files", "Roles", "ORM", "Folder"],
                  [[item.name, len(item.files), item.roles(),
                    item.orm.name if item.orm else "-", str(item.directory)]
                   for item in sets],
                  numeric_columns=(1,), path_column=4)

        if rejected:
            ctx.table("Not convertible", ["Set", "Why", "Folder"],
                      [[item.name, why, str(item.directory)]
                       for item, why in rejected], path_column=2)

        warnings = [[item.name, warning] for item in sets
                    for warning in item.warnings]
        if warnings:
            ctx.table("Warnings", ["Set", "Detail"], warnings)

        summary = f"scan only: {len(sets)} convertible, {len(rejected)} not"
        ctx.info(summary)
        ctx.summary(summary)


def _relative(folder: Path, root: Path) -> Path:
    try:
        return folder.relative_to(root)
    except ValueError:
        return Path()
