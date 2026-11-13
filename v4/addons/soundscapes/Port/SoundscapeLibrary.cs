namespace PortingKit.Addons.Soundscapes;

/// <summary>One soundscape found in a script, summarised for a list.</summary>
/// <param name="Name">The soundscape's name.</param>
/// <param name="File">The script it was read from.</param>
/// <param name="Body">Its rules, unchanged.</param>
/// <param name="Events">How many Source 2 <c>playevent</c> rules it holds.</param>
/// <param name="Rules">How many Source 1 <c>playlooping</c> and <c>playrandom</c> rules it holds.</param>
/// <param name="Nested">How many other soundscapes it plays.</param>
public sealed record SoundscapeEntry(string Name, string File, KvBlock Body, int Events, int Rules, int Nested)
{
    public string Kind => (Events > 0, Rules > 0) switch
    {
        (true, true) => "Mixed",
        (true, false) => "Source 2",
        (false, true) => "Source 1",
        _ => Nested > 0 ? "Group" : "Empty"
    };
}

/// <summary>Every soundscape in a game's scripts.</summary>
public sealed class SoundscapeLibrary
{
    private readonly Dictionary<string, SoundscapeEntry> byName = new(StringComparer.OrdinalIgnoreCase);

    public List<SoundscapeEntry> Entries { get; } = [];

    /// <summary>Scripts that could not be read, with the reason.</summary>
    public List<string> Problems { get; } = [];

    public SoundscapeEntry? Find(string name) => byName.GetValueOrDefault(name);

    /// <summary>Adds the soundscapes of one script. A name defined twice keeps its last definition, as the engine does.</summary>
    public void AddScript(string file, string text)
    {
        KvBlock root;
        try
        {
            root = KeyValuesText.Parse(text);
        }
        catch (InvalidDataException exception)
        {
            Problems.Add($"{file}: {exception.Message}");
            return;
        }
        foreach (var (name, value) in root.Pairs)
        {
            if (value is not KvBlock body) continue;
            int Count(params string[] keys) => body.Pairs.Count(pair => pair.Value is KvBlock && keys.Contains(pair.Key, StringComparer.OrdinalIgnoreCase));
            var entry = new SoundscapeEntry(name, file, body, Count("playevent"), Count("playlooping", "playrandom"), Count("playsoundscape"));
            if (byName.TryGetValue(name, out var previous)) Entries.Remove(previous);
            byName[name] = entry;
            Entries.Add(entry);
        }
    }

    /// <summary>Expands names and <c>*</c> patterns into the soundscapes they match, in script order.</summary>
    /// <param name="patterns">Names or patterns, such as <c>a2_pistol_*</c>.</param>
    /// <param name="unknown">Receives every entry that matched nothing.</param>
    public List<string> Match(IEnumerable<string> patterns, List<string> unknown) => Wildcards.Match(Entries.Select(entry => entry.Name), patterns, unknown);
}

/// <summary>Matches names against patterns where <c>*</c> stands for any run of characters.</summary>
public static class Wildcards
{
    public static List<string> Match(IEnumerable<string> names, IEnumerable<string> patterns, List<string> unknown)
    {
        var all = names.ToList();
        var picked = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var pattern in patterns.Select(pattern => pattern.Trim()).Where(pattern => pattern.Length > 0))
        {
            var expression = new System.Text.RegularExpressions.Regex("^" + System.Text.RegularExpressions.Regex.Escape(pattern).Replace("\\*", ".*") + "$",
                System.Text.RegularExpressions.RegexOptions.IgnoreCase | System.Text.RegularExpressions.RegexOptions.CultureInvariant);
            var before = picked.Count;
            var matched = false;
            foreach (var name in all.Where(name => expression.IsMatch(name)))
            {
                matched = true;
                picked.Add(name);
            }
            if (!matched && picked.Count == before) unknown.Add(pattern);
        }
        return [.. all.Where(picked.Contains)];
    }
}
