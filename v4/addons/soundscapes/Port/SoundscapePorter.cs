using System.Globalization;

namespace PortingKit.Addons.Soundscapes;

/// <summary>How a port names what it writes.</summary>
/// <param name="SoundPrefix">Folder under <c>sound/</c> that every ported sound sits in, such as <c>riggs9162/hlvr</c>.</param>
/// <param name="NamePrefix">Text put before every soundscape name. Soundscape names are global in Source 1.</param>
/// <param name="EventPrefix">Text put before every sound event name in the Lua definitions.</param>
/// <param name="FoldSurround">Drops the rear pair of four-channel beds.</param>
public sealed record PortOptions(string SoundPrefix, string NamePrefix = "", string EventPrefix = "", bool FoldSurround = true);

/// <summary>A sound event as Garry's Mod's <c>sound.Add</c> takes it.</summary>
/// <param name="Name">The name the definition is registered under.</param>
/// <param name="Event">The Source 2 event it came from.</param>
/// <param name="Combined">True when the event plays several layers or children at once, which one definition can only pick between.</param>
public sealed record SoundDefinition(string Name, string Event, double VolumeLow, double VolumeHigh, int PitchLow, int PitchHigh, int Level, IReadOnlyList<string> Waves, bool Combined);

/// <summary>The outcome of porting a set of soundscapes.</summary>
public sealed class PortResult
{
    /// <summary>The Source 1 script, one block per soundscape.</summary>
    public KvBlock Script { get; } = new();

    /// <summary>Ported soundscapes by their original name, in the order written.</summary>
    public List<string> Soundscapes { get; } = [];

    public List<SoundDefinition> Definitions { get; } = [];

    /// <summary>Every Source 2 sound the script and the definitions need, without repeats.</summary>
    public List<string> Sounds { get; } = [];

    /// <summary>Sounds a <c>playlooping</c> rule uses.</summary>
    public HashSet<string> Looped { get; } = new(StringComparer.OrdinalIgnoreCase);

    /// <summary>Sounds a <c>playrandom</c> rule uses.</summary>
    public HashSet<string> Timed { get; } = new(StringComparer.OrdinalIgnoreCase);

    /// <summary>The Source 2 sound behind each written wave path.</summary>
    public Dictionary<string, string> Waves { get; } = new(StringComparer.OrdinalIgnoreCase);

    public List<string> Warnings { get; } = [];

    public string ScriptText => KeyValuesText.Write(Script);
}

/// <summary>Turns Source 2 soundscapes into Source 1 soundscapes and sound definitions.</summary>
/// <remarks>
/// <para>A Source 2 soundscape names sound events where Source 1 names files. Each <c>playevent</c> is resolved through
/// <see cref="EventGraph"/>: a continuous voice becomes a <c>playlooping</c> rule per layer, because Source 1 cannot
/// layer inside one rule, and a timed voice becomes one <c>playrandom</c> rule with its takes in <c>rndwave</c>.
/// Volumes multiply down the graph and by the rule's own volume.</para>
/// <para>Valve mix both formats in one script. Source 1 rules are kept as written, except that a wave the game itself
/// ships is pointed at its ported copy. A wave the game does not ship is a stock Half-Life 2 sound and is left alone.</para>
/// <para>Soundscapes a picked one plays through <c>playsoundscape</c> are ported with it.</para>
/// </remarks>
public sealed class SoundscapePorter(SoundscapeLibrary library, EventTable table, PortOptions options, Func<string, bool> hasSound)
{
    private const string SoundCharacters = "*#@<>^)}$!?&~";
    private static readonly string[] Scalars = ["dsp", "dsp_volume", "dsp_spatial", "fadetime", "soundmixer"];
    private static readonly string[] RuleOnlyKeys = ["event", "volume"];
    private readonly PortResult result = new();
    private readonly HashSet<string> sounds = new(StringComparer.OrdinalIgnoreCase);
    private readonly List<string> events = [];
    private readonly HashSet<string> seenEvents = new(StringComparer.OrdinalIgnoreCase);

    /// <summary>Ports the named soundscapes, and defines every sound event they and <paramref name="extraEvents"/> use.</summary>
    public PortResult Port(IEnumerable<string> soundscapes, IEnumerable<string> extraEvents)
    {
        var queue = new Queue<string>(soundscapes);
        var done = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        while (queue.TryDequeue(out var name))
        {
            if (!done.Add(name)) continue;
            if (library.Find(name) is not { } entry)
            {
                result.Warnings.Add($"{name}: no script defines this soundscape");
                continue;
            }
            result.Script.Add(options.NamePrefix + entry.Name, Convert(entry, queue));
            result.Soundscapes.Add(entry.Name);
        }
        foreach (var name in extraEvents) NoteEvents(EventGraph.Resolve(table, name, options.FoldSurround), name);
        foreach (var name in events) Define(name);
        var distinct = result.Warnings.Distinct(StringComparer.Ordinal).ToList();
        result.Warnings.Clear();
        result.Warnings.AddRange(distinct);
        return result;
    }

