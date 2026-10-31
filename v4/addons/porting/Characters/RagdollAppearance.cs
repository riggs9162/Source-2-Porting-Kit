using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>Compiler sources plus a feature-by-feature account of the authored graph.</summary>
public sealed class AppearanceResult
{
    public List<string> QcLines { get; } = [];
    public List<string> Files { get; } = [];
    public JsonObject Report { get; set; } = [];
    public JsonObject AddonDrivers { get; set; } = [];

    /// <summary>Flex DMX files paired with the reference SMD that follows the same skeleton.</summary>
    public List<(string Dmx, string Reference)> FlexSources { get; } = [];

    /// <summary>The authored vertices already measured, per vertex stream.</summary>
    public Dictionary<string, HashSet<int>> WeightSources { get; } = new(StringComparer.Ordinal);

    internal RagdollAppearance.SkinWeightAudit? SkinWeights { get; set; }
}

/// <summary>The optional inputs of an appearance pass. Everything beyond the model name serves a rig that replaces the source skeleton.</summary>
public sealed record AppearanceOptions
{
    public string Modelname { get; init; } = "";

    /// <summary>Source bone name to target skeleton bone name.</summary>
    public IReadOnlyDictionary<string, string>? BoneNameMap { get; init; }

    /// <summary>Writes a compiled submodel in place of <see cref="RagdollAppearance.WriteReference"/>.</summary>
    public Action<string, GltfSkin, IReadOnlyList<AppearancePrimitive>, CoordinateMode>? ReferenceWriter { get; init; }

    /// <summary>A rigid or uniformly scaled transform that replaces the source coordinate conversion of morph deltas.</summary>
    public M4? MorphTransform { get; init; }

    /// <summary>Reworks a primitive list before it is written. Returning null keeps the list as it is.</summary>
    public Func<List<AppearancePrimitive>, List<AppearancePrimitive>?>? PrimitiveTransform { get; init; }

    /// <summary>A rigid alignment between the source model and the target rig.</summary>
    public M4? SourceModelRotation { get; init; }

    /// <summary>Requests that source animations are checked before an aim becomes a native procedure.</summary>
    public bool IncludeSourceAnimations { get; init; }

    /// <summary>The skeleton the compiled model finally uses, when it differs from the appearance skin.</summary>
    public GltfSkin? ProceduralSkin { get; init; }
}

/// <summary>Writes authored bodygroup choices, material families and supported model flexes.</summary>
public static partial class RagdollAppearance
{
    private sealed class SourceSpec
    {
        public required string Name { get; init; }
        public string? Group { get; init; }
        public long? Choice { get; init; }
        public required List<int> Meshes { get; init; }
        public int Part { get; set; }
        public string? Source { get; set; }
        public int CompiledChoice => (int)(Choice ?? 0);

        public LodSpecification ToLod() => new(Name, Meshes, Part, CompiledChoice, Source);

        public JsonObject ToJson() => new()
        {
            ["name"] = Name, ["group"] = Group, ["choice"] = Choice, ["meshes"] = JsonKeys.Integers(Meshes), ["part"] = Part, ["compiled_choice"] = CompiledChoice, ["source"] = Source
        };
    }

    /// <summary>The shared state of one appearance pass.</summary>
    private sealed record Pass(GltfSkin Skin, CoordinateMode Coord, string Output, Action<string, GltfSkin, IReadOnlyList<AppearancePrimitive>, CoordinateMode> Writer, JsonObject Rig, AppearanceResult Result, M4? MorphTransform);

