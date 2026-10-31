using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>Authored LOD groups the native model porter cannot preserve.</summary>
public sealed class NativeLodException(string message) : Exception(message);

/// <summary>One authored lower-resolution mesh and its switch distance.</summary>
public sealed record NativeLod(double Distance, string Source);

/// <summary>Preserves authored LOD meshes alongside a glTF export.</summary>
/// <remarks>The glTF exporter emits the primary mesh only, so a separate ModelDoc decompile supplies lower-resolution
/// DMX meshes and their switch distances. Only single-mesh LOD groups can feed the native model porter; multi-mesh
/// groups would need native mesh composition.</remarks>
public static class NativeLods
{
    /// <summary>Rejects absolute, parent-relative or drive-qualified paths, returning the normalized form.</summary>
    private static string SafeResourcePath(string value)
    {
        var text = value.Replace('\\', '/');
        var parts = text.Split('/').Where(part => part.Length > 0 && part != ".").ToArray();
        if (text.StartsWith('/') || parts.Contains("..") || text.Contains(':') || parts.Length == 0)
            throw new NativeLodException($"Unsafe native LOD resource path: {PathText.Repr(value)}");
        return string.Join('/', parts);
    }

    /// <summary>Visits ModelDoc child nodes in authoring order.</summary>
    internal static IEnumerable<JsonObject> ModeldocNodes(JsonObject node)
    {
        yield return node;
        foreach (var child in node.Get("children").Arr().OfType<JsonObject>())
            foreach (var nested in ModeldocNodes(child)) yield return nested;
    }

    /// <summary>Reads ordered single-mesh LOD groups without guessing names, omitting the primary group.</summary>
    /// <remarks>The first group is the primary glTF mesh. Distances must be finite and strictly increasing, and every
    /// referenced mesh must have a DMX source.</remarks>
    public static List<NativeLod> ParseModeldoc(string text)
    {
        var documents = Kv3.ParseDocuments(text);
        if (documents.Count != 1 || documents[0].Get("rootNode") is not JsonObject root) throw new NativeLodException("Native decompile did not produce one ModelDoc root");
        var nodes = ModeldocNodes(root).ToList();
        var meshes = new Dictionary<string, string>();
        foreach (var node in nodes.Where(node => node.Get("_class").Str() == "RenderMeshFile"))
        {
            if (node.Get("name") is not JsonValue name || name.GetValueKind() != System.Text.Json.JsonValueKind.String || meshes.ContainsKey(name.Str()))
                throw new NativeLodException("Render mesh names are missing or duplicated");
            meshes[name.Str()] = node.Get("filename").Str();
        }
        var groups = nodes.Where(node => node.Get("_class").Str() == "LODGroup").ToList();
        if (groups.Count == 0) return [];
        var lods = new List<NativeLod>();
        var previous = -1.0;
        foreach (var group in groups)
        {
            var distance = group.Get("switch_threshold").Dbl(double.NaN);
            if (!double.IsFinite(distance) || distance < 0 || distance <= previous) throw new NativeLodException("LOD switch distances must be finite and increasing");
            var references = group.Get("mesh_references").Arr();
            if (references.Count != 1 || references[0] is not JsonObject reference) throw new NativeLodException("Multiple or empty meshes in a LOD group are unsupported");
            var meshName = reference.Get("mesh_name").Str();
            if (!meshes.TryGetValue(meshName, out var filename)) throw new NativeLodException($"LOD mesh {PathText.Repr(meshName)} has no RenderMeshFile");
            var source = SafeResourcePath(filename);
            if (!PathText.Suffix(source).Equals(".dmx", StringComparison.OrdinalIgnoreCase)) throw new NativeLodException($"LOD mesh {PathText.Repr(meshName)} is not a DMX source");
            lods.Add(new(distance, source));
            previous = distance;
        }
        if (lods[0].Distance != 0) throw new NativeLodException("The primary LOD group must have switch distance zero");
        return lods[1..];
    }

    private static List<double> AuthoredDistances(string data)
    {
        var documents = Kv3.ParseDocuments(data);
        if (documents.Count != 1) throw new NativeLodException("Expected one compiled model DATA document");
        return documents[0].Get("m_lodGroupSwitchDistances").Doubles().ToList();
    }