    /// <summary>The path Source 1 plays a Source 2 sound from, relative to <c>sound/</c>.</summary>
    /// <remarks>Source 2 keeps sounds under <c>sounds/</c>. That folder is dropped, the prefix is put in its place and
    /// the rest of the path is kept, so <c>sounds/ambient/wind.vsnd</c> becomes <c>prefix/ambient/wind.wav</c>.</remarks>
    public static string WavePath(string source, string prefix)
    {
        var path = source.Replace('\\', '/').TrimStart('/');
        if (path.StartsWith("sounds/", StringComparison.OrdinalIgnoreCase)) path = path[7..];
        var dot = path.LastIndexOf('.');
        if (dot > path.LastIndexOf('/')) path = path[..dot];
        prefix = prefix.Replace('\\', '/').Trim('/');
        return (prefix.Length > 0 ? prefix + "/" : "") + path + ".wav";
    }

    private KvBlock Convert(SoundscapeEntry entry, Queue<string> queue)
    {
        var ported = new KvBlock();
        var loops = new Dictionary<string, (KvBlock Block, bool Placed)>(StringComparer.OrdinalIgnoreCase);
        foreach (var (key, value) in entry.Body.Pairs)
        {
            var rule = key.ToLowerInvariant();
            if (value is string text)
            {
                if (Scalars.Contains(rule)) ported.Add(rule, text);
                else result.Warnings.Add($"{entry.Name}: left out the unknown setting '{key}'");
                continue;
            }
            var block = (KvBlock)value;
            switch (rule)
            {
                case "playevent":
                    PlayEvent(entry.Name, block, ported, loops);
                    break;
                case "playlooping" or "playrandom":
                    ported.Add(rule, CopyRule(entry.Name, block, rule == "playlooping"));
                    break;
                case "playsoundscape":
                    ported.Add(rule, CopyNested(entry.Name, block, queue));
                    break;
                default:
                    result.Warnings.Add($"{entry.Name}: left out the unknown rule '{key}'");
                    break;
            }
        }
        if (ported.Pairs.Count == 0) result.Warnings.Add($"{entry.Name}: nothing is left to play after porting");
        return ported;
    }

    private void PlayEvent(string soundscape, KvBlock rule, KvBlock ported, Dictionary<string, (KvBlock Block, bool Placed)> loops)
    {
        var name = rule.Text("event");
        if (string.IsNullOrWhiteSpace(name))
        {
            result.Warnings.Add($"{soundscape}: skipped a playevent with no event name");
            return;
        }
        var overrides = rule.Pairs.Where(pair => pair.Value is string && !RuleOnlyKeys.Contains(pair.Key, StringComparer.OrdinalIgnoreCase))
            .GroupBy(pair => pair.Key, StringComparer.Ordinal).ToDictionary(group => group.Key, group => (string)group.Last().Value, StringComparer.Ordinal);
        var playable = EventGraph.Resolve(table, name, options.FoldSurround, overrides);
        NoteEvents(playable, name);
        foreach (var missing in playable.Missing) result.Warnings.Add($"{soundscape}: no sound event file defines {missing}");
        var scale = Math.Max(0, Number(rule.Text("volume")) ?? 1);
        var position = rule.Text("position");
        var origin = Origin(rule.Text("origin"));
        if (position is null && origin is null && table.Resolve(name) is { } fields && EventFields.Flag(fields, "position_random", false)) position = "random";

        foreach (var voice in playable.Voices.Where(voice => voice.Takes.Count > 0))
        {
            if (voice.Timer is null)
            {
                Loop(soundscape, voice, scale, position, origin, ported, loops);
                continue;
            }
            var (low, high) = voice.Timer.Value;
            var block = new KvBlock();
            block.Add("time", Range(low, high, "0.##"));
            block.Add("volume", Range(Math.Clamp(voice.Takes.Min(take => take.VolumeLow) * scale, 0, 1), Math.Clamp(voice.Takes.Max(take => take.VolumeHigh) * scale, 0, 1), "0.###"));
            AddPitch(block, voice.Takes.Min(take => take.PitchLow), voice.Takes.Max(take => take.PitchHigh));
            Place(block, position, origin, voice.Takes[0]);
            var waves = new KvBlock();
            foreach (var path in voice.Takes.Select(take => take.Path).Distinct(StringComparer.OrdinalIgnoreCase)) waves.Add("wave", Use(path, result.Timed));
            block.Add("rndwave", waves);
            ported.Add("playrandom", block);
        }
        foreach (var note in playable.Notes) result.Warnings.Add($"{soundscape}: {note}");
    }

