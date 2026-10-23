namespace PortingKit.Addons.Vrf;

public sealed class InspectTool : ITool
{
    public ToolDefinition Definition { get; } = new("Inspect resource block", "Assets",
        "Saves a selected resource block as text without importing its geometry into the desktop app.",
        [
            new("input", "Compiled resource", ParameterKind.File, Required: true),
            new("block", "Resource block", ParameterKind.Choice, "DATA", Choices: ["DATA", "RERL", "PHYS", "REDI", "NTRO"]),
            new("executable", "Source2Viewer-CLI override", ParameterKind.File)
        ]);

    public async Task<ToolResult> RunAsync(ToolContext context, ToolArguments arguments, CancellationToken cancellationToken)
    {
        var executable = VrfProcess.Executable(context, arguments);
        var input = VrfProcess.Input(arguments);
        if (Directory.Exists(input) || Path.GetExtension(input).Equals(".vpk", StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException("Select a loose compiled resource file for block inspection.");
        var path = Path.Combine(context.OutputDirectory, arguments.Text("block").ToLowerInvariant() + ".txt");
        using var writer = new StreamWriter(path);
        var result = await context.Host.Processes.RunAsync(new(executable, ["-i", input, "-b", arguments.Text("block")]),
            line => writer.WriteLine(line.Text), cancellationToken);
        if (result.ExitCode != 0)
            throw new InvalidOperationException($"VRF exited with code {result.ExitCode}.");
        return new("Resource block saved", [new(path, "Resource block")]);
    }
}
