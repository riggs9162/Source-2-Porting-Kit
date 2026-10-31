namespace PortingKit.Addons.Porting;

/// <summary>Triangle mesh measurements and surgery, written to give the numbers trimesh gives.</summary>
/// <remarks>
/// Collision measurements decide cuts and masses, so these keep trimesh's exact formulas: a cross product of
/// t1-t0 and t2-t1, the 1e-13 zero-normal threshold, and first-occurrence vertex merging on rounded coordinates.
/// </remarks>
public static class MeshOps
{
    /// <summary>The cross product magnitude below which trimesh calls a face degenerate.</summary>
    private const double ZeroNormal = 1e-13;

    /// <summary>The decimal places trimesh quantises to when no weld distance is given.</summary>
    public const int DefaultMergeDigits = 8;

    /// <summary>Rounds half to even, which is what a NumPy rint does.</summary>
    public static double Rint(double value) => Math.Round(value, MidpointRounding.ToEven);

    /// <summary>Rounds to decimal places the way NumPy does: scale, rint, unscale.</summary>
    public static double RoundTo(double value, int decimals)
    {
        var scale = Math.Pow(10.0, decimals);
        return Rint(value * scale) / scale;
    }

    /// <summary>Sorts rows lexicographically and drops exact duplicates, as np.unique on rows does.</summary>
    public static V3[] UniqueRows(IEnumerable<V3> points)
    {
        var sorted = points.ToArray();
        Array.Sort(sorted, CompareRows);
        var write = 0;
        for (var read = 0; read < sorted.Length; read++)
            if (write == 0 || CompareRows(sorted[write - 1], sorted[read]) != 0)
                sorted[write++] = sorted[read];
        Array.Resize(ref sorted, write);
        return sorted;
    }

    /// <summary>Lexicographic comparison by value, so that negative and positive zero tie as they do in NumPy.</summary>
    public static int CompareRows(V3 a, V3 b)
    {
        if (a.X != b.X) return a.X < b.X ? -1 : 1;
        if (a.Y != b.Y) return a.Y < b.Y ? -1 : 1;
        if (a.Z != b.Z) return a.Z < b.Z ? -1 : 1;
        return 0;
    }

    /// <summary>Whether a mesh has any vertices that matter.</summary>
    public static bool IsEmpty(TriMesh? mesh) => mesh is null || mesh.FaceCount == 0;

    /// <summary>Merges vertices that round to the same grid position, keeping the first and dropping any unused.</summary>
    /// <remarks>Rounds rather than measures distance, so two points a hair apart can still straddle a cell boundary.
    /// That is what trimesh does, and what the rest of the collision pipeline was measured against.</remarks>
    public static TriMesh MergeVertices(TriMesh mesh, int digits = DefaultMergeDigits)
    {
        if (mesh.Vertices.Length == 0) return mesh;
        var referenced = new bool[mesh.Vertices.Length];
        foreach (var index in mesh.Faces) referenced[index] = true;
        if (mesh.Faces.Length == 0) Array.Fill(referenced, true);
        var scale = Math.Pow(10.0, digits);
        var seen = new Dictionary<(long, long, long), int>();
        var inverse = new int[mesh.Vertices.Length];
        var kept = new List<V3>();
        for (var index = 0; index < mesh.Vertices.Length; index++)
        {
            if (!referenced[index]) continue;
            var vertex = mesh.Vertices[index];
            var key = ((long)Rint(vertex.X * scale), (long)Rint(vertex.Y * scale), (long)Rint(vertex.Z * scale));
            if (!seen.TryGetValue(key, out var target))
            {
                target = kept.Count;
                seen[key] = target;
                kept.Add(vertex);
            }
            inverse[index] = target;
        }
        return new(kept.ToArray(), Array.ConvertAll(mesh.Faces, index => inverse[index]));
    }

    /// <summary>Connected-component label per vertex, over the graph the faces induce.</summary>
    /// <remarks>Union-find over the deduplicated triangle edges in sorted order. Deduplicating first is what keeps
    /// it quick, and the sorted order fixes which root wins, which in turn fixes the order elements come back in.</remarks>
    public static int[] VertexGroups(int[] faces, int vertexCount)
    {
        var parent = new int[Math.Max(vertexCount, 1)];
        for (var index = 0; index < parent.Length; index++) parent[index] = index;
        int Find(int node)
        {
            var root = node;
            while (parent[root] != root) root = parent[root];
            while (parent[node] != root)
            {
                var next = parent[node];
                parent[node] = root;
                node = next;
            }
            return root;
        }
        var edges = new long[faces.Length];
        for (var face = 0; face < faces.Length / 3; face++)
            for (var corner = 0; corner < 3; corner++)
            {
                int first = faces[face * 3 + corner], second = faces[face * 3 + (corner + 1) % 3];
                edges[face * 3 + corner] = (long)Math.Min(first, second) * parent.Length + Math.Max(first, second);
            }
        Array.Sort(edges);
        for (var at = 0; at < edges.Length; at++)
        {
            if (at > 0 && edges[at] == edges[at - 1]) continue;
            int left = Find((int)(edges[at] / parent.Length)), right = Find((int)(edges[at] % parent.Length));
            if (left != right) parent[left] = right;
        }
        var labels = new int[vertexCount];
        for (var index = 0; index < vertexCount; index++) labels[index] = Find(index);
        return labels;
    }

