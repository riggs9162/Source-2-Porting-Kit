namespace PortingKit.Addons.Porting;

/// <summary>Quickhull in double precision, standing in for the qhull build behind trimesh.</summary>
/// <remarks>
/// <para>Reproduces <c>ConvexHull(points, "QbB Pp Qt")</c> as trimesh wraps it. Each axis is scaled into a unit box
/// first, so a sliver 0.0008 inches thick and twelve wide is hulled at full precision, and the tolerances are
/// qhull's own for that box: a point counts as outside a facet above 4.4e-15 and sees one above 2.2e-15. Coordinates
/// come back unscaled, in input order with hull-only indices, then merged on the 1e-8 grid as trimesh's mesh
/// constructor does.</para>
/// <para>A new facet that lies in the plane of the facet across its horizon edge shares that facet's plane instead
/// of computing its own. Qhull merges such facets; sharing the plane gives the same effect, keeping a sliver in a
/// large flat side from tilting on rounding noise. Whatever triangulates a coplanar side is arbitrary, so diagonals
/// and facet order can differ from qhull's. Vertices, normals, volume and area are the part downstream code reads,
/// and studiomdl rebuilds the triangles of every element for itself anyway.</para>
/// <para>Flat input has no hull. Qhull refuses it and trimesh retries with joggling, which produces a zero-volume
/// mesh that every caller then discards, so null is the same answer.</para>
/// </remarks>
public static class ConvexHull
{
    private const double VisibleDistance = 2.2e-15;
    private const double OutsideDistance = 4.4e-15;
    private const double FlatDistance = 3e-15;
    private const double SmallestVolume = 1e-9;

    /// <summary>The convex hull of a cloud, or null when it spans no volume.</summary>
    public static TriMesh? Of(IReadOnlyList<V3> points)
    {
        var active = new bool[points.Count];
        Array.Fill(active, true);
        while (true)
        {
            var solver = Solver.Create(points, active);
            if (solver is null || !solver.Run()) return null;
            var redundant = solver.RedundantVertices();
            if (redundant.Count == 0) return MeshOps.MergeVertices(solver.ToMesh(points));
            foreach (var vertex in redundant) active[vertex] = false;
        }
    }

    /// <summary>The solid studiomdl would build around these points, or null if they are flat.</summary>
    /// <remarks>Duplicates are removed and the rest sorted first, so hull vertices come back in lexicographic order.
    /// Compiled audits later pair hull vertices by that order, so it is part of the contract.</remarks>
    public static TriMesh? OfDistinct(IEnumerable<V3> points)
    {
        var distinct = MeshOps.UniqueRows(points);
        if (distinct.Length < 4 || !distinct.All(point => point.IsFinite)) return null;
        var hull = Of(distinct);
        return hull is null || hull.FaceCount == 0 || Math.Abs(MeshOps.Volume(hull)) <= SmallestVolume ? null : hull;
    }

    private sealed class Plane(V3 normal, V3 origin)
    {
        public double Distance(V3 point) =>
            normal.X * (point.X - origin.X) + normal.Y * (point.Y - origin.Y) + normal.Z * (point.Z - origin.Z);
    }

    private sealed class Facet(int a, int b, int c, Plane plane)
    {
        public readonly int[] Corners = [a, b, c];
        public readonly Facet?[] Neighbours = new Facet?[3];
        public readonly Plane Plane = plane;
        public List<int>? Outside;
        public int Furthest = -1;
        public double FurthestDistance;
        public bool Alive = true;
        public int Stamp;

        public double Distance(V3 point) => Plane.Distance(point);

        public void Offer(int point, double distance)
        {
            (Outside ??= []).Add(point);
            if (Furthest >= 0 && distance <= FurthestDistance) return;
            Furthest = point;
            FurthestDistance = distance;
        }
    }

    private readonly record struct HorizonEdge(int From, int To, Facet Inside, Facet Outside);

    private sealed class Solver
    {
        private readonly V3[] scaled;
        private readonly List<Facet> facets = [];
        private readonly Queue<Facet> pending = new();
        private readonly int[] active;
        private int stamp;

