using System.Globalization;
using System.Text;
using System.Text.Json;

namespace PortingKit.Addons.Hotspot;

public static class HotspotFormats
{
    public const int Source2Extent = 32768;
    public const string Kv3Header = "<!-- kv3 encoding:text:version{e21c7f3c-8a33-41c5-9977-a76d3a32aa0d} format:generic:version{7412167c-06e9-4698-aff2-e63eb59037e7} -->";

    public static HotspotDocument Read(string path, int width = 0, int height = 0)
    {
        if (new FileInfo(path).Length > 4 * 1024 * 1024) throw new InvalidDataException("Hotspot input exceeds 4 MiB.");
        var text = File.ReadAllText(path);
        if (Path.GetExtension(path).Equals(".json", StringComparison.OrdinalIgnoreCase))
        {
            var doc = JsonSerializer.Deserialize<HotspotDocument>(text, HotspotDocument.JsonOptions)
                ?? throw new InvalidDataException("Empty hotspot document.");
            doc.CheckStructure();
            return doc;
        }
        return Parse(text, width, height);
    }

    public static HotspotDocument Parse(string text, int width, int height)
    {
        new HotspotDocument(width, height, []).CheckStructure();
        var tokens = new ValveText(text);
        var source2 = tokens.Peek == "{";
        var result = source2 ? ReadSource2(tokens, width, height) : ReadHammer(tokens, width, height);
        if (!tokens.End) throw new InvalidDataException($"Unexpected trailing token: {tokens.Peek}");
        result.CheckStructure();
        if (!source2)
            result = result with { Comments = text.Split('\n').Select(l => l.Trim()).Where(l => l.StartsWith("//", StringComparison.Ordinal)).Select(l => l[2..].Trim()).ToArray() };
        return result;
    }

    private static HotspotDocument ReadHammer(ValveText reader, int width, int height)
    {
        reader.Expect("Rectangles");
        reader.Expect("{");
        var rects = new List<HotspotRect>();
        while (!reader.Accept("}"))
        {
            reader.Expect("rectangle");
            reader.Expect("{");
            var values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            while (!reader.Accept("}"))
            {
                var key = reader.Take();
                if (!new[] { "min", "max", "rotate", "reflect", "alt" }.Contains(key, StringComparer.OrdinalIgnoreCase))
                    throw new InvalidDataException($"Unsupported rectangle field: {key}");
                var value = reader.Take();
                if ((key.Equals("min", StringComparison.OrdinalIgnoreCase) || key.Equals("max", StringComparison.OrdinalIgnoreCase)) && !value.Any(char.IsWhiteSpace))
                    value += " " + reader.Take();
                if (!values.TryAdd(key, value)) throw new InvalidDataException($"Duplicate rectangle field: {key}");
            }
            if (!values.TryGetValue("min", out var min) || !values.TryGetValue("max", out var max))
                throw new InvalidDataException("Every rectangle needs min and max coordinates.");
            var a = Pair(min); var b = Pair(max);
            bool Flag(string key) => values.TryGetValue(key, out var value) && Number(value) != 0;
            rects.Add(FromEdges(a[0], a[1], b[0], b[1]) with { Rotate = Flag("rotate"), Reflect = Flag("reflect"), Alt = Flag("alt") });
            if (rects.Count > HotspotDocument.MaxRectangles) throw new InvalidDataException("Too many rectangles.");
        }
        return new(width, height, rects.ToArray()) { SourceFormat = "hammerpp" };
    }

    private static HotspotDocument ReadSource2(ValveText reader, int width, int height)
    {
        var root = Object(reader.ReadKv3());
        var sets = Array(Get(root, "RectangleSets"));
        var rects = new List<HotspotRect>();
        var warnings = new HashSet<string>();
        Unknown(root, ["RectangleSets"], warnings);
        foreach (var entry in sets)
        {
            var set = Object(entry);
            Unknown(set, ["name", "properties", "rectangles"], warnings);
            if (set.TryGetValue("name", out var name) && name is string s && s.Length != 0)
                warnings.Add("Named Source 2 rectangle sets are flattened into one editable sheet.");
            var defaults = Properties(set.GetValueOrDefault("properties"), warnings);
            foreach (var item in Array(Get(set, "rectangles")))
            {
                var values = Object(item);
                Unknown(values, ["min", "max", "inset", "properties"], warnings);
                var min = KvPair(Get(values, "min")); var max = KvPair(Get(values, "max"));
                var inset = values.TryGetValue("inset", out var rawInset) ? KvPair(rawInset) : new[] { 0, 0 };
                var props = new Dictionary<string, bool>(defaults);
                foreach (var pair in Properties(values.GetValueOrDefault("properties"), warnings)) props[pair.Key] = pair.Value;
                int Px(int n, int extent) => checked((int)Math.Round((double)n * extent / Source2Extent));
                rects.Add(FromEdges(Px(min[0], width), Px(min[1], height), Px(max[0], width), Px(max[1], height)) with
                {
                    Rotate = props.GetValueOrDefault("allowRotation"), Tiling = props.GetValueOrDefault("allowTiling"),
                    InsetX = Px(inset[0], width), InsetY = Px(inset[1], height)
                });
                if (rects.Count > HotspotDocument.MaxRectangles) throw new InvalidDataException("Too many rectangles.");
            }
        }
        return new(width, height, rects.ToArray()) { SourceFormat = "source2", ImportWarnings = warnings.ToArray() };
    }

