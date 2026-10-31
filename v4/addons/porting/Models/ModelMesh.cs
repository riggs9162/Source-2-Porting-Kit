using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>A flattened model mesh with the per-face material and unit markers a scene load leaves behind.</summary>
/// <remarks>
/// Vertex normals are always derived. A concatenated mesh never keeps the file's NORMAL stream, so a static
/// SMD carries angle-weighted normals computed from the final faces. That is why normals cannot be read
/// straight from the glTF here, and why a native reference has its own path that does keep them.
/// </remarks>
public sealed class ModelMesh(V3[] vertices, int[] faces)
{
    /// <summary>Below this a cross product or summed normal counts as zero.</summary>
    private const double ZeroLength = 1e-13;

    /// <summary>Faces with a corner angle under this are treated as having no angles at all.</summary>
    private const double MergeTolerance = 1e-8;

    public V3[] Vertices { get; set; } = vertices;

    /// <summary>Three vertex indices per triangle.</summary>
    public int[] Faces { get; set; } = faces;

    /// <summary>Per-vertex texture coordinates, or null when the mesh has none.</summary>
    public V2[]? Uvs { get; set; }

    public List<string>? MaterialNames { get; set; }

    /// <summary>One index into <see cref="MaterialNames"/> per face.</summary>
    public int[]? FaceMaterials { get; set; }

    /// <summary>Raw POSITION values were kept: vmdl-space inches, with the conversion still on a node.</summary>
    public bool NativeUnits { get; set; }

    /// <summary>Source 2 Viewer baked its metre and Y-up conversion into the vertices, so both remain to undo.</summary>
    public bool MetreUnits { get; set; }

    /// <summary>The parsed glTF JSON, kept so skin and clip data can be read without opening the file again.</summary>
    public JsonObject? GltfData { get; set; }

    public string? SourcePath { get; set; }

    public int FaceCount => Faces.Length / 3;

    /// <summary>Copies the mesh and its per-vertex data, dropping nothing.</summary>
    public ModelMesh Copy() => new((V3[])Vertices.Clone(), (int[])Faces.Clone())
    {
        Uvs = (V2[]?)Uvs?.Clone(),
        MaterialNames = MaterialNames is null ? null : [.. MaterialNames],
        FaceMaterials = (int[]?)FaceMaterials?.Clone(),
        NativeUnits = NativeUnits,
        MetreUnits = MetreUnits,
        GltfData = GltfData,
        SourcePath = SourcePath
    };

    /// <summary>The geometry alone, as the collision passes take it.</summary>
    public TriMesh ToTriMesh() => new(Vertices, Faces);

    /// <summary>Wraps a rebuilt collision mesh. Rebuilding drops materials and UVs along with the old topology.</summary>
    public static ModelMesh FromTriMesh(TriMesh mesh) => new(mesh.Vertices, mesh.Faces);

    /// <summary>The axis-aligned extent of the vertices faces actually use, or zero when there are none.</summary>
    public double LargestExtent()
    {
        var used = new bool[Vertices.Length];
        foreach (var index in Faces)
            if (index >= 0 && index < used.Length) used[index] = true;
            else return 0.0;
        double minX = double.MaxValue, minY = double.MaxValue, minZ = double.MaxValue;
        double maxX = double.MinValue, maxY = double.MinValue, maxZ = double.MinValue;
        var any = false;
        for (var index = 0; index < used.Length; index++)
        {
            if (!used[index]) continue;
            var (x, y, z) = Vertices[index];
            any = true;
            minX = Math.Min(minX, x);
            minY = Math.Min(minY, y);
            minZ = Math.Min(minZ, z);
            maxX = Math.Max(maxX, x);
            maxY = Math.Max(maxY, y);
            maxZ = Math.Max(maxZ, z);
        }
        return any ? Math.Max(maxX - minX, Math.Max(maxY - minY, maxZ - minZ)) : 0.0;
    }

