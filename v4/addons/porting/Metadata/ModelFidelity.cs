using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>Retains a model's authored appearance graph and the lossless block dumps behind it.</summary>
/// <remarks>The sidecar resolves mesh masks and CTRL's absolute block indices into a portable graph, then keeps every
/// source block beside it so later stages never have to return to the archive.</remarks>
public static partial class ModelFidelity
{
    public const string Schema = "s2kit.model-fidelity.v1";

    [GeneratedRegex(@"-- Block:\s+(\w+)\s+Size:\s+(\d+)\s+bytes\s+\[Offset:\s*(\d+)\]")]
    private static partial Regex BlockLine();

    [GeneratedRegex(@"^\s*at\s+[^\r\n]*(?:\bToDmxAnim\s*\(|\bNmClipExtract\.)", RegexOptions.Multiline)]
    private static partial Regex AnimationStack();

    [GeneratedRegex(@"^\s*at\s+[^\r\n]*(?:\bToDmxMesh\s*\(|\bDatamodelRenderMesh|\bVBIB\.)", RegexOptions.Multiline)]
    private static partial Regex GeometryStack();

    /// <summary>Loads a sidecar, accepting a GLB or the explicit sidecar path.</summary>
    /// <remarks>The result gains <c>_sidecar_directory</c> so relative block and mesh paths can be resolved.</remarks>
    public static JsonObject Load(string path)
    {
        if (Path.GetExtension(path).ToLowerInvariant() is ".glb" or ".gltf") path = Path.ChangeExtension(path, ".fidelity.json");
        var result = Json.Load(path);
        result["_sidecar_directory"] = Path.GetDirectoryName(Path.GetFullPath(path));
        if (result.Get("schema").Str() != Schema) throw new InvalidDataException("Unsupported or missing model fidelity sidecar schema.");
        return result;
    }

    /// <summary>Requires one root document, as the DATA and CTRL block types always have.</summary>
    private static JsonObject OneDocument(string text, string name)
    {
        var documents = Kv3.ParseDocuments(text);
        return documents.Count == 1 ? documents[0] : throw new InvalidDataException($"Expected one {name} document, found {documents.Count}");
    }

    /// <summary>A key the format guarantees. Its absence is reported the way v3's sidecar warning names it.</summary>
    private static JsonNode? Field(JsonNode? item, string key) =>
        item is JsonObject map && map.TryGetPropertyValue(key, out var value) ? value : throw new KeyNotFoundException($"'{key}'");

    /// <summary>A present value kept as authored, including an explicit null; the fallback only covers absence.</summary>
    private static JsonNode? Copy(JsonObject map, string key, JsonNode fallback) => map.TryGetPropertyValue(key, out var value) ? value?.DeepClone() : fallback;

    /// <summary>A whole number with its exact value kept, including unsigned 64-bit masks above the signed range.</summary>
    private static JsonNode Whole(JsonNode? node)
    {
        if (node is not JsonValue value) throw new InvalidDataException($"Expected a whole number, found {node?.ToJsonString() ?? "null"}");
        if (value.TryGetValue<long>(out var signed)) return signed;
        if (value.TryGetValue<ulong>(out var unsigned)) return unsigned;
        return value.GetValueKind() switch
        {
            JsonValueKind.Number => (long)Math.Truncate(value.Dbl()),
            JsonValueKind.True => 1L,
            JsonValueKind.False => 0L,
            JsonValueKind.String => long.Parse(value.GetValue<string>().Trim(), System.Globalization.CultureInfo.InvariantCulture),
            _ => throw new InvalidDataException($"Expected a whole number, found {value.ToJsonString()}")
        };
    }

    private static JsonArray Items(IEnumerable<JsonObject> items) => new(items.Select(item => (JsonNode?)item).ToArray());

    private static JsonArray Numbers(params double[] values) => new(values.Select(value => (JsonNode?)value).ToArray());

