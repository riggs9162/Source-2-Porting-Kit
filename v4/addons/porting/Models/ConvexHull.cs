namespace PortingKit.Addons.Porting;

/// <summary>A closed convex hull with outward-facing triangles, used to fit generated ragdoll bodies.</summary>
/// <remarks>
/// <para>This is QuickHull: start from a tetrahedron, repeatedly take the point farthest outside some face, and
/// replace the connected region of faces that point can see. Farthest-first and the flood across face
/// neighbours are both load-bearing. Adding points in input order on rounded model data, where whole panels are
/// coplanar to within 1e-5, breeds slivers whose visibility is noise, and the surface tears apart.</para>
/// <para>The result is the same solid as the original's Qhull call, containing all of its vertices with the same
/// volume, but a point that ends up on a hull edge or face can stay as a vertex where Qhull drops it, and coplanar
/// regions triangulate differently. A generated ragdoll SMD therefore matches in shape, not triangle for triangle.</para>
/// </remarks>
internal sealed class ConvexHull
{
    /// <summary>How far outside a face, in inches, a point must be to count. Model data is rounded to 1e-5.</summary>
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

    private sealed class Face
    {
        public Face(V3[] points, int a, int b, int c)
        {
            (A, B, C) = (a, b, c);
            var normal = (points[b] - points[a]).Cross(points[c] - points[a]);
            var length = normal.Length;
            Normal = length > 0 ? normal / length : V3.Zero;
            Origin = points[a];
        }

        public int A { get; }
        public int B { get; }
        public int C { get; }
        public V3 Normal { get; }
        public V3 Origin { get; }
        public List<int> Outside { get; } = [];
        public bool Removed { get; set; }

        public double Distance(V3 point) => Normal.Dot(point - Origin);
        public (int, int)[] Edges => [(A, B), (B, C), (C, A)];
    }

    private static (int, int, int, int)? Seed(V3[] points)
    {
        var second = -1;
        var best = 0.0;
        for (var index = 1; index < points.Length; index++)
        {
            var distance = (points[index] - points[0]).Length;
            if (distance <= best) continue;
            best = distance;
            second = index;
        }
        if (second < 0) return null;
        var third = -1;
        best = Tolerance;
        var axis = points[second] - points[0];
        for (var index = 0; index < points.Length; index++)
        {
            var area = axis.Cross(points[index] - points[0]).Length;
            if (area <= best) continue;
            best = area;
            third = index;
        }
        if (third < 0) return null;
        var plane = new Face(points, 0, second, third);
        var fourth = -1;
        best = Tolerance;
        for (var index = 0; index < points.Length; index++)
        {
            var height = Math.Abs(plane.Distance(points[index]));
            if (height <= best) continue;
            best = height;
            fourth = index;
        }
        return fourth < 0 ? null : (0, second, third, fourth);
    }

    /// <summary>Gives each point to the first face it lies outside, dropping points inside all of them.</summary>
    private static void Assign(V3[] points, IEnumerable<int> candidates, List<Face> faces)
    {
        foreach (var point in candidates)
            foreach (var face in faces)
            {
                if (face.Distance(points[point]) <= Tolerance) continue;
                face.Outside.Add(point);
                break;
            }
    }

    /// <summary>The faces an outside point can see, flooded across edges from one it is known to see.</summary>
    /// <returns>The visible faces, and the directed edges bounding them.</returns>
    private static (List<Face> Visible, List<(int From, int To)> Horizon) Flood(Dictionary<(int, int), Face> byEdge, Face start, V3 point)
    {
        var visible = new List<Face> { start };
        var seen = new HashSet<Face> { start };
        var horizon = new List<(int, int)>();
        for (var index = 0; index < visible.Count; index++)
            foreach (var (from, to) in visible[index].Edges)
            {
                var neighbour = byEdge[(to, from)];
                if (seen.Contains(neighbour)) continue;
                if (neighbour.Distance(point) > Tolerance)
                {
                    seen.Add(neighbour);
                    visible.Add(neighbour);
                }
                else horizon.Add((from, to));
            }
        return (visible, horizon);
    }

    /// <summary>Builds the hull of a point cloud, or fails when the cloud has no volume.</summary>
    public static ConvexHull Build(IReadOnlyList<V3> cloud)
    {
        var points = cloud.ToArray();
        if (Seed(points) is not var (a, b, c, d)) throw new InvalidDataException("Convex hull needs four points that are not coplanar.");
        var centre = (points[a] + points[b] + points[c] + points[d]) / 4.0;
        Face Outward(int x, int y, int z)
        {
            var face = new Face(points, x, y, z);
            return face.Distance(centre) > 0 ? new(points, x, z, y) : face;
        }
        var faces = new List<Face> { Outward(a, b, c), Outward(a, b, d), Outward(a, c, d), Outward(b, c, d) };
        var byEdge = new Dictionary<(int, int), Face>();
        foreach (var face in faces)
            foreach (var edge in face.Edges) byEdge[edge] = face;
        Assign(points, Enumerable.Range(0, points.Length), faces);
        var pending = new Stack<Face>(faces);
        while (pending.Count > 0)
        {
            var face = pending.Pop();
            if (face.Removed || face.Outside.Count == 0) continue;
            var farthest = face.Outside.MaxBy(point => face.Distance(points[point]));
            var (visible, horizon) = Flood(byEdge, face, points[farthest]);
            var orphans = visible.SelectMany(item => item.Outside).Where(point => point != farthest).ToList();
            foreach (var item in visible)
            {
                item.Removed = true;
                foreach (var edge in item.Edges) byEdge.Remove(edge);
            }
            var created = horizon.ConvertAll(edge => new Face(points, edge.From, edge.To, farthest));
            foreach (var item in created)
                foreach (var edge in item.Edges) byEdge[edge] = item;
            Assign(points, orphans, created);
            faces.AddRange(created);
            foreach (var item in created) pending.Push(item);
        }
        var remap = new Dictionary<int, int>();
        var vertices = new List<V3>();
        var indices = new List<int>();
        void Map(int index)
        {
            if (!remap.TryGetValue(index, out var mapped))
            {
                remap[index] = mapped = vertices.Count;
                vertices.Add(points[index]);
            }
            indices.Add(mapped);
        }
        foreach (var face in faces.Where(item => !item.Removed))
        {
            Map(face.A);
            Map(face.B);
            Map(face.C);
        }
        return new(vertices.ToArray(), indices.ToArray());
    }
}
