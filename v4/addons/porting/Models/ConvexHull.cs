namespace PortingKit.Addons.Porting;

/// <summary>A closed convex hull with outward-facing triangles, used to fit generated ragdoll bodies.</summary>
/// <remarks>Built incrementally: start from a tetrahedron, then let each outside point replace the faces it can see.
/// The result is the same solid as the original's Qhull call, but where several points are coplanar the choice of
/// triangles can differ, so generated ragdoll SMDs match in shape rather than vertex for vertex.</remarks>
internal sealed class ConvexHull
{
    private const double Tolerance = 1e-9;

    private ConvexHull(V3[] vertices, int[] faces)
    {
        Mesh = new(vertices, faces);
    }

    public ModelMesh Mesh { get; }

    /// <summary>The axis-aligned size on each axis.</summary>
    public V3 Extents
    {
        get
        {
            V3 low = Mesh.Vertices[0], high = Mesh.Vertices[0];
            foreach (var (x, y, z) in Mesh.Vertices)
            {
                low = new(Math.Min(low.X, x), Math.Min(low.Y, y), Math.Min(low.Z, z));
                high = new(Math.Max(high.X, x), Math.Max(high.Y, y), Math.Max(high.Z, z));
            }
            return high - low;
        }
    }

    /// <summary>The enclosed volume, by summing signed tetrahedra.</summary>
    public double Volume
    {
        get
        {
            var total = 0.0;
            for (var index = 0; index < Mesh.Faces.Length; index += 3)
            {
                V3 a = Mesh.Vertices[Mesh.Faces[index]], b = Mesh.Vertices[Mesh.Faces[index + 1]], c = Mesh.Vertices[Mesh.Faces[index + 2]];
                total += a.Dot(b.Cross(c));
            }
            return total / 6.0;
        }
    }

    private static double Side(V3[] points, (int A, int B, int C) face, V3 point)
    {
        var normal = (points[face.B] - points[face.A]).Cross(points[face.C] - points[face.A]);
        var length = normal.Length;
        return length <= 0 ? 0.0 : normal.Dot(point - points[face.A]) / length;
    }

    private static (int, int, int, int)? Seed(V3[] points)
    {
        var first = 0;
        var second = -1;
        var best = 0.0;
        for (var index = 1; index < points.Length; index++)
        {
            var distance = (points[index] - points[first]).Length;
            if (distance <= best) continue;
            best = distance;
            second = index;
        }
        if (second < 0) return null;
        var third = -1;
        best = Tolerance;
        var axis = points[second] - points[first];
        for (var index = 0; index < points.Length; index++)
        {
            var area = axis.Cross(points[index] - points[first]).Length;
            if (area <= best) continue;
            best = area;
            third = index;
        }
        if (third < 0) return null;
        var fourth = -1;
        best = Tolerance;
        for (var index = 0; index < points.Length; index++)
        {
            var height = Math.Abs(Side(points, (first, second, third), points[index]));
            if (height <= best) continue;
            best = height;
            fourth = index;
        }
        return fourth < 0 ? null : (first, second, third, fourth);
    }

    /// <summary>Builds the hull of a point cloud, or fails when the cloud has no volume.</summary>
    public static ConvexHull Build(IReadOnlyList<V3> cloud)
    {
        var points = cloud.ToArray();
        if (Seed(points) is not var (a, b, c, d)) throw new InvalidDataException("Convex hull needs four points that are not coplanar.");
        var centre = (points[a] + points[b] + points[c] + points[d]) / 4.0;
        (int, int, int) Outward((int A, int B, int C) face) => Side(points, face, centre) > 0 ? (face.A, face.C, face.B) : face;
        var faces = new List<(int A, int B, int C)> { Outward((a, b, c)), Outward((a, b, d)), Outward((a, c, d)), Outward((b, c, d)) };
        for (var index = 0; index < points.Length; index++)
        {
            var point = points[index];
            var visible = faces.Where(face => Side(points, face, point) > Tolerance).ToList();
            if (visible.Count == 0) continue;
            var edges = new Dictionary<(int, int), int>();
            foreach (var (x, y, z) in visible)
                foreach (var edge in new[] { (x, y), (y, z), (z, x) })
                    edges[edge] = edges.GetValueOrDefault(edge) + 1;
            var horizon = edges.Keys.Where(edge => !edges.ContainsKey((edge.Item2, edge.Item1))).ToList();
            faces.RemoveAll(visible.Contains);
            foreach (var (from, to) in horizon) faces.Add((from, to, index));
        }
        var remap = new Dictionary<int, int>();
        var vertices = new List<V3>();
        var indices = new List<int>(faces.Count * 3);
        int Map(int index)
        {
            if (!remap.TryGetValue(index, out var mapped))
            {
                remap[index] = mapped = vertices.Count;
                vertices.Add(points[index]);
            }
            return mapped;
        }
        foreach (var (x, y, z) in faces)
        {
            indices.Add(Map(x));
            indices.Add(Map(y));
            indices.Add(Map(z));
        }
        return new(vertices.ToArray(), indices.ToArray());
    }
}