    /// <summary>Resolves model masks and absolute CTRL block indices into a portable graph.</summary>
    public static JsonObject Parse(string dataText, string ctrlText, string morphText = "", string physText = "", string resource = "", string mdatText = "")
    {
        var data = OneDocument(dataText, "DATA");
        var ctrl = OneDocument(ctrlText, "CTRL");
        var blockTable = new JsonArray();
        var morphBlocks = new List<long>();
        foreach (Match match in BlockLine().Matches(dataText))
        {
            if (match.Groups[1].Value == "MRPH") morphBlocks.Add(blockTable.Count);
            blockTable.Add(new JsonObject
            {
                ["index"] = blockTable.Count, ["type"] = match.Groups[1].Value,
                ["size"] = long.Parse(match.Groups[2].Value), ["offset"] = long.Parse(match.Groups[3].Value)
            });
        }
        var morphs = Kv3.ParseDocuments(morphText);
        if (morphs.Count > 0 && morphBlocks.Count != morphs.Count) throw new InvalidDataException("MRPH document count does not match the model resource block table");
        var masks = data.Get("m_refMeshGroupMasks").Arr();
        var lodMasks = data.Get("m_refLODGroupMasks").Arr();
        var meshes = new JsonArray();
        foreach (var entry in ctrl.Get("embedded_meshes").Arr())
        {
            var meshIndex = Whole(Field(entry, "mesh_index")).Lng();
            if (meshIndex < 0 || meshIndex >= masks.Count) throw new InvalidDataException($"CTRL mesh {meshIndex} is absent from authored mask tables");
            var morphBlock = entry.Has("morph_block") ? Whole(entry.Get("morph_block")).Lng() : -1;
            var name = Field(entry, "name").Str();
            var morphIndex = morphBlocks.IndexOf(morphBlock);
            if (morphBlock >= 0 && morphIndex < 0) throw new InvalidDataException($"Missing MRPH block {morphBlock} for mesh {name}");
            meshes.Add(new JsonObject
            {
                ["index"] = meshIndex, ["name"] = name, ["data_block"] = Whole(Field(entry, "data_block")), ["morph_block"] = morphBlock,
                ["morph_index"] = morphIndex >= 0 ? morphIndex : null, ["group_mask"] = Whole(masks[(int)meshIndex]),
                ["lod_mask"] = meshIndex < lodMasks.Count ? Whole(lodMasks[(int)meshIndex]) : -1
            });
        }
        var keyvalues = Kv3.ParseDocuments(data.Get("m_modelInfo").Get("m_keyValueText").Str());
        var embedded = keyvalues.Count > 0 ? keyvalues[0] : [];
        var groups = data.Get("m_meshGroups").Arr().Select(group => group.Str()).ToList();
        if (groups.Count != groups.Distinct().Count()) throw new InvalidDataException("Source mesh-group names are not unique");
        var metadata = ModelMetadata.Parse(dataText, mdatText);
        return new JsonObject
        {
            ["schema"] = Schema, ["resource"] = resource.Length > 0 ? resource : data.Get("m_name").Str(), ["meshes"] = meshes,
            ["groups"] = new JsonArray(groups.Select(group => (JsonNode?)group).ToArray()),
            ["default_mask"] = data.Has("m_nDefaultMeshGroupMask") ? Whole(data.Get("m_nDefaultMeshGroupMask")) : -1,
            ["hidden_bodygroups"] = Copy(data, "m_BodyGroupsHiddenInTools", new JsonArray()), ["skins"] = Copy(data, "m_materialGroups", new JsonArray()),
            ["lod_distances"] = data.Get("m_lodGroupSwitchDistances").Bool() ? data.Get("m_lodGroupSwitchDistances")!.DeepClone() : new JsonArray(),
            ["morphs"] = Items(morphs), ["constraints"] = Copy(embedded, "BoneConstraintList", new JsonArray()),
            ["skin_morphs"] = Copy(embedded, "materialgroup_driven_morph_list", new JsonArray()), ["physics"] = Items(Kv3.ParseDocuments(physText)),
            ["source_keyvalues"] = embedded, ["source_data"] = data, ["source_ctrl"] = ctrl, ["block_table"] = blockTable,
            ["source_mdat"] = Items(Kv3.ParseDocuments(mdatText)),
            ["source_metadata"] = new JsonObject
            {
                ["attachments"] = new JsonArray(metadata.Attachments.Select(item => (JsonNode?)new JsonObject
                {
                    ["name"] = item.Name, ["bone"] = item.Bone, ["position"] = Numbers(item.Position.X, item.Position.Y, item.Position.Z),
                    ["rotation"] = Numbers(item.Rotation.X, item.Rotation.Y, item.Rotation.Z, item.Rotation.W), ["ignore_rotation"] = item.IgnoreRotation,
                    ["root_transform"] = item.RootTransform, ["influences"] = item.Influences
                }).ToArray()),
                ["hitboxes"] = new JsonArray(metadata.Hitboxes.Select(item => (JsonNode?)new JsonObject
                {
                    ["set_name"] = item.SetName, ["name"] = item.Name, ["bone"] = item.Bone, ["group"] = item.Group,
                    ["minimum"] = Numbers(item.Minimum.X, item.Minimum.Y, item.Minimum.Z), ["maximum"] = Numbers(item.Maximum.X, item.Maximum.Y, item.Maximum.Z),
                    ["translation_only"] = item.TranslationOnly
                }).ToArray())
            }
        };
    }

