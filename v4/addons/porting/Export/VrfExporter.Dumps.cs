using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

public static partial class VrfExporter
{
    /// <summary>Windows caps a command line at 32767 characters, and filter lists derived from references can exceed
    /// that on a multi-subtree export. Lists are pruned first, then split across several runs as a last resort.</summary>
    private const int MaxCommandLine = 30000;

    /// <summary>Resource paths in <c>-b RERL</c> output. References carry their source extension, not the compiled one.</summary>
    [GeneratedRegex(@"([A-Za-z][\w./\-]*?/[\w./\-]+\.(?:vmat|vtex|vmdl|vmesh|vphys|vsnd|vsndevts))", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant)]
    private static partial Regex ReferencePath();

    /// <summary>The per-file header in a block dump, such as <c>[2/3] models/foo.vmdl_c</c>.</summary>
    [GeneratedRegex(@"^\[\d+/\d+\]\s+(\S+\.vmdl_c)\s*$", RegexOptions.Multiline)]
    private static partial Regex DumpHeader();

    /// <summary>One <c>m_materialGroups</c> entry: a name followed shortly by its material array.</summary>
    [GeneratedRegex(@"m_name\s*=\s*""(?<name>[^""]*)""[^{]*?m_materials\s*=\s*\[(?<mats>[^\]]*)\]", RegexOptions.Singleline)]
    private static partial Regex MaterialGroup();

    [GeneratedRegex(@"resource:""([^""]+)""")]
    private static partial Regex ResourceReference();

    /// <summary>Splits tokens so each command line stays under the limit. One chunk in the common case.</summary>
    private static List<List<string>> SplitFilters(IReadOnlyList<string> arguments, IReadOnlyList<string> filters, int limit = MaxCommandLine)
    {
        if (filters.Count == 0) return [];
        var budget = limit - PathText.CommandLine([.. arguments, "-f", ""]).Length;
        var chunks = new List<List<string>>();
        var current = new List<string>();
        var length = 0;
        foreach (var token in filters)
        {
            var added = token.Length + (current.Count > 0 ? 1 : 0);
            if (current.Count > 0 && length + added > budget)
            {
                chunks.Add(current);
                current = [];
                length = 0;
                added = token.Length;
            }
            current.Add(token);
            length += added;
        }
        if (current.Count > 0) chunks.Add(current);
        return chunks;
    }

    private static async Task<(int Code, string Output)> CaptureAsync(IReadOnlyList<string> arguments, PortContext context)
    {
        var lines = new List<string>();
        var result = await context.Processes.RunAsync(new(arguments[0], arguments.Skip(1).ToArray()), line => lines.Add(line.Text), context.Token);
        return (result.ExitCode, string.Join('\n', lines));
    }

    /// <summary>Runs <c>-b &lt;block&gt;</c> over the filtered entries and returns the combined output.</summary>
    /// <remarks>The filter list splits across several processes when one command line would exceed the Windows limit.
    /// Returns null when nothing could be dumped: a launch failure, or every pass exiting non-zero. Partial output
    /// from a multi-pass run is still returned, with the failed passes logged.</remarks>
    public static async Task<string?> RunBlockDumpAsync(string executable, string input, string vpkFilepath, string entityFilter, string block, PortContext context)
    {
        var filters = vpkFilepath.Split(',').Select(token => token.Trim()).Where(token => token.Length > 0).ToList();
        List<string> bare = [executable, "-i", input, "-e", entityFilter, "-b", block];
        var chunks = SplitFilters(bare, filters);
        if (chunks.Count > 1) context.Log($"Filter list needs {chunks.Count} `-b {block}` passes (command-line length limit)");
        var outputs = new List<string>();
        var failed = 0;
        foreach (var chunk in chunks)
        {
            List<string> arguments = [.. bare, "-f", string.Join(',', chunk)];
            context.Log("Pre-pass: " + PathText.Format(arguments));
            int code;
            string output;
            try
            {
                (code, output) = await CaptureAsync(arguments, context);
            }
            catch (Exception exception) when (ExportErrors.IsStart(exception))
            {
                context.Log($"`-b {block}` dump failed to launch: {ExportErrors.Start(executable, exception)}");
                return null;
            }
            if (code != 0)
            {
                failed++;
                context.Log($"`-b {block}` pass exited with code {code}");
                continue;
            }
            outputs.Add(output);
        }
        return failed > 0 && outputs.Count == 0 ? null : string.Join('\n', outputs);
    }

    /// <summary>Lists the resources the matching entries reference, in source-extension form.</summary>
    /// <remarks>Safe on failure: an unreadable dump logs and yields nothing.</remarks>
    public static async Task<List<string>> DumpRerlAsync(string executable, string input, string vpkFilepath, string entityFilter, PortContext context)
    {
        var output = await RunBlockDumpAsync(executable, input, vpkFilepath, entityFilter, "RERL", context);
        if (output is null)
        {
            context.Log("RERL pre-pass failed; continuing without references");
            return [];
        }
        return ReferencePath().Matches(output).Select(match => match.Groups[1].Value.Replace('\\', '/')).Distinct().ToList();
    }

