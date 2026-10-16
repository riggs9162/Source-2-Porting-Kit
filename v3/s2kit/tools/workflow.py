"""Guided port planning, preflight, resumable execution and project reports."""

from pathlib import Path

from s2kit.core.jobs import JobCancelled, JobContext
from s2kit.core.params import BoolParam, ChoiceParam, PathParam, TextParam, options
from s2kit.core.registry import tool
from s2kit.pipelines.workflow import WorkflowOptions, run_workflow
from s2kit.ui.tool_page import ToolPage


@tool(id="port-workflow", title="Porting Workflow", category="Models", order=0,
      icon="ACCOUNT_TREE", description="Plan a port, inspect its inputs, and resume a saved batch across conversion stages.",
      keywords=("guided", "preflight", "queue", "resume", "report"))
class PortingWorkflow(ToolPage):
    """Shared tool chrome around the framework-free workflow runner."""

    RUN_LABEL = "Plan / run"
    PARAMS = [
        ChoiceParam("action", "Action", default="inspect", group="Workflow", style="segmented",
                    choices=options(("inspect", "Plan and check"), ("run", "Run / resume"))),
        ChoiceParam("target", "Target", default="sources", group="Workflow",
                    choices=options(("sources", "Source 1 model sources (SMD / QC)"),
                                    ("compiled", "Compiled Source 1 models"),
                                    ("materials", "Source 1 materials (VMT / VTF)")),
                    help="Native skeleton conversion only. For ValveBiped NPCs or playermodels use Direct Port."),
        ChoiceParam("input_kind", "Source type", default="folder", group="Input",
                    choices=options(("folder", "Exported folder"), ("file", "Asset or VPK"))),
        PathParam("source_folder", "Exported source folder", mode="directory", required=True,
                  group="Input", depends_on=("input_kind", "folder")),
        PathParam("source_file", "Source asset or VPK", mode="open_file", required=True,
                  extensions=("glb", "gltf", "vpk", "vmdl_c", "vmat", "vmat_c"),
                  group="Input", depends_on=("input_kind", "file")),
        TextParam("vpk_filter", "Paths inside VPK", group="Input", depends_on=("input_kind", "file"),
                  help="Required for VPKs. Use a model/material path or subtree, as in VRF Batch Export."),
        PathParam("material_source", "VMAT / texture folder (optional)", mode="directory", group="Input",
                  help="Otherwise uses materialsrc inside the source/export. Material outputs are staged for review."),
        PathParam("project", "Workflow project folder", mode="directory", must_exist=False,
                  required=True, group="Output", help="Choose a separate folder outside the source tree. Reuse it to resume the same settings."),
        TextParam("namespace", "Output namespace", default="ports", required=True, group="Output",
                  help="Lowercase folder path used under models and materials/models, for example my_addon/props."),
        BoolParam("collision", "Generate model collision", default=True, group="Conversion"),
        BoolParam("emit_normal", "Ship material normal maps", default=True, group="Conversion",
                  help="Turn off for baked prop_static lighting. Uses the existing material conversion pipeline."),
        PathParam("compiler", "studiomdl executable", mode="open_file", must_exist=False,
                  group="Compile", depends_on=("target", "compiled"),
                  help="Empty uses Preferences. Compiling writes models into the selected game's models folder."),
        PathParam("game", "Compile game folder", mode="directory", must_exist=False,
                  group="Compile", depends_on=("target", "compiled"), help="Empty uses Preferences; gameinfo.txt must exist."),
    ]
    HELP = """### Plan, run and resume

Choose the source, target and a separate project folder, then **Plan and check**.
Output lists the suggested stages and measured preflight findings. Planning
writes reports only. Compiled Source 2 assets need extraction before their mesh,
texture and skeleton checks can run; the plan labels those checks deferred.

**Run / resume** carries extracted files into the existing model and material
converters, then optionally studiomdl. Items run one at a time. Failed items do
not stop independent items, but their dependent compile is blocked. Fix a
transient problem and run again. Cancel pauses at a checkpoint; an interrupted
item restarts rather than continuing halfway through its file.

The project retains `workflow-queue.json`, `port-report.json`, and
`port-report.md`. Completed items are reused only when their output hashes
still match. Changed source files rebuild the queue. Changed workflow settings
require a new project folder. A project cannot run in two sessions at once.

This is native-skeleton conversion, not a ValveBiped retargeting wizard.
Preflight checks resource existence, skeleton structure/weights and compiler
paths. It does not certify animation alignment or in-game collision. Materials
are staged under the project, not installed. The report lists remaining checks.
"""

    def run_job(self, ctx: JobContext) -> None:
        """Run the planned stages without touching controls from the worker."""
        values = ctx.params
        settings = WorkflowOptions(
            source=str(values["source_folder"] if values["input_kind"] == "folder" else values["source_file"]),
            project=str(values["project"]), target=str(values["target"]),
            material_source=str(values["material_source"] or ""), namespace=str(values["namespace"]),
            vpk_filter=str(values["vpk_filter"] or ""),
            vrf_executable=str(ctx.prefs.get("vrf_cli_path", "") or ""),
            compiler=str(values["compiler"] or ctx.prefs.get("studiomdl_path", "") or ""),
            game=str(values["game"] or ctx.prefs.get("gameinfo_path", "") or ""),
            collision=bool(values["collision"]), emit_normal=bool(values["emit_normal"]))
        state = run_workflow(settings, inspect_only=values["action"] == "inspect",
                             log=ctx.log, progress=ctx.progress, cancel=ctx.cancel_event)
        ctx.table("Workflow queue", ["Step", "Stage", "Status", "Issue"],
                  [[key, record["kind"], record["status"], record.get("error", "")]
                   for key, record in state["tasks"].items()])
        ctx.table("Preflight", ["Level", "Asset", "Finding"],
                  [[item["severity"], item["asset"], item["message"]] for item in state["findings"]])
        for name in ("port-report.md", "port-report.json"):
            ctx.artifact(Path(settings.project) / name)
        successful = sum(record["status"] == "success" for record in state["tasks"].values())
        ctx.metric("Completed items", successful)
        ctx.summary(f"{state['status'].title()}: {successful}/{len(state['tasks'])} items complete")
        if state["status"] == "interrupted":
            raise JobCancelled()
        if state["status"] == "incomplete":
            raise RuntimeError("Workflow incomplete; see the port report for failed or blocked items")
