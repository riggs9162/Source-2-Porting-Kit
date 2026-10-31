namespace PortingKit.Addons.Porting;

/// <summary>Orients faces the way the sanitize pass does before a static SMD is written.</summary>
/// <remarks>
/// This is not cosmetic. Half-Life Alyx meshes are almost never watertight as a whole, but they often hold
/// closed sub-shells, and a closed shell wound inward has a negative volume, so its faces get reversed.
/// That rewrites the face corner order in the SMD and turns the derived normals of that shell around.
/// On headcrab_classic_gib02 it reverses 412 of 482 faces.
/// </remarks>
internal static class MeshRepair
{
    /// <summary>Every edge used by exactly two faces, sorted by larger vertex then smaller vertex.</summary>
    /// <remarks>The sort order is load-bearing: it fixes the insertion order of the face graph, which decides
    /// where each traversal starts and how far a flip spreads through an inconsistently wound mesh.</remarks>
    private static List<(int First, int Second)> PairedEdges(int[] faces, IReadOnlyList<int>? subset = null)
    {
        var count = subset?.Count ?? faces.Length / 3;
        var rows = new (long Key, int Row)[count * 3];
        for (var index = 0; index < count; index++)
        {
            var face = subset is null ? index : subset[index];
            for (var corner = 0; corner < 3; corner++)
            {
                int a = faces[face * 3 + corner], b = faces[face * 3 + (corner + 1) % 3];
                rows[index * 3 + corner] = (((long)Math.Max(a, b) << 32) | (uint)Math.Min(a, b), index * 3 + corner);
            }
        }
        Array.Sort(rows, (left, right) => left.Key != right.Key ? left.Key.CompareTo(right.Key) : left.Row.CompareTo(right.Row));
        var pairs = new List<(int, int)>();
        for (var start = 0; start < rows.Length;)
        {
            var end = start + 1;
            while (end < rows.Length && rows[end].Key == rows[start].Key) end++;
            if (end - start == 2) pairs.Add((rows[start].Row, rows[start + 1].Row));
            start = end;
        }
        return pairs;
    }

    private static (int From, int To) Edge(int[] faces, int row, IReadOnlyList<int>? subset)
    {
        var face = subset is null ? row / 3 : subset[row / 3];
        var corner = row % 3;
        return (faces[face * 3 + corner], faces[face * 3 + (corner + 1) % 3]);
    }

    /// <summary>Whether every edge appears exactly twice, and whether those pairs run in opposite directions.</summary>
    private static (bool Watertight, bool Consistent) Inspect(int[] faces, IReadOnlyList<int>? subset = null)
    {
        var pairs = PairedEdges(faces, subset);
        var consistent = true;
        foreach (var (first, second) in pairs)
            if (Edge(faces, first, subset).To != Edge(faces, second, subset).From)
            {
                consistent = false;
                break;
            }
        var edges = (subset?.Count ?? faces.Length / 3) * 3;
        return (pairs.Count * 2 == edges, consistent);
    }

    /// <summary>Pairs of faces sharing an edge, each pair sorted, in shared-edge order.</summary>
    private static List<(int Low, int High)> FaceAdjacency(int[] faces)
    {
        var result = new List<(int, int)>();
        foreach (var (first, second) in PairedEdges(faces))
        {
            int a = first / 3, b = second / 3;
            if (a != b) result.Add(a < b ? (a, b) : (b, a));
        }
        return result;
    }

    /// <summary>How many separate groups of connected vertices the mesh holds, counting isolated vertices.</summary>
    private static int BodyCount(ModelMesh mesh)
    {
        var parent = Enumerable.Range(0, mesh.Vertices.Length).ToArray();
        int Find(int node)
        {
            while (parent[node] != node) node = parent[node] = parent[parent[node]];
            return node;
        }
        for (var index = 0; index < mesh.Faces.Length; index += 3)
        {
            int a = mesh.Faces[index], b = mesh.Faces[index + 1], c = mesh.Faces[index + 2];
            parent[Find(a)] = Find(b);
            parent[Find(b)] = Find(c);
        }
        var bodies = 0;
        for (var node = 0; node < parent.Length; node++)
            if (Find(node) == node) bodies++;
        return bodies;
    }

    /// <summary>The graph of adjacent faces, with nodes and neighbours in first-seen order.</summary>
    private sealed class FaceGraph
    {
        private readonly Dictionary<int, List<int>> neighbours = [];

        public FaceGraph(List<(int Low, int High)> adjacency)
        {
            foreach (var (low, high) in adjacency)
            {
                Link(low, high);
                Link(high, low);
            }
        }

        public List<int> Nodes { get; } = [];

        private void Link(int from, int to)
        {
            if (!neighbours.TryGetValue(from, out var list))
            {
                neighbours[from] = list = [];
                Nodes.Add(from);
            }
            if (!list.Contains(to)) list.Add(to);
        }

        public List<int> Neighbours(int node) => neighbours[node];