    /// <summary>Splits a multi-model dump by file header, retaining all repeated MDAT blocks.</summary>
    /// <remarks>Dump text stays exact here: each section is the literal text after its header, which is what the raw
    /// block files store. Text with no header belongs to <paramref name="fallback"/> when it holds a KV3 document.</remarks>
    public static Dictionary<string, string> ModelDumpSections(string text, string fallback = "")
    {
        var matches = DumpHeader().Matches(text);
        if (matches.Count == 0) return fallback.Length > 0 && text.Contains("<!-- kv3", StringComparison.Ordinal) ? new() { [fallback] = text } : [];
        var sections = new Dictionary<string, string>();
        for (var index = 0; index < matches.Count; index++)
        {
            var start = matches[index].Index + matches[index].Length;
            var end = index + 1 < matches.Count ? matches[index + 1].Index : text.Length;
            sections[matches[index].Groups[1].Value.Replace('\\', '/')] = text[start..end];
        }
        return sections;
    }

    /// <summary>Splits a dump by header one line at a time, rebuilding each section from its lines.</summary>
    /// <remarks>Unlike <see cref="ModelDumpSections"/> this drops the blank line after each header and any trailing
    /// newline, which is the exact content <c>_phys.txt</c> and the raw PHYS block have always held.</remarks>
    private static Dictionary<string, string> LineSections(string text)
    {
        var sections = new Dictionary<string, string>();
        string? path = null;
        var lines = new List<string>();
        foreach (var line in PathText.SplitLines(text))
        {
            var match = DumpHeader().Match(line);
            if (!match.Success)
            {
                if (path is not null) lines.Add(line);
                continue;
            }
            if (path is not null) sections[path] = string.Join('\n', lines);
            path = match.Groups[1].Value.Replace('\\', '/');
            lines = [];
        }
        if (path is not null) sections[path] = string.Join('\n', lines);
        return sections;
    }

    /// <summary>Reads the <c>m_materialGroups</c> array from one file's DATA section.</summary>
    /// <remarks>Bracket depth is tracked so the nested material arrays cannot end the block early.</remarks>
    private static List<(string Name, List<string> Materials)> MaterialGroupsBlock(string text)
    {
        var start = text.IndexOf("m_materialGroups", StringComparison.Ordinal);
        if (start < 0) return [];
        var open = text.IndexOf('[', start);
        if (open < 0) return [];
        var depth = 1;
        var at = open + 1;
        while (at < text.Length && depth > 0)
        {
            if (text[at] == '[') depth++;
            else if (text[at] == ']' && --depth == 0) break;
            at++;
        }
        return MaterialGroup().Matches(text[(open + 1)..at]).Select(match => (match.Groups["name"].Value,
            ResourceReference().Matches(match.Groups["mats"].Value).Select(reference => reference.Groups[1].Value).ToList())).ToList();
    }

    /// <summary>Extracts each model's material groups from a DATA dump. The first group is the default skin.</summary>
    private static Dictionary<string, List<(string Name, List<string> Materials)>> ParseMaterialGroups(string text) =>
        LineSections(text).Select(section => (section.Key, Groups: MaterialGroupsBlock(section.Value))).Where(item => item.Groups.Count > 0)
            .ToDictionary(item => item.Key, item => item.Groups);

    /// <summary>Returns each model's PHYS text, the lossless physics source for back-porting.</summary>
    /// <remarks>Per-bone shapes, bind poses and joints with real limits survive only here, not in the merged physics
    /// glTF. Models without real parts are dropped; their empty blocks would confuse the collision stage.
    /// Safe on failure: an unreadable dump logs and yields nothing.</remarks>
    public static async Task<Dictionary<string, string>> DumpPhysBlocksAsync(string executable, string input, string vpkFilepath, PortContext context)
    {
        context.Log("Dumping physics (PHYS) blocks via `-b PHYS`...");
        var output = await RunBlockDumpAsync(executable, input, vpkFilepath, "vmdl_c", "PHYS", context);
        if (output is null)
        {
            context.Log("PHYS dump failed; continuing without physics data");
            return [];
        }
        return LineSections(output).Where(section => section.Value.Contains("m_parts", StringComparison.Ordinal) && section.Value.Contains("m_flMass", StringComparison.Ordinal))
            .ToDictionary(section => section.Key, section => section.Value);
    }