    private static Dictionary<string, bool> Properties(object? value, HashSet<string> warnings)
    {
        var result = new Dictionary<string, bool>(StringComparer.OrdinalIgnoreCase);
        if (value is null || value is "null") return result;
        var props = Object(value);
        Unknown(props, ["allowRotation", "allowTiling"], warnings);
        foreach (var key in new[] { "allowRotation", "allowTiling" })
            if (props.TryGetValue(key, out var field))
                result[key] = field is string s && bool.TryParse(s, out var b) ? b : throw new InvalidDataException($"Invalid boolean: {key}");
        return result;
    }

    private static void Unknown(Dictionary<string, object?> obj, string[] supported, HashSet<string> warnings)
    {
        foreach (var key in obj.Keys.Where(k => !supported.Contains(k, StringComparer.OrdinalIgnoreCase)))
            warnings.Add($"Unsupported Source 2 field '{key}' is omitted on export.");
    }

    private static object? Get(Dictionary<string, object?> obj, string key) => obj.TryGetValue(key, out var value) ? value : throw new InvalidDataException($"Missing {key}.");
    private static Dictionary<string, object?> Object(object? obj) => obj as Dictionary<string, object?> ?? throw new InvalidDataException("Expected a KV3 object.");
    private static List<object?> Array(object? obj) => obj as List<object?> ?? throw new InvalidDataException("Expected a KV3 array.");
    private static int[] KvPair(object? obj)
    {
        var values = Array(obj);
        if (values.Count != 2) throw new InvalidDataException("Coordinates must have exactly two values.");
        return values.Select(v => Number(v as string ?? "")).ToArray();
    }
    private static int[] Pair(string text)
    {
        var values = text.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        if (values.Length != 2) throw new InvalidDataException("Coordinates must have exactly two values.");
        return values.Select(Number).ToArray();
    }
    private static int Number(string text) => double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out var value)
        && double.IsFinite(value) && value >= int.MinValue && value <= int.MaxValue
        ? checked((int)Math.Round(value)) : throw new InvalidDataException($"Invalid coordinate: {text}");
    private static HotspotRect FromEdges(int x, int y, int right, int bottom) => new(Math.Min(x, right), Math.Min(y, bottom),
        checked((int)Math.Abs((long)right - x)), checked((int)Math.Abs((long)bottom - y)));

    public static string Write(HotspotDocument doc, string target, bool flags = true)
    {
        var errors = doc.Validate().Where(i => i.Level == "error").ToArray();
        if (errors.Length != 0) throw new InvalidDataException(string.Join(" ", errors.Select(e => e.Message)));
        if (target == "json") return JsonSerializer.Serialize(doc, HotspotDocument.JsonOptions) + "\n";
        if (target == "hammerpp")
        {
            var output = new StringBuilder();
            foreach (var comment in doc.Comments) output.Append("// ").AppendLine(comment);
            output.Append("Rectangles\n{\n");
            foreach (var r in doc.Rectangles)
            {
                output.Append($"\trectangle\n\t{{\n\t\tmin\t\t\"{r.X} {r.Y}\"\n\t\tmax\t\t\"{r.Right} {r.Bottom}\"\n");
                if (flags && r.Rotate) output.Append("\t\trotate\t1\n");
                if (flags && r.Reflect) output.Append("\t\treflect\t1\n");
                if (flags && r.Alt) output.Append("\t\talt\t1\n");
                output.Append("\t}\n");
            }
            return output.Append("}\n").ToString().Replace("\r\n", "\n");
        }
        if (target != "source2") throw new ArgumentException("Format must be hammerpp, source2, or json.");
        var builder = new StringBuilder(Kv3Header + "\n{\n\tRectangleSets = [\n\t\t{\n\t\t\tname = \"\"\n\t\t\tproperties = null\n\t\t\trectangles = [\n");
        int Units(long n, int extent) => (int)Math.Round((double)n * Source2Extent / extent);
        foreach (var r in doc.Rectangles)
        {
            builder.Append($"\t\t\t\t{{\n\t\t\t\t\tmin = [ {Units(r.X, doc.Width)}, {Units(r.Y, doc.Height)} ]\n");
            builder.Append($"\t\t\t\t\tmax = [ {Units(r.Right, doc.Width)}, {Units(r.Bottom, doc.Height)} ]\n");
            if (r.InsetX != 0 || r.InsetY != 0) builder.Append($"\t\t\t\t\tinset = [ {Units(r.InsetX, doc.Width)}, {Units(r.InsetY, doc.Height)} ]\n");
            builder.Append("\t\t\t\t\tproperties = ");
            if (!r.Rotate && !r.Tiling) builder.Append("null\n");
            else
            {
                builder.Append("{\n");
                if (r.Rotate) builder.Append("\t\t\t\t\t\tallowRotation = true\n");
                if (r.Tiling) builder.Append("\t\t\t\t\t\tallowTiling = true\n");
                builder.Append("\t\t\t\t\t}\n");
            }
            builder.Append("\t\t\t\t},\n");
        }
        return builder.Append("\t\t\t]\n\t\t},\n\t]\n}\n").ToString();
    }

    /// <summary>Replaces one output atomically. Optional backup preserves the previous file at path.bak.</summary>
    public static async Task SaveAsync(string path, string text, bool backup, CancellationToken token)
    {
        path = Path.GetFullPath(path);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var temp = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            await File.WriteAllTextAsync(temp, text, new UTF8Encoding(false), token);
            token.ThrowIfCancellationRequested();
            if (backup && File.Exists(path)) File.Copy(path, path + ".bak", true);
            File.Move(temp, path, true);
        }
        finally { if (File.Exists(temp)) File.Delete(temp); }
    }
}