        /// <summary>Components as sets of nodes, in the order their first node was seen.</summary>
        /// <remarks>Each set grows breadth first and stops as soon as it has as many nodes as remain unclaimed.
        /// The insertion history is kept because the table layout of those sets picks the traversal start.</remarks>
        public IEnumerable<List<int>> Components()
        {
            var claimed = new HashSet<int>();
            foreach (var source in Nodes)
            {
                if (claimed.Contains(source)) continue;
                var remaining = Nodes.Count - claimed.Count;
                var inserted = new List<int> { source };
                var seen = new HashSet<int> { source };
                var level = new List<int> { source };
                while (level.Count > 0 && seen.Count != remaining)
                {
                    var next = new List<int>();
                    foreach (var node in level)
                    {
                        foreach (var other in Neighbours(node))
                            if (seen.Add(other))
                            {
                                inserted.Add(other);
                                next.Add(other);
                            }
                        if (seen.Count == remaining) break;
                    }
                    level = next;
                }
                claimed.UnionWith(seen);
                yield return inserted;
            }
        }

        /// <summary>Picks the face a traversal starts from.</summary>
        /// <remarks>The original asks a filtered graph view for its first node. The view walks the graph's own
        /// order unless the component is under half the graph, when it walks the component's Python set, whose
        /// order comes from hash slots. Which face starts decides which side of a badly wound patch keeps its
        /// winding, so the set's order is rebuilt rather than approximated. The view holds a copy of the component's
        /// set, which is why the order is applied twice.</remarks>
        public int Start(List<int> component)
        {
            if (2 * component.Count >= Nodes.Count)
            {
                var members = new HashSet<int>(component);
                return Nodes.First(members.Contains);
            }
            return PythonIntSet.Order(PythonIntSet.Order(component))[0];
        }
    }

    /// <summary>Walks each component breadth first, reversing faces whose shared edge runs the same way as their parent's.</summary>
    private static void FixWinding(ModelMesh mesh)
    {
        if (mesh.Vertices.Length + mesh.Faces.Length > 0 && Inspect(mesh.Faces).Consistent) return;
        var graph = new FaceGraph(FaceAdjacency(mesh.Faces));
        var faces = (int[])mesh.Faces.Clone();
        var flipped = 0;
        foreach (var component in graph.Components())
        {
            var start = graph.Start(component);
            var seen = new HashSet<int> { start };
            var total = component.Count;
            var queue = new Queue<int>();
            queue.Enqueue(start);
            while (queue.Count > 0 && seen.Count < total)
            {
                var parent = queue.Dequeue();
                foreach (var child in graph.Neighbours(parent))
                {
                    if (!seen.Add(child)) continue;
                    queue.Enqueue(child);
                    if (!SharedEdgeAgrees(faces, parent, child)) continue;
                    flipped++;
                    Flip(faces, child);
                }
            }
        }
        if (flipped > 0) mesh.Faces = faces;
    }

    /// <summary>Whether the first edge two faces share in exactly two rows runs in the same direction in both.</summary>
    private static bool SharedEdgeAgrees(int[] faces, int first, int second)
    {
        Span<(int From, int To)> edges = stackalloc (int, int)[6];
        for (var corner = 0; corner < 3; corner++)
        {
            edges[corner] = (faces[first * 3 + corner], faces[first * 3 + (corner + 1) % 3]);
            edges[corner + 3] = (faces[second * 3 + corner], faces[second * 3 + (corner + 1) % 3]);
        }
        var best = long.MaxValue;
        int bestFirst = -1, bestSecond = -1;
        for (var row = 0; row < 6; row++)
        {
            var key = Key(edges[row]);
            var matches = 0;
            var other = -1;
            for (var candidate = 0; candidate < 6; candidate++)
            {
                if (candidate == row || Key(edges[candidate]) != key) continue;
                matches++;
                other = candidate;
            }
            if (matches != 1 || key >= best) continue;
            best = key;
            (bestFirst, bestSecond) = (Math.Min(row, other), Math.Max(row, other));
        }
        return bestFirst >= 0 && edges[bestFirst].From == edges[bestSecond].From;
    }

    private static long Key((int From, int To) edge) => ((long)Math.Max(edge.From, edge.To) << 32) | (uint)Math.Min(edge.From, edge.To);

    /// <summary>Signed volume of a set of faces by the divergence theorem, in the original's x-axis form.</summary>
    private static double Volume(ModelMesh mesh, IEnumerable<int> faces)
    {
        var sum = 0.0;
        foreach (var face in faces)
        {
            V3 a = mesh.Vertices[mesh.Faces[face * 3]], b = mesh.Vertices[mesh.Faces[face * 3 + 1]], c = mesh.Vertices[mesh.Faces[face * 3 + 2]];
            sum += (b - a).Cross(c - b).X * (a.X + b.X + c.X);
        }
        return sum / 6.0;
    }

