using System.Globalization;

namespace PortingKit.Addons.Porting;

/// <summary>One convex element of a collision model: what studiomdl makes of one connected component.</summary>
/// <param name="Extents">Bounding box sizes in inches, smallest first.</param>
public sealed record ConvexPiece(double Volume, double Area, (double Smallest, double Middle, double Largest) Extents)
{
    public double MinThickness => Extents.Smallest;

    /// <summary>Whether studiomdl would call this piece two-dimensional.</summary>
    public bool Is2D => MinThickness < StaticCollision.MinPieceThicknessIn;
}

/// <summary>A collision mesh measured the way studiomdl measures it.</summary>
public sealed class CollisionModel
{
    public List<ConvexPiece> Pieces { get; init; } = [];
    public int PieceCount => Pieces.Count;

    /// <summary>Summed convex volume in cubic inches, which is the compiled "volume" field.</summary>
    public double Volume => Pieces.Sum(piece => piece.Volume);

    /// <summary>Summed convex surface area in square inches, for the shell mass law.</summary>
    public double Area => Pieces.Sum(piece => piece.Area);

    /// <summary>Indices of the pieces studiomdl would flag as two-dimensional.</summary>
    public List<int> ThinPieces => Enumerable.Range(0, Pieces.Count).Where(index => Pieces[index].Is2D).ToList();
}

/// <summary>What the open shell repair replaced.</summary>
public sealed record ShellSlabs(TriMesh? Mesh = null, int Replaced = 0, int Slabs = 0)
{
    public bool Changed => Replaced > 0;
}

/// <summary>What the thin piece repair had to do to a collision mesh.</summary>
/// <param name="Thinnest">The thinnest piece found, before anything was done about it.</param>
public sealed record ThinPieceRepair(TriMesh? Mesh = null, int Thickened = 0, int Dropped = 0, double Thinnest = 0.0)
{
    public bool Changed => Thickened > 0 || Dropped > 0;
}

/// <summary>What concave decomposition did to a collision mesh.</summary>
/// <param name="Unsplit">Pieces still concave enough to be worth cutting when the budget ran out.</param>
public sealed record Decomposition(TriMesh? Mesh = null, int PiecesBefore = 0, int PiecesAfter = 0, double VolumeBefore = 0.0, double VolumeAfter = 0.0, int Unsplit = 0)
{
    public bool Changed => PiecesAfter > PiecesBefore;

    /// <summary>The fraction of the hulled-over volume the cuts gave back.</summary>
    public double Recovered => VolumeBefore <= 0 ? 0.0 : 1.0 - VolumeAfter / VolumeBefore;
}

/// <summary>Measures and repairs static collision meshes.</summary>
/// <remarks>
/// <para>Everything studiomdl does to a collision SMD, reproduced so the kit can predict the compile instead of
/// guessing at it. The numbers were derived by pairing Valve's HL2 model sources (1593 static blocks) against the
/// shipped .phy files (1587 pairs) and confirmed against collisionmodel.cpp.</para>
/// <para>studiomdl splits the collision SMD into connected components and hulls each one; $concave only allows more
/// than one. $automass is volume times density, except for materials with a thickness, which are hollow shells and
/// use surface area times thickness times density. A hollow metal prop comes out about 30 times too heavy if that
/// branch is wrong. Omitting $inertia, $damping and $rotdamping yields 1, 0 and 0, which is what 1587 of Valve's
/// 1593 static props ship with, so inventing values is how a prop ends up rolling like nothing else in the game.</para>
/// <para>Density is kg per cubic metre and thickness is inches, straight from surfaceproperties.txt.</para>
/// </remarks>
public static class StaticCollision
{
    public const int DefaultMaxConvexPieces = 40;
    public const double MaxMassKg = 100_000.0;

    /// <summary>studiomdl clamps every compiled solid to this floor, silently, after distributing $mass.</summary>
    public const double MinSolidMassKg = 1.0;

    /// <summary>What a thin piece is inflated to. A hair over the limit, because a piece at exactly 0.5 is still
    /// rejected: the SMD carries limited decimals and the hull rebuilt from it lands just under. 0.55 passes.</summary>
    public const double ThickenTargetIn = 0.55;

    /// <summary>One cubic inch in cubic metres. Source works in inches and $mass is kilograms.</summary>
    public const double CubicInchInCubicMetres = 1.6387064e-5;

    /// <summary>A convex piece thinner than this makes studiomdl report 2-dimensional geometry and give up on
    /// decomposing the model at all.</summary>
    public const double MinPieceThicknessIn = 0.5;

