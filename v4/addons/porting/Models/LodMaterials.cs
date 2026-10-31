using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>One primitive a material plan routes. The plan never touches any triangle or vertex attribute.</summary>
/// <param name="SourceMaterial">The authored material token.</param>
/// <param name="Material">The compiler material the primitive currently carries.</param>
/// <param name="IndexCount">Triangle indices in the primitive.</param>
/// <param name="Data">The caller's own primitive, carried through untouched.</param>
public sealed record LodPrimitive(string SourceMaterial, string Material, int IndexCount, object? Data = null);

/// <summary>One bodygroup choice the plan covers.</summary>
public sealed record LodSpecification(string Name, IReadOnlyList<int> Meshes, int Part, int CompiledChoice, string? Source = null);

/// <summary>The routed streams and the audit of which alias carries which material at each LOD.</summary>
public sealed class LodMaterialPlan
{
    public Dictionary<(string Name, int Level), List<LodPrimitive>> Streams { get; } = [];
    public Dictionary<string, string> Aliases { get; } = new(StringComparer.Ordinal);
    public List<JsonObject> SkinSlots { get; } = [];
    public Dictionary<int, Dictionary<string, string>> Replacements { get; } = [];
    public List<JsonObject> Records { get; } = [];
}

/// <summary>Preserves lower-LOD materials through Source 1's fixed LOD0 mesh slots.</summary>
public static class LodMaterials
{
    private static List<string> Text(JsonNode? array) => array.Arr().Select(item => item.Str()).ToList();

    private static JsonArray Strings(IEnumerable<string> values) => new(values.Select(value => (JsonNode?)value).ToArray());

    /// <summary>Applies the authored default-to-selected material substitution independently per skin.</summary>
    private static List<string> SkinSources(string material, JsonObject fidelity)
    {
        var families = fidelity.Get("skins").Arr();
        if (families.Count == 0) return [material];
        var baseline = Text(families[0].Get("m_materials")).ConvertAll(Resources.Token);
        var result = new List<string>();
        foreach (var family in families)
        {
            var values = Text(family.Get("m_materials")).ConvertAll(Resources.Token);
            if (baseline.Count != values.Count) throw new InvalidDataException("Source material families have incompatible slot counts");
            var candidates = baseline.Zip(values).Where(pair => pair.First == material).Select(pair => pair.Second).Distinct(StringComparer.Ordinal).ToList();
            if (candidates.Count > 1) throw new InvalidDataException($"Authored skin maps material {PythonText.Repr(material)} to conflicting resources");
            result.Add(candidates.Count > 0 ? candidates[0] : material);
        }
        return result;
    }