        private Solver(V3[] scaled, int[] active)
        {
            this.scaled = scaled;
            this.active = active;
        }

        public static Solver? Create(IReadOnlyList<V3> points, bool[] enabled)
        {
            var low = new[] { double.PositiveInfinity, double.PositiveInfinity, double.PositiveInfinity };
            var high = new[] { double.NegativeInfinity, double.NegativeInfinity, double.NegativeInfinity };
            var active = new List<int>(points.Count);
            for (var index = 0; index < points.Count; index++)
            {
                if (!enabled[index]) continue;
                active.Add(index);
                for (var axis = 0; axis < 3; axis++)
                {
                    low[axis] = Math.Min(low[axis], points[index][axis]);
                    high[axis] = Math.Max(high[axis], points[index][axis]);
                }
            }
            if (active.Count < 4) return null;
            var scales = new double[3];
            var shifts = new double[3];
            for (var axis = 0; axis < 3; axis++)
            {
                var range = high[axis] - low[axis];
                if (range <= 0.0) return null;
                scales[axis] = 1.0 / range;
                shifts[axis] = (-0.5 * high[axis] - 0.5 * low[axis]) / range;
            }
            var scaled = new V3[points.Count];
            foreach (var index in active)
            {
                var point = points[index];
                scaled[index] = new(
                    Math.Clamp(point.X * scales[0] + shifts[0], -0.5, 0.5),
                    Math.Clamp(point.Y * scales[1] + shifts[1], -0.5, 0.5),
                    Math.Clamp(point.Z * scales[2] + shifts[2], -0.5, 0.5));
            }
            return new(scaled, active.ToArray());
        }

        private static double Product(double a, double b, double c, double d)
        {
            var product = c * d;
            return Math.FusedMultiplyAdd(a, b, -product) + Math.FusedMultiplyAdd(-c, d, product);
        }

        private Plane MakePlane(int a, int b, int c)
        {
            V3 origin = scaled[a], first = scaled[b] - origin, second = scaled[c] - origin;
            return new(new V3(
                Product(first.Y, second.Z, first.Z, second.Y),
                Product(first.Z, second.X, first.X, second.Z),
                Product(first.X, second.Y, first.Y, second.X)).Normalized(), origin);
        }

        private bool InitialSimplex(out int a, out int b, out int c, out int d)
        {
            a = b = c = d = -1;
            var extremes = new List<int>(6);
            for (var axis = 0; axis < 3; axis++)
            {
                int smallest = active[0], largest = active[0];
                foreach (var index in active)
                {
                    if (scaled[index][axis] < scaled[smallest][axis]) smallest = index;
                    if (scaled[index][axis] > scaled[largest][axis]) largest = index;
                }
                extremes.Add(smallest);
                extremes.Add(largest);
            }
            var longest = 0.0;
            foreach (var first in extremes)
                foreach (var second in extremes)
                {
                    var span = scaled[first] - scaled[second];
                    var length = span.Dot(span);
                    if (length <= longest) continue;
                    longest = length;
                    a = first;
                    b = second;
                }
            if (a < 0) return false;
            var direction = (scaled[b] - scaled[a]).Normalized();
            var widest = 0.0;
            foreach (var index in active)
            {
                var offset = scaled[index] - scaled[a];
                var rejection = offset - direction * offset.Dot(direction);
                var length = rejection.Dot(rejection);
                if (length <= widest) continue;
                widest = length;
                c = index;
            }
            if (c < 0) return false;
            var plane = MakePlane(a, b, c);
            var tallest = FlatDistance;
            foreach (var index in active)
            {
                var distance = Math.Abs(plane.Distance(scaled[index]));
                if (distance <= tallest) continue;
                tallest = distance;
                d = index;
            }
            if (d < 0) return false;
            if (plane.Distance(scaled[d]) > 0.0) (b, c) = (c, b);
            return true;
        }

        private static void Link(Facet facet, int edge, Facet neighbour) => facet.Neighbours[edge] = neighbour;

