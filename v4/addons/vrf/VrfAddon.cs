namespace PortingKit.Addons.Vrf;

public sealed class VrfAddon : IAddon
{
    public void Initialize(IAddonContext context)
    {
        context.Register<ITool>("export", new ExportTool());
        context.Register<ITool>("inspect", new InspectTool());
    }

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}

internal static class VrfProcess
{
    public static string Executable(ToolContext context, ToolArguments arguments)
    {
        var path = arguments.Text("executable");
        if (string.IsNullOrWhiteSpace(path))
            path = context.Host.Settings.Get("tools.vrf", "");
        if (!File.Exists(path) || !Path.GetFileNameWithoutExtension(path).Contains("cli", StringComparison.OrdinalIgnoreCase))
            throw new FileNotFoundException("Configure Source2Viewer-CLI in Settings, or select its executable in this tool.");
        return Path.GetFullPath(path);
    }

    public static string Input(ToolArguments arguments)
    {
        var input = Path.GetFullPath(arguments.Text("input"));
        if (!File.Exists(input) && !Directory.Exists(input))
            throw new FileNotFoundException("The input does not exist.", input);
        return input;
    }

    public static async Task RunAsync(ToolContext context, string executable, List<string> arguments,
        CancellationToken cancellationToken)
    {
        var result = await context.Host.Processes.RunAsync(new(executable, arguments),
            line => context.Log(line.Text, line.IsError ? LogLevel.Warning : LogLevel.Info), cancellationToken);
        if (result.ExitCode != 0)
            throw new InvalidOperationException($"Source 2 Viewer exited with code {result.ExitCode}. {string.Join(" ", result.Tail.TakeLast(4))}");
    }
}
