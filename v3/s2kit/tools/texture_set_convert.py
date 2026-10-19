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
from s2kit.core.params import options
from s2kit.pipelines import materials, texture_sets
from s2kit.tools import _materials_shared as shared
from s2kit.definitions.texture_set_convert import TextureSetConverter as _TextureSetConverterDefinition


class TextureSetConverter(_TextureSetConverterDefinition):
    """Filename-driven batch conversion of loose PBR textures."""


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