        private void Enqueue(Facet facet)
        {
            if (facet.Outside is { Count: > 0 }) pending.Enqueue(facet);
        }

        public bool Run()
        {
            if (!InitialSimplex(out var a, out var b, out var c, out var d)) return false;
            Facet bottom = new(a, b, c, MakePlane(a, b, c)), first = new(a, d, b, MakePlane(a, d, b));
            Facet second = new(b, d, c, MakePlane(b, d, c)), third = new(c, d, a, MakePlane(c, d, a));
            Link(bottom, 0, first);
            Link(bottom, 1, second);
            Link(bottom, 2, third);
            Link(first, 0, third);
            Link(first, 1, second);
            Link(first, 2, bottom);
            Link(second, 0, first);
            Link(second, 1, third);
            Link(second, 2, bottom);
            Link(third, 0, second);
            Link(third, 1, first);
            Link(third, 2, bottom);
            Facet[] start = [bottom, first, second, third];
            facets.AddRange(start);
            foreach (var point in active)
            {
                if (point == a || point == b || point == c || point == d) continue;
                Assign(point, start);
            }
            foreach (var facet in start) Enqueue(facet);
            var visible = new List<Facet>();
            var horizon = new List<HorizonEdge>();
            while (pending.TryDequeue(out var facet))
            {
                if (!facet.Alive || facet.Outside is not { Count: > 0 }) continue;
                var eye = facet.Furthest;
                CollectVisible(facet, eye, visible);
                if (!FindHorizon(eye, visible, horizon)) return false;
                var created = Extrude(eye, horizon);
                foreach (var dead in visible)
                {
                    dead.Alive = false;
                    if (dead.Outside is null) continue;
                    foreach (var point in dead.Outside)
                        if (point != eye) Assign(point, created);
                    dead.Outside = null;
                }
                foreach (var made in created) Enqueue(made);
            }
            return true;
        }

        private void Assign(int point, IReadOnlyList<Facet> candidates)
        {
            Facet? best = null;
            var bestDistance = OutsideDistance;
            foreach (var facet in candidates)
            {
                var distance = facet.Distance(scaled[point]);
                if (distance <= bestDistance) continue;
                bestDistance = distance;
                best = facet;
            }
            best?.Offer(point, bestDistance);
        }

        private void CollectVisible(Facet start, int eye, List<Facet> visible)
        {
            stamp++;
            visible.Clear();
            visible.Add(start);
            start.Stamp = stamp;
            for (var at = 0; at < visible.Count; at++)
                foreach (var neighbour in visible[at].Neighbours)
                {
                    if (neighbour!.Stamp == stamp) continue;
                    if (neighbour.Distance(scaled[eye]) <= VisibleDistance) continue;
                    neighbour.Stamp = stamp;
                    visible.Add(neighbour);
                }
        }

        private bool FindHorizon(int eye, List<Facet> visible, List<HorizonEdge> horizon)
        {
            while (true)
            {
                horizon.Clear();
                foreach (var facet in visible)
                    for (var edge = 0; edge < 3; edge++)
                    {
                        var neighbour = facet.Neighbours[edge]!;
                        if (neighbour.Stamp != stamp)
                            horizon.Add(new(facet.Corners[edge], facet.Corners[(edge + 1) % 3], facet, neighbour));
                    }
                if (horizon.Count < 3) return false;
                var next = new Dictionary<int, HorizonEdge>(horizon.Count);
                Facet? offender = null;
                foreach (var edge in horizon)
                    if (!next.TryAdd(edge.From, edge)) offender = Worse(offender, edge.Outside, eye);
                if (offender is null)
                {
                    var reached = new HashSet<int>();
                    var cursor = horizon[0];
                    while (reached.Add(cursor.From) && next.TryGetValue(cursor.To, out var following)) cursor = following;
                    if (reached.Count == horizon.Count) return true;
                    foreach (var edge in horizon)
                        if (!reached.Contains(edge.From)) offender = Worse(offender, edge.Outside, eye);
                }
                if (offender is null) return false;
                offender.Stamp = stamp;
                visible.Add(offender);
            }
        }