    /// <summary>Writes exact dump text alongside typed authoring data for one model.</summary>
    /// <remarks>Refreshing a sidecar from unchanged source keeps its retained LOD sources and warnings, so re-reading the
    /// blocks never forces a second native decompile. Attachments and hitboxes missing from DATA and MDAT fall back to
    /// the <c>.metadata.json</c> beside the model.</remarks>
    /// <param name="animationExport">What the export did with clips. Later stages read <c>additive_composed</c> to decide
    /// whether delta clips are safe. When null it is carried over from the same source, or recorded as unknown.</param>
    public static string WriteSidecar(string glbPath, IReadOnlyDictionary<string, string> blocks, string resource = "", JsonObject? animationExport = null)
    {
        string Block(string name) => blocks.GetValueOrDefault(name, "");

        var fidelity = Parse(Block("DATA"), Block("CTRL"), Block("MRPH"), Block("PHYS"), resource, Block("MDAT"));
        var target = Path.ChangeExtension(glbPath, ".fidelity.json");
        var previous = new JsonObject();
        if (File.Exists(target))
        {
            try
            {
                previous = Load(target);
            }
            catch (Exception exception) when (ExportErrors.IsData(exception) || ExportErrors.IsDisk(exception))
            {
                previous = [];
            }
        }
        var sameSource = previous.Has("resource") && previous.Get("resource").Str() == fidelity.Get("resource").Str();
        if (sameSource && JsonNode.DeepEquals(previous.Get("source_data"), fidelity.Get("source_data")) && JsonNode.DeepEquals(previous.Get("source_ctrl"), fidelity.Get("source_ctrl")))
            foreach (var key in new[] { "dmx_sources", "lod_export_warnings", "warnings" })
                if (previous.Get(key).Bool()) fidelity[key] = previous.Get(key)!.DeepClone();
        var stem = Path.GetFileNameWithoutExtension(glbPath);
        var rawDirectory = Path.Combine(Path.GetDirectoryName(glbPath)!, stem + "_fidelity");
        Directory.CreateDirectory(rawDirectory);
        var raw = new JsonObject();
        foreach (var (block, text) in blocks)
        {
            var name = block.ToLowerInvariant() + ".txt";
            Fmt.WriteText(Path.Combine(rawDirectory, name), text);
            raw[block] = $"{stem}_fidelity/{name}";
        }
        fidelity["raw_blocks"] = raw;
        fidelity["animation_export"] = animationExport?.DeepClone() ?? (sameSource ? previous.Get("animation_export")?.DeepClone() : null)
            ?? new JsonObject { ["included"] = null, ["additive_composed"] = false };
        var metadataSidecar = Path.ChangeExtension(glbPath, ".metadata.json");
        var sourceMetadata = fidelity.Get("source_metadata").Obj();
        if (File.Exists(metadataSidecar) && sourceMetadata.Get("attachments").Arr().Count == 0)
        {
            var metadata = Json.Load(metadataSidecar);
            sourceMetadata["attachments"] = Copy(metadata, "attachments", new JsonArray());
            sourceMetadata["hitboxes"] = Copy(metadata, "hitboxes", new JsonArray());
        }
        Json.Save(target, fidelity);
        return target;
    }

