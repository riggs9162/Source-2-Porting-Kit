namespace PortingKit.Addons.Porting;

/// <summary>One convex element of a collision model: what studiomdl makes of one connected component.</summary>
public sealed record ConvexPiece(double Volume, double Area, (double Smallest, double Middle, double Largest) Extents);

/// <summary>A collision mesh measured the way studiomdl measures it.</summary>
public sealed class CollisionModel
{
    public List<ConvexPiece> Pieces { get; init; } = [];
}

/// <summary>What the open shell repair replaced.</summary>
public sealed record ShellSlabs(TriMesh? Mesh = null, int Replaced = 0, int Slabs = 0);

/// <summary>What the thin piece repair had to do to a collision mesh.</summary>
public sealed record ThinPieceRepair(TriMesh? Mesh = null, int Thickened = 0, int Dropped = 0, double Thinnest = 0.0);

/// <summary>What concave decomposition did to a collision mesh.</summary>
public sealed record Decomposition(TriMesh? Mesh = null, int PiecesBefore = 0, int PiecesAfter = 0, double VolumeBefore = 0.0, double VolumeAfter = 0.0, int Unsplit = 0);

/// <summary>Measures and repairs static collision meshes.</summary>
public static class StaticCollision
{
    public const int DefaultMaxConvexPieces = 40;
    public const double MaxMassKg = 100_000.0;
    public const double MinSolidMassKg = 1.0;
    public const double ThickenTargetIn = 0.55;

    public static string ResolveSurfaceprop(string? name) => throw new NotImplementedException();
    public static (double Density, double Thickness) SurfacepropDensity(string? name) => throw new NotImplementedException();
    public static void WeldVertices(ref TriMesh mesh, double distance = 0.0) => throw new NotImplementedException();
    public static CollisionModel ConvexPieces(TriMesh mesh, double weldDistance = 0.0) => throw new NotImplementedException();
    public static ShellSlabs SlabOpenShells(TriMesh mesh, double weldDistance = 1e-4, double thickness = ThickenTargetIn) => throw new NotImplementedException();
    public static ThinPieceRepair ThickenThinPieces(TriMesh mesh, double weldDistance = 1e-4, double minThickness = ThickenTargetIn) => throw new NotImplementedException();
    public static Decomposition DecomposeConcave(TriMesh mesh, int maxPieces = DefaultMaxConvexPieces) => throw new NotImplementedException();
    public static double Automass(CollisionModel model, string? surfaceprop) => throw new NotImplementedException();
    public static int ConvexPieceBudget(int pieceCount) => throw new NotImplementedException();
    public static List<string> CheckCollisionModel(CollisionModel model, double mass, int maxConvexPieces = DefaultMaxConvexPieces) => throw new NotImplementedException();
    public static List<string> CollisionModelBlock(string smdName, double? mass = null, bool concave = true, int pieceCount = 1, int? maxConvexPieces = null,
        bool remove2d = false, double? inertia = null, double? damping = null, double? rotDamping = null, string indent = "    ") => throw new NotImplementedException();
}

/// <summary>Generated collision QC and an audit of source parts and repaired hulls.</summary>
public sealed record NativePhysicsResult(List<string> QcLines, System.Text.Json.Nodes.JsonObject Report, List<string> Warnings);

/// <summary>Writes rigidly weighted compound hulls without inventing source joints.</summary>
public static class NativePhysics
{
    public static NativePhysicsResult WriteKinematicPhysics(string physPath, string smdPath, string referencePath, IReadOnlyList<string> boneNames, string rootBone, string originBone = "") =>
        throw new NotImplementedException();
}
