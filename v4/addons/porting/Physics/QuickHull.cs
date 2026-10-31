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
/// and facet order differ from qhull's. Vertices, normals, volume and area are the part downstream code reads. studiomdl
/// does read triangle order on a dense hull, but those are ragdoll shapes, which the exact serializer replaces.</para>
/// <para>Tolerances alone cannot survive points scattered around a plane at about the size of the tolerance: facets
/// disagree about which side a point is on, a coplanar facet folds over its neighbour, and the volume comes out wrong
/// while everything still looks closed. Qhull recovers by merging facets. Here each finished hull is checked for
/// closure, local convexity and folds, and one that fails is rebuilt on a 40-bit integer lattice, where orientation is
/// an exact 128-bit determinant and none of that can happen. The lattice costs about 1e-12 of the model's size, and it
/// is only a fallback because rounding to it would make rotated flat sides stop being flat.</para>
/// <para>Flat input has no hull. Qhull refuses it and trimesh retries with joggling, which produces a zero-volume
/// mesh that every caller then discards, so null is the same answer.</para>
/// </remarks>
public static class QuickHull
{
    private const double VisibleDistance = 2.2e-15;
    private const double OutsideDistance = 4.4e-15;
    private const double FlatDistance = 3e-15;
    private const double SmallestVolume = 1e-9;
    private const double ConcaveLimit = 1e-11;
    private const double SliverAltitude = 1e-9;
    private const double LatticeCells = 1L << 40;

    private enum Outcome { Flat, Failed, Built }

    /// <summary>The convex hull of a cloud, or null when it spans no volume.</summary>
    public static TriMesh? Of(IReadOnlyList<V3> points)
    {
        var hull = OfUnmerged(points);
        return hull is null ? null : MeshOps.MergeVertices(hull);
    }

    /// <summary>The hull before near-coincident vertices merge. Merging can collapse a sliver, so only this form is
    /// guaranteed closed.</summary>
    public static TriMesh? OfUnmerged(IReadOnlyList<V3> points)
    {
        var outcome = Build(points, false, out var mesh);
        if (outcome == Outcome.Failed) Build(points, true, out mesh);
        return mesh;
    }

