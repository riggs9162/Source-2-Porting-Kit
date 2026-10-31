using System.Text;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>Normalizes authored Source 2 blend layers into independent material documents.</summary>
public static partial class VmatLayers
{
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
}