    /// <summary>Writes a continuous voice as one looping rule.</summary>
    /// <remarks>
    /// <para>Source 1 plays a sound file once per soundscape, however many rules name it. Source 2 soundscapes often
    /// place one humming event at several lights, so a repeat takes the event's next unused loop, and when none is
    /// left it gets its own copy of the file, which is how Valve's Source 1 maps work around the limit.</para>
    /// <para>A repeat that is not placed anywhere would only play the same bed again, so the louder volume is kept.</para>
    /// </remarks>
    private void Loop(string soundscape, Voice voice, double scale, string? position, string? origin, KvBlock ported, Dictionary<string, (KvBlock Block, bool Placed)> loops)
    {
        var placed = position is not null || origin is not null;
        var take = voice.Takes.FirstOrDefault(candidate => !loops.ContainsKey(WavePath(candidate.Path, options.SoundPrefix))) ?? voice.Takes[0];
        var volume = Math.Clamp(take.Volume * scale, 0, 1);
        var wave = Use(take.Path, result.Looped);
        if (loops.TryGetValue(wave, out var earlier))
        {
            if (!placed && !earlier.Placed)
            {
                var index = earlier.Block.Pairs.FindIndex(pair => pair.Key == "volume");
                if (volume > (Number((string)earlier.Block.Pairs[index].Value) ?? 0)) earlier.Block.Pairs[index] = new("volume", Format(volume));
                result.Warnings.Add($"{soundscape}: {wave} is looped twice with no position, so it plays once at the louder volume");
                return;
            }
            var copy = 2;
            while (loops.ContainsKey(CopyPath(wave, copy))) copy++;
            wave = Use(take.Path, result.Looped, copy);
        }
        var block = new KvBlock();
        block.Add("volume", Format(volume));
        AddPitch(block, take.Pitch, take.Pitch);
        Place(block, position, origin, take);
        block.Add("wave", wave);
        loops[wave] = (block, placed);
        ported.Add("playlooping", block);
    }

    private static string CopyPath(string wave, int copy) => $"{wave[..^4]}_copy{copy.ToString(CultureInfo.InvariantCulture)}.wav";

    /// <summary>Writes where a rule plays from.</summary>
    /// <remarks>A placed rule always gets a sound level. Garry's Mod plays a placed random sound everywhere when it
    /// has none.</remarks>
    private static void Place(KvBlock block, string? position, string? origin, Take take)
    {
        if (position is null && origin is null) return;
        var level = take.SoundLevel();
        block.Add("soundlevel", $"SNDLVL_{(level == 0 ? 75 : level).ToString(CultureInfo.InvariantCulture)}dB");
        if (origin is not null) block.Add("origin", origin);
        else block.Add("position", position!.Trim());
    }

    private static void AddPitch(KvBlock block, double low, double high)
    {
        var from = (int)Math.Round(low * 100);
        var to = (int)Math.Round(high * 100);
        if (from == 100 && to == 100) return;
        block.Add("pitch", from == to ? from.ToString(CultureInfo.InvariantCulture) : $"{from.ToString(CultureInfo.InvariantCulture)},{to.ToString(CultureInfo.InvariantCulture)}");
    }

    /// <summary>Source 2 scripts write an origin with spaces or commas. Source 1 reads commas only.</summary>
    private static string? Origin(string? text)
    {
        if (text is null) return null;
        var parts = text.Split([',', ' ', '\t'], StringSplitOptions.RemoveEmptyEntries);
        return parts.Length == 3 && parts.All(part => Number(part) is not null) ? string.Join(", ", parts) : null;
    }

    private KvBlock CopyRule(string soundscape, KvBlock rule, bool looping)
    {
        var copy = new KvBlock();
        foreach (var (key, value) in rule.Pairs)
        {
            if (value is KvBlock child) copy.Add(key, CopyRule(soundscape, child, looping));
            else if (key.Equals("wave", StringComparison.OrdinalIgnoreCase)) copy.Add("wave", Remap(soundscape, (string)value, looping));
            else if (key.Equals("origin", StringComparison.OrdinalIgnoreCase)) copy.Add("origin", Origin((string)value) ?? (string)value);
            else copy.Add(key, (string)value);
        }
        return copy;
    }

    private string Remap(string soundscape, string wave, bool looping)
    {
        var marks = wave.Length - wave.TrimStart(SoundCharacters.ToCharArray()).Length;
        var body = wave[marks..].Replace('\\', '/').TrimStart('/');
        var native = body.StartsWith("sounds/", StringComparison.OrdinalIgnoreCase) && body.EndsWith(".vsnd", StringComparison.OrdinalIgnoreCase);
        var source = native ? body : "sounds/" + System.IO.Path.ChangeExtension(body, ".vsnd").Replace('\\', '/');
        if (hasSound(source)) return wave[..marks] + Use(source, looping ? result.Looped : result.Timed);
        if (!native) return wave;
        result.Warnings.Add($"{soundscape}: the game does not ship {body}, so the rule plays the stock sound of that name");
        return wave[..marks] + body[7..^5] + ".wav";
    }

