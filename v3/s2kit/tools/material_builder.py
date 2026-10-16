"""
Material Builder.

One material, maps picked by hand. This is the tool for tuning: change the AO
strength or the Phong tint mode, run, look at the result on the Preview tab, and
run again. Every run is a history record, so a good setting can be recovered
later and applied to a whole folder with one of the batch tools.

It is also the escape hatch for a material the batch tools cannot group -
textures with no naming convention, or maps pulled from three different places.
"""

from __future__ import annotations

from pathlib import Path

from s2kit.core.jobs import JobContext
from s2kit.core.params import BoolParam, ChoiceParam, PathParam, TextParam, options
from s2kit.core.registry import tool
from s2kit.pipelines import materials, textures
from s2kit.tools import _materials_shared as shared
from s2kit.ui.tool_page import ToolPage

IMAGES = tuple(ext.lstrip(".") for ext in textures.IMAGE_EXTENSIONS)


def _map(key: str, label: str, help_text: str = "") -> PathParam:
    return PathParam(key, label, mode="open_file", group="Maps",
                     extensions=IMAGES, help=help_text)


@tool(
    id="material-builder",
    title="Material Builder",
    category="Materials / PBR",
    icon="TUNE",
    description="Build one Source 1 material from hand-picked PBR maps.",
    keywords=("pbr", "material", "single", "manual", "vmt", "vtf", "tune",
              "phong", "envmap"),
    order=30,
)
class MaterialBuilder(ToolPage):
    """Convert one material set, with every knob exposed."""

    RUN_LABEL = "Build"
    RUN_ICON = "TUNE"

    PARAMS = [
        _map("color", "Colour / albedo", "Required."),
        _map("normal", "Normal",
             "Required unless 'Ship a normal map' is off or 'Synthesise a "
             "missing normal map' is on."),
        _map("ao", "Ambient occlusion"),
        _map("roughness", "Roughness"),
        _map("metallic", "Metallic"),
        _map("orm", "Packed ORM",
             "R = occlusion, G = roughness, B = metallic. Split in memory; the "
             "individual maps above win where both are supplied."),
        _map("translucency", "Opacity"),
        _map("selfillum", "Self-illumination mask"),
        _map("tintmask", "Tint mask",
             "White takes the entity's render colour. Fake PBR only."),
        _map("heightmap", "Height", "Exo PBR parallax only."),

        PathParam("output_dir", "Output folder", mode="directory",
                  required=True, must_exist=False, group="Destination"),
        TextParam("name", "Material name", required=True, group="Destination",
                  hint="metal_crate_01",
                  help="Used for the VMT and as the prefix of every VTF."),
        TextParam("material_path", "Material path", default="models/ports",
                  group="Destination",
                  help="The path inside materials/ that the VMT's texture "
                       "references are written against."),

        *shared.conversion_params(),

        ChoiceParam("transparency", "Transparency", default="opaque",
                    group="Source intent",
                    choices=options(("opaque", "Opaque"),
                                    ("translucent", "$translucent"),
                                    ("alphatest", "$alphatest"))),
        BoolParam("tintable", "Colourable prop", default=False,
                  group="Source intent", depends_on=("mode", "fake"),
                  help="Bakes the tint mask into the basetexture alpha and "
                       "enables $blendtintbybasealpha."),

        *shared.fake_pbr_params(),
        *shared.exo_pbr_params(),
        *shared.output_params(batch=False),
    ]

    PREFERENCES = shared.preferences(parallel=False)

    HELP = """
### Material Builder

Pick the maps, set the knobs, build. The result lands on the Preview tab, so you
can inspect the packed channels without leaving the app - the texture viewer's
channel isolation is the fast way to check a Phong mask or a baked normal alpha.

#### The two modes

**Fake PBR** targets the stock Source 1 shaders. Source 1 has no metallic
workflow, so the conversion fakes it: AO and metal darkening are baked into the
albedo, the specular response becomes a Phong mask in the bumpmap's alpha, and
metal reflection colour becomes a cubemap mask. It is not energy-conserving and
cannot be. The generated material uses restrained Phong highlights and
metallicity- and roughness-controlled cubemap reflections, with only a faint
roughness-shaped rim response.
Three knobs decide where the faked energy lands:

* **Metal diffuse suppression** - how much darker metallic areas get. Higher is
  more physically honest; too high and dark metals go muddy.
* **Coloured metal relief** - eases that suppression on saturated pixels, so gold
  and copper keep their hue instead of turning grey.
* **Phong tint compensation** - `$phongalbedotint` makes the engine multiply
  Phong by the albedo at runtime. *Selective* divides that back out on coloured
  metals only and suppresses dielectrics, leaving their specular to the cubemap.

**Exo PBR** targets the ExoPBR screenspace shader, which does real PBR from a
packed ARM map. Better results, but the shader has to be installed on the target.

#### Channel layout it writes

| File | RGB | Alpha |
| --- | --- | --- |
| `_color.vtf` | albedo, AO and metal baked | opacity or tint mask |
| `_normal.vtf` | tangent normal, green flipped | Phong mask |
| `_phong.vtf` | R exponent, G metallic | - |
| `_envmask.vtf` | reflection tint | - |

The green channel is inverted on the way in, because Source 2 authors normals
green-up and Source 1 wants them green-down. A map whose filename says `_dx` is
left alone; **Normal green channel** forces it either way.

#### Tuning workflow

Every run is stored in History with its parameters. Find a setting that looks
right on one material, then feed the same numbers to the **VMAT Converter** or
**Texture Set Converter** for the rest of the pack.
"""

    def run_job(self, ctx: JobContext) -> None:
        params = ctx.params
        name = str(params["name"] or "").strip()
        if not name:
            raise ValueError("The material needs a name")
        if not params["color"]:
            raise ValueError("A colour map is required")

        transparency = str(params["transparency"])
        options = shared.build_options(
            params, ctx.prefs,
            translucent=transparency == "translucent",
            alphatest=transparency == "alphatest",
            tintable=bool(params["tintable"]))

        source = materials.MaterialSource(
            color=_path(params["color"]),
            normal=_path(params["normal"]),
            ao=_path(params["ao"]),
            roughness=_path(params["roughness"]),
            metallic=_path(params["metallic"]),
            translucency=_path(params["translucency"]),
            selfillum=_path(params["selfillum"]),
            tintmask=_path(params["tintmask"]),
            heightmap=_path(params["heightmap"]),
        )

        orm = _path(params["orm"])
        if orm is not None:
            packed = textures.load_rgba(orm)
            if packed is None:
                ctx.warn(f"Could not read the ORM map: {orm.name}")
            else:
                from s2kit.pipelines.pbr_maps import split_orm

                ao, roughness, metallic = split_orm(packed)
                # An explicitly picked single map is the more deliberate choice,
                # so it keeps the role.
                source.ao = source.ao or ao
                source.roughness = source.roughness or roughness
                source.metallic = source.metallic or metallic
                ctx.info(f"Split {orm.name} into AO, roughness and metallic")

        output_dir = Path(str(params["output_dir"]))
        result = materials.build_material(
            source, output_dir, name,
            str(params["material_path"] or "models/ports"), options,
            log=ctx.info, cancel=ctx.cancel_event)

        for note in result.notes:
            ctx.warn(note)
        summary = shared.report(ctx, [result])
        ctx.metric("Resolution", f"{result.width}x{result.height}")
        if result.stats is not None:
            ctx.metric("Avg roughness", f"{result.stats.avg_roughness:.2f}")
            ctx.metric("Avg metallic", f"{result.stats.avg_metallic:.2f}")
        if result.vmt is not None:
            ctx.table("VMT", ["Parameter", "Value"],
                      [[key, value] for key, value in result.vmt.params.items()])
        ctx.success(summary)
        ctx.summary(f"{name}: {result.summary()}")


def _path(value) -> Path | None:
    text = str(value or "").strip()
    return Path(text) if text else None