    /// <summary>Allocates choice-local carrier aliases without changing any triangle or vertex attribute.</summary>
    /// <remarks>A Source 1 LOD can only swap meshes, not add material slots. A lower LOD that introduces a new
    /// material therefore borrows a LOD0 slot it no longer uses, and that slot gets a hashed alias the runtime
    /// replaces per LOD. If there are not enough idle slots the plan refuses rather than discard or guess.</remarks>
    /// <param name="meshes">Authored meshes by index, each with its <c>lod_mask</c>.</param>
    /// <param name="lookup">Primitives by authored mesh index.</param>
    public static LodMaterialPlan PlanLodMaterials(IReadOnlyList<LodSpecification> specifications, IReadOnlyDictionary<int, JsonObject> meshes,
        IReadOnlyDictionary<int, IReadOnlyList<LodPrimitive>> lookup, JsonObject fidelity, Func<string, string> targetToken)
    {
        var levels = Math.Max(1, fidelity.Get("lod_distances").Arr().Count);
        var plan = new LodMaterialPlan();
        for (var level = 1; level < levels; level++) plan.Replacements[level] = new(StringComparer.Ordinal);
        foreach (var specification in specifications)
        {
            var name = specification.Name;
            var selected = new Dictionary<int, List<LodPrimitive>>();
            for (var level = 0; level < levels; level++)
            {
                var mask = 1L << level;
                selected[level] = specification.Meshes.Where(index => (meshes[index].Get("lod_mask").Lng() & mask) != 0)
                    .SelectMany(index => lookup.TryGetValue(index, out var primitives) ? primitives : []).ToList();
            }
            var baseline = selected[0].Select(primitive => primitive.SourceMaterial).ToHashSet(StringComparer.Ordinal);
            var assignments = new Dictionary<int, Dictionary<string, string>>();
            var carriers = new SortedSet<string>(StringComparer.Ordinal);
            for (var level = 1; level < levels; level++)
            {
                var current = selected[level].Select(primitive => primitive.SourceMaterial).ToHashSet(StringComparer.Ordinal);
                var introduced = current.Except(baseline).Order(StringComparer.Ordinal).ToList();
                var available = baseline.Except(current).Order(StringComparer.Ordinal).ToList();
                if (introduced.Count > available.Count)
                    throw new InvalidDataException($"Authored LOD {level} of {PythonText.Repr(name)} requires {introduced.Count} new material slots but only {available.Count} unused LOD0 slots exist; refusing to discard or guess geometry");
                assignments[level] = introduced.Zip(available).ToDictionary(pair => pair.First, pair => pair.Second, StringComparer.Ordinal);
                carriers.UnionWith(assignments[level].Values);
            }
            var carrierAliases = new Dictionary<string, List<string>>(StringComparer.Ordinal);
            foreach (var carrier in carriers)
            {
                var identity = string.Join('\0', fidelity.Get("resource").Str(), name, carrier);
                var alias = "__s2kit_lod_slots/" + Digest.Text(identity)[..24];
                var sources = SkinSources(carrier, fidelity);
                var targets = Enumerable.Range(0, sources.Count).Select(skin => skin == 0 ? alias : alias + $"_skin{skin}").ToList();
                carrierAliases[carrier] = targets;
                foreach (var (target, source) in targets.Zip(sources)) plan.Aliases[target] = source;
                plan.SkinSlots.Add(new JsonObject { ["compiler_material"] = alias, ["skin_materials"] = Strings(targets), ["source_materials"] = Strings(sources) });
            }
            for (var level = 0; level < levels; level++)
            {
                var values = new List<LodPrimitive>();
                var counts = new List<(string Source, string Compiler, List<string> Runtime, List<string> Authored, int Count)>();
                foreach (var primitive in selected[level])
                {
                    var source = primitive.SourceMaterial;
                    var carrier = assignments.TryGetValue(level, out var assigned) && assigned.TryGetValue(source, out var mapped) ? mapped : source;
                    var aliased = carrierAliases.TryGetValue(carrier, out var targets);
                    var compiler = aliased ? targets![0] : primitive.Material;
                    var authored = SkinSources(source, fidelity);
                    var runtime = authored.ConvertAll(value => targetToken(value));
                    if (level == 0 && aliased) runtime = targets!;
                    else if (aliased)
                        foreach (var (alias, target) in targets!.Zip(runtime))
                        {
                            if (!plan.Replacements[level].TryAdd(alias, target) && plan.Replacements[level][alias] != target)
                                throw new InvalidDataException($"Conflicting per-LOD skin material replacement for {alias}");
                        }
                    values.Add(primitive with { Material = compiler });
                    var triangles = primitive.IndexCount / 3;
                    var slot = counts.FindIndex(item => item.Source == source && item.Compiler == compiler && item.Runtime.SequenceEqual(runtime) && item.Authored.SequenceEqual(authored));
                    if (slot >= 0) counts[slot] = counts[slot] with { Count = counts[slot].Count + triangles };
                    else counts.Add((source, compiler, runtime, authored, triangles));
                }
                plan.Streams[(name, level)] = values;
                foreach (var (source, compiler, runtime, authored, count) in counts)
                    plan.Records.Add(new()
                    {
                        ["part"] = specification.Part, ["choice"] = specification.CompiledChoice, ["lod"] = level, ["specification"] = name,
                        ["source_material"] = source, ["compiler_material"] = compiler, ["runtime_material"] = runtime[0],
                        ["runtime_materials"] = Strings(runtime), ["authored_materials"] = Strings(authored), ["triangle_count"] = count
                    });
            }
        }
        return plan;
    }

