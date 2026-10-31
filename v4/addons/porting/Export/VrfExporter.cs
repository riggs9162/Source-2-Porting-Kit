namespace PortingKit.Addons.Porting;

/// <summary>One Source 2 Viewer run: a single input and the internal VPK paths to pull from it.</summary>
/// <remarks>The paths are empty for a loose file, a folder, or a whole-archive VPK. Several paths merge into one
/// filter so the archive opens only once.</remarks>
public sealed record ExportJob(string InputPath)
{
    public List<string> VpkFilepaths { get; init; } = [];
}

/// <summary>What one export run produces, matching the keywords of v3's export.</summary>
public sealed record VrfExportOptions
{
    /// <summary><c>models</c> exports glTF plus the materials tree; <c>vmats</c> exports materials and textures
    /// only, and skips every model-only step such as the stray-image purge and the sidecars.</summary>
    public string Mode { get; init; } = "models";

    public string GltfFormat { get; init; } = "glb";
    public bool Recursive { get; init; } = true;
    public int Threads { get; init; } = 4;
    public bool KeepStrayImages { get; init; }

    /// <summary>Moves every image and VMAT into a flat <c>materialsrc</c> root as the last step. This overrides
    /// <see cref="KeepStrayImages"/>: strays are swept into the root rather than kept or purged.</summary>
    public bool FlattenMaterialsRoot { get; init; }

    /// <summary>Prefixes inside a VPK, such as <c>models/props_c17/</c>. Only used when the input is a VPK.</summary>
    public IReadOnlyList<string> VpkFilepaths { get; init; } = [];

    /// <summary>When false the clips stay out, but the animation switch stays on with a reserved clip name, because
    /// the switch is what keeps the skin skeleton in the glTF.</summary>
    public bool IncludeAnimations { get; init; } = true;

    /// <summary>Bakes additive clips over the bind pose. The fidelity sidecar records it because later stages refuse
    /// delta clips that were not composed.</summary>
    public bool ComposeAdditive { get; init; }

    /// <summary>Fully checks a native LOD mesh. Only needed when a decompile fails in animation extraction; without
    /// it such a model keeps its glTF and sidecars but loses its lower LODs, with a warning.</summary>
    public Action<string>? NativeLodValidator { get; init; }
}

/// <summary>Drives Source2Viewer-CLI and writes every sidecar the later stages read.</summary>
/// <remarks>A project root receives a Source 1 sibling layout: <c>modelsrc/&lt;rest&gt;</c> holds each glTF and its
/// sidecars, <c>materialsrc/&lt;rest&gt;</c> the VMATs and decoded textures. The viewer writes into a private staging
/// folder so it can never touch compiled MDL, VTF or VMT trees already living beside those folders.</remarks>
public static partial class VrfExporter
{
    private const string BindOnlyClip = "__s2kit_bind_only__";

    public static bool IsVpk(string path) => PathText.Suffix(path).Equals(".vpk", StringComparison.OrdinalIgnoreCase);

    /// <summary>Source2Viewer ships a GUI and a CLI binary. Given the GUI, every argument token is opened as a file and
    /// the user sees a confusing "File '-i' does not exist", so the name is checked up front.</summary>
    private static bool LooksLikeCliBinary(string path) => PathText.Stem(path).Contains("cli", StringComparison.OrdinalIgnoreCase);

    /// <summary>Finds the CLI: the explicit path, then the host's <c>tools.vrf</c> setting, then PATH.</summary>
    public static string ResolveExecutable(string? explicitPath = null, ISettings? settings = null)
    {
        foreach (var candidate in new[] { explicitPath, settings?.Get("tools.vrf", "") })
        {
            if (string.IsNullOrEmpty(candidate) || !File.Exists(candidate)) continue;
            if (!LooksLikeCliBinary(candidate))
                throw new VrfRunnerException($"'{Path.GetFileName(candidate)}' looks like the GUI build of Source 2 Viewer. " +
                    "This tool needs the command-line build (Source2Viewer-CLI.exe), which is a separate download on the VRF releases page: " +
                    "https://github.com/ValveResourceFormat/ValveResourceFormat/releases");
            return PathText.Native(candidate);
        }
        return OnPath("Source2Viewer-CLI") ?? OnPath("Source2Viewer-CLI.exe") ?? throw new VrfRunnerException(
            "Could not locate Source2Viewer-CLI.exe. Pass an explicit path, set 'tools.vrf' in settings, or put it on your PATH. " +
            "Note: this is the CLI build, a separate download from the GUI Source2Viewer.exe.");
    }

    private static string? OnPath(string command)
    {
        var extensions = (Environment.GetEnvironmentVariable("PATHEXT") ?? "").Split(';', StringSplitOptions.RemoveEmptyEntries);
        var suffixed = extensions.Select(extension => command + extension);
        var names = extensions.Any(extension => command.EndsWith(extension, StringComparison.OrdinalIgnoreCase)) ? suffixed.Prepend(command) : suffixed;
        var folders = (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries).Prepend(Environment.CurrentDirectory);
        return folders.SelectMany(folder => names.Select(name => Path.Combine(folder, name))).FirstOrDefault(File.Exists);
    }

