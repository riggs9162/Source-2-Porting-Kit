"""
Material Extractor.

The conversion run backwards: a built Source 1 material back into the PBR maps it
was baked from. Useful when the original textures are gone - a workshop addon you
want to re-author, or your own output after the source folder was cleaned up.

The Phong exponent bake inverts exactly, so roughness and metallic come back
cleanly. Colour does not: AO was multiplied into the same pixels the albedo lives
in, and one equation cannot separate two unknowns. What comes out is the AO-baked
albedo with the metal darkening removed - a good starting point to re-author
from, not the original file.
"""

from __future__ import annotations

from pathlib import Path
from s2kit.core.jobs import JobContext
from s2kit.core.params import options
from s2kit.pipelines import materials, pbr_maps, vmt
from s2kit.tools._materials_shared import CLAMP_CHOICES
from s2kit.definitions.material_extract import MaterialExtractor as _MaterialExtractorDefinition

#: `<name>_color.vtf` and friends - the layout the converters write.
VTF_ROLES = {"_color": "color", "_normal": "normal", "_phong": "phong",
             "_envmask": "envmask", "_selfillum": "selfillum"}


class MaterialExtractor(_MaterialExtractorDefinition):
    """Reverse a Fake PBR material set back into editable maps."""


    def run_job(self, ctx: JobContext) -> None:
        params = ctx.params
        root = Path(str(params["input_root"]))
        if not root.is_dir():
            raise ValueError(f"Not a folder: {root}")

        recursive = bool(params["recursive"])
        if str(params["scan_mode"]) == "vmt":
            found = self._scan_vmt(root, recursive)
        else:
            found = self._scan_vtf(root, recursive)

        if not found:
            ctx.warn("No materials found here.")
            ctx.summary("Nothing to extract")
            return
        ctx.info(f"Found {len(found)} material(s)")

        output_root = Path(str(params["output_root"]))
        preserve = bool(params["preserve_structure"])
        options = materials.ExtractOptions(
            gloss_gamma=float(params["gloss_gamma"]),
            phong_strength=float(params["phong_strength"]),
            metal_diffuse_suppression=float(params["metal_diffuse_suppression"]),
            recover_albedo=bool(params["recover_albedo"]),
            invert_green=bool(params["invert_green"]),
            write_opacity=bool(params["write_opacity"]),
            clamp_size=int(params["clamp_size"] or 0),
            overwrite=bool(params["overwrite"]),
        )

        def extract(entry) -> dict:
            name, folder, sources = entry
            ctx.raise_if_cancelled()
            target = output_root
            if preserve:
                try:
                    target = output_root / folder.relative_to(root)
                except ValueError:
                    pass
            written = materials.extract_material(sources, target, name, options,
                                                 log=ctx.debug)
            return {"name": name, "written": written, "roles": sorted(sources),
                    "folder": target}

        outcome = ctx.map(found, extract, workers=int(ctx.prefs.get("workers", 4)),
                          label=lambda entry: entry[0], unit="material",
                          log_each=False)

        rows = [row for row in outcome.results if row is not None]
        extracted = [row for row in rows if row["written"]]
        skipped = len(rows) - len(extracted)
        files = [path for row in extracted for path in row["written"]]

        ctx.metric("Materials", len(extracted),
                   tone="good" if extracted else "neutral")
        ctx.metric("Maps written", len(files))
        if skipped:
            ctx.metric("Skipped", skipped, tone="warn")
        if outcome.failed:
            ctx.metric("Failed", outcome.failed, tone="bad")

        ctx.table("Extracted", ["Material", "Sources", "Maps", "Output"],
                  [[row["name"], ", ".join(row["roles"]), len(row["written"]),
                    str(row["folder"])] for row in rows],
                  numeric_columns=(2,), path_column=3)

        if bool(ctx.prefs.get("preview_results", True)):
            limit = int(ctx.prefs.get("preview_limit", 8))
            for row in extracted[:limit]:
                for path in row["written"][:3]:
                    ctx.artifact(path, label=path.name, kind="image")

        summary = f"extracted {len(extracted)}/{len(rows)} material(s), " \
                  f"{len(files)} map(s)"
        if skipped:
            summary += f", {skipped} already present"
        (ctx.error if outcome.failed and not extracted else ctx.success)(summary)
        ctx.summary(summary)

    # ------------------------------------------------------------------

    @staticmethod
    def _scan_vmt(root: Path, recursive: bool) -> list[tuple[str, Path, dict]]:
        """Every VMT whose texture references resolve to at least a basetexture."""
        found: list[tuple[str, Path, dict]] = []
        pattern = "**/*.vmt" if recursive else "*.vmt"
        for path in sorted(root.glob(pattern)):
            refs = vmt.read_texture_refs(path)
            sources: dict[str, Path] = {}
            for role, ref in refs.items():
                resolved = vmt.resolve_texture_ref(ref, path.parent, root)
                if resolved is not None:
                    sources[role] = resolved
            if "color" in sources:
                found.append((path.stem, path.parent, sources))
        return found

    @staticmethod
    def _scan_vtf(root: Path, recursive: bool) -> list[tuple[str, Path, dict]]:
        """Group VTFs by the `_color` / `_normal` / `_phong` suffix convention."""
        groups: dict[tuple[Path, str], dict[str, Path]] = {}
        pattern = "**/*.vtf" if recursive else "*.vtf"
        for path in sorted(root.glob(pattern)):
            stem = path.stem.lower()
            for suffix, role in VTF_ROLES.items():
                if stem.endswith(suffix):
                    key = (path.parent, path.stem[:-len(suffix)])
                    groups.setdefault(key, {})[role] = path
                    break
        return [(name, folder, sources)
                for (folder, name), sources in sorted(groups.items(),
                                                      key=lambda item: str(item[0]))
                if "color" in sources]
