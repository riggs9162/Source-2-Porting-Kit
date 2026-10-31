using System.Globalization;
using System.Text;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>What a painted Source 2 blend needs beyond its two layers.</summary>
/// <param name="Mask">The blend mask's image, or its authored path before it is resolved.</param>
/// <param name="MaskConstant">The mask's value when it was authored as a literal.</param>
/// <param name="Repeat">How many times the second layer repeats across one tile of the first.</param>
/// <param name="MaskRepeat">How many times the mask repeats across one tile of the first layer.</param>
/// <param name="Notes">What could not carry over exactly.</param>
public sealed record VmatBlend(string? Mask, double? MaskConstant, (int Across, int Down) Repeat, (int Across, int Down) MaskRepeat, List<string> Notes);

/// <summary>Normalizes authored Source 2 blend layers into independent material documents.</summary>
public static partial class VmatLayers
{
    private const string TwoWayShader = "vr_simple_2way_blend.vfx";
    private const string TriplanarShader = "vr_simple_blend_to_triplanar.vfx";
    private const string MembraneShader = "vr_simple_blend_to_xen_membrane.vfx";

    [GeneratedRegex(@"^Texture(?:Color|Albedo|Diffuse)([A-Z])\z")]
    private static partial Regex LayerKey();

    [GeneratedRegex(@"^(?:Texture\w+|g_fl\w+|g_v\w+)[A-Z]\z")]
    private static partial Regex LayeredKey();

    /// <summary>Keeps shared parameters and exact layer overrides while excluding blend masks.</summary>
    /// <remarks>Only explicit albedo-bearing layers qualify. Missing layer maps stay absent rather than inheriting another layer through filename inference.</remarks>
    public static List<(string Layer, string Document)> LayerDocuments(string text)
    {
        var document = KeyValues.Parse(text);
        if (document.Pairs.Count != 1 || document.Pairs[0].Value is not KeyValueBlock block) return [];
        var fields = block.Scalars();
        var layers = new SortedSet<string>(StringComparer.Ordinal);
        foreach (var key in fields.Keys)
            if (LayerKey().Match(key) is { Success: true } match) layers.Add(match.Groups[1].Value);
        if (layers.Count == 0) return [];
        var ordered = layers.ToList();
        if (!layers.Contains("A") && VmatParser.KeyAliases["color"].Any(fields.ContainsKey)) ordered.Insert(0, "A");
        var aliases = VmatParser.KeyAliases.Values.SelectMany(names => names).Where(alias => !alias.EndsWith('A')).ToHashSet();
        aliases.UnionWith(["g_flRoughness", "g_flMetalness", "g_vColorTint"]);
        var layered = fields.Keys.Where(key => LayeredKey().IsMatch(key) && aliases.Contains(key[..^1])).ToHashSet();
        var outputs = new List<(string, string)>();
        foreach (var layer in ordered)
        {
            var values = new OrderedDictionary<string, string>();
            foreach (var (key, value) in fields)
                if (!layered.Contains(key) && key != "TextureMask") values[key] = value;
            foreach (var key in fields.Keys)
                if (layered.Contains(key) && key.EndsWith(layer, StringComparison.Ordinal)) values[key[..^1]] = fields[key];
            foreach (var names in VmatParser.KeyAliases.Values)
            {
                var candidate = names.Where(name => !name.EndsWith('A') && fields.ContainsKey(name + layer)).Select(name => name + layer).FirstOrDefault();
                if (candidate is null) continue;
                foreach (var name in names) values.Remove(name);
                values[names[0]] = fields[candidate];
            }
            if (fields.TryGetValue($"g_flMaterial{layer[0] - 'A'}EmissiveLevel", out var emissive)) values["g_flSelfIllumBrightness"] = emissive;
            var output = new StringBuilder("\"Layer0\"\n{\n");
            foreach (var (key, value) in values) output.Append('"').Append(key).Append("\" \"").Append(value).Append("\"\n");
            if (block.Block("SystemAttributes") is { } attributes)
            {
                output.Append("\"SystemAttributes\"\n{\n");
                foreach (var (key, value) in attributes.Scalars()) output.Append('"').Append(key).Append("\" \"").Append(value).Append("\"\n");
                output.Append("}\n");
            }
            outputs.Add((layer, output.Append("}\n").ToString()));
        }
        return outputs;
    }

