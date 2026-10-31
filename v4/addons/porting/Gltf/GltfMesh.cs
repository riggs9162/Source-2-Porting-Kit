using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>A triangle list with no vertex welding or reordering.</summary>
public sealed class TriMesh(V3[] vertices, int[] faces)
{
    public V3[] Vertices { get; } = vertices;

    /// <summary>Three vertex indices per triangle.</summary>
    public int[] Faces { get; } = faces.Length % 3 == 0 ? faces : throw new InvalidDataException("Triangle indices must come in threes.");

    public int FaceCount => Faces.Length / 3;

    public static TriMesh Concatenate(IEnumerable<TriMesh> meshes)
    {
        var vertices = new List<V3>();
        var faces = new List<int>();
        foreach (var mesh in meshes)
        {
            var offset = vertices.Count;
            vertices.AddRange(mesh.Vertices);
            faces.AddRange(mesh.Faces.Select(index => index + offset));
        }
        return new(vertices.ToArray(), faces.ToArray());
    }

    /// <summary>Reads raw POSITION data without applying scene transforms, or returns null when there is none.</summary>
    /// <remarks>Source 2 Viewer physics exports keep vmdl-space inches in POSITION, the space the porter works in.</remarks>
    public static TriMesh? Load(string path)
    {
        var document = GltfDocument.Read(path);
        var parts = new List<TriMesh>();
        foreach (var mesh in document.Meshes)
            foreach (var primitive in mesh.Get("primitives").Arr())
            {
                var attributes = primitive.Get("attributes");
                if (!attributes.Has("POSITION") || !primitive.Has("indices")) continue;
                var positions = document.Accessor(attributes.Get("POSITION").Int());
                var vertices = new V3[positions.Count];
                for (var i = 0; i < vertices.Length; i++) vertices[i] = positions.Vector(i);
                parts.Add(new(vertices, document.Accessor(primitive.Get("indices").Int()).Integers()));
            }
        return parts.Count == 0 ? null : Concatenate(parts);
    }
}

/// <summary>One skinned glTF primitive kept exactly as exported.</summary>
public sealed class SkinnedPrimitive
{
    public required V3[] Vertices { get; init; }
    public V3[]? Normals { get; init; }
    public V2[]? Texcoords { get; init; }

    /// <summary>Joint indices, <see cref="Slots"/> per vertex, indexing the skin's joint list.</summary>
    public required int[] Joints { get; init; }

    /// <summary>Weights in step with <see cref="Joints"/>.</summary>
    public required double[] Weights { get; init; }

    public required int Slots { get; init; }
    public required int[] Indices { get; init; }
    public required string Material { get; init; }

    public int Joint(int vertex, int slot) => Joints[vertex * Slots + slot];
    public double Weight(int vertex, int slot) => Weights[vertex * Slots + slot];

    /// <summary>Reads per-mesh-node skinned primitives, stripping the vmdl path prefix every mesh name carries.</summary>
    public static (List<(string Name, List<SkinnedPrimitive> Primitives)> Sets, List<SkinnedPrimitive> All) Collect(GltfDocument document, PortContext? context = null)
    {
        var sets = new List<(string Name, List<SkinnedPrimitive> Primitives)>();
        var all = new List<SkinnedPrimitive>();
        var nodes = document.Nodes;
        for (var index = 0; index < nodes.Count; index++)
        {
            var node = nodes[index];
            if (!node.Has("mesh") || !node.Has("skin")) continue;
            var mesh = document.Meshes[node.Get("mesh").Int()];
            var name = mesh.Get("name").Str();
            if (name.Length == 0) name = node.Get("name").Str();
            if (name.Length == 0) name = $"mesh{index}";
            var primitives = new List<SkinnedPrimitive>();
            foreach (var entry in mesh.Get("primitives").Arr())
            {
                var attributes = entry.Get("attributes");
                if (!attributes.Has("JOINTS_0")) continue;
                var primitive = Read(document, entry, attributes);
                primitives.Add(primitive);
                all.Add(primitive);
            }
            if (primitives.Count > 0) sets.Add((name, primitives));
        }
        if (sets.Count == 0) throw new InvalidDataException("no skinned meshes found in the GLB");
        if (sets.Count > 1)
        {
            var prefix = sets[0].Name;
            foreach (var (name, _) in sets.Skip(1))
                while (!name.StartsWith(prefix, StringComparison.Ordinal)) prefix = prefix[..^1];
            if (prefix.Length > 3)
                sets = sets.ConvertAll(set => (set.Name.Length > prefix.Length ? set.Name[prefix.Length..] : set.Name, set.Primitives));
        }
        context?.Log($"{sets.Count} mesh set(s): {string.Join(", ", sets.Select(set => set.Name))}");
        return (sets, all);
    }

    private static SkinnedPrimitive Read(GltfDocument document, JsonNode? primitive, JsonNode? attributes)
    {
        var positions = document.Accessor(attributes.Get("POSITION").Int());
        var vertices = new V3[positions.Count];
        for (var i = 0; i < vertices.Length; i++) vertices[i] = positions.Vector(i);
        V3[]? normals = null;
        if (attributes.Has("NORMAL"))
        {
            var data = document.Accessor(attributes.Get("NORMAL").Int());
            normals = new V3[data.Count];
            for (var i = 0; i < normals.Length; i++) normals[i] = data.Vector(i);
        }
        V2[]? texcoords = null;
        if (attributes.Has("TEXCOORD_0"))
        {
            var data = document.Accessor(attributes.Get("TEXCOORD_0").Int());
            texcoords = new V2[data.Count];
            for (var i = 0; i < texcoords.Length; i++) texcoords[i] = data.Texcoord(i);
        }
        var joints = document.Accessor(attributes.Get("JOINTS_0").Int());
        var weights = document.Accessor(attributes.Get("WEIGHTS_0").Int());
        var values = (double[])weights.Values.Clone();
        if (values.Length > 0 && values.Max() > 2.0)
            for (var i = 0; i < values.Length; i++) values[i] /= 255.0;
        var material = "default";
        if (primitive.Has("material"))
        {
            var named = document.Materials[primitive.Get("material").Int()].Get("name");
            if (named is not null) material = named.Str();
        }
        return new()
        {
            Vertices = vertices, Normals = normals, Texcoords = texcoords,
            Joints = joints.Integers(), Weights = values, Slots = joints.Width,
            Indices = document.Accessor(primitive.Get("indices").Int()).Integers(), Material = material
        };
    }
}
