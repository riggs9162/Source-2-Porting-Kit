using PortingKit.Core.Automation;

namespace PortingKit.Core;

public sealed record HostOptions(int Workers = 1, int DiskCacheMegabytes = 512)
{
    public HostOptions Validate()
    {
        if (Workers is < 1 or > 4 || DiskCacheMegabytes is < 16 or > 65536)
            throw new ArgumentException("Workers must be 1-4 and disk cache must be 16-65536 MB.");
        return this;
    }
}

/// <summary>Owns shared services and shuts jobs down before releasing add-on resources.</summary>
public sealed class KitHost : IHostServices, IAsyncDisposable
{
    private readonly FileStream stateLock;
    private int disposed;
    private AgentServer? agentServer;
    private readonly CancellationTokenSource headlessStop = new();
    public string HostKind { get; private set; } = "embedded";
    public CancellationToken HeadlessStopping => headlessStop.Token;
    internal void RequestHeadlessStop() => headlessStop.Cancel();

    private KitHost(string rootDirectory, string stateDirectory, SettingsStore settings, FileStream stateLock)
    {
        this.stateLock = stateLock;
        RootDirectory = Path.GetFullPath(rootDirectory);
        StateDirectory = Path.GetFullPath(stateDirectory);
        Settings = settings;
        Options = settings.Get("runtime", new HostOptions()).Validate();
        Log = new KitLog(Path.Combine(StateDirectory, "logs"));
        Events = new EventHub(Log);
        Catalog = new();
        Processes = new ProcessRunner();
        Cache = new DiskCache(Path.Combine(StateDirectory, "cache"), Options.DiskCacheMegabytes * 1024L * 1024L);
        Workspaces = new Workspaces(Settings, Events);
        Queue = new(this);
        Addons = new(this, Catalog);
    }

    public string RootDirectory { get; }
    public string StateDirectory { get; }
    public HostOptions Options { get; }
    public ISettings Settings { get; }
    public KitLog Log { get; }
    ILog IHostServices.Log => Log;
    public IEventHub Events { get; }
    public ContributionCatalog Catalog { get; }
    public IContributions Contributions => Catalog;
    public IProcessRunner Processes { get; }
    public JobQueue Queue { get; }
    public IJobQueue Jobs => Queue;
    public IDiskCache Cache { get; }
    public IWorkspaces Workspaces { get; }
    public AddonManager Addons { get; }

    public Task SetAddonEnabledAsync(string id, bool enabled, CancellationToken cancellationToken = default)
    {
        if (!Addons.Snapshot().Any(addon => addon.Id == id)) throw new KeyNotFoundException("Unknown add-on: " + id);
        return ((SettingsStore)Settings).UpdateAsync("addons", new Dictionary<string, bool>(),
            current => new Dictionary<string, bool>(current) { [id] = enabled }, cancellationToken);
    }

    public static async Task<KitHost> OpenAsync(string rootDirectory, string? stateDirectory = null,
        CancellationToken cancellationToken = default, string hostKind = "embedded")
    {
        stateDirectory ??= Path.Combine(rootDirectory, ".state");
        Directory.CreateDirectory(stateDirectory);
        FileStream stateLock;
        try
        {
            stateLock = new(Path.Combine(stateDirectory, "host.lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
        }
        catch (IOException exception)
        {
            throw new IOException("This V4 settings directory is already in use. Close the other instance or choose a separate --state directory.", exception);
        }
        KitHost host;
        try
        {
            var settings = await SettingsStore.OpenAsync(Path.Combine(stateDirectory, "settings.json"));
            host = new KitHost(rootDirectory, stateDirectory, settings, stateLock);
            host.HostKind = hostKind;
        }
        catch
        {
            stateLock.Dispose();
            throw;
        }
        try
        {
            await host.Queue.StartAsync(host.Options.Workers);
            await host.Addons.LoadAsync(cancellationToken);
            host.agentServer = new(host);
            host.Log.Write(LogLevel.Info, "host", $"V4 ready. Workers: {host.Options.Workers}. Cache: {host.Options.DiskCacheMegabytes} MB.");
            return host;
        }
        catch
        {
            await host.DisposeAsync();
            throw;
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref disposed, 1) != 0)
            return;
        try
        {
            if (agentServer is not null) await agentServer.DisposeAsync();
            await Queue.DisposeAsync();
            await Addons.DisposeAsync();
            await Log.DisposeAsync();
        }
        finally
        {
            stateLock.Dispose();
            headlessStop.Dispose();
        }
    }
}
