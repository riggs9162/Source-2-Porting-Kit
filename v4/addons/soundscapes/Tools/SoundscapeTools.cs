using System.Text;
using System.Text.Json;

namespace PortingKit.Addons.Soundscapes;

/// <summary>Lists the soundscapes a pack or folder holds.</summary>
public sealed class SoundscapeListTool : ITool
{
    public ToolDefinition Definition { get; } = new("List soundscapes", "Sounds",
        "Lists every soundscape in a game's pack or an extracted folder, with the script it comes from and whether it uses Source 2 sound events, Source 1 rules or both.",
        [
            new("source", "Source", ParameterKind.Text, Required: true, Description: "vpk:<pack>_dir.vpk:scripts/, a pack's _dir.vpk, an extracted game folder, or one soundscape script."),
            new("search", "Search", ParameterKind.Text, Description: "Optional. Keeps soundscapes whose name contains this text.")
        ]) { ShowInDesktop = false };

    public Task<ToolResult> RunAsync(ToolContext context, ToolArguments arguments, CancellationToken cancellationToken)
    {
        var library = GameSource.Open(arguments.Text("source")).ReadLibrary();
        foreach (var problem in library.Problems) context.Log(problem, LogLevel.Warning);
        var search = arguments.Text("search");
        var entries = library.Entries.Where(entry => search.Length == 0 || entry.Name.Contains(search, StringComparison.OrdinalIgnoreCase))
            .Select(entry => new { name = entry.Name, file = entry.File, kind = entry.Kind, events = entry.Events, rules = entry.Rules, nested = entry.Nested }).ToList();
        Directory.CreateDirectory(context.OutputDirectory);
        var listing = Path.Combine(context.OutputDirectory, "soundscapes.json");
        File.WriteAllText(listing, JsonSerializer.Serialize(new { soundscapes = entries }, SoundscapePortTool.Indented));
        return Task.FromResult(new ToolResult($"{entries.Count} soundscapes", [new(listing, "Soundscape list")], JsonSerializer.SerializeToElement(new { count = entries.Count })));
    }
}

/// <summary>Ports soundscapes, their sound events and their sounds into a Garry's Mod addon folder.</summary>
/// <remarks>
/// <para>The output is an addon layout: <c>scripts/&lt;script&gt;.txt</c> holds the Source 1 soundscapes,
/// <c>lua/autorun/&lt;lua&gt;.lua</c> registers every sound event they use, and <c>sound/&lt;prefix&gt;/</c> holds the sounds
/// at the paths the game stores them under.</para>
/// <para>Sounds are read one at a time. A whole game's ambience is thousands of files, and the packs often sit on a
/// slow drive.</para>
/// </remarks>
public sealed class SoundscapePortTool : ITool
{
    private const int LoggedWarnings = 60;
    internal static readonly JsonSerializerOptions Indented = new() { WriteIndented = true };

    public ToolDefinition Definition { get; } = new("Soundscape port", "Sounds",
        "Ports Source 2 and Source 1 soundscapes into a Garry's Mod addon folder: a Source 1 soundscape script, Lua definitions for every sound event they use, and the sounds themselves as WAV files with their loop points.",
        [
            new("source", "Source", ParameterKind.Text, Required: true, Description: "vpk:<pack>_dir.vpk:scripts/, a pack's _dir.vpk, an extracted game folder, or one soundscape script."),
            new("soundscapes", "Soundscapes", ParameterKind.Lines, Description: "One name per line. * matches any run of characters, so a2_pistol_* takes a whole level and * takes everything. Soundscapes these play are ported with them."),
            new("events", "Extra sound events", ParameterKind.Lines, Description: "Optional. Sound events to define and bring sounds for although no picked soundscape uses them, one per line. * matches any run of characters."),
            new("output", "Output folder", ParameterKind.Directory, Required: true, Description: "Receives scripts, lua and sound folders, laid out as a Garry's Mod addon."),
            new("sound_prefix", "Sound folder", ParameterKind.Text, "ports", Description: "Folder inside sound that every ported sound goes under, for example my_name/hlvr. The rest of each path stays as the game has it."),
            new("name_prefix", "Soundscape name prefix", ParameterKind.Text, Description: "Optional. Put before every soundscape name. Soundscape names are shared by the whole game, so a ported one replaces a stock one of the same name."),
            new("event_prefix", "Sound event name prefix", ParameterKind.Text, Description: "Optional. Put before every sound event name in the Lua definitions."),
            new("script_name", "Script name", ParameterKind.Text, "soundscapes_ports", Description: "Written as scripts/<name>.txt. Name it soundscapes_<map name> and that map loads it by itself."),
            new("lua_name", "Lua file name", ParameterKind.Text, "ports_sound_events", Description: "Written as lua/autorun/<name>.lua."),
            new("fold_surround", "Fold surround beds to their front pair", ParameterKind.Boolean, "true", Description: "Source 1 soundscapes have no rear channels and would play them as extra front layers."),
            new("port_sounds", "Port the sounds", ParameterKind.Boolean, "true", Description: "Off writes only the script and the Lua definitions."),
            new("manifest", "Soundscape manifest", ParameterKind.Choice, "none", Description: "Merge writes Garry's Mod's own manifest with this script added, for scripts that are not named after a map. Only one addon can do this at a time.", Choices: ["none", "merge"]),
            new("gmod", "Garry's Mod game folder", ParameterKind.Directory, Description: "The garrysmod folder. Needed to merge the manifest."),
            new("ffmpeg", "ffmpeg", ParameterKind.File, Description: "Leave empty to use the tools.ffmpeg setting, then PATH. Decodes the game's MP3 sounds."),
            new("vrf", "Source2Viewer-CLI", ParameterKind.File, Description: "Leave empty to use the tools.vrf setting, then PATH. Decompiles sound events.")
        ]) { ShowInDesktop = false };