    /// <summary>Copies the selected faces with only the vertices they use, in their original order.</summary>
    public static TriMesh Submesh(TriMesh mesh, IReadOnlyList<int> faces)
    {
        var used = new SortedSet<int>();
        foreach (var face in faces)
            for (var corner = 0; corner < 3; corner++) used.Add(mesh.Faces[face * 3 + corner]);
        var remap = new Dictionary<int, int>(used.Count);
        var vertices = new V3[used.Count];
        var next = 0;
        foreach (var vertex in used)
        {
            remap[vertex] = next;
            vertices[next++] = mesh.Vertices[vertex];
        }
        var indices = new int[faces.Count * 3];
        for (var at = 0; at < faces.Count; at++)
            for (var corner = 0; corner < 3; corner++)
                indices[at * 3 + corner] = remap[mesh.Faces[faces[at] * 3 + corner]];
        return new(vertices, indices);
    }

    /// <summary>The connected elements of a collision mesh, the way studiomdl groups them.</summary>
    /// <remarks>
    /// Two triangles belong to the same element when they share a vertex, which is the rule collisionmodel.cpp
    /// applies and not the rule trimesh's split applies. That one walks face adjacency, and an edge used by more
    /// than two faces drops out of it entirely. Source 2 collision is full of interpenetrating boxes that do
    /// exactly that: the combine printer's 2,902-triangle collision comes apart into 1,156 fragments that way,
    /// against 121 elements here and the 118 studiomdl builds. Holes are left alone as well, because capping the
    /// reveal around a doorway invents a solid slab that then survives decomposition, since a box is already convex.
    /// </remarks>
    public static List<TriMesh> SplitElements(TriMesh mesh)
    {
        if (mesh.FaceCount == 0) return [];
        var labels = VertexGroups(mesh.Faces, mesh.Vertices.Length);
        var groups = new SortedDictionary<int, List<int>>();
        for (var face = 0; face < mesh.FaceCount; face++)
        {
            var label = labels[mesh.Faces[face * 3]];
            if (!groups.TryGetValue(label, out var members)) groups[label] = members = [];
            members.Add(face);
        }
        return groups.Count <= 1 ? [mesh] : groups.Values.Select(group => Submesh(mesh, group)).ToList();
    }

    /// <summary>Cross product of each triangle's two leading edges.</summary>
    public static V3 FaceCross(TriMesh mesh, int face)
    {
        V3 a = mesh.Vertices[mesh.Faces[face * 3]], b = mesh.Vertices[mesh.Faces[face * 3 + 1]], c = mesh.Vertices[mesh.Faces[face * 3 + 2]];
        return (b - a).Cross(c - b);
    }

    /// <summary>Unit normal per face, with zero for a face too small to have one.</summary>
    public static V3[] FaceNormals(TriMesh mesh)
    {
        var normals = new V3[mesh.FaceCount];
        for (var face = 0; face < normals.Length; face++)
        {
            var cross = FaceCross(mesh, face);
            var norm = Math.Sqrt(cross.X * cross.X + cross.Y * cross.Y + cross.Z * cross.Z);
            normals[face] = norm > ZeroNormal ? cross * (1.0 / norm) : V3.Zero;
        }
        return normals;
    }

    /// <summary>The barycentre of each face.</summary>
    public static V3[] FaceCentres(TriMesh mesh)
    {
        var centres = new V3[mesh.FaceCount];
        for (var face = 0; face < centres.Length; face++)
            centres[face] = (mesh.Vertices[mesh.Faces[face * 3]] + mesh.Vertices[mesh.Faces[face * 3 + 1]] + mesh.Vertices[mesh.Faces[face * 3 + 2]]) / 3.0;
        return centres;
    }

    /// <summary>Summed triangle area.</summary>
    public static double Area(TriMesh mesh)
    {
        var total = 0.0;
        for (var face = 0; face < mesh.FaceCount; face++)
        {
            var cross = FaceCross(mesh, face);
            total += Math.Sqrt(cross.X * cross.X + cross.Y * cross.Y + cross.Z * cross.Z) / 2.0;
        }
        return total;
    }

    /// <summary>Signed volume by the divergence theorem. Only means anything on a closed mesh.</summary>
    public static double Volume(TriMesh mesh)
    {
        var total = 0.0;
        for (var face = 0; face < mesh.FaceCount; face++)
        {
            var sum = mesh.Vertices[mesh.Faces[face * 3]].X + mesh.Vertices[mesh.Faces[face * 3 + 1]].X + mesh.Vertices[mesh.Faces[face * 3 + 2]].X;
            total += FaceCross(mesh, face).X * sum;
        }
        return total / 6.0;
    }