    /// <summary>Binds QC skins to the actual DMX and SMD texture identities and restores their full paths.</summary>
    /// <remarks>The Source compiler gives DMX materials a relative-path flag. QC's lookup uses the opposite flag and
    /// only matches their basename. Temporarily unique, bare names make that lookup exact without flattening final
    /// resource paths. A full path imported through both DMX and SMD has two texture identities, and both receive
    /// their own temporary reference and the same authored skin rule.</remarks>
    public static (List<string> Lines, List<JsonObject> References) DmxSkinMaterialQc(IReadOnlyList<LodSpecification> specifications, LodMaterialPlan plan, int skinCount)
    {
        var formats = new SortedDictionary<string, SortedSet<string>>(StringComparer.Ordinal);
        foreach (var specification in specifications)
        {
            if (string.IsNullOrEmpty(specification.Source)) continue;
            var kind = Path.GetExtension(PythonText.PosixName(specification.Source)).ToLowerInvariant();
            foreach (var primitive in plan.Streams[(specification.Name, 0)])
            {
                if (!formats.TryGetValue(primitive.Material, out var kinds)) formats[primitive.Material] = kinds = new(StringComparer.Ordinal);
                kinds.Add(kind);
            }
        }
        if (skinCount < 2 || !formats.Values.Any(kinds => kinds.Contains(".dmx"))) return ([], []);
        var families = new Dictionary<string, List<string>>(StringComparer.Ordinal);
        foreach (var record in plan.Records)
        {
            if (record.Get("lod").Int() != 0) continue;
            var material = record.Get("compiler_material").Str();
            var values = Text(record.Get("runtime_materials"));
            if (values.Count != skinCount) throw new InvalidDataException("Compiler material has an incompatible skin family count");
            if (!families.TryAdd(material, values) && !families[material].SequenceEqual(values))
                throw new InvalidDataException($"Compiler material {PythonText.Repr(material)} has conflicting authored skin rules");
        }
        var references = new List<JsonObject>();
        var byMaterial = new Dictionary<string, List<string>>(StringComparer.Ordinal);
        var reserved = formats.Keys.Select(material => PythonText.PosixName(material).ToLowerInvariant()).ToHashSet(StringComparer.Ordinal);
        foreach (var (material, kinds) in formats)
        {
            var identities = material.Contains('/') ? kinds.Count : 1;
            for (var identity = 0; identity < identities; identity++)
            {
                var reference = "__s2_skin_ref_" + Digest.Text(material + "\0" + Fmt.Int(identity))[..24];
                if (!reserved.Add(reference.ToLowerInvariant())) throw new InvalidDataException("Generated compiler skin reference collides with a source material");
                references.Add(new()
                {
                    ["compiler_reference"] = reference, ["restored_material"] = material,
                    ["source_formats"] = Strings(kinds), ["texture_identity"] = identity
                });
                if (!byMaterial.TryGetValue(material, out var list)) byMaterial[material] = list = [];
                list.Add(reference);
            }
        }
        var lines = references.Select(record => $"$renamematerial \"{record.Get("restored_material").Str()}\" \"{record.Get("compiler_reference").Str()}\"").ToList();
        lines.AddRange(["$texturegroup \"skinfamilies\"", "{"]);
        for (var skin = 0; skin < skinCount; skin++)
        {
            var row = new List<string>();
            foreach (var record in references)
            {
                var material = record.Get("restored_material").Str();
                var target = families[material][skin];
                var targetReference = target == material ? record.Get("compiler_reference").Str() : byMaterial.TryGetValue(target, out var known) ? known[0] : target;
                row.Add($"\"{targetReference}\"");
            }
            lines.Add("    { " + string.Join(' ', row) + " }");
        }
        lines.Add("}");
        lines.AddRange(references.Select(record => $"$renamematerial \"{record.Get("compiler_reference").Str()}\" \"{record.Get("restored_material").Str()}\""));
        return (lines, references);
    }
}