    /// <summary>How many times one piece may be inflated. Pushing a piece out along its thinnest direction fixes that
    /// direction and no other, which is how the combine printer shipped eight pieces down to 0.148in after a repair
    /// reporting 157 thickened. Each pass fixes one axis and cannot undo an earlier one, so three cover three
    /// dimensions and the fourth is slack.</summary>
    public const int MaxThickenPasses = 4;

    /// <summary>Slack in $maxconvexpieces, as a fraction of the count plus a flat margin. It is a hard budget, not a
    /// hint: 120 real parts compile as 120 solids at 120 and as a single 3KB hull at 119. Our count can differ from the
    /// compiler's by a few either way, and declaring too many costs nothing at all.</summary>
    public const double ConvexBudgetHeadroom = 0.25;

    /// <inheritdoc cref="ConvexBudgetHeadroom"/>
    public const int ConvexBudgetMargin = 8;

    /// <summary>How far past the cut plane each half is taken, in inches. Halves that merely meet share vertices,
    /// studiomdl welds those, and the two hulls fuse back into the one hull the split existed to avoid.
    /// Interpenetration is how Valve's own collision is authored, and vphysics does not care.</summary>
    public const double SplitOverlapIn = 0.05;

    /// <summary>A cut must buy this fraction of the starting hull volume to be worth a piece. Over the Alyx combine
    /// hallway set 1% is where the curve flattens: it gives back 63-80% of the volume a single hull swallowed, while
    /// 2% stops at half that and 0.5% only overruns the budget.</summary>
    public const double MinSplitGain = 0.01;

    /// <summary>Cut positions tried per axis, spread across the piece's extent.</summary>
    public const int SplitSamples = 11;

    /// <summary>A patch count above this means a curved shell, where a slab per facet would cost more pieces than
    /// the model is allowed. Doorway reveals, window surrounds and open boxes come in far under it.</summary>
    public const int MaxShellPatches = 12;

    /// <summary>An open surface whose hull holds this many times its shell volume encloses space the source never had.</summary>
    public const double HollowHullRatio = 3.0;

    private static readonly Dictionary<string, (double Density, double Thickness)> Surfaceprops = new()
    {
        ["alienflesh"] = (900, 0), ["antlion"] = (900, 0), ["antlionsand"] = (1600, 0), ["armorflesh"] = (900, 0),
        ["bloodyflesh"] = (900, 0), ["boulder"] = (2400, 0), ["brakingrubbertire"] = (1600, 0), ["brick"] = (2400, 0),
        ["canister"] = (2700, 0.1), ["cardboard"] = (500, 0.25), ["carpet"] = (500, 0.1), ["ceiling_tile"] = (500, 0.25),
        ["chain"] = (1600, 0.5), ["chainlink"] = (1600, 0.5), ["combine_glass"] = (2700, 0.5), ["combine_metal"] = (2700, 0),
        ["computer"] = (2700, 0.1), ["concrete"] = (2400, 0), ["concrete_block"] = (2400, 0), ["crowbar"] = (2700, 0.1),
        ["default"] = (2000, 0), ["default_silent"] = (2000, 0), ["dirt"] = (1600, 0), ["flesh"] = (900, 0),
        ["floating_metal_barrel"] = (500, 0.1), ["floatingstandable"] = (800, 0), ["foliage"] = (700, 0), ["glass"] = (2700, 0.5),
        ["glassbottle"] = (2700, 0.5), ["grass"] = (1600, 0), ["gravel"] = (2400, 0), ["grenade"] = (2700, 0.1),
        ["gunship"] = (2700, 0.1), ["ice"] = (917, 0), ["item"] = (600, 0.25), ["jeeptire"] = (1600, 0),
        ["ladder"] = (2700, 0.1), ["metal"] = (2700, 0.1), ["metal_barrel"] = (2700, 0.1), ["metal_bouncy"] = (10000, 0),
        ["metal_box"] = (2700, 0.1), ["metal_seafloorcar"] = (2700, 0.1), ["metalgrate"] = (1600, 0.5), ["metalpanel"] = (2700, 0.1),
        ["metalvehicle"] = (2700, 0.1), ["metalvent"] = (2700, 0.04), ["mud"] = (1600, 0), ["no_decal"] = (900, 0),
        ["paintcan"] = (2700, 0.1), ["paper"] = (500, 0.25), ["papercup"] = (500, 0.25), ["plaster"] = (1600, 0),
        ["plastic"] = (500, 0.25), ["plastic_barrel"] = (500, 0.25), ["plastic_barrel_buoyant"] = (150, 0.25), ["plastic_box"] = (500, 0.25),
        ["player"] = (1000, 0), ["player_control_clip"] = (2000, 0), ["popcan"] = (2700, 0.1), ["porcelain"] = (2400, 0),
        ["pottery"] = (2700, 0.5), ["quicksand"] = (600, 0), ["rock"] = (2400, 0), ["roller"] = (2700, 0.1),
        ["rubber"] = (1600, 0), ["rubbertire"] = (1600, 0), ["sand"] = (1600, 0), ["slidingrubbertire"] = (1600, 0),
        ["slidingrubbertire_front"] = (1600, 0), ["slidingrubbertire_rear"] = (1600, 0), ["slime"] = (2000, 0), ["slipperymetal"] = (2700, 0.1),
        ["slipperyslime"] = (1600, 0), ["snow"] = (800, 0), ["solidmetal"] = (2700, 0), ["strider"] = (2700, 0.1),
        ["tile"] = (2700, 0.5), ["wade"] = (1000, 0), ["water"] = (1000, 0), ["watermelon"] = (900, 0),
        ["weapon"] = (2700, 0.1), ["wood"] = (700, 0), ["wood_box"] = (700, 0), ["wood_crate"] = (700, 0),
        ["wood_furniture"] = (700, 0), ["wood_lowdensity"] = (300, 0), ["wood_panel"] = (700, 1), ["wood_plank"] = (700, 0),
        ["wood_solid"] = (700, 0), ["woodladder"] = (700, 0), ["zombieflesh"] = (900, 0)
    };