    /// <summary>Writes one compiled submodel with its paired flex-DMX representation when needed.</summary>
    private static string WriteChoiceSource(string stem, List<AppearancePrimitive> selected, Pass pass)
    {
        var result = pass.Result;
        RecordSkinWeights(selected, result);
        var destination = Path.Combine(pass.Output, stem + ".smd");
        pass.Writer(destination, pass.Skin, selected, pass.Coord);
        result.Files.Add(destination);
        if (!selected.Exists(primitive => primitive.Targets.Count > 0)) return Path.GetFileName(destination);
        var dmx = Path.ChangeExtension(destination, ".dmx");
        var compacted = RagdollFlex.WriteFlexDmx(dmx, destination, selected, pass.Coord, pass.Rig, pass.MorphTransform, result.FlexSources.Count == 0);
        Appended(result.Report, "flex_geometry").Add(new JsonObject
        {
            ["source"] = Path.GetFileName(dmx), ["source_corners"] = compacted.SourceCorners, ["vertices"] = compacted.Vertices, ["indexed"] = compacted.Indexed
        });
        result.Files.Add(dmx);
        result.FlexSources.Add((dmx, destination));
        return Path.GetFileName(dmx);
    }

    private static JsonArray Appended(JsonObject map, string key)
    {
        if (map.Get(key) is JsonArray existing) return existing;
        var created = new JsonArray();
        map[key] = created;
        return created;
    }

    /// <summary>Decodes retained authored LOD meshes before assigning stable compiler material slots.</summary>
    private static Dictionary<int, List<AppearancePrimitive>> LoadAuthoredLodMeshes(GltfDocument gltf, GltfSkin skin, JsonObject fidelity, Dictionary<int, List<AppearancePrimitive>> lookup,
        IReadOnlyDictionary<string, string> rename, AppearanceOptions options, AppearanceResult result, PortContext? context, IReadOnlySet<int> selectedIndices)
    {
        var sourceCoord = CoordinateMode.For(gltf);
        var directory = fidelity.Has("_sidecar_directory") ? fidelity.Get("_sidecar_directory").Str() : ".";
        var sources = fidelity.Get("dmx_sources").Obj();
        var loaded = new Dictionary<int, List<AppearancePrimitive>>(lookup);
        foreach (var mesh in fidelity.Need("meshes").Arr())
        {
            context?.Check();
            var index = mesh.Need("index").Int();
            if (loaded.ContainsKey(index) || !selectedIndices.Contains(index) || mesh.Need("lod_mask").Lng() == 0) continue;
            var relative = sources.Get(Fmt.Int(index)).Str();
            if (relative.Length == 0) throw new InvalidDataException($"Authored lower-LOD source is missing for {mesh.Need("name").Str()}; preserve model LOD sources first");
            var path = Path.GetFullPath(Path.Combine(directory, relative));
            var atlasMorphs = MorphAtlas.LoadMeshMorphs(fidelity, mesh);
            var morphReport = new JsonObject();
            var primitives = ReadDmxPrimitives(path, skin, sourceCoord, rename, atlasMorphs, morphReport);
            if (morphReport.Count > 0)
            {
                var entry = new JsonObject { ["mesh"] = index };
                foreach (var (key, value) in morphReport.ToList())
                {
                    morphReport.Remove(key);
                    entry[key] = value;
                }
                Appended(result.Report, "lod_morphs").Add(entry);
            }
            MapPrimitiveMaterials(primitives);
            if (options.PrimitiveTransform is { } transform) primitives = transform(primitives) ?? primitives;
            loaded[index] = primitives;
        }
        return loaded;
    }

    /// <summary>Attaches the emitted compiler file to independently auditable per-material topology records.</summary>
    private static void RecordLodMaterialSources(AppearanceResult result, SourceSpec specification, int level, string? source)
    {
        foreach (var record in result.Report.Need("lod_material_slots").Arr())
            if (record.Need("specification").Str() == specification.Name && record.Need("lod").Int() == level) record!["source"] = source;
    }

    private static List<AppearancePrimitive> Stream(LodMaterialPlan plan, string name, int level) =>
        plan.Streams[(name, level)].ConvertAll(primitive => ((AppearancePrimitive)primitive.Data!).WithMaterial(primitive.Material));

