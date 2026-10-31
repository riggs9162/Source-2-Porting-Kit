using System.Buffers.Binary;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>Loads glTF and GLB files into one flat mesh, producing what the kit's scene load produced.</summary>
/// <remarks>
/// <para>Each primitive is its own piece, with vertices as stored and no welding.</para>
/// <para>What happens next depends on <see cref="GltfDocument.PositionSpace"/>, measured per file. A file still
/// carrying the 0.0254 matrix keeps raw POSITION values, piece by piece in mesh order, with every node ignored:
/// they are vmdl-space inches already. Anything else is baked through its scene graph, so legitimate node
/// placement survives, and pieces then arrive in graph order rather than mesh order.</para>
/// <para>Graph order is not file order. Roots and children are taken from the back of their lists, so a file
/// with two mesh nodes concatenates the second one first. Face order in a static SMD follows from that.</para>
/// <para>V is always flipped here (1 - v), once, in single precision, which is why a port leaves flip_v off.
/// A primitive with no material has no texture visual at all, so it gets zero UVs even when it has TEXCOORD_0.</para>
/// </remarks>
public static class GltfMeshLoader
{
    private const uint GlbMagic = 0x46546C67;
    private const uint GlbChunkJson = 0x4E4F534A;
    private const int ModeTriangles = 4;
    private const int ModeStrip = 5;
    private const string NoFaces = "Mesh has no faces after loading";

    private sealed record Piece(V3[] Vertices, int[] Faces, V2[]? Uvs, string? Material);