    /// <summary>Whether a square block of a matrix differs from identity by less than a tolerance.</summary>
    /// <remarks>This measures the spread of the differences, not their size, the way the original's own
    /// fast comparison does: a block offset evenly from identity counts as close however far it is.</remarks>
    private static bool Close(M4 matrix, int size, double tolerance)
    {
        double low = double.MaxValue, high = double.MinValue;
        for (var row = 0; row < size; row++)
            for (var column = 0; column < size; column++)
            {
                var difference = matrix[row, column] - (row == column ? 1.0 : 0.0);
                low = Math.Min(low, difference);
                high = Math.Max(high, difference);
            }
        return high - low < tolerance;
    }

    private static double Determinant(M4 m) =>
        m[0, 0] * (m[1, 1] * m[2, 2] - m[1, 2] * m[2, 1]) -
        m[0, 1] * (m[1, 0] * m[2, 2] - m[1, 2] * m[2, 0]) +
        m[0, 2] * (m[1, 0] * m[2, 1] - m[1, 1] * m[2, 0]);

    /// <summary>Moves the mesh, reversing every face when the matrix mirrors it.</summary>
    /// <remarks>A matrix within 1e-8 of identity is skipped outright, and reversing is judged on the 3x3 block,
    /// where a matrix within 1e-6 of identity can never mirror.</remarks>
    public void ApplyTransform(M4 matrix)
    {
        if (Close(matrix, 4, 1e-8)) return;
        var moved = new V3[Vertices.Length];
        for (var index = 0; index < moved.Length; index++) moved[index] = matrix.Point(Vertices[index]);
        Vertices = moved;
        if (Close(matrix, 3, 1e-6) || Determinant(matrix) >= 0.0) return;
        var flipped = new int[Faces.Length];
        for (var index = 0; index < Faces.Length; index += 3)
        {
            flipped[index] = Faces[index + 2];
            flipped[index + 1] = Faces[index + 1];
            flipped[index + 2] = Faces[index];
        }
        Faces = flipped;
    }

    /// <summary>Scales about the origin.</summary>
    public void ApplyScale(double scale) => ApplyTransform(new([scale, 0, 0, 0, 0, scale, 0, 0, 0, 0, scale, 0, 0, 0, 0, 1]));

    /// <summary>The cross product of one face's first two consecutive edges.</summary>
    private V3 Cross(int face)
    {
        V3 a = Vertices[Faces[face * 3]], b = Vertices[Faces[face * 3 + 1]], c = Vertices[Faces[face * 3 + 2]];
        return (b - a).Cross(c - b);
    }

    /// <summary>Unit face normals. A face with no measurable area gets a zero normal rather than an arbitrary one.</summary>
    public V3[] FaceNormals()
    {
        var normals = new V3[FaceCount];
        for (var face = 0; face < normals.Length; face++) normals[face] = Unit(Cross(face));
        return normals;
    }

    private static V3 Unit(V3 vector)
    {
        var length = vector.Length;
        return length > ZeroLength ? vector * (1.0 / length) : V3.Zero;
    }

    /// <summary>Corner angles of one face, all zero when any of them collapses.</summary>
    private (double A, double B, double C) FaceAngles(int face)
    {
        V3 a = Vertices[Faces[face * 3]], b = Vertices[Faces[face * 3 + 1]], c = Vertices[Faces[face * 3 + 2]];
        V3 u = Unit(b - a), v = Unit(c - a), w = Unit(c - b);
        var first = Math.Acos(Math.Clamp(u.Dot(v), -1.0, 1.0));
        var second = Math.Acos(Math.Clamp((-u).Dot(w), -1.0, 1.0));
        var third = Math.PI - first - second;
        return first < MergeTolerance || second < MergeTolerance || third < MergeTolerance ? (0.0, 0.0, 0.0) : (first, second, third);
    }

