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
from s2kit.core.params import (
    BoolParam, ChoiceParam, FloatParam, IntParam, PathParam, options,
)
from s2kit.core.registry import tool
from s2kit.pipelines import materials, pbr_maps, vmt
from s2kit.tools._materials_shared import CLAMP_CHOICES
from s2kit.ui.tool_page import ToolPage

#: `<name>_color.vtf` and friends - the layout the converters write.
VTF_ROLES = {"_color": "color", "_normal": "normal", "_phong": "phong",
             "_envmask": "envmask", "_selfillum": "selfillum"}


@tool(
    id="material-extract",
    title="Material Extractor",
    category="Materials / PBR",
    icon="UNDO",
    description="Decode built Source 1 materials back into PBR maps as PNG.",
    keywords=("reverse", "extract", "decode", "vtf", "vmt", "pbr", "roughness",
              "metallic", "unbake"),
    order=40,
)
class MaterialExtractor(ToolPage):
    """Reverse a Fake PBR material set back into editable maps."""

    RUN_LABEL = "Extract"
    RUN_ICON = "UNDO"

    PARAMS = [
        PathParam("input_root", "Materials folder", mode="directory",
                  required=True, group="Input"),
        BoolParam("recursive", "Include subfolders", default=True, group="Input"),
        ChoiceParam("scan_mode", "Find materials by", default="vmt",
                    group="Input", style="segmented",
                    choices=options(("vmt", "VMT files"),
                                    ("vtf", "VTF filenames")),
                    help="VMT is the reliable one - it names its own textures, "
                         "so the output is clean even when the VTFs are not."),
        PathParam("output_root", "Output folder", mode="directory",
                  required=True, must_exist=False, group="Output"),
        BoolParam("preserve_structure", "Mirror the input folder tree",
                  default=True, group="Output"),
        BoolParam("overwrite", "Overwrite existing PNGs", default=False,
                  group="Output"),
        ChoiceParam("clamp_size", "Clamp resolution", default=0, group="Output",
                    choices=CLAMP_CHOICES),

        FloatParam("phong_strength", "Phong strength used", default=0.5,
                   min=0.01, max=1.0, step=0.05, decimals=2, group="Bake settings",
                   help="Must match what built the material, or roughness comes "
                        "back on the wrong curve."),
        FloatParam("gloss_gamma", "Gloss gamma used", default=2.0, min=0.5,
                   max=4.0, step=0.1, decimals=2, group="Bake settings"),
        FloatParam("metal_diffuse_suppression", "Metal suppression used",
                   default=pbr_maps.METAL_DIFFUSE_SUPPRESSION, min=0.0, max=1.0, step=0.01, decimals=2,
                   group="Bake settings"),
        BoolParam("recover_albedo", "Undo the metal darkening", default=True,
                  group="Bake settings",
                  help="Needs the Phong map, which carries the metallic channel."),
        BoolParam("invert_green", "Flip the normal's green channel back",
                  default=True, group="Bake settings",
                  help="On by default so the maps come back the way Source 2 "
                       "authored them, and feed straight into a converter "
                       "again. Turn it off to keep Source 1's convention."),
        BoolParam("write_opacity", "Write an opacity map when alpha is used",
                  default=True, group="Bake settings"),
    ]

    PREFERENCES = [
        IntParam("workers", "Materials extracted in parallel", default=4, min=1,
                 max=32, group="Performance"),
        BoolParam("preview_results", "Add extracted maps to Preview",
                  default=True, group="Report"),
        IntParam("preview_limit", "Preview at most", default=8, min=0, max=100,
                 suffix="materials", group="Report"),
    ]

    HELP = """
### Material Extractor

Decodes `_color.vtf`, `_normal.vtf` and `_phong.vtf` back into
`_color.png`, `_normal.png`, `_roughness.png` and `_metallic.png`.

#### The two scan modes

**By VMT** reads each `.vmt`, follows its `$basetexture`, `$bumpmap` and
`$phongexponenttexture` to real files, and names the output after the VMT. This
works even when the VTFs are named inconsistently, and it is what you want for
someone else's addon.

**By VTF filenames** matches the `_color` / `_normal` / `_phong` suffix
convention directly. Faster, and correct for output this kit produced.

#### What inverts and what does not

`_phong.vtf` stores `R = (1 - roughness) ^ gamma * strength` and `G = metallic`.
Given the same two numbers the bake used, roughness inverts exactly - which is
why the **Bake settings** group exists. Leave them at the defaults for materials
this kit built; change them if you know the material came from something else.

The basetexture is the lossy direction. AO was multiplied into the albedo before
encoding, and nothing recorded it separately, so the AO cannot be pulled back
out. Undoing the metal darkening is possible because the metallic channel
survived in the Phong map, and that is what **Undo the metal darkening** does.

The bumpmap's alpha is skipped entirely. It holds the Phong mask, which is a
product of roughness, metallic and AO all at once - already recovered more
cleanly from the Phong map.

#### The green channel comes back flipped

The converters invert the normal's green on the way in, because Source 2 authors
it green-up and Source 1 wants green-down. So the extractor inverts it back, and
what lands on disk is the map the way Source 2 had it - which is what you want if
it is going into a PBR tool or straight back through a converter. Turn **Flip the
normal's green channel back** off to keep Source 1's convention instead.
"""

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
