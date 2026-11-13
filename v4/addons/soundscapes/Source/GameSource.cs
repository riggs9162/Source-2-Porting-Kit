using System.Collections.Concurrent;
using System.Security.Cryptography;
using System.Text;

namespace PortingKit.Addons.Soundscapes;

/// <summary>Where soundscapes, sound events and sounds are read from: a game's pack or an extracted folder.</summary>
/// <remarks>
/// <para>A pack is named the way Source 2 Viewer's "Copy URL" writes it, <c>vpk:&lt;pack&gt;_dir.vpk:scripts/</c>. A bare
/// pack path means its <c>scripts/</c> folder, and the part after the pack may also name one script. A folder is either
/// a content root holding <c>scripts/</c> or the scripts folder itself.</para>
/// <para>Soundscape scripts are plain text even inside a pack, so listing them needs no other program. Sound events are
/// compiled, so Source 2 Viewer decompiles them once and the result is kept until the pack changes.</para>
/// </remarks>
public sealed class GameSource
{
    private static readonly ConcurrentDictionary<string, EventTable> Tables = new(StringComparer.OrdinalIgnoreCase);
    private static readonly SemaphoreSlim EventsGate = new(1, 1);
    private readonly VpkArchive? pack;
    private readonly string root;
    private readonly string scripts;
    private readonly string? onlyScript;

    /// <summary>The source as the user wrote it.</summary>
    public string Display { get; }

    /// <summary>Changes whenever the pack or folder does, so cached sound events are never stale.</summary>
    public string Identity { get; }

    private GameSource(string display, VpkArchive? pack, string root, string scripts, string? onlyScript, string identity)
    {
        Display = display;
        this.pack = pack;
        this.root = root;
        this.scripts = scripts;
        this.onlyScript = onlyScript;
        Identity = identity;
    }