    /// <summary>Composes each bodygroup's authored lower-level meshes without flattening groups.</summary>
    /// <remarks>Source 2's screen coverage threshold becomes a Source 1 switch distance unchanged, as an adjustable starting point.</remarks>
    private static List<string> WriteAuthoredLods(JsonObject fidelity, List<SourceSpec> sourceSpecs, LodMaterialPlan materialPlan, Pass pass, PortContext? context)
    {
        var distances = fidelity.Get("lod_distances").Doubles();
        if (distances.Length < 2) return [];
        if (!distances.All(value => double.IsFinite(value) && value >= 0) || distances.Zip(distances.Skip(1)).Any(pair => pair.Second <= pair.First))
            throw new InvalidDataException("Authored LOD thresholds must be finite and increase");
        var meshByIndex = new Dictionary<int, JsonNode?>();
        foreach (var mesh in fidelity.Need("meshes").Arr()) meshByIndex[mesh.Need("index").Int()] = mesh;
        var result = pass.Result;
        var qc = new List<string>();
        for (var level = 1; level < distances.Length; level++)
        {
            context?.Check();
            var distance = distances[level];
            var replacements = new JsonArray();
            qc.AddRange([$"$lod {G9(distance)}", "{"]);
            foreach (var source in sourceSpecs)
            {
                var selectedIndices = source.Meshes.Where(index => HasBit(meshByIndex[index].Need("lod_mask").Lng(), level)).ToList();
                var baseIndices = source.Meshes.Where(index => HasBit(meshByIndex[index].Need("lod_mask").Lng(), 0)).ToList();
                if (selectedIndices.SequenceEqual(baseIndices))
                {
                    RecordLodMaterialSources(result, source, level, source.Source);
                    continue;
                }
                if (source.Source is null)
                {
                    if (selectedIndices.Count > 0) throw new InvalidDataException("An authored LOD adds geometry to a blank base bodygroup choice");
                    continue;
                }
                if (selectedIndices.Count == 0)
                {
                    qc.Add($"    removemodel {Quoted(source.Source)}");
                    replacements.Add(new JsonObject { ["base"] = source.Source, ["replacement"] = null });
                    continue;
                }
                var stem = "appearance_" + Filename(source.Name) + $"_lod{level}";
                var destination = WriteChoiceSource(stem, Stream(materialPlan, source.Name, level), pass);
                qc.Add($"    replacemodel {Quoted(source.Source)} {Quoted(destination)}");
                RecordLodMaterialSources(result, source, level, destination);
                replacements.Add(new JsonObject { ["base"] = source.Source, ["replacement"] = destination, ["source_meshes"] = JsonKeys.Integers(selectedIndices) });
            }
            foreach (var (first, second) in materialPlan.Replacements[level].OrderBy(pair => pair.Key, StringComparer.Ordinal))
                qc.Add($"    replacematerial {Quoted(first)} {Quoted(second)}");
            qc.Add("}");
            result.Report.Need("lods").Arr().Add(new JsonObject
            {
                ["level"] = level, ["source_threshold"] = distance, ["target_distance"] = distance, ["replacements"] = replacements,
                ["adaptation"] = "Source 2 screen coverage threshold reused as an adjustable Source 1 distance"
            });
        }
        return qc;
    }

    private static string Percent(double value) => Fmt.F6(value * 100) + "%";