    /// <summary>Where decompiled sound events and preview sounds are kept.</summary>
    public static string CacheDirectory(IHostServices host) => Path.Combine(host.StateDirectory, "addons", "soundscapes", "cache");

    public async Task<ToolResult> RunAsync(ToolContext context, ToolArguments arguments, CancellationToken cancellationToken)
    {
        var prefix = arguments.Text("sound_prefix").Replace('\\', '/').Trim('/');
        RequireRelative(prefix, "The sound folder");
        var scriptName = FileName(arguments.Text("script_name"), "The script name");
        var luaName = FileName(arguments.Text("lua_name"), "The Lua file name");
        var output = Path.GetFullPath(arguments.Text("output"));
        var settings = context.Host.Settings;
        string Vrf() => ExternalTools.Locate(arguments.Text("vrf"), settings, "tools.vrf", "Source2Viewer-CLI.exe", "decompile sound events");
        string Ffmpeg() => ExternalTools.Locate(arguments.Text("ffmpeg"), settings, "tools.ffmpeg", "ffmpeg.exe", "decode the game's MP3 sounds");

        context.Report(0, "Reading soundscapes");
        var source = GameSource.Open(arguments.Text("source"));
        var library = source.ReadLibrary();
        foreach (var problem in library.Problems) context.Log(problem, LogLevel.Warning);
        var unknown = new List<string>();
        var names = library.Match(Lines(arguments.Text("soundscapes")), unknown);
        if (unknown.Count > 0) throw new InvalidDataException($"No soundscape matches: {string.Join(", ", unknown)}");

        context.Report(0.02, "Reading sound events");
        var table = await source.LoadEventsAsync(Vrf, context.Host.Processes, CacheDirectory(context.Host), message => context.Log(message), cancellationToken);
        var events = Wildcards.Match(table.Names, Lines(arguments.Text("events")), unknown);
        if (unknown.Count > 0) throw new InvalidDataException($"No sound event matches: {string.Join(", ", unknown)}");
        if (names.Count == 0 && events.Count == 0) throw new InvalidDataException("Name at least one soundscape or sound event.");

        var options = new PortOptions(prefix, arguments.Text("name_prefix"), arguments.Text("event_prefix"), arguments.Boolean("fold_surround"));
        var result = new SoundscapePorter(library, table, options, source.HasSound).Port(names, events);
        var artifacts = new List<Artifact>();
        if (result.Soundscapes.Count > 0)
        {
            var script = Path.Combine(output, "scripts", scriptName + ".txt");
            Save(script, result.ScriptText);
            artifacts.Add(new(script, "Soundscape script"));
            if (arguments.Text("manifest") == "merge") artifacts.Add(new(WriteManifest(output, scriptName, arguments.Text("gmod")), "Soundscape manifest"));
        }
        if (result.Definitions.Count > 0)
        {
            var lua = Path.Combine(output, "lua", "autorun", luaName + ".lua");
            Save(lua, SoundScriptWriter.Write(result.Definitions));
            artifacts.Add(new(lua, "Sound event definitions"));
        }

        var sounds = new List<object>();
        int written = 0, looped = 0, failed = 0;
        var waves = result.Waves.OrderBy(pair => pair.Key, StringComparer.OrdinalIgnoreCase).ToList();
        if (arguments.Boolean("port_sounds") && waves.Count > 0)
        {
            var scratch = Path.Combine(context.OutputDirectory, "scratch");
            var porter = new AudioPorter(source, context.Host.Processes, Ffmpeg, Vrf, scratch);
            var finished = new Dictionary<string, (string File, SoundOutcome Outcome)>(StringComparer.OrdinalIgnoreCase);
            for (var index = 0; index < waves.Count; index++)
            {
                cancellationToken.ThrowIfCancellationRequested();
                var (wave, path) = waves[index];
                var destination = Path.Combine(output, "sound", wave.Replace('/', Path.DirectorySeparatorChar));
                context.Report(0.05 + 0.95 * index / waves.Count, $"Porting sound {index + 1} of {waves.Count}");
                var timedOnly = result.Timed.Contains(path) && !result.Looped.Contains(path);
                SoundOutcome outcome;
                try
                {
                    if (finished.TryGetValue(path, out var first))
                    {
                        outcome = first.Outcome;
                        if (outcome.Written) File.Copy(first.File, destination, true);
                    }
                    else
                    {
                        outcome = await porter.PortAsync(path, destination, !timedOnly, cancellationToken);
                        finished[path] = (destination, outcome);
                    }
                }
                catch (Exception exception) when (exception is IOException or InvalidDataException or UnauthorizedAccessException)
                {
                    outcome = new(false, false, exception.Message);
                }
                if (outcome.Written) written++;
                else
                {
                    failed++;
                    result.Warnings.Add($"{path}: {outcome.Problem}");
                }
                if (outcome.Looped) looped++;
                if (outcome.Looped && result.Timed.Contains(path)) result.Warnings.Add($"{wave}: loops, and a random rule also plays it, so that rule's sound will not stop by itself");
                sounds.Add(new { source = path, wave, written = outcome.Written, looped = outcome.Looped, problem = outcome.Problem });
            }
            if (Directory.Exists(scratch)) Directory.Delete(scratch, true);
            if (written > 0) artifacts.Add(new(Path.Combine(output, "sound", prefix.Replace('/', Path.DirectorySeparatorChar)), "Sounds folder", "directory"));
        }

        foreach (var warning in result.Warnings.Take(LoggedWarnings)) context.Log(warning, LogLevel.Warning);
        if (result.Warnings.Count > LoggedWarnings) context.Log($"{result.Warnings.Count - LoggedWarnings} more notes are in the report.", LogLevel.Warning);
        Directory.CreateDirectory(context.OutputDirectory);
        var report = Path.Combine(context.OutputDirectory, "soundscape-report.json");
        File.WriteAllText(report, JsonSerializer.Serialize(new
        {
            source = source.Display,
            soundscapes = result.Soundscapes,
            events = result.Definitions.Select(definition => new { name = definition.Name, sounds = definition.Waves.Count, combined = definition.Combined }),
            sounds = sounds.Count > 0 ? sounds : [.. waves.Select(pair => (object)new { source = pair.Value, wave = pair.Key })],
            warnings = result.Warnings
        }, Indented));
        artifacts.Insert(0, new(report, "Port report"));
        context.Report(1, "Done");
        var summary = $"{result.Soundscapes.Count} soundscapes, {result.Definitions.Count} sound events, {written} of {waves.Count} sounds written, {looped} looping";
        if (failed > 0 && failed == waves.Count) throw new InvalidDataException("No sound could be ported. " + summary);
        return new(summary, artifacts, JsonSerializer.SerializeToElement(new
        {
            soundscapes = result.Soundscapes.Count, events = result.Definitions.Count, sounds = waves.Count, written, looped, failed, warnings = result.Warnings.Count
        }));
    }