    /// <summary>Opens a source from the text a user pastes.</summary>
    public static GameSource Open(string text)
    {
        var value = text.Trim().Trim('"');
        if (value.Length == 0) throw new InvalidDataException("Name a pack or a folder to read soundscapes from.");
        var isUrl = value.StartsWith("vpk:", StringComparison.OrdinalIgnoreCase);
        if (isUrl || value.EndsWith(".vpk", StringComparison.OrdinalIgnoreCase))
        {
            var body = isUrl ? value[4..] : value;
            var at = body.IndexOf(".vpk:", StringComparison.OrdinalIgnoreCase);
            var archive = at < 0 ? body : body[..(at + 4)];
            var inner = at < 0 ? "" : body[(at + 5)..].Trim().Replace('\\', '/').TrimStart('/');
            if (!archive.EndsWith(".vpk", StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException($"Expected vpk:<pack>_dir.vpk:scripts/, got: {text}");
            var opened = VpkArchive.Open(archive);
            string? script = inner.EndsWith(".txt", StringComparison.OrdinalIgnoreCase) ? inner : null;
            var folder = script is not null ? inner[..(inner.LastIndexOf('/') + 1)] : inner.Length == 0 ? "scripts/" : inner.TrimEnd('/') + "/";
            var info = new FileInfo(opened.Path);
            return new(value, opened, RootOf(folder), folder, script, $"{opened.Path}|{info.Length}|{info.LastWriteTimeUtc.Ticks}");
        }
        if (File.Exists(value) && value.EndsWith(".txt", StringComparison.OrdinalIgnoreCase))
        {
            var full = Path.GetFullPath(value);
            var folder = Path.GetDirectoryName(full)!;
            return new(value, null, ContentRoot(folder), folder, full, full);
        }
        if (Directory.Exists(value))
        {
            var full = Path.GetFullPath(value);
            var nested = Path.Combine(full, "scripts");
            var folder = Directory.Exists(nested) && Directory.EnumerateFiles(nested, "soundscapes*.txt").Any() ? nested : full;
            return new(value, null, ContentRoot(folder), folder, null, full);
        }
        throw new InvalidDataException($"Not a pack, a folder or a soundscape script: {text}");
    }

    private static string RootOf(string scriptsFolder) =>
        scriptsFolder.EndsWith("scripts/", StringComparison.OrdinalIgnoreCase) ? scriptsFolder[..^"scripts/".Length] : "";

    private static string ContentRoot(string scriptsFolder) =>
        Path.GetFileName(scriptsFolder).Equals("scripts", StringComparison.OrdinalIgnoreCase) ? Path.GetDirectoryName(scriptsFolder) ?? scriptsFolder : scriptsFolder;

    private static bool IsScript(string name) =>
        name.StartsWith("soundscapes", StringComparison.OrdinalIgnoreCase) && !name.Contains("manifest", StringComparison.OrdinalIgnoreCase);

    /// <summary>Reads every soundscape script, in name order. The manifest is not a script and is left out.</summary>
    public SoundscapeLibrary ReadLibrary()
    {
        var library = new SoundscapeLibrary();
        if (pack is not null)
        {
            IEnumerable<string> files = onlyScript is not null ? [onlyScript] : pack.List(scripts, ".txt").Where(path => path.IndexOf('/', scripts.Length) < 0 && IsScript(path[scripts.Length..]));
            foreach (var path in files) library.AddScript(path[(path.LastIndexOf('/') + 1)..], Encoding.UTF8.GetString(pack.Read(path)));
            return library;
        }
        IEnumerable<string> loose = onlyScript is not null ? [onlyScript] : Directory.EnumerateFiles(scripts, "soundscapes*.txt").Where(path => IsScript(Path.GetFileName(path))).Order(StringComparer.OrdinalIgnoreCase);
        foreach (var path in loose) library.AddScript(Path.GetFileName(path), File.ReadAllText(path));
        return library;
    }

    /// <summary>True when the source holds a Source 2 sound, such as <c>sounds/ambient/wind.vsnd</c>.</summary>
    public bool HasSound(string path) => pack is not null ? pack.Contains(root + path + "_c") : LooseSound(path) is not null;

    /// <summary>Reads a sound's file: the compiled <c>.vsnd_c</c>, or an already decoded file from an extracted folder.</summary>
    /// <returns>The bytes and their file extension, or null when the source does not hold the sound.</returns>
    public (byte[] Bytes, string Extension)? ReadSound(string path)
    {
        if (pack is not null) return pack.Contains(root + path + "_c") ? (pack.Read(root + path + "_c"), ".vsnd_c") : null;
        return LooseSound(path) is { } file ? (File.ReadAllBytes(file), Path.GetExtension(file).ToLowerInvariant()) : null;
    }

    private string? LooseSound(string path)
    {
        var stem = Path.Combine(root, path.Replace('/', Path.DirectorySeparatorChar));
        return new[] { stem + "_c", Path.ChangeExtension(stem, ".wav"), Path.ChangeExtension(stem, ".mp3") }.FirstOrDefault(File.Exists);
    }

    /// <summary>Loads every sound event the source defines.</summary>
    /// <param name="vrf">Finds Source2Viewer-CLI. Only called when compiled events have to be decompiled.</param>
    /// <param name="cache">Folder that keeps decompiled events between runs.</param>
    public async Task<EventTable> LoadEventsAsync(Func<string> vrf, IProcessRunner processes, string cache, Action<string> log, CancellationToken cancellationToken)
    {
        if (Tables.TryGetValue(Identity, out var known)) return known;
        await EventsGate.WaitAsync(cancellationToken);
        try
        {
            if (Tables.TryGetValue(Identity, out known)) return known;
            var folder = await EventsFolderAsync(vrf, processes, cache, log, cancellationToken);
            var table = new EventTable();
            if (folder is not null)
            {
                foreach (var file in Directory.EnumerateFiles(folder, "*.vsndevts", SearchOption.AllDirectories).Order(StringComparer.OrdinalIgnoreCase))
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    try
                    {
                        table.AddFile(await File.ReadAllTextAsync(file, cancellationToken));
                    }
                    catch (InvalidDataException exception)
                    {
                        log($"{Path.GetFileName(file)}: {exception.Message}");
                    }
                }
            }
            log($"Read {table.Count} sound events");
            Tables[Identity] = table;
            return table;
        }
        finally
        {
            EventsGate.Release();
        }
    }

    private async Task<string?> EventsFolderAsync(Func<string> vrf, IProcessRunner processes, string cache, Action<string> log, CancellationToken cancellationToken)
    {
        string input;
        List<string> filter;
        if (pack is not null)
        {
            if (pack.List(root + "soundevents/", ".vsndevts_c").Count == 0) return null;
            input = pack.Path;
            filter = ["-f", root + "soundevents/", "-e", "vsndevts_c"];
        }
        else
        {
            var loose = Path.Combine(root, "soundevents");
            if (!Directory.Exists(loose)) return null;
            if (Directory.EnumerateFiles(loose, "*.vsndevts", SearchOption.AllDirectories).Any()) return loose;
            if (!Directory.EnumerateFiles(loose, "*.vsndevts_c", SearchOption.AllDirectories).Any()) return null;
            input = loose;
            filter = ["-e", "vsndevts_c", "--recursive"];
        }
        var folder = Path.Combine(cache, "events", Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(Identity)))[..16]);
        var marker = Path.Combine(folder, "complete");
        if (File.Exists(marker)) return folder;
        if (Directory.Exists(folder)) Directory.Delete(folder, true);
        Directory.CreateDirectory(folder);
        log("Decompiling sound events with Source 2 Viewer");
        var outcome = await processes.RunAsync(new(vrf(), ["-i", input, "-o", folder, .. filter, "-d"], Timeout: TimeSpan.FromMinutes(10)), null, cancellationToken);
        if (outcome.ExitCode != 0) throw new InvalidDataException($"Source 2 Viewer could not decompile the sound events (exit code {outcome.ExitCode}). {string.Join(" ", outcome.Tail.TakeLast(3))}");
        await File.WriteAllTextAsync(marker, Identity, cancellationToken);
        return folder;
    }
}

/// <summary>Finds the external programs the port uses.</summary>
public static class ExternalTools
{
    /// <summary>Finds a program: the given path, then the host setting, then PATH.</summary>
    /// <param name="what">What the program is for, to finish the sentence "... is needed to ...".</param>
    public static string Locate(string? given, ISettings settings, string setting, string executable, string what)
    {
        foreach (var candidate in new[] { given, settings.Get(setting, "") })
        {
            if (string.IsNullOrWhiteSpace(candidate)) continue;
            var path = candidate.Trim().Trim('"');
            return File.Exists(path) ? Path.GetFullPath(path) : throw new FileNotFoundException($"{executable} was not found at {path}");
        }
        foreach (var folder in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            try
            {
                var path = Path.Combine(folder.Trim(), executable);
                if (File.Exists(path)) return path;
            }
            catch (ArgumentException)
            {
                continue;
            }
        }
        throw new FileNotFoundException($"{executable} is needed to {what}. Set its path here, in the {setting} setting, or put it on PATH.");
    }
}