    private static readonly Dictionary<string, string> Source2Aliases = new()
    {
        ["metal_combine"] = "combine_metal", ["metal_thin"] = "metalpanel", ["metal_thick"] = "solidmetal", ["metal_hollow"] = "metalvent",
        ["metal_grate"] = "metalgrate", ["metal_panel"] = "metalpanel", ["metal_barrel"] = "metal_barrel", ["wood_thin"] = "wood_panel",
        ["wood_thick"] = "wood_solid", ["wood_plank"] = "wood_plank", ["cardboard_box"] = "cardboard", ["plaster_wall"] = "plaster",
        ["stone"] = "rock", ["stucco"] = "plaster", ["asphalt"] = "concrete", ["ceramic"] = "porcelain", ["rubber_tire"] = "rubbertire",
        ["foliage_leaves"] = "foliage", ["flesh_human"] = "flesh", ["glass_bottle"] = "glassbottle", ["glass_window"] = "glass"
    };

    /// <summary>The best Source 1 surfaceprop for a raw, possibly Source 2, material name.</summary>
    /// <remarks>Source 2 names are dotted and namespaced; Source 1 wants a flat name from surfaceproperties.txt. Tries
    /// the whole name, the alias table, then the last dotted segment, and finally gives up on default, which is what
    /// the engine would fall back to anyway.</remarks>
    public static string ResolveSurfaceprop(string? name)
    {
        if (string.IsNullOrEmpty(name)) return "default";
        var raw = name.Trim().Trim('"').ToLowerInvariant().Replace('\\', '/');
        if (Surfaceprops.ContainsKey(raw)) return raw;
        if (Source2Aliases.TryGetValue(raw, out var alias)) return alias;
        var tail = raw[(raw.LastIndexOf('.') + 1)..];
        if (Surfaceprops.ContainsKey(tail)) return tail;
        return Source2Aliases.GetValueOrDefault(tail, "default");
    }

    public static (double Density, double Thickness) SurfacepropDensity(string? name) => Surfaceprops[ResolveSurfaceprop(name)];

    /// <summary>Merges coincident vertices, tolerating the given slop.</summary>
    /// <remarks>The distance is converted to decimal places, so this quantises rather than measures. Zero means
    /// the eight decimal places trimesh uses by default.</remarks>
    public static void WeldVertices(ref TriMesh mesh, double distance = 0.0) => mesh = MeshOps.MergeVertices(mesh, WeldDigits(distance));

    private static int WeldDigits(double distance) =>
        distance > 0 ? Math.Max(0, (int)MeshOps.Rint(-Math.Log10(distance))) : MeshOps.DefaultMergeDigits;

