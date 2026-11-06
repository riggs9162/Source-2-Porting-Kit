using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>Choices a project makes for one model on top of what its source authored.</summary>
/// <remarks>Both come from Model port settings: looping is chosen by sequence name across the whole project, and
/// extra skins by model name, so a project of many models can change one without touching the rest.</remarks>
public sealed partial record ModelOverrides
{
    public static readonly ModelOverrides None = new();

    [GeneratedRegex("^[a-z0-9_]+$")]
    private static partial Regex SkinName();

    /// <summary>Sequence names that play on a loop whatever their source says; <c>*</c> loops every sequence.</summary>
    public IReadOnlySet<string> LoopSequences { get; init; } = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

    /// <summary>Skins to add after the authored ones, each starting as copies of the default skin's materials.</summary>
    public IReadOnlyList<string> ExtraSkins { get; init; } = [];

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

    /// <summary>Reads the extra skins setting: one <c>model: skin, skin</c> line per model.</summary>
    /// <remarks>Skin names become material file names, so they are limited to lowercase letters, digits and
    /// underscores. A model named twice, or a skin named twice for one model, is refused rather than merged.</remarks>
    public static Dictionary<string, List<string>> ParseExtraSkins(string text)
    {
        var result = new Dictionary<string, List<string>>(StringComparer.OrdinalIgnoreCase);
        foreach (var line in Lines(text))
        {
            var colon = line.IndexOf(':');
            if (colon <= 0) throw new InvalidDataException($"Extra skins: write {line} as model: skin");
            var model = line[..colon].Trim().Replace('\\', '/').Trim('/');
            var skins = line[(colon + 1)..].Split(',', StringSplitOptions.TrimEntries | StringSplitOptions.RemoveEmptyEntries).ToList();
            if (model.Length == 0 || skins.Count == 0) throw new InvalidDataException($"Extra skins: write {line} as model: skin");
            foreach (var skin in skins.Where(skin => !SkinName().IsMatch(skin)))
                throw new InvalidDataException($"Extra skins: {skin} must use only lowercase letters, digits and underscores");
            if (skins.Distinct(StringComparer.Ordinal).Count() != skins.Count) throw new InvalidDataException($"Extra skins: {model} names a skin twice");
            if (!result.TryAdd(model, skins)) throw new InvalidDataException($"Extra skins: {model} is listed twice");
        }
        return result;
    }

    /// <summary>Whether an extra skins line names this model, by its file name or its full path under models.</summary>
    public static bool Names(string model, PortAsset asset) =>
        model.Equals(asset.Relative, StringComparison.OrdinalIgnoreCase) || model.Equals(Path.GetFileName(asset.Relative), StringComparison.OrdinalIgnoreCase);

    /// <summary>Refuses an extra skins line that matches no selected model, or more than one.</summary>
    public static void Validate(WorkflowOptions options, IReadOnlyList<PortAsset> assets)
    {
        ParseLoopSequences(options.LoopAnimations);
        foreach (var model in ParseExtraSkins(options.ExtraSkins).Keys)
        {
            var matches = assets.Count(asset => Names(model, asset));
            if (matches == 0) throw new InvalidDataException($"Extra skins: no selected model is called {model}");
            if (matches > 1) throw new InvalidDataException($"Extra skins: {model} matches {matches} models; use its path under models instead");
        }
    }

    /// <summary>The choices a workflow's settings make for one of its models.</summary>
    public static ModelOverrides For(WorkflowOptions options, PortAsset asset) => new()
    {
        LoopSequences = ParseLoopSequences(options.LoopAnimations),
        ExtraSkins = ParseExtraSkins(options.ExtraSkins).Where(pair => Names(pair.Key, asset)).SelectMany(pair => pair.Value).ToList()
    };
}
