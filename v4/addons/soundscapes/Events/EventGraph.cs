namespace PortingKit.Addons.Soundscapes;

/// <summary>One sound file an event can play, with the values the event gives it.</summary>
/// <param name="Event">The event that names the file.</param>
/// <param name="Path">The Source 2 path, such as <c>sounds/ambient/wind.vsnd</c>.</param>
/// <param name="Volume">Volume with every parent's volume multiplied in.</param>
/// <param name="VolumeLow">Lowest volume after the event's random offset.</param>
/// <param name="VolumeHigh">Highest volume after the event's random offset.</param>
/// <param name="Pitch">Pitch as a multiplier, where 1 is unchanged.</param>
/// <param name="PitchLow">Lowest pitch after the event's random offset.</param>
/// <param name="PitchHigh">Highest pitch after the event's random offset.</param>
/// <param name="FalloffMin">Distance in units where the volume starts to drop, when the event sets one.</param>
/// <param name="FalloffMax">Distance in units where the sound is silent, when the event sets one.</param>
/// <param name="Flat">True for a bed that is heard everywhere instead of from a point.</param>
public sealed record Take(string Event, string Path, double Volume, double VolumeLow, double VolumeHigh, double Pitch, double PitchLow, double PitchHigh,
    double? FalloffMin, double? FalloffMax, bool Flat)
{
    public Take Scaled(double factor) => this with { Volume = Volume * factor, VolumeLow = VolumeLow * factor, VolumeHigh = VolumeHigh * factor };

    /// <summary>The Source 1 sound level that matches this take's falloff distances.</summary>
    /// <remarks>Source 2 holds full volume to the near distance and reaches silence at the far one. Source 1 halves the
    /// volume each time the distance doubles from a reference set by the sound level. The level returned puts Source 1's
    /// half volume where Source 2's is: midway between the two distances. A bed returns 0, which Source 1 plays
    /// everywhere, and an event with no distances returns the engine's normal level of 75.</remarks>
    public int SoundLevel()
    {
        if (Flat) return 0;
        if (FalloffMax is not { } far || far <= 0) return 75;
        var half = ((FalloffMin ?? 0) + far) / 2;
        return (int)Math.Clamp(Math.Round(60 + 20 * Math.Log10(half / 2 / 36)), 20, 140);
    }
}

/// <summary>Sounds that are picked between. Separate voices of one event play together.</summary>
public sealed class Voice
{
    public List<Take> Takes { get; } = [];

    /// <summary>Seconds between plays. Null means the voice plays continuously.</summary>
    public (double Low, double High)? Timer { get; set; }

    /// <summary>How many sounds one trigger of a burst plays.</summary>
    public double Repeats { get; set; } = 1;
}

/// <summary>What one event plays once its children and bases are followed.</summary>
public sealed class Playable
{
    public List<Voice> Voices { get; } = [];

    /// <summary>Every defined event visited, the asked one first.</summary>
    public List<string> Events { get; } = [];

    /// <summary>Events that were named but that no file defines.</summary>
    public List<string> Missing { get; } = [];

    /// <summary>Detail that Source 1 cannot play and that was left out.</summary>
    public List<string> Notes { get; } = [];
}

/// <summary>Follows a sound event down to the files it plays.</summary>
/// <remarks>
/// <para>Events reference their children under several spellings. Numbered <c>soundevent_01</c> children play together,
/// a <c>soundevents</c> list is picked from, and <c>random_soundevent_01_name</c> children replay on a timer. References
/// that exist for mixing only, such as <c>limiter_event_name</c> and <c>soundname_string</c>, are not followed: they would
/// pull unrelated sounds into an ambience.</para>
/// <para>Numbered <c>vsnd_file_01</c> files are layers with their own <c>vsnd_vol_01</c> trim. A <c>vsnd_files</c>
/// list is a set of takes to pick between, which Valve write as a bare string when there is one take. Newer games
/// write the same list as <c>vsnd_files_track_01</c>, one per track, and replay an event every
/// <c>retrigger_interval</c> seconds when <c>enable_trigger</c> is set.</para>
/// </remarks>
public static class EventGraph
{
    private const int MaximumDepth = 12;
    private static readonly string[] SingleChildKeys = ["soundevent_name", "soundevent", "soundevent_core", "child_soundevent_name", "child_soundevent", "oneshot_child", "event_name", "whoosh_sound"];
    private static readonly string[] SurroundSuffixes = ["_ls", "_rs", "_sl", "_sr"];

