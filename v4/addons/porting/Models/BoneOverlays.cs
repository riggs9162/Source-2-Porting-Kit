using System.Globalization;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>Compiles configured bone-local display surfaces into native model geometry.</summary>
public static partial class BoneOverlays
{
    [GeneratedRegex(@"^[a-zA-Z0-9_./]+\z")]
    private static partial Regex MaterialPattern();

    [GeneratedRegex(@"^[a-zA-Z0-9_]+\z")]
    private static partial Regex NamePattern();

    [GeneratedRegex(@"""([^""]*)""|(\S+)")]
    private static partial Regex NodeField();

    /// <summary>The configured skin material, repeating the last entry for extra skins.</summary>
    public static string OverlaySkinMaterial(JsonObject surface, int index)
    {
        var skins = surface.Has("skins") ? surface.Get("skins") as JsonArray : new JsonArray(surface.Get("material").Str());
        if (skins is null || skins.Count == 0) throw new InvalidDataException("Overlay skins must be a nonempty material list");
        var material = skins[Math.Min(index, skins.Count - 1)].Str();
        return MaterialPattern().IsMatch(material) ? material : throw new InvalidDataException("Invalid overlay skin material path");
    }

    private static Dictionary<int, (string Name, int Parent)> Nodes(string header)
    {
        var body = header[(header.IndexOf("nodes\n", StringComparison.Ordinal) + "nodes\n".Length)..];
        body = body[..body.IndexOf("\nend", StringComparison.Ordinal)];
        var nodes = new Dictionary<int, (string, int)>();
        foreach (var line in PythonText.SplitLines(body))
        {
            var fields = NodeField().Matches(line).Select(match => match.Groups[1].Success ? match.Groups[1].Value : match.Groups[2].Value).ToArray();
            if (fields.Length != 3) throw new InvalidDataException($"Invalid SMD node line: {line}");
            nodes[int.Parse(fields[0], CultureInfo.InvariantCulture)] = (fields[1], int.Parse(fields[2], CultureInfo.InvariantCulture));
        }
        return nodes;
    }

    private static Dictionary<int, M4> Transforms(string header)
    {
        var body = header[(header.IndexOf("skeleton\n", StringComparison.Ordinal) + "skeleton\n".Length)..];
        body = body[..body.IndexOf("\nend", StringComparison.Ordinal)];
        var transforms = new Dictionary<int, M4>();
        foreach (var line in PythonText.SplitLines(body))
        {
            var fields = line.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
            if (fields.Length != 7) continue;
            var values = fields.Skip(1).Select(field => double.Parse(field, CultureInfo.InvariantCulture)).ToArray();
            double sx = Math.Sin(values[3]), sy = Math.Sin(values[4]), sz = Math.Sin(values[5]);
            double cx = Math.Cos(values[3]), cy = Math.Cos(values[4]), cz = Math.Cos(values[5]);
            transforms[int.Parse(fields[0], CultureInfo.InvariantCulture)] = new([
                cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx, values[0],
                sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx, values[1],
                -sy, cy * sx, cy * cx, values[2],
                0, 0, 0, 1]);
        }
        return transforms;
    }

    /// <summary>Writes rigid quad SMDs sharing the reference skeleton and its bind transforms.</summary>
    /// <remarks>Each bone resolves once, including independently rooted chains.</remarks>
    public static List<string> WriteBoneOverlays(string reference, IReadOnlyList<JsonObject> surfaces, string output)
    {
        if (surfaces.Count == 0) return [];
        var text = PythonText.Read(reference);
        var header = text[..text.IndexOf("triangles\n", StringComparison.Ordinal)];
        var nodes = Nodes(header);
        var transforms = Transforms(header);
        var world = new Dictionary<int, M4>();
        M4 Resolve(int index)
        {
            if (world.TryGetValue(index, out var known)) return known;
            var parent = nodes[index].Parent;
            return world[index] = parent < 0 ? transforms[index] : Resolve(parent) * transforms[index];
        }
        var names = new Dictionary<string, int>(StringComparer.Ordinal);
        foreach (var (index, (name, _)) in nodes) names[name] = index;
        var paths = new List<string>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var surface in surfaces)
        {
            var name = surface.Get("name").Str();
            var material = surface.Get("material").Str();
            if (!NamePattern().IsMatch(name) || seen.Contains(name)) throw new InvalidDataException("Overlay names must be unique safe identifiers");
            if (!MaterialPattern().IsMatch(material)) throw new InvalidDataException("Invalid overlay material path");
            seen.Add(name);
            var bone = names[surface.Get("bone").Str()];
            var vertices = surface.Get("vertices").Arr();
            var uv = surface.Get("uv").Arr();
            if (vertices.Count != 4 || uv.Count != 4 || vertices.Any(row => row.Arr().Count != 3) || uv.Any(row => row.Arr().Count != 2) ||
                vertices.Concat(uv).Any(row => row.Doubles().Any(value => !double.IsFinite(value))))
                throw new InvalidDataException("Overlay quads require four finite positions and UV pairs");
            var matrix = Resolve(bone);
            var positions = vertices.Select(row => matrix.Point(row.Vec3())).ToArray();
            var normal = (positions[1] - positions[0]).Cross(positions[2] - positions[0]);
            var length = normal.Length;
            if (length < 1e-8) throw new InvalidDataException("Overlay quad has no surface area");
            normal /= length;
            var lines = new StringBuilder(header.TrimEnd()).Append("\ntriangles\n");
            foreach (var triangle in new[] { new[] { 0, 1, 2 }, [0, 2, 3] })
            {
                lines.Append(material).Append('\n');
                foreach (var index in triangle)
                {
                    var (x, y, z) = positions[index];
                    var coordinate = uv[index].Doubles();
                    lines.Append(string.Join(' ', Fmt.Int(bone), Fmt.F9(x), Fmt.F9(y), Fmt.F9(z), Fmt.F9(normal.X), Fmt.F9(normal.Y), Fmt.F9(normal.Z), Fmt.F9(coordinate[0]), Fmt.F9(coordinate[1])));
                    lines.Append($" 1 {bone} 1\n");
                }
            }
            var path = Path.Combine(output, name + ".smd");
            PythonText.Write(path, lines.Append("end\n").ToString());
            paths.Add(path);
        }
        return paths;
    }
}
