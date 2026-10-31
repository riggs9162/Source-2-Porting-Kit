using System.Buffers.Binary;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>The files a compiled model is made of, and whether they agree.</summary>
/// <remarks>
/// The engine refuses a model whose .vvd or .vtx checksum differs from the .mdl's, reporting
/// "Error Vertex File ... checksum X should be Y" and then "Bad geometry". That happens when a file does not
/// get rewritten, usually because the game had the model loaded. A stale .phy is quieter and worse: the engine
/// loads it whenever it is there, so a recompile without collision keeps the collision from an earlier compile.
/// </remarks>
public sealed class CompiledModel(string path)
{
    public string Path { get; } = path;
    public int? Checksum { get; init; }
    public Dictionary<string, int?> Siblings { get; } = new(StringComparer.OrdinalIgnoreCase);
    public List<string> Missing { get; } = [];
    public IEnumerable<string> Stale => Siblings.Where(pair => pair.Value != Checksum).Select(pair => pair.Key);
    public bool Ok => Checksum is not null && Missing.Count == 0 && !Stale.Any();

    /// <summary>One line per defect, in the words the engine would use.</summary>
    public List<string> Problems()
    {
        if (Checksum is null) return [$"{System.IO.Path.GetFileName(Path)} is not a readable .mdl"];
        var stem = System.IO.Path.GetFileNameWithoutExtension(Path);
        var lines = Missing.ConvertAll(suffix => $"{stem}{suffix} is missing");
        foreach (var path in Stale)
        {
            var found = Siblings[path]?.ToString(System.Globalization.CultureInfo.InvariantCulture) ?? "unreadable";
            var name = System.IO.Path.GetFileName(path);
            lines.Add(path.EndsWith(".phy", StringComparison.OrdinalIgnoreCase)
                ? $"{name} checksum {found} should be {Checksum} - it is left over from an earlier compile, and the engine loads it anyway, so the model still has that compile's collision"
                : $"{name} checksum {found} should be {Checksum} - it is left over from an earlier compile");
        }
        return lines;
    }
}

/// <summary>Runs studiomdl and verifies what it left behind.</summary>
/// <remarks>The game argument must be the folder containing gameinfo.txt. For StudioMDL CE that is
/// <c>install/csgo</c>; passing the install root crashes the compiler instead of printing an error.</remarks>
public static partial class Studiomdl
{
    /// <summary>The VTX flavours studiomdl writes. Garry's Mod loads dx90; the others are checked when present.</summary>
    public static readonly string[] VtxSuffixes = [".dx90.vtx", ".dx80.vtx", ".sw.vtx"];

    private static readonly string[] SdkVariables = ["VPROJECT", "SteamAppId", "SteamGameId"];

    [GeneratedRegex(@"^\s*\$(?:body|bodygroup|model)\b", RegexOptions.Multiline | RegexOptions.IgnoreCase)]
    private static partial Regex GeometryCommand();

    /// <summary>Whether a QC declares any render geometry.</summary>
    public static bool HasGeometry(string qcPath) => GeometryCommand().IsMatch(File.ReadAllText(qcPath));

    private static int? ChecksumOf(string path, int offset)
    {
        try
        {
            using var stream = File.OpenRead(path);
            Span<byte> bytes = stackalloc byte[4];
            stream.Position = offset;
            return stream.ReadAtLeast(bytes, 4, false) == 4 ? BinaryPrimitives.ReadInt32LittleEndian(bytes) : null;
        }
        catch (IOException)
        {
            return null;
        }
    }

    /// <summary>Reads the checksum out of a model's .mdl, .vvd, .vtx and .phy.</summary>
    /// <remarks>An unwritten .vtx flavour is not reported, and neither is an absent .phy, since collision is
    /// optional. A missing .vvd is, and so is any present file carrying a different compile's checksum.</remarks>
    public static CompiledModel CheckCompiledModel(string mdlPath, bool requireGeometry = true)
    {
        var model = new CompiledModel(mdlPath) { Checksum = ChecksumOf(mdlPath, 8) };
        if (model.Checksum is null) return model;
        var vvd = Path.ChangeExtension(mdlPath, ".vvd");
        if (File.Exists(vvd)) model.Siblings[vvd] = ChecksumOf(vvd, 8);
        else if (requireGeometry) model.Missing.Add(".vvd");
        var phy = Path.ChangeExtension(mdlPath, ".phy");
        if (File.Exists(phy)) model.Siblings[phy] = ChecksumOf(phy, 12);
        var foundVtx = false;
        foreach (var suffix in VtxSuffixes)
        {
            var vtx = Path.ChangeExtension(mdlPath, null) + suffix;
            if (!File.Exists(vtx)) continue;
            foundVtx = true;
            model.Siblings[vtx] = ChecksumOf(vtx, 16);
        }
        if (!foundVtx && requireGeometry) model.Missing.Add(".dx90.vtx");
        return model;
    }

