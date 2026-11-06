using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>Editable prop sources with the material identities the addon must ship.</summary>
public sealed record PropPortResult(string Qc, List<string> Files, JsonObject Report, List<string> Warnings);

/// <summary>Assembles static and animated prop sources for the one-click model workflow.</summary>
public static partial class PropPort
{
    private sealed record Ported(string Qc, List<string> Files, JsonObject Physics, List<string> Warnings, List<string> LodLines, bool Partitioned);

    [GeneratedRegex(@"^\$texturegroup\s+""[^""]*""\s*\{(.*?)^\}", RegexOptions.Multiline | RegexOptions.Singleline)]
    private static partial Regex TextureGroup();

    [GeneratedRegex(@"""([^""]+)""")]
    private static partial Regex QuotedName();

    private static string Sibling(string path, string suffix) => Path.Combine(Path.GetDirectoryName(path) ?? "", Path.GetFileNameWithoutExtension(path) + suffix);

    /// <summary>Reads the PHYS sidecar written beside an export, or nothing when it is absent.</summary>
    private static List<JsonObject> PhysicsDocuments(string glbPath)
    {
        var path = Sibling(glbPath, "_phys.txt");
        return File.Exists(path) ? Kv3.ParseDocuments(PythonText.ReadBomless(path)) : [];
    }

    /// <summary>Loads the authored model graph when the export carries one.</summary>
    private static JsonObject Fidelity(string glbPath) => File.Exists(Path.ChangeExtension(glbPath, ".fidelity.json")) ? ModelFidelity.Load(glbPath) : [];

    /// <summary>Whether the model authors the AO capsules the ragdoll converter builds a humanoid from.</summary>
    private static bool HasAoProxy(JsonObject fidelity)
    {
        var keys = Kv3.ParseDocuments(fidelity.Get("source_data").Get("m_modelInfo").Get("m_keyValueText").Str());
        return keys.Count > 0 && keys[0].Get("ao_proxy_capsule_list").Bool();
    }

    /// <summary>Whether an exported render model carries a skeleton.</summary>
    public static bool IsSkinned(string glbPath) => GltfDocument.Read(glbPath).Root.Get("skins").Bool();

    /// <summary>Returns "ragdoll" for jointed or AO-proxy characters and "prop" otherwise.</summary>
    /// <remarks>A skinned model is a ragdoll when its PHYS block joins bodies together, or when it has no PHYS bodies
    /// at all but authors the AO proxy capsules. Unskinned meshes and skinned models with rigid or bone-following
    /// collision are props.</remarks>
    public static string ClassifyModel(string glbPath)
    {
        if (!IsSkinned(glbPath)) return "prop";
        var documents = PhysicsDocuments(glbPath);
        if (documents.Any(item => item.Get("m_joints").Bool() || item.Get("m_constraints2").Bool())) return "ragdoll";
        if (!documents.Any(item => item.Get("m_parts").Bool()) && HasAoProxy(Fidelity(glbPath))) return "ragdoll";
        return "prop";
    }

    /// <summary>Whether a model may also ship as a separate static prop.</summary>
    /// <remarks>Only a model with no physics, or one rigid body, can stand still without losing behaviour. Multiple
    /// articulated bodies, as on a ragdoll, have no static form.</remarks>
    public static bool StaticEligible(string glbPath)
    {
        var documents = PhysicsDocuments(glbPath);
        if (documents.Any(item => item.Get("m_joints").Bool() || item.Get("m_constraints2").Bool())) return false;
        return documents.Sum(item => item.Get("m_parts").Arr().Count) <= 1;
    }

    /// <summary>Totals the PHYS part masses, or nothing when any part leaves its mass to the engine.</summary>
    private static double? AuthoredMass(string glbPath)
    {
        var masses = PhysicsDocuments(glbPath).SelectMany(item => item.Get("m_parts").Arr()).Select(part => part.Get("m_flMass").Dbl()).ToList();
        return masses.Count == 0 || masses.Any(mass => mass <= 0) ? null : masses.Sum();
    }