    /// <summary>Extracts the JSON chunk of a GLB, or returns null on any malformed read.</summary>
    /// <remarks>Callers treat this as best effort, so a missing or damaged header is an answer rather than a failure.</remarks>
    public static JsonObject? ReadGlbJson(string path)
    {
        try
        {
            var head = new byte[20];
            using var stream = File.OpenRead(path);
            stream.ReadExactly(head);
            if (BinaryPrimitives.ReadUInt32LittleEndian(head) != GlbMagic) return null;
            if (BinaryPrimitives.ReadUInt32LittleEndian(head.AsSpan(16)) != GlbChunkJson) return null;
            var chunk = new byte[BinaryPrimitives.ReadUInt32LittleEndian(head.AsSpan(12))];
            stream.ReadExactly(chunk);
            return Json.Parse(chunk);
        }
        catch (Exception error) when (error is IOException or JsonException or InvalidDataException or InvalidOperationException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    private static JsonObject? ReadJson(string path)
    {
        try
        {
            return Json.Load(path);
        }
        catch (Exception error) when (error is IOException or JsonException or InvalidOperationException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    private static JsonObject? Header(string path) => Path.GetExtension(path).Equals(".glb", StringComparison.OrdinalIgnoreCase) ? ReadGlbJson(path) : ReadJson(path);

    /// <summary>Reads the JSON of either container, or returns null when it cannot be read.</summary>
    internal static JsonObject? ReadHeader(string path) => Header(path);

    /// <summary>Checks a glTF's dependencies before loading it.</summary>
    /// <returns>False only when a geometry buffer is missing; missing textures are listed but not fatal, and a GLB is self-contained.</returns>
    public static (bool Success, List<string> Missing) PreflightCheck(string path)
    {
        if (Path.GetExtension(path).Equals(".glb", StringComparison.OrdinalIgnoreCase)) return (true, []);
        JsonObject data;
        try
        {
            data = Json.Load(path);
        }
        catch (Exception error) when (error is IOException or JsonException or InvalidOperationException or UnauthorizedAccessException)
        {
            return (false, [$"Failed to parse GLTF: {error.Message}"]);
        }
        var critical = new List<string>();
        var warnings = new List<string>();
        var directory = Path.GetDirectoryName(Path.GetFullPath(path)) ?? "";
        void Check(string key, string label, List<string> into)
        {
            var items = data.Get(key).Arr();
            for (var index = 0; index < items.Count; index++)
            {
                if (!items[index].Has("uri")) continue;
                var uri = items[index].Get("uri").Str();
                if (!uri.StartsWith("data:", StringComparison.Ordinal) && !Path.Exists(Path.Combine(directory, uri))) into.Add($"{label}[{index}]: {uri}");
            }
        }
        Check("buffers", "buffer", critical);
        Check("images", "texture", warnings);
        return (critical.Count == 0, [.. critical, .. warnings]);
    }

    private static V3[] Positions(GltfDocument document, int accessor)
    {
        var data = document.Accessor(accessor);
        var vertices = new V3[data.Count];
        for (var index = 0; index < vertices.Length; index++) vertices[index] = data.Vector(index);
        return vertices;
    }

    private static int[] StripFaces(int[] strip)
    {
        var faces = new int[Math.Max(strip.Length - 2, 0) * 3];
        for (var index = 0; index + 2 < strip.Length; index++)
        {
            var odd = index % 2 == 1;
            faces[index * 3] = strip[odd ? index + 2 : index];
            faces[index * 3 + 1] = strip[index + 1];
            faces[index * 3 + 2] = strip[odd ? index : index + 2];
        }
        return faces;
    }

    private static string? MaterialName(GltfDocument document, JsonNode? primitive)
    {
        var name = document.Materials[primitive.Get("material").Int()].Get("name");
        return name is null ? null : name.Str();
    }

    private static V2[]? FlippedUvs(GltfDocument document, JsonNode? primitive, JsonNode? attributes)
    {
        if (!primitive.Has("material") || !attributes.Has("TEXCOORD_0")) return null;
        var data = document.Accessor(attributes.Get("TEXCOORD_0").Int());
        var uvs = new V2[data.Count];
        for (var index = 0; index < uvs.Length; index++) uvs[index] = new((float)data[index, 0], 1.0f - (float)data[index, 1]);
        return uvs;
    }

    private static Piece? ReadPiece(GltfDocument document, JsonNode? primitive)
    {
        var mode = primitive.Has("mode") ? primitive.Get("mode").Int() : ModeTriangles;
        var attributes = primitive.Get("attributes");
        if (mode is not (ModeTriangles or ModeStrip)) return null;
        if (!attributes.Has("POSITION")) throw new InvalidDataException("'POSITION'");
        var vertices = Positions(document, attributes.Get("POSITION").Int());
        var indices = primitive.Has("indices") ? document.Accessor(primitive.Get("indices").Int()).Integers() : Enumerable.Range(0, vertices.Length).ToArray();
        var faces = mode == ModeStrip ? StripFaces(indices) : indices;
        if (faces.Length % 3 != 0) throw new InvalidDataException($"cannot reshape array of size {faces.Length} into shape (3)");
        return new(vertices, faces, FlippedUvs(document, primitive, attributes), primitive.Has("material") ? MaterialName(document, primitive) : null);
    }

    /// <summary>One transform for a node's edge: its matrix, then translation, rotation and scale, each applied after.</summary>
    private static M4 Local(JsonNode? node)
    {
        var matrix = M4.Identity;
        if (node.Has("matrix"))
        {
            var cells = node.Get("matrix").Doubles();
            var rows = new double[16];
            for (var row = 0; row < 4; row++)
                for (var column = 0; column < 4; column++)
                    rows[row * 4 + column] = cells[column * 4 + row];
            matrix = new(rows);
        }
        if (node.Has("translation"))
        {
            var (x, y, z) = node.Get("translation").Vec3();
            matrix *= new M4([1, 0, 0, x, 0, 1, 0, y, 0, 0, 1, z, 0, 0, 0, 1]);
        }
        if (node.Has("rotation")) matrix *= M4.FromQuaternion(node.Get("rotation").Quat());
        if (node.Has("scale"))
        {
            var (x, y, z) = node.Get("scale").Vec3();
            matrix *= new M4([x, 0, 0, 0, 0, y, 0, 0, 0, 0, z, 0, 0, 0, 0, 1]);
        }
        return matrix;
    }

    /// <summary>Restores a matrix that is almost a rotation by replacing its 3x3 block with the nearest orthogonal one.</summary>
    /// <remarks>
    /// <para>The scene graph does this to every world transform, and it matters more than it looks. Source 2 Viewer
    /// 20 leaves a node matrix behind whose diagonal is 0.99999994. Multiplied straight in, that shrinks a render
    /// mesh by about 1e-6in per 16in, enough to change printed SMD digits. Repaired, it becomes a rotation within
    /// 1e-14 of identity, which the transform step then skips, so the baked vertices stay exactly as stored.</para>
    /// <para>Only deviation between 1e-13 and 1e-5 is touched, so a real scale such as 0.0254 passes through.</para>
    /// </remarks>
    private static M4 FixRigid(M4 matrix)
    {
        var deviation = 0.0;
        for (var row = 0; row < 3; row++)
            for (var column = 0; column < 3; column++)
            {
                var dot = 0.0;
                for (var k = 0; k < 3; k++) dot += matrix[row, k] * matrix[column, k];
                deviation = Math.Max(deviation, Math.Abs(dot - (row == column ? 1.0 : 0.0)));
            }
        if (deviation is <= 1e-13 or >= 1e-5) return matrix;
        var rotation = new[] { matrix[0, 0], matrix[0, 1], matrix[0, 2], matrix[1, 0], matrix[1, 1], matrix[1, 2], matrix[2, 0], matrix[2, 1], matrix[2, 2] };
        for (var iteration = 0; iteration < 32; iteration++)
        {
            var next = AverageWithInverseTranspose(rotation);
            var change = next.Zip(rotation, (a, b) => Math.Abs(a - b)).Max();
            rotation = next;
            if (change < 1e-16) break;
        }
        return new([
            rotation[0], rotation[1], rotation[2], matrix[0, 3],
            rotation[3], rotation[4], rotation[5], matrix[1, 3],
            rotation[6], rotation[7], rotation[8], matrix[2, 3],
            0, 0, 0, 1]);
    }

    /// <summary>One step of the iteration converging on a matrix's orthogonal polar factor, the same matrix as U times V of its SVD.</summary>
    private static double[] AverageWithInverseTranspose(double[] m)
    {
        var c = new[]
        {
            m[4] * m[8] - m[5] * m[7], m[5] * m[6] - m[3] * m[8], m[3] * m[7] - m[4] * m[6],
            m[2] * m[7] - m[1] * m[8], m[0] * m[8] - m[2] * m[6], m[1] * m[6] - m[0] * m[7],
            m[1] * m[5] - m[2] * m[4], m[2] * m[3] - m[0] * m[5], m[0] * m[4] - m[1] * m[3]
        };
        var determinant = m[0] * c[0] + m[1] * c[1] + m[2] * c[2];
        var result = new double[9];
        for (var index = 0; index < 9; index++) result[index] = 0.5 * (m[index] + c[index] / determinant);
        return result;
    }

    private static bool NearlyIdentity(M4 matrix)
    {
        for (var row = 0; row < 4; row++)
            for (var column = 0; column < 4; column++)
                if (Math.Abs(matrix[row, column] - (row == column ? 1.0 : 0.0)) > 1e-8) return false;
        return true;
    }

    /// <summary>The scene's nodes with geometry, in graph order, each with its world transform.</summary>
    /// <remarks>The traversal is a stack, which is what reverses sibling order.</remarks>
    private static List<(int Mesh, M4 World)> SceneInstances(GltfDocument document)
    {
        var nodes = document.Nodes;
        var scenes = document.Root.Get("scenes").Arr();
        var instances = new List<(int Mesh, int Node)>();
        var parents = new Dictionary<int, int>();
        var stack = new Stack<(int Parent, int Child)>();
        var consumed = new HashSet<(int, int)>();
        if (scenes.Count > 0)
            foreach (var root in scenes[document.Root.Get("scene").Int()].Get("nodes").Arr()) stack.Push((-1, root.Int()));
        while (stack.Count > 0)
        {
            var edge = stack.Pop();
            if (!consumed.Add(edge)) continue;
            var (parent, child) = edge;
            var node = nodes[child];
            parents[child] = parent;
            foreach (var next in node.Get("children").Arr()) stack.Push((child, next.Int()));
            if (node.Has("mesh")) instances.Add((node.Get("mesh").Int(), child));
        }
        var result = new List<(int, M4)>(instances.Count);
        foreach (var (mesh, node) in instances)
        {
            var chain = new List<int>();
            for (var at = node; at >= 0; at = parents[at])
            {
                chain.Add(at);
                if (chain.Count > nodes.Count) throw new InvalidDataException("Loop in node parenting.");
            }
            chain.Reverse();
            var world = M4.Identity;
            foreach (var local in chain.Select(index => Local(nodes[index])).Where(local => !NearlyIdentity(local))) world *= local;
            result.Add((mesh, FixRigid(world)));
        }
        return result;
    }

    private static List<(Piece Piece, M4? World)> Flatten(GltfDocument document, List<List<Piece>> meshes, bool raw)
    {
        var pieces = new List<(Piece, M4?)>();
        if (raw)
        {
            foreach (var mesh in meshes)
                foreach (var piece in mesh) pieces.Add((piece, null));
            return pieces;
        }
        foreach (var (mesh, world) in SceneInstances(document))
            foreach (var piece in meshes[mesh]) pieces.Add((piece, world));
        return pieces;
    }

    private static void CheckContainer(string path)
    {
        if (!Path.GetExtension(path).Equals(".glb", StringComparison.OrdinalIgnoreCase)) return;
        var head = new byte[20];
        using var stream = File.OpenRead(path);
        stream.ReadExactly(head);
        if (BinaryPrimitives.ReadUInt32LittleEndian(head) != GlbMagic) throw new InvalidDataException("incorrect header on GLB file");
        var version = BinaryPrimitives.ReadUInt32LittleEndian(head.AsSpan(4));
        if (version != 2) throw new InvalidDataException($"only GLTF 2 is supported not `{version}`");
        if (BinaryPrimitives.ReadUInt32LittleEndian(head.AsSpan(16)) != GlbChunkJson) throw new InvalidDataException("no initial JSON header!");
    }

    private static string MaterialFor(string? name, JsonObject data) =>
        !string.IsNullOrEmpty(name) ? name
        : data.Get("materials").Arr() is [var only] && !string.IsNullOrEmpty(only.Get("name").Str()) ? only.Get("name").Str()
        : "material";

    private static ModelMesh? Concatenate(List<(Piece Piece, M4? World)> pieces, JsonObject data)
    {
        var names = new List<string>();
        var lookup = new Dictionary<string, int>(StringComparer.Ordinal);
        var vertices = new List<V3>();
        var faces = new List<int>();
        var uvs = new List<V2>();
        var materials = new List<int>();
        foreach (var (piece, world) in pieces)
        {
            if (piece.Faces.Length == 0) continue;
            var placed = new ModelMesh(piece.Vertices, piece.Faces);
            if (world is not null) placed.ApplyTransform(world);
            var name = MaterialFor(piece.Material, data);
            if (!lookup.TryGetValue(name, out var material))
            {
                lookup[name] = material = names.Count;
                names.Add(name);
            }
            materials.AddRange(Enumerable.Repeat(material, placed.FaceCount));
            var offset = vertices.Count;
            vertices.AddRange(placed.Vertices);
            faces.AddRange(placed.Faces.Select(index => index + offset));
            if (piece.Uvs is { } coordinates && coordinates.Length == placed.Vertices.Length) uvs.AddRange(coordinates);
            else uvs.AddRange(new V2[placed.Vertices.Length]);
        }
        if (vertices.Count == 0) return null;
        return new(vertices.ToArray(), faces.ToArray()) { Uvs = uvs.ToArray(), MaterialNames = names, FaceMaterials = materials.ToArray() };
    }

    /// <summary>Loads and flattens a model, keeping UV ranges and material slots exactly as stored.</summary>
    /// <returns>The mesh, or null with a reason. Failures are values because a stub physics file is ordinary.</returns>
    /// <remarks>UVs are never wrapped, clamped or normalized here; SMD export controls that. Pieces are concatenated
    /// by hand, because a general concatenation would atlas-pack per-piece textures and remap their UVs.</remarks>
    public static (ModelMesh? Mesh, string Error) LoadMesh(string path)
    {
        try
        {
            CheckContainer(path);
            var data = Header(path) ?? throw new InvalidDataException($"Could not read {path}");
            if (data.Get("meshes").Arr().Count == 0) return (null, NoFaces);
            var document = GltfDocument.Read(path);
            var meshes = new List<List<Piece>>();
            foreach (var mesh in document.Meshes)
            {
                var pieces = new List<Piece>();
                foreach (var primitive in mesh.Get("primitives").Arr())
                    if (ReadPiece(document, primitive) is { } piece) pieces.Add(piece);
                meshes.Add(pieces);
            }
            var space = document.PositionSpace();
            var native = space == GltfDocument.SpaceSource;
            var flat = Flatten(document, meshes, native);
            if (flat.Count == 0) return (null, NoFaces);
            var result = Concatenate(flat, data);
            if (result is null) return (null, NoFaces);
            result.NativeUnits = native;
            result.MetreUnits = space == GltfDocument.SpaceMetres;
            result.GltfData = data;
            result.SourcePath = path;
            return (result, "");
        }
        catch (Exception error) when (error is IOException or InvalidDataException or JsonException or InvalidOperationException or IndexOutOfRangeException or ArgumentException or FormatException or UnauthorizedAccessException)
        {
            return (null, error.Message);
        }
    }

    /// <summary>Whether a physics glTF actually carries geometry.</summary>
    /// <remarks>Source 2 Viewer writes a stub, with no nodes, meshes or accessors, for a model with no collision.
    /// That is a statement rather than a failure: Half-Life Alyx does not make that model solid either. A file
    /// that cannot be read at all answers true, because the absence of collision is then ours, not the source's.</remarks>
    public static bool DeclaresCollision(string? path)
    {
        if (path is null || !File.Exists(path)) return false;
        return Header(path) is not { } data || data.Get("meshes").Bool();
    }
}