    /// <summary>Copies exported lower LODs beside a model and writes its portable <c>.lods.json</c> manifest.</summary>
    /// <remarks>Exact-file exports flatten companion DMXs beside their ModelDoc. All sources are checked before anything
    /// is written, including collisions after flattening. The archived ModelDoc keeps the authored paths.</remarks>
    public static int WriteSidecar(string modeldoc, string modelPath, IReadOnlyList<double> expectedDistances)
    {
        var lods = ParseModeldoc(File.ReadAllText(modeldoc));
        if (!lods.Select(lod => lod.Distance).SequenceEqual(expectedDistances.Skip(1))) throw new NativeLodException("ModelDoc LOD distances differ from compiled model DATA");
        var files = new List<string>();
        var names = new HashSet<string>();
        var folder = Path.GetDirectoryName(modeldoc)!;
        foreach (var lod in lods)
        {
            var name = Path.GetFileName(SafeResourcePath(lod.Source));
            if (names.Contains(name.ToLowerInvariant())) throw new NativeLodException($"Native LOD filenames collide after export: {name}");
            var source = Path.Combine(folder, name);
            if (!File.Exists(source)) throw new NativeLodException($"Native LOD companion was not exported: {name}");
            names.Add(name.ToLowerInvariant());
            files.Add(source);
        }
        var stem = Path.GetFileNameWithoutExtension(modelPath);
        var destination = Path.Combine(Path.GetDirectoryName(modelPath)!, stem + "_native_lods");
        Directory.CreateDirectory(destination);
        var entries = new JsonArray();
        for (var index = 0; index < lods.Count; index++)
        {
            var name = Path.GetFileName(files[index]);
            File.Copy(files[index], Path.Combine(destination, name), true);
            entries.Add(new JsonObject { ["distance"] = lods[index].Distance, ["source"] = $"{stem}_native_lods/{name}" });
        }
        File.Copy(modeldoc, Path.Combine(destination, "source.vmdl"), true);
        Json.Save(Path.ChangeExtension(modelPath, ".lods.json"), entries);
        return entries.Count;
    }

    /// <summary>Decompiles models with authored LODs, retaining only the DMX files they require.</summary>
    /// <remarks>Each model gets an isolated exact-file export to avoid output-path ambiguity. Unsupported groups or a failed
    /// decompile produce explicit warnings; cancellation propagates to the owning job.</remarks>
    public static async Task<int> ExportSidecarsAsync(string executable, string input, string staging, IReadOnlyDictionary<string, string> modelData, PortContext context)
    {
        var written = 0;
        foreach (var (modelName, data) in modelData)
        {
            context.Check();
            try
            {
                var distances = AuthoredDistances(data);
                if (distances.Count < 2) continue;
                var relative = SafeResourcePath(modelName);
                if (!PathText.Suffix(relative).Equals(".vmdl_c", StringComparison.OrdinalIgnoreCase)) throw new NativeLodException("Native LOD export needs a compiled VMDL resource");
                var modelPath = Path.Combine(staging, PathText.Native(Path.ChangeExtension(relative, ".glb")));
                if (!File.Exists(modelPath) && !File.Exists(Path.ChangeExtension(modelPath, ".gltf"))) throw new NativeLodException("No matching glTF model was exported");
                context.Log($"Preserving {distances.Count - 1} authored LOD mesh(es): {modelName}");
                var workspace = Path.Combine(staging, ".native_lods_" + Path.GetFileNameWithoutExtension(Path.GetRandomFileName()));
                Directory.CreateDirectory(workspace);
                try
                {
                    var modeldoc = Path.Combine(workspace, "model.vmdl");
                    List<string> command = [executable, "-i", input, "-e", "vmdl_c", "-f", relative, "-d", "-o", modeldoc, "--threads", "1"];
                    context.Log("Running: " + PathText.Format(command));
                    var result = await context.Processes.RunAsync(new(executable, command.Skip(1).ToArray()), line => context.Log(line.Text), context.Token);
                    if (result.ExitCode != 0 || !File.Exists(modeldoc)) throw new NativeLodException($"VRF native decompile failed (exit {result.ExitCode})");
                    written += WriteSidecar(modeldoc, modelPath, distances);
                }
                finally
                {
                    Directory.Delete(workspace, true);
                }
            }
            catch (Exception exception) when (ExportErrors.IsData(exception) || ExportErrors.IsDisk(exception) || ExportErrors.IsStart(exception))
            {
                context.Warn($"{modelName}: authored LODs were not preserved: {exception.Message}");
            }
        }
        if (written > 0) context.Log($"Preserved {written} authored lower LOD mesh(es) with switch distances");
        return written;
    }
}
