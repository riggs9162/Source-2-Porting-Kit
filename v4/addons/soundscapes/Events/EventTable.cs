using System.Globalization;

namespace PortingKit.Addons.Soundscapes;

/// <summary>Every sound event a game defines, keyed by name.</summary>
/// <remarks>Sound events inherit. About a fifth of Half-Life: Alyx's name another event as their <c>base</c> and
/// override only a file list or an angle, so reading one without its chain loses its volume and often its sounds.</remarks>
public sealed class EventTable
{
    private readonly Dictionary<string, Dictionary<string, object>> events = new(StringComparer.OrdinalIgnoreCase);

    public int Count => events.Count;

    public IEnumerable<string> Names => events.Keys;

    public bool Contains(string name) => events.ContainsKey(name);

    /// <summary>Adds every event in one decompiled <c>.vsndevts</c> file and returns how many it held.</summary>
    public int AddFile(string text)
    {
        var added = 0;
        foreach (var (name, value) in Kv3Text.Parse(text))
        {
            if (value is not Dictionary<string, object> fields) continue;
            events[name] = fields;
            added++;
        }
        return added;
    }

    /// <summary>An event's fields with its <c>base</c> chain merged in, the event's own values winning.</summary>
    /// <returns>Null when no file defines the event.</returns>
    public Dictionary<string, object>? Resolve(string name)
    {
        if (!events.ContainsKey(name)) return null;
        var merged = new Dictionary<string, object>(StringComparer.Ordinal);
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var current = name;
        while (current is not null && seen.Add(current) && events.TryGetValue(current, out var fields))
        {
            foreach (var (key, value) in fields) merged.TryAdd(key, value);
            current = fields.GetValueOrDefault("base") as string;
        }
        merged.Remove("base");
        return merged;
    }
}

/// <summary>Reads typed values out of an event's fields.</summary>
public static class EventFields
{
    public static double? Number(IReadOnlyDictionary<string, object> fields, string key) =>
        fields.GetValueOrDefault(key) is string text && double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out var value) ? value : null;

    public static string Text(IReadOnlyDictionary<string, object> fields, string key) => fields.GetValueOrDefault(key) as string ?? "";

    /// <summary>Valve write a flag as <c>true</c>, <c>1.0</c> or <c>1</c>, so a missing flag takes the fallback.</summary>
    public static bool Flag(IReadOnlyDictionary<string, object> fields, string key, bool fallback) =>
        fields.GetValueOrDefault(key) is string text ? IsOn(text) : fallback;

    /// <summary>Reads a written flag. Text that is neither a boolean nor a number counts as on.</summary>
    public static bool IsOn(string text)
    {
        if (bool.TryParse(text, out var flag)) return flag;
        return !double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out var value) || value != 0;
    }

    /// <summary>A value Valve write either as one string or as an array of strings.</summary>
    public static List<string> Strings(IReadOnlyDictionary<string, object> fields, string key) => fields.GetValueOrDefault(key) switch
    {
        string text when text.Trim().Length > 0 => [text],
        List<object> items => [.. items.OfType<string>().Where(item => item.Trim().Length > 0)],
        _ => []
    };

    /// <summary>Fields such as <c>vsnd_file_01</c>, keyed by their number.</summary>
    public static SortedDictionary<int, string> Numbered(IReadOnlyDictionary<string, object> fields, string prefix, string suffix = "")
    {
        var found = new SortedDictionary<int, string>();
        foreach (var (key, value) in fields)
        {
            if (value is not string text || text.Trim().Length == 0) continue;
            if (!key.StartsWith(prefix, StringComparison.Ordinal) || !key.EndsWith(suffix, StringComparison.Ordinal) || key.Length <= prefix.Length + suffix.Length) continue;
            if (int.TryParse(key.AsSpan(prefix.Length, key.Length - prefix.Length - suffix.Length), NumberStyles.None, CultureInfo.InvariantCulture, out var number)) found[number] = text;
        }
        return found;
    }
}
