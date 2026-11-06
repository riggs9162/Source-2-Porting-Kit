using System.Globalization;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>Choices a project makes for one model on top of what its source authored.</summary>
/// <remarks>All come from Model port settings: looping is chosen by sequence name across the whole project, and
/// extra skins and texture sizes by model name, so a project of many models can change one without touching the rest.</remarks>
public sealed partial record ModelOverrides
{
    public static readonly ModelOverrides None = new();

    private static readonly int[] Sizes = [512, 1024, 2048, 4096];

    [GeneratedRegex("^[a-z0-9_]+$")]
    private static partial Regex SkinName();

    /// <summary>Sequence names that play on a loop whatever their source says; <c>*</c> loops every sequence.</summary>
    public IReadOnlySet<string> LoopSequences { get; init; } = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

    /// <summary>Skins to add after the authored ones, each starting as copies of the default skin's materials.</summary>
    public IReadOnlyList<string> ExtraSkins { get; init; } = [];

    /// <summary>This model's texture cap in place of the project's; zero keeps native size.</summary>
    public int? Resolution { get; init; }

    /// <summary>This model's static form texture cap in place of the project's; zero keeps native size.</summary>
    public int? StaticResolution { get; init; }

    /// <summary>Whether a sequence of this name should loop.</summary>
    public bool Loops(string sequence) => LoopSequences.Contains("*") || LoopSequences.Contains(sequence);

    /// <summary>The authored timing with the project's loop choice applied.</summary>
    public AnimationMetadata Apply(AnimationMetadata authored, string sequence) => !authored.Loop && Loops(sequence) ? authored with { Loop = true } : authored;

    private static IEnumerable<string> Lines(string text) => KeyValues.Lines(text).Select(line => line.Trim()).Where(line => line.Length > 0);

    /// <summary>Reads the loop setting: one sequence name per line, or <c>*</c> for all of them.</summary>
    public static HashSet<string> ParseLoopSequences(string text)
    {
        var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var line in Lines(text))
        {
            if (line.AsSpan().IndexOfAny("\"\\") >= 0) throw new InvalidDataException($"Loop animations: {line} is not a sequence name");
            names.Add(line);
        }
        return names;
    }

    /// <summary>Reads one <c>model: value, value</c> line per model, refusing a model named twice rather than merging.</summary>
    private static Dictionary<string, List<string>> ParseModelLines(string text, string setting, string example)
    {
        var result = new Dictionary<string, List<string>>(StringComparer.OrdinalIgnoreCase);
        foreach (var line in Lines(text))
        {
            var colon = line.IndexOf(':');
            var model = colon > 0 ? line[..colon].Trim().Replace('\\', '/').Trim('/') : "";
            var values = colon > 0 ? line[(colon + 1)..].Split(',', StringSplitOptions.TrimEntries | StringSplitOptions.RemoveEmptyEntries).ToList() : [];
            if (model.Length == 0 || values.Count == 0) throw new InvalidDataException($"{setting}: write {line} as model: {example}");
            if (!result.TryAdd(model, values)) throw new InvalidDataException($"{setting}: {model} is listed twice");
        }
        return result;
    }

    /// <summary>Reads the extra skins setting: one <c>model: skin, skin</c> line per model.</summary>
    /// <remarks>Skin names become material file names, so they are limited to lowercase letters, digits and
    /// underscores. A skin named twice for one model is refused.</remarks>
    public static Dictionary<string, List<string>> ParseExtraSkins(string text)
    {
        var result = ParseModelLines(text, "Extra skins", "skin");
        foreach (var (model, skins) in result)
        {
            foreach (var skin in skins.Where(skin => !SkinName().IsMatch(skin)))
                throw new InvalidDataException($"Extra skins: {skin} must use only lowercase letters, digits and underscores");
            if (skins.Distinct(StringComparer.Ordinal).Count() != skins.Count) throw new InvalidDataException($"Extra skins: {model} names a skin twice");
        }
        return result;
    }

    private static int ParseSize(string value)
    {
        if (value.Equals("original", StringComparison.OrdinalIgnoreCase)) return 0;
        return int.TryParse(value, NumberStyles.None, CultureInfo.InvariantCulture, out var size) && Sizes.Contains(size)
            ? size
            : throw new InvalidDataException($"Texture sizes: {value} must be 512, 1024, 2048, 4096 or original");
    }

    /// <summary>Reads the texture sizes setting: <c>model: size</c> for both forms, or <c>model: size, static size</c>.</summary>
    public static Dictionary<string, (int Regular, int Static)> ParseTextureSizes(string text)
    {
        var result = new Dictionary<string, (int, int)>(StringComparer.OrdinalIgnoreCase);
        foreach (var (model, values) in ParseModelLines(text, "Texture sizes", "size, static size"))
        {
            if (values.Count > 2) throw new InvalidDataException($"Texture sizes: {model} takes one size, or a size and a static size");
            var regular = ParseSize(values[0]);
            result[model] = (regular, values.Count == 2 ? ParseSize(values[1]) : regular);
        }
        return result;
    }

    /// <summary>Whether a per-model line names this model, by its file name or its full path under models.</summary>
    public static bool Names(string model, PortAsset asset) =>
        model.Equals(asset.Relative, StringComparison.OrdinalIgnoreCase) || model.Equals(Path.GetFileName(asset.Relative), StringComparison.OrdinalIgnoreCase);

    /// <summary>Refuses unreadable settings, and a per-model line that matches no selected model or more than one.</summary>
    public static void Validate(WorkflowOptions options, IReadOnlyList<PortAsset> assets)
    {
        ParseLoopSequences(options.LoopAnimations);
        (string Setting, IEnumerable<string> Models)[] named =
            [("Extra skins", ParseExtraSkins(options.ExtraSkins).Keys), ("Texture sizes", ParseTextureSizes(options.TextureSizes).Keys)];
        foreach (var (setting, models) in named)
            foreach (var model in models)
            {
                var matches = assets.Count(asset => Names(model, asset));
                if (matches == 0) throw new InvalidDataException($"{setting}: no selected model is called {model}");
                if (matches > 1) throw new InvalidDataException($"{setting}: {model} matches {matches} models; use its path under models instead");
            }
    }

    /// <summary>The choices a workflow's settings make for one of its models.</summary>
    public static ModelOverrides For(WorkflowOptions options, PortAsset asset)
    {
        var sizes = ParseTextureSizes(options.TextureSizes).Where(pair => Names(pair.Key, asset)).Select(pair => pair.Value).ToList();
        return new()
        {
            LoopSequences = ParseLoopSequences(options.LoopAnimations),
            ExtraSkins = ParseExtraSkins(options.ExtraSkins).Where(pair => Names(pair.Key, asset)).SelectMany(pair => pair.Value).ToList(),
            Resolution = sizes.Count > 0 ? sizes[0].Regular : null,
            StaticResolution = sizes.Count > 0 ? sizes[0].Static : null
        };
    }
}
