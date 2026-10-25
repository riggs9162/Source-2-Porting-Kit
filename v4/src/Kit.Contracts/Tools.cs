using System.Text.Json;

namespace PortingKit;

public enum ParameterKind { Text, File, Directory, Integer, Boolean, Choice }

public sealed record ToolParameter(
    string Key,
    string Label,
    ParameterKind Kind = ParameterKind.Text,
    string Default = "",
    bool Required = false,
    string Description = "",
    string[]? Choices = null,
    int Minimum = int.MinValue,
    int Maximum = int.MaxValue);

public sealed record ToolDefinition(
    string Name,
    string Category,
    string Description,
    IReadOnlyList<ToolParameter> Parameters)
{
    /// <summary>Controls the generated sidebar form without affecting CLI or MCP discovery.</summary>
    public bool ShowInDesktop { get; init; } = true;
}

/// <summary>Runs through the shared queue in both the desktop app and command line.</summary>
public interface ITool
{
    ToolDefinition Definition { get; }
    Task<ToolResult> RunAsync(ToolContext context, ToolArguments arguments, CancellationToken cancellationToken);
}

public sealed class ToolArguments(IReadOnlyDictionary<string, string> values)
{
    public string Text(string key) => values.TryGetValue(key, out var value) ? value : "";
    public bool Boolean(string key) => bool.TryParse(Text(key), out var value) && value;
    public int Integer(string key) => int.Parse(Text(key), System.Globalization.CultureInfo.InvariantCulture);
    public IReadOnlyDictionary<string, string> Values => values;
}

public sealed record Artifact(string Path, string Label, string Kind = "file");
public sealed record ToolResult(string Summary, IReadOnlyList<Artifact>? Artifacts = null, JsonElement? Data = null);
public sealed record JobProgress(double? Fraction, string Message);

/// <summary>Provides job-scoped reporting and durable output paths to a tool.</summary>
public sealed class ToolContext(
    Guid jobId,
    string addonId,
    string outputDirectory,
    IHostServices host,
    Action<LogLevel, string> log,
    Action<JobProgress> progress)
{
    public Guid JobId { get; } = jobId;
    public string AddonId { get; } = addonId;
    public string OutputDirectory { get; } = outputDirectory;
    public IHostServices Host { get; } = host;
    public void Log(string message, LogLevel level = LogLevel.Info) => log(level, message);
    public void Report(double? fraction, string message) => progress(new(fraction, message));
}

public enum JobStatus { Queued, Running, Cancelling, Succeeded, Failed, Cancelled, Interrupted }

public sealed record JobSnapshot(
    Guid Id,
    string ToolId,
    string Name,
    JobStatus Status,
    DateTimeOffset Created,
    DateTimeOffset? Started,
    DateTimeOffset? Finished,
    JobProgress Progress,
    ToolResult? Result,
    string? Error,
    IReadOnlyDictionary<string, string> Arguments);

public sealed record JobChanged(JobSnapshot Job);

public interface IJobQueue
{
    IReadOnlyList<JobSnapshot> Snapshot();
    Task<JobSnapshot> SubmitAsync(string toolId, IReadOnlyDictionary<string, string> arguments, CancellationToken cancellationToken = default);
    bool Cancel(Guid id);
    bool HasActiveJobs { get; }
}