    /// <summary>Measures a mesh as studiomdl's convex decomposition would see it.</summary>
    /// <remarks>studiomdl treats each connected component as one convex element, which is why collision is authored
    /// as separate closed shapes that may interpenetrate. Welding first is what makes the components connected at
    /// all, since a physics glTF has three unshared vertices per triangle. Degenerate components are dropped, matching
    /// studiomdl discarding them rather than emitting a solid.</remarks>
    public static CollisionModel ConvexPieces(TriMesh mesh, double weldDistance = 0.0)
    {
        var model = new CollisionModel();
        if (mesh.Vertices.Length == 0 || mesh.FaceCount == 0) return model;
        var work = new TriMesh(mesh.Vertices, mesh.Faces);
        WeldVertices(ref work, weldDistance);
        var components = MeshOps.SplitElements(work);
        if (components.Count == 0) components = [work];
        foreach (var component in components)
        {
            if (component.Vertices.Length < 4) continue;
            var hull = QuickHull.Of(component.Vertices);
            if (hull is null || hull.FaceCount == 0) continue;
            var volume = Math.Abs(MeshOps.Volume(hull));
            if (volume <= 1e-9) continue;
            var (low, high) = MeshOps.Bounds(hull);
            double[] extents = [Math.Abs(high.X - low.X), Math.Abs(high.Y - low.Y), Math.Abs(high.Z - low.Z)];
            Array.Sort(extents);
            model.Pieces.Add(new(volume, Math.Abs(MeshOps.Area(hull)), (extents[0], extents[1], extents[2])));
        }
        return model;
    }

    /// <summary>The solid studiomdl would build around these points, or null if they are flat.</summary>
    public static TriMesh? HullOfPoints(IEnumerable<V3> points) => QuickHull.OfDistinct(points);

    /// <summary>The direction a piece is thinnest in and how thin it is there, or a null direction when it has none.</summary>
    /// <remarks>Face normals are the candidates: the minimum width of a convex solid is reached along one of them or
    /// an edge-pair direction, and for the slabs and sheets this exists to catch it is always a face normal. Read off
    /// the component rather than its hull, because a piece with no volume has no hull to read, and that is exactly the
    /// piece that needs finding. Rounding the normals merges a flat side's triangles into one candidate, and doing it
    /// the same way picks the same direction among a symmetric shape's near ties.</remarks>
    public static (V3? Direction, double Width) ThinnestDirection(TriMesh component)
    {
        var points = component.Vertices;
        if (points.Length < 3) return (null, 0.0);
        var candidates = MeshOps.UniqueRows(MeshOps.FaceNormals(component).Where(normal => normal.IsFinite)
            .Select(normal => new V3(MeshOps.RoundTo(normal.X, 5), MeshOps.RoundTo(normal.Y, 5), MeshOps.RoundTo(normal.Z, 5))));
        if (candidates.Length == 0) return (null, 0.0);
        V3? best = null;
        var bestWidth = double.PositiveInfinity;
        foreach (var candidate in candidates)
        {
            var length = Norm(candidate);
            if (length < 0.5) continue;
            var direction = candidate / length;
            var width = WidthAlong(points, direction);
            if (width >= bestWidth) continue;
            best = direction;
            bestWidth = width;
        }
        return best is null ? (null, 0.0) : (best, bestWidth);
    }

    /// <summary>A piece's extent along a unit direction.</summary>
    /// <remarks>A symmetric capsule has several face normals of equal width, and the rounding of this product is what
    /// picks one, which then decides the shape the piece inflates into. The fused form is the exact expression
    /// OpenBLAS evaluates for the matrix-vector product v3 used, so ties fall the same way as they do on an
    /// AVX2 or AVX-512 machine.</remarks>
    private static double WidthAlong(V3[] points, V3 direction)
    {
        double low = double.PositiveInfinity, high = double.NegativeInfinity;
        foreach (var point in points)
        {
            var projected = Math.FusedMultiplyAdd(point.Z, direction.Z, Math.FusedMultiplyAdd(point.X, direction.X, point.Y * direction.Y));
            low = Math.Min(low, projected);
            high = Math.Max(high, projected);
        }
        return high - low;
    }

    /// <summary>A vector's length, with the rounding of the sequential fused dot product NumPy's norm goes through.</summary>
    public static double Norm(V3 vector) =>
        Math.Sqrt(Math.FusedMultiplyAdd(vector.Z, vector.Z, Math.FusedMultiplyAdd(vector.Y, vector.Y, vector.X * vector.X)));

    /// <summary>As <see cref="ThinnestDirection"/>, reading the candidates off the point set's own hull, which is the
    /// solid studiomdl will build. The piece being measured between inflation passes has no faces of its own.</summary>
    private static (V3? Direction, double Width) ThinnestDirectionOfPoints(IEnumerable<V3> points)
    {
        var hull = HullOfPoints(points);
        return hull is null ? (null, 0.0) : ThinnestDirection(hull);
    }