    /// <summary>Refreshes authoring sidecars from an existing GLB without exporting geometry.</summary>
    /// <param name="includeLods">Also retains lower-LOD meshes, as an export would.</param>
    /// <param name="validator">See <see cref="PreserveLodsAsync"/>.</param>
    public static async Task<string> RefreshAsync(string executable, string input, string resource, string glbPath, PortContext context, bool includeLods = false, Action<string>? validator = null)
    {
        var blocks = new Dictionary<string, string>();
        foreach (var block in new[] { "DATA", "CTRL", "MRPH", "PHYS", "MDAT" })
        {
            var text = await VrfExporter.RunBlockDumpAsync(executable, input, resource, "vmdl_c", block, context);
            blocks[block] = VrfExporter.ModelDumpSections(text ?? "", resource).TryGetValue(resource, out var section)
                ? section : throw new InvalidDataException($"No {block} dump for requested model {resource}");
        }
        var target = WriteSidecar(glbPath, blocks, resource);
        if (includeLods) await PreserveLodsAsync(executable, input, resource, glbPath, context, validator: validator);
        context.Log($"Preserved authored model graph: {Path.GetFileName(target)}");
        return target;
    }

    /// <summary>Recognizes explicit animation-only extraction stacks, excluding mesh failures.</summary>
    /// <remarks>Some models write all their geometry and then crash decoding a clip. Those stacks are recoverable; a stack
    /// naming mesh code is not, even when it also names animation code.</remarks>
    private static bool AnimationExportFailure(IEnumerable<string> lines)
    {
        var text = string.Join('\n', lines);
        return AnimationStack().IsMatch(text) && !GeometryStack().IsMatch(text);
    }

    private static string OpenWorkspace(string parent)
    {
        while (true)
        {
            var path = Path.Combine(parent, ".fidelity_lods_" + Path.GetFileNameWithoutExtension(Path.GetRandomFileName()));
            if (Path.Exists(path)) continue;
            Directory.CreateDirectory(path);
            return path;
        }
    }

    /// <summary>Removes a finished workspace, unless a recovered failure asked for it to stay for diagnosis.</summary>
    private static void CloseWorkspace(string workspace, string parent, PortContext context)
    {
        if (File.Exists(Path.Combine(workspace, "failure.txt")))
        {
            context.Log($"Retained native decompile after recoverable export failure: {workspace}");
            return;
        }
        var resolved = Path.GetFullPath(workspace);
        var owned = resolved.StartsWith(Path.TrimEndingDirectorySeparator(Path.GetFullPath(parent)) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase);
        if (!owned || !Path.GetFileName(resolved).StartsWith(".fidelity_lods_", StringComparison.Ordinal)) throw new InvalidDataException("Native decompile cleanup escaped its owned workspace");
        Directory.Delete(resolved, true);
    }

