using System.Text.Json;
using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>Ports Source 2 materials, for models and for brush faces, into a game-ready Source 1 material tree.</summary>
/// <remarks>
/// <para>Brush materials follow the recipe the Half-Life: Alyx brush pack was built with: LightmappedGeneric, surface properties translated
/// to stock names, every authored blend layer exported as its own material, projected decals keeping decal intent, and a separate
/// emission texture carried by the unlit additive detail pass so it never takes the opacity alpha. Every resource exports into its own
/// folder, because decoded maps from different materials otherwise collide under shared filenames.</para>
/// <para>A material painted between two layers also gets a <c>_blend</c> material that shows both on a displacement. See <see cref="BlendMaterial"/>.</para>
/// <para>Model materials follow the model workflow: VertexLitGeneric with Phong, one material per resource, and files under
/// <c>materials/models/&lt;namespace&gt;</c>, because that is the folder the compiled model's <c>$cdmaterials</c> points at.</para>
/// </remarks>
public sealed class MaterialPortTool : ITool
{
    private const string Implementation = "material-port-1";
    private const string DumpMarker = "--- Dump written to ";

    public ToolDefinition Definition { get; } = new("Material port", "Materials",
        "Ports Source 2 materials from a VPK, a loose .vmat/.vmat_c or an exported folder into a game-ready materials tree, for brushes and for models.",
        [
            new("sources", "Materials", ParameterKind.Lines, Required: true, Description: "One per line: vpk:<vpk path>:<resource>.vmat_c, a loose .vmat or .vmat_c, or an exported folder."),
            new("output", "Output folder", ParameterKind.Directory, Required: true, Description: "Receives the materials tree."),
            new("namespace", "Namespace", ParameterKind.Text, "ports", Description: "Folder inside materials, for example my_addon/props."),
            new("surface", "Surface", ParameterKind.Choice, "auto", Description: "Auto treats anything under materials/models as a model material and the rest as brush.", Choices: ["auto", "model", "brush"]),
            new("resolution", "Maximum resolution", ParameterKind.Choice, "original", Choices: ["512", "1024", "2048", "4096", "original"]),
            new("tool_size", "Hammer and compile size", ParameterKind.Choice, "off", Description: "Set this to the resolution your maps were built with. Hammer, compiles and hotspots then see that size while the game draws the full texture, so every brush face keeps its scale and shifts.", Choices: ["off", "512", "1024", "2048", "4096"]),
            new("brush_normals", "Brush normal maps", ParameterKind.Choice, "half", Description: "Compression breaks reflections on smooth surfaces into square blocks. Half stays uncompressed at half size, which takes the same space as compressed at full size. Full is four times that. Compressed brings the blocks back.", Choices: ["half", "full", "compressed"]),
            new("emit_normal", "Ship a normal map", ParameterKind.Boolean, "true", Description: "Off drops $bumpmap and $phong, which is what a prop_static needs to keep its baked lighting."),
            new("vrf", "Source2Viewer-CLI", ParameterKind.File, Description: "Leave empty to use the tools.vrf setting, then PATH.")
        ]);

    private sealed record Input(string Source, string? Vpk, string? Resource, string? Path);

    private sealed record Prepared(string Vmat, string Resource, string Root, string TextureRoot, string? Archive, string? RawRoot);

    private sealed class Run(ToolContext tool, PortContext port, string output, string space, string surface, int clamp, int toolSize, NormalStorage normalStorage, bool emitNormal, string? explicitVrf)
    {
        public ToolContext Tool { get; } = tool;
        public PortContext Port { get; } = port;
        public string Output { get; } = output;
        public string Namespace { get; } = space;
        public string Surface { get; } = surface;
        public int Clamp { get; } = clamp;
        public int ToolSize { get; } = toolSize;
        public NormalStorage NormalStorage { get; } = normalStorage;
        public bool EmitNormal { get; } = emitNormal;
        private string? executable;

        public string Vrf => executable ??= Locate(explicitVrf, Tool.Host.Settings.Get("tools.vrf", ""));
    }