    /// <summary>The component's faces grouped into connected coplanar patches.</summary>
    /// <param name="component">The element to group.</param>
    /// <param name="limit">Gives up and returns nothing once the component has more distinct planes than this.
    /// Grouping is the expensive half and a curved shell has a plane per facet, so a caller that would reject the
    /// answer should never pay for it. Zero means no limit.</param>
    public static List<TriMesh> CoplanarPatches(TriMesh component, int limit = 0)
    {
        var normals = MeshOps.FaceNormals(component);
        var centres = MeshOps.FaceCentres(component);
        if (normals.Length == 0) return [];
        var keys = new (double, double, double, double)[normals.Length];
        for (var face = 0; face < normals.Length; face++)
        {
            var (normal, centre) = (normals[face], centres[face]);
            keys[face] = (MeshOps.RoundTo(normal.X, 2), MeshOps.RoundTo(normal.Y, 2), MeshOps.RoundTo(normal.Z, 2),
                MeshOps.RoundTo(normal.X * centre.X + normal.Y * centre.Y + normal.Z * centre.Z, 1));
        }
        var planes = new SortedDictionary<(double, double, double, double), List<int>>(Comparer<(double, double, double, double)>.Create(ComparePlaneKeys));
        for (var face = 0; face < keys.Length; face++)
        {
            if (!planes.TryGetValue(keys[face], out var members)) planes[keys[face]] = members = [];
            members.Add(face);
        }
        if (limit > 0 && planes.Count > limit) return [];
        var patches = new List<TriMesh>();
        foreach (var members in planes.Values) patches.AddRange(MeshOps.SplitElements(MeshOps.Submesh(component, members)));
        return patches;
    }

    private static int ComparePlaneKeys((double, double, double, double) a, (double, double, double, double) b)
    {
        var order = MeshOps.CompareRows(new(a.Item1, a.Item2, a.Item3), new(b.Item1, b.Item2, b.Item3));
        if (order != 0) return order;
        return a.Item4 < b.Item4 ? -1 : a.Item4 > b.Item4 ? 1 : 0;
    }

    /// <summary>A flat patch given a thickness, as the solid studiomdl will build.</summary>
    private static TriMesh? SlabFromPatch(TriMesh patch, double thickness)
    {
        var normals = MeshOps.FaceNormals(patch);
        if (normals.Length == 0) return null;
        var length = Norm(normals[0]);
        if (length < 0.5) return null;
        var direction = normals[0] / length;
        var offset = direction * thickness / 2.0;
        return HullOfPoints(patch.Vertices.Select(point => point + offset).Concat(patch.Vertices.Select(point => point - offset)));
    }

    /// <inheritdoc cref="SlabOpenShells(TriMesh, double, double, int, double)"/>
    public static ShellSlabs SlabOpenShells(TriMesh mesh, double weldDistance = 1e-4, double thickness = ThickenTargetIn) =>
        SlabOpenShells(mesh, weldDistance, thickness, MaxShellPatches, HollowHullRatio);

    /// <summary>Rebuilds hollow open surfaces as one slab per flat face.</summary>
    /// <remarks>A Source 2 m_meshes shape is a surface, and some of what it describes is a hole: the reveal around a
    /// doorway is four strips forming an open tube. Hulling that tube fills the doorway in, and no convex decomposition
    /// undoes it, because cutting a frame in half leaves two halves whose hulls still cover the opening, so the volume
    /// saved is zero and a greedy search correctly refuses the cut. The hole only opens on the fourth cut, which no
    /// pairwise measure sees coming. Following the surface sidesteps the search: each flat face becomes its own thin
    /// solid and the opening is what is left between them. Only open, hollow and few-faced components qualify, since
    /// a curved shell of five hundred facets would want five hundred slabs and the model is allowed forty.</remarks>
    public static ShellSlabs SlabOpenShells(TriMesh mesh, double weldDistance, double thickness, int maxPatches, double hollowRatio)
    {
        if (mesh is null || mesh.FaceCount == 0) return new(mesh);
        var work = new TriMesh(mesh.Vertices, mesh.Faces);
        WeldVertices(ref work, weldDistance);
        var components = MeshOps.SplitElements(work);
        if (components.Count == 0) components = [work];
        var parts = new List<TriMesh>();
        int replaced = 0, slabCount = 0;
        foreach (var component in components)
        {
            var hull = HullOf(Soup(component));
            var shellVolume = Math.Abs(MeshOps.Area(component)) * thickness / 2.0;
            if (MeshOps.IsWatertight(component) || hull is null || shellVolume <= 0 || Math.Abs(MeshOps.Volume(hull)) <= hollowRatio * shellVolume)
            {
                parts.Add(component);
                continue;
            }
            var patches = CoplanarPatches(component, maxPatches);
            if (patches.Count == 0 || patches.Count > maxPatches)
            {
                parts.Add(component);
                continue;
            }
            var slabs = patches.Select(patch => SlabFromPatch(patch, thickness)).OfType<TriMesh>().ToList();
            if (slabs.Count == 0)
            {
                parts.Add(component);
                continue;
            }
            parts.AddRange(slabs);
            replaced++;
            slabCount += slabs.Count;
        }
        return new(replaced > 0 && parts.Count > 0 ? TriMesh.Concatenate(parts) : mesh, replaced, slabCount);
    }