    /// <summary>Reads the viewer's right-click "Copy URL": <c>vpk:&lt;archive&gt;:&lt;resource&gt;</c>.</summary>
    /// <remarks>The archive path contains a drive colon of its own, so the split is anchored on the <c>.vpk:</c>
    /// boundary rather than the first colon. Returns null when the text is not a well-formed URL.</remarks>
    public static (string Archive, string Resource)? ParseCopyUrl(string text)
    {
        var value = text.Trim().Trim('"').Trim();
        if (!IsUrl(value)) return null;
        var body = value[4..];
        var at = body.IndexOf(".vpk:", StringComparison.OrdinalIgnoreCase);
        if (at < 0) return null;
        var archive = body[..(at + 4)];
        var resource = body[(at + 5)..].Trim().Replace('\\', '/').TrimStart('/');
        return archive.Length == 0 || resource.Length == 0 ? null : (archive, resource);
    }

    private static bool IsUrl(string value) => value.StartsWith("vpk:", StringComparison.OrdinalIgnoreCase);

    /// <summary>The key that groups entries naming one archive; Windows paths ignore case and separator style.</summary>
    private static string VpkKey(string path)
    {
        var text = PathText.Native(path);
        var root = Path.GetPathRoot(text) ?? "";
        var parts = new List<string>();
        foreach (var part in text[root.Length..].Split(Path.DirectorySeparatorChar, StringSplitOptions.RemoveEmptyEntries))
        {
            if (part == ".") continue;
            if (part != "..") parts.Add(part);
            else if (parts.Count > 0 && parts[^1] != "..") parts.RemoveAt(parts.Count - 1);
            else if (root.Length == 0) parts.Add(part);
        }
        return (root + string.Join(Path.DirectorySeparatorChar, parts)).ToLowerInvariant();
    }

    /// <summary>Resolves a batch of path-like entries into deduplicated jobs.</summary>
    /// <remarks>Each entry is a copy URL, a bare internal path paired with a VPK <paramref name="defaultInput"/>, or a
    /// filesystem path to a file, a VPK or a folder. Entries naming one VPK merge into a single job. Blank lines and
    /// <c>#</c> comments are ignored. Entries that resolve to nothing are returned as problems.</remarks>
    public static (List<ExportJob> Jobs, List<string> Problems) BuildExportJobs(IEnumerable<string> entries, string? defaultInput = null)
    {
        var jobs = new List<ExportJob>();
        var problems = new List<string>();
        var archives = new Dictionary<string, ExportJob>();
        var defaultIsVpk = defaultInput is not null && IsVpk(defaultInput);

        ExportJob Archive(string path)
        {
            var key = VpkKey(path);
            if (archives.TryGetValue(key, out var existing)) return existing;
            var job = new ExportJob(PathText.Native(path));
            archives[key] = job;
            jobs.Add(job);
            return job;
        }

        static void AddFilter(ExportJob job, string resource)
        {
            if (resource.Length > 0 && !job.VpkFilepaths.Contains(resource)) job.VpkFilepaths.Add(resource);
        }

        foreach (var raw in entries)
        {
            var line = raw.Trim();
            if (line.Length == 0 || line.StartsWith('#')) continue;
            if (ParseCopyUrl(line) is { } url)
            {
                AddFilter(Archive(url.Archive), url.Resource);
                continue;
            }
            if (IsUrl(line))
            {
                problems.Add($"Malformed VRF URL (expected vpk:<archive>.vpk:<path>): {line}");
                continue;
            }
            var candidate = line.Trim('"');
            if (IsVpk(candidate))
            {
                Archive(candidate);
                continue;
            }
            if (Path.Exists(candidate))
            {
                jobs.Add(new(PathText.Native(candidate)));
                continue;
            }
            var normal = candidate.Replace('\\', '/').TrimStart('/');
            if (defaultIsVpk && normal.Contains('/')) AddFilter(Archive(defaultInput!), normal);
            else problems.Add($"Could not resolve entry (no matching file or VPK context): {line}");
        }
        return (jobs, problems);
    }

    /// <summary>Adds the parallel <c>materials/&lt;path&gt;</c> so VMATs and textures get pulled too.</summary>
    /// <remarks>A model at <c>models/X/foo.vmdl_c</c> conventionally references materials under
    /// <c>materials/models/X/</c>, and the viewer only writes source VMATs when they are primary targets. Character
    /// materials instead live beside the model, which is why both trees have to be covered. No-op for any other root.</remarks>
    public static string ExpandVpkFilter(string vpkFilepath)
    {
        var path = vpkFilepath.Replace('\\', '/').TrimStart('/');
        return path.StartsWith("models/", StringComparison.Ordinal) ? $"{path},materials/{path}" : path;
    }

