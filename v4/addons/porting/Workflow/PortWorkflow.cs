using System.Text;

namespace PortingKit.Addons.Porting;

/// <summary>The stage implementations a workflow runs, kept apart so its checkpoints can be proven alone.</summary>
public interface IWorkflowStages
{
    IReadOnlyList<PortAsset> Discover(WorkflowOptions options);
    bool StaticEligible(WorkflowOptions options, PortAsset asset);
    bool PlayerEligible(WorkflowOptions options, PortAsset asset);
    Task<IReadOnlyList<string>> CalibrateAsync(WorkflowOptions options, PortContext context);
    Task<IReadOnlyList<string>> ExtractAsync(WorkflowOptions options, PortAsset asset, PortContext context);
    Task<IReadOnlyList<string>> ConvertAsync(WorkflowOptions options, PortAsset asset, ModelVariant variant, PortContext context);
    Task<IReadOnlyList<string>> MaterialsAsync(WorkflowOptions options, PortAsset asset, ModelVariant variant, PortContext context);
    Task<IReadOnlyList<string>> CompileAsync(WorkflowOptions options, PortAsset asset, ModelVariant variant, PortContext context);
}

/// <summary>Where one asset's extraction, editable sources and isolated addon payload live.</summary>
public static class WorkflowPaths
{
    public static string Project(WorkflowOptions options) => Path.GetFullPath(options.Project);
    public static string Extraction(WorkflowOptions options, PortAsset asset) => Path.Combine(Project(options), "extracted", asset.Key);
    /// <summary>A model's path beneath the addon's models folder, without its extension.</summary>
    public static string Model(WorkflowOptions options, PortAsset asset) => $"{options.Namespace}/{Resources.UnderRoot(options.Namespace, asset.Relative)}";

    public static string Sources(WorkflowOptions options, PortAsset asset) => Path.Combine(Project(options), "modelsrc", asset.Relative.Replace('/', Path.DirectorySeparatorChar));
    public static string Payload(WorkflowOptions options, PortAsset asset) => Path.Combine(Project(options), "staged", asset.Key, "addon");

    /// <summary>Whether a path is the root or lies beneath it, ignoring case as Windows does.</summary>
    public static bool Within(string path, string root)
    {
        var full = Path.GetFullPath(path).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        return full.StartsWith(Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase);
    }
}

/// <summary>Runs independent model stages serially with durable per-stage output hashes.</summary>
public static class PortWorkflow
{
    /// <summary>Rejects ambiguous output names and writes that overlap an input.</summary>
    public static void Validate(WorkflowOptions options, IReadOnlyList<PortAsset> assets)
    {
        var problem = Resources.NamespaceProblem(options.Namespace);
        if (problem.Length > 0) throw new InvalidDataException($"Output namespace: {problem}");
        if (string.IsNullOrWhiteSpace(options.Project)) throw new InvalidDataException("Choose a workflow project folder.");
        if (options.ModelType is not ("auto" or "ragdoll" or "prop")) throw new InvalidDataException("Model type must be automatic, ragdoll or prop.");
        var project = WorkflowPaths.Project(options);
        foreach (var root in assets.Select(asset => Path.GetDirectoryName(asset.Source) ?? asset.Source).Distinct(StringComparer.OrdinalIgnoreCase))
            if (WorkflowPaths.Within(project, root) || WorkflowPaths.Within(root, project))
                throw new InvalidDataException("Choose a project folder outside the input tree.");
        if (options.MaterialSource.Length > 0)
        {
            if (!Directory.Exists(options.MaterialSource)) throw new InvalidDataException("Material source must be an existing folder.");
            if (WorkflowPaths.Within(project, options.MaterialSource) || WorkflowPaths.Within(options.MaterialSource, project))
                throw new InvalidDataException("Choose a project folder outside the material input tree.");
        }
        if (!options.Install) return;
        if (options.GmodAddons.Length == 0 || !Path.GetFileName(Path.GetFullPath(options.GmodAddons).TrimEnd(Path.DirectorySeparatorChar)).Equals("addons", StringComparison.OrdinalIgnoreCase) || !Directory.Exists(options.GmodAddons))
            throw new InvalidDataException("Installation requires the existing GMod addons folder.");
        if (WorkflowPaths.Within(project, options.GmodAddons) || WorkflowPaths.Within(options.GmodAddons, project))
            throw new InvalidDataException("Keep editable project sources outside the GMod addons folder.");
    }