    public async Task<ToolResult> RunAsync(ToolContext context, ToolArguments arguments, CancellationToken cancellationToken)
    {
        var space = Resources.NormalizeNamespace(arguments.Text("namespace"));
        var problem = Resources.NamespaceProblem(space);
        if (problem.Length > 0) throw new InvalidDataException(problem);
        var inputs = KeyValues.Lines(arguments.Text("sources")).Select(line => line.Trim()).Where(line => line.Length > 0).Select(Classify).ToArray();
        if (inputs.Length == 0) throw new InvalidDataException("Name at least one material.");
        var output = Path.GetFullPath(arguments.Text("output"));
        var resolution = arguments.Text("resolution");
        var clamp = resolution == "original" ? 0 : int.Parse(resolution, System.Globalization.CultureInfo.InvariantCulture);
        var toolSize = arguments.Text("tool_size") is var size && size == "off" ? 0 : int.Parse(size, System.Globalization.CultureInfo.InvariantCulture);
        var port = new PortContext(context.Host.Processes, (message, level) => context.Log(message, level == LogLevel.Info ? LogLevel.Debug : level), cancellationToken);
        var run = new Run(context, port, output, space, arguments.Text("surface"), clamp, toolSize, Enum.Parse<NormalStorage>(arguments.Text("brush_normals"), ignoreCase: true), arguments.Boolean("emit_normal"), arguments.Text("vrf"));
        var audits = new JsonArray();
        var brushes = new JsonArray();
        var artifacts = new List<Artifact>();
        var failures = 0;
        var files = 0;
        for (var index = 0; index < inputs.Length; index++)
        {
            cancellationToken.ThrowIfCancellationRequested();
            var input = inputs[index];
            context.Report((double)index / inputs.Length, $"Porting {input.Source}");
            try
            {
                foreach (var prepared in await Prepare(input, run))
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    if (IsModel(prepared.Resource, run.Surface)) files += await PortModel(prepared, run, audits);
                    else files += PortBrush(prepared, run, brushes, ref failures);
                }
            }
            catch (Exception exception) when (exception is not OperationCanceledException)
            {
                failures++;
                context.Log($"{input.Source}: {exception.Message}", LogLevel.Error);
                brushes.Add(new JsonObject { ["resource"] = input.Resource ?? input.Source, ["status"] = "failed", ["reason"] = exception.Message, ["layers"] = new JsonArray() });
            }
        }
        var report = Path.Combine(context.OutputDirectory, "material-report.json");
        Json.Save(report, new JsonObject { ["materials"] = audits, ["aliases"] = new JsonArray(), ["brushes"] = brushes });
        artifacts.Add(new(report, "Material report"));
        artifacts.Add(new(Path.Combine(output, "materials"), "Materials folder", "directory"));
        context.Report(1.0, "Done");
        var summary = $"{audits.Count + brushes.Count - failures} of {audits.Count + brushes.Count} materials ported, {files} files written";
        if (failures > 0 && failures == inputs.Length) throw new InvalidDataException("No material could be ported. " + summary);
        return new(summary, artifacts, JsonSerializer.SerializeToElement(new { materials = audits.Count, brushes = brushes.Count, failures, files }));
    }

    private static bool IsModel(string resource, string surface)
    {
        if (surface != "auto") return surface == "model";
        var path = resource.Replace('\\', '/').ToLowerInvariant();
        return path.StartsWith("materials/models/", StringComparison.Ordinal) || path.StartsWith("models/", StringComparison.Ordinal);
    }

    private static Input Classify(string line)
    {
        var text = line.Trim().Trim('"');
        if (!text.StartsWith("vpk:", StringComparison.OrdinalIgnoreCase)) return new(text, null, null, text);
        var body = text[4..];
        var at = body.IndexOf(".vpk:", StringComparison.OrdinalIgnoreCase);
        if (at < 0) throw new InvalidDataException($"Expected vpk:<vpk path>:<resource>.vmat_c, got: {line}");
        return new(text, body[..(at + 4)], body[(at + 5)..].Trim().Replace('\\', '/').TrimStart('/'), null);
    }

    private static string Locate(string? explicitPath, string? setting)
    {
        foreach (var candidate in new[] { explicitPath, setting })
        {
            if (string.IsNullOrEmpty(candidate) || !File.Exists(candidate)) continue;
            if (!Path.GetFileNameWithoutExtension(candidate).Contains("cli", StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException($"'{Path.GetFileName(candidate)}' looks like the GUI build of Source 2 Viewer. This tool needs the command-line build (Source2Viewer-CLI.exe), which is a separate download on the VRF releases page: https://github.com/ValveResourceFormat/ValveResourceFormat/releases");
            return candidate;
        }
        foreach (var folder in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            var path = Path.Combine(folder, "Source2Viewer-CLI.exe");
            if (File.Exists(path)) return path;
        }
        throw new InvalidDataException("Could not locate Source2Viewer-CLI.exe. Pass an explicit path, set 'tools.vrf' in settings, or put it on your PATH. Note: this is the CLI build, a separate download from the GUI Source2Viewer.exe.");
    }

    private static async Task<List<Prepared>> Prepare(Input input, Run run)
    {
        if (input.Vpk is not null) return [await ExportFromArchive(input.Vpk, input.Resource!, run)];
        var path = Path.GetFullPath(input.Path!);
        if (Directory.Exists(path))
        {
            var nested = Path.Combine(path, "materials");
            var root = Directory.Exists(nested) ? nested : path;
            return VmatParser.FindVmats(path).Select(vmat => Loose(vmat, root)).ToList();
        }
        if (!File.Exists(path)) throw new FileNotFoundException($"Material not found: {path}");
        return Path.GetExtension(path).ToLowerInvariant() switch
        {
            ".vmat" => [Loose(path, MaterialsRoot(path) ?? Path.GetDirectoryName(path)!)],
            ".vmat_c" => [await ExportLoose(path, run)],
            _ => throw new InvalidDataException($"Expected a .vmat, a .vmat_c, a folder or a vpk: line, got: {path}")
        };
    }

    private static string? MaterialsRoot(string file)
    {
        for (var parent = Path.GetDirectoryName(file); !string.IsNullOrEmpty(parent); parent = Path.GetDirectoryName(parent))
            if (Path.GetFileName(parent).Equals("materials", StringComparison.OrdinalIgnoreCase)) return parent;
        return null;
    }

    /// <summary>Describes an exported vmat. Authored paths start with <c>materials/</c>, so they resolve from the folder above that one.</summary>
    private static Prepared Loose(string vmat, string root)
    {
        var relative = Path.GetRelativePath(root, vmat).Replace('\\', '/');
        var underMaterials = Path.GetFileName(root).Equals("materials", StringComparison.OrdinalIgnoreCase);
        var textureRoot = underMaterials ? Path.GetDirectoryName(root)! : root;
        return new(vmat, underMaterials ? "materials/" + relative : relative, root, textureRoot, null, null);
    }

    private static async Task Viewer(IReadOnlyList<string> arguments, string workingDirectory, Run run, Action<string>? observe = null)
    {
        Directory.CreateDirectory(workingDirectory);
        var result = await run.Port.Processes.RunAsync(new ProcessRequest(run.Vrf, arguments, workingDirectory, TimeSpan.FromSeconds(240)), line =>
        {
            run.Port.Log(line.Text);
            observe?.Invoke(line.Text);
        }, run.Port.Token);
        if (result.ExitCode != 0) throw new InvalidDataException($"VRF exited {result.ExitCode}: {string.Join(" ", arguments)}");
    }

    /// <summary>Keeps shader-aware exports isolated, so decoded maps from other materials cannot overwrite these.</summary>
    private static async Task<Prepared> ExportFromArchive(string vpk, string resource, Run run)
    {
        if (!File.Exists(vpk)) throw new FileNotFoundException($"VPK not found: {vpk}");
        var key = Digest.Text(resource)[..16];
        var folder = Path.Combine(run.Tool.OutputDirectory, "materialsrc", key);
        var filters = resource.StartsWith("models/", StringComparison.Ordinal) ? $"{resource},materials/{resource}" : resource;
        await Viewer(["-i", vpk, "-o", folder, "-d", "--threads", "1", "-e", "vmat_c,vtex_c", "-f", filters], folder, run);
        var found = Directory.GetFiles(folder, "*.vmat", SearchOption.AllDirectories);
        if (found.Length != 1) throw new InvalidDataException($"VRF selected unexpected materials: [{string.Join(", ", found)}]");
        await ResolveDefaults(vpk, folder, found[0], run);
        var materials = Path.Combine(folder, "materials");
        return new(found[0], resource, Directory.Exists(materials) ? materials : folder, folder, vpk, folder);
    }

    /// <summary>Exports a compiled material from a folder laid out like a game, so the viewer can find its compiled textures.</summary>
    /// <remarks>
    /// The viewer joins the output folder to the input path, and an absolute input wins that join, so a loose file dumps beside itself.
    /// A relative input does not dump at all. The dumped files are therefore gathered from what the viewer reports and moved into
    /// the isolated folder straight away. A file that was already there before the export is copied instead of moved, so a port
    /// never removes anything it did not create.
    /// </remarks>
    private static async Task<Prepared> ExportLoose(string file, Run run)
    {
        var resourceRoot = MaterialsRoot(file);
        var folder = Path.Combine(run.Tool.OutputDirectory, "materialsrc", Digest.Text(file)[..16]);
        Directory.CreateDirectory(folder);
        var game = resourceRoot is null ? Path.GetDirectoryName(file)! : Path.GetDirectoryName(resourceRoot)!;
        var dumped = new List<string>();
        var started = DateTime.UtcNow;
        await Viewer(["-i", file, "-o", folder, "-d", "--threads", "1", "-e", "vmat_c"], folder, run, line =>
        {
            var at = line.IndexOf(DumpMarker, StringComparison.Ordinal);
            if (at >= 0) lock (dumped) dumped.Add(line[(at + DumpMarker.Length)..].Trim().Trim('"'));
        });
        foreach (var path in dumped.Distinct(StringComparer.OrdinalIgnoreCase).Where(File.Exists).ToArray())
        {
            if (path.StartsWith(folder, StringComparison.OrdinalIgnoreCase)) continue;
            var target = Path.Combine(folder, Path.GetRelativePath(game, path));
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            if (File.GetCreationTimeUtc(path) < started) File.Copy(path, target, true);
            else File.Move(path, target, true);
        }
        var found = Directory.GetFiles(folder, "*.vmat", SearchOption.AllDirectories);
        if (found.Length != 1) throw new InvalidDataException($"VRF selected unexpected materials: [{string.Join(", ", found)}]");
        var materials = Path.Combine(folder, "materials");
        return new(found[0], Path.GetRelativePath(game, file).Replace('\\', '/'), Directory.Exists(materials) ? materials : Path.GetDirectoryName(found[0])!, folder, null, folder);
    }

    /// <summary>Recovers only exact compiled builtin identities and their verified neutral channels.</summary>
    /// <remarks>
    /// A material naming a builtin such as <c>materials/default/default_ao.tga</c> is exported without that map. The compiled default is
    /// fetched by its exact identity and written under the authored name, as PNG data despite the name, and only once its pixels prove
    /// they carry the neutral meaning the role expects. Defaults that export as HDR have no PNG here and stay absent, as they did in v3.
    /// </remarks>
    private static async Task ResolveDefaults(string vpk, string folder, string vmat, Run run)
    {
        var (fields, compiled) = SemanticMaterials.Fields(vmat);
        var proofs = new JsonArray();
        foreach (var (key, image) in fields)
        {
            if (!image.StartsWith("materials/default/", StringComparison.Ordinal)) continue;
            var stem = Path.GetFileNameWithoutExtension(image);
            var matches = compiled.Values.Where(value => Path.GetFileNameWithoutExtension(value).StartsWith(stem + "_", StringComparison.Ordinal)).Distinct().ToArray();
            if (matches.Length != 1) continue;
            var source = await ExportDefault(vpk, matches[0], folder, run);
            if (source is null) continue;
            var target = Path.Combine(folder, image);
            if (File.Exists(target)) continue;
            var rgba = SourceImage.Open(source)!.ToRgba();
            var selected = SelectDefault(key, rgba, out var channel);
            if (selected is null) continue;
            Textures.SavePng(selected, target);
            proofs.Add(new JsonObject { ["parameter"] = key, ["resource"] = matches[0], ["source_sha256"] = Digest.File(source), ["target"] = target, ["channel"] = channel, ["sha256"] = Digest.File(target) });
        }
        if (proofs.Count > 0) Fmt.WriteText(Path.Combine(folder, "default-inputs.json"), proofs.ToJsonString(Json.Options) + "\n");
    }

    private static async Task<string?> ExportDefault(string vpk, string resource, string folder, Run run)
    {
        var compiled = resource + "_c";
        var path = Path.Combine(folder, Path.ChangeExtension(resource, ".png"));
        if (File.Exists(path)) return path;
        foreach (var archive in new[] { vpk, Path.Combine(Path.GetDirectoryName(Path.GetDirectoryName(vpk)!)!, "core", "pak01_dir.vpk") })
        {
            if (!File.Exists(archive)) continue;
            await Viewer(["-i", archive, "-o", folder, "-d", "--threads", "1", "-e", "vtex_c", "-f", compiled], folder, run);
            if (File.Exists(path)) return path;
        }
        return null;
    }

    private static ByteImage? SelectDefault(string key, ByteImage rgba, out string channel)
    {
        channel = "";
        var data = rgba.Data;
        bool whiteRed = true, flatNormal = true, blackRgb = true;
        for (var i = 0; i < data.Length; i += 4)
        {
            whiteRed &= data[i] == 255;
            flatNormal &= Math.Abs(data[i] - 128) <= 1 && Math.Abs(data[i + 1] - 128) <= 1 && data[i + 2] == 255;
            blackRgb &= data[i] == 0 && data[i + 1] == 0 && data[i + 2] == 0;
        }
        if (key.StartsWith("TextureAmbientOcclusion", StringComparison.Ordinal) && whiteRed)
        {
            channel = "R: validated neutral AO";
            return rgba.Channel(0);
        }
        if (key.StartsWith("TextureNormal", StringComparison.Ordinal) && flatNormal)
        {
            channel = "RGB: validated flat tangent normal";
            var rgb = new ByteImage(rgba.Height, rgba.Width, 3);
            for (var i = 0; i < rgba.Height * rgba.Width; i++)
                for (var c = 0; c < 3; c++) rgb.Data[i * 3 + c] = data[i * 4 + c];
            return rgb;
        }
        if (key.StartsWith("TextureSelfIllum", StringComparison.Ordinal) && blackRgb)
        {
            channel = "R: validated black emission";
            return rgba.Channel(0);
        }
        if (!key.StartsWith("TextureColor", StringComparison.Ordinal)) return null;
        channel = "RGBA: compiled builtin colour";
        return rgba;
    }

    /// <summary>Converts every authored surface layer and records exact map identities.</summary>
    private static int PortBrush(Prepared prepared, Run run, JsonArray brushes, ref int failures)
    {
        var started = DateTime.UtcNow;
        var entries = VmatParser.ParseLayers(prepared.Vmat, prepared.Root, prepared.TextureRoot);
        var layers = new JsonArray();
        var built = new List<MaterialResult>();
        var written = 0;
        var failed = false;
        foreach (var entry in entries)
        {
            run.Port.Check();
            if (!entry.Buildable)
            {
                failed = true;
                layers.Add(new JsonObject { ["name"] = entry.Name, ["layer"] = entry.Layer, ["status"] = "failed", ["reason"] = "no authored colour map or constant", ["warnings"] = Strings(entry.Warnings) });
                continue;
            }
            var surfaceprop = BrushMaterials.Source1Surfaceprop(entry.Surfaceprop);
            var options = BrushOptions(entry, surfaceprop, run);
            var materialPath = Resources.UnderNamespace(run.Namespace, entry.RelDir);
            var folder = Path.Combine(run.Output, "materials", materialPath);
            var result = MaterialBuilder.Build(entry.ToSource(includeTint: false), folder, entry.Name, materialPath, options, run.Port.For(entry.Name));
            built.Add(result);
            var missing = entry.Warnings.Where(warning => warning.StartsWith("missing:", StringComparison.Ordinal) || warning.StartsWith("ambiguous:", StringComparison.Ordinal)).ToList();
            var adapted = entry.Warnings.Where(warning => warning.StartsWith("adapted:", StringComparison.Ordinal)).ToList();
            if (entry.Shader.Contains("parallax")) adapted.Add("Source 2 parallax is represented by base colour and tangent normals");
            if (entry.Decal) adapted.Add("Source 2 projected decal adapted to a Source 1 decal/overlay");
            if (entry.Shader.Contains("glass")) adapted.Add("Source 2 glass effects adapted to stock brush transparency and cubemap reflection");
            if (entry.Selfillum) adapted.Add("Brush emission uses stock unlit additive detail; dynamic emissive flow is not reproduced");
            if (surfaceprop != entry.Surfaceprop) adapted.Add($"Surface property {entry.Surfaceprop} mapped to {surfaceprop}");
            var maps = new JsonObject();
            foreach (var (role, image) in entry.Resolved)
                maps[role] = new JsonObject { ["path"] = image, ["sha256"] = Digest.File(image), ["resolution"] = entry.Sources.GetValueOrDefault(role) };
            var constants = new JsonObject();
            foreach (var (key, value) in entry.Constants) constants[key] = Numbers(value);
            var dropped = new JsonArray();
            foreach (var (name, reason) in result.Dropped) dropped.Add(new JsonArray(name, reason));
            var filesList = new JsonArray();
            foreach (var path in result.Written)
                filesList.Add(new JsonObject { ["path"] = Path.GetRelativePath(run.Output, path).Replace('\\', '/'), ["sha256"] = Digest.File(path), ["bytes"] = new FileInfo(path).Length });
            written += result.Written.Count;
            layers.Add(new JsonObject
            {
                ["name"] = entry.Name, ["layer"] = entry.Layer, ["status"] = adapted.Count > 0 || missing.Count > 0 || result.Notes.Count > 0 ? "adapted" : "converted", ["shader"] = entry.Shader,
                ["vmat"] = prepared.Vmat, ["vmat_sha256"] = Digest.File(prepared.Vmat), ["material"] = $"{materialPath}/{entry.Name}", ["size"] = new JsonArray(result.Width, result.Height),
                ["source_maps"] = maps, ["constants"] = constants, ["roughness_constant"] = entry.RoughnessConstant, ["metallic_constant"] = entry.MetallicConstant,
                ["color_tint"] = entry.ColorTint is null ? null : Numbers(entry.ColorTint), ["surfaceprop"] = entry.Surfaceprop, ["warnings"] = Strings(entry.Warnings),
                ["adaptations"] = Strings(adapted.Concat(result.Notes)), ["dropped"] = dropped, ["files"] = filesList
            });
        }
        if (failed) failures++;
        var record = new JsonObject { ["resource"] = prepared.Resource, ["status"] = failed ? "failed" : "complete", ["seconds"] = Math.Round((DateTime.UtcNow - started).TotalSeconds, 2), ["layers"] = layers };
        if (!failed && BlendMaterial.Supports(entries) && VmatParser.ParseBlend(prepared.Vmat, prepared.TextureRoot) is { } blend) written += PortBlend(blend, entries[1], built, run, record);
        brushes.Add(record);
        return written;
    }

    private static MaterialOptions BrushOptions(VmatEntry entry, string surfaceprop, Run run) => new()
    {
        ClampSize = run.Clamp, ToolSize = run.ToolSize, NormalStorage = run.NormalStorage, Branch = "gmod", Shader = "LightmappedGeneric", Surfaceprop = surfaceprop, Decal = entry.Decal, EmitNormal = run.EmitNormal && entry.Has("normal"),
        Translucent = entry.Translucent, Alphatest = entry.Alphatest, Tintable = false, NormalGreen = "flip", GlowMode = "selfillum", PhongTintMode = "off"
    };

    /// <summary>Builds the material that blends two already built layers, repeating the second one first when it tiles faster.</summary>
    /// <remarks>The repeat goes in textures of its own, because the plain second layer may already sit on brushes in compiled maps.</remarks>
    private static int PortBlend(VmatBlend blend, VmatEntry second, List<MaterialResult> built, Run run, JsonObject record)
    {
        var (first, plain) = (built[0], built[1]);
        var surfaceprop = BrushMaterials.Source1Surfaceprop(second.Surfaceprop);
        MaterialResult? repeated = null;
        if (blend.Repeat is not (1, 1))
        {
            var name = first.Name + "_blend_b";
            var options = BrushOptions(second, surfaceprop, run);
            options.ToolSize = 0;
            options.GenerateVmt = false;
            options.Repeat = blend.Repeat;
            options.RepeatEdge = BlendMaterial.RepeatEdge(plain, run.Clamp);
            repeated = MaterialBuilder.Build(second.ToSource(includeTint: false), first.OutputDir, name, first.MaterialPath, options, run.Port.For(name));
        }
        var result = BlendMaterial.Build(blend, first, plain, repeated, run.Clamp, surfaceprop, run.Port.For(first.Name + "_blend"));
        var written = result.Written.Concat(repeated?.Written ?? []).ToList();
        var files = new JsonArray();
        foreach (var path in written)
            files.Add(new JsonObject { ["path"] = Path.GetRelativePath(run.Output, path).Replace('\\', '/'), ["sha256"] = Digest.File(path), ["bytes"] = new FileInfo(path).Length });
        record["blend"] = new JsonObject
        {
            ["material"] = $"{result.MaterialPath}/{result.Name}", ["shader"] = result.Vmt!.Shader, ["mask"] = blend.Mask, ["softness"] = BlendMaterial.Softness,
            ["layer_b_repeat"] = new JsonArray(blend.Repeat.Across, blend.Repeat.Down), ["adaptations"] = Strings(result.Notes), ["files"] = files
        };
        return written.Count;
    }

    /// <summary>Converts one model material the way the model workflow does, recording its source provenance.</summary>
    private static async Task<int> PortModel(Prepared prepared, Run run, JsonArray audits)
    {
        VmatEntry entry;
        JsonObject provenance;
        if (prepared.Archive is not null)
        {
            var signature = Digest.Text($"{prepared.Archive}|{new FileInfo(prepared.Archive).Length}|{File.GetLastWriteTimeUtc(prepared.Archive):O}");
            (entry, provenance, _) = await SemanticMaterials.Resolve(prepared.Resource, prepared.Vmat, prepared.RawRoot!, prepared.Archive, run.Tool.OutputDirectory, run.Vrf, signature, Implementation, run.Port);
        }
        else
        {
            entry = VmatParser.Parse(prepared.Vmat, prepared.Root, prepared.TextureRoot);
            provenance = SemanticMaterials.MaterialProvenance(entry);
            provenance["mode"] = "exported_vmat";
        }
        if (!entry.Buildable) throw new InvalidDataException($"No resolved base color for {prepared.Vmat}");
        var target = Resources.UnderRoot($"models/{run.Namespace}", Resources.AddonMaterial(prepared.Resource));
        var cut = target.LastIndexOf('/');
        var parent = cut >= 0 ? target[..cut] : "";
        var name = target[(cut + 1)..];
        var materialPath = parent.Length > 0 ? $"models/{run.Namespace}/{parent}" : $"models/{run.Namespace}";
        var options = new MaterialOptions { ClampSize = run.Clamp, EmitNormal = run.EmitNormal, Translucent = entry.Translucent, Alphatest = entry.Alphatest, Tintable = entry.Tintable };
        var result = MaterialBuilder.Build(entry.ToSource(), Path.Combine(run.Output, "materials", materialPath), name, materialPath, options, run.Port.For(name));
        audits.Add(new JsonObject
        {
            ["source"] = Resources.Token(prepared.Resource), ["target"] = target, ["status"] = "adapted", ["notes"] = Strings(entry.Warnings.Concat(result.Notes)), ["shader"] = entry.Shader,
            ["target_shader"] = options.Shader, ["source_provenance"] = provenance
        });
        return result.Written.Count;
    }

    private static JsonArray Strings(IEnumerable<string> values) => new(values.Select(value => (JsonNode?)JsonValue.Create(value)).ToArray());
    private static JsonArray Numbers(IEnumerable<double> values) => new(values.Select(value => (JsonNode?)JsonValue.Create(value)).ToArray());
}