    /// <summary>Inflates collision pieces too thin for studiomdl to make a solid of.</summary>
    /// <remarks>One piece under half an inch takes the whole model with it. studiomdl hulls each connected element, and
    /// an element it cannot turn into a solid makes it abandon that and hull the entire mesh into a single piece, saying
    /// nothing but a 2-dimensional geometry warning. On combine_hall_wall_window_wrapped_a nine real pieces compile to
    /// 566,525 cubic inches and one 0.0008 cubic inch sheet alongside them takes it to 1,098,614. Source 2 meshes are
    /// full of these because a zero-thickness quad is a real barrier there, so they are thickened rather than dropped:
    /// $remove2d deletes them and costs the grate, the sign and the pane their collision. The inflation is a Minkowski
    /// sum with a segment along the thin axis, which keeps the piece convex and grows it in that one direction only.
    /// When nothing survives the mesh is left alone, because an empty collision model is worse than the hull it compiles
    /// now.</remarks>
    public static ThinPieceRepair ThickenThinPieces(TriMesh mesh, double weldDistance = 1e-4, double minThickness = ThickenTargetIn)
    {
        if (mesh is null || mesh.FaceCount == 0) return new(mesh);
        var work = new TriMesh(mesh.Vertices, mesh.Faces);
        WeldVertices(ref work, weldDistance);
        var components = MeshOps.SplitElements(work);
        if (components.Count == 0) components = [work];
        var parts = new List<TriMesh>();
        var thinnest = double.PositiveInfinity;
        int thickened = 0, dropped = 0;
        foreach (var component in components)
        {
            var (direction, width) = ThinnestDirection(component);
            if (direction is not null) thinnest = Math.Min(thinnest, width);
            if (direction is null)
            {
                dropped++;
                continue;
            }
            if (width >= minThickness)
            {
                parts.Add(component);
                continue;
            }
            var points = component.Vertices;
            for (var pass = 0; pass < MaxThickenPasses; pass++)
            {
                var push = direction!.Value * (0.5 * (minThickness - width));
                var inflated = new V3[points.Length * 2];
                for (var index = 0; index < points.Length; index++)
                {
                    inflated[index] = points[index] + push;
                    inflated[points.Length + index] = points[index] - push;
                }
                points = inflated;
                (direction, width) = ThinnestDirectionOfPoints(points);
                if (direction is null || width >= minThickness) break;
            }
            var slab = HullOfPoints(points);
            if (slab is null)
            {
                dropped++;
                continue;
            }
            parts.Add(slab);
            thickened++;
        }
        var lowest = double.IsPositiveInfinity(thinnest) ? 0.0 : thinnest;
        if (parts.Count == 0) return new(mesh, 0, 0, lowest);
        return new(thickened > 0 || dropped > 0 ? TriMesh.Concatenate(parts) : mesh, thickened, dropped, lowest);
    }

    private static V3[] Soup(TriMesh mesh) => Array.ConvertAll(mesh.Faces, index => mesh.Vertices[index]);

    private static TriMesh? HullOf(V3[] triangles) => triangles.Length == 0 ? null : HullOfPoints(triangles);

    private sealed record Cut(double Total, V3[] Below, V3[] Above);

    /// <summary>One candidate piece, plus the cut it is holding out for.</summary>
    private sealed class Piece(V3[] triangles, int component, double volume)
    {
        public V3[] Triangles => triangles;
        public int Component => component;
        public double Volume => volume;
        public Cut? Best { get; private set; }
        private bool searched;

        /// <summary>The volume the best cut would give back. Searched once, then kept.</summary>
        public double Gain(double overlap)
        {
            if (!searched)
            {
                Best = BestCut(triangles, overlap);
                searched = true;
            }
            return Best is null ? 0.0 : volume - Best.Total;
        }
    }