        private Facet Worse(Facet? current, Facet candidate, int eye) =>
            current is null || candidate.Distance(scaled[eye]) > current.Distance(scaled[eye]) ? candidate : current;

        private List<Facet> Extrude(int eye, List<HorizonEdge> horizon)
        {
            var created = new List<Facet>(horizon.Count);
            var byStart = new Dictionary<int, Facet>(horizon.Count);
            var byEnd = new Dictionary<int, Facet>(horizon.Count);
            foreach (var edge in horizon)
            {
                var inherited = Math.Abs(edge.Outside.Distance(scaled[eye])) <= VisibleDistance;
                Facet facet = new(edge.From, edge.To, eye, inherited ? edge.Outside.Plane : MakePlane(edge.From, edge.To, eye));
                facet.Neighbours[0] = edge.Outside;
                for (var slot = 0; slot < 3; slot++)
                    if (ReferenceEquals(edge.Outside.Neighbours[slot], edge.Inside) &&
                        edge.Outside.Corners[slot] == edge.To && edge.Outside.Corners[(slot + 1) % 3] == edge.From)
                        edge.Outside.Neighbours[slot] = facet;
                byStart[edge.From] = facet;
                byEnd[edge.To] = facet;
                created.Add(facet);
                facets.Add(facet);
            }
            foreach (var facet in created)
            {
                facet.Neighbours[1] = byStart[facet.Corners[1]];
                facet.Neighbours[2] = byEnd[facet.Corners[0]];
            }
            return created;
        }

        private double Size(Facet facet)
        {
            var cross = (scaled[facet.Corners[1]] - scaled[facet.Corners[0]]).Cross(scaled[facet.Corners[2]] - scaled[facet.Corners[0]]);
            return cross.Dot(cross);
        }

        /// <summary>Hull vertices that only lie on a flat side or a straight edge rather than forming a corner.</summary>
        /// <remarks>Quickhull adds a point before it can know a later one will swallow it. Qhull's facet merging
        /// sheds these; here they come out by hulling again without them. Large facets are asked first, because a
        /// sliver through three collinear points has an arbitrary plane that would otherwise pass for a third side.</remarks>
        public List<int> RedundantVertices()
        {
            var incident = new Dictionary<int, List<Facet>>();
            foreach (var facet in facets)
            {
                if (!facet.Alive) continue;
                foreach (var corner in facet.Corners)
                {
                    if (!incident.TryGetValue(corner, out var list)) incident[corner] = list = [];
                    list.Add(facet);
                }
            }
            var redundant = new List<int>();
            var distinct = new List<Facet>(3);
            foreach (var (vertex, members) in incident)
            {
                distinct.Clear();
                foreach (var facet in members.OrderByDescending(Size))
                {
                    if (distinct.Exists(other => Coplanar(other, facet))) continue;
                    distinct.Add(facet);
                    if (distinct.Count == 3) break;
                }
                if (distinct.Count < 3) redundant.Add(vertex);
            }
            return redundant;
        }

        private bool Coplanar(Facet first, Facet second) => ReferenceEquals(first.Plane, second.Plane) ||
            second.Corners.All(corner => Math.Abs(first.Distance(scaled[corner])) <= OutsideDistance);

        public TriMesh ToMesh(IReadOnlyList<V3> points)
        {
            var used = new SortedSet<int>();
            var alive = facets.Where(facet => facet.Alive).ToList();
            foreach (var facet in alive)
                foreach (var corner in facet.Corners) used.Add(corner);
            var remap = new Dictionary<int, int>(used.Count);
            var vertices = new V3[used.Count];
            var next = 0;
            foreach (var vertex in used)
            {
                remap[vertex] = next;
                vertices[next++] = points[vertex];
            }
            var faces = new int[alive.Count * 3];
            var write = 0;
            foreach (var facet in alive)
                foreach (var corner in facet.Corners) faces[write++] = remap[corner];
            return new(vertices, faces);
        }
    }
}
