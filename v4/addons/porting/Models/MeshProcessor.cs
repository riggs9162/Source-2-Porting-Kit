namespace PortingKit.Addons.Porting;

/// <summary>Applies scale, axis conversion and cleanup to a loaded mesh.</summary>
public static class MeshProcessor
{
    /// <summary>studiomdl floors every compiled solid at 1kg, so asking for less is a no-op.</summary>
    public const double MinMassKg = StaticCollision.MinSolidMassKg;

    /// <summary>Undoes Source 2 Viewer's own basis swap.</summary>
    /// <remarks>Only for vertices it already baked into glTF convention. A file that still carries the conversion
    /// matrix needs no rotation at all, and a Blender export needs <see cref="ApplyAxisConversion"/> instead.</remarks>
    public static void ApplySource2AxisConversion(ModelMesh mesh) => mesh.ApplyTransform(GltfDocument.Source2Basis);

    /// <summary>Rotates +90 degrees about X, taking a Y-up export into Source's Z-up.</summary>
    public static void ApplyAxisConversion(ModelMesh mesh)
    {
        double cosine = Math.Cos(Math.PI / 2), sine = Math.Sin(Math.PI / 2);
        mesh.ApplyTransform(new([1, 0, 0, 0, 0, cosine, -sine, 0, 0, sine, cosine, 0, 0, 0, 0, 1]));
    }

    /// <summary>Removes invalid geometry, keeping per-face materials aligned, then orients faces.</summary>
    /// <remarks>The original swallows any failure part way through and writes what it has, so a face index
    /// past the vertex array stops the pass here and is filtered again at SMD export. A mesh with no per-face
    /// materials reaches trimesh methods that no longer exist and stops silently after dropping infinities.</remarks>
    public static void Sanitize(ModelMesh mesh)
    {
        try
        {
            mesh.RemoveInfiniteValues();
            if (mesh.FaceMaterials is null) return;
            mesh.UpdateFaces(mesh.NondegenerateFaces());
            mesh.UpdateFaces(mesh.UniqueFaces());
            mesh.RemoveUnreferencedVertices();
            MeshRepair.FixNormals(mesh);
        }
        catch (IndexOutOfRangeException)
        {
        }
    }

    /// <summary>Measures a physics mesh the way studiomdl's decomposition will.</summary>
    /// <remarks>Welding matters here: a physics glTF has three unshared vertices per triangle, so without it
    /// every triangle is its own connected component and the model reads as thousands of degenerate pieces.</remarks>
    public static CollisionModel MeasureCollision(ModelMesh? mesh, double weldDistance = 0.0) =>
        mesh is null ? new() : StaticCollision.ConvexPieces(mesh.ToTriMesh(), Math.Max(weldDistance, 1e-4));

    /// <summary>The <c>$mass</c> of a measured collision model by studiomdl's automass rule, scaled by the modifier.</summary>
    public static double? CalculateSurfaceMass(CollisionModel? model, string? surfaceprop, double massModifier)
    {
        if (model is null || model.Pieces.Count == 0) return null;
        var mass = StaticCollision.Automass(model, surfaceprop) * (massModifier != 0.0 ? massModifier : 1.0);
        return Math.Min(Math.Max(mass, MinMassKg), StaticCollision.MaxMassKg);
    }
}