    private static Outcome Build(IReadOnlyList<V3> points, bool exact, out TriMesh? mesh)
    {
        mesh = null;
        var active = new bool[points.Count];
        Array.Fill(active, true);
        while (true)
        {
            var solver = Solver.Create(points, active, exact);
            if (solver is null) return Outcome.Flat;
            var outcome = solver.Run();
            if (outcome != Outcome.Built) return outcome;
            if (!exact && !solver.IsConsistent()) return Outcome.Failed;
            var redundant = solver.RedundantVertices();
            if (redundant.Count == 0)
            {
                mesh = solver.ToMesh(points);
                return Outcome.Built;
            }
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
        private readonly long[]? lattice;
        private readonly List<Facet> facets = [];
        private readonly Queue<Facet> pending = new();
        private readonly int[] active;
        private int stamp;

        private Solver(V3[] scaled, int[] active, long[]? lattice)
        {
            this.scaled = scaled;
            this.active = active;
            this.lattice = lattice;
        }

        public static Solver? Create(IReadOnlyList<V3> points, bool[] enabled, bool exact)
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
            var lattice = exact ? new long[points.Count * 3] : null;
            foreach (var index in active)
            {
                var point = points[index];
                scaled[index] = new(
                    Math.Clamp(point.X * scales[0] + shifts[0], -0.5, 0.5),
                    Math.Clamp(point.Y * scales[1] + shifts[1], -0.5, 0.5),
                    Math.Clamp(point.Z * scales[2] + shifts[2], -0.5, 0.5));
                if (lattice is null) continue;
                for (var axis = 0; axis < 3; axis++) lattice[index * 3 + axis] = (long)Math.Round(scaled[index][axis] * LatticeCells);
            }
            return new(scaled, active.ToArray(), lattice);
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

        /// <summary>Which side of the plane through a, b and c a point lies on, exactly. Differences of 40-bit lattice
        /// coordinates need 41 bits, so the determinant needs at most 126 and cannot overflow.</summary>
        private int Side(int a, int b, int c, int point)
        {
            var grid = lattice!;
            Int128 ax = grid[b * 3] - grid[a * 3], ay = grid[b * 3 + 1] - grid[a * 3 + 1], az = grid[b * 3 + 2] - grid[a * 3 + 2];
            Int128 bx = grid[c * 3] - grid[a * 3], by = grid[c * 3 + 1] - grid[a * 3 + 1], bz = grid[c * 3 + 2] - grid[a * 3 + 2];
            Int128 px = grid[point * 3] - grid[a * 3], py = grid[point * 3 + 1] - grid[a * 3 + 1], pz = grid[point * 3 + 2] - grid[a * 3 + 2];
            var determinant = px * (ay * bz - az * by) + py * (az * bx - ax * bz) + pz * (ax * by - ay * bx);
            return determinant.CompareTo(Int128.Zero);
        }

        private int Side(Facet facet, int point) => Side(facet.Corners[0], facet.Corners[1], facet.Corners[2], point);

        private bool IsOutside(Facet facet, int point, out double distance)
        {
            distance = facet.Distance(scaled[point]);
            return lattice is null ? distance > OutsideDistance : Side(facet, point) > 0;
        }

        private bool IsVisible(Facet facet, int point) =>
            lattice is null ? facet.Distance(scaled[point]) > VisibleDistance : Side(facet, point) > 0;

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
            var tallest = lattice is null ? FlatDistance : 0.0;
            foreach (var index in active)
            {
                var distance = Math.Abs(plane.Distance(scaled[index]));
                if (distance <= tallest || (lattice is not null && Side(a, b, c, index) == 0)) continue;
                tallest = distance;
                d = index;
            }
            if (d < 0) return false;
            var above = lattice is null ? plane.Distance(scaled[d]) > 0.0 : Side(a, b, c, d) > 0;
            if (above) (b, c) = (c, b);
            return true;
        }

        private static void Link(Facet facet, int edge, Facet neighbour) => facet.Neighbours[edge] = neighbour;

        private void Enqueue(Facet facet)
        {
            if (facet.Outside is { Count: > 0 }) pending.Enqueue(facet);
        }

        public Outcome Run()
        {
            if (!InitialSimplex(out var a, out var b, out var c, out var d)) return Outcome.Flat;
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
                if (!FindHorizon(eye, visible, horizon)) return Outcome.Failed;
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
            return Outcome.Built;
        }

        private void Assign(int point, IReadOnlyList<Facet> candidates)
        {
            Facet? best = null;
            var bestDistance = double.NegativeInfinity;
            foreach (var facet in candidates)
            {
                if (!IsOutside(facet, point, out var distance) || distance <= bestDistance) continue;
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
                    if (neighbour!.Stamp == stamp || !IsVisible(neighbour, eye)) continue;
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
                var inherited = lattice is null && Math.Abs(edge.Outside.Distance(scaled[eye])) <= VisibleDistance;
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

        /// <summary>Whether the finished surface is one closed sphere with no real dent and no facet folded over another.</summary>
        /// <remarks>Local convexity on a closed sphere is global convexity, so this needs one pass over the edges rather
        /// than every point against every facet. That matters, since a concave cut search hulls thousands of times. A
        /// sliver proves nothing either way, so the rare hull holding one also has every point tried against every
        /// well-shaped facet.</remarks>
        public bool IsConsistent()
        {
            var alive = 0;
            var edges = 0;
            var slivers = false;
            var vertices = new HashSet<int>();
            foreach (var facet in facets)
            {
                if (!facet.Alive) continue;
                alive++;
                for (var edge = 0; edge < 3; edge++)
                {
                    vertices.Add(facet.Corners[edge]);
                    edges++;
                    var neighbour = facet.Neighbours[edge]!;
                    int from = facet.Corners[edge], to = facet.Corners[(edge + 1) % 3];
                    var slot = Array.FindIndex(neighbour.Corners, corner => corner == to);
                    if (!neighbour.Alive || slot < 0 || neighbour.Corners[(slot + 1) % 3] != from) return false;
                    var opposite = neighbour.Corners[(slot + 2) % 3];
                    if (Altitude(facet) < SliverAltitude || Altitude(neighbour) < SliverAltitude)
                    {
                        slivers = true;
                        continue;
                    }
                    var height = facet.Distance(scaled[opposite]);
                    if (height > ConcaveLimit) return false;
                    if (height >= -ConcaveLimit && Folded(facet, edge, opposite)) return false;
                }
            }
            return vertices.Count - edges / 2 + alive == 2 && (!slivers || HoldsEveryPoint());
        }

        private bool HoldsEveryPoint()
        {
            foreach (var facet in facets)
            {
                if (!facet.Alive || Altitude(facet) < SliverAltitude) continue;
                foreach (var point in active)
                    if (facet.Distance(scaled[point]) > ConcaveLimit) return false;
            }
            return true;
        }

        /// <summary>Whether a facet's coplanar neighbour lies on the same side of their shared edge rather than across it.</summary>
        private bool Folded(Facet facet, int edge, int opposite)
        {
            var origin = scaled[facet.Corners[edge]];
            var along = scaled[facet.Corners[(edge + 1) % 3]] - origin;
            var own = along.Cross(scaled[facet.Corners[(edge + 2) % 3]] - origin);
            return own.Dot(along.Cross(scaled[opposite] - origin)) > 0;
        }

        /// <summary>A facet's smallest height. Below <see cref="SliverAltitude"/> its plane is arbitrary and proves nothing.</summary>
        private double Altitude(Facet facet)
        {
            V3 a = scaled[facet.Corners[0]], b = scaled[facet.Corners[1]], c = scaled[facet.Corners[2]];
            double first = (b - a).Dot(b - a), second = (c - b).Dot(c - b), third = (a - c).Dot(a - c);
            return Math.Sqrt(Size(facet) / Math.Max(Math.Max(first, second), Math.Max(third, double.Epsilon)));
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

        private bool Coplanar(Facet first, Facet second)
        {
            if (lattice is not null) return second.Corners.All(corner => Side(first, corner) == 0);
            return ReferenceEquals(first.Plane, second.Plane) || second.Corners.All(corner => Math.Abs(first.Distance(scaled[corner])) <= OutsideDistance);
        }

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
