"""Guided port planning, preflight, resumable execution and project reports."""

from pathlib import Path

from s2kit.core.jobs import JobCancelled, JobContext
from s2kit.pipelines.workflow import WorkflowOptions, run_workflow
from s2kit.definitions.workflow import PortingWorkflow as _PortingWorkflowDefinition


class PortingWorkflow(_PortingWorkflowDefinition):
    """Shared workflow validation and execution for UI, CLI and MCP."""


    def validate(self, values: dict) -> dict[str, str]:
        """Apply the workflow's source, namespace and project invariants before launching."""
        try:
            self._options(values, {}).validate()
        except (ValueError, OSError) as exc:
            return {"project": str(exc)}
        return {}

    @staticmethod
    def _options(values: dict, prefs: dict) -> WorkflowOptions:
        """Build identical options for validation and execution."""
        input_kind = values["input_kind"]
        if input_kind == "urls" and values["model_mode"] != "ragdoll":
            raise ValueError("Pasted model URLs require the GMod ragdoll addon port type")
        source = values["source_folder"] if input_kind == "folder" else values["source_file"] if input_kind == "file" else ""
        return WorkflowOptions(
            source=str(source),
            project=str(values["project"]),
            target="compiled" if values["model_mode"] == "ragdoll" else str(values["target"]),
            material_source=str(values["material_source"] or ""), namespace=str(values["namespace"]),
            vpk_filter=str(values["vpk_filter"] or ""),
            vrf_executable=str(prefs.get("vrf_cli_path", "") or ""),
            compiler=str(values["compiler"] or prefs.get("studiomdl_path", "") or ""),
            game=str(values["game"] or prefs.get("gameinfo_path", "") or ""),
            collision=bool(values["collision"]), emit_normal=bool(values["emit_normal"]),
            model_mode=str(values["model_mode"]),
            source_urls=str(values["source_urls"]) if input_kind == "urls" else "",
            include_source_animations=bool(values["include_source_animations"]),
            create_playermodels=bool(values["create_playermodels"]),
            install=bool(values["install"]), gmod_addons=str(values["gmod_addons"] or ""))

    def run_job(self, ctx: JobContext) -> None:
        """Run the planned stages without touching controls from the worker."""
        values = ctx.params
        settings = self._options(values, ctx.prefs)
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