    /// <summary>Resolves an event.</summary>
    /// <param name="table">The game's events.</param>
    /// <param name="name">The event to resolve.</param>
    /// <param name="foldSurround">Drops the rear pair of a four-channel bed, because Source 1 would play it as extra front layers.</param>
    /// <param name="overrides">Fields a soundscape sets on the event, which replace the event's own.</param>
    public static Playable Resolve(EventTable table, string name, bool foldSurround, IReadOnlyDictionary<string, string>? overrides = null)
    {
        var result = new Playable();
        result.Voices.AddRange(Walk(table, name, foldSurround, overrides, result, new(StringComparer.OrdinalIgnoreCase), 0));
        return result;
    }

    /// <summary>True for Valve's silent placeholder file.</summary>
    public static bool IsSilence(string path) => path.Replace('\\', '/').EndsWith("/null.vsnd", StringComparison.OrdinalIgnoreCase);

    private static List<Voice> Walk(EventTable table, string name, bool foldSurround, IReadOnlyDictionary<string, string>? overrides, Playable result, HashSet<string> stack, int depth)
    {
        var fields = table.Resolve(name);
        if (fields is null)
        {
            if (!result.Missing.Contains(name, StringComparer.OrdinalIgnoreCase)) result.Missing.Add(name);
            return [];
        }
        if (depth > MaximumDepth || !stack.Add(name)) return [];
        if (!result.Events.Contains(name, StringComparer.OrdinalIgnoreCase)) result.Events.Add(name);
        if (overrides is not null)
            foreach (var (key, value) in overrides) fields[key] = value;
        var type = EventFields.Text(fields, "type").ToLowerInvariant();
        var volume = Math.Max(0, EventFields.Number(fields, "volume") ?? 1);
        var voices = OwnVoices(name, fields, type, volume);
        List<Voice> Children(string child) => Walk(table, child, foldSurround, null, result, stack, depth + 1);

        var uses = EventFields.Numbered(fields, "use_");
        var together = new List<string>();
        foreach (var (number, child) in EventFields.Numbered(fields, "soundevent_"))
            if (!uses.TryGetValue(number, out var use) || EventFields.IsOn(use)) together.Add(child);
        foreach (var key in SingleChildKeys) together.AddRange(EventFields.Strings(fields, key));
        if (foldSurround) together = Fold(name, type, together, result);
        var inherited = new List<Voice>();
        foreach (var child in together) inherited.AddRange(Children(child));

        var choices = EventFields.Strings(fields, "soundevents").Distinct(StringComparer.OrdinalIgnoreCase).ToList();
        if (choices.Count == 1) inherited.AddRange(Children(choices[0]));
        else if (choices.Count > 1)
        {
            var union = new Voice();
            foreach (var voice in choices.SelectMany(Children))
            {
                union.Takes.AddRange(voice.Takes);
                union.Timer ??= voice.Timer;
                union.Repeats = Math.Max(union.Repeats, voice.Repeats);
            }
            if (union.Takes.Count > 0) inherited.Add(union);
        }
        ApplyBurst(fields, inherited);

        var lows = EventFields.Numbered(fields, "random_soundevent_", "_timer_min");
        var highs = EventFields.Numbered(fields, "random_soundevent_", "_timer_max");
        foreach (var (number, child) in EventFields.Numbered(fields, "random_soundevent_", "_name"))
        {
            var low = Parse(lows.GetValueOrDefault(number));
            var high = Parse(highs.GetValueOrDefault(number));
            foreach (var voice in Children(child))
            {
                Retime(voice, low ?? high ?? 0, high ?? low ?? 0);
                inherited.Add(voice);
            }
        }

        foreach (var voice in inherited)
        {
            for (var index = 0; index < voice.Takes.Count; index++) voice.Takes[index] = voice.Takes[index].Scaled(volume);
            voices.Add(voice);
        }
        stack.Remove(name);
        return voices;
    }

    private static double? Parse(string? text) =>
        double.TryParse(text, System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var value) ? value : null;