    private static string Identity(WorkflowOptions options)
    {
        static string Location(string value) => value.Length == 0 ? "" : Path.GetFullPath(value).ToLowerInvariant();
        return string.Join('|', options.Sources, Location(options.Project), options.Namespace, options.ModelType, Location(options.MaterialSource),
            Location(options.VrfExecutable), Location(options.Compiler), Location(options.Game), options.IncludeSourceAnimations, options.EmitNormal, options.Resolution,
            options.StaticVariant, options.StaticResolution, options.CreatePlayermodels, options.Install, Location(options.GmodAddons));
    }

    /// <summary>Stamps selected archives and companions by size and time, without walking an entire game.</summary>
    public static string InputSignature(WorkflowOptions options, IReadOnlyList<PortAsset> assets)
    {
        var files = new SortedSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var asset in assets)
        {
            files.Add(asset.Source);
            var directory = Path.GetDirectoryName(asset.Source) ?? "";
            var stem = Path.GetFileNameWithoutExtension(asset.Source);
            if (Path.GetExtension(asset.Source).Equals(".vpk", StringComparison.OrdinalIgnoreCase))
            {
                var prefix = stem.EndsWith("_dir", StringComparison.OrdinalIgnoreCase) ? stem[..^4] : stem;
                files.UnionWith(Directory.EnumerateFiles(directory, prefix + "_*.vpk").Where(path => char.IsAsciiDigit(Path.GetFileName(path)[prefix.Length + 1])));
            }
            else if (!asset.CompiledInput)
            {
                files.UnionWith(Directory.EnumerateFiles(directory, stem + ".*"));
                files.UnionWith(Directory.EnumerateFiles(directory, stem + "_phys.txt"));
                var fidelity = Path.Combine(directory, stem + "_fidelity");
                if (Directory.Exists(fidelity)) files.UnionWith(Directory.EnumerateFiles(fidelity, "*", SearchOption.AllDirectories));
                if (asset.MaterialRoot.Length > 0 && Directory.Exists(asset.MaterialRoot))
                    files.UnionWith(Directory.EnumerateFiles(asset.MaterialRoot, "*", SearchOption.AllDirectories));
            }
        }
        if (options.MaterialSource.Length > 0) files.UnionWith(Directory.EnumerateFiles(options.MaterialSource, "*", SearchOption.AllDirectories));
        foreach (var executable in new[] { options.Compiler, options.VrfExecutable }.Where(File.Exists))
        {
            files.Add(Path.GetFullPath(executable));
            files.UnionWith(Directory.EnumerateFiles(Path.GetDirectoryName(Path.GetFullPath(executable)) ?? "", "*.dll"));
        }
        if (Studiomdl.ResolveGameArgument(options.Game) is { } game && File.Exists(Path.Combine(game, "gameinfo.txt"))) files.Add(Path.Combine(game, "gameinfo.txt"));
        var text = new StringBuilder(Identity(options)).Append('\n');
        foreach (var path in files)
        {
            var info = new FileInfo(path);
            text.Append($"{path}|{info.Length}|{info.LastWriteTimeUtc.Ticks}\n");
        }
        return Digest.Text(text.ToString());
    }

    internal static string StageSignature(string kind) => Digest.Text(kind + ":" + typeof(PortWorkflow).Module.ModuleVersionId.ToString("N"));

    private static string DependencySignature(WorkflowState state, IEnumerable<string> dependencies)
    {
        var text = new StringBuilder();
        foreach (var key in dependencies.Order(StringComparer.Ordinal))
        {
            var record = state.Tasks[key];
            text.Append(key).Append('|').Append(record.Status).Append('|').Append(record.ConverterSignature).Append('\n');
            foreach (var (path, digest) in record.Outputs.OrderBy(pair => pair.Key, StringComparer.Ordinal)) text.Append(path).Append('|').Append(digest).Append('\n');
        }
        return Digest.Text(text.ToString());
    }

    private static List<(string Key, string Kind, PortAsset? Asset, string[] Dependencies)> Queue(WorkflowOptions options, IReadOnlyList<PortAsset> assets)
    {
        var queue = new List<(string, string, PortAsset?, string[])> { ("calibrate", "calibrate", null, []) };
        foreach (var asset in assets)
        {
            var prerequisite = Array.Empty<string>();
            if (asset.CompiledInput)
            {
                queue.Add(($"extract:{asset.Key}", "extract", asset, []));
                prerequisite = [$"extract:{asset.Key}"];
            }
            queue.Add(($"native:{asset.Key}", "native", asset, [.. prerequisite, "calibrate"]));
            queue.Add(($"materials:{asset.Key}", "materials", asset, [$"native:{asset.Key}"]));
            queue.Add(($"compile:{asset.Key}", "compile", asset, [$"native:{asset.Key}", $"materials:{asset.Key}"]));
            if (options.StaticVariant)
            {
                queue.Add(($"static:{asset.Key}", "static", asset, [$"native:{asset.Key}"]));
                queue.Add(($"static_materials:{asset.Key}", "static_materials", asset, [$"static:{asset.Key}"]));
                queue.Add(($"static_compile:{asset.Key}", "static_compile", asset, [$"static:{asset.Key}", $"static_materials:{asset.Key}"]));
            }
            if (!options.CreatePlayermodels || options.ModelType == "prop") continue;
            queue.Add(($"player:{asset.Key}", "player", asset, [$"native:{asset.Key}"]));
            queue.Add(($"compile_pm:{asset.Key}", "compile_pm", asset, [$"player:{asset.Key}", $"materials:{asset.Key}"]));
        }
        queue.Add(("package", "package", null, []));
        if (options.Install) queue.Add(("install", "install", null, ["package"]));
        return queue;
    }

    private static WorkflowState Resume(string checkpoint, WorkflowOptions options, IReadOnlyList<PortAsset> assets, string signature)
    {
        var fresh = new WorkflowState { Options = options, Signature = signature, Assets = assets.ToList() };
        if (!File.Exists(checkpoint)) return fresh;
        var saved = WorkflowState.Load(checkpoint);
        if (saved.Version != WorkflowState.CurrentVersion || Identity(saved.Options) != Identity(options))
            throw new InvalidDataException("This project uses different settings; choose a separate project folder.");
        var same = saved.Signature == signature || (saved.Options != options && saved.Signature == InputSignature(saved.Options, assets));
        if (!same) return fresh;
        saved.Options = options;
        saved.Signature = signature;
        return saved;
    }

    /// <summary>Runs or plans a project, returning its final state with status success, incomplete or interrupted.</summary>
    /// <remarks>Each stage is reused only when its outputs still match their recorded hashes and its code and
    /// prerequisites are unchanged, so a resume after a failure or cancellation repeats only what it must.</remarks>
    public static async Task<WorkflowState> RunAsync(WorkflowOptions options, IWorkflowStages stages, bool planOnly, PortContext context, Action<int, int>? progress = null)
    {
        var assets = stages.Discover(options);
        Validate(options, assets);
        var project = WorkflowPaths.Project(options);
        Directory.CreateDirectory(project);
        FileStream gate;
        try
        {
            gate = new(Path.Combine(project, ".workflow.lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
        }
        catch (IOException exception)
        {
            throw new InvalidDataException("This workflow project is already running in another session.", exception);
        }
        using (gate)
        {
            var checkpoint = Path.Combine(project, "workflow-queue.json");
            var signature = InputSignature(options, assets);
            var state = planOnly ? new WorkflowState { Options = options, Signature = signature, Assets = assets.ToList() } : Resume(checkpoint, options, assets, signature);
            var queue = Queue(options, assets);
            state.Findings = assets.Select(asset => new Finding("info", asset.Relative, asset.CompiledInput
                ? "Source metadata and physics are validated after extraction"
                : "Native sources selected; compiled and engine behavior will be checked separately")).ToList();
            foreach (var (key, kind, asset, dependencies) in queue)
                if (!state.Tasks.ContainsKey(key))
                    state.Tasks[key] = new() { Id = key, Kind = kind, Source = asset?.Source ?? "", Dependencies = [.. dependencies], Status = planOnly ? "planned" : "pending" };
            if (planOnly)
            {
                state.WriteReport(project);
                return state;
            }
            state.Status = "running";
            try
            {
                for (var index = 0; index < queue.Count; index++)
                {
                    context.Check();
                    await RunStage(state, queue[index], options, assets, stages, context, checkpoint, project);
                    progress?.Invoke(index + 1, queue.Count);
                }
                state.Status = state.Tasks.Values.All(record => record.Status is "success" or "skipped") ? "success" : "incomplete";
            }
            catch (OperationCanceledException) when (context.Token.IsCancellationRequested)
            {
                state.Status = "interrupted";
            }
            state.UpdatedAt = DateTimeOffset.UtcNow;
            state.Save(checkpoint);
            state.WriteReport(project);
            return state;
        }
    }

    private static async Task RunStage(WorkflowState state, (string Key, string Kind, PortAsset? Asset, string[] Dependencies) stage,
        WorkflowOptions options, IReadOnlyList<PortAsset> assets, IWorkflowStages stages, PortContext context, string checkpoint, string project)
    {
        var (key, kind, asset, dependencies) = stage;
        var record = state.Tasks[key];
        var code = StageSignature(kind);
        var cacheDependencies = kind == "package" ? state.Tasks.Keys.Where(name => name is not ("package" or "install")).ToArray() : dependencies;
        var dependencySignature = DependencySignature(state, cacheDependencies);
        var reusable = record.Status == "success" && record.Outputs.Count > 0 && record.ConverterSignature == code &&
            (cacheDependencies.Length == 0 || record.DependencySignature == dependencySignature) &&
            record.Outputs.All(pair => File.Exists(pair.Key) && Digest.File(pair.Key) == pair.Value);
        if (kind == "package" && state.Tasks.Any(pair => pair.Key is not ("package" or "install") && pair.Value.Rerun)) reusable = false;
        if (reusable && !dependencies.Any(name => state.Tasks[name].Rerun))
        {
            record.Rerun = false;
            return;
        }
        if (kind.StartsWith("static", StringComparison.Ordinal) && state.Tasks[$"native:{asset!.Key}"].Status == "success" && !stages.StaticEligible(options, asset))
        {
            record.Status = "skipped";
            record.Error = "";
            record.Rerun = false;
            record.Outputs = [];
            record.Messages = [["Info", "Static props are built only from models with no physics or one rigid body"]];
            return;
        }
        if (kind is "player" or "compile_pm" && state.Tasks[$"native:{asset!.Key}"].Status == "success" && !stages.PlayerEligible(options, asset))
        {
            record.Status = "skipped";
            record.Error = "";
            record.Rerun = false;
            record.Outputs = [];
            record.Messages = [["Info", "Playermodels are built only from humanoid characters"]];
            return;
        }
        record.Status = "running";
        record.Messages = [];
        record.Error = "";
        record.Rerun = true;
        if (dependencies.Any(name => state.Tasks[name].Status != "success"))
        {
            record.Status = "blocked";
            record.Error = "A prerequisite failed";
            return;
        }
        state.Save(checkpoint);
        var label = asset?.Relative ?? key;
        var scoped = context.For(label).Tee((message, level) =>
        {
            record.Messages.Add([level.ToString(), message]);
            if (record.Messages.Count > 1000) record.Messages.RemoveAt(0);
        });
        try
        {
            IReadOnlyList<string> outputs = kind switch
            {
                "calibrate" => await stages.CalibrateAsync(options, scoped),
                "extract" => await stages.ExtractAsync(options, asset!, scoped),
                "native" => await stages.ConvertAsync(options, asset!, ModelVariant.Regular, scoped),
                "materials" => await stages.MaterialsAsync(options, asset!, ModelVariant.Regular, scoped),
                "compile" => await stages.CompileAsync(options, asset!, ModelVariant.Regular, scoped),
                "static" => await stages.ConvertAsync(options, asset!, ModelVariant.Static, scoped),
                "static_materials" => await stages.MaterialsAsync(options, asset!, ModelVariant.Static, scoped),
                "static_compile" => await stages.CompileAsync(options, asset!, ModelVariant.Static, scoped),
                "player" => await stages.ConvertAsync(options, asset!, ModelVariant.Player, scoped),
                "compile_pm" => await stages.CompileAsync(options, asset!, ModelVariant.Player, scoped),
                "package" => AddonPackager.Package(options, assets, state, scoped),
                _ => AddonPackager.Install(options, scoped)
            };
            scoped.Check();
            if (outputs.Count == 0 || outputs.Any(path => !File.Exists(path))) throw new InvalidDataException("Stage did not produce its expected files.");
            record.Status = "success";
            record.ConverterSignature = code;
            record.DependencySignature = dependencySignature;
            record.Outputs = outputs.Select(Path.GetFullPath).Distinct(StringComparer.OrdinalIgnoreCase).ToDictionary(path => path, Digest.File);
        }
        catch (OperationCanceledException) when (context.Token.IsCancellationRequested)
        {
            record.Status = "interrupted";
            record.Error = "Cancelled; resume reruns this stage";
            throw;
        }
        catch (Exception exception)
        {
            record.Status = "failed";
            record.Error = exception.Message;
            scoped.Log(exception.Message, LogLevel.Error);
        }
        state.UpdatedAt = DateTimeOffset.UtcNow;
        state.Save(checkpoint);
        state.WriteReport(project);
    }
}
