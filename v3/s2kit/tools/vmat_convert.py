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

from pathlib import Path
from typing import Optional

from s2kit.core.jobs import JobContext
from s2kit.core.params import BoolParam, ChoiceParam, PathParam, TextParam, options
from s2kit.core.registry import tool
from s2kit.pipelines import materials, vmat
from s2kit.tools import _materials_shared as shared
from s2kit.ui.tool_page import ToolPage

REQUIREMENT_CHOICES = options(
    ("color", "A colour map only"),
    ("color_normal", "Colour and normal"),
    ("full", "Colour, normal and a specular signal"))


@tool(
    id="vmat-convert",
    title="VMAT Converter",
    category="Materials / PBR",
    icon="AUTO_AWESOME_MOSAIC",
    description="Convert Source 2 .vmat materials into Source 1 VMT + VTF.",
    keywords=("vmat", "source 2", "vmt", "vtf", "material", "pbr", "port",
              "batch", "convert"),
    order=10,
)
class VmatConverter(ToolPage):
    """Batch-convert exported Source 2 materials."""

    RUN_LABEL = "Convert"
    RUN_ICON = "AUTO_AWESOME_MOSAIC"

    PARAMS = [
        PathParam("vmat_root", "VMAT folder", mode="directory", required=True,
                  group="Input",
                  help="Usually the materialsrc/ folder VRF Batch Export wrote."),
        BoolParam("recursive", "Include subfolders", default=True, group="Input"),
        PathParam("texture_root", "Texture root", mode="directory",
                  must_exist=False, group="Input", advanced=True,
                  help="Where game-relative texture paths resolve from. Empty "
                       "looks beside each vmat, which is where VRF puts them."),
        TextParam("name_filter", "Only names containing", group="Input",
                  help="Case-insensitive substring filter. Empty converts all."),
        ChoiceParam("requires", "Only convert materials with",
                    default="color", group="Input", choices=REQUIREMENT_CHOICES),

        PathParam("output_root", "Output folder", mode="directory",
                  required=True, must_exist=False, group="Destination"),
        BoolParam("preserve_structure", "Mirror the input folder tree",
                  default=True, group="Destination"),
        BoolParam("append_subfolders", "Append subfolders to the material path",
                  default=False, group="Destination",
                  help="Keeps the VMT's texture references pointing at the "
                       "mirrored tree rather than one flat folder."),

        *shared.naming_params(),
        *shared.conversion_params(),

        ChoiceParam("transparency", "Transparency", default="auto",
                    group="Source intent",
                    choices=options(("auto", "Follow the vmat"),
                                    ("translucent", "Force $translucent"),
                                    ("alphatest", "Force $alphatest"),
                                    ("opaque", "Force opaque")),
                    help="Forcing only applies to materials that already showed "
                         "some transparency intent."),
        BoolParam("port_selfillum", "Port self-illumination", default=True,
                  group="Source intent"),
        BoolParam("port_tint", "Port colourable tint masks", default=True,
                  group="Source intent",
                  help="Fake PBR only - the Exo shader has no "
                       "$blendtintbybasealpha equivalent."),

        *shared.fake_pbr_params(),
        *shared.exo_pbr_params(),
        *shared.output_params(),
        BoolParam("dry_run", "Scan only (report, write nothing)", default=False,
                  group="Output"),
    ]

    PREFERENCES = shared.preferences()

    HELP = """
### VMAT Converter

Point it at the `materialsrc/` folder VRF Batch Export produced and it writes a
Source 1 material for every `.vmat` inside.

#### What comes across

| Source 2 | Source 1 |
| --- | --- |
| `TextureColor` + `TextureAmbientOcclusion` | `$basetexture` with AO baked in |
| `TextureNormal` | `$bumpmap`, green flipped, Phong mask in alpha |
| `TextureRoughness` / `g_flRoughness` | `$phongexponenttexture`, `$phongboost` |
| `TextureMetalness` / `g_flMetalness` | `$envmapmask`, metal darkening |
| `F_TRANSLUCENT` / `F_ALPHA_TEST` | `$translucent` / `$alphatest` |
| `F_SELF_ILLUM` + `g_vSelfIllumTint` | `$selfillum` or `$EmissiveBlend*` |
| `F_TINT_MASK` + `g_flModelTintAmount` | `$blendtintbybasealpha` |

Fake PBR uses restrained Phong, low-intensity cubemap reflections, and a faint
roughness-shaped rim response.

#### Resolving textures

A vmat names its textures game-relative (`models/props/foo_color.png`) while the
exported PNG sits next to the vmat. Resolution tries the texture root, then that
same filename beside the vmat, then falls back to matching siblings by name -
`glow_sign.vmat` will find `glow_sign_selfillum.png` on its own.

When a texture value is a bracketed literal (`"TextureColor" "[1 1 1 0]"`) the
artist used a solid colour, and that literal is baked as a uniform map rather
than reported missing.

#### The normal's green channel is flipped

Source 2 authors tangent normals green-up; Source 1 wants them green-down. A
straight copy leaves every bump on the model reading inside out, so the green
channel is inverted by default. A map whose filename already says `_dx` is
taken at its word and left alone; **Normal green channel** overrides both ways.

#### Shipping without a normal map

**Ship a normal map** off writes no `$bumpmap` or `$phong` with it, since Phong
shades against the bumpmap's normal and is ignored without one.

That combination is what a `prop_static` needs. On every branch before the CS:GO
engine, Garry's Mod included, a model whose material carries `$bumpmap`,
`$normalmap` or `$phong` on *any* skin loses its per-vertex lighting: the vertex
colours VRAD baked are discarded and the prop is lit from a single point at its
`$illumposition`, the way a `prop_dynamic` is. Static prop lightmapping
(`-StaticPropLighting` on the Source 2013 multiplayer, TF2 and GMod branches)
carries the same restriction. So a bumpmapped prop sitting in carefully baked
map lighting is the one that ends up looking wrong.

Map assets therefore often want converting twice: once with the normal for
anything placed as a `prop_dynamic` or spawned as a physics prop, once without it
for anything that has to sit in baked lighting. `$envmap` is untouched either way
- a cubemap reflection needs no tangent normal - so a static prop can still be
shiny.

#### Baking normal detail into the base texture

With **Ship a normal map** off, enable **Bake normal detail into base texture**
to retain subtle surface relief without writing a bumpmap or Phong block.
**Baked detail strength** defaults to 0.25; 0 leaves the base texture unchanged.
The bake uses the source normal and the **Normal green channel** setting, keeps
opacity and tint masks intact, and fades out under emissive pixels. Missing or
flat normals are skipped. **Write VTF textures** must be enabled.

This is fixed shading, so it cannot follow moving lights and may reveal rotated
UV islands. It is an optional visual approximation for static props, not dynamic
normal mapping. The default remains off.

#### Nothing is invented

A material with no roughness or metallic gets no Phong block and no
`_phong.vtf`. A flat normal gets no `$bumpmap`. A mask that bakes to pure black
is dropped along with the parameter that would have referenced it. The Output
tab lists every drop and why.

#### Name prefixes that move things

A prefix containing slashes overrides the material path as well as the filename,
so typing `models/me/hlvr/` puts the files under that folder *and* points the
VMT's `$basetexture` at it - which is what Source 1 needs in order to find them.
"""

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
        entries = vmat.parse_all(found, root=root, texture_root=texture_root)

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