    /// <summary>The axis-aligned cut that covers this piece in the least volume.</summary>
    /// <remarks>Volume is the whole objective: a hull that swallows a doorway costs exactly the doorway, so the cut
    /// that shrinks the summed hulls most is the one that gave the most of the opening back.</remarks>
    private static Cut? BestCut(V3[] triangles, double overlap)
    {
        Cut? best = null;
        var (low, high) = MeshOps.Bounds(triangles);
        var step = (0.9 - 0.1) / (SplitSamples - 1);
        for (var axis = 0; axis < 3; axis++)
        {
            var span = high[axis] - low[axis];
            if (span < 4.0 * overlap) continue;
            for (var sample = 0; sample < SplitSamples; sample++)
            {
                var fraction = sample == SplitSamples - 1 ? 0.9 : sample * step + 0.1;
                var position = low[axis] + span * fraction;
                var below = MeshOps.ClipTriangles(triangles, axis, position + overlap, true);
                var above = MeshOps.ClipTriangles(triangles, axis, position - overlap, false);
                TriMesh? hullBelow = HullOf(below), hullAbove = HullOf(above);
                if (hullBelow is null || hullAbove is null) continue;
                var total = Math.Abs(MeshOps.Volume(hullBelow)) + Math.Abs(MeshOps.Volume(hullAbove));
                if (best is null || total < best.Total) best = new(total, below, above);
            }
        }
        return best;
    }

    /// <inheritdoc cref="DecomposeConcave(TriMesh, int, double, double, double, CancellationToken)"/>
    public static Decomposition DecomposeConcave(TriMesh mesh, int maxPieces = DefaultMaxConvexPieces) =>
        DecomposeConcave(mesh, maxPieces, 1e-4);

    /// <summary>Cuts concave components up until studiomdl can represent them.</summary>
    /// <remarks>Source 2 collision is often one m_meshes triangle soup, which Rubikon takes as it is for anything that
    /// does not move. Source 1 has no such shape, so a doorframe compiles as a solid slab with the doorway filled in and
    /// nothing in the compile says so. Concave components are therefore cut greedily and axis-aligned: whichever piece
    /// can give back the most volume is cut next, until the cuts stop paying or the budget runs out. A cut component
    /// comes back as convex hulls, the exact solids the compiler will build, while a component nobody had to cut keeps
    /// its own triangles, so a mesh that was already fine passes through untouched. Cancelling stops the cutting and
    /// keeps what is finished.</remarks>
    public static Decomposition DecomposeConcave(TriMesh mesh, int maxPieces, double weldDistance, double overlap = SplitOverlapIn,
        double minGain = MinSplitGain, CancellationToken cancel = default)
    {
        if (mesh is null || mesh.FaceCount == 0) return new(mesh);
        var work = new TriMesh(mesh.Vertices, mesh.Faces);
        WeldVertices(ref work, weldDistance);
        var components = MeshOps.SplitElements(work);
        if (components.Count == 0) components = [work];
        var pieces = new List<Piece>();
        for (var index = 0; index < components.Count; index++)
        {
            var triangles = Soup(components[index]);
            var hull = HullOf(triangles);
            if (hull is not null) pieces.Add(new(triangles, index, Math.Abs(MeshOps.Volume(hull))));
        }
        var before = pieces.Count;
        var volumeBefore = pieces.Sum(piece => piece.Volume);
        if (pieces.Count == 0) return new(mesh, 0, 0, volumeBefore, volumeBefore);
        var floor = minGain * volumeBefore;
        while (pieces.Count < maxPieces)
        {
            if (cancel.IsCancellationRequested) break;
            var best = pieces[0];
            var bestGain = best.Gain(overlap);
            foreach (var piece in pieces.Skip(1))
            {
                var gain = piece.Gain(overlap);
                if (gain <= bestGain) continue;
                best = piece;
                bestGain = gain;
            }
            if (best.Best is null || bestGain <= floor) break;
            var (_, below, above) = best.Best;
            pieces.Remove(best);
            foreach (var triangles in new[] { below, above })
            {
                var hull = HullOf(triangles);
                if (hull is not null) pieces.Add(new(triangles, best.Component, Math.Abs(MeshOps.Volume(hull))));
            }
        }
        var after = pieces.Count;
        var volumeAfter = pieces.Sum(piece => piece.Volume);
        var unsplit = pieces.Count(piece => piece.Gain(overlap) > floor);
        if (after <= before) return new(mesh, before, after, volumeBefore, volumeAfter, unsplit);
        var wasCut = pieces.GroupBy(piece => piece.Component).Where(group => group.Count() > 1).Select(group => group.Key).ToHashSet();
        var parts = components.Where((_, index) => !wasCut.Contains(index)).ToList();
        foreach (var piece in pieces)
        {
            if (!wasCut.Contains(piece.Component)) continue;
            var hull = HullOf(piece.Triangles);
            if (hull is not null) parts.Add(hull);
        }
        return new(parts.Count > 0 ? TriMesh.Concatenate(parts) : mesh, before, after, volumeBefore, volumeAfter, unsplit);
    }

