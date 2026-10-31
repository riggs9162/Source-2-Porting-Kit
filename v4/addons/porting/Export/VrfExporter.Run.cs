using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

public static partial class VrfExporter
{
    /// <summary>Runs a full export into a project root and returns the viewer's exit code.</summary>
    public static async Task<int> RunExportAsync(string executable, string source, string destination, VrfExportOptions options, PortContext context)
    {
        RequireMode(options.Mode);
        executable = PathText.Native(executable);
        source = PathText.Native(source);
        destination = PathText.Native(destination);
        if (!File.Exists(executable)) throw new VrfRunnerException($"VRF executable not found: {executable}");
        if (!Path.Exists(source)) throw new VrfRunnerException($"Input path does not exist: {source}");
        var paths = options.VpkFilepaths.Where(path => !string.IsNullOrWhiteSpace(path)).ToList();
        options = options with { VpkFilepaths = paths };
        Directory.CreateDirectory(destination);
        var staging = Path.Combine(destination, ".vrf_staging");
        DeleteTree(staging);
        Directory.CreateDirectory(staging);
        List<string> extraFilters = IsVpk(source) && paths.Count > 0 ? await ResolveReferencesAsync(executable, source, paths, options.Mode, context) : [];
        var code = await RunPassesAsync(BuildCommand(executable, source, staging, options, extraFilters), context);
        if (code != 0)
        {
            context.Log($"VRF exited with code {code}");
            return code;
        }
        if (options.Mode == "models")
        {
            if (options.FlattenMaterialsRoot) context.Log("Flatten-to-root enabled: stray images will be swept into materialsrc/ root (stray-image option overridden)");
            else if (!options.KeepStrayImages) PurgeStrayImagesNextToGltf(staging, context);
            else context.Log("Skipping stray-image cleanup (keep_stray_images=True)");
            if (IsVpk(source) && paths.Count > 0) await WriteModelSidecarsAsync(executable, source, staging, options, context);
        }
        var packages = DetectNestedPackageDirs(staging);
        HoistInnerMaterialsToTop(staging, context);
        ReorganizeToProjectLayout(staging, destination, context);
        DeleteTree(staging);
        var modelsrc = Path.Combine(destination, "modelsrc");
        var materialsrc = Path.Combine(destination, "materialsrc");
        PurgeInnerMaterialsDirs(modelsrc, context);
        PurgeUnreferencedTextures(materialsrc, context);
        PurgeCompiledTextureImages(materialsrc, context);
        PurgeCompiledTextures(materialsrc, context);
        if (packages.Count > 0) FlattenNestedPackages(destination, packages, context);
        if (options.FlattenMaterialsRoot) FlattenMaterialsToRoot(destination, context);
        return code;
    }

    /// <summary>Finds the out-of-tree resources a selection depends on, so their exact paths can join the filter.</summary>
    /// <remarks>Models recurse one level: each discovered VMAT is asked for its VTEX files by exact path. Falling back
    /// to parent folders would pull every unrelated texture sharing the VMAT's folder. Materials under the requested
    /// paths are already targets, so only out-of-tree ones are named, which also keeps the command line short.</remarks>
    private static async Task<List<string>> ResolveReferencesAsync(string executable, string source, IReadOnlyList<string> paths, string mode, PortContext context)
    {
        var prefixes = ExpandVpkFilters(paths);
        var joined = string.Join(',', prefixes);
        List<string> references;
        if (mode == "models")
        {
            context.Log("Resolving model references via `-b RERL`...");
            var first = await DumpRerlAsync(executable, source, joined, "vmdl_c", context);
            var materials = first.Where(reference => reference.EndsWith(".vmat", StringComparison.OrdinalIgnoreCase)).ToList();
            var second = new List<string>();
            if (materials.Count > 0)
            {
                context.Log($"Resolving texture references from {materials.Count} material(s)...");
                second = await DumpRerlAsync(executable, source, string.Join(',', MergeFilters(prefixes, materials)), "vmat_c", context);
            }
            references = first.Concat(second).Distinct().ToList();
        }
        else
        {
            context.Log("Resolving texture references via `-b RERL`...");
            references = await DumpRerlAsync(executable, source, joined, "vmat_c", context);
        }
        if (references.Count == 0)
        {
            context.Log("No references resolved; falling back to parallel-path filter only.");
            return [];
        }
        var extra = ReferencesOutsideFilters(references, prefixes);
        context.Log($"Found {references.Count} referenced resource(s); {references.Count - extra.Count} already inside the requested paths");
        if (extra.Count > 0) context.Log($"Adding {extra.Count} out-of-tree reference(s):");
        foreach (var reference in extra) context.Log($"  - {reference}");
        return extra;
    }

