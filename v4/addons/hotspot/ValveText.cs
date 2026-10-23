using System.Text;

namespace PortingKit.Addons.Hotspot;

internal sealed record ValveToken(string Value, int Start, int End);

/// <summary>Bounded tokenizer for textual KV3 and KeyValues, preserving offsets for precise VMT edits.</summary>
internal sealed class ValveText
{
    public List<ValveToken> Tokens { get; } = [];
    public int Position { get; set; }
    public bool End => Position >= Tokens.Count;
    public string Peek => End ? "" : Tokens[Position].Value;

    public ValveText(string text)
    {
        if (text.Length > 4 * 1024 * 1024) throw new InvalidDataException("Valve text exceeds the 4 MiB limit.");
        for (var i = 0; i < text.Length;)
        {
            if (char.IsWhiteSpace(text[i]) || text[i] == '\uFEFF') { i++; continue; }
            if (text.AsSpan(i).StartsWith("//")) { while (i < text.Length && text[i] != '\n') i++; continue; }
            if (text.AsSpan(i).StartsWith("<!--") || text.AsSpan(i).StartsWith("/*"))
            {
                var close = text.AsSpan(i).StartsWith("<!--") ? "-->" : "*/";
                var end = text.IndexOf(close, i + 2, StringComparison.Ordinal);
                if (end < 0) throw new InvalidDataException("Unclosed comment.");
                i = end + close.Length;
                continue;
            }
            var start = i;
            string value;
            if (text[i] == '"')
            {
                i++;
                var builder = new StringBuilder();
                while (i < text.Length && text[i] != '"')
                {
                    if (text[i] == '\\' && i + 1 < text.Length && text[i + 1] is '"' or '\\') i++;
                    builder.Append(text[i++]);
                }
                if (i >= text.Length) throw new InvalidDataException("Unclosed quoted string.");
                i++;
                value = builder.ToString();
            }
            else if ("{}[]=,".Contains(text[i])) value = text[i++].ToString();
            else
            {
                while (i < text.Length && !char.IsWhiteSpace(text[i]) && !"{}[]=,\"".Contains(text[i])) i++;
                value = text[start..i];
            }
            Tokens.Add(new(value, start, i));
            if (Tokens.Count > 300000) throw new InvalidDataException("Too many Valve text tokens.");
        }
    }

    public string Take()
    {
        if (End) throw new InvalidDataException("Unexpected end of Valve text.");
        return Tokens[Position++].Value;
    }

    public bool Accept(string value)
    {
        if (!string.Equals(Peek, value, StringComparison.OrdinalIgnoreCase)) return false;
        Position++;
        return true;
    }

    public void Expect(string value)
    {
        if (!Accept(value)) throw new InvalidDataException($"Expected '{value}', found '{Peek}'.");
    }

    public object? ReadKv3(int depth = 0)
    {
        if (depth > 32) throw new InvalidDataException("KV3 nesting is too deep.");
        if (Accept("{"))
        {
            var result = new Dictionary<string, object?>(StringComparer.OrdinalIgnoreCase);
            while (!Accept("}"))
            {
                var key = Take();
                Expect("=");
                if (!result.TryAdd(key, ReadKv3(depth + 1))) throw new InvalidDataException($"Duplicate KV3 key: {key}");
                Accept(",");
            }
            return result;
        }
        if (Accept("["))
        {
            var result = new List<object?>();
            while (!Accept("]"))
            {
                result.Add(ReadKv3(depth + 1));
                if (Peek != "]") Expect(",");
                else Accept(",");
            }
            return result;
        }
        return Take();
    }
}