    /// <summary>Expands several internal paths into one deduplicated token list, in first-seen order.</summary>
    public static List<string> ExpandVpkFilters(IEnumerable<string> vpkFilepaths) =>
        MergeFilters(vpkFilepaths.Where(path => !string.IsNullOrWhiteSpace(path)).Select(ExpandVpkFilter));

    /// <summary>Flattens and deduplicates filter strings in first-seen order, then drops covered tokens.</summary>
    public static List<string> MergeFilters(params IEnumerable<string>[] groups)
    {
        var seen = new HashSet<string>();
        var merged = new List<string>();
        foreach (var group in groups)
            foreach (var raw in group)
                foreach (var token in raw.Split(','))
                {
                    var text = token.Trim().Replace('\\', '/');
                    if (text.Length > 0 && seen.Add(text)) merged.Add(text);
                }
        return PruneCoveredFilters(merged);
    }

    /// <summary>Drops tokens that start with another token, ignoring case.</summary>
    /// <remarks>The viewer's filter is a starts-with match, so such a token can never widen the set. This keeps
    /// reference lists of 1500+ entries off the command-line limit, and it is also why a model's own
    /// <c>.vmdl_c</c> filter disappears once its <c>.vmdl</c> reference joins the list.</remarks>
    private static List<string> PruneCoveredFilters(List<string> filters)
    {
        var lowered = filters.Select(filter => filter.ToLowerInvariant()).ToList();
        var kept = new List<string>();
        for (var index = 0; index < filters.Count; index++)
        {
            var covered = false;
            for (var other = 0; other < lowered.Count && !covered; other++)
                covered = other != index && lowered[index].StartsWith(lowered[other], StringComparison.Ordinal) &&
                    (lowered[other].Length < lowered[index].Length || other < index);
            if (!covered) kept.Add(filters[index]);
        }
        return kept;
    }

    /// <summary>Keeps references no filter already matches; only out-of-tree ones need their own entry.</summary>
    private static List<string> ReferencesOutsideFilters(IEnumerable<string> references, IEnumerable<string> filters)
    {
        var lowered = filters.Select(filter => filter.ToLowerInvariant()).ToList();
        return references.Where(reference => !lowered.Any(filter => reference.ToLowerInvariant().StartsWith(filter, StringComparison.Ordinal))).ToList();
    }

    /// <summary>Composes the viewer's arguments, with the executable first.</summary>
    /// <remarks><c>--gltf_export_materials</c> is required to get a materials array with real VMAT names; without it
    /// the converter falls back to a literal "material" and the MDL's <c>$cdmaterials</c> lookup breaks. Its cost is
    /// the hash-suffixed PNGs dropped beside the glTF, which the stray-image purge removes afterwards.</remarks>
    public static List<string> BuildCommand(string executable, string input, string output, VrfExportOptions options, IEnumerable<string>? extraFilters = null)
    {
        RequireMode(options.Mode);
        List<string> arguments = [executable, "-i", input, "-o", output, "-d"];
        if (options.Mode == "models")
        {
            if (options.GltfFormat is not ("gltf" or "glb")) throw new VrfRunnerException($"gltf_format must be 'gltf' or 'glb' (got {PathText.Repr(options.GltfFormat)})");
            arguments.AddRange(["--gltf_export_format", options.GltfFormat, "--gltf_export_materials", "--gltf_export_extras", "--gltf_export_animations"]);
            if (!options.IncludeAnimations) arguments.AddRange(["--gltf_animation_list", BindOnlyClip]);
            if (options.ComposeAdditive) arguments.Add("--gltf_compose_additive");
        }
        arguments.AddRange(["--threads", Fmt.Int(options.Threads), "-e", options.Mode == "models" ? "vmdl_c,vmat_c,vtex_c" : "vmat_c,vtex_c"]);
        if (options.Recursive && Directory.Exists(input)) arguments.Add("--recursive");
        if (IsVpk(input) && options.VpkFilepaths.Count > 0)
        {
            var merged = MergeFilters(ExpandVpkFilters(options.VpkFilepaths), extraFilters ?? []);
            if (merged.Count > 0) arguments.AddRange(["-f", string.Join(',', merged)]);
        }
        return arguments;
    }

    private static void RequireMode(string mode)
    {
        if (mode is not ("models" or "vmats")) throw new VrfRunnerException($"mode must be 'models' or 'vmats' (got {PathText.Repr(mode)})");
    }

    /// <summary>A stable folder name for one resource, independent of queue order.</summary>
    /// <remarks>Each model exports into its own folder so an unrelated resource cannot block it. Goldens and later
    /// stages key <c>extracted/&lt;key&gt;</c> on this value.</remarks>
    public static string ResourceKey(string source, string resource) =>
        Digest.Text(Path.GetFullPath(source).ToLowerInvariant() + ":" + resource.ToLowerInvariant())[..16];
}