    /// <summary>Reads the VMAT resources a set of authored LOD meshes reference.</summary>
    private static List<string> LodMaterialNames(IEnumerable<(double Distance, string Source)> lodSources)
    {
        var resources = new List<string>();
        foreach (var (_, source) in lodSources)
            foreach (var mesh in DmxDocument.Load(source).OfType("DmeMesh"))
                resources.AddRange(mesh.Elements("faceSets").Select(faceSet => faceSet!.Element("material")!.Text("mtlName") ?? ""));
        return resources;
    }

    /// <summary>Maps each compiler material name to the one VMAT resource it was exported from.</summary>
    private static Dictionary<string, string> MaterialResources(JsonObject document, JsonObject fidelity, IEnumerable<(double Distance, string Source)> lodSources)
    {
        var candidates = document.Get("materials").Arr().Select(material => material.Get("extras").Get("vmat").Get("Name").Str()).ToList();
        candidates.AddRange(fidelity.Get("skins").Arr().SelectMany(family => family.Get("m_materials").Arr()).Select(material => material.Str()));
        candidates.AddRange(LodMaterialNames(lodSources));
        var resources = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var candidate in candidates.Where(item => item.Length > 0))
        {
            var token = Resources.Token(candidate);
            var name = PythonText.PosixName(token).ToLowerInvariant();
            if (!resources.TryAdd(name, token) && resources[name] != token)
                throw new InvalidDataException($"Materials {resources[name]} and {token} share the compiler name {name}");
        }
        return resources;
    }

    /// <summary>Collects every material name the compiled model will reference.</summary>
    private static HashSet<string> RenderMaterials(string qc, IEnumerable<string> files)
    {
        var used = new HashSet<string>(StringComparer.Ordinal);
        foreach (var path in files)
        {
            if (!Path.GetExtension(path).Equals(".smd", StringComparison.OrdinalIgnoreCase) || Path.GetFileNameWithoutExtension(path).EndsWith("_physics", StringComparison.Ordinal)) continue;
            var text = PythonText.Read(path);
            var at = text.IndexOf("triangles\n", StringComparison.Ordinal);
            if (at < 0) continue;
            var records = PythonText.SplitLines(text[(at + "triangles\n".Length)..]);
            for (var index = 0; index < records.Count - 1; index += 4) used.Add(records[index].Trim());
        }
        if (TextureGroup().Match(PythonText.Read(qc)) is { Success: true } groups)
            foreach (Match match in QuotedName().Matches(groups.Groups[1].Value)) used.Add(match.Groups[1].Value);
        used.Remove("");
        return used;
    }

    /// <summary>The <c>$cdmaterials</c> folder that holds one material resource beneath the output root.</summary>
    private static string SearchPath(string cdmaterials, string token)
    {
        var folder = cdmaterials.TrimEnd('/') + "/";
        var parent = PythonText.PosixParent(token);
        return parent == "." ? folder : $"{folder}{parent}/";
    }

    /// <summary>Pins the exact model name and material search paths, then appends LOD declarations.</summary>
    private static void RewriteQc(string qc, string modelname, IEnumerable<string> materialPaths, IEnumerable<string> extraLines, bool dropBodies)
    {
        var paths = string.Join(' ', materialPaths.Select(path => $"\"{path}\""));
        var lines = new List<string>();
        foreach (var line in PythonText.SplitLines(PythonText.Read(qc)))
        {
            if (line.StartsWith("$modelname ", StringComparison.Ordinal)) lines.Add($"$modelname \"{modelname}\"");
            else if (line.StartsWith("$cdmaterials ", StringComparison.Ordinal)) lines.Add($"$cdmaterials {paths}");
            else if (!dropBodies || !line.StartsWith("$body ", StringComparison.Ordinal)) lines.Add(line);
        }
        PythonText.Write(qc, string.Join('\n', lines.Concat(extraLines)) + "\n");
    }

    /// <summary>Writes a <c>$staticprop</c> with source collision, authored mass and authored LOD meshes.</summary>
    private static Ported PortStatic(ModelSet modelSet, string outDir, string modelname, string cdmaterials, List<(double Distance, string Source)> lodSources, bool rigid, PortContext? context)
    {
        var mass = AuthoredMass(modelSet.RenderPath);
        var options = new ConversionOptions
        {
            ModelnamePrefix = PythonText.PosixParent(modelname), Cdmaterials = cdmaterials, ExportAnimations = false, Rigid = rigid, PreserveFolders = false, ReplaceExisting = true,
            AutoMass = mass is null, StaticMass = mass is { } authored && authored != 0 ? authored : 10.0
        };
        var converted = GltfSmd.ConvertModelSet(modelSet, options, modelSet.BaseDir, outDir, context);
        if (!converted.Ok) throw new InvalidDataException(converted.Reason.Length > 0 ? converted.Reason : $"Conversion returned {converted.Status}");
        var qc = Path.Combine(outDir, $"{modelSet.Name}.qc");
        var (lodLines, lodFiles, partitioned) = DmxMesh.WriteLodReferences(Path.Combine(outDir, $"{modelSet.Name}.smd"), lodSources, [], context, rigid);
        var physics = new JsonObject
        {
            ["collision"] = converted.HasPhysics, ["surfaceprop"] = converted.Surfaceprop, ["mass"] = converted.Mass,
            ["mass_source"] = mass is not null ? "authored" : "surfaceprop_density"
        };
        return new(qc, [.. converted.Files, .. lodFiles], physics, [.. converted.Warnings], lodLines, partitioned);
    }

    /// <summary>Writes a dynamic prop with its native skeleton, authored clips and bone-following collision.</summary>
    private static Ported PortAnimated(string glbPath, ModelSet modelSet, string outDir, string modelname, string cdmaterials, ModelOverrides overrides, PortContext? context)
    {
        var surfaceprop = SurfacepropDetector.Detect(modelSet.Name, source2Prop: Surfaceprops.Strongest(Surfaceprops.GltfSurfaceprops(modelSet.PhysicsPath)));
        var result = NativePort.Port(glbPath, outDir, modelname, new() { Cdmaterials = cdmaterials, Surfaceprop = surfaceprop, DynamicProp = true, Overrides = overrides }, context);
        var physics = new JsonObject
        {
            ["collision"] = result.Physics.Count > 0 || PythonText.Read(result.Qc).Contains("$collisionmodel", StringComparison.Ordinal),
            ["surfaceprop"] = surfaceprop, ["bones"] = result.Bones, ["sequences"] = result.Sequences, ["attachments"] = result.Attachments, ["hitboxes"] = result.Hitboxes,
            ["kinematic"] = result.Physics.DeepClone()
        };
        return new(result.Qc, [.. result.Files], physics, [.. result.Warnings], [], false);
    }

    /// <summary>Writes a compilable prop keeping the source materials, skins, LODs and collision.</summary>
    /// <remarks>Unskinned models become a <c>$staticprop</c> whose LOD meshes come from the export's
    /// <c>.lods.json</c>. Skinned models keep their native skeleton and every exported clip as a dynamic prop.
    /// Material names stay the VMAT basenames, and <c>$cdmaterials</c> lists <paramref name="cdmaterials"/> joined
    /// with each source folder, so the shipped VMTs mirror the source resource tree.</remarks>
    /// <param name="staticVariant">Writes the separate static form of a skinned model too: its bind pose as a rigid
    /// <c>$staticprop</c>, keeping source collision and collapsing weighted LODs onto the one bone.</param>
    /// <param name="overrides">The project's loop choices.</param>
    public static PropPortResult Port(string glbPath, string outDir, string modelname, string cdmaterials = "models/", PortContext? context = null, bool staticVariant = false,
        ModelOverrides? overrides = null)
    {
        overrides ??= ModelOverrides.None;
        glbPath = Path.GetFullPath(glbPath);
        Directory.CreateDirectory(outDir);
        context?.Check();
        var document = GltfDocument.Read(glbPath).Root;
        var modelSet = new ModelSetScanner(Path.GetDirectoryName(Path.GetFullPath(glbPath)) ?? "", false).FindSets().FirstOrDefault(item => NativePort.PathsEqual(item.RenderPath, glbPath))
            ?? throw new InvalidDataException($"Could not discover the model set for {glbPath}");
        var fidelity = Fidelity(glbPath);
        var skinned = document.Get("skins").Bool();
        var animated = skinned && !staticVariant;
        var manifest = Path.ChangeExtension(glbPath, ".lods.json");
        var lodSources = File.Exists(manifest) ? NativePort.LoadLodSources(manifest) : [];
        var ported = animated
            ? PortAnimated(glbPath, modelSet, outDir, modelname, cdmaterials, overrides, context)
            : PortStatic(modelSet, outDir, modelname, cdmaterials, lodSources, skinned, context);
        context?.Check();
        var resources = MaterialResources(document, fidelity, lodSources);
        var used = RenderMaterials(ported.Qc, ported.Files);
        var unresolved = used.Where(name => !resources.ContainsKey(name.ToLowerInvariant())).Order(StringComparer.Ordinal).ToList();
        if (unresolved.Count > 0) throw new InvalidDataException("Materials without an exported VMAT resource: " + string.Join(", ", unresolved));
        var tokens = used.Select(name => resources[name.ToLowerInvariant()]).Distinct(StringComparer.Ordinal).Order(StringComparer.Ordinal).ToList();
        var targets = new JsonObject();
        foreach (var token in tokens) targets[token] = Resources.AddonMaterial(token);
        if (tokens.Select(Resources.AddonMaterial).Distinct(StringComparer.Ordinal).Count() != tokens.Count)
            throw new InvalidDataException("Material resources collide once their models/ root is dropped: " + string.Join(", ", tokens));
        var materialPaths = tokens.Select(token => SearchPath(cdmaterials, Resources.UnderRoot(cdmaterials, Resources.AddonMaterial(token)))).Distinct(StringComparer.Ordinal).Order(StringComparer.Ordinal).ToList();
        RewriteQc(ported.Qc, modelname, materialPaths, ported.LodLines, ported.Partitioned);
        var warnings = ported.Warnings;
        if (fidelity.Get("groups").Bool()) warnings.Add("Source bodygroups are combined into one body; every mesh group choice renders together.");
        var distinct = warnings.Distinct(StringComparer.Ordinal).ToList();
        var report = new JsonObject
        {
            ["version"] = 1, ["kind"] = "prop", ["variant"] = animated ? "animated" : "static", ["source"] = glbPath, ["modelname"] = modelname,
            ["material_map"] = targets,
            ["material_search_paths"] = new JsonArray(materialPaths.Select(path => (JsonNode?)path).ToArray()),
            ["skins"] = new JsonArray(modelSet.Skins.Select(skin => (JsonNode?)new JsonObject
            {
                ["name"] = skin.Name, ["materials"] = new JsonArray(skin.Materials.Select(material => (JsonNode?)material).ToArray())
            }).ToArray()),
            ["lod_count"] = lodSources.Count, ["physics"] = ported.Physics,
            ["engine_validation"] = "not_run", ["warnings"] = new JsonArray(distinct.Select(warning => (JsonNode?)warning).ToArray())
        };
        if (staticVariant) report["static_variant"] = true;
        var reportPath = Path.Combine(outDir, "prop-report.json");
        Json.Save(reportPath, report);
        var files = ported.Files;
        files.Add(reportPath);
        foreach (var warning in distinct) context?.Warn(warning);
        context?.Log($"Wrote {(animated ? "animated" : "static")} prop with {tokens.Count} material(s) and {lodSources.Count} LOD(s)", LogLevel.Success);
        return new(ported.Qc, files, report, distinct);
    }
}
