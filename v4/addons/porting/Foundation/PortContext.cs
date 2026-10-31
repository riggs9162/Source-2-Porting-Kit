namespace PortingKit.Addons.Porting;

/// <summary>The logging, cancellation and process access every porting stage receives.</summary>
public sealed class PortContext(IProcessRunner processes, Action<string, LogLevel> log, CancellationToken token)
{
    public IProcessRunner Processes { get; } = processes;
    public CancellationToken Token { get; } = token;
    public void Log(string message, LogLevel level = LogLevel.Info) => log(message, level);
    public void Warn(string message) => log(message, LogLevel.Warning);

    /// <summary>Stops between units of work. Call it as a bare statement at the top of a loop.</summary>
    public void Check() => Token.ThrowIfCancellationRequested();

    /// <summary>Prefixes every message from a stage with the asset it belongs to.</summary>
    public PortContext For(string label) => new(Processes, (message, level) => log($"{label}: {message}", level), Token);

    /// <summary>Copies messages into a stage record while still reporting them.</summary>
    public PortContext Tee(Action<string, LogLevel> capture) => new(Processes, (message, level) =>
    {
        capture(message, level);
        log(message, level);
    }, Token);
}
