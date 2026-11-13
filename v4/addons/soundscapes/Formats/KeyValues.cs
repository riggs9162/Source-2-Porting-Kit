using System.Text;

namespace PortingKit.Addons.Soundscapes;

/// <summary>An ordered KeyValues block that keeps repeated keys.</summary>
/// <remarks>A soundscape is mostly a run of blocks sharing one key, such as <c>playevent</c>, so a dictionary would
/// keep only the last of them. Order is kept because a soundscape reads top to bottom.</remarks>
public sealed class KvBlock
{
    public List<KeyValuePair<string, object>> Pairs { get; } = [];

    public void Add(string key, string value) => Pairs.Add(new(key, value));

    public void Add(string key, KvBlock value) => Pairs.Add(new(key, value));

    /// <summary>The first text value stored under a key, matched without regard to case.</summary>
    public string? Text(string key) =>
        Pairs.Where(pair => pair.Value is string && pair.Key.Equals(key, StringComparison.OrdinalIgnoreCase)).Select(pair => (string)pair.Value).FirstOrDefault();

    public KvBlock? Block(string key) =>
        Pairs.Where(pair => pair.Value is KvBlock && pair.Key.Equals(key, StringComparison.OrdinalIgnoreCase)).Select(pair => (KvBlock)pair.Value).FirstOrDefault();

    public IEnumerable<string> Texts(string key) =>
        Pairs.Where(pair => pair.Value is string && pair.Key.Equals(key, StringComparison.OrdinalIgnoreCase)).Select(pair => (string)pair.Value);
}

/// <summary>Reads and writes the KeyValues text that soundscape scripts and manifests use.</summary>
public static class KeyValuesText
{
    /// <summary>Parses a document into a root block.</summary>
    /// <remarks>
    /// <para>Comments are removed line by line, and only outside quotes. Valve disable rules by commenting them out in
    /// place, so a reader that kept them would port sounds the game never plays.</para>
    /// <para>Keys may be bare words: the shipped manifest opens with one.</para>
    /// </remarks>
    public static KvBlock Parse(string text)
    {
        var root = new KvBlock();
        var stack = new Stack<KvBlock>();
        stack.Push(root);
        string? pending = null;
        foreach (var (token, brace) in Tokens(text))
        {
            if (brace && token == "{")
            {
                if (pending is null) throw new InvalidDataException("A '{' has no key before it.");
                var child = new KvBlock();
                stack.Peek().Add(pending, child);
                stack.Push(child);
                pending = null;
            }
            else if (brace)
            {
                if (stack.Count == 1) throw new InvalidDataException("A '}' has no matching '{'.");
                stack.Pop();
                pending = null;
            }
            else if (pending is null) pending = token;
            else
            {
                stack.Peek().Add(pending, token);
                pending = null;
            }
        }
        if (stack.Count != 1) throw new InvalidDataException("A block was never closed.");
        return root;
    }

    private static IEnumerable<(string Token, bool Brace)> Tokens(string text)
    {
        var builder = new StringBuilder();
        foreach (var line in text.Split('\n'))
        {
            var index = 0;
            while (index < line.Length)
            {
                var current = line[index];
                if (char.IsWhiteSpace(current)) { index++; continue; }
                if (current == '/' && index + 1 < line.Length && line[index + 1] == '/') break;
                if (current is '{' or '}') { yield return (current.ToString(), true); index++; continue; }
                builder.Clear();
                if (current == '"')
                {
                    index++;
                    while (index < line.Length && line[index] != '"')
                    {
                        if (line[index] == '\\' && index + 1 < line.Length && line[index + 1] == '"') index++;
                        builder.Append(line[index++]);
                    }
                    index++;
                }
                else
                {
                    while (index < line.Length && !char.IsWhiteSpace(line[index]) && line[index] is not ('{' or '}' or '"'))
                    {
                        if (line[index] == '/' && index + 1 < line.Length && line[index + 1] == '/') break;
                        builder.Append(line[index++]);
                    }
                }
                yield return (builder.ToString(), false);
            }
        }
    }

    /// <summary>Writes a block in Valve's layout: tabs, quoted keys and values, braces on their own lines.</summary>
    public static string Write(KvBlock block)
    {
        var builder = new StringBuilder();
        Write(block, builder, 0);
        return builder.ToString();
    }

    private static void Write(KvBlock block, StringBuilder builder, int depth)
    {
        var pad = new string('\t', depth);
        foreach (var (key, value) in block.Pairs)
        {
            if (value is KvBlock child)
            {
                builder.Append(pad).Append('"').Append(key).Append("\"\n").Append(pad).Append("{\n");
                Write(child, builder, depth + 1);
                builder.Append(pad).Append("}\n");
                if (depth == 0) builder.Append('\n');
            }
            else builder.Append(pad).Append('"').Append(key).Append("\"\t\"").Append((string)value).Append("\"\n");
        }
    }
}
