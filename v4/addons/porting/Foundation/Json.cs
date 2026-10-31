using System.Globalization;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>Reads KV3 and JSON trees with the forgiving lookups the exported documents need.</summary>
public static class Json
{
    public static readonly JsonSerializerOptions Options = new()
    {
        WriteIndented = true,
        IndentSize = 2,
        MaxDepth = 512,
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping
    };

    private static readonly JsonDocumentOptions ReadOptions = new() { MaxDepth = 512 };

    public static JsonObject Load(string path) => Parse(File.ReadAllBytes(path));

    public static JsonObject Parse(ReadOnlySpan<byte> utf8)
    {
        if (utf8.Length >= 3 && utf8[0] == 0xEF && utf8[1] == 0xBB && utf8[2] == 0xBF) utf8 = utf8[3..];
        return JsonNode.Parse(utf8, null, ReadOptions)?.AsObject() ?? throw new InvalidDataException("Expected a JSON object.");
    }

    /// <summary>Replaces a file only after its complete contents reach the disk.</summary>
    public static void Save(string path, JsonNode node)
    {
        var directory = Path.GetDirectoryName(path);
        if (!string.IsNullOrEmpty(directory)) Directory.CreateDirectory(directory);
        var temporary = path + ".tmp";
        using (var stream = new FileStream(temporary, FileMode.Create, FileAccess.Write, FileShare.None))
        {
            JsonSerializer.Serialize(stream, node, Options);
            stream.WriteByte((byte)'\n');
            stream.Flush(true);
        }
        File.Move(temporary, path, true);
    }

    public static JsonNode? Get(this JsonNode? node, string key) =>
        node is JsonObject map && map.TryGetPropertyValue(key, out var value) ? value : null;

    public static JsonObject Obj(this JsonNode? node) => node as JsonObject ?? [];
    public static JsonArray Arr(this JsonNode? node) => node as JsonArray ?? [];
    public static bool Has(this JsonNode? node, string key) => node is JsonObject map && map.ContainsKey(key);

    public static string Str(this JsonNode? node, string fallback = "") => node is not JsonValue value
        ? fallback
        : value.GetValueKind() switch
        {
            JsonValueKind.String => value.GetValue<string>(),
            JsonValueKind.Number => value.ToJsonString(),
            JsonValueKind.True => "True",
            JsonValueKind.False => "False",
            _ => fallback
        };

    public static double Dbl(this JsonNode? node, double fallback = 0.0)
    {
        if (node is not JsonValue value) return fallback;
        switch (value.GetValueKind())
        {
            case JsonValueKind.Number: return value.GetValue<double>();
            case JsonValueKind.True: return 1.0;
            case JsonValueKind.False: return 0.0;
            case JsonValueKind.String:
                var text = value.GetValue<string>();
                if (text.Equals("nan", StringComparison.OrdinalIgnoreCase)) return double.NaN;
                if (text.Equals("inf", StringComparison.OrdinalIgnoreCase)) return double.PositiveInfinity;
                if (text.Equals("-inf", StringComparison.OrdinalIgnoreCase)) return double.NegativeInfinity;
                return double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out var parsed) ? parsed : fallback;
            default: return fallback;
        }
    }

    /// <summary>Reads an integer; values above the signed range wrap, preserving 64-bit masks.</summary>
    public static long Lng(this JsonNode? node, long fallback = 0)
    {
        if (node is not JsonValue value) return fallback;
        if (value.TryGetValue<long>(out var signed)) return signed;
        if (value.TryGetValue<ulong>(out var unsigned)) return unchecked((long)unsigned);
        return (long)value.Dbl(fallback);
    }

    public static int Int(this JsonNode? node, int fallback = 0) => (int)node.Lng(fallback);

    /// <summary>Follows Python truthiness: null, false, zero and empty values are false.</summary>
    public static bool Bool(this JsonNode? node, bool fallback = false) => node switch
    {
        null => fallback,
        JsonArray array => array.Count > 0,
        JsonObject map => map.Count > 0,
        JsonValue value => value.GetValueKind() switch
        {
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            JsonValueKind.Number => value.GetValue<double>() != 0,
            JsonValueKind.String => value.GetValue<string>().Length > 0,
            _ => false
        },
        _ => false
    };

    public static double[] Doubles(this JsonNode? node) => node.Arr().Select(item => item.Dbl()).ToArray();
    public static V3 Vec3(this JsonNode? node) => node.Arr() is [var x, var y, var z] ? new(x.Dbl(), y.Dbl(), z.Dbl()) : throw new InvalidDataException("Expected three numbers.");
    public static Q4 Quat(this JsonNode? node) => node.Arr() is [var x, var y, var z, var w] ? new(x.Dbl(), y.Dbl(), z.Dbl(), w.Dbl()) : throw new InvalidDataException("Expected four numbers.");
}
