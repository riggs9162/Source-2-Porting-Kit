namespace PortingKit;

/// <summary>Describes one self-contained directory under the add-ons root.</summary>
public sealed record AddonManifest
{
    public required string Id { get; init; }
    public required string Name { get; init; }
    public required string Version { get; init; }
    public string Description { get; init; } = "";
    public string Author { get; init; } = "";
    public int ApiVersion { get; init; } = 1;
    public required string Assembly { get; init; }
    public required string EntryPoint { get; init; }
    public string? Project { get; init; }
    public string[] Dependencies { get; init; } = [];
}

/// <summary>Registers contributions at startup; releases resources during shutdown.</summary>
public interface IAddon : IAsyncDisposable
{
    void Initialize(IAddonContext context);
}

/// <summary>Scopes contributions, hooks, settings, and paths to the owning add-on.</summary>
public interface IAddonContext
{
    AddonManifest Manifest { get; }
    string Directory { get; }
    string DataDirectory { get; }
    ISettings Settings { get; }
    IHostServices Host { get; }
    void Register<T>(string id, T contribution) where T : class;
    void Subscribe<T>(Func<T, CancellationToken, ValueTask> handler);
}

public sealed record Contribution<T>(string Id, string AddonId, T Value);

/// <summary>Exposes registered extensions without coupling the backend to a UI framework.</summary>
public interface IContributions
{
    IReadOnlyList<Contribution<T>> GetAll<T>() where T : class;
    T Get<T>(string id) where T : class;
}

public interface IHostServices
{
    string RootDirectory { get; }
    string StateDirectory { get; }
    ISettings Settings { get; }
    ILog Log { get; }
    IEventHub Events { get; }
    IContributions Contributions { get; }
    IProcessRunner Processes { get; }
    IJobQueue Jobs { get; }
    IDiskCache Cache { get; }
    IWorkspaces Workspaces { get; }
}
