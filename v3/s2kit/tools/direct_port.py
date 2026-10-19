"""
Model Porter.

Convert a skinned Source 2 GLB with its native skeleton and animations, or
adapt a character to ValveBiped and an HL2 animation library.

The conversions live in :mod:`s2kit.pipelines.native_port` and
:mod:`s2kit.pipelines.direct_port`; this file defines parameters and reporting.
"""

from __future__ import annotations

from s2kit.definitions.direct_port import DONOR_PRESETS

from pathlib import Path
from typing import Any, Optional

from s2kit.core.jobs import JobCancelled, JobContext
from s2kit.media.probe import human_size
from s2kit.definitions.direct_port import DirectPort as _DirectPortDefinition

#: The stock GMod animation libraries, and what each one is for. "custom" lets
#: the donor field point anywhere.


class DirectPort(_DirectPortDefinition):
    """Write native model sources or a character adapted to ValveBiped."""


    def validate(self, values: dict[str, Any]) -> dict[str, str]:
        """Require a donor only for the ValveBiped conversion path."""
        errors = {}
        if not str(values.get("modelname", "")).strip():
            errors["modelname"] = "$modelname is required"
        if values.get("port_mode", "native") == "native":
            if values.get("dynamic_prop") and str(values.get("ragdoll_bones") or "").strip():
                errors["ragdoll_bones"] = "Clear generated ragdoll bones when Dynamic prop only is enabled"
            return errors
        donor = str(values.get("donor") or "").strip()
        preset = str(values.get("donor_preset", "custom"))
        if not donor or not Path(donor).is_file():
            errors["donor"] = "A donor .mdl file is required"
        elif preset != "custom" and Path(donor).name.lower() != preset.lower():
            errors["donor"] = f"Choose {preset} or change the library to Custom"
        return errors

    def run_job(self, ctx: JobContext) -> None:
        """Dispatch the selected native or ValveBiped model conversion."""
        from s2kit.pipelines import direct_port as pipeline
        from s2kit.pipelines.porting import PortCancelled

        try:
            if ctx.params.get("port_mode", "native") == "native":
                self._run_native(ctx)
                return
            params = ctx.params
            compiler, game = self._compile_paths(ctx)
            result = pipeline.port_model(
                Path(params["glb"]), Path(params["output"]), Path(params["donor"]),
                modelname=self._modelname(params), cdmaterials=str(params["cdmaterials"]),
                surfaceprop=str(params["surfaceprop"]), align_yaw=_align_yaw(params["align_yaw"]),
                scale=float(params["scale"]), max_bones=int(ctx.prefs.get("max_bones", 128)),
                studiomdl=compiler, game=game, on_log=ctx.log, cancel=ctx.cancel_event)
            self._report(ctx, result)
        except PortCancelled as exc:
            raise JobCancelled() from exc

    @staticmethod
    def _modelname(params: dict) -> str:
        """Normalize the requested model path's extension."""
        name = str(params["modelname"]).strip()
        return name if name.lower().endswith(".mdl") else name + ".mdl"

    def _run_native(self, ctx: JobContext) -> None:
        """Run the original-skeleton path without a ValveBiped donor."""
        from s2kit.pipelines.native_port import port_native_model

        params = ctx.params
        compiler, game = self._compile_paths(ctx)
        metadata = str(params.get("metadata") or "").strip()
        bones = [name.strip() for name in str(params.get("ragdoll_bones") or "").split(",") if name.strip()]
        result = port_native_model(
            Path(params["glb"]), Path(params["output"]), modelname=self._modelname(params),
            cdmaterials=str(params["cdmaterials"]), surfaceprop=str(params["surfaceprop"]),
            metadata_path=Path(metadata) if metadata else None,
            dynamic_prop=bool(params.get("dynamic_prop", False)),
            ragdoll_bones=bones or None, ragdoll_mass=float(params.get("ragdoll_mass", 25)),
            ragdoll_limit=float(params.get("ragdoll_limit", 25)),
            studiomdl=compiler, game=game, on_log=ctx.log, cancel=ctx.cancel_event)
        for label, value in (("Bones", result.bones), ("Animations", result.sequences),
                             ("Attachments", result.attachments), ("Hitboxes", result.hitboxes)):
            ctx.metric(label, value)
        if result.compatibility_bones:
            ctx.metric("Compatibility bones", len(result.compatibility_bones))
        if result.physics:
            ctx.metric("Collision bodies", result.physics["part_count"])
            ctx.metric("Convex hulls", result.physics["hull_count"])
        for warning in result.warnings:
            ctx.warn(warning)
        self._report_files(ctx, result.files, result.qc)
        if result.ragdoll_qc:
            ctx.artifact(result.ragdoll_qc)
        summary = f"{result.bones} native bones, {result.sequences} authored animations"
        if result.dynamic_prop:
            summary = "Dynamic prop: " + summary
        ctx.success(summary)
        ctx.summary(summary)

    def _compile_paths(self, ctx: JobContext):
        """Resolve the compiler and game directory from form values or preferences."""
        from s2kit.pipelines.studiomdl import resolve_game_arg

        if not bool(ctx.params.get("compile")):
            return None, None
        compiler = str(ctx.params.get("studiomdl") or ctx.prefs.get("studiomdl_path") or "").strip()
        if not compiler or not Path(compiler).is_file():
            raise ValueError("Compile is on but studiomdl.exe was not found; set it here or in Preferences")
        game = str(ctx.params.get("gameinfo") or ctx.prefs.get("gameinfo_path") or "").strip()
        resolved = resolve_game_arg(game)
        if resolved is None:
            raise ValueError("Compile is on but the game folder must contain gameinfo.txt")
        return Path(compiler), resolved

    def _report_files(self, ctx: JobContext, files: list[Path], qc: Path) -> None:
        """Expose reproducible sources through the common Output and Preview views."""
        if files:
            ctx.metric("Written", human_size(sum(_size(path) for path in files)))
        if files and bool(ctx.prefs.get("list_files", True)):
            ctx.table("Written files", ["File", "Kind", "Size", "Path"],
                      [[path.name, _kind(path), human_size(_size(path)), str(path)]
                       for path in sorted(files)], path_column=3)
        if bool(ctx.prefs.get("preview_results", True)):
            ctx.artifact(qc)

    def _report(self, ctx: JobContext, result) -> None:
        """Report donor retargeting outcomes and compiler failures."""
        for label, value in (("Bones", result.bones), ("ValveBiped", result.canonical),
                             ("Auxiliary", result.auxiliary), ("Pruned", result.discarded),
                             ("Bodygroups", len(result.bodygroups)),
                             ("Weights merged", result.merged_vertices)):
            ctx.metric(label, value)
        if result.alignment_yaw:
            ctx.metric("Turned", f"{result.alignment_yaw:+.0f}deg")
        if result.poseparameters:
            ctx.metric("Pose params", len(result.poseparameters))
        if result.unmatched_donor_bones:
            ctx.warn("Donor bones without a counterpart: " + ", ".join(result.unmatched_donor_bones))
        self._report_files(ctx, result.files, result.qc)
        summary = f"{result.canonical} ValveBiped + {result.auxiliary} auxiliary bones, {len(result.bodygroups)} bodygroups"
        failed = bool(ctx.params.get("compile")) and not result.compiled
        if bool(ctx.params.get("compile")):
            summary += "; COMPILE FAILED" if failed else "; compiled"
        (ctx.error if failed else ctx.success)(summary)
        ctx.summary(summary)


def _align_yaw(value: Any) -> Optional[float]:
    """Resolve automatic facing or an explicit angle."""
    text = str(value or "auto").strip().lower()
    if text in ("", "auto"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _kind(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".qc"):
        return "qc"
    if name.startswith("ref_"):
        return "mesh"
    if name.endswith("pose.smd"):
        return "pose"
    return "smd"


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