    private static void Flip(int[] faces, int face) => (faces[face * 3], faces[face * 3 + 2]) = (faces[face * 3 + 2], faces[face * 3]);

    /// <summary>Reverses inward-facing closed shells.</summary>
    /// <remarks>With one body the whole mesh must be watertight. With several, each connected patch of at
    /// least four faces is judged alone, and only a closed, consistently wound one is touched.</remarks>
    private static void FixInversion(ModelMesh mesh, bool multibody)
    {
        if (!multibody)
        {
            if (!Inspect(mesh.Faces).Watertight || Volume(mesh, Enumerable.Range(0, mesh.FaceCount)) >= 0.0) return;
            for (var face = 0; face < mesh.FaceCount; face++) Flip(mesh.Faces, face);
            return;
        }
        var flip = new List<int>();
        foreach (var group in Groups(FaceAdjacency(mesh.Faces)))
        {
            if (group.Count < 4) continue;
            var (tight, wound) = Inspect(mesh.Faces, group);
            if (tight && wound && Volume(mesh, group) < 0.0) flip.AddRange(group);
        }
        foreach (var face in flip) Flip(mesh.Faces, face);
    }

    private static List<List<int>> Groups(List<(int Low, int High)> adjacency)
    {
        var parent = new Dictionary<int, int>();
        int Find(int node)
        {
            if (!parent.TryGetValue(node, out var above))
            {
                parent[node] = node;
                return node;
            }
            while (above != node)
            {
                var top = parent[above];
                parent[node] = top;
                node = above;
                above = top;
            }
            return node;
        }
        foreach (var (low, high) in adjacency) parent[Find(low)] = Find(high);
        var groups = new Dictionary<int, List<int>>();
        foreach (var node in parent.Keys.ToList())
        {
            var root = Find(node);
            if (!groups.TryGetValue(root, out var members)) groups[root] = members = [];
            members.Add(node);
        }
        return groups.Values.ToList();
    }

    /// <summary>Runs the winding pass, then the inversion pass, in the original's order.</summary>
    public static void FixNormals(ModelMesh mesh)
    {
        var multibody = BodyCount(mesh) > 1;
        FixWinding(mesh);
        FixInversion(mesh, multibody);
    }
}

/// <summary>Rebuilds the iteration order of a CPython set of small non-negative integers.</summary>
/// <remarks>Only the first element is ever needed, but that is decided by the whole insertion history,
/// including table growth and probe collisions, so the insert path is reproduced exactly.</remarks>
internal static class PythonIntSet
{
    private const int LinearProbes = 9;
    private const int PerturbShift = 5;
    private const int MinimumSize = 8;

    private sealed class Table(int size)
    {
        public long[] Slots { get; } = Enumerable.Repeat(-1L, size).ToArray();
        public int Used { get; set; }
        public int Mask => Slots.Length - 1;

        public void Insert(long value)
        {
            var perturb = (ulong)value;
            var index = (int)((ulong)value & (ulong)Mask);
            while (true)
            {
                var probes = index + LinearProbes <= Mask ? LinearProbes : 0;
                for (var at = index; at <= index + probes; at++)
                {
                    if (Slots[at] == value) return;
                    if (Slots[at] >= 0) continue;
                    Slots[at] = value;
                    Used++;
                    return;
                }
                perturb >>= PerturbShift;
                index = (int)(((ulong)index * 5 + 1 + perturb) & (ulong)Mask);
            }
        }

        public void InsertClean(long value)
        {
            var perturb = (ulong)value;
            var index = (int)((ulong)value & (ulong)Mask);
            while (true)
            {
                var probes = index + LinearProbes <= Mask ? LinearProbes : 0;
                for (var at = index; at <= index + probes; at++)
                {
                    if (Slots[at] >= 0) continue;
                    Slots[at] = value;
                    Used++;
                    return;
                }
                perturb >>= PerturbShift;
                index = (int)(((ulong)index * 5 + 1 + perturb) & (ulong)Mask);
            }
        }
    }

    private static Table Resized(Table table, int minimum)
    {
        var size = MinimumSize;
        while (size <= minimum) size <<= 1;
        var next = new Table(size);
        foreach (var value in table.Slots)
            if (value >= 0) next.InsertClean(value);
        return next;
    }

    private static Table Add(Table table, long value)
    {
        var before = table.Used;
        table.Insert(value);
        if (table.Used == before || (long)table.Used * 5 < (long)table.Mask * 3) return table;
        return Resized(table, table.Used > 50000 ? table.Used * 2 : table.Used * 4);
    }

    private static Table Build(IEnumerable<int> values)
    {
        var table = new Table(MinimumSize);
        foreach (var value in values) table = Add(table, value);
        return table;
    }

    /// <summary>The order a set built by adding these values, in turn, iterates in.</summary>
    public static List<int> Order(IEnumerable<int> inserted) => Build(inserted).Slots.Where(value => value >= 0).Select(value => (int)value).ToList();
}