    internal static IEnumerable<string> Lines(string text) => text.Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);

    private static void Save(string path, string text)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllText(path, text, new UTF8Encoding(false));
    }

    private static void RequireRelative(string path, string what)
    {
        if (path.Contains(':') || path.Split('/').Any(part => part is ".." or ".") || path.IndexOfAny(Path.GetInvalidPathChars()) >= 0)
            throw new InvalidDataException($"{what} must be a plain relative folder such as my_name/hlvr.");
    }

    private static string FileName(string name, string what)
    {
        name = name.Trim();
        if (name.EndsWith(".txt", StringComparison.OrdinalIgnoreCase) || name.EndsWith(".lua", StringComparison.OrdinalIgnoreCase)) name = name[..^4];
        if (name.Length == 0 || name.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0) throw new InvalidDataException($"{what} must be a file name without a folder.");
        return name;
    }

    /// <summary>Writes Garry's Mod's manifest with the ported script added.</summary>
    /// <remarks>The game reads one manifest. An addon that ships its own replaces the stock list, so the stock entries
    /// are read first, from a loose file or from the game's pack, and written back with the new script after them.</remarks>
    private static string WriteManifest(string output, string scriptName, string gmod)
    {
        const string manifest = "scripts/soundscapes_manifest.txt";
        if (gmod.Length == 0 || !Directory.Exists(gmod)) throw new InvalidDataException("Set the Garry's Mod game folder to merge the soundscape manifest.");
        var loose = Path.Combine(gmod, "scripts", "soundscapes_manifest.txt");
        var pack = Path.Combine(gmod, "garrysmod_dir.vpk");
        string text;
        if (File.Exists(loose)) text = File.ReadAllText(loose);
        else if (File.Exists(pack) && VpkArchive.Open(pack) is var archive && archive.Contains(manifest)) text = Encoding.UTF8.GetString(archive.Read(manifest));
        else throw new InvalidDataException($"Garry's Mod's soundscape manifest was not found in {gmod}.");
        var files = KeyValuesText.Parse(text).Block("soundscapes_manifest")?.Texts("file").ToList() ?? [];
        var entry = $"scripts/{scriptName}.txt";
        if (!files.Contains(entry, StringComparer.OrdinalIgnoreCase)) files.Add(entry);
        var body = new KvBlock();
        foreach (var file in files) body.Add("file", file);
        var root = new KvBlock();
        root.Add("soundscapes_manifest", body);
        var path = Path.Combine(output, "scripts", "soundscapes_manifest.txt");
        Save(path, KeyValuesText.Write(root));
        return path;
    }
}