    /// <summary>Normals weighted by each face's corner angle at the vertex, then unitized.</summary>
    /// <remarks>Vertices no face reaches come out as zero, as do those surrounded only by degenerate faces.</remarks>
    public V3[] VertexNormals()
    {
        var faceNormals = FaceNormals();
        var summed = new V3[Vertices.Length];
        for (var face = 0; face < faceNormals.Length; face++)
        {
            var normal = faceNormals[face];
            if (normal.Dot(normal) <= 0.5) continue;
            var (a, b, c) = FaceAngles(face);
            summed[Faces[face * 3]] += normal * a;
            summed[Faces[face * 3 + 1]] += normal * b;
            summed[Faces[face * 3 + 2]] += normal * c;
        }
        for (var index = 0; index < summed.Length; index++) summed[index] = Unit(summed[index]);
        return summed;
    }

    /// <summary>Keeps the faces a mask selects, with their materials.</summary>
    public void UpdateFaces(bool[] keep)
    {
        if (keep.All(flag => flag)) return;
        var faces = new List<int>(Faces.Length);
        var materials = FaceMaterials is null ? null : new List<int>(FaceMaterials.Length);
        for (var face = 0; face < keep.Length; face++)
        {
            if (!keep[face]) continue;
            faces.Add(Faces[face * 3]);
            faces.Add(Faces[face * 3 + 1]);
            faces.Add(Faces[face * 3 + 2]);
            materials?.Add(FaceMaterials![face]);
        }
        Faces = faces.ToArray();
        FaceMaterials = materials?.ToArray();
    }

    /// <summary>Keeps the vertices a mask selects, rewriting face indices through the survivors.</summary>
    /// <remarks>A face that referred to a dropped vertex ends up pointing at vertex zero, exactly as the
    /// array-based original does; nothing here removes that face.</remarks>
    public void UpdateVertices(bool[] keep)
    {
        if (keep.All(flag => flag)) return;
        var remap = new int[keep.Length];
        var next = 0;
        for (var index = 0; index < keep.Length; index++)
            if (keep[index]) remap[index] = next++;
        Faces = Array.ConvertAll(Faces, index => remap[index]);
        var vertices = new V3[next];
        var uvs = Uvs is null ? null : new V2[next];
        next = 0;
        for (var index = 0; index < keep.Length; index++)
        {
            if (!keep[index]) continue;
            vertices[next] = Vertices[index];
            if (uvs is not null) uvs[next] = Uvs![index];
            next++;
        }
        Vertices = vertices;
        Uvs = uvs;
    }

    /// <summary>Drops vertices holding NaN or infinity.</summary>
    public void RemoveInfiniteValues() => UpdateVertices(Array.ConvertAll(Vertices, vertex => vertex.IsFinite));

    /// <summary>Marks faces whose two-dimensional bounding box is thicker than the merge tolerance on both sides.</summary>
    public bool[] NondegenerateFaces()
    {
        var keep = new bool[FaceCount];
        for (var face = 0; face < keep.Length; face++)
        {
            var twiceArea = Cross(face).Length;
            var origin = Vertices[Faces[face * 3]];
            double lengthA = (Vertices[Faces[face * 3 + 1]] - origin).Length, lengthB = (Vertices[Faces[face * 3 + 2]] - origin).Length;
            var heightA = lengthA > MergeTolerance ? twiceArea / lengthA : 0.0;
            var heightB = lengthB > MergeTolerance ? twiceArea / lengthB : 0.0;
            keep[face] = heightA > MergeTolerance && heightB > MergeTolerance;
        }
        return keep;
    }

    /// <summary>Marks the first face using each set of three vertices, whatever its winding.</summary>
    public bool[] UniqueFaces()
    {
        var keep = new bool[FaceCount];
        var seen = new HashSet<(int, int, int)>(keep.Length);
        for (var face = 0; face < keep.Length; face++)
        {
            int a = Faces[face * 3], b = Faces[face * 3 + 1], c = Faces[face * 3 + 2];
            if (a > b) (a, b) = (b, a);
            if (b > c) (b, c) = (c, b);
            if (a > b) (a, b) = (b, a);
            keep[face] = seen.Add((a, b, c));
        }
        return keep;
    }

    /// <summary>Drops every vertex no face refers to, which also reindexes the rest.</summary>
    public void RemoveUnreferencedVertices()
    {
        var used = new bool[Vertices.Length];
        foreach (var index in Faces) used[index] = true;
        UpdateVertices(used);
    }
}