    /// <summary>Retains every authored lower-LOD mesh, including multi-mesh bodygroup levels.</summary>
    /// <remarks>A failed decompile keeps its workspace with <c>failure.txt</c> for diagnosis. An animation-only failure is
    /// recovered: the geometry was written, and the model continues once every selected mesh passes validation.</remarks>
    /// <param name="modeldoc">An existing decompile to read instead of running the viewer again.</param>
    /// <param name="validator">Fully checks one DMX mesh file, throwing when it is unusable. Recovery claims every mesh
    /// passed, so without a validator an animation-only failure is reported as a failure rather than assumed sound.</param>
    public static async Task<string> PreserveLodsAsync(string executable, string input, string resource, string glbPath, PortContext context,
        string? modeldoc = null, Action<string>? validator = null)
    {
        executable = Path.GetFullPath(executable);
        input = Path.GetFullPath(input);
        glbPath = Path.GetFullPath(glbPath);
        var sidecar = Path.ChangeExtension(glbPath, ".fidelity.json");
        var fidelity = Load(sidecar);
        var meshes = Field(fidelity, "meshes").Arr().ToList();

        static long LodMask(JsonNode? mesh) => Field(mesh, "lod_mask").Lng();

        var required = meshes.Where(mesh => (LodMask(mesh) & ~1L) != 0 && (LodMask(mesh) & 1L) == 0).ToList();
        var distances = fidelity.Get("lod_distances").Doubles();
        if (distances.Length < 2 || required.Count == 0) return sidecar;
        var parent = Path.GetDirectoryName(glbPath)!;
        var known = fidelity.Get("dmx_sources").Obj();
        if (required.All(mesh => known.Has(Field(mesh, "index").Str()) && File.Exists(Path.Combine(parent, known.Get(Field(mesh, "index").Str()).Str())))) return sidecar;
        var stem = Path.GetFileNameWithoutExtension(glbPath);
        var destination = Path.Combine(parent, stem + "_fidelity", "meshes");
        Directory.CreateDirectory(destination);
        context.Log($"Preserving {required.Count} authored lower-LOD mesh sources: {resource}");

        async Task<JsonObject> Retain(string workspace, string document, bool export)
        {
            JsonObject? recovered = null;
            if (export)
            {
                List<string> command = [executable, "-i", input, "-f", resource, "-e", "vmdl_c", "-d", "-o", document, "--threads", "1"];
                var diagnostics = new List<string>();

                void Capture(string message)
                {
                    diagnostics.Add(message);
                    context.Log(message);
                }

                Capture("Running: " + PathText.Format(command));
                var code = (await context.Processes.RunAsync(new(executable, command.Skip(1).ToArray(), workspace), line => Capture(line.Text), context.Token)).ExitCode;
                context.Check();
                if (code != 0) Fmt.WriteText(Path.Combine(workspace, "native-export.log"), string.Join(Environment.NewLine, diagnostics) + Environment.NewLine);
                if (!File.Exists(document) || (code != 0 && !AnimationExportFailure(diagnostics))) throw new InvalidDataException($"Native LOD decompile failed for {resource} (exit {code})");
                if (code != 0)
                    recovered = new JsonObject
                    {
                        ["exit_code"] = code, ["source"] = resource, ["reason"] = "Animation extraction failed after native model geometry was written",
                        ["diagnostic_directory"] = Path.GetFullPath(workspace)
                    };
            }
            var documents = Kv3.ParseDocuments(await File.ReadAllTextAsync(document, context.Token));
            if (documents.Count != 1 || !documents[0].Has("rootNode")) throw new InvalidDataException("Native decompile did not produce a ModelDoc root");
            var nodes = NativeLods.ModeldocNodes(documents[0].Get("rootNode").Obj()).ToList();
            var lodGroups = nodes.Where(node => node.Get("_class").Str() == "LODGroup").ToList();
            if (!lodGroups.Select(group => Field(group, "switch_threshold").Dbl(double.NaN)).SequenceEqual(distances))
                throw new InvalidDataException("Cached ModelDoc LOD thresholds do not match compiled source DATA");
            var allLevels = (1L << lodGroups.Count) - 1;
            var shared = meshes.Where(mesh => (LodMask(mesh) & allLevels) == allLevels).Select(mesh => Field(mesh, "name").Str()).ToHashSet();
            for (var level = 0; level < lodGroups.Count; level++)
            {
                var declared = lodGroups[level].Get("mesh_references").Arr().Select(reference => Field(reference, "mesh_name").Str()).ToHashSet();
                var expected = meshes.Where(mesh => (LodMask(mesh) & (1L << level)) != 0).Select(mesh => Field(mesh, "name").Str()).ToHashSet();
                var present = declared.Union(shared).ToHashSet();
                if (!present.SetEquals(expected))
                    throw new InvalidDataException($"Cached ModelDoc mesh membership differs from source DATA at LOD {level}: " +
                        $"missing={PathText.Reprs(expected.Except(present))}, unexpected={PathText.Reprs(present.Except(expected))}");
            }
            var names = new Dictionary<string, JsonObject>();
            foreach (var entry in nodes.Where(node => node.Get("_class").Str() == "RenderMeshFile"))
            {
                var name = entry.Get("name").Str();
                if (!names.TryAdd(name, entry)) throw new InvalidDataException($"Reconstructed ModelDoc has duplicate render mesh name {name}");
            }
            var retained = new JsonObject();
            foreach (var mesh in required)
            {
                var name = Field(mesh, "name").Str();
                if (!names.TryGetValue(name, out var entry)) throw new InvalidDataException($"Authored mesh {name} is missing from the ModelDoc decompile");
                var sourceName = Path.GetFileName(Field(entry, "filename").Str().Replace('\\', '/'));
                var source = Path.Combine(Path.GetDirectoryName(document)!, sourceName);
                if (!PathText.Suffix(source).Equals(".dmx", StringComparison.OrdinalIgnoreCase) || !File.Exists(source)) throw new InvalidDataException($"Native LOD mesh source was not exported: {sourceName}");
                if (recovered is not null)
                {
                    if (validator is null) throw new InvalidDataException($"Native LOD source cannot be checked without a mesh validator: {source}");
                    validator(source);
                }
                var meshIndex = Field(mesh, "index").Lng();
                var fileName = $"mesh{meshIndex:D3}_{sourceName}";
                File.Copy(source, Path.Combine(destination, fileName), true);
                retained[Fmt.Int(meshIndex)] = $"{stem}_fidelity/meshes/{fileName}";
            }
            File.Copy(document, Path.Combine(Path.GetDirectoryName(destination)!, "source.vmdl"), true);
            try
            {
                NativeLods.WriteSidecar(document, glbPath, distances);
            }
            catch (NativeLodException)
            {
            }
            if (recovered is not null)
            {
                var warning = $"Native animation export failed for {resource}; all selected LOD meshes passed complete validation. Diagnostics retained in {workspace}";
                Appended(fidelity, "lod_export_warnings").Add(recovered);
                Appended(fidelity, "warnings").Add(warning);
                Fmt.WriteText(Path.Combine(workspace, "failure.txt"), warning + Environment.NewLine);
                context.Warn(warning);
            }
            return retained;
        }

        JsonObject retained;
        if (modeldoc is not null)
        {
            retained = await Retain(Path.GetDirectoryName(Path.GetFullPath(modeldoc))!, Path.GetFullPath(modeldoc), false);
        }
        else
        {
            var workspace = OpenWorkspace(parent);
            try
            {
                retained = await Retain(workspace, Path.Combine(workspace, "model.vmdl"), true);
            }
            catch (Exception exception)
            {
                Fmt.WriteText(Path.Combine(workspace, "failure.txt"), exception.Message + Environment.NewLine);
                context.Log($"Retained native decompile after failure: {workspace}");
                throw;
            }
            CloseWorkspace(workspace, parent, context);
        }
        fidelity["dmx_sources"] = retained;
        fidelity.Remove("_sidecar_directory");
        Json.Save(sidecar, fidelity);
        return sidecar;
    }

    private static JsonArray Appended(JsonObject map, string key)
    {
        if (map.Get(key) is JsonArray existing) return existing;
        var created = new JsonArray();
        map[key] = created;
        return created;
    }
}