    /// <summary>Writes exact source-mask bodygroups and material identities without name inference.</summary>
    /// <remarks>Geometry stays on the complete source skeleton, so physics can remap it later. Morph positions are checked
    /// against the retained atlas before the atlas fills in the normals VRF left out. Procedural constraints resolve twice
    /// when native aims take over, because taking one over frees its source index.</remarks>
    public static AppearanceResult WriteAppearance(GltfDocument gltf, GltfSkin skin, CoordinateMode coord, string outDir, JsonObject fidelity, AppearanceOptions? options = null, PortContext? context = null)
    {
        options ??= new();
        if (fidelity.Get("schema").Str() != ModelFidelity.Schema) throw new InvalidDataException("Unsupported or missing model fidelity sidecar schema");
        Directory.CreateDirectory(outDir);
        var rename = options.BoneNameMap ?? NoRename;
        var primitives = ExtractPrimitives(gltf, skin, rename);
        var baseMorphs = RestoreBaseMorphChannels(fidelity, AuthoredMeshLookup(fidelity, primitives), CoordinateMode.For(gltf));
        if (options.PrimitiveTransform is { } transform) primitives = transform(primitives) ?? primitives;
        MapPrimitiveMaterials(primitives);
        var lookup = AuthoredMeshLookup(fidelity, primitives);
        var (automatic, groups) = AuthoredBodygroups(fidelity);
        var result = new AppearanceResult();
        var report = result.Report;
        report["bodygroups"] = new JsonArray(groups.Select(group => (JsonNode?)new JsonObject
        {
            ["name"] = group.Name, ["default"] = group.Default,
            ["choices"] = new JsonArray(group.Choices.Select(choice => (JsonNode?)new JsonObject { ["value"] = choice.Value, ["bit"] = choice.Bit, ["meshes"] = JsonKeys.Integers(choice.Meshes) }).ToArray())
        }).ToArray());
        report["automatic_meshes"] = JsonKeys.Integers(automatic);
        report["source_meshes"] = lookup.Count;
        report["material_map"] = new JsonObject();
        report["skins"] = new JsonArray();
        report["lods"] = new JsonArray();
        report["unsupported"] = new JsonArray();
        report["flexes"] = new JsonObject();
        report["modelname"] = options.Modelname;
        report["warnings"] = fidelity.Get("warnings")?.DeepClone() ?? new JsonArray();
        report["base_morphs"] = baseMorphs;
        var writer = options.ReferenceWriter ?? WriteReference;
        var (rig, unsupported) = FlexRig(fidelity);
        foreach (var item in unsupported.ToList())
        {
            unsupported.Remove(item);
            report["unsupported"]!.AsArray().Add(item);
        }
        report["flexes"] = new JsonObject
        {
            ["controllers"] = rig.Get("controllers")?.DeepClone(), ["targets"] = JsonKeys.Strings(rig.Get("expressions").Obj().Select(pair => pair.Key)),
            ["expressions"] = rig.Get("expressions")?.DeepClone(), ["rules"] = rig.Get("rules")?.DeepClone(),
            ["installation"] = "Source 1 controller and rule tables restored after compile"
        };
        var sourceSpecs = new List<SourceSpec>();
        if (automatic.Count > 0) sourceSpecs.Add(new() { Name = "automatic", Meshes = automatic });
        foreach (var group in groups)
            foreach (var choice in group.Choices)
                sourceSpecs.Add(new() { Name = $"{group.Name}_{Fmt.Int(choice.Value)}", Group = group.Name, Choice = choice.Value, Meshes = choice.Meshes });
        var partIndices = new Dictionary<string, int>(StringComparer.Ordinal);
        for (var index = 0; index < groups.Count; index++) partIndices[groups[index].Name] = index + (automatic.Count > 0 ? 1 : 0);
        foreach (var source in sourceSpecs) source.Part = source.Group is null ? 0 : partIndices[source.Group];
        var selectedMeshes = sourceSpecs.SelectMany(source => source.Meshes).ToHashSet();
        lookup = LoadAuthoredLodMeshes(gltf, skin, fidelity, lookup, rename, options, result, context, selectedMeshes);
        var meshRecords = new Dictionary<int, JsonObject>();
        foreach (var mesh in fidelity.Need("meshes").Arr()) meshRecords[mesh.Need("index").Int()] = mesh.Obj();
        var planPrimitives = new Dictionary<int, IReadOnlyList<LodPrimitive>>();
        foreach (var (index, list) in lookup)
            planPrimitives[index] = list.ConvertAll(primitive => new LodPrimitive(primitive.SourceMaterial ?? primitive.Material, primitive.Material, primitive.Indices.Length, primitive));
        var materialPlan = LodMaterials.PlanLodMaterials(sourceSpecs.ConvertAll(source => source.ToLod()), meshRecords, planPrimitives, fidelity, TargetMaterialToken);
        report["lod_material_slots"] = RagdollPhysics.Nodes(materialPlan.Records);
        var aliases = new JsonObject();
        foreach (var (target, source) in materialPlan.Aliases) aliases[target] = source;
        report["material_aliases"] = aliases;
        report["material_slot_skins"] = RagdollPhysics.Nodes(materialPlan.SkinSlots);
        var pass = new Pass(skin, coord, outDir, writer, rig, result, options.MorphTransform);
        var writtenNames = new HashSet<string>(StringComparer.Ordinal);
        foreach (var source in sourceSpecs)
        {
            context?.Check();
            var selected = Stream(materialPlan, source.Name, 0);
            if (selected.Count == 0)
            {
                source.Source = null;
                continue;
            }
            var stem = "appearance_" + Filename(source.Name);
            if (!writtenNames.Add(stem.ToLowerInvariant())) throw new InvalidDataException($"Appearance source filenames collide for {source.Name}");
            source.Source = WriteChoiceSource(stem, selected, pass);
            RecordLodMaterialSources(result, source, 0, source.Source);
        }
        foreach (var source in sourceSpecs)
            if (source.Group is null && source.Source is not null) result.QcLines.Add($"$body \"automatic\" {Quoted(source.Source)}");
        foreach (var group in groups)
        {
            result.QcLines.AddRange([$"$bodygroup {Quoted(group.Name)}", "{"]);
            foreach (var choice in group.Choices)
            {
                var source = sourceSpecs.First(item => item.Group == group.Name && item.Choice == choice.Value);
                result.QcLines.Add(source.Source is not null ? $"    studio {Quoted(source.Source)}" : "    blank");
            }
            result.QcLines.Add("}");
        }
        var materials = new SortedSet<string>(primitives.Select(primitive => primitive.SourceMaterial!), StringComparer.Ordinal);
        var skins = fidelity.Get("skins").Arr();
        foreach (var family in skins)
        {
            var tokens = family.Need("m_materials").Arr().Select(material => Resources.Token(material.Str())).ToList();
            materials.UnionWith(tokens);
            report["skins"]!.AsArray().Add(new JsonObject
            {
                ["name"] = family.Need("m_name")?.DeepClone(), ["materials"] = JsonKeys.Strings(tokens), ["target_materials"] = JsonKeys.Strings(tokens.ConvertAll(TargetMaterialToken))
            });
        }
        if (skins.Count > 1)
        {
            if (skins.Select(family => family.Need("m_materials").Arr().Count).Distinct().Count() != 1) throw new InvalidDataException("Source material families have incompatible slot counts");
            var (skinQc, skinReferences) = LodMaterials.DmxSkinMaterialQc(sourceSpecs.ConvertAll(source => source.ToLod()), materialPlan, skins.Count);
            report["material_skin_references"] = RagdollPhysics.Nodes(skinReferences);
            if (skinQc.Count > 0) result.QcLines.AddRange(skinQc);
            else
            {
                result.QcLines.AddRange(["$texturegroup \"skinfamilies\"", "{"]);
                var families = report.Need("skins").Arr();
                for (var skinIndex = 0; skinIndex < families.Count; skinIndex++)
                {
                    var tokens = families[skinIndex].Need("target_materials").Arr().Select(token => token.Str()).ToList();
                    tokens.AddRange(materialPlan.SkinSlots.Select(slot => slot.Need("skin_materials").Arr()[skinIndex].Str()));
                    result.QcLines.Add("    { " + string.Join(' ', tokens.Select(Quoted)) + " }");
                }
                result.QcLines.Add("}");
            }
        }
        var materialMap = new JsonObject();
        foreach (var material in materials) materialMap[material] = TargetMaterialToken(material);
        foreach (var selected in materialPlan.Streams.Values)
            foreach (var primitive in selected) materialMap[primitive.SourceMaterial] = TargetMaterialToken(primitive.SourceMaterial);
        foreach (var source in materialPlan.Aliases.Values) materialMap[source] = TargetMaterialToken(source);
        report["material_map"] = materialMap;
        result.QcLines.AddRange(WriteAuthoredLods(fidelity, sourceSpecs, materialPlan, pass, context));
        var drivers = result.AddonDrivers;
        var defaults = new JsonObject();
        foreach (var group in groups) defaults[group.Name] = group.Default;
        drivers["bodygroup_defaults"] = defaults;
        drivers["skin_names"] = new JsonArray(skins.Select(family => family.Need("m_name")?.DeepClone()).ToArray());
        drivers["skin_morphs"] = fidelity.Get("skin_morphs")?.DeepClone() ?? new JsonArray();
        drivers["source_constraints"] = fidelity.Get("constraints")?.DeepClone() ?? new JsonArray();
        var sourceAttachments = new JsonObject();
        foreach (var attachment in fidelity.Get("source_metadata").Get("attachments").Arr()) sourceAttachments[attachment.Need("name").Str()] = attachment?.DeepClone();
        drivers["source_attachments"] = sourceAttachments;
        drivers["available_flexes"] = new JsonArray(rig.Get("controllers").Arr().Select(controller => controller.Need("m_szName")?.DeepClone()).ToArray());
        drivers["source_model_rotation"] = JsonKeys.Rows(SourceRotationMatrix(options.SourceModelRotation));
        var (twists, corrections) = SourceTwistDrivers(gltf, skin, coord, fidelity, rename, options.SourceModelRotation);
        drivers["twist_constraints"] = twists;
        drivers["source_frames"] = corrections;
        var prepared = RagdollCorrectives.PrepareCorrectiveDrivers(drivers);
        Update(drivers, prepared);
        var nativeProcedurals = ProceduralNative.WriteNativeProcedurals(drivers, options.ProceduralSkin ?? skin, outDir, options.IncludeSourceAnimations ? gltf : null);
        drivers["native_procedurals"] = nativeProcedurals.Records.DeepClone();
        if (nativeProcedurals.Records.Count > 0)
        {
            prepared = RagdollCorrectives.PrepareCorrectiveDrivers(drivers);
            Update(drivers, prepared);
            result.QcLines.AddRange(nativeProcedurals.QcLines);
            result.Files.AddRange(nativeProcedurals.Files);
        }
        var runtimeConstraints = prepared.Need("runtime_constraints").Arr();
        var correctiveControllers = runtimeConstraints.Where(constraint => constraint.Need("kind").Str() == "pose_morph")
            .SelectMany(constraint => constraint.Need("outputs").Arr().Select(name => name.Str())).ToHashSet(StringComparer.Ordinal);
        var correctiveTargets = new JsonObject();
        foreach (var (target, operations) in rig.Get("rules").Obj())
        {
            var list = operations.Arr();
            if (list.Count != 1 || list[0].Need("opcode").Str() != "FETCH1" || !correctiveControllers.Contains(list[0].Need("controller").Str())) continue;
            var controller = list[0].Need("controller").Str();
            if (correctiveTargets.Get(controller) is not JsonArray entries) correctiveTargets[controller] = entries = [];
            entries.Add(target);
        }
        drivers["corrective_flex_targets"] = correctiveTargets;
        var (proceduralQc, proceduralReport) = ProceduralQc(fidelity, rename, skin.JointNames.ToHashSet(StringComparer.Ordinal));
        var translated = new HashSet<int>();
        foreach (var record in twists) translated.Add(record.Need("source_index").Int());
        foreach (var record in runtimeConstraints) translated.Add(record.Need("source_index").Int());
        foreach (var record in prepared.Need("inert_runtime_constraints").Arr()) translated.Add(record.Need("source_index").Int());
        foreach (var record in nativeProcedurals.Records) translated.Add(record.Need("source_index").Int());
        proceduralReport["native_procedurals"] = nativeProcedurals.Records;
        proceduralReport["native_procedural_fallbacks"] = nativeProcedurals.Rejected;
        proceduralReport["runtime_twists"] = twists.DeepClone();
        proceduralReport["runtime_constraint_summary"] = prepared.Need("runtime_constraint_summary")?.DeepClone();
        proceduralReport["morph_output_precedence"] = new JsonObject
        {
            ["policy"] = "Source bone constraints override decoded animation controller values", ["direct_targets"] = correctiveTargets.DeepClone(),
            ["combined_animated_controllers"] = JsonKeys.Strings(correctiveControllers.Where(name => !correctiveTargets.ContainsKey(name)).Order(StringComparer.Ordinal))
        };
        var remaining = new JsonArray();
        foreach (var record in proceduralReport.Need("unsupported_constraints").Arr())
        {
            if (translated.Contains(record.Need("index").Int())) continue;
            var copy = record!.DeepClone().AsObject();
            var precise = prepared.Need("unsupported_runtime_constraints").Arr().FirstOrDefault(item => item.Need("source_index").Int() == record.Need("index").Int());
            if (precise is not null) copy["reason"] = precise.Need("reason")?.DeepClone();
            remaining.Add(copy);
        }
        proceduralReport["unsupported_constraints"] = remaining;
        result.QcLines.AddRange(proceduralQc);
        report["procedural"] = proceduralReport;
        report["sources"] = new JsonArray(sourceSpecs.Select(source => (JsonNode?)source.ToJson()).ToArray());
        var targetsSeen = materialMap.Select(pair => pair.Value.Str()).ToList();
        if (targetsSeen.Distinct(StringComparer.Ordinal).Count() != targetsSeen.Count) throw new InvalidDataException("Compiler-safe material aliases collide with an authored source resource");
        var materialAdaptations = new JsonArray();
        foreach (var (source, target) in materialMap)
            if (target.Str() != Resources.AddonMaterial(source))
                materialAdaptations.Add(new JsonObject { ["source"] = source, ["target"] = target.Str(), ["reason"] = "Source 1 discards geometry with this reserved token; original texture and slot preserved" });
        report["material_adaptations"] = materialAdaptations;
        var lodAdaptations = new JsonArray();
        foreach (var (target, source) in materialPlan.Aliases)
            lodAdaptations.Add(new JsonObject { ["target"] = target, ["source"] = source, ["reason"] = "Choice-local stable LOD0 carrier; identical original VMT bytes and explicit per-LOD/skin replacements" });
        report["lod_material_adaptations"] = lodAdaptations;
        var warnings = report.Need("warnings").Arr();
        if (lodAdaptations.Count > 0)
            warnings.Add("Lower-LOD-only authored materials use stable choice-local compiler slots with identical base VMT aliases and explicit per-LOD/skin material replacements; all source triangles retained.");
        foreach (var adaptation in materialAdaptations)
            warnings.Add($"Authored material {adaptation.Need("source").Str()} uses safe target alias {adaptation.Need("target").Str()} to preserve its geometry and textures.");
        if (result.SkinWeights is { Affected: > 0 } audit)
            warnings.Add($"Source 1 retained the strongest three bone influences on {audit.Affected} vertices; maximum discarded source weight {Percent(audit.MaxDiscarded)}, mean {Percent(audit.MeanDiscarded)}.");
        var reportPath = Path.Combine(outDir, "appearance_report.json");
        PythonText.Write(reportPath, PythonJson.Indented(report) + "\n");
        result.Files.Add(reportPath);
        return result;
    }

    /// <summary>Copies every key of one object over another, the way <c>dict.update</c> does.</summary>
    internal static void Update(JsonObject target, JsonObject values)
    {
        foreach (var (key, value) in values) target[key] = value?.DeepClone();
    }

    /// <summary>Applies remapped reference-SMD skeletons to corresponding flex-bearing DMX files.</summary>
    public static void SyncAppearanceSkeleton(AppearanceResult result)
    {
        foreach (var (dmx, reference) in result.FlexSources) RagdollFlex.SyncFlexSkeleton(dmx, reference);
    }
}
