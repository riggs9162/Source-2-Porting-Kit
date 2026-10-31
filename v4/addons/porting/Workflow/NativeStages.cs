using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>Runs the real porting stages for a workflow.</summary>
/// <remarks>A regular model and its optional static form are separate outputs: their sources, material folders and
/// compiled files never share a path, so a stage failing on one cannot touch or block the other.</remarks>
public sealed class NativeStages(ISettings settings) : IWorkflowStages
{
    private const string MaterialsImplementation = "workflow-materials-1";

    private static string SourcesFor(WorkflowOptions options, PortAsset asset, bool staticVariant)
    {
        var sources = WorkflowPaths.Sources(options, asset);
        return staticVariant ? Path.Combine(sources, "static") : sources;
    }

    private static string MaterialRoot(WorkflowOptions options, bool staticVariant) => $"models/{options.Namespace}{(staticVariant ? "_static" : "")}";

    private static string Modelname(WorkflowOptions options, PortAsset asset, bool staticVariant) => $"{options.Namespace}/{asset.Relative}{(staticVariant ? "_static" : "")}.mdl";

    /// <summary>Resolves pasted links, exact compiled models, or exported model sets.</summary>
    public IReadOnlyList<PortAsset> Discover(WorkflowOptions options)
    {
        var entries = KeyValues.Lines(options.Sources).Select(line => line.Trim()).Where(line => line.Length > 0).ToList();
        var (jobs, problems) = VrfExporter.BuildExportJobs(entries);
        if (problems.Count > 0) throw new InvalidDataException(string.Join("\n", problems));
        var assets = new List<PortAsset>();
        foreach (var job in jobs)
        {
            var source = Path.GetFullPath(job.InputPath);
            if (!Path.Exists(source)) throw new InvalidDataException($"Source does not exist: {source}");
            var extension = Path.GetExtension(source).ToLowerInvariant();
            if (extension == ".vpk")
            {
                if (job.VpkFilepaths.Count == 0) throw new InvalidDataException("Select exact VMDL resources inside the VPK");
                foreach (var name in job.VpkFilepaths)
                {
                    var path = Resources.Relative(name);
                    if (!path.EndsWith(".vmdl_c", StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException($"Model input must name an exact .vmdl_c resource: {name}");
                    assets.Add(PortAsset.Create(source, path, path));
                }
            }
            else if (extension == ".vmdl_c") assets.Add(PortAsset.Create(source, "", Path.GetFileName(source)));
            else assets.AddRange(ExportedAssets(options, source));
        }
        var unique = assets.GroupBy(asset => asset.Key).Select(group => group.Last()).ToList();
        if (unique.Count == 0) throw new InvalidDataException("No selected model sources were found");
        if (unique.Select(asset => asset.Relative.ToLowerInvariant()).Distinct().Count() != unique.Count)
            throw new InvalidDataException("Selected resources collide under the output namespace");
        return unique;
    }

    private static IEnumerable<PortAsset> ExportedAssets(WorkflowOptions options, string source)
    {
        var file = File.Exists(source);
        var root = file ? Path.GetDirectoryName(source)! : source;
        var nested = Path.Combine(root, "modelsrc");
        var modelRoot = Directory.Exists(nested) ? nested : root;
        var materialRoot = options.MaterialSource.Length > 0 ? options.MaterialSource : Path.Combine(root, "materialsrc");
        foreach (var model in new ModelSetScanner(modelRoot).FindSets())
        {
            if (file && !NativePort.PathsEqual(model.RenderPath, source)) continue;
            yield return PortAsset.Create(model.RenderPath, "", Path.GetRelativePath(modelRoot, model.RenderPath).Replace('\\', '/'), Path.GetFullPath(materialRoot));
        }
    }

    /// <summary>Finds the selected render model without accidentally porting its companions.</summary>
    private static string RenderSource(WorkflowOptions options, PortAsset asset)
    {
        if (!asset.CompiledInput) return asset.Source;
        var extraction = WorkflowPaths.Extraction(options, asset);
        if (asset.Resource.Length > 0)
        {
            var expected = Path.Combine(extraction, "modelsrc", Path.ChangeExtension(asset.Resource, ".glb").Replace('/', Path.DirectorySeparatorChar));
            if (File.Exists(expected)) return expected;
        }
        var stem = Path.GetFileNameWithoutExtension(asset.Resource.Length > 0 ? asset.Resource : asset.Source);
        var selected = Directory.Exists(extraction) ? Directory.GetFiles(extraction, "*.glb", SearchOption.AllDirectories).Where(path => Path.GetFileNameWithoutExtension(path) == stem).ToList() : [];
        return selected.Count == 1 ? selected[0] : throw new InvalidDataException($"Expected one exported render model for {asset.Relative}; found {selected.Count}");
    }

    private static string ModelKind(WorkflowOptions options, PortAsset asset) =>
        options.ModelType != "auto" ? options.ModelType : PropPort.ClassifyModel(RenderSource(options, asset));

    private static JsonObject Descriptor(string directory) => Json.Load(Path.Combine(directory, "build.json"));

    /// <summary>Whether this asset may produce the separate static model.</summary>
    public bool StaticEligible(WorkflowOptions options, PortAsset asset) =>
        Descriptor(WorkflowPaths.Sources(options, asset)).Get("kind").Str() == "prop" && PropPort.StaticEligible(RenderSource(options, asset));

    public async Task<IReadOnlyList<string>> CalibrateAsync(WorkflowOptions options, PortContext context)
    {
        var project = WorkflowPaths.Project(options);
        var game = Studiomdl.CompilerGame(project, options.Game);
        await Task.Run(() => PhyAudit.CalibrateCompiler(options.Compiler, game, Path.Combine(project, "calibration"), context), context.Token);
        return [Path.Combine(project, "calibration", "compiler_calibration.json")];
    }

    private async Task Export(WorkflowOptions options, PortAsset asset, string destination, bool animations, PortContext context)
    {
        var export = new VrfExportOptions
        {
            VpkFilepaths = asset.Resource.Length > 0 ? [asset.Resource] : [], Threads = 1, IncludeAnimations = animations, ComposeAdditive = animations,
            NativeLodValidator = RagdollAppearance.ValidateNativeLodMesh
        };
        var code = await VrfExporter.RunExportAsync(VrfExporter.ResolveExecutable(options.VrfExecutable, settings), asset.Source, destination, export, context);
        context.Check();
        if (code != 0) throw new InvalidDataException($"Source 2 Viewer exited with {code}");
    }

    /// <summary>Exports one asset on its own, so an unrelated resource cannot block it.</summary>
    /// <remarks>Props always keep their authored clips. Ragdolls keep them only when requested, so an automatic export
    /// without clips is repeated with them once the model turns out to be an animated prop.</remarks>
    public async Task<IReadOnlyList<string>> ExtractAsync(WorkflowOptions options, PortAsset asset, PortContext context)
    {
        var destination = WorkflowPaths.Extraction(options, asset);
        if (Directory.Exists(destination)) Directory.Move(destination, $"{destination}-previous-{DateTime.UtcNow.Ticks}");
        var animations = options.IncludeSourceAnimations || options.ModelType == "prop";
        await Export(options, asset, destination, animations, context);
        if (!animations && ModelKind(options, asset) == "prop" && PropPort.IsSkinned(RenderSource(options, asset)))
        {
            context.Log("Animated prop detected; exporting again with its authored clips");
            Directory.Delete(destination, true);
            await Export(options, asset, destination, true, context);
        }
        return Directory.GetFiles(destination, "*", SearchOption.AllDirectories).Order(StringComparer.Ordinal).ToList();
    }

    /// <summary>Writes prop or ragdoll sources and keeps their complete conversion audit.</summary>
    public Task<IReadOnlyList<string>> ConvertAsync(WorkflowOptions options, PortAsset asset, bool staticVariant, PortContext context) => Task.Run<IReadOnlyList<string>>(() =>
    {
        var source = RenderSource(options, asset);
        var destination = SourcesFor(options, asset, staticVariant);
        var modelname = Modelname(options, asset, staticVariant);
        var cdmaterials = MaterialRoot(options, staticVariant) + "/";
        var kind = staticVariant ? "prop" : ModelKind(options, asset);
        var calibration = Path.Combine(WorkflowPaths.Project(options), "calibration", "compiler_calibration.json");
        var friction = File.Exists(calibration) ? Json.Load(calibration).Get("friction_compile_scale").Dbl(1.0) : 1.0;
        string qc;
        List<string> files, warnings;
        JsonObject report;
        if (kind == "prop") (qc, files, report, warnings) = PropPort.Port(source, destination, modelname, cdmaterials, context, staticVariant);
        else (qc, files, report, warnings) = RagdollPort.Port(source, destination, modelname, cdmaterials, options.IncludeSourceAnimations, friction, context);
        var descriptor = Path.Combine(destination, "build.json");
        Json.Save(descriptor, new JsonObject
        {
            ["modelname"] = modelname, ["qcs"] = new JsonArray(qc), ["source"] = source, ["kind"] = kind, ["static_variant"] = staticVariant,
            ["report"] = report.DeepClone(), ["warnings"] = new JsonArray(warnings.Select(warning => (JsonNode?)warning).ToArray())
        });
        return [.. files, descriptor];
    }, context.Token);

    private static string MaterialSources(WorkflowOptions options, PortAsset asset)
    {
        if (options.MaterialSource.Length > 0) return options.MaterialSource;
        return asset.CompiledInput ? Path.Combine(WorkflowPaths.Extraction(options, asset), "materialsrc") : asset.MaterialRoot;
    }

    private static List<(string Relative, string Path)> SelectMaterials(string root, JsonObject required)
    {
        var paths = Directory.Exists(root) ? Directory.GetFiles(root, "*.vmat", SearchOption.AllDirectories).Order(StringComparer.Ordinal).ToList() : [];
        var selected = paths.ConvertAll(path => (Path.ChangeExtension(Path.GetRelativePath(root, path), null).Replace('\\', '/'), path));
        if (required.Count > 0)
        {
            selected = [];
            var missing = new List<string>();
            foreach (var key in required.Select(pair => Resources.Token(pair.Key)).Distinct().Order(StringComparer.Ordinal))
            {
                var candidates = new[] { key, key.Replace("/materials/", "/").Replace("/material/", "/") }
                    .Select(name => Path.Combine(root, name + ".vmat")).Distinct(StringComparer.OrdinalIgnoreCase).Where(File.Exists).ToList();
                if (candidates.Count > 1) throw new InvalidDataException($"Ambiguous extracted material resource: {key}");
                if (candidates.Count == 1) selected.Add((key, candidates[0]));
                else missing.Add(key);
            }
            if (missing.Count > 0) throw new InvalidDataException("Missing referenced VMATs: " + string.Join(", ", missing));
        }
        return selected.Count > 0 ? selected : throw new InvalidDataException($"No referenced VMAT materials were found under {root}");
    }

    /// <summary>Converts full resource-relative material paths into an isolated addon payload.</summary>
    /// <remarks>The static form obeys Source 1's static-prop lighting rules: normal detail is baked into the base
    /// texture and no $bumpmap, $normalmap or $phong reaches any skin, because any one of them discards the
    /// vertex lighting VRAD baked. Cubemap reflections, masks, metal darkening, opacity, tint and emission all
    /// stay. Its folder is separate, so the two lighting setups can never overwrite each other.</remarks>
    public async Task<IReadOnlyList<string>> MaterialsAsync(WorkflowOptions options, PortAsset asset, bool staticVariant, PortContext context)
    {
        var root = MaterialSources(options, asset);
        var destination = SourcesFor(options, asset, staticVariant);
        var payload = WorkflowPaths.Payload(options, asset);
        var report = Descriptor(destination).Get("report");
        var required = report.Get("material_map").Obj();
        var aliases = report.Get("material_aliases").Obj();
        foreach (var (alias, source) in aliases)
        {
            Resources.Relative(alias);
            if (!required.ContainsKey(Resources.Token(source.Str()))) throw new InvalidDataException($"Unmapped material source for compiler alias {alias}: {source.Str()}");
        }
        var selected = SelectMaterials(root, required);
        var folder = MaterialRoot(options, staticVariant);
        var written = new List<string>();
        var audits = new JsonArray();
        var compiled = asset.CompiledInput && options.MaterialSource.Length == 0;
        var signature = compiled ? PortWorkflow.InputSignature(options, [asset]) : "";
        foreach (var (relative, path) in selected)
        {
            context.Check();
            VmatEntry entry;
            JsonObject provenance;
            if (compiled)
            {
                List<string> sources;
                (entry, provenance, sources) = await SemanticMaterials.Resolve(relative + ".vmat", path, root, asset.Source, WorkflowPaths.Project(options),
                    VrfExporter.ResolveExecutable(options.VrfExecutable, settings), signature, MaterialsImplementation, context);
                written.AddRange(sources);
            }
            else
            {
                entry = VmatParser.Parse(path, root, root);
                provenance = SemanticMaterials.MaterialProvenance(entry);
                provenance["mode"] = options.MaterialSource.Length > 0 ? "explicit_material_source" : "exported_vmat";
            }
            if (!entry.Buildable) throw new InvalidDataException($"No resolved base color for {path}");
            var target = Resources.Relative(required.Get(relative).Str(relative));
            var cut = target.LastIndexOf('/');
            var materialPath = cut >= 0 ? $"{folder}/{target[..cut]}" : folder;
            var material = new MaterialOptions
            {
                ClampSize = staticVariant ? options.StaticResolution : options.Resolution, EmitNormal = !staticVariant && options.EmitNormal && entry.Has("normal"), BakeNormalDetail = staticVariant,
                Translucent = entry.Translucent, Alphatest = entry.Alphatest, Tintable = entry.Tintable
            };
            var result = MaterialBuilder.Build(entry.ToSource(), Path.Combine(payload, "materials", materialPath.Replace('/', Path.DirectorySeparatorChar)), target[(cut + 1)..], materialPath, material, context);
            written.AddRange(result.Written);
            audits.Add(new JsonObject
            {
                ["source"] = relative, ["target"] = target, ["status"] = "adapted",
                ["notes"] = new JsonArray(entry.Warnings.Concat(result.Notes).Select(note => (JsonNode?)note).ToArray()),
                ["shader"] = entry.Shader, ["target_shader"] = material.Shader, ["source_provenance"] = provenance
            });
        }
        var aliasAudits = WriteAliases(payload, folder, required, aliases, audits, written, context);
        var reportPath = Path.Combine(destination, "material-report.json");
        Json.Save(reportPath, new JsonObject { ["materials"] = audits, ["aliases"] = aliasAudits });
        written.Add(reportPath);
        return written;
    }

    private static JsonArray WriteAliases(string payload, string folder, JsonObject required, JsonObject aliases, JsonArray audits, List<string> written, PortContext context)
    {
        string Vmt(string token) => Path.Combine(payload, "materials", (folder + "/" + token + ".vmt").Replace('/', Path.DirectorySeparatorChar));
        var originals = audits.Select(entry => Vmt(entry.Get("target").Str())).ToHashSet(StringComparer.OrdinalIgnoreCase);
        var result = new JsonArray();
        var seen = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var (alias, value) in aliases.OrderBy(pair => pair.Key, StringComparer.Ordinal))
        {
            context.Check();
            var token = Resources.Token(alias);
            var source = Resources.Token(value.Str());
            if (seen.TryGetValue(token, out var known) && known != source) throw new InvalidDataException($"Conflicting compiler material alias: {token}");
            seen[token] = source;
            var sourceTarget = Resources.Relative(required.Get(source).Str());
            var content = File.ReadAllBytes(Vmt(sourceTarget));
            var target = Vmt(token);
            if (originals.Contains(target) && !File.ReadAllBytes(target).AsSpan().SequenceEqual(content))
                throw new InvalidDataException($"Compiler material alias replaces another source material: {token}");
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            File.WriteAllBytes(target, content);
            written.Add(target);
            result.Add(new JsonObject
            {
                ["target"] = token, ["source"] = source, ["source_target"] = sourceTarget, ["status"] = "generated", ["vmt_bytes_preserved"] = true, ["additional_textures"] = 0
            });
        }
        return result;
    }

    /// <summary>Compiles, checks and stages exactly this model's runtime files.</summary>
    public async Task<IReadOnlyList<string>> CompileAsync(WorkflowOptions options, PortAsset asset, bool staticVariant, PortContext context)
    {
        var destination = SourcesFor(options, asset, staticVariant);
        var payload = WorkflowPaths.Payload(options, asset);
        var descriptor = Descriptor(destination);
        var game = Studiomdl.CompilerGame(WorkflowPaths.Project(options), options.Game, payload);
        var written = new List<string>();
        var physicsAudits = new JsonArray();
        var modelAudits = new JsonArray();
        foreach (var name in descriptor.Get("qcs").Arr())
        {
            var qc = name.Str();
            if (!await Studiomdl.CompileQcAsync(qc, options.Compiler, game, context))
            {
                context.Check();
                throw new InvalidDataException($"Compiler failed: {qc}");
            }
            var mdl = Studiomdl.ModelPathFor(qc, game) ?? throw new InvalidDataException($"The QC names no model: {qc}");
            var geometry = Studiomdl.HasGeometry(qc);
            var check = Studiomdl.CheckCompiledModel(mdl, geometry);
            if (!check.Ok) throw new InvalidDataException(string.Join("; ", check.Problems()));
            var outputs = new List<string> { mdl };
            outputs.AddRange(check.Siblings.Keys);
            if (geometry) await CompiledStage.FinalizeAsync(options, descriptor, destination, qc, mdl, payload, physicsAudits, modelAudits, context);
            var ani = Path.ChangeExtension(mdl, ".ani");
            if (File.Exists(ani)) outputs.Add(ani);
            foreach (var source in outputs)
            {
                var target = Path.Combine(payload, Path.GetRelativePath(game, source));
                Directory.CreateDirectory(Path.GetDirectoryName(target)!);
                File.Copy(source, target, true);
                written.Add(target);
            }
        }
        written.AddRange(CompiledStage.CopyDrivers(descriptor, payload));
        var reportPath = Path.Combine(destination, "compiled-report.json");
        Json.Save(reportPath, new JsonObject
        {
            ["checksum_validation"] = "passed", ["modelname"] = descriptor.Get("modelname").Str(), ["engine_validation"] = "not_run",
            ["files"] = new JsonArray(written.Select(path => (JsonNode?)path).ToArray()), ["physics_audits"] = physicsAudits, ["model_audits"] = modelAudits
        });
        written.Add(reportPath);
        return written;
    }
}