    /// <summary>Reads how a material painted between two layers is authored, or null for any other material.</summary>
    /// <remarks>
    /// <para>The two-way shader samples layer B and the mask at <c>g_vTexCoordScale2</c> times layer A's UVs. The triplanar shaders project
    /// layer B along the world axes every <c>g_flTriplanarScale</c> units, so against a first layer spanning WorldMappingWidth units it
    /// repeats width over scale times. Their mask follows that projection when F_MASK_USES_TRIPLANAR_BLENDING is set and always on the
    /// membrane, and otherwise stays on layer A's UVs.</para>
    /// <para>Source 1 gives both layers one set of UVs, so only whole repeats can be represented, and a layer B larger than layer A cannot be
    /// represented at all. Those round to the nearest whole repeat and say so.</para>
    /// </remarks>
    public static VmatBlend? PaintedBlend(string text)
    {
        var document = KeyValues.Parse(text);
        if (document.Pairs.Count != 1 || document.Pairs[0].Value is not KeyValueBlock block) return null;
        var fields = block.Scalars();
        var shader = fields.GetValueOrDefault("shader", "").ToLowerInvariant();
        if (shader is not (TwoWayShader or TriplanarShader or MembraneShader)) return null;
        var projectedMask = shader == MembraneShader || fields.GetValueOrDefault("F_MASK_USES_TRIPLANAR_BLENDING") == "1";
        var keys = projectedMask ? new[] { "TextureTriplanarMask", "TextureMask" } : ["TextureMask", "TextureTriplanarMask"];
        var mask = keys.Where(fields.ContainsKey).Select(key => fields[key]).FirstOrDefault();
        var (across, down) = shader == TwoWayShader ? AuthoredScale(fields) : ProjectedScale(fields, block.Block("SystemAttributes")?.Scalars());
        var notes = new List<string>();
        var repeat = (Whole(across, "across", notes), Whole(down, "down", notes));
        return new(mask, null, repeat, shader == TwoWayShader || projectedMask ? repeat : (1, 1), notes);
    }

    private static (double Across, double Down) AuthoredScale(IReadOnlyDictionary<string, string> fields)
    {
        var scale = fields.TryGetValue("g_vTexCoordScale2", out var raw) ? VmatParser.ParseRgba(raw) : null;
        return scale is null ? (1.0, 1.0) : (scale[0], scale[1]);
    }

    private static (double Across, double Down) ProjectedScale(IReadOnlyDictionary<string, string> fields, IReadOnlyDictionary<string, string>? attributes)
    {
        var projection = Number(fields, "g_flTriplanarScale");
        if (attributes is null || projection is not > 0.0) return (1.0, 1.0);
        return ((Number(attributes, "WorldMappingWidth") ?? projection.Value) / projection.Value, (Number(attributes, "WorldMappingHeight") ?? projection.Value) / projection.Value);
    }

    private static double? Number(IReadOnlyDictionary<string, string> fields, string key) =>
        fields.TryGetValue(key, out var raw) && double.TryParse(raw, NumberStyles.Float, CultureInfo.InvariantCulture, out var value) ? value : null;

    private static int Whole(double scale, string axis, List<string> notes)
    {
        var whole = Math.Max(1, (int)Math.Round(scale));
        if (Math.Abs(scale - whole) > 1e-3) notes.Add($"Layer B repeats {whole}x {axis} instead of the authored {Vmt.Number(scale)}x, because Source 1 gives both layers one set of UVs");
        return whole;
    }
}
