"""
VMAT Converter.

The other half of a Source 2 model port. VRF Batch Export writes `modelsrc/` and
`materialsrc/`; glTF Batch SMD turns the first into something studiomdl can
compile, and this turns the second into the VMTs and VTFs that compiled model
will look for.

It reads each `.vmat`, resolves its texture references, and hands the result to
the material builder. Everything the vmat states about intent - translucency,
self-illumination, colourable tint, uniform roughness and metalness constants -
is carried across rather than guessed at, which is the entire reason to convert
from the vmat instead of from the loose PNGs beside it.
"""

from __future__ import annotations

from s2kit.definitions.vmat_convert import REQUIREMENT_CHOICES

from pathlib import Path
from typing import Optional

from s2kit.core.jobs import JobContext
from s2kit.core.params import options
from s2kit.pipelines import materials, vmat
from s2kit.pipelines.brush_materials import source1_surfaceprop
from s2kit.tools import _materials_shared as shared
from s2kit.definitions.vmat_convert import VmatConverter as _VmatConverterDefinition


class VmatConverter(_VmatConverterDefinition):
    """Batch-convert exported Source 2 materials."""


    def run_job(self, ctx: JobContext) -> None:
        params = ctx.params
        root = Path(str(params["vmat_root"]))
        if not root.is_dir():
            raise ValueError(f"Not a folder: {root}")

        found = vmat.find_vmats(root, recursive=bool(params["recursive"]))
        if not found:
            ctx.warn("No .vmat files here.")
            ctx.summary("No materials found")
            return
        ctx.info(f"Found {len(found)} vmat file(s)")

        texture_root = (Path(str(params["texture_root"]))
                        if params["texture_root"] else None)
        entries = vmat.parse_all(found, root=root, texture_root=texture_root,
                                 blend_layers=str(params.get("blend_layers", "base")))
        identities = [(entry.rel_dir.as_posix().casefold(), entry.name.casefold())
                      for entry in entries]
        if len(set(identities)) != len(identities):
            raise ValueError("Layer output names collide with another input material")

        needle = str(params["name_filter"] or "").strip().lower()
        if needle:
            entries = [entry for entry in entries if needle in entry.name.lower()]
            ctx.info(f"{len(entries)} match '{needle}'")

        entries, rejected = self._filter(entries, str(params["requires"]))
        for entry in entries:
            for warning in entry.warnings:
                ctx.debug(f"{entry.name}: {warning}")

        if bool(params["dry_run"]):
            self._report_scan(ctx, entries, rejected)
            return
        if not entries:
            ctx.warn("Nothing left to convert.")
            ctx.summary("Nothing to convert")
            return

        output_root = Path(str(params["output_root"]))
        prefix_path, prefix_tail = vmat.split_prefix(str(params["prefix"] or ""))
        suffix = str(params["suffix"] or "")
        base_path = str(params["material_path"] or "models/ports")
        preserve = bool(params["preserve_structure"])
        append = bool(params["append_subfolders"])
        options_mode = str(params["mode"])
        skip_existing = bool(params["skip_existing"])

        # A list, not a counter: `list.append` is atomic under the GIL while
        # `count += 1` from several workers can interleave and lose increments.
        skipped: list[str] = []

        def convert(entry: vmat.VmatEntry) -> Optional[materials.MaterialResult]:
            ctx.raise_if_cancelled()
            translucent, alphatest = vmat.resolve_transparency(
                entry, str(params["transparency"]))
            options = shared.build_options(
                params, ctx.prefs,
                translucent=translucent, alphatest=alphatest,
                tintable=entry.tintable and bool(params["port_tint"])
                         and options_mode == "fake")
            if options.shader == "LightmappedGeneric":
                options.surfaceprop = options.surfaceprop or source1_surfaceprop(entry.surfaceprop)
                options.decal = entry.decal
                if not entry.has("normal"):
                    options.emit_normal = False

            folder = output_root
            if prefix_path:
                folder = folder / prefix_path
            if preserve:
                folder = folder / entry.rel_dir
            name = f"{prefix_tail}{entry.name}{suffix}"

            if skip_existing and materials.is_already_built(folder, name, options):
                skipped.append(name)
                ctx.debug(f"{name}: already converted")
                return None

            source = entry.to_source(
                include_selfillum=bool(params["port_selfillum"]),
                include_tint=bool(params["port_tint"]))
            result = materials.build_material(
                source, folder, name,
                shared.material_path_for(base_path, prefix_path, entry.rel_dir,
                                         append),
                options, log=shared.logger_for(ctx, name),
                cancel=ctx.cancel_event)
            for note in result.notes:
                ctx.warn(f"{name}: {note}")
            return result

        outcome = ctx.map(entries, convert, workers=int(ctx.prefs.get("workers", 4)),
                          label=lambda entry: entry.name, unit="material",
                          log_each=False)

        summary = shared.report(
            ctx, outcome.results, failed=outcome.failed, skipped=len(skipped),
            unbuildable=[(entry.name, reason) for entry, reason in rejected])
        (ctx.error if outcome.failed and not outcome.succeeded else ctx.success)(summary)
        ctx.summary(summary)

    # ------------------------------------------------------------------

    @staticmethod
    def _filter(entries: list[vmat.VmatEntry], requirement: str):
        """Split entries into (convertible, [(entry, why)]) for the chosen rule."""
        keep: list[vmat.VmatEntry] = []
        rejected: list[tuple[vmat.VmatEntry, str]] = []
        for entry in entries:
            if not entry.buildable:
                rejected.append((entry, "no colour map"))
                continue
            if requirement in ("color_normal", "full") and not entry.has("normal"):
                rejected.append((entry, "no normal map"))
                continue
            if requirement == "full" and not (entry.has("roughness")
                                              or entry.has("metallic")
                                              or entry.roughness_constant is not None
                                              or entry.metallic_constant is not None):
                rejected.append((entry, "no roughness or metallic"))
                continue
            keep.append(entry)
        return keep, rejected

    @staticmethod
    def _report_scan(ctx: JobContext, entries: list[vmat.VmatEntry],
                     rejected: list[tuple[vmat.VmatEntry, str]]) -> None:
        ctx.metric("Convertible", len(entries),
                   tone="good" if entries else "warn")
        if rejected:
            ctx.metric("Unbuildable", len(rejected), tone="warn")

        roles = ("color", "normal", "ao", "roughness", "metallic",
                 "translucency", "selfillum", "tintmask")
        ctx.table(
            "Materials",
            ["Material", *[role.title() for role in roles], "Intent", "Source"],
            [[entry.name,
              *[entry.sources.get(role, "-") if entry.has(role) else ""
                for role in roles],
              _intent(entry), str(entry.path)]
             for entry in entries],
            path_column=len(roles) + 2)

        if rejected:
            ctx.table("Not convertible", ["Material", "Why", "File"],
                      [[entry.name, why, str(entry.path)]
                       for entry, why in rejected], path_column=2)

        warnings = [[entry.name, warning] for entry in entries
                    for warning in entry.warnings]
        if warnings:
            ctx.table("Warnings", ["Material", "Detail"], warnings)

        summary = f"scan only: {len(entries)} convertible, {len(rejected)} not"
        ctx.info(summary)
        ctx.summary(summary)


def _intent(entry: vmat.VmatEntry) -> str:
    flags = []
    if entry.translucent:
        flags.append("translucent")
    if entry.alphatest:
        flags.append("alphatest")
    if entry.selfillum:
        flags.append("glow")
    if entry.tintable:
        flags.append("tintable")
    return ", ".join(flags) or "opaque"
