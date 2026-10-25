"""Shared workflow schema and metadata."""
from __future__ import annotations

from s2kit.core.registry import tool
from s2kit.core.tool_definition import ToolDefinition
from s2kit.core.params import BoolParam, ChoiceParam, PathParam, TextParam, options


@tool(id="port-workflow", title="Porting Workflow", category="Models", order=0,
      icon="ACCOUNT_TREE", description="Port Source 2 props and ragdolls into a complete GMod addon, with optional playermodels.",
      keywords=("prop", "static", "dynamic", "ragdoll", "playermodel", "vpk", "guided", "preflight",
                "queue", "resume", "report"))
class PortingWorkflow(ToolDefinition):
    """Shared declaration for PortingWorkflow."""
    EXECUTOR = "s2kit.tools.workflow:PortingWorkflow"
    CUSTOM_VALIDATION = True
    RUN_LABEL = "Port"
    PARAMS = [
        ChoiceParam("model_mode", "Port type", default="ragdoll", group="Workflow",
                    choices=options(("ragdoll", "GMod model addon"), ("generic", "General asset workflow"))),
        ChoiceParam("model_type", "Model type", default="auto", group="Workflow",
                    depends_on=("model_mode", "ragdoll"),
                    choices=options(("auto", "Detect for each model"), ("prop", "Props"), ("ragdoll", "Ragdolls")),
                    help="Detection ports jointed characters as ragdolls and everything else as static or animated props."),
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
        BoolParam("include_source_animations", "Include original Alyx animations on ragdolls", default=False,
                  group="Conversion", depends_on=("model_mode", "ragdoll"),
                  help="Props always keep their authored animations."),
        BoolParam("create_playermodels", "Also create humanoid playermodels", default=False,
                  group="Conversion", depends_on=("model_mode", "ragdoll"),
                  help="Keep original body proportions while retargeting the standard GMod player animation set. "
                       "Props are skipped."),
        BoolParam("emit_normal", "Ship material normal maps", default=True, group="Conversion",
                  help="Preserve supported source normal maps through the material conversion pipeline."),
        PathParam("compiler", "studiomdl executable", mode="open_file", must_exist=False,
                  group="Compile",
                  help="Empty uses Preferences. Model addons compile in an isolated project folder."),
        PathParam("game", "Compile game folder", mode="directory", must_exist=False,
                  group="Compile", help="Empty uses Preferences; gameinfo.txt must exist."),
        BoolParam("install", "Install completed addon into GMod", default=False,
                  group="Installation", depends_on=("model_mode", "ragdoll")),
        PathParam("gmod_addons", "GMod addons folder", mode="directory",
                  group="Installation", depends_on=("install", True),
                  help="Editable sources and reports remain in the workflow project."),
    ]
    HELP = """### One-click model addons

Choose **GMod model addon**, paste Source 2 Viewer model URLs one per line,
and choose a separate project folder and output namespace. Set 32-bit StudioMDL CE
and its game folder here or in Preferences, then click **Port**.

**Model type** decides how each model is built. **Detect for each model** ports
skinned models whose physics joins bodies with joints, and characters with
authored AO capsules, as ragdolls. Every other model becomes a prop:

* An unskinned model becomes a static prop with its source collision, authored
  mass, skins and lower LOD meshes.
* A skinned model becomes an animated prop with its native skeleton, every
  authored clip, attachments, hitboxes and bone-following collision.

Source bodygroups on props are combined into one body; the report says so.

Exact ragdoll collision output uses the compiler's VPhysics library and a local helper
built with the Windows .NET Framework C# compiler. Enable .NET Framework 4.8 if
it is unavailable. The GMod addon has no helper or compiler runtime dependency.

The project contains editable model sources, original extraction data, detailed
conversion reports, and a ready-to-install **addon** folder. Original ragdoll animations,
source-proportioned humanoid playermodels, and installation are independent options.
Playermodels are built only from ragdolls.

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
