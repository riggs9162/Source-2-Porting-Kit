using System.Text;

namespace PortingKit.Addons.Soundscapes;

/// <summary>Reads the KV3 text that decompiled <c>.vsndevts</c> files hold.</summary>
/// <remarks>Values come back as a string, a <c>List&lt;object&gt;</c> or a <c>Dictionary&lt;string, object&gt;</c>.
/// Numbers and booleans stay text so a value is written back exactly as Valve wrote it.</remarks>
public sealed class Kv3Text
{
    private readonly List<(string Text, bool Quoted)> tokens = [];
    private int index;

    private Kv3Text(string text)
    {
        var position = 0;
        var builder = new StringBuilder();
        while (position < text.Length)
        {
            var current = text[position];
            if (char.IsWhiteSpace(current)) { position++; continue; }
            if (current == '<' && text.AsSpan(position).StartsWith("<!--"))
            {
                var end = text.IndexOf("-->", position, StringComparison.Ordinal);
                position = end < 0 ? text.Length : end + 3;
                continue;
            }
            if (current == '/' && position + 1 < text.Length && text[position + 1] == '/')
            {
                var end = text.IndexOf('\n', position);
                position = end < 0 ? text.Length : end;
                continue;
            }
            if (current == '/' && position + 1 < text.Length && text[position + 1] == '*')
            {
                var end = text.IndexOf("*/", position, StringComparison.Ordinal);
                position = end < 0 ? text.Length : end + 2;
                continue;
            }
            if (current is '{' or '}' or '[' or ']' or '=' or ',')
            {
                tokens.Add((current.ToString(), false));
                position++;
                continue;
            }
            builder.Clear();
            if (current == '"')
            {
                position++;
                while (position < text.Length && text[position] != '"')
                {
                    if (text[position] == '\\' && position + 1 < text.Length) position++;
                    builder.Append(text[position++]);
                }
                position++;
                tokens.Add((builder.ToString(), true));
                continue;
            }
            while (position < text.Length && !char.IsWhiteSpace(text[position]) && text[position] is not ('{' or '}' or '[' or ']' or '=' or ',' or '"'))
                builder.Append(text[position++]);
            tokens.Add((builder.ToString(), false));
        }
    }

    /// <summary>Parses a document whose root is an object.</summary>
    public static Dictionary<string, object> Parse(string text)
    {
        var reader = new Kv3Text(text);
        return reader.Value() as Dictionary<string, object> ?? throw new InvalidDataException("The KV3 document does not start with an object.");
    }

    private bool Punctuation(string text) => index < tokens.Count && !tokens[index].Quoted && tokens[index].Text == text;

    private (string Text, bool Quoted) Next() => index < tokens.Count ? tokens[index++] : throw new InvalidDataException("The KV3 document ends early.");

    private object Value()
    {
        var token = Next();
        if (token.Quoted) return token.Text;
        if (token.Text == "{")
        {
            var fields = new Dictionary<string, object>(StringComparer.Ordinal);
            while (!Punctuation("}"))
            {
                if (Punctuation(",")) { index++; continue; }
                var key = Next().Text;
                if (!Punctuation("=")) throw new InvalidDataException($"Expected '=' after '{key}' in KV3.");
                index++;
                fields[key] = Value();
            }
            index++;
            return fields;
        }
        if (token.Text == "[")
        {
            var items = new List<object>();
            while (!Punctuation("]"))
            {
                if (Punctuation(",")) { index++; continue; }
                items.Add(Value());
            }
            index++;
            return items;
        }
        return token.Text;
    }
}
