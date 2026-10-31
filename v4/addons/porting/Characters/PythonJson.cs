using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>JSON text exactly as Python's <c>json.dumps</c> writes it.</summary>
/// <remarks>The model driver's data folder is named after a hash of this text, so a whole-number float must keep its
/// <c>.0</c>, exponents must read <c>1e-05</c>, and non-ASCII must be escaped, or an unchanged model would change its
/// include paths. The reports use the same writer so they remain directly comparable.</remarks>
internal static class PythonJson
{
    /// <summary>The compact form, separated by a bare comma and colon.</summary>
    public static string Compact(JsonNode? node)
    {
        var text = new StringBuilder();
        Write(text, node, null, 0);
        return text.ToString();
    }

    /// <summary>The indented form of <c>json.dumps(value, indent=2)</c>.</summary>
    public static string Indented(JsonNode? node)
    {
        var text = new StringBuilder();
        Write(text, node, 2, 0);
        return text.ToString();
    }

    private static void Write(StringBuilder text, JsonNode? node, int? indent, int level)
    {
        switch (node)
        {
            case null: text.Append("null"); break;
            case JsonObject map: WriteObject(text, map, indent, level); break;
            case JsonArray array: WriteArray(text, array, indent, level); break;
            default: WriteValue(text, (JsonValue)node); break;
        }
    }

    private static void Break(StringBuilder text, int? indent, int level)
    {
        if (indent is { } width) text.Append('\n').Append(' ', width * level);
    }

    private static void WriteObject(StringBuilder text, JsonObject map, int? indent, int level)
    {
        if (map.Count == 0)
        {
            text.Append("{}");
            return;
        }
        text.Append('{');
        var first = true;
        foreach (var (key, value) in map)
        {
            if (!first) text.Append(',');
            first = false;
            Break(text, indent, level + 1);
            WriteString(text, key);
            text.Append(indent is null ? ":" : ": ");
            Write(text, value, indent, level + 1);
        }
        Break(text, indent, level);
        text.Append('}');
    }

    private static void WriteArray(StringBuilder text, JsonArray array, int? indent, int level)
    {
        if (array.Count == 0)
        {
            text.Append("[]");
            return;
        }
        text.Append('[');
        for (var index = 0; index < array.Count; index++)
        {
            if (index > 0) text.Append(',');
            Break(text, indent, level + 1);
            Write(text, array[index], indent, level + 1);
        }
        Break(text, indent, level);
        text.Append(']');
    }

    private static void WriteValue(StringBuilder text, JsonValue value)
    {
        if (value.TryGetValue<JsonElement>(out var element))
        {
            WriteElement(text, element);
            return;
        }
        switch (value.GetValueKind())
        {
            case JsonValueKind.String: WriteString(text, value.GetValue<string>()); break;
            case JsonValueKind.True: text.Append("true"); break;
            case JsonValueKind.False: text.Append("false"); break;
            case JsonValueKind.Null: text.Append("null"); break;
            default:
                if (value.TryGetValue<double>(out var number)) text.Append(Repr(number));
                else if (value.TryGetValue<float>(out var single)) text.Append(Repr(single));
                else text.Append(value.ToJsonString());
                break;
        }
    }

    private static void WriteElement(StringBuilder text, JsonElement element)
    {
        switch (element.ValueKind)
        {
            case JsonValueKind.String: WriteString(text, element.GetString() ?? ""); break;
            case JsonValueKind.True: text.Append("true"); break;
            case JsonValueKind.False: text.Append("false"); break;
            case JsonValueKind.Null: text.Append("null"); break;
            case JsonValueKind.Number:
                var raw = element.GetRawText();
                if (raw.AsSpan().IndexOfAny(".eE") < 0) text.Append(raw);
                else text.Append(Repr(element.GetDouble()));
                break;
            default: Write(text, JsonNode.Parse(element.GetRawText()), null, 0); break;
        }
    }

    /// <summary>A string with everything outside printable ASCII escaped, as <c>ensure_ascii</c> requires.</summary>
    private static void WriteString(StringBuilder text, string value)
    {
        text.Append('"');
        foreach (var character in value)
            switch (character)
            {
                case '"': text.Append("\\\""); break;
                case '\\': text.Append("\\\\"); break;
                case '\n': text.Append("\\n"); break;
                case '\r': text.Append("\\r"); break;
                case '\t': text.Append("\\t"); break;
                case '\f': text.Append("\\f"); break;
                case '\b': text.Append("\\b"); break;
                default:
                    if (character is >= ' ' and <= '~') text.Append(character);
                    else text.Append("\\u").Append(((int)character).ToString("x4", CultureInfo.InvariantCulture));
                    break;
            }
        text.Append('"');
    }