    /// <summary>Reads the authored fields of each model from its DATA, MDAT, ANIM and ASEQ dumps.</summary>
    /// <param name="dataDump">A DATA dump already in hand, reused instead of running the viewer again.</param>
    /// <param name="includeAnimations">False reads only DATA and MDAT.</param>
    /// <param name="blockDumps">Receives the split dumps so the caller can store them.</param>
    public static async Task<Dictionary<string, ModelMetadata>> DumpModelMetadataAsync(string executable, string input, string vpkFilepath, PortContext context,
        string? dataDump = null, bool includeAnimations = true, Dictionary<string, Dictionary<string, string>>? blockDumps = null)
    {
        context.Log("Reading authored hitboxes, attachments and animation metadata...");
        var fallback = PathText.Suffix(input).Equals(".vmdl_c", StringComparison.OrdinalIgnoreCase) ? Path.GetFileName(input) : "";
        var dumps = new Dictionary<string, Dictionary<string, string>>();
        foreach (var block in includeAnimations ? new[] { "DATA", "MDAT", "ANIM", "ASEQ" } : ["DATA", "MDAT"])
        {
            var text = block == "DATA" && dataDump is not null ? dataDump : await RunBlockDumpAsync(executable, input, vpkFilepath, "vmdl_c", block, context);
            dumps[block] = ModelDumpSections(text ?? "", fallback);
            if (block == "DATA" && dumps[block].Count == 1) fallback = dumps[block].Keys.First();
        }
        if (blockDumps is not null)
            foreach (var (block, sections) in dumps) blockDumps[block] = sections;

        string Section(string block, string path) => dumps.TryGetValue(block, out var sections) ? sections.GetValueOrDefault(path, "") : "";

        var result = new Dictionary<string, ModelMetadata>();
        foreach (var path in dumps.Values.SelectMany(sections => sections.Keys).Distinct())
        {
            ModelMetadata metadata;
            try
            {
                metadata = ModelMetadata.Parse(Section("DATA", path), Section("MDAT", path), Section("ANIM", path), Section("ASEQ", path));
            }
            catch (Exception exception) when (ExportErrors.IsData(exception))
            {
                context.Log($"{path}: authored metadata could not be read: {exception.Message}");
                continue;
            }
            result[path] = metadata;
            foreach (var warning in metadata.Warnings) context.Log($"{path}: {warning}");
        }
        return result;
    }

    private static string ModelBase(string path)
    {
        foreach (var extension in new[] { ".vmdl_c", ".vmdl" })
            if (path.EndsWith(extension, StringComparison.OrdinalIgnoreCase)) return path[..^extension.Length];
        return path;
    }

    /// <summary>Places <c>.metadata.json</c> beside each glTF for the native model porter.</summary>
    public static int WriteMetadataSidecars(string staging, IReadOnlyDictionary<string, ModelMetadata> metadata, PortContext context)
    {
        foreach (var (path, value) in metadata)
        {
            var stem = path.EndsWith(".vmdl_c", StringComparison.OrdinalIgnoreCase) ? path[..^7] : path.EndsWith(".vmdl", StringComparison.Ordinal) ? path[..^5] : path;
            var relative = stem + ".metadata.json";
            if (Path.IsPathFullyQualified(relative) || relative.Split('/', '\\').Contains("..")) throw new VrfRunnerException($"Unsafe model metadata path: {path}");
            value.Write(Path.Combine(staging, PathText.Native(relative)));
        }
        context.Log($"Wrote {metadata.Count} authored model metadata sidecar(s)");
        return metadata.Count;
    }

    /// <summary>Writes each PHYS text as <c>&lt;stem&gt;_phys.txt</c> beside its glTF.</summary>
    /// <remarks>The file uses the platform's line endings, while the copy under <c>_fidelity</c> keeps LF. Goldens and
    /// the collision builder both accept this, so it stays as v3 wrote it.</remarks>
    public static int WritePhysSidecars(string staging, IReadOnlyDictionary<string, string> blocks, PortContext context)
    {
        foreach (var (path, text) in blocks)
            Fmt.WriteText(Path.Combine(staging, PathText.Native(ModelBase(path) + "_phys.txt")), text.Replace("\n", Environment.NewLine));
        context.Log($"Wrote {blocks.Count} physics sidecar(s)");
        return blocks.Count;
    }

    /// <summary>The bare name of a material reference, without folder or <c>.vmat</c>.</summary>
    private static string VmatStem(string reference)
    {
        var text = reference.Replace('\\', '/').Trim().TrimEnd('/');
        text = text[(text.LastIndexOf('/') + 1)..];
        return text.EndsWith(".vmat", StringComparison.OrdinalIgnoreCase) ? text[..^5] : text;
    }

    /// <summary>Writes <c>.skins.json</c> for each model with several material groups.</summary>
    /// <remarks>Models with one group or none are skipped because they have no skins to declare.</remarks>
    private static int WriteSkinSidecars(string staging, Dictionary<string, List<(string Name, List<string> Materials)>> groups, PortContext context)
    {
        var written = 0;
        foreach (var (path, skins) in groups)
        {
            if (skins.Count <= 1) continue;
            Json.Save(Path.Combine(staging, PathText.Native(ModelBase(path) + ".skins.json")), new JsonObject
            {
                ["schema"] = "skins.v1",
                ["skins"] = new JsonArray(skins.Select(skin => (JsonNode?)new JsonObject
                {
                    ["name"] = skin.Name,
                    ["materials"] = new JsonArray(skin.Materials.Select(material => (JsonNode?)VmatStem(material)).ToArray())
                }).ToArray())
            });
            written++;
        }
        context.Log($"Wrote {written} skin sidecar(s)");
        return written;
    }
}
