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

from s2kit.definitions.material_builder import IMAGES

from pathlib import Path

from s2kit.core.jobs import JobContext
from s2kit.core.params import PathParam, options
from s2kit.pipelines import materials, textures
from s2kit.tools import _materials_shared as shared
from s2kit.definitions.material_builder import MaterialBuilder as _MaterialBuilderDefinition


def _map(key: str, label: str, help_text: str = "") -> PathParam:
    return PathParam(key, label, mode="open_file", group="Maps",
                     extensions=IMAGES, help=help_text)


class MaterialBuilder(_MaterialBuilderDefinition):
    """Convert one material set, with every knob exposed."""


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