    private KvBlock CopyNested(string soundscape, KvBlock rule, Queue<string> queue)
    {
        var copy = new KvBlock();
        foreach (var (key, value) in rule.Pairs)
        {
            if (value is not string text) continue;
            if (!key.Equals("name", StringComparison.OrdinalIgnoreCase))
            {
                copy.Add(key, text);
                continue;
            }
            if (library.Find(text) is { } nested)
            {
                queue.Enqueue(nested.Name);
                copy.Add("name", options.NamePrefix + nested.Name);
                continue;
            }
            result.Warnings.Add($"{soundscape}: plays the soundscape {text}, which no script defines, so its name is kept");
            copy.Add("name", text);
        }
        return copy;
    }

    /// <summary>Records that a sound is needed and returns the path it will play from.</summary>
    /// <param name="copy">Above 1, names a separate copy of the file instead of the file itself.</param>
    private string Use(string source, HashSet<string>? usage, int copy = 1)
    {
        var path = source.Replace('\\', '/').TrimStart('/');
        if (sounds.Add(path)) result.Sounds.Add(path);
        usage?.Add(path);
        var wave = WavePath(path, options.SoundPrefix);
        if (copy > 1) wave = CopyPath(wave, copy);
        result.Waves[wave] = path;
        return wave;
    }

    private void NoteEvents(Playable playable, string asked)
    {
        if (playable.Events.Count == 0 && !table.Contains(asked)) result.Warnings.Add($"No sound event file defines {asked}");
        foreach (var name in playable.Events.Where(seenEvents.Add)) events.Add(name);
    }

    /// <summary>Builds the definitions of one event from its own values.</summary>
    /// <remarks>
    /// <para>An event that plays one sound, or picks between takes, is one Source 1 sound and is defined exactly. An
    /// event that plays timed children lists all of their takes.</para>
    /// <para>An event that plays several layers at once cannot be one Source 1 sound. Its definition plays the first
    /// layer and is marked as combined for the report. When the layers are the event's own files, each also gets an
    /// exact definition named <c>&lt;event&gt;.Layer1</c>, <c>.Layer2</c> and so on, so Lua can start them together.</para>
    /// </remarks>
    private void Define(string name)
    {
        var playable = EventGraph.Resolve(table, name, options.FoldSurround);
        var voices = playable.Voices.Where(voice => voice.Takes.Count > 0).ToList();
        if (voices.Count == 0) return;
        var takes = voices.Any(voice => voice.Timer is not null) ? voices.SelectMany(voice => voice.Takes) : voices[0].Takes;
        AddDefinition(options.EventPrefix + name, name, [.. takes.DistinctBy(take => take.Path, StringComparer.OrdinalIgnoreCase)], voices.Count > 1);
        var layers = voices.Where(voice => voice.Takes.Count == 1 && voice.Timer is null && voice.Takes[0].Event.Equals(name, StringComparison.OrdinalIgnoreCase)).ToList();
        if (layers.Count < 2 || layers.Count != voices.Count) return;
        for (var index = 0; index < layers.Count; index++)
            AddDefinition($"{options.EventPrefix}{name}.Layer{(index + 1).ToString(CultureInfo.InvariantCulture)}", name, layers[index].Takes, false);
    }

    private void AddDefinition(string name, string source, IReadOnlyList<Take> takes, bool combined)
    {
        var varies = takes.Any(take => take.VolumeLow != take.VolumeHigh);
        var loudest = takes.Max(take => take.Volume);
        result.Definitions.Add(new(name, source,
            Math.Clamp(varies ? takes.Min(take => take.VolumeLow) : loudest, 0, 1), Math.Clamp(varies ? takes.Max(take => take.VolumeHigh) : loudest, 0, 1),
            (int)Math.Round(takes.Min(take => take.PitchLow) * 100), (int)Math.Round(takes.Max(take => take.PitchHigh) * 100),
            takes[0].SoundLevel(), [.. takes.Select(take => Use(take.Path, null))], combined));
    }

    private static double? Number(string? text) => double.TryParse(text, NumberStyles.Float, CultureInfo.InvariantCulture, out var value) ? value : null;

    private static string Format(double value) => value.ToString("0.###", CultureInfo.InvariantCulture);

    private static string Range(double low, double high, string format)
    {
        var from = low.ToString(format, CultureInfo.InvariantCulture);
        var to = high.ToString(format, CultureInfo.InvariantCulture);
        return from == to ? from : $"{from},{to}";
    }
}
