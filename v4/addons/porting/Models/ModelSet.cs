using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>One row of a Source 1 <c>$texturegroup</c>: a named variant replacing the default skin column for column.</summary>
/// <remarks>Comes from the <c>&lt;model&gt;.skins.json</c> sidecar an export writes for a model with several material groups.</remarks>
public sealed record SkinDef(string Name, List<string> Materials);

/// <summary>A source glTF model with its optional physics companion.</summary>
public sealed record ModelSet(string Name, string BaseDir, string RenderPath, string? PhysicsPath = null)
{
    public List<string> Warnings { get; init; } = [];
    public List<SkinDef> Skins { get; init; } = [];
}

/// <summary>Scans a folder for glTF and GLB model sets, optionally recursing.</summary>
public sealed partial class ModelSetScanner(string root, bool recursive = true)
{
    [GeneratedRegex(@"^.*physics\.(gltf|glb)$", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant)]
    private static partial Regex PhysicsPattern();

    public string Root { get; } = root;
    public bool Recursive { get; } = recursive;

    /// <summary>Whether a file name indicates a physics mesh.</summary>
    public static bool IsPhysicsFile(string filename) => PhysicsPattern().IsMatch(filename);

    private static List<string> Names(string directory, bool files)
    {
        try
        {
            return (files ? Directory.EnumerateFiles(directory) : Directory.EnumerateDirectories(directory)).Select(path => Path.GetFileName(path)).ToList();
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            return [];
        }
    }

    private IEnumerable<(string Base, List<string> Files)> Walk(string directory)
    {
        yield return (directory, Names(directory, true));
        if (!Recursive) yield break;
        foreach (var child in Names(directory, false))
            foreach (var entry in Walk(Path.Combine(directory, child))) yield return entry;
    }

    /// <summary>Finds every render model, pairing it with a physics file named after it.</summary>
    /// <remarks>A pair matches when dropping <c>_physics</c> or <c>physics</c> from the physics stem leaves the render name.</remarks>
    public List<ModelSet> FindSets()
    {
        var sets = new List<ModelSet>();
        var seen = new HashSet<(string, string)>();
        foreach (var (directory, files) in Walk(Root))
        {
            var gltf = files.Where(file => file.EndsWith(".gltf", StringComparison.OrdinalIgnoreCase) || file.EndsWith(".glb", StringComparison.OrdinalIgnoreCase)).ToList();
            if (gltf.Count == 0) continue;
            var physics = gltf.Where(IsPhysicsFile).ToList();
            foreach (var render in gltf.Where(file => !IsPhysicsFile(file)))
            {
                var name = Path.GetFileNameWithoutExtension(render);
                var renderPath = Path.Combine(directory, render);
                string? physicsPath = null;
                foreach (var candidate in physics)
                {
                    var stem = Path.GetFileNameWithoutExtension(candidate).ToLowerInvariant().Replace("_physics", "").Replace("physics", "");
                    if (stem != name.ToLowerInvariant()) continue;
                    physicsPath = Path.Combine(directory, candidate);
                    break;
                }
                if (!seen.Add((directory, name))) continue;
                var warnings = new List<string>();
                if (!File.Exists(renderPath)) warnings.Add("Render file missing");
                if (physicsPath is not null && !File.Exists(physicsPath)) warnings.Add("Physics file missing");
                sets.Add(new(name, directory, renderPath, physicsPath) { Warnings = warnings, Skins = LoadSkinSidecar(renderPath) });
            }
        }
        return sets;
    }

    /// <summary>Loads a render model's skin sidecar. Skins are an optional enrichment, so anything malformed reads as none.</summary>
    public static List<SkinDef> LoadSkinSidecar(string renderPath)
    {
        var sidecar = Path.ChangeExtension(renderPath, ".skins.json");
        if (!File.Exists(sidecar)) return [];
        JsonObject payload;
        try
        {
            payload = Json.Load(sidecar);
        }
        catch (Exception error) when (error is IOException or JsonException or InvalidOperationException or UnauthorizedAccessException)
        {
            return [];
        }
        var result = new List<SkinDef>();
        foreach (var entry in payload.Get("skins").Arr())
        {
            if (entry is not JsonObject skin || skin.Get("name") is not JsonValue name || name.GetValueKind() != JsonValueKind.String) continue;
            if (skin.Get("materials") is not JsonArray materials) continue;
            var list = materials.Where(item => item is JsonValue value && value.GetValueKind() == JsonValueKind.String).Select(item => item.Str()).ToList();
            if (list.Count > 0) result.Add(new(name.Str(), list));
        }
        return result;
    }
}