    /// <summary>A float as <c>repr</c> shows it: the shortest digits that round trip, always marked as a float.</summary>
    public static string Repr(double value)
    {
        if (double.IsNaN(value)) return "NaN";
        if (double.IsInfinity(value)) return value > 0 ? "Infinity" : "-Infinity";
        if (value == 0) return double.IsNegative(value) ? "-0.0" : "0.0";
        var round = Math.Abs(value).ToString("R", CultureInfo.InvariantCulture);
        var exponent = 0;
        var at = round.IndexOf('E');
        if (at >= 0)
        {
            exponent = int.Parse(round[(at + 1)..], CultureInfo.InvariantCulture);
            round = round[..at];
        }
        var dot = round.IndexOf('.');
        var digits = dot < 0 ? round : round[..dot] + round[(dot + 1)..];
        var point = (dot < 0 ? round.Length : dot) + exponent;
        var trimmed = digits.TrimStart('0');
        point -= digits.Length - trimmed.Length;
        digits = trimmed.TrimEnd('0');
        var sign = value < 0 ? "-" : "";
        if (point is <= -4 or > 16)
        {
            var mantissa = digits.Length > 1 ? digits[..1] + "." + digits[1..] : digits;
            var power = point - 1;
            return $"{sign}{mantissa}e{(power < 0 ? "-" : "+")}{Math.Abs(power).ToString("00", CultureInfo.InvariantCulture)}";
        }
        if (point <= 0) return sign + "0." + new string('0', -point) + digits;
        if (point >= digits.Length) return sign + digits + new string('0', point - digits.Length) + ".0";
        return sign + digits[..point] + "." + digits[point..];
    }
}

/// <summary>Reads the JSON a port reads with a Python subscript, where a missing key is a fault rather than a default.</summary>
internal static class JsonKeys
{
    /// <summary>A key the data must have. Its absence is reported as Python's <c>KeyError</c> shows it, quoted.</summary>
    public static JsonNode? Need(this JsonNode? node, string key) =>
        node is JsonObject map && map.TryGetPropertyValue(key, out var value) ? value : throw new KeyNotFoundException($"'{key}'");

    /// <summary>A JSON array of text.</summary>
    public static JsonArray Strings(IEnumerable<string> values) => new(values.Select(value => (JsonNode?)value).ToArray());

    /// <summary>A JSON array of whole numbers.</summary>
    public static JsonArray Integers(IEnumerable<int> values) => new(values.Select(value => (JsonNode?)value).ToArray());

    /// <summary>A JSON array of floats.</summary>
    public static JsonArray Numbers(params double[] values) => new(values.Select(value => (JsonNode?)value).ToArray());

    /// <summary>A vector as a JSON array of three floats.</summary>
    public static JsonArray Vector(V3 value) => Numbers(value.X, value.Y, value.Z);

    /// <summary>A quaternion as a JSON array of four floats.</summary>
    public static JsonArray Quaternion(Q4 value) => Numbers(value.X, value.Y, value.Z, value.W);

    /// <summary>A transform as nested rows, the form <c>ndarray.tolist()</c> gives it.</summary>
    public static JsonArray Rows(M4 matrix)
    {
        var rows = new JsonArray();
        for (var row = 0; row < 4; row++) rows.Add(Numbers(matrix[row, 0], matrix[row, 1], matrix[row, 2], matrix[row, 3]));
        return rows;
    }

    /// <summary>A transform read back from nested rows.</summary>
    public static M4 Matrix(JsonNode? rows)
    {
        var cells = new double[16];
        var table = rows.Arr();
        for (var row = 0; row < 4; row++)
        {
            var values = table[row].Arr();
            for (var column = 0; column < 4; column++) cells[row * 4 + column] = values[column].Dbl();
        }
        return new(cells);
    }

    /// <summary>A value as Python's <c>str</c> prints it into a message, where absent reads <c>None</c>.</summary>
    public static string Shown(JsonNode? node) => node switch
    {
        null => "None",
        JsonValue value when value.GetValueKind() == JsonValueKind.String => value.GetValue<string>(),
        JsonValue value when value.GetValueKind() == JsonValueKind.True => "True",
        JsonValue value when value.GetValueKind() == JsonValueKind.False => "False",
        _ => node.ToJsonString()
    };
}