    /// <summary>The bounding box of the vertices faces actually use, or of all of them for a bare cloud.</summary>
    public static (V3 Low, V3 High) Bounds(TriMesh mesh)
    {
        IEnumerable<V3> points = mesh.Faces.Length > 0 ? mesh.Faces.Select(index => mesh.Vertices[index]) : mesh.Vertices;
        return Bounds(points);
    }

    /// <summary>The bounding box of a point cloud.</summary>
    public static (V3 Low, V3 High) Bounds(IEnumerable<V3> points)
    {
        double lowX = double.PositiveInfinity, lowY = lowX, lowZ = lowX, highX = double.NegativeInfinity, highY = highX, highZ = highX;
        foreach (var point in points)
        {
            lowX = Math.Min(lowX, point.X);
            lowY = Math.Min(lowY, point.Y);
            lowZ = Math.Min(lowZ, point.Z);
            highX = Math.Max(highX, point.X);
            highY = Math.Max(highY, point.Y);
            highZ = Math.Max(highZ, point.Z);
        }
        return (new(lowX, lowY, lowZ), new(highX, highY, highZ));
    }

    /// <summary>Whether every edge is used by exactly two faces.</summary>
    public static bool IsWatertight(TriMesh mesh)
    {
        if (mesh.FaceCount == 0) return false;
        var counts = new Dictionary<long, int>(mesh.Faces.Length);
        long stride = mesh.Vertices.Length + 1L;
        for (var face = 0; face < mesh.FaceCount; face++)
            for (var corner = 0; corner < 3; corner++)
            {
                int first = mesh.Faces[face * 3 + corner], second = mesh.Faces[face * 3 + (corner + 1) % 3];
                var key = Math.Min(first, second) * stride + Math.Max(first, second);
                counts[key] = counts.GetValueOrDefault(key) + 1;
            }
        return counts.Values.All(count => count == 2);
    }

    /// <summary>Applies a rigid 3x4 transform to every vertex, keeping the faces.</summary>
    public static TriMesh Transform(TriMesh mesh, M4 transform) => new(Array.ConvertAll(mesh.Vertices, transform.Point), mesh.Faces);

    /// <summary>Clips a triangle soup against one axis-aligned plane.</summary>
    /// <remarks>Sutherland-Hodgman over the three cases that survive: a triangle wholly inside passes through, one
    /// vertex inside becomes a smaller triangle, and two inside become a quad cut in two. It asks nothing of the
    /// topology, which is the point, because a physics mesh cut this way is rarely closed. Each triangle occupies
    /// three consecutive entries.</remarks>
    public static V3[] ClipTriangles(V3[] triangles, int axis, double position, bool keepBelow)
    {
        var whole = new List<V3>();
        var single = new List<V3>();
        var first = new List<V3>();
        var second = new List<V3>();
        var distance = new double[3];
        var inside = new bool[3];
        for (var triangle = 0; triangle < triangles.Length; triangle += 3)
        {
            var count = 0;
            for (var corner = 0; corner < 3; corner++)
            {
                distance[corner] = triangles[triangle + corner][axis] - position;
                if (!keepBelow) distance[corner] = -distance[corner];
                inside[corner] = distance[corner] <= 0.0;
                if (inside[corner]) count++;
            }
            if (count == 3)
            {
                whole.Add(triangles[triangle]);
                whole.Add(triangles[triangle + 1]);
                whole.Add(triangles[triangle + 2]);
            }
            else if (count == 1)
            {
                var start = inside[0] ? 0 : inside[1] ? 1 : 2;
                var (keep, lostA, lostB) = (triangles[triangle + start], triangles[triangle + (start + 1) % 3], triangles[triangle + (start + 2) % 3]);
                double near = distance[start], farA = distance[(start + 1) % 3], farB = distance[(start + 2) % 3];
                single.Add(keep);
                single.Add(Crossing(keep, lostA, near, farA));
                single.Add(Crossing(keep, lostB, near, farB));
            }
            else if (count == 2)
            {
                var start = !inside[0] ? 0 : !inside[1] ? 1 : 2;
                var (lost, keepA, keepB) = (triangles[triangle + start], triangles[triangle + (start + 1) % 3], triangles[triangle + (start + 2) % 3]);
                double near = distance[start], farA = distance[(start + 1) % 3], farB = distance[(start + 2) % 3];
                var cutA = Crossing(lost, keepA, near, farA);
                var cutB = Crossing(lost, keepB, near, farB);
                first.Add(cutA);
                first.Add(keepA);
                first.Add(keepB);
                second.Add(cutA);
                second.Add(keepB);
                second.Add(cutB);
            }
        }
        whole.AddRange(single);
        whole.AddRange(first);
        whole.AddRange(second);
        return whole.ToArray();
    }

    private static V3 Crossing(V3 a, V3 b, double near, double far) => a + (b - a) * (near / (near - far));
}
