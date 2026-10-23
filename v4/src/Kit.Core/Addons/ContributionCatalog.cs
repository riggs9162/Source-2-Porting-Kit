namespace PortingKit.Core;

public sealed class ContributionCatalog : IContributions
{
    private readonly object gate = new();
    private readonly Dictionary<Type, Dictionary<string, (string Owner, object Value)>> entries = [];

    internal void Add<T>(string owner, string localId, T value) where T : class
    {
        if (!System.Text.RegularExpressions.Regex.IsMatch(localId, "^[a-z][a-z0-9-]*$"))
            throw new ArgumentException($"Invalid contribution ID: {localId}");
        var id = owner + "/" + localId;
        lock (gate)
        {
            if (!entries.TryGetValue(typeof(T), out var list))
                entries[typeof(T)] = list = new(StringComparer.Ordinal);
            if (!list.TryAdd(id, (owner, value)))
                throw new InvalidOperationException($"Duplicate {typeof(T).Name}: {id}");
        }
    }

    public IReadOnlyList<Contribution<T>> GetAll<T>() where T : class
    {
        lock (gate)
            return entries.TryGetValue(typeof(T), out var list)
                ? list.Select(pair => new Contribution<T>(pair.Key, pair.Value.Owner, (T)pair.Value.Value)).ToArray()
                : [];
    }

    public T Get<T>(string id) where T : class
    {
        lock (gate)
            return entries.TryGetValue(typeof(T), out var list) && list.TryGetValue(id, out var entry)
                ? (T)entry.Value : throw new KeyNotFoundException($"Unknown {typeof(T).Name}: {id}");
    }

    internal void Remove(string owner)
    {
        lock (gate)
        {
            foreach (var list in entries.Values)
                foreach (var key in list.Where(pair => pair.Value.Owner == owner).Select(pair => pair.Key).ToArray())
                    list.Remove(key);
        }
    }
}
