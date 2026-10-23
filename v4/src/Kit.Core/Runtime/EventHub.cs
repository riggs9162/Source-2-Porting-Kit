namespace PortingKit.Core;

public sealed class EventHub(ILog log) : IEventHub
{
    private readonly object gate = new();
    private readonly Dictionary<Type, List<Delegate>> handlers = [];

    public IDisposable Subscribe<T>(Func<T, CancellationToken, ValueTask> handler)
    {
        lock (gate)
        {
            if (!handlers.TryGetValue(typeof(T), out var list))
                handlers[typeof(T)] = list = [];
            list.Add(handler);
        }
        return new Subscription(() =>
        {
            lock (gate)
                handlers[typeof(T)].Remove(handler);
        });
    }

    public async ValueTask PublishAsync<T>(T message, CancellationToken cancellationToken = default)
    {
        Delegate[] snapshot;
        lock (gate)
            snapshot = handlers.TryGetValue(typeof(T), out var list) ? list.ToArray() : [];
        foreach (var handler in snapshot)
        {
            cancellationToken.ThrowIfCancellationRequested();
            try
            {
                await ((Func<T, CancellationToken, ValueTask>)handler)(message, cancellationToken);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception exception)
            {
                log.Write(LogLevel.Error, "hooks", $"{typeof(T).Name}: {exception.Message}");
            }
        }
    }

    private sealed class Subscription(Action remove) : IDisposable
    {
        private Action? action = remove;
        public void Dispose() => Interlocked.Exchange(ref action, null)?.Invoke();
    }
}
