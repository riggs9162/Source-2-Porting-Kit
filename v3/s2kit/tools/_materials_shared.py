"""
Shared schema and reporting for the material tools.

The VMAT Converter, Texture Set Converter and Material Builder differ only in
how they find their inputs - once a material set is resolved, every knob and
every output is the same. This module holds that common half: the parameter
blocks, the preference block, the translation into a
:class:`~s2kit.pipelines.materials.MaterialOptions`, and the metrics/table each
tool reports afterwards.

Modules starting with `_` are skipped by the tool discovery pass, so this is a
plain helper rather than a tool.
"""

from __future__ import annotations

from s2kit.definitions._materials_shared import (
    CLAMP_CHOICES, MODE_CHOICES, NORMAL_GREEN_CHOICES, SHADER_CHOICES, TINT_MODE_CHOICES,
    conversion_params, exo_pbr_params, fake_pbr_params, naming_params, output_params, preferences,
)

from pathlib import Path
from typing import Any, Optional, Sequence

from s2kit.core.jobs import JobContext
from s2kit.media.probe import human_size
from s2kit.pipelines import materials, pbr_maps, vmt


def build_options(params: dict[str, Any], prefs: dict[str, Any], *,
                  translucent: bool = False, alphatest: bool = False,
                  tintable: bool = False) -> materials.MaterialOptions:
    """Turn a tool's form values into the pipeline's option record.

    Transparency and tint are passed in rather than read from `params`: a batch
    tool derives them per material from the source vmat, while the single-material
    tool exposes them as form fields.
    """
    return materials.MaterialOptions(
        mode=str(params.get("mode", "fake")),
        generate_vtf=bool(params.get("generate_vtf", True)),
        generate_vmt=bool(params.get("generate_vmt", True)),
        generate_mipmaps=bool(params.get("generate_mipmaps", True)),
        clamp_size=int(params.get("clamp_size", 0) or 0),
        output_clamps=output_clamps(prefs),
        synthesize_missing_maps=bool(params.get("synthesize_missing_maps", False)),
        branch=str(params.get("branch", "gmod")),
        shader=str(params.get("shader", "VertexLitGeneric")),
        envmap=str(params.get("envmap", "env_cubemap") or "env_cubemap"),
        surfaceprop=str(params.get("surfaceprop", "") or ""),
        ao_strength=float(params.get("ao_strength", 0.7)),
        emissive_ao_relief=float(params.get("emissive_ao_relief", 1.0)),
        gloss_gamma=float(params.get("gloss_gamma", 2.0)),
        metal_diffuse_suppression=float(
            params.get("metal_diffuse_suppression", pbr_maps.METAL_DIFFUSE_SUPPRESSION)),
        envmask_gamma=float(params.get("envmask_gamma", 1.5)),
        phong_strength=float(params.get("phong_strength", 0.75)),
        phong_tint_mode=str(params.get("phong_tint_mode", "selective")),
        colored_metal_relief=float(params.get("colored_metal_relief", pbr_maps.COLORED_METAL_RELIEF)),
        emit_normal=bool(params.get("emit_normal", True)),
        bake_normal_detail=bool(params.get("bake_normal_detail", False)),
        normal_detail_strength=float(params.get("normal_detail_strength", 0.25)),
        normal_green=str(params.get("normal_green", "auto")),
        translucent=translucent,
        alphatest=alphatest,
        tintable=tintable,
        glow_mode=str(params.get("glow_mode", "emissiveblend")),
        emission_scale=float(params.get("emission_scale", 0.0)),
        parallax_scale=float(params.get("parallax_scale", 0.0)),
        alpha_blend=bool(params.get("alpha_blend", False)),
    )


def output_clamps(prefs: dict[str, Any]) -> dict[str, int]:
    """The per-output resolution caps, or an empty dict when they are off."""
    if not bool(prefs.get("per_output_clamps", False)):
        return {}
    return {key: int(prefs.get(f"clamp_{key}", 0) or 0)
            for key in materials.OUTPUT_KEYS}


def material_path_for(base: str, prefix_path: str, rel_dir: Path,
                      append_subfolders: bool) -> str:
    """The material path one output should use.

    A path-shaped prefix overrides the base entirely - that is the whole point
    of typing one - and the source's own subfolder is appended when the tool is
    mirroring the input tree.
    """
    path = prefix_path or base
    if not append_subfolders:
        return path
    relative = rel_dir.as_posix().strip("./")
    return f"{path}/{relative}" if relative else path


# ----------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------

def report(ctx: JobContext, results: Sequence[Optional[materials.MaterialResult]],
           *, failed: int = 0, skipped: int = 0,
           unbuildable: Sequence[tuple[str, str]] = ()) -> str:
    """Emit the metrics, tables and artefacts every material tool reports.

    Returns the one-line summary the caller should pass to `ctx.summary`.
    """
    built = [result for result in results if result is not None]
    files = [path for result in built for path in result.written]
    total_bytes = sum(_size(path) for path in files)

    ctx.metric("Materials", len(built), tone="good" if built else "neutral")
    ctx.metric("Files", len(files))
    ctx.metric("Written", human_size(total_bytes))
    if skipped:
        ctx.metric("Skipped", skipped, tone="warn")
    if unbuildable:
        ctx.metric("Unbuildable", len(unbuildable), tone="warn")
    if failed:
        ctx.metric("Failed", failed, tone="bad")

    if built:
        ctx.table(
            "Materials",
            ["Material", "Size", "VTF", "Dropped", "Output"],
            [[result.name, f"{result.width}x{result.height}",
              len(result.vtf_files),
              ", ".join(name for name, _ in result.dropped) or "-",
              str(result.vmt_file or result.output_dir)]
             for result in built],
            numeric_columns=(2,), path_column=4)

    dropped_rows = [[result.name, name, reason]
                    for result in built for name, reason in result.dropped]
    if dropped_rows:
        ctx.table("Skipped outputs", ["Material", "Output", "Why"], dropped_rows)

    if unbuildable:
        ctx.table("Not converted", ["Material", "Why"],
                  [[name, why] for name, why in unbuildable])

    if bool(ctx.prefs.get("preview_results", True)):
        limit = int(ctx.prefs.get("preview_limit", 8))
        shown = 0
        for result in built:
            if shown >= limit:
                break
            for path in result.written[:3]:
                ctx.artifact(path, label=path.name)
            shown += 1

    summary = f"{len(built)} material(s), {len(files)} file(s), " \
              f"{human_size(total_bytes)}"
    if skipped:
        summary += f", {skipped} skipped"
    if failed:
        summary += f", {failed} failed"
    return summary


def logger_for(ctx: JobContext, name: str):
    """A log callback for the pipeline, honouring the verbosity preference."""
    verbose = bool(ctx.prefs.get("log_each_material", False))
    if verbose:
        return lambda message: ctx.info(f"{name}: {message}")
    return lambda message: ctx.debug(f"{name}: {message}")


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
