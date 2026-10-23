using System.Threading.Channels;

namespace PortingKit.Core;

/// <summary>Runs a bounded queue off the UI thread with cancellation and durable terminal records.</summary>
public sealed class JobQueue : IJobQueue, IAsyncDisposable
{
    private readonly IHostServices host;
    private readonly object gate = new();
    private readonly SemaphoreSlim submissions = new(1);
    private readonly Dictionary<Guid, Entry> entries = [];
    private readonly Channel<Entry> pending = Channel.CreateBounded<Entry>(32);
    private readonly List<Task> workers = [];
    private Task? shutdown;
    private bool stopping;

    public JobQueue(IHostServices host) => this.host = host;

    public async Task StartAsync(int workerCount)
    {
        var directory = Path.Combine(host.StateDirectory, "jobs");
        Directory.CreateDirectory(directory);
        foreach (var path in Directory.EnumerateFiles(directory, "*.json").OrderByDescending(File.GetLastWriteTimeUtc).Take(100))
        {
            try
            {
                var snapshot = await JsonFiles.ReadAsync<JobSnapshot>(path);
                if (snapshot is null)
                    continue;
                if (snapshot.Status is JobStatus.Running or JobStatus.Queued or JobStatus.Cancelling)
                {
                    snapshot = snapshot with
                    {
                        Status = JobStatus.Interrupted, Finished = DateTimeOffset.Now,
                        Error = "The application stopped before this job finished."
                    };
                    await SaveAsync(snapshot);
                }
                entries[snapshot.Id] = new(snapshot, null);
            }
            catch (Exception exception) when (exception is System.Text.Json.JsonException or IOException)
            {
                host.Log.Write(LogLevel.Warning, "jobs", $"Cannot read {path}: {exception.Message}");
            }
        }
        for (var index = 0; index < workerCount; index++)
            workers.Add(Task.Run(ConsumeAsync));
    }

    public bool HasActiveJobs
    {
        get
        {
            lock (gate)
                return entries.Values.Any(entry => IsActive(entry.Snapshot.Status));
        }
    }

    public IReadOnlyList<JobSnapshot> Snapshot()
    {
        lock (gate)
            return entries.Values.Select(entry => entry.Snapshot).OrderByDescending(job => job.Created).ToArray();
    }

    public async Task<JobSnapshot> SubmitAsync(string toolId, IReadOnlyDictionary<string, string> arguments,
        CancellationToken cancellationToken = default)
    {
        var tool = host.Contributions.Get<ITool>(toolId);
        var values = ToolValidation.Normalize(tool.Definition, arguments);
        await submissions.WaitAsync(cancellationToken);
        try
        {
            lock (gate)
            {
                if (stopping)
                    throw new InvalidOperationException("The job queue is shutting down.");
                if (entries.Values.Count(entry => IsActive(entry.Snapshot.Status)) >= 32)
                    throw new InvalidOperationException("The job queue is full.");
            }
            var snapshot = new JobSnapshot(Guid.NewGuid(), toolId, tool.Definition.Name, JobStatus.Queued,
                DateTimeOffset.Now, null, null, new(null, "Waiting"), null, null, values);
            var entry = new Entry(snapshot, tool);
            await SaveAsync(snapshot);
            lock (gate)
            {
                if (stopping)
                    throw new InvalidOperationException("The job queue is shutting down.");
                entries.Add(snapshot.Id, entry);
                if (!pending.Writer.TryWrite(entry))
                    throw new InvalidOperationException("The job queue is full.");
            }
            await host.Events.PublishAsync(new JobChanged(snapshot));
            return snapshot;
        }
        finally
        {
            submissions.Release();
        }
    }

    public bool Cancel(Guid id)
    {
        Entry? entry;
        lock (gate)
        {
            if (!entries.TryGetValue(id, out entry) || !IsActive(entry.Snapshot.Status))
                return false;
            entry.Snapshot = entry.Snapshot with { Status = JobStatus.Cancelling };
        }
        entry.Cancellation.Cancel();
        return true;
    }