    /// <summary>Runs an export, spreading a filter list too long for one command line across several passes.</summary>
    /// <remarks>Passes write into the same staging folder and their outputs are per file, so they compose.</remarks>
    private static async Task<int> RunPassesAsync(List<string> arguments, PortContext context)
    {
        var commands = new List<List<string>> { arguments };
        var at = arguments.IndexOf("-f");
        if (at >= 0)
        {
            var bare = arguments.Where((_, index) => index != at && index != at + 1).ToList();
            var tokens = arguments[at + 1].Split(',');
            var chunks = SplitFilters(bare, tokens);
            if (chunks.Count > 1)
            {
                context.Log($"{tokens.Length} filter path(s) exceed one command line; splitting the export into {chunks.Count} passes");
                commands = chunks.Select(chunk => (List<string>)[.. bare, "-f", string.Join(',', chunk)]).ToList();
            }
        }
        var code = 0;
        for (var pass = 0; pass < commands.Count; pass++)
        {
            if (commands.Count > 1) context.Log($"Export pass {pass + 1}/{commands.Count}");
            var result = await StreamAsync(commands[pass], context);
            code = code != 0 ? code : result;
        }
        return code;
    }

    /// <summary>Runs the viewer and forwards each output line to the log. A launch failure fails this export only;
    /// cancellation is left to propagate.</summary>
    private static async Task<int> StreamAsync(IReadOnlyList<string> arguments, PortContext context)
    {
        context.Check();
        context.Log("Running: " + PathText.Format(arguments));
        try
        {
            return (await context.Processes.RunAsync(new(arguments[0], arguments.Skip(1).ToArray()), line => context.Log(line.Text), context.Token)).ExitCode;
        }
        catch (Exception exception) when (ExportErrors.IsStart(exception))
        {
            throw new VrfRunnerException(ExportErrors.Start(arguments[0], exception), exception);
        }
    }

    /// <summary>Writes every sidecar while the archive is still at hand, so reorganizing carries them into modelsrc.</summary>
    /// <remarks>Material groups become Source 1 texture groups. The PHYS text holds the real per-bone shapes and joint
    /// limits the merged physics glTF loses. A model whose fidelity data cannot be read keeps its glTF and only loses
    /// the fidelity sidecar, with a warning.</remarks>
    private static async Task WriteModelSidecarsAsync(string executable, string source, string staging, VrfExportOptions options, PortContext context)
    {
        var filter = string.Join(',', ExpandVpkFilters(options.VpkFilepaths));
        var dataDump = await RunBlockDumpAsync(executable, source, filter, "vmdl_c", "DATA", context);
        var groups = ParseMaterialGroups(dataDump ?? "");
        if (groups.Count > 0) WriteSkinSidecars(staging, groups, context);
        var metadataDumps = new Dictionary<string, Dictionary<string, string>>();
        var metadata = await DumpModelMetadataAsync(executable, source, filter, context, dataDump, options.IncludeAnimations, metadataDumps);
        if (metadata.Count > 0) WriteMetadataSidecars(staging, metadata, context);
        var physics = await DumpPhysBlocksAsync(executable, source, filter, context);
        if (physics.Count > 0) WritePhysSidecars(staging, physics, context);
        var dumps = new Dictionary<string, Dictionary<string, string>>
        {
            ["DATA"] = ModelDumpSections(dataDump ?? ""), ["PHYS"] = physics, ["MDAT"] = metadataDumps.GetValueOrDefault("MDAT") ?? []
        };
        foreach (var block in new[] { "CTRL", "MRPH" })
            dumps[block] = ModelDumpSections(await RunBlockDumpAsync(executable, source, filter, "vmdl_c", block, context) ?? "");
        foreach (var resource in dumps["DATA"].Keys)
        {
            var model = Path.Combine(staging, PathText.Native(Path.ChangeExtension(resource, ".glb")));
            if (!File.Exists(model)) model = Path.ChangeExtension(model, ".gltf");
            if (!File.Exists(model)) continue;
            try
            {
                var blocks = dumps.ToDictionary(pair => pair.Key, pair => pair.Value.GetValueOrDefault(resource, ""));
                ModelFidelity.WriteSidecar(model, blocks, resource, new JsonObject { ["included"] = options.IncludeAnimations, ["additive_composed"] = options.ComposeAdditive });
                await ModelFidelity.PreserveLodsAsync(executable, source, resource, model, context, validator: options.NativeLodValidator);
            }
            catch (Exception exception) when (ExportErrors.IsData(exception))
            {
                context.Warn($"{resource}: model fidelity sidecar failed: {exception.Message}");
            }
        }
    }
}
