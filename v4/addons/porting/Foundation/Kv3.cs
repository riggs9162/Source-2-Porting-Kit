using System.Globalization;
using System.Numerics;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>Reads the nested text KV3 structures emitted by Source 2 Viewer.</summary>
/// <remarks>Typed resources lose their flag, binary blobs read as null, and duplicate keys keep the last value.</remarks>
public static partial class Kv3
{
    [GeneratedRegex(@"<!--\s*kv3\b.*?-->", RegexOptions.Singleline)]
    private static partial Regex Header();

    /// <summary>Reads every KV3 document in a block dump, ignoring its command-line banner.</summary>
    public static List<JsonObject> ParseDocuments(string text)
    {
        var documents = new List<JsonObject>();
        var position = 0;
        while (true)
        {
            var header = Header().Match(text, position);
            Reader reader;
            if (header.Success) reader = new(text, header.Index + header.Length);
            else if (documents.Count == 0 && text.TrimStart().StartsWith('{')) reader = new(text, 0);
            else break;
            if (reader.Value() is not JsonObject value)
                throw new InvalidDataException("A model metadata document must be an object.");
            documents.Add(value);
            position = reader.Position;
            if (!header.Success) break;
        }
        return documents;
    }

    private sealed class Reader(string text, int position)
    {
        public int Position { get; private set; } = position;

        private bool Starts(string value) => string.CompareOrdinal(text, Position, value, 0, value.Length) == 0;

        private void Space()
        {
            while (Position < text.Length)
            {
                if (char.IsWhiteSpace(text[Position])) Position++;
                else if (Starts("//"))
                {
                    var end = text.IndexOf('\n', Position);
                    Position = end < 0 ? text.Length : end + 1;
                }
                else if (Starts("/*"))
                {
                    var end = text.IndexOf("*/", Position + 2, StringComparison.Ordinal);
                    if (end < 0) return;
                    Position = end + 2;
                }
                else if (Starts("<!--"))
                {
                    var end = text.IndexOf("-->", Position + 4, StringComparison.Ordinal);
                    if (end < 0) return;
                    Position = end + 3;
                }
                else return;
            }
        }

        private static bool IsPunctuation(char c) => c is '{' or '}' or '[' or ']' or '=' or ',' or ':';

        private string Token()
        {
            Space();
            if (Position >= text.Length) throw Invalid();
            var start = Position;
            var first = text[start];
            if (first == '"')
            {
                var at = start + 1;
                while (at < text.Length && text[at] != '"')
                {
                    if (text[at] == '\\')
                    {
                        at++;
                        if (at >= text.Length || text[at] == '\n') throw Invalid();
                    }
                    at++;
                }
                if (at >= text.Length) throw Invalid();
                Position = at + 1;
                return text[start..Position];
            }
            if (IsPunctuation(first))
            {
                Position++;
                return first.ToString();
            }
            var cursor = start;
            while (cursor < text.Length && !char.IsWhiteSpace(text[cursor]) && !IsPunctuation(text[cursor]) && text[cursor] is not ('"' or '#'))
                cursor++;
            if (cursor == start) throw Invalid();
            Position = cursor;
            return text[start..cursor];
        }

        private InvalidDataException Invalid() => new($"Invalid KV3 token at character {Position}.");

        public JsonNode? Value()
        {
            Space();
            if (Starts("\"\"\""))
            {
                var start = Position + 3;
                var end = text.IndexOf("\"\"\"", start, StringComparison.Ordinal);
                if (end < 0) throw new InvalidDataException("Unclosed multiline KV3 string.");
                Position = end + 3;
                return text[start..end];
            }
            if (Starts("#["))
            {
                var end = text.IndexOf(']', Position + 2);
                if (end < 0) throw new InvalidDataException("Unclosed KV3 binary blob.");
                Position = end + 1;
                return null;
            }
            var token = Token();
            if (token == "{") return Map();
            if (token == "[") return List();
            if (token.StartsWith('"')) return Unescape(token);
            Space();
            if (Position < text.Length && text[Position] == ':')
            {
                Position++;
                return Value();
            }
            return token switch { "true" => true, "false" => false, "null" => null, _ => Scalar(token) };
        }

        private JsonObject Map()
        {
            var result = new JsonObject();
            while (true)
            {
                var key = Token();
                if (key == "}") return result;
                if (key == ",") continue;
                var separator = Token();
                if (separator != "=") throw new InvalidDataException($"Expected '=', found '{separator}'.");
                result[key.StartsWith('"') ? Unescape(key) : key] = Value();
            }
        }

        private JsonArray List()
        {
            var result = new JsonArray();
            while (true)
            {
                Space();
                if (Position >= text.Length) throw new InvalidDataException("Unclosed KV3 array.");
                var next = text[Position];
                if (next == ']')
                {
                    Position++;
                    return result;
                }
                if (next == ',')
                {
                    Position++;
                    continue;
                }
                result.Add(Value());
            }
        }

        private static string Unescape(string token)
        {
            var inner = token[1..^1];
            if (!inner.Contains('\\')) return inner;
            var result = new StringBuilder(inner.Length);
            for (var i = 0; i < inner.Length; i++)
            {
                var c = inner[i];
                if (c != '\\' || i + 1 >= inner.Length)
                {
                    result.Append(c);
                    continue;
                }
                var next = inner[++i];
                switch (next)
                {
                    case 'n': result.Append('\n'); break;
                    case 't': result.Append('\t'); break;
                    case 'r': result.Append('\r'); break;
                    case 'b': result.Append('\b'); break;
                    case 'f': result.Append('\f'); break;
                    case 'u' when i + 4 < inner.Length && ushort.TryParse(inner.AsSpan(i + 1, 4), NumberStyles.HexNumber, CultureInfo.InvariantCulture, out var code):
                        result.Append((char)code);
                        i += 4;
                        break;
                    default: result.Append(next); break;
                }
            }
            return result.ToString();
        }

        private static JsonNode? Scalar(string token)
        {
            var negative = token.StartsWith('-');
            var digits = negative || token.StartsWith('+') ? token[1..] : token;
            if (digits.StartsWith("0x", StringComparison.OrdinalIgnoreCase) &&
                BigInteger.TryParse("0" + digits[2..], NumberStyles.HexNumber, CultureInfo.InvariantCulture, out var hex))
                return Integer(negative ? -hex : hex);
            if (digits.Length > 0 && digits.All(char.IsAsciiDigit) && (digits[0] != '0' || digits.All(c => c == '0')) &&
                BigInteger.TryParse(digits, NumberStyles.None, CultureInfo.InvariantCulture, out var whole))
                return Integer(negative ? -whole : whole);
            if (digits.Equals("nan", StringComparison.OrdinalIgnoreCase) || digits.Equals("inf", StringComparison.OrdinalIgnoreCase) ||
                digits.Equals("infinity", StringComparison.OrdinalIgnoreCase))
                return (negative ? "-" : "") + digits[..3].ToLowerInvariant();
            return double.TryParse(token, NumberStyles.Float, CultureInfo.InvariantCulture, out var number) ? number : token;
        }

        private static JsonNode Integer(BigInteger value)
        {
            if (value >= long.MinValue && value <= long.MaxValue) return (long)value;
            if (value >= 0 && value <= ulong.MaxValue) return (ulong)value;
            return (double)value;
        }
    }
}
