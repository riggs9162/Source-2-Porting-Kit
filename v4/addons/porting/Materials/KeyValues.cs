using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>Raised when a file cannot be parsed as KeyValues.</summary>
public sealed class KeyValuesError(string message) : Exception(message);

/// <summary>An ordered, duplicate-tolerant KeyValues block, where a value is either a string or another block.</summary>
public sealed class KeyValueBlock
{
    public List<(string Key, object Value)> Pairs { get; } = [];

    public void Add(string key, object value) => Pairs.Add((key, value));

    /// <summary>The first block stored under a key, matched case-insensitively.</summary>
    public KeyValueBlock? Block(string key)
    {
        foreach (var (name, value) in Pairs)
            if (value is KeyValueBlock block && name.Equals(key, StringComparison.OrdinalIgnoreCase)) return block;
        return null;
    }

    /// <summary>A flat view of the scalar pairs in first-seen order, the last of a repeated key winning.</summary>
    public OrderedDictionary<string, string> Scalars()
    {
        var result = new OrderedDictionary<string, string>();
        foreach (var (name, value) in Pairs)
            if (value is string text) result[name] = text;
        return result;
    }
}

/// <summary>Reads Valve KeyValues text.</summary>
public static partial class KeyValues
{
    [GeneratedRegex("""
        "((?:[^"\\]|\\.)*)"|([^\s{}"]+)|([{}])
        """)]
    private static partial Regex Token();

    /// <summary>Removes a line comment, ignoring one that sits inside a quoted string.</summary>
    private static string StripComment(string line)
    {
        var quoted = false;
        for (var index = 0; index < line.Length; index++)
        {
            var c = line[index];
            if (c == '\\' && quoted)
            {
                index++;
                continue;
            }
            if (c == '"') quoted = !quoted;
            else if (c == '/' && !quoted && index + 1 < line.Length && line[index + 1] == '/') return line[..index];
        }
        return line;
    }

    private static bool IsLineBreak(char c) => c is >= (char)0x0A and <= (char)0x0D or >= (char)0x1C and <= (char)0x1E or (char)0x85 or (char)0x2028 or (char)0x2029;

    /// <summary>Splits text on every line boundary Python recognises.</summary>
    public static IEnumerable<string> Lines(string text)
    {
        var start = 0;
        for (var i = 0; i < text.Length; i++)
        {
            var c = text[i];
            if (!IsLineBreak(c)) continue;
            yield return text[start..i];
            if (c == '\r' && i + 1 < text.Length && text[i + 1] == '\n') i++;
            start = i + 1;
        }
        if (start < text.Length) yield return text[start..];
    }

    /// <summary>Parses a document into a root block.</summary>
    /// <remarks>
    /// Comments are stripped line by line rather than with one pattern over the whole document, so a double slash inside a quoted
    /// path cannot swallow the rest of the file. Keys may be bare: Valve's own files open with unquoted keys.
    /// </remarks>
    public static KeyValueBlock Parse(string text)
    {
        var root = new KeyValueBlock();
        var stack = new List<KeyValueBlock> { root };
        string? pending = null;
        foreach (var raw in Lines(text))
        {
            var line = StripComment(raw);
            if (string.IsNullOrWhiteSpace(line)) continue;
            foreach (Match match in Token().Matches(line))
            {
                string value;
                if (match.Groups[3].Success)
                {
                    if (match.Groups[3].Value == "{")
                    {
                        if (pending is null) throw new KeyValuesError("'{' with no key before it");
                        var child = new KeyValueBlock();
                        stack[^1].Add(pending, child);
                        stack.Add(child);
                        pending = null;
                    }
                    else
                    {
                        if (stack.Count == 1) throw new KeyValuesError("'}' without a matching '{'");
                        stack.RemoveAt(stack.Count - 1);
                    }
                    continue;
                }
                if (match.Groups[1].Success) value = match.Groups[1].Value.Replace("\\\"", "\"");
                else if (match.Groups[2].Success) value = match.Groups[2].Value;
                else continue;
                if (pending is null) pending = value;
                else
                {
                    stack[^1].Add(pending, value);
                    pending = null;
                }
            }
        }
        if (stack.Count != 1) throw new KeyValuesError("unbalanced braces - a block was never closed");
        return root;
    }
}
