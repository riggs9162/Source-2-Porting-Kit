"""
Model Porter.

Convert a skinned Source 2 GLB with its native skeleton and animations, or
adapt a character to ValveBiped and an HL2 animation library.

The conversions live in :mod:`s2kit.pipelines.native_port` and
:mod:`s2kit.pipelines.direct_port`; this file defines parameters and reporting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from s2kit.core.jobs import JobCancelled, JobContext
from s2kit.core.params import (
    BoolParam, ChoiceParam, FloatParam, IntParam, PathParam, TextParam, options,
)
from s2kit.core.registry import tool
from s2kit.media.probe import human_size
from s2kit.ui.tool_page import ToolPage

#: The stock GMod animation libraries, and what each one is for. "custom" lets
#: the donor field point anywhere.
DONOR_PRESETS = options(
    ("custom", "Custom (use the donor field)"),
    ("m_anm.mdl", "m_anm.mdl - male citizen"),
    ("f_anm.mdl", "f_anm.mdl - female citizen"),
    ("z_anm.mdl", "z_anm.mdl - zombie"),
    ("police_animations.mdl", "police_animations.mdl - metropolice"),
)


@tool(
    id="direct-port",
    title="Model Porter",
    category="Models",
    icon="ACCESSIBILITY_NEW",
    description="Preserve a model's native bones and animations, or convert a character to ValveBiped.",
    keywords=("valvebiped", "playermodel", "npc", "pm", "character", "bones",
              "retarget", "donor", "includemodel", "proportions"),
    order=30,
)
class DirectPort(ToolPage):
    """Write native model sources or a character adapted to ValveBiped."""

    RUN_LABEL = "Port"
    RUN_ICON = "ACCESSIBILITY_NEW"

    PARAMS = [
        ChoiceParam("port_mode", "Port type", default="native",
                    choices=options(("native", "Native model and animations"),
                                    ("valvebiped", "ValveBiped character")), group="Input"),
        PathParam("glb", "Source 2 model (.glb)", mode="open_file", required=True,
                  group="Input", extensions=("glb", "gltf"),
                  help="The skinned render GLB from VRF Batch Export. Keep its "
                       "metadata and physics companions beside it for native mode. "
                       "ValveBiped mode does not import source collision."),
        ChoiceParam("donor_preset", "Animation library", default="m_anm.mdl",
                    depends_on=("port_mode", "valvebiped"),
                    choices=DONOR_PRESETS, group="Input",
                    help="The HL2 animation library for the intended NPC or "
                         "playermodel. Select its matching donor .mdl below."),
        PathParam("donor", "Donor .mdl", mode="open_file", required=False,
                  depends_on=("port_mode", "valvebiped"),
                  group="Input", extensions=("mdl",),
                  help="The compiled animation library supplies the bind and "
                       "bone axes. Its animations and pose parameters are "
                       "inherited through $includemodel."),

        PathParam("output", "Output folder", mode="directory", required=True,
                  must_exist=False, group="Output"),
        TextParam("modelname", "$modelname", default="", required=True,
                  group="Output", hint="myaddon/characters/grunt.mdl"),
        TextParam("cdmaterials", "$cdmaterials", default="models/",
                  group="Output", hint="models/myaddon/characters/"),
        TextParam("surfaceprop", "$surfaceprop", default="metal",
                  group="Output"),
        PathParam("metadata", "Authored metadata (.metadata.json)",
                  required=False, group="Native model", extensions=("json",),
                  depends_on=("port_mode", "native"),
                  help="Empty uses the sidecar written beside the GLB by VRF Batch Export."),
        BoolParam("dynamic_prop", "Dynamic prop only", default=False,
                  group="Native model", depends_on=("port_mode", "native"),
                  help="Keep authored animations and supported collision without a "
                       "generated ragdoll sequence or ragdoll companion."),
        TextParam("ragdoll_bones", "Generated ragdoll bones", group="Native model",
                  depends_on=("port_mode", "native"),
                  help="Optional comma-separated bone names, including the skeleton root. "
                       "Writes a separate adapted ragdoll; empty preserves supported rigid or bone-driven source collision. "
                       "Leave empty when Dynamic prop only is enabled."),
        FloatParam("ragdoll_mass", "Generated ragdoll mass (kg)", default=25.0,
                   min=1, max=10000, group="Native model", depends_on=("port_mode", "native")),
        FloatParam("ragdoll_limit", "Generated joint limit (degrees)", default=25.0,
                   min=0, max=180, group="Native model", depends_on=("port_mode", "native")),

        ChoiceParam("align_yaw", "Face alignment", default="auto",
                    depends_on=("port_mode", "valvebiped"),
                    choices=options(("auto", "Measure against the donor"),
                                    ("0", "0 (already aligned)"),
                                    ("90", "+90 degrees"),
                                    ("-90", "-90 degrees"),
                                    ("180", "180 degrees")),
                    group="Geometry",
                    help="A Source 2 rig faces a quarter turn away from "
                         "ValveBiped, so the skeleton and the mesh are turned "
                         "together to match the donor. Auto measures the angle "
                         "from the shared bones and is almost always right; "
                         "override only if a rig defeats it."),
        FloatParam("scale", "Scale", default=1.0, min=0.001, max=10000.0,
                   step=1.0, decimals=4, group="Geometry", advanced=True,
                   help="ValveBiped mode only: scales glTFs without Source units. "
                        "Exports already in Source units ignore this value; "
                        "baked-metre Source 2 Viewer exports are unsupported in "
                        "ValveBiped mode. Native mode converts known exporter "
                        "units automatically, including baked metres."),

        BoolParam("compile", "Compile with studiomdl", default=False,
                  group="Compile"),
        PathParam("studiomdl", "studiomdl.exe", mode="open_file", required=False,
                  must_exist=False, group="Compile",
                  depends_on=("compile", True),
                  help="Empty uses the path from Preferences."),
        PathParam("gameinfo", "Game folder", mode="directory",
                  required=False, must_exist=False, group="Compile",
                  depends_on=("compile", True),
                  help="Choose the folder containing gameinfo.txt, or paste its file path. "
                       "Empty uses the path from Preferences."),
    ]

    PREFERENCES = [
        IntParam("max_bones", "Compiled bone limit", default=128, min=32,
                 max=256, group="Limits",
                 help="ValveBiped mode only: auxiliary leaves fold away to fit "
                      "this compiler limit; ValveBiped bones are retained. "
                      "Native mode keeps every bone, accepts at most 256 and "
                      "requires a compiler compatible with the skeleton size."),
        BoolParam("preview_results", "Add written files to Preview",
                  default=True, group="Report"),
        BoolParam("list_files", "List every written file in a table",
                  default=True, group="Report"),
    ]

    HELP = """
