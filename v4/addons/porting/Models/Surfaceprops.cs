using System.Text.Json;
using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>Picks a Source 1 surfaceprop for a model.</summary>
/// <remarks>A real Source 2 surfaceprop from the model's physics data always beats a guess, so name keywords
/// are only the fallback. The choice is load-bearing beyond footstep sounds: it sets the density and whether
/// mass is computed as a solid or a hollow shell. Order is deliberate, with specific materials first, so
/// "metal grate" lands on <c>metalgrate</c> (a 0.5in shell) rather than <c>metal</c>.</remarks>
public static class SurfacepropDetector
{
    private static readonly (string Surfaceprop, string[] Keywords)[] Heuristics =
    [
        ("metalgrate", ["grate", "grating", "catwalk"]),
        ("metalvent", ["vent", "duct", "ventilation"]),
        ("metalpanel", ["panel", "sheet", "plating", "siding"]),
        ("chainlink", ["chainlink", "chain_link", "fence", "wiremesh"]),
        ("metal", ["metal", "pipe", "iron", "steel", "aluminum", "aluminium", "girder", "beam", "rebar"]),
        ("wood_crate", ["crate"]),
        ("wood_furniture", ["chair", "table", "desk", "shelf", "cabinet", "furniture"]),
        ("wood_panel", ["plywood", "board", "plank"]),
        ("wood", ["wood", "timber", "log", "barrel"]),
        ("glassbottle", ["bottle"]),
        ("glass", ["glass", "window", "mirror"]),
        ("porcelain", ["porcelain", "sink", "toilet", "ceramic"]),
        ("tile", ["tile"]),
        ("concrete", ["concrete", "cement", "curb", "sidewalk"]),
        ("brick", ["brick"]),
        ("rock", ["rock", "stone", "boulder"]),
        ("plaster", ["plaster", "drywall", "stucco"]),
        ("cardboard", ["cardboard", "carton"]),
        ("paper", ["paper", "poster", "newspaper"]),
        ("plastic", ["plastic", "polymer", "vinyl"]),
        ("carpet", ["carpet", "rug", "mat"]),
        ("foliage", ["foliage", "plant", "leaf", "tree", "bush", "xen", "flora"]),
        ("dirt", ["dirt", "soil", "ground", "mud"]),
        ("sand", ["sand"]),
        ("computer", ["computer", "monitor", "console", "terminal", "keyboard"])
    ];

    /// <summary>The surfaceprop for a model; a source value wins outright when it maps to something real.</summary>
    public static string Detect(string name, string? materialName = null, string? source2Prop = null)
    {
        if (!string.IsNullOrEmpty(source2Prop))
        {
            var resolved = StaticCollision.ResolveSurfaceprop(source2Prop);
            if (resolved != "default") return resolved;
        }
        var text = name.ToLowerInvariant();
        if (!string.IsNullOrEmpty(materialName)) text += " " + materialName.ToLowerInvariant();
        foreach (var (surfaceprop, keywords) in Heuristics)
            if (keywords.Any(keyword => text.Contains(keyword, StringComparison.Ordinal))) return surfaceprop;
        return "default";
    }
}

/// <summary>Reads the surface properties a model declares and applies user overrides.</summary>
public static class Surfaceprops
{
    /// <summary>The Source 2 surface properties a glTF declares, weighted by how much each covers.</summary>
    /// <remarks>Source 2 Viewer groups collision by surface property, names each node
    /// <c>physics_group_&lt;property&gt;</c> and repeats the property in that node's extras. That is the model's
    /// own answer to what the name heuristics can only guess. The weight is the group's vertex count, read from
    /// accessor headers. A model that is mostly metal with one pane of glass therefore resolves to metal: Source 1
    /// carries one <c>$surfaceprop</c> per model, so something has to win, and coverage beats declaration order.
    /// A group with no mesh still counts for one, because it declared itself.</remarks>
    public static Dictionary<string, int> DeclaredSurfaceprops(JsonObject gltf)
    {
        var weights = new Dictionary<string, int>(StringComparer.Ordinal);
        var meshes = gltf.Get("meshes").Arr();
        var accessors = gltf.Get("accessors").Arr();
        foreach (var node in gltf.Get("nodes").Arr())
        {
            if (node.Get("extras").Get("SurfaceProperty") is not JsonValue value || value.GetValueKind() != JsonValueKind.String) continue;
            var key = value.Str().Trim();
            if (key.Length == 0) continue;
            var vertices = 0;
            if (node.Get("mesh") is JsonValue reference && reference.GetValueKind() == JsonValueKind.Number)
            {
                var index = reference.Int();
                if (index >= 0 && index < meshes.Count)
                    foreach (var primitive in meshes[index].Get("primitives").Arr())
                        if (primitive.Get("attributes").Get("POSITION") is JsonValue position && position.GetValueKind() == JsonValueKind.Number)
                        {
                            var at = position.Int();
                            if (at >= 0 && at < accessors.Count) vertices += accessors[at].Get("count").Int();
                        }
            }
            weights[key] = weights.GetValueOrDefault(key) + Math.Max(vertices, 1);
        }
        return weights;
    }

    /// <summary>The declared properties of a glTF on disk, or none when it has none or cannot be read.</summary>
    public static Dictionary<string, int> GltfSurfaceprops(string? path)
    {
        if (path is null || !File.Exists(path)) return new(StringComparer.Ordinal);
        return GltfMeshLoader.ReadHeader(path) is { } data ? DeclaredSurfaceprops(data) : new(StringComparer.Ordinal);
    }

    /// <summary>The first declared key with the highest weight, or null when nothing was declared.</summary>
    public static string? Strongest(Dictionary<string, int> declared)
    {
        string? best = null;
        var weight = int.MinValue;
        foreach (var (key, value) in declared)
        {
            if (value <= weight) continue;
            best = key;
            weight = value;
        }
        return best;
    }

    /// <summary>The surfaceprop forced on a model name, or null when no rule applies.</summary>
    /// <remarks>A bare value forces itself on every model. <c>pattern=prop</c> forces one only where the name
    /// matches, as a case-insensitive substring or a glob when the pattern has <c>*</c> or <c>?</c>. A folder can
    /// keep the automatic choice where the guess is right and be corrected where it is not: the detector reads
    /// names, and a name is a poor witness. A doorframe called <c>..._windowed_wrapped</c> lands on <c>glass</c>,
    /// a 0.1in shell material, which changes the compiled mass as well as the footsteps. Entries split on newlines
    /// or commas, the first matching pattern wins, and a bare value is the fallback rather than a competitor. A bare
    /// value with a wildcard is a rule someone forgot to finish, because no Source 1 material has one in its name.</remarks>
    public static string? SurfacepropOverrideFor(string name, string? rules)
    {
        var subject = name.ToLowerInvariant();
        string? fallback = null;
        foreach (var line in PythonText.SplitLines(rules ?? ""))
            foreach (var raw in line.Split(','))
            {
                var entry = raw.Trim();
                if (entry.Length == 0) continue;
                var equals = entry.IndexOf('=');
                if (equals < 0)
                {
                    if (fallback is null && !entry.AsSpan().ContainsAny("*?")) fallback = entry;
                    continue;
                }
                var pattern = entry[..equals].Trim().ToLowerInvariant();
                var value = entry[(equals + 1)..].Trim();
                if (pattern.Length == 0 || value.Length == 0) continue;
                var matched = pattern.AsSpan().ContainsAny("*?") ? PythonText.FnMatch(subject, pattern) : subject.Contains(pattern, StringComparison.Ordinal);
                if (matched) return value;
            }
        return fallback;
    }
}
