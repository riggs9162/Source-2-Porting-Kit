using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace PortingKit.Core.Automation;

public sealed record AgentSettingsChanged(string Key, string? AddonId);

/// <summary>Shares backend operations across local agents and the desktop-owned host.</summary>
public sealed class AgentApi(KitHost host)
{
    private readonly SemaphoreSlim settingsGate = new(1);
    public AgentCatalog Catalog { get; } = new(host);

    public async Task<object?> InvokeAsync(string operation, JsonElement input, CancellationToken cancellationToken)
    {
        if (operation == "operations") return Catalog.Operations();
        var definition = Catalog.Operations().FirstOrDefault(item => item.Name == operation)
            ?? throw new AgentException("unknown_operation", $"Unknown operation: {operation}");
        AgentSchema.Validate(definition.InputSchema, input);
        var fields = new Inputs(input);
        switch (operation)
        {
            case "status":
                using (var process = Process.GetCurrentProcess())
                    return new { name = "Source Backport", apiVersion = 1, hostKind = host.HostKind, processId = Environment.ProcessId,
                        root = host.RootDirectory, state = host.StateDirectory, memoryBytes = process.WorkingSet64,
                        activeJobs = host.Jobs.Snapshot().Count(job => JobQueue.IsActive(job.Status)),
                        loadedAddons = host.Addons.Snapshot().Count(addon => addon.State == AddonState.Loaded),
                        effectiveRuntime = host.Options, configuredRuntime = host.Settings.Get("runtime", new HostOptions()),
                        logWriteError = host.Log.WriteError };
            case "tools.list":
                return host.Contributions.GetAll<ITool>().Select(item => new { item.Id, item.AddonId, item.Value.Definition,
                    agentOperation = AgentCatalog.ForTool(item.Id, item.Value.Definition).Name });
            case "tools.describe":
                var toolId = fields.Text("tool_id");
                var tool = host.Contributions.Get<ITool>(toolId);
                return new { id = toolId, tool.Definition, operation = AgentCatalog.ForTool(toolId, tool.Definition) };
            case "tools.validate":
                return new { arguments = ToolValidation.Normalize(host.Contributions.Get<ITool>(fields.Text("tool_id")).Definition,
                    AgentSchema.ToolValues(input.GetProperty("arguments"))) };
            case "jobs.submit":
                return await host.Jobs.SubmitAsync(fields.Text("tool_id"), AgentSchema.ToolValues(input.GetProperty("arguments")), cancellationToken);
            case "jobs.list":
                var all = host.Jobs.Snapshot();
                var offset = fields.Int("offset", 0);
                var count = fields.Int("count", 50);
                return new { items = all.Skip(offset).Take(count), offset, hasMore = (long)offset + count < all.Count };
            case "jobs.get": return Job(fields.Text("job_id"));
            case "jobs.cancel":
                var cancelled = Job(fields.Text("job_id"));
                return new { requested = host.Jobs.Cancel(cancelled.Id), job = Job(cancelled.Id.ToString()) };
            case "jobs.wait":
                var clock = Stopwatch.StartNew();
                var duration = TimeSpan.FromSeconds(fields.Int("wait_seconds", 20));
                while (true)
                {
                    var current = Job(fields.Text("job_id"));
                    if (!JobQueue.IsActive(current.Status) || clock.Elapsed >= duration)
                        return new { completed = !JobQueue.IsActive(current.Status), job = current };
                    await Task.Delay(100, cancellationToken);
                }
            case "logs.tail":
                var source = fields.Text("job_id") is { Length: > 0 } id ? Job(id).Id.ToString("N")[..8] : null;
                return new { items = host.Log.Tail(500).Where(entry => source is null || entry.Source == source)
                    .TakeLast(fields.Int("count", 100)), scope = "live tail since backend start" };
            case "addons.list": return new { items = host.Addons.Snapshot(), configured = host.Settings.Get<Dictionary<string, bool>>("addons", []) };
            case "addons.set_enabled":
                RequireAddon(fields.Text("addon_id"));
                await settingsGate.WaitAsync(cancellationToken);
                try
                {
                    await host.SetAddonEnabledAsync(fields.Text("addon_id"), input.GetProperty("enabled").GetBoolean(), cancellationToken);
                    return new { saved = true, restartRequired = true };
                }
                finally { settingsGate.Release(); }
            case "settings.get":
                return new { key = fields.Text("key"), value = (await SettingsAsync(fields.Text("addon_id")))
                    .Get(fields.Text("key"), AgentProtocol.Element(null)) };
            case "settings.set":
                return await SetSettingAsync(fields.Text("key"), fields.Text("addon_id"), input.GetProperty("value"), cancellationToken);
            case "workspace.get": return new { current = host.Workspaces.Current, recent = host.Workspaces.Recent };
            case "workspace.create": return await host.Workspaces.CreateAsync(fields.Text("directory"), cancellationToken);
            case "workspace.open": return await host.Workspaces.OpenAsync(fields.Text("directory"), cancellationToken);
            case "assets.providers": return host.Contributions.GetAll<IAssetProvider>().Select(provider => new { provider.Id, provider.Value.Name });
            case "assets.list":
                var provider = Provider(fields.Text("provider"), fields.Text("container"));
                return new { provider = provider.Id, page = await provider.Value.ListAsync(fields.Text("container"), fields.Text("search"),
                    fields.Int("offset", 0), fields.Int("count", 100), cancellationToken) };
            case "assets.read":
                var assetProvider = Provider(fields.Text("provider"), fields.Text("container"));
                await using (var asset = await assetProvider.Value.OpenAsync(new(assetProvider.Id, fields.Text("container"), fields.Text("path")), cancellationToken))
                    return await ReadChunkAsync(asset, fields, cancellationToken);
            case "artifacts.list":
                var directory = OutputPath(fields.Text("job_id"), fields.Text("path"));
                var entries = Directory.EnumerateFileSystemEntries(directory).Skip(fields.Int("offset", 0)).Take(fields.Int("count", 100) + 1).ToArray();
                return new { items = entries.Take(fields.Int("count", 100)).Select(path => new { name = Path.GetFileName(path),
                    path = Path.GetRelativePath(OutputPath(fields.Text("job_id"), ""), path),
                    directory = Directory.Exists(path), link = (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0 }),
                    offset = fields.Int("offset", 0), hasMore = entries.Length > fields.Int("count", 100) };
            case "artifacts.read":
                await using (var artifact = new FileStream(OutputPath(fields.Text("job_id"), fields.Text("path")), FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
                    return await ReadChunkAsync(artifact, fields, cancellationToken);
            case "cache.stats": return await host.Cache.StatisticsAsync(cancellationToken);
            case "cache.clear":
                await host.Cache.ClearAsync(cancellationToken);
                return await host.Cache.StatisticsAsync(cancellationToken);
            case "host.stop":
                if (host.HostKind != "headless") throw new InvalidOperationException("Only an idle headless backend can be stopped by agents.");
                if (!await host.Queue.StopIfIdleAsync(cancellationToken)) throw new InvalidOperationException("The backend has active jobs. Wait for completion or cancel them before stopping it.");
                return new { stopping = true };
            default:
                var contribution = host.Contributions.GetAll<ITool>().First(item => AgentCatalog.ForTool(item.Id, item.Value.Definition).Name == operation);
                return await host.Jobs.SubmitAsync(contribution.Id, AgentSchema.ToolValues(input), cancellationToken);
        }
    }

    private JobSnapshot Job(string id)
    {
        if (!Guid.TryParse(id, out var parsed)) throw new ArgumentException("job_id must be a UUID.");
        return host.Jobs.Snapshot().FirstOrDefault(job => job.Id == parsed)
            ?? throw new KeyNotFoundException("Job not found in retained history.");
    }

    private Contribution<IAssetProvider> Provider(string id, string container) => id.Length > 0
        ? host.Contributions.GetAll<IAssetProvider>().FirstOrDefault(provider => provider.Id == id)
            ?? throw new KeyNotFoundException($"Unknown asset provider: {id}")
        : host.Contributions.GetAll<IAssetProvider>().FirstOrDefault(provider => provider.Value.CanOpen(container))
            ?? throw new ArgumentException("No loaded asset provider can open this container.");

    private void RequireAddon(string id)
    {
        if (!host.Addons.Snapshot().Any(addon => addon.Id == id))
            throw new KeyNotFoundException($"Unknown add-on: {id}");
    }

    private Task<ISettings> SettingsAsync(string addon)
    {
        if (addon.Length == 0) return Task.FromResult(host.Settings);
        RequireAddon(addon);
        return Task.FromResult(host.Addons.GetSettings(addon));
    }

    private async Task<object> SetSettingAsync(string key, string addon, JsonElement value, CancellationToken cancellationToken)
    {
        if (key.Length is < 1 or > 128) throw new ArgumentException("Setting keys must contain 1-128 characters.");
        if (addon.Length == 0)
        {
            if (key is "tools.vrf" or "tools.ffmpeg" or "tools.studiomdl")
            {
                if (value.ValueKind != JsonValueKind.String) throw new ArgumentException("Tool paths must be strings.");
            }
            else if (key == "runtime")
                (value.Deserialize<HostOptions>(AgentProtocol.Json) ?? throw new ArgumentException("Runtime settings must be an object.")).Validate();
            else if (key == "appearance")
            {
                if (value.ValueKind != JsonValueKind.Object) throw new ArgumentException("Appearance must be an object.");
            }
            else throw new ArgumentException("Use a supported host setting, or specify addon_id for an add-on setting.");
        }
        await settingsGate.WaitAsync(cancellationToken);
        try { await (await SettingsAsync(addon)).SetAsync(key, value, cancellationToken); }
        finally { settingsGate.Release(); }
        await host.Events.PublishAsync(new AgentSettingsChanged(key, addon.Length == 0 ? null : addon), cancellationToken);
        return new { saved = true, restartRequired = addon.Length > 0 || key == "runtime" };
    }

    private string OutputPath(string id, string relative)
    {
        var root = Path.GetFullPath(Path.Combine(host.StateDirectory, "jobs", Job(id).Id.ToString("N")));
        if (Path.IsPathRooted(relative)) throw new ArgumentException("Artifact paths must be relative to the job output.");
        var path = Path.GetFullPath(Path.Combine(root, relative));
        if (!path.Equals(root, StringComparison.OrdinalIgnoreCase) && !path.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException("Artifact paths must stay inside the job output.");
        for (var current = new DirectoryInfo(Path.GetDirectoryName(path)!); current is not null; current = current.Parent)
            if ((current.Attributes & FileAttributes.ReparsePoint) != 0)
                throw new ArgumentException("Artifact links are not supported.");
        if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
            throw new ArgumentException("Artifact links are not supported.");
        if (Path.GetFileName(path).Contains(':')) throw new ArgumentException("Artifact stream names are not supported.");
        return path;
    }

    private static async Task<object> ReadChunkAsync(Stream stream, Inputs input, CancellationToken cancellationToken)
    {
        var offset = input.Long("offset", 0);
        if (!stream.CanSeek && offset > 0) throw new ArgumentException("This asset stream does not support offsets.");
        if (stream.CanSeek) stream.Seek(Math.Min(offset, stream.Length), SeekOrigin.Begin);
        var bytes = new byte[input.Int("max_bytes", 16384) + 1];
        var used = 0;
        while (used < bytes.Length)
        {
            var read = await stream.ReadAsync(bytes.AsMemory(used), cancellationToken);
            if (read == 0) break;
            used += read;
        }
        var length = Math.Min(used, bytes.Length - 1);
        return new { offset, bytesRead = length, nextOffset = offset + length, hasMore = used > length,
            base64 = Convert.ToBase64String(bytes, 0, length), text = input.Bool("utf8") ? Encoding.UTF8.GetString(bytes, 0, length) : null };
    }

    private sealed class Inputs(JsonElement value)
    {
        public string Text(string key) => value.TryGetProperty(key, out var item) ? item.GetString()! : "";
        public int Int(string key, int fallback) => value.TryGetProperty(key, out var item) ? item.GetInt32() : fallback;
        public long Long(string key, long fallback) => value.TryGetProperty(key, out var item) ? item.GetInt64() : fallback;
        public bool Bool(string key) => value.TryGetProperty(key, out var item) && item.GetBoolean();
    }
}