    /// <summary>$mass in kilograms, by studiomdl's $automass rule.</summary>
    /// <remarks>Solid materials use volume; materials with a thickness are hollow shells and use surface area times
    /// that thickness. The result is floored at the per-solid minimum because studiomdl would apply it anyway.</remarks>
    public static double Automass(CollisionModel model, string? surfaceprop)
    {
        var (density, thickness) = SurfacepropDensity(surfaceprop);
        var cubicInches = thickness > 0 ? model.Area * thickness : model.Volume;
        return Math.Min(Math.Max(cubicInches * density * CubicInchInCubicMetres, MinSolidMassKg), MaxMassKg);
    }

    /// <summary>The $maxconvexpieces to declare for a mesh measured at this piece count.</summary>
    /// <remarks>Never below studiomdl's default of 40, and above the count by a margin, because a budget the compiler's
    /// part count overruns collapses the whole collision model into a single hull.</remarks>
    public static int ConvexPieceBudget(int pieceCount)
    {
        if (pieceCount <= 0) return DefaultMaxConvexPieces;
        return Math.Max(DefaultMaxConvexPieces, (int)Math.Ceiling(pieceCount * (1.0 + ConvexBudgetHeadroom)) + ConvexBudgetMargin);
    }

    /// <summary>The warnings studiomdl or the shipped model would produce, in advance.</summary>
    /// <remarks>An empty list means the mesh compiles clean. These are the failures that otherwise only show up as a
    /// prop that falls through the world or weighs nothing.</remarks>
    public static List<string> CheckCollisionModel(CollisionModel model, double mass, int maxConvexPieces = DefaultMaxConvexPieces)
    {
        if (model.Pieces.Count == 0) return ["collision mesh has no usable convex pieces - studiomdl will produce no collision at all"];
        var warnings = new List<string>();
        if (model.PieceCount > maxConvexPieces)
            warnings.Add($"{Fmt.Int(model.PieceCount)} convex pieces exceeds the default $maxconvexpieces {Fmt.Int(maxConvexPieces)} - the QC raises the budget " +
                $"to {Fmt.Int(ConvexPieceBudget(model.PieceCount))} to cover them, since a budget the compiler overruns makes it hull the whole model into one piece, " +
                "but 'COSTLY COLLISION MODEL' is a real cost and the prop is expensive to simulate");
        var thin = model.ThinPieces;
        if (thin.Count > 0)
        {
            var thinnest = thin.Min(index => model.Pieces[index].MinThickness);
            warnings.Add($"{Fmt.Int(thin.Count)} of {Fmt.Int(model.PieceCount)} convex piece(s) are thinner than {MinPieceThicknessIn.ToString("0.0", CultureInfo.InvariantCulture)}in " +
                $"(thinnest {Fmt.Fixed(thinnest, 3)}in) - studiomdl reports 'Model has 2-dimensional geometry' and the collision is unreliable");
        }
        if (mass <= MinSolidMassKg)
            warnings.Add($"$mass {Fmt.Fixed(mass, 3)} is at or below studiomdl's 1.0kg per-solid floor - the compiled prop will weigh 1.0kg regardless");
        return warnings;
    }

    /// <summary>The $collisionmodel QC lines for a static prop.</summary>
    /// <remarks>Shaped like Valve's own: of 1593 static blocks in the HL2 sources, 1477 carry $concave, 1345 a $mass, and
    /// only a handful set anything else. $concave is only written when the mesh has more than one convex piece, and
    /// $maxconvexpieces only when the piece count needs it: Valve never raises it, and raising it blindly just hides
    /// the compiler's cost warning.</remarks>
    public static List<string> CollisionModelBlock(string smdName, double? mass = null, bool concave = true, int pieceCount = 1, int? maxConvexPieces = null,
        bool remove2d = false, double? inertia = null, double? damping = null, double? rotDamping = null, string indent = "    ")
    {
        var lines = new List<string> { $"$collisionmodel \"{smdName}\" {{" };
        if (remove2d) lines.Add($"{indent}$remove2d");
        if (concave && pieceCount > 1) lines.Add($"{indent}$concave");
        if (maxConvexPieces is { } budget && pieceCount > DefaultMaxConvexPieces) lines.Add($"{indent}$maxconvexpieces {Fmt.Int(budget)}");
        if (mass is { } kilograms) lines.Add($"{indent}$mass {Fmt.Fixed(kilograms, 2)}");
        if (inertia is { } scale) lines.Add($"{indent}$inertia {Fmt.Fixed(scale, 2)}");
        if (damping is { } linear) lines.Add($"{indent}$damping {Fmt.Fixed(linear, 2)}");
        if (rotDamping is { } angular) lines.Add($"{indent}$rotdamping {Fmt.Fixed(angular, 2)}");
        lines.Add("}");
        return lines;
    }
}