    private static List<Voice> OwnVoices(string name, Dictionary<string, object> fields, string type, double volume)
    {
        var flat = type.Contains("fixed_rotation", StringComparison.Ordinal) || type.Contains("_2d", StringComparison.Ordinal);
        var volumeLow = EventFields.Number(fields, "volume_rand_min") ?? 0;
        var volumeHigh = EventFields.Number(fields, "volume_rand_max") ?? 0;
        var pitch = EventFields.Number(fields, "pitch") ?? 1;
        var pitchLow = EventFields.Number(fields, "pitch_rand_min") ?? 0;
        var pitchHigh = EventFields.Number(fields, "pitch_rand_max") ?? 0;
        Take Make(string path, double trim) => new(name, path, volume * trim, Math.Max(0, volume + Math.Min(volumeLow, volumeHigh)) * trim, Math.Max(0, volume + Math.Max(volumeLow, volumeHigh)) * trim,
            pitch, pitch + Math.Min(pitchLow, pitchHigh), pitch + Math.Max(pitchLow, pitchHigh),
            EventFields.Number(fields, "volume_falloff_min"), EventFields.Number(fields, "volume_falloff_max"), flat);

        var voices = new List<Voice>();
        var trims = EventFields.Numbered(fields, "vsnd_vol_");
        foreach (var (number, path) in EventFields.Numbered(fields, "vsnd_file_"))
        {
            if (IsSilence(path)) continue;
            var voice = new Voice();
            voice.Takes.Add(Make(path, Parse(trims.GetValueOrDefault(number)) ?? 1));
            voices.Add(voice);
        }
        foreach (var key in fields.Keys.Where(key => key == "vsnd_files" || key.StartsWith("vsnd_files_track_", StringComparison.Ordinal)).Order(StringComparer.Ordinal).ToList())
        {
            var takes = EventFields.Strings(fields, key).Where(path => !IsSilence(path)).ToList();
            if (takes.Count == 0) continue;
            var voice = new Voice();
            voice.Takes.AddRange(takes.Select(path => Make(path, 1)));
            voices.Add(voice);
        }
        var retriggerLow = EventFields.Number(fields, "retrigger_interval_min");
        var retriggerHigh = EventFields.Number(fields, "retrigger_interval_max");
        if (EventFields.Flag(fields, "enable_trigger", false) && (retriggerLow ?? retriggerHigh) is { } interval)
            foreach (var voice in voices) voice.Timer = (Math.Max(0.1, retriggerLow ?? interval), Math.Max(0.1, retriggerHigh ?? interval));
        return voices;
    }

    /// <summary>Keeps the front pair of a surround bed.</summary>
    /// <remarks>The rear channels are found by their name suffix, not the event type: Valve build four-channel beds
    /// under both the quad type and the plain multi type.</remarks>
    private static List<string> Fold(string name, string type, List<string> children, Playable result)
    {
        var rear = children.Where(child => SurroundSuffixes.Any(suffix => child.EndsWith(suffix, StringComparison.OrdinalIgnoreCase))).ToList();
        if (rear.Count > 0 && rear.Count < children.Count)
        {
            result.Notes.Add($"{name}: kept the front pair of a surround bed and left out {string.Join(", ", rear)}");
            return [.. children.Except(rear)];
        }
        if (type.Contains("multi_quad", StringComparison.Ordinal) && children.Count > 2)
        {
            result.Notes.Add($"{name}: kept the front pair of a quad bed and left out {string.Join(", ", children.Skip(2))}");
            return [.. children.Take(2)];
        }
        return children;
    }

    /// <summary>Turns a burst event's children into timed voices.</summary>
    /// <remarks>A burst replays its child every <c>delay_timer</c> seconds for <c>burst_length</c> seconds, and a count
    /// burst replays it <c>burst_count</c> times. The number of sounds per burst is kept so an outer timer can spread
    /// them: Source 1 has no bursts, only a time between plays.</remarks>
    private static void ApplyBurst(Dictionary<string, object> fields, List<Voice> voices)
    {
        var delayLow = EventFields.Number(fields, "delay_timer_min");
        var delayHigh = EventFields.Number(fields, "delay_timer_max");
        var countLow = EventFields.Number(fields, "burst_count_min");
        var countHigh = EventFields.Number(fields, "burst_count_max");
        if (delayLow is null && delayHigh is null && countLow is null && countHigh is null) return;
        var low = Math.Max(0.1, delayLow ?? delayHigh ?? EventFields.Number(fields, "wait_rand_min") ?? 0.1);
        var high = Math.Max(low, delayHigh ?? delayLow ?? EventFields.Number(fields, "wait_rand_max") ?? low);
        double repeats;
        if (countLow is not null || countHigh is not null) repeats = ((countLow ?? countHigh ?? 1) + (countHigh ?? countLow ?? 1)) / 2;
        else
        {
            var length = ((EventFields.Number(fields, "burst_length_min") ?? 0) + (EventFields.Number(fields, "burst_length_max") ?? 0)) / 2;
            repeats = length / ((low + high) / 2);
        }
        foreach (var voice in voices.Where(voice => voice.Timer is null))
        {
            voice.Timer = (low, high);
            voice.Repeats = Math.Max(1, repeats);
        }
    }

    private static void Retime(Voice voice, double low, double high)
    {
        if (high < low) (low, high) = (high, low);
        if (high <= 0) (low, high) = voice.Timer ?? (4, 8);
        else if (voice.Timer is not null && voice.Repeats > 1) (low, high) = (low / voice.Repeats, high / voice.Repeats);
        voice.Timer = (Math.Max(0.1, low), Math.Max(0.1, high));
        voice.Repeats = 1;
    }
}
