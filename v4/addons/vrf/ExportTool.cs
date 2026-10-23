namespace PortingKit.Addons.Vrf;

public sealed class ExportTool : ITool
{
    public ToolDefinition Definition { get; } = new("Export Source 2 assets", "Assets",
        "Uses Source2Viewer-CLI to export selected resources. Each job writes to a fresh output directory.",
        [
            new("input", "Input file, folder, or VPK", ParameterKind.File, Required: true),
            new("filter", "Internal VPK path filter", Description: "For example: models/props_junk/popcan01a.vmdl_c"),
            new("format", "Model format", ParameterKind.Choice, "gltf", Choices: ["gltf", "glb", "source"]),
            new("animations", "Include model animations", ParameterKind.Boolean, "false"),
            new("threads", "Export workers", ParameterKind.Integer, "1", Minimum: 1, Maximum: 4),
            new("executable", "Source2Viewer-CLI override", ParameterKind.File)
        ]);

    public async Task<ToolResult> RunAsync(ToolContext context, ToolArguments arguments, CancellationToken cancellationToken)
    {
        var executable = VrfProcess.Executable(context, arguments);
        var input = VrfProcess.Input(arguments);
        if (Path.GetExtension(input).Equals(".vpk", StringComparison.OrdinalIgnoreCase) && string.IsNullOrWhiteSpace(arguments.Text("filter")))
            throw new ArgumentException("Select an internal VPK path or folder before exporting an archive.");
        var output = Path.Combine(context.OutputDirectory, "export");
        Directory.CreateDirectory(output);
        var args = new List<string> { "-i", input, "-o", output, "-d", "--threads", arguments.Text("threads") };
        if (!string.IsNullOrWhiteSpace(arguments.Text("filter")))
            args.AddRange(["-f", arguments.Text("filter")]);
        if (Directory.Exists(input))
            args.Add("--recursive");
        if (arguments.Text("format") != "source")
            args.AddRange(["--gltf_export_format", arguments.Text("format"), "--gltf_export_materials", "--gltf_export_extras"]);
        if (arguments.Boolean("animations"))
            args.Add("--gltf_export_animations");
        context.Report(null, "Exporting selected resources");
        await VrfProcess.RunAsync(context, executable, args, cancellationToken);
        if (!Directory.EnumerateFiles(output, "*", SearchOption.AllDirectories).Any())
            throw new InvalidOperationException("VRF returned without writing files. Check the selected path and job log.");
        return new("Source 2 export completed", [new(output, "Exported assets", "directory")]);
    }
}