    /// <summary>Where the QC's $modelname lands beneath the game's models folder.</summary>
    public static string? ModelPathFor(string qcPath, string game)
    {
        if (!File.Exists(qcPath)) return null;
        foreach (var line in File.ReadLines(qcPath))
        {
            var text = line.Trim();
            if (!text.StartsWith("$modelname", StringComparison.OrdinalIgnoreCase)) continue;
            var name = text["$modelname".Length..].Trim().Trim('"').Trim();
            if (name.Length == 0) return null;
            var target = Path.Combine(game, "models", name);
            return Path.HasExtension(target) ? target : target + ".mdl";
        }
        return null;
    }

    /// <summary>Finds the folder with gameinfo.txt, accepting a StudioMDL CE install root.</summary>
    public static string? ResolveGameDirectory(string game)
    {
        if (File.Exists(Path.Combine(game, "gameinfo.txt"))) return game;
        var nested = Path.Combine(game, "csgo");
        return File.Exists(Path.Combine(nested, "gameinfo.txt")) ? nested : null;
    }

    /// <summary>Resolves a setting that may name gameinfo.txt itself or a folder.</summary>
    public static string? ResolveGameArgument(string? value)
    {
        var text = value?.Trim() ?? "";
        if (text.Length == 0) return null;
        if (File.Exists(text)) return Path.GetFileName(text).Equals("gameinfo.txt", StringComparison.OrdinalIgnoreCase) ? Path.GetDirectoryName(text) : null;
        return Directory.Exists(text) ? ResolveGameDirectory(text) : null;
    }

    /// <summary>Compiles a QC, true only when studiomdl reports completion and every file agrees.</summary>
    /// <remarks>Exit code 0 is not enough: studiomdl returns it on several failures, so "Completed" must appear
    /// too. Even then a file it could not open keeps its old contents, so checksums are verified afterwards.</remarks>
    public static async Task<bool> CompileQcAsync(string qcPath, string studiomdl, string game, PortContext context)
    {
        var resolved = ResolveGameArgument(game) ?? ResolveGameDirectory(game);
        if (resolved is null)
        {
            context.Log($"-game is invalid: {game} has no gameinfo.txt (and no csgo subfolder with one) - studiomdl would crash", LogLevel.Error);
            return false;
        }
        var output = new List<string>();
        void Collect(ProcessLine line)
        {
            output.Add(line.Text);
            var lowered = line.Text.ToLowerInvariant();
            if (lowered.Contains("error", StringComparison.Ordinal)) context.Log(line.Text, LogLevel.Error);
            else if (lowered.Contains("warning", StringComparison.Ordinal)) context.Log(line.Text, LogLevel.Warning);
        }
        context.Log($"compiling: {studiomdl} -game {resolved} -nop4 {qcPath}");
        ProcessResult result;
        try
        {
            result = await context.Processes.RunAsync(new(studiomdl, ["-game", resolved, "-nop4", qcPath], Path.GetDirectoryName(qcPath), null, SdkVariables), Collect, context.Token);
        }
        catch (Exception exception) when (exception is IOException or System.ComponentModel.Win32Exception)
        {
            context.Log($"studiomdl could not be run: {exception.Message}", LogLevel.Error);
            return false;
        }
        foreach (var line in output.TakeLast(3)) context.Log(line);
        var compiled = result.ExitCode == 0 && output.Any(line => line.Contains("Completed", StringComparison.Ordinal));
        if (compiled && ModelPathFor(qcPath, resolved) is { } target && File.Exists(target))
        {
            var model = CheckCompiledModel(target, HasGeometry(qcPath));
            foreach (var problem in model.Problems()) context.Log(problem, LogLevel.Error);
            if (!model.Ok)
            {
                context.Log("studiomdl reported success but left a file from an earlier compile - the engine will refuse this model. Close whatever has it open and compile again.", LogLevel.Error);
                compiled = false;
            }
        }
        context.Log("compile " + (compiled ? "succeeded" : "FAILED"), compiled ? LogLevel.Success : LogLevel.Error);
        return compiled;
    }

    /// <summary>Writes a project-local game so a compile lands in the workflow, not the configured game folder.</summary>
    /// <param name="project">The workflow project folder.</param>
    /// <param name="game">The configured compiler game, mounted read-only for shared resources.</param>
    /// <param name="materialPayload">An addon payload to mount, so the compiler sees the ported materials.</param>
    public static string CompilerGame(string project, string game, string? materialPayload = null)
    {
        var original = ResolveGameArgument(game) ?? throw new InvalidDataException("Set the compiler game folder containing gameinfo.txt.");
        var target = Path.Combine(Path.GetFullPath(project), "compiler-game");
        Directory.CreateDirectory(target);
        var mount = original.Replace('\\', '/');
        var materials = materialPayload is null ? "" : $"            Game \"{materialPayload.Replace('\\', '/')}\"\n";
        var text = "\"GameInfo\"\n{\n    game \"Source 2 Porting Kit\"\n    FileSystem\n    {\n        SteamAppId 730\n        ToolsAppId 211\n        SearchPaths\n        {\n            Game \"|gameinfo_path|.\"\n" +
            $"{materials}            Game \"{mount}\"\n        }}\n    }}\n}}\n";
        var path = Path.Combine(target, "gameinfo.txt");
        if (!File.Exists(path) || File.ReadAllText(path) != text) Fmt.WriteText(path, text);
        return target;
    }
}
