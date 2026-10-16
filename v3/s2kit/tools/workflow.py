"""Guided port planning, preflight, resumable execution and project reports."""

from pathlib import Path

from s2kit.core.jobs import JobCancelled, JobContext
from s2kit.core.params import BoolParam, ChoiceParam, PathParam, TextParam, options
from s2kit.core.registry import tool
from s2kit.pipelines.workflow import WorkflowOptions, run_workflow
from s2kit.ui.tool_page import ToolPage


@tool(id="port-workflow", title="Porting Workflow", category="Models", order=0,
      icon="ACCOUNT_TREE", description="Port Source 2 ragdolls into a complete GMod addon, with optional playermodels.",
      keywords=("ragdoll", "playermodel", "vpk", "guided", "preflight", "queue", "resume", "report"))
class PortingWorkflow(ToolPage):
    """Shared tool chrome around the framework-free workflow runner."""

    RUN_LABEL = "Port"
    PARAMS = [
        ChoiceParam("model_mode", "Port type", default="ragdoll", group="Workflow",
                    choices=options(("ragdoll", "GMod ragdoll addon"), ("generic", "General asset workflow"))),
        ChoiceParam("action", "Action", default="run", group="Workflow", style="segmented",
                    choices=options(("run", "Port / resume"), ("inspect", "Plan and check"))),
        ChoiceParam("target", "Target", default="sources", group="Workflow",
                    depends_on=("model_mode", "generic"),
                    choices=options(("sources", "Source 1 model sources (SMD / QC)"),
                                    ("compiled", "Compiled Source 1 models"),
                                    ("materials", "Source 1 materials (VMT / VTF)")),
                    help="Select the output stage for the general asset workflow."),
        ChoiceParam("input_kind", "Source type", default="urls", group="Input",
                    choices=options(("urls", "Pasted VPK model URLs"), ("folder", "Exported folder"), ("file", "Asset or VPK"))),
        TextParam("source_urls", "Source 2 Viewer model URLs", lines=6, max_lines=12,
                  required=True, group="Input", depends_on=("input_kind", "urls"),
                  hint="vpk:S:/SteamLibrary/.../pak01_dir.vpk:models/characters/model.vmdl_c",
                  help="Paste one model URL per line. Each model is processed independently."),
        PathParam("source_folder", "Exported source folder", mode="directory", required=True,
                  group="Input", depends_on=("input_kind", "folder")),
        PathParam("source_file", "Source asset or VPK", mode="open_file", required=True,
                  extensions=("glb", "gltf", "vpk", "vmdl_c", "vmat", "vmat_c"),
                  group="Input", depends_on=("input_kind", "file")),
        TextParam("vpk_filter", "Paths inside VPK", group="Input", depends_on=("input_kind", "file"),
                  help="Required for VPKs. Use a model/material path or subtree, as in VRF Batch Export."),
        PathParam("material_source", "VMAT / texture folder (optional)", mode="directory", group="Input",
                  help="Otherwise uses materialsrc from the selected models or VPK extraction."),
        PathParam("project", "Workflow project folder", mode="directory", must_exist=False,
                  required=True, group="Output", help="Choose a separate folder outside the source tree. Reuse it to resume the same settings."),
        TextParam("namespace", "Output namespace", default="ports", required=True, group="Output",
                  help="Lowercase folder path used under models and materials/models, for example my_addon/props."),
        BoolParam("collision", "Generate model collision", default=True, group="Conversion",
                  depends_on=("model_mode", "generic")),
        BoolParam("include_source_animations", "Include original Alyx animations", default=False,
                  group="Conversion", depends_on=("model_mode", "ragdoll")),
        BoolParam("create_playermodels", "Also create humanoid playermodels", default=False,
                  group="Conversion", depends_on=("model_mode", "ragdoll"),
                  help="Keep original body proportions while retargeting the standard GMod player animation set."),
        BoolParam("emit_normal", "Ship material normal maps", default=True, group="Conversion",
                  help="Preserve supported source normal maps through the material conversion pipeline."),
        PathParam("compiler", "studiomdl executable", mode="open_file", must_exist=False,
                  group="Compile",
                  help="Empty uses Preferences. Ragdoll addons compile in an isolated project folder."),
        PathParam("game", "Compile game folder", mode="directory", must_exist=False,
                  group="Compile", help="Empty uses Preferences; gameinfo.txt must exist."),
        BoolParam("install", "Install completed addon into GMod", default=False,
                  group="Installation", depends_on=("model_mode", "ragdoll")),
        PathParam("gmod_addons", "GMod addons folder", mode="directory",
                  group="Installation", depends_on=("install", True),
                  help="Editable sources and reports remain in the workflow project."),
    ]
    HELP = """### One-click ragdoll addons

Choose **GMod ragdoll addon**, paste Source 2 Viewer model URLs one per line,
and choose a separate project folder and output namespace. Set 32-bit StudioMDL CE
and its game folder here or in Preferences, then click **Port**.

Exact collision output uses the compiler's VPhysics library and a local helper
built with the Windows .NET Framework C# compiler. Enable .NET Framework 4.8 if
it is unavailable. The GMod addon has no helper or compiler runtime dependency.

The project contains editable model sources, original extraction data, detailed
conversion reports, and a ready-to-install **addon** folder. Original animations,
source-proportioned humanoid playermodels, and installation are independent options.

Native ragdolls retain source-sized geometry and source-derived collision.
Reports distinguish preserved values, Source 1 adaptations and generated physics.
The Alyx metrocop choreo model has no collision; its labelled fallback uses
authored AO capsules with generated humanoid joint settings.

Models run serially. One failed model does not stop other ports. Cancel saves
a checkpoint; **Port** resumes intact outputs and retries interrupted stages.
Resume verifies output hashes and converter versions. Use another project folder
when changing the batch settings. The **Plan and check** action writes reports
without running extraction or compilation.

**General asset workflow** retains the existing source/model/material targets.
"""

    def run_job(self, ctx: JobContext) -> None:
        """Run the planned stages without touching controls from the worker."""
        values = ctx.params
        input_kind = values["input_kind"]
        if input_kind == "urls" and values["model_mode"] != "ragdoll":
            raise ValueError("Pasted model URLs require the GMod ragdoll addon port type")
        source = values["source_folder"] if input_kind == "folder" else values["source_file"] if input_kind == "file" else ""
        settings = WorkflowOptions(
            source=str(source),
            project=str(values["project"]),
            target="compiled" if values["model_mode"] == "ragdoll" else str(values["target"]),
            material_source=str(values["material_source"] or ""), namespace=str(values["namespace"]),
            vpk_filter=str(values["vpk_filter"] or ""),
            vrf_executable=str(ctx.prefs.get("vrf_cli_path", "") or ""),
            compiler=str(values["compiler"] or ctx.prefs.get("studiomdl_path", "") or ""),
            game=str(values["game"] or ctx.prefs.get("gameinfo_path", "") or ""),
            collision=bool(values["collision"]), emit_normal=bool(values["emit_normal"]),
            model_mode=str(values["model_mode"]),
            source_urls=str(values["source_urls"]) if input_kind == "urls" else "",
            include_source_animations=bool(values["include_source_animations"]),
            create_playermodels=bool(values["create_playermodels"]),
            install=bool(values["install"]), gmod_addons=str(values["gmod_addons"] or ""))
        state = run_workflow(settings, inspect_only=values["action"] == "inspect",
                             log=ctx.log, progress=ctx.progress, cancel=ctx.cancel_event)
        assets = {asset["key"]: asset["relative"] for asset in state.get("assets", [])}
        ctx.table("Workflow queue", ["Model", "Stage", "Status", "Issue"],
                  [[assets.get(key.partition(":")[2], Path(record.get("source", "")).name or "Addon"),
                    record["kind"], record["status"], record.get("error", "")]
                   for key, record in state["tasks"].items()])
        ctx.table("Preflight", ["Level", "Asset", "Finding"],
                  [[item["severity"], item["asset"], item["message"]] for item in state["findings"]])
        for name in ("port-report.md", "port-report.json"):
            ctx.artifact(Path(settings.project) / name)
        if (Path(settings.project) / "addon-manifest.json").is_file():
            ctx.artifact(Path(settings.project) / "addon-manifest.json")
        successful = sum(record["status"] == "success" for record in state["tasks"].values())
        ctx.metric("Completed items", successful)
        ctx.summary(f"{state['status'].title()}: {successful}/{len(state['tasks'])} items complete")
        if state["status"] == "interrupted":
            raise JobCancelled()
        if state["status"] == "incomplete":
            raise RuntimeError("Workflow incomplete; see the port report for failed or blocked items")
