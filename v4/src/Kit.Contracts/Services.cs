namespace PortingKit;

public interface ISettings
{
    T Get<T>(string key, T fallback);
    Task SetAsync<T>(string key, T value, CancellationToken cancellationToken = default);
}

public enum LogLevel { Debug, Info, Warning, Error, Success }
public sealed record LogEntry(DateTimeOffset Time, LogLevel Level, string Source, string Message);

public interface ILog
{
    void Write(LogLevel level, string source, string message);
    IReadOnlyList<LogEntry> Tail(int count = 200);
}

/// <summary>Dispatches typed hooks in registration order; each handler has an isolated failure boundary.</summary>
public interface IEventHub
{
    IDisposable Subscribe<T>(Func<T, CancellationToken, ValueTask> handler);
    ValueTask PublishAsync<T>(T message, CancellationToken cancellationToken = default);
}

public sealed record ProcessRequest(
    string Executable,
    IReadOnlyList<string> Arguments,
    string? WorkingDirectory = null,
    TimeSpan? Timeout = null);

public sealed record ProcessLine(bool IsError, string Text);
public sealed record ProcessResult(int ExitCode, TimeSpan Duration, IReadOnlyList<string> Tail);

/// <summary>Runs an executable without a shell and terminates its process tree on cancellation.</summary>
public interface IProcessRunner
{
    Task<ProcessResult> RunAsync(ProcessRequest request, Action<ProcessLine>? output = null, CancellationToken cancellationToken = default);
}

public sealed record CacheStatistics(long Bytes, long Limit, int Files, int ActiveLeases);

/// <summary>Pins a cached file until its consumer releases the lease.</summary>
public interface ICacheLease : IDisposable
{
    string Path { get; }
}

public interface IDiskCache
{
    Task<ICacheLease> AcquireAsync(string key, Func<Stream, CancellationToken, Task> create, CancellationToken cancellationToken = default);
    Task<CacheStatistics> StatisticsAsync(CancellationToken cancellationToken = default);
    Task ClearAsync(CancellationToken cancellationToken = default);
}

public sealed record Workspace(string Name, string RootDirectory, string OutputDirectory);
public sealed record WorkspaceChanged(Workspace Workspace);

public interface IWorkspaces
{
    Workspace? Current { get; }
    IReadOnlyList<string> Recent { get; }
    Task<Workspace> CreateAsync(string directory, CancellationToken cancellationToken = default);
    Task<Workspace> OpenAsync(string directory, CancellationToken cancellationToken = default);
}

/// <summary>A reference to an asset remains small; providers open content only when requested.</summary>
public sealed record AssetReference(string Provider, string Container, string Path);
public sealed record AssetEntry(AssetReference Reference, string Name, string Kind, long Size);
public sealed record AssetPage(IReadOnlyList<AssetEntry> Items, int Offset, bool HasMore);

public interface IAssetProvider
{
    string Name { get; }
    bool CanOpen(string path);
    Task<AssetPage> ListAsync(string container, string search, int offset, int count, CancellationToken cancellationToken);
    Task<Stream> OpenAsync(AssetReference asset, CancellationToken cancellationToken);
}

/// <summary>Owns preview resources so closing a view can release CPU and GPU allocations.</summary>
public interface IPreviewSession : IAsyncDisposable
{
    AssetReference Asset { get; }
    long EstimatedResidentBytes { get; }
}
