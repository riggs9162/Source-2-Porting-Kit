using System.Threading.Channels;

namespace PortingKit.Core;

/// <summary>Keeps a bounded live tail and rotates disk logs independently of the UI thread.</summary>
public sealed class KitLog : ILog, IAsyncDisposable
{
    private readonly object gate = new();
    private readonly Queue<LogEntry> tail = new();
    private readonly Channel<LogEntry> pending = Channel.CreateBounded<LogEntry>(new BoundedChannelOptions(2048)
    {
        SingleReader = true,
        FullMode = BoundedChannelFullMode.DropOldest
    });
    private readonly Task writer;
    public string? WriteError { get; private set; }

    public KitLog(string directory)
    {
        Directory.CreateDirectory(directory);
        writer = Task.Run(() => WriteLoopAsync(Path.Combine(directory, "kit.log")));
    }

    public void Write(LogLevel level, string source, string message)
    {
        var entry = new LogEntry(DateTimeOffset.Now, level, source, message.Length > 8192 ? message[..8192] : message);
        lock (gate)
        {
            tail.Enqueue(entry);
            while (tail.Count > 500)
                tail.Dequeue();
        }
        pending.Writer.TryWrite(entry);
    }

    public IReadOnlyList<LogEntry> Tail(int count = 200)
    {
        lock (gate)
            return tail.TakeLast(Math.Clamp(count, 0, 500)).ToArray();
    }

    private async Task WriteLoopAsync(string path)
    {
        await foreach (var entry in pending.Reader.ReadAllAsync())
        {
            try
            {
                if (File.Exists(path) && new FileInfo(path).Length >= 4 * 1024 * 1024)
                {
                    for (var index = 2; index >= 0; index--)
                    {
                        var source = index == 0 ? path : path + "." + index;
                        if (File.Exists(source))
                            File.Move(source, path + "." + (index + 1), true);
                    }
                }
                await File.AppendAllTextAsync(path,
                    $"{entry.Time:O} [{entry.Level}] {entry.Source}: {entry.Message}{Environment.NewLine}");
            }
            catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
            {
                WriteError = exception.Message;
            }
        }
    }

    public async ValueTask DisposeAsync()
    {
        pending.Writer.TryComplete();
        await writer;
    }
}
