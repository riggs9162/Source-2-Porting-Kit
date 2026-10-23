using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json;

namespace PortingKit.Addons.Utilities;

public sealed class EnvironmentTool : ITool
{
    public ToolDefinition Definition { get; } = new("Check environment", "Utilities",
        "Reports the runtime, memory, cache, providers, and configured external tools without loading game assets.", []);

    public async Task<ToolResult> RunAsync(ToolContext context, ToolArguments arguments, CancellationToken cancellationToken)
    {
        context.Report(null, "Checking environment");
        using var process = Process.GetCurrentProcess();
        var cache = await context.Host.Cache.StatisticsAsync(cancellationToken);
        var tools = new[] { "vrf", "studiomdl", "ffmpeg" }.ToDictionary(key => key,
            key => context.Host.Settings.Get("tools." + key, ""));
        var data = new
        {
            runtime = RuntimeInformation.FrameworkDescription,
            operatingSystem = RuntimeInformation.OSDescription,
            architecture = RuntimeInformation.ProcessArchitecture.ToString(),
            workingSetBytes = process.WorkingSet64,
            managedHeapBytes = GC.GetTotalMemory(false),
            cache,
            tools,
            assetProviders = context.Host.Contributions.GetAll<IAssetProvider>().Select(provider => provider.Id).ToArray(),
            toolCount = context.Host.Contributions.GetAll<ITool>().Count
        };
        var path = Path.Combine(context.OutputDirectory, "environment.json");
        await using (var file = File.Create(path))
            await JsonSerializer.SerializeAsync(file, data, new JsonSerializerOptions { WriteIndented = true }, cancellationToken);
        foreach (var (name, location) in tools)
            context.Log($"{name}: {(string.IsNullOrEmpty(location) ? "Not configured" : File.Exists(location) ? location : "Missing: " + location)}");
        context.Log($"Working set: {process.WorkingSet64 / 1048576.0:F1} MB", LogLevel.Success);
        return new("Environment report saved", [new(path, "Environment report")], JsonSerializer.SerializeToElement(data));
    }
}
