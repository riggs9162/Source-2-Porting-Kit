namespace PortingKit.Addons.Porting;

/// <summary>Choices a project makes for one model on top of what its source authored.</summary>
/// <remarks>Looping comes from a Model port setting and is chosen by sequence name across the whole project, so a
/// project of many models can change one clip without touching the rest.</remarks>
public sealed record ModelOverrides
{
    public static readonly ModelOverrides None = new();

    /// <summary>Sequence names that play on a loop whatever their source says; <c>*</c> loops every sequence.</summary>
    public IReadOnlySet<string> LoopSequences { get; init; } = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

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

    /// <summary>Refuses settings that cannot be read.</summary>
    public static void Validate(WorkflowOptions options, IReadOnlyList<PortAsset> assets) => ParseLoopSequences(options.LoopAnimations);

    /// <summary>The choices a workflow's settings make for one of its models.</summary>
    public static ModelOverrides For(WorkflowOptions options, PortAsset asset) => new() { LoopSequences = ParseLoopSequences(options.LoopAnimations) };
}