    private async Task ConsumeAsync()
    {
        await foreach (var entry in pending.Reader.ReadAllAsync())
        {
            try
            {
                entry.Cancellation.Token.ThrowIfCancellationRequested();
                var started = Update(entry, snapshot => snapshot with
                {
                    Status = JobStatus.Running, Started = DateTimeOffset.Now, Progress = new(null, "Starting")
                });
                await SaveAsync(started);
                await host.Events.PublishAsync(new JobChanged(started));
                var output = Path.Combine(host.StateDirectory, "jobs", started.Id.ToString("N"));
                Directory.CreateDirectory(output);
                var context = new ToolContext(started.Id, started.ToolId.Split('/')[0], output, host,
                    (level, message) => host.Log.Write(level, started.Id.ToString("N")[..8], message),
                    progress => Update(entry, snapshot => snapshot with
                    {
                        Progress = progress with { Fraction = progress.Fraction is { } fraction ? Math.Clamp(fraction, 0, 1) : null }
                    }));
                var result = await entry.Tool!.RunAsync(context, new(started.Arguments), entry.Cancellation.Token);
                entry.Cancellation.Token.ThrowIfCancellationRequested();
                Update(entry, snapshot => snapshot with
                {
                    Status = JobStatus.Succeeded, Result = result, Finished = DateTimeOffset.Now,
                    Progress = new(1, result.Summary)
                });
            }
            catch (OperationCanceledException) when (entry.Cancellation.IsCancellationRequested)
            {
                Update(entry, snapshot => snapshot with
                {
                    Status = JobStatus.Cancelled, Finished = DateTimeOffset.Now, Progress = new(null, "Cancelled")
                });
            }
            catch (Exception exception)
            {
                Update(entry, snapshot => snapshot with
                {
                    Status = JobStatus.Failed, Finished = DateTimeOffset.Now, Error = exception.Message,
                    Progress = new(null, "Failed")
                });
                host.Log.Write(LogLevel.Error, entry.Snapshot.ToolId, exception.ToString());
            }
            finally
            {
                entry.Tool = null;
            }
            try
            {
                await SaveAsync(entry.Snapshot);
                await host.Events.PublishAsync(new JobChanged(entry.Snapshot));
                await PruneAsync();
            }
            catch (Exception exception)
            {
                host.Log.Write(LogLevel.Error, "jobs", $"Cannot save job history: {exception.Message}");
            }
        }
    }

    private JobSnapshot Update(Entry entry, Func<JobSnapshot, JobSnapshot> update)
    {
        lock (gate)
        {
            entry.Snapshot = update(entry.Snapshot);
            return entry.Snapshot;
        }
    }

    private Task SaveAsync(JobSnapshot snapshot) => JsonFiles.WriteAsync(
        Path.Combine(host.StateDirectory, "jobs", snapshot.Id.ToString("N") + ".json"), snapshot);

    private Task PruneAsync()
    {
        Guid[] remove;
        lock (gate)
        {
            remove = entries.Values.Where(entry => !IsActive(entry.Snapshot.Status))
                .OrderByDescending(entry => entry.Snapshot.Created).Skip(100).Select(entry => entry.Snapshot.Id).ToArray();
            foreach (var id in remove)
            {
                entries[id].Cancellation.Dispose();
                entries.Remove(id);
            }
        }
        foreach (var id in remove)
            File.Delete(Path.Combine(host.StateDirectory, "jobs", id.ToString("N") + ".json"));
        return Task.CompletedTask;
    }

    public static bool IsActive(JobStatus status) => status is JobStatus.Queued or JobStatus.Running or JobStatus.Cancelling;

    public ValueTask DisposeAsync()
    {
        lock (gate)
            return new(shutdown ??= ShutdownAsync());
    }

    private async Task ShutdownAsync()
    {
        await submissions.WaitAsync();
        try
        {
            Entry[] active;
            lock (gate)
            {
                stopping = true;
                active = entries.Values.Where(entry => IsActive(entry.Snapshot.Status)).ToArray();
            }
            foreach (var entry in active)
                entry.Cancellation.Cancel();
            pending.Writer.TryComplete();
        }
        finally
        {
            submissions.Release();
        }
        await Task.WhenAll(workers);
        foreach (var entry in entries.Values)
            entry.Cancellation.Dispose();
    }

    private sealed class Entry(JobSnapshot snapshot, ITool? tool)
    {
        public JobSnapshot Snapshot { get; set; } = snapshot;
        public ITool? Tool { get; set; } = tool;
        public CancellationTokenSource Cancellation { get; } = new();
    }
}
