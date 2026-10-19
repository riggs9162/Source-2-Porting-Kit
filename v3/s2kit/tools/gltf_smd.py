"""
glTF Batch SMD.

Stage two of the port: turns the `modelsrc/` tree VRF Batch Export wrote into
SMD meshes, animation SMDs and a QC that studiomdl can compile. Point it at a
folder, and every glTF/GLB under it becomes a compilable model.

The conversion itself lives in :mod:`s2kit.pipelines.gltf_smd`; this file is
the parameter schema plus the batch orchestration.
"""

from __future__ import annotations

from s2kit.definitions.gltf_smd import UV_MODES

import fnmatch
from pathlib import Path
from typing import Optional

from s2kit.core.jobs import JobCancelled, JobContext
from s2kit.media.probe import human_size
from s2kit.pipelines import gltf_smd as pipeline
from s2kit.definitions.gltf_smd import GltfBatchSmd as _GltfBatchSmdDefinition


class GltfBatchSmd(_GltfBatchSmdDefinition):
    """Batch glTF -> SMD/QC conversion."""


    # ------------------------------------------------------------------

    def run_job(self, ctx: JobContext) -> None:
        params = ctx.params
        input_root = Path(str(params["input"]))
        output_root = Path(str(params["output"]))
        if not input_root.is_dir():
            raise ValueError(f"Not a folder: {input_root}")

        ctx.status(f"Scanning {input_root}...")
        model_sets = pipeline.ModelSetScanner(
            input_root, recursive=bool(params["recursive"])).find_sets()
        ctx.info(f"Found {len(model_sets)} model set(s) under {input_root}")

        patterns = _patterns(str(params["filter"] or ""))
        if patterns:
            before = len(model_sets)
            model_sets = [ms for ms in model_sets if _matches(ms.name, patterns)]
            ctx.info(f"Filter {', '.join(patterns)} kept "
                     f"{len(model_sets)} of {before}")
        if not model_sets:
            ctx.warn("Nothing to convert.")
            ctx.summary("No model sets matched")
            return

        model_sets.sort(key=lambda ms: str(ms.render_path).lower())
        for model_set in model_sets:
            for warning in model_set.warnings:
                ctx.warn(f"{model_set.name}: {warning}")

        conversion = self._options(ctx)
        if conversion.dry_run:
            ctx.warn("Dry run - nothing will be written.")

        def convert(model_set):
            return pipeline.convert_model_set(
                model_set, conversion,
                input_root=input_root, output_root=output_root,
                on_log=ctx.info, cancel=ctx.cancel_event)

        workers = int(ctx.prefs.get("workers", 4))
        try:
            outcome = ctx.map(model_sets, convert, workers=workers,
                              label=lambda ms: ms.name, unit="model",
                              log_each=False)
        except pipeline.ConversionCancelled as exc:
            raise JobCancelled() from exc

        results = [r for r in outcome.results if r is not None]
        self._report(ctx, results, outcome, output_root)

    # ------------------------------------------------------------------

    def _options(self, ctx: JobContext) -> pipeline.ConversionOptions:
        """Parameters -> the pipeline's options object."""
        params = ctx.params
        return pipeline.ConversionOptions(
            scale=float(params["scale"]),
            axis_conversion=bool(params["axis_conversion"]),
            force_manual_scale=bool(params["force_manual_scale"]),
            flip_v=bool(params["flip_v"]),
            uv_mode=str(params["uv_mode"]),
            generate_smd=bool(params["generate_smd"]),
            generate_qc=bool(params["generate_qc"]),
            export_physics=bool(params["export_physics"]),
            export_animations=bool(params["export_animations"]),
            animation_fps=float(params["animation_fps"]),
            auto_loop_detect=bool(params["auto_loop_detect"]),
            modelname_prefix=str(params["modelname_prefix"]),
            cdmaterials=str(params["cdmaterials"]),
            concave=bool(params["concave"]),
            auto_mass=bool(params["auto_mass"]),
            mass_modifier=float(params["mass_modifier"]),
            static_mass=float(params["static_mass"]),
            auto_surfaceprop=bool(params["auto_surfaceprop"]),
            surfaceprop_override=str(params["surfaceprop_override"] or ""),
            static_surfaceprop=str(params["static_surfaceprop"]),
            decompose_collision=bool(params["decompose_collision"]),
            max_convex_pieces=int(params["max_convex_pieces"]),
            remove_2d_collision=bool(params["remove_2d_collision"]),
            collision_from_render=bool(params["collision_from_render"]),
            skip_physics_for_large=bool(params["skip_physics_for_large"]),
            large_model_threshold=float(params["large_model_threshold"]),
            preserve_folders=bool(params["preserve_folders"]),
            replace_existing=bool(params["replace_existing"]),
            dry_run=bool(params["dry_run"]),
            verbose=bool(ctx.prefs.get("verbose", False)),
        )

    def _report(self, ctx: JobContext, results: list, outcome,
                output_root: Path) -> None:
        converted = [r for r in results if r.status == "converted"]
        dry = [r for r in results if r.status == "dry-run"]
        skipped = [r for r in results if r.status == "skipped"]
        failed = [r for r in results if r.status == "failed"]
        written = [path for r in results for path in r.files]

        ctx.metric("Converted", len(converted) or len(dry),
                   tone="good" if (converted or dry) else "neutral")
        if skipped:
            ctx.metric("Skipped", len(skipped), tone="warn")
        if failed or outcome.failed:
            ctx.metric("Failed", len(failed) + outcome.failed, tone="bad")
        ctx.metric("Triangles", f"{sum(r.triangles for r in results):,}")
        animated = [r for r in results if r.bones]
        if animated:
            ctx.metric("Skinned", len(animated), tone="good")
            ctx.metric("Clips", sum(r.clips for r in results))
        if written:
            ctx.metric("Written",
                       human_size(sum(_size(p) for p in written)))

        ctx.table(
            "Models",
            ["Model", "Result", "Tris", "Bones", "Clips", "Collision",
             "Mass (kg)", "Surfaceprop", "Time", "Detail"],
            [[r.name, r.status, r.triangles, r.bones or "", r.clips or "",
              _collision(r), _mass(r), r.surfaceprop or "",
              f"{r.elapsed:.2f}s", r.reason or "; ".join(r.warnings)]
             for r in sorted(results, key=lambda r: r.name.lower())],
            numeric_columns=(2, 3, 4, 6),
        )

        if bool(ctx.prefs.get("list_files", True)) and written:
            ctx.table(
                f"Written files ({len(written)})",
                ["File", "Kind", "Size", "Path"],
                [[p.name, _kind(p), human_size(_size(p)), str(p)]
                 for p in sorted(written, key=lambda p: str(p).lower())],
                path_column=3,
            )

        for result in failed:
            ctx.error(f"{result.name}: {result.reason}")

        if bool(ctx.prefs.get("preview_results", True)):
            limit = int(ctx.prefs.get("preview_limit", 6))
            for result in converted[:limit]:
                for path in result.files:
                    if path.suffix.lower() in (".smd", ".qc"):
                        ctx.artifact(path, label=path.name)

        verb = "would convert" if dry else "converted"
        count = len(dry) if dry else len(converted)
        summary = f"{verb} {count}/{len(results)} model(s)"
        if skipped:
            summary += f", {len(skipped)} skipped"
        if failed:
            summary += f", {len(failed)} failed"
        (ctx.error if failed and not converted else ctx.success)(summary)
        ctx.summary(summary)


def _patterns(raw: str) -> list[str]:
    return [part.strip() for part in raw.replace(";", ",").split(",")
            if part.strip()]


def _matches(name: str, patterns: list[str]) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatch(lowered, pattern.lower()) for pattern in patterns)


def _collision(result) -> str:
    if result.physics_fallback:
        return "render mesh"
    if result.has_physics:
        return "physics glTF"
    return "none"


def _mass(result) -> str:
    return f"{result.mass:.1f}" if result.mass else ""


def _kind(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".qc"):
        return "qc"
    if "_anim_" in name:
        return "animation"
    if name.endswith("_physics.smd"):
        return "collision"
    return "mesh"


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
