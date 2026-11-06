using System.Globalization;
using System.Text.Json;

namespace PortingKit.Addons.Porting;

/// <summary>Turns Source 2 models into a finished, verified Source 1 addon.</summary>
public sealed class ModelPortTool : ITool
{
    private static readonly string[] Resolutions = ["512", "1024", "2048", "4096", "original"];

    public ToolDefinition Definition { get; } = new("Model port", "Models",
        "Ports Source 2 models into a compiled Source 1 addon: props and ragdolls with their textures, skins, LODs, animations and physics. Stages are checkpointed: running a project again resumes it, adds any new models to it, and rebuilds only what changed settings affect.",
        [
            new("sources", "Models", ParameterKind.Lines, Required: true, Description: "One per line: vpk:<vpk path>:<resource>.vmdl_c, a loose .vmdl_c, an exported .glb, or an exported folder."),
            new("project", "Project folder", ParameterKind.Directory, Required: true, Description: "Keeps editable sources, reports and the finished addon. It must be outside the input folders."),
            new("namespace", "Namespace", ParameterKind.Text, "ports", Description: "Folder inside models and materials, for example my_addon/props."),
            new("model_type", "Model type", ParameterKind.Choice, "auto", Description: "Auto makes jointed characters ragdolls and everything else a prop.", Choices: ["auto", "prop", "ragdoll"]),
            new("include_animations", "Include source animations", ParameterKind.Boolean, "true"),
            new("resolution", "Maximum texture size", ParameterKind.Choice, "original", Choices: Resolutions),
            new("emit_normal", "Ship normal maps", ParameterKind.Boolean, "true"),
            new("static_variant", "Also build a static prop", ParameterKind.Boolean, "false", Description: "Adds a separate <name>_static model for models with no physics or one rigid body. It compiles as $staticprop, bakes normal detail into its textures, and uses its own material folder so it keeps VRAD's baked lighting."),
            new("static_resolution", "Static prop maximum texture size", ParameterKind.Choice, "original", Choices: Resolutions),
            new("create_playermodels", "Also build a playermodel", ParameterKind.Boolean, "false", Description: "Adds a separate <name>_pm model for humanoid characters, on the ValveBiped skeleton with Garry's Mod's player animations, and lists it in the player model menu. The animation sources download once, the first time this runs."),
            new("loop_animations", "Loop animations", ParameterKind.Lines, Description: "Optional. Sequence names to play on a loop whatever the source says, one per line. * loops every sequence."),
            new("material_source", "Material folder", ParameterKind.Directory, Description: "Optional. VMATs and textures for models that were exported separately."),
            new("compiler", "studiomdl", ParameterKind.File, Description: "Leave empty to use the tools.studiomdl setting."),
            new("game", "Compiler game folder", ParameterKind.Directory, Description: "The folder holding gameinfo.txt. Leave empty to look beside studiomdl."),
            new("vrf", "Source2Viewer-CLI", ParameterKind.File, Description: "Leave empty to use the tools.vrf setting, then PATH."),
            new("install", "Install into Garry's Mod", ParameterKind.Boolean, "false"),
            new("gmod_addons", "Garry's Mod addons folder", ParameterKind.Directory, Description: "Required when installing."),
            new("plan_only", "Only plan", ParameterKind.Boolean, "false", Description: "Writes the stage plan and report without converting anything.")
        ]);

    private static int Resolution(string value) => value == "original" ? 0 : int.Parse(value, CultureInfo.InvariantCulture);

    private static string Compiler(ToolContext context, ToolArguments arguments)
    {
        var compiler = arguments.Text("compiler");
        if (compiler.Length == 0) compiler = context.Host.Settings.Get("tools.studiomdl", "");
        return File.Exists(compiler) ? Path.GetFullPath(compiler) : throw new InvalidDataException("Set an existing studiomdl executable, here or in the tools.studiomdl setting.");
    }

    /// <summary>A StudioMDL CE install keeps its game folder beside <c>bin</c>, so the setting can usually be left empty.</summary>
    private static string Game(string compiler, ToolArguments arguments)
    {
        var game = arguments.Text("game");
        var install = Path.GetDirectoryName(Path.GetDirectoryName(compiler));
        var resolved = game.Length > 0 ? Studiomdl.ResolveGameArgument(game) : install is null ? null : Studiomdl.ResolveGameDirectory(install);
        return resolved ?? throw new InvalidDataException("Set a compiler game folder containing gameinfo.txt.");
    }

    public async Task<ToolResult> RunAsync(ToolContext context, ToolArguments arguments, CancellationToken cancellationToken)
    {
        var compiler = Compiler(context, arguments);
        var options = new WorkflowOptions
        {
            Sources = arguments.Text("sources"), Project = arguments.Text("project"), Namespace = Resources.NormalizeNamespace(arguments.Text("namespace")),
            ModelType = arguments.Text("model_type"), MaterialSource = arguments.Text("material_source"), VrfExecutable = arguments.Text("vrf"), Compiler = compiler,
            Game = Game(compiler, arguments), IncludeSourceAnimations = arguments.Boolean("include_animations"), EmitNormal = arguments.Boolean("emit_normal"),
            Resolution = Resolution(arguments.Text("resolution")), StaticVariant = arguments.Boolean("static_variant"), StaticResolution = Resolution(arguments.Text("static_resolution")),
            CreatePlayermodels = arguments.Boolean("create_playermodels"), AnimationCache = Path.Combine(context.Host.StateDirectory, "addons", "porting", "player_animations"),
            Install = arguments.Boolean("install"), GmodAddons = arguments.Text("gmod_addons"),
            LoopAnimations = arguments.Text("loop_animations")
        };
        var port = new PortContext(context.Host.Processes, (message, level) => context.Log(message, level), cancellationToken);
        var state = await PortWorkflow.RunAsync(options, new NativeStages(context.Host.Settings), arguments.Boolean("plan_only"), port,
            (done, total) => context.Report((double)done / total, $"Finished {done} of {total} stages"));
        var project = WorkflowPaths.Project(options);
        var complete = state.Tasks.Values.Count(record => record.Status is "success" or "skipped");
        var summary = $"{char.ToUpperInvariant(state.Status[0])}{state.Status[1..]}: {complete}/{state.Tasks.Count} stages complete";
        if (state.Status == "interrupted") cancellationToken.ThrowIfCancellationRequested();
        if (state.Status == "incomplete") throw new InvalidDataException($"{summary}. See {Path.Combine(project, "port-report.md")} for the failed or blocked items.");
        var artifacts = new List<Artifact> { new(Path.Combine(project, "port-report.md"), "Port report"), new(Path.Combine(project, "port-report.json"), "Port report data") };
        var addon = Path.Combine(project, "addon");
        if (Directory.Exists(addon)) artifacts.Add(new(addon, "Finished addon", "directory"));
        return new(summary, artifacts, JsonSerializer.SerializeToElement(new { state.Status, stages = state.Tasks.Count, complete }));
    }
}