### Model Porter

**Native model and animations** writes the Source 2 skeleton, vertex normals,
UVs, weights and every exported clip. Run VRF Batch Export first and select its
skinned render GLB. Native mode converts known Source 2 Viewer units automatically,
including baked metres; it needs no donor and does not prune bones.

Keep the metadata and physics companions beside the GLB. The metadata sidecar
carries authored frame rates, loop flags, bone masks, attachments and hitboxes.
Authored pose-parameter ranges are declared in QC; their sequence blend graphs
still require manual authoring.

Native mode converts rigid collision from the physics GLB, or up to 32 bone-driven
convex parts directly from the PHYS sidecar. Multiple hulls on each bone remain
separate. Thin hulls receive documented thickness repairs. Animated collision
requires a prop_dynamic with bone followers. This kinematic collision is not a
ragdoll. No explicit constraints are invented; compiler zero-limit defaults apply
only where a physical parent survives compilation.
Mass is calculated using Source 1 surface properties. Rubikon solver behavior,
free physics joints, additive clips and unsupported scaling are not translated.
Source collision is not generated when no physics companion exists. Model-space
attachments receive a stationary compatibility bone within the 256-bone limit.
Animation events, animation graphs,
particles and branch-specific shaders require separate translation; metadata
warnings appear in the report.

An optional `<model>.lods.json` beside the GLB lists authored DMX LOD sources as
`[{"distance":14,"source":"scanner_lod1.dmx"}]`. Paths resolve beside that file.
Only single-mesh Source 2 Viewer DMX exports in original Source units are supported.
The source LOD meshes are exported with corresponding partitions for large
meshes. StudioMDL can remove degenerate faces and weld nearby LOD vertices.
Source 1's distance-based switching differs from Source 2's screen-coverage
thresholds and should be tuned for each asset.

**Generated ragdoll bones** creates a separate `_ragdoll.mdl` with convex bodies
fitted to selected bone weights. Include the root and 1 to 31 other bones. Its
mass and angular limits are user controlled. This is an explicit physics
adaptation. The main model retains its converted source collision, if available.

**Dynamic prop only** keeps the authored animations and supported collision,
without adding a ragdoll bind sequence or companion. Leave Generated ragdoll
bones empty when using this option. Bone-driven collision still uses
prop_dynamic bone followers.

**ValveBiped character** uses the selected donor library's exact bind and a
single global mesh alignment. It retains required auxiliary bones and writes
procedural twist helpers. It uses the library's proportions and animation set.

Compile with StudioMDL CE or a compatible compiler. The compiler output is checked
for matching MDL, VVD, VTX and PHY checksums. Validate the final result in GMod.
    """

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
