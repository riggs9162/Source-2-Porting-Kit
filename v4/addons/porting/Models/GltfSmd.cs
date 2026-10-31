using System.Diagnostics;

namespace PortingKit.Addons.Porting;

/// <summary>Everything one conversion needs, with the same defaults as the tool's parameter schema.</summary>
public sealed record ConversionOptions
{
    public double Scale { get; init; } = 40.0;
    public double WeldDistance { get; init; } = 0.0001;
    public bool AxisConversion { get; init; } = true;
    public bool ForceManualScale { get; init; }
    public bool FlipV { get; init; }
    public string UvMode { get; init; } = "preserve";

    public bool GenerateSmd { get; init; } = true;
    public bool GenerateQc { get; init; } = true;
    public bool ExportPhysics { get; init; } = true;
    public bool ExportAnimations { get; init; } = true;

    /// <summary>Ignores any skeleton and writes the bind pose as one rigid mesh, which is what a separate static prop needs.</summary>
    /// <remarks>Skinned vertices are already in model space at the bind pose, so dropping the skin moves nothing.</remarks>
    public bool Rigid { get; init; }
    public double AnimationFps { get; init; } = 30.0;
    public bool AutoLoopDetect { get; init; } = true;

    public string ModelnamePrefix { get; init; } = "";
    public string Cdmaterials { get; init; } = "models/";
    public bool Concave { get; init; } = true;
    public bool AutoMass { get; init; } = true;
    public double MassModifier { get; init; } = 1.0;
    public double StaticMass { get; init; } = 10.0;
    public bool AutoSurfaceprop { get; init; } = true;
    public string StaticSurfaceprop { get; init; } = "metal";

    /// <summary><c>$remove2d</c> drops any convex piece under half an inch thick.</summary>
    /// <remarks>Valve never uses it on a static prop, and on a ported model it silently deletes the collision of
    /// thin panels, signs and grates. So it is opt-in, and the default reports which pieces would have gone.</remarks>
    public bool Remove2dCollision { get; init; }

    /// <summary>Forces <c>$surfaceprop</c> over both the detector and the fixed value. See <see cref="Surfaceprops.SurfacepropOverrideFor"/>.</summary>
    public string SurfacepropOverride { get; init; } = "";

    /// <summary>Builds collision from the render mesh for a model the source gives none.</summary>
    /// <remarks>Source 2 leaves those models non-solid, as decoration, but a prop dropped into a Hammer map usually
    /// has to hold a player up, and clipping a floor by hand for each one is worse than a hull that follows it.
    /// Off gives the source's answer instead: no <c>$collisionmodel</c>.</remarks>
    public bool CollisionFromRender { get; init; } = true;

    /// <summary>Cuts concave collision into pieces studiomdl's per-element hulls can hold.</summary>
    /// <remarks>Source 2 collision is often one concave triangle soup, which Source 1 has no shape for.</remarks>
    public bool DecomposeCollision { get; init; } = true;

    /// <summary>The piece budget. studiomdl's default is 40; staying inside it avoids <c>$maxconvexpieces</c> and the cost warning.</summary>
    public int MaxConvexPieces { get; init; } = StaticCollision.DefaultMaxConvexPieces;

    public bool SkipPhysicsForLarge { get; init; } = true;
    public double LargeModelThreshold { get; init; } = 1024.0;

    public bool PreserveFolders { get; init; } = true;
    public bool ReplaceExisting { get; init; } = true;
    public bool DryRun { get; init; }
    public bool Verbose { get; init; }
}

/// <summary>What happened to one model set.</summary>
public sealed class ConversionResult(string name)
{
    public string Name { get; } = name;

    /// <summary>One of converted, skipped, failed or dry-run.</summary>
    public string Status { get; set; } = "converted";
    public string Reason { get; set; } = "";
    public string? OutputDir { get; set; }
    public List<string> Files { get; } = [];
    public int Bones { get; set; }
    public int Clips { get; set; }
    public int Triangles { get; set; }
    public int Vertices { get; set; }
    public double? Mass { get; set; }
    public string Surfaceprop { get; set; } = "";
    public bool HasPhysics { get; set; }
    public bool PhysicsFallback { get; set; }
    public int Skins { get; set; }
    public double Elapsed { get; set; }
    public List<string> Warnings { get; } = [];
    public bool Ok => Status is "converted" or "dry-run";
}

/// <summary>Turns an exported glTF into the SMD meshes, animation SMDs and QC that studiomdl compiles.</summary>
/// <remarks>This orchestrates one model. Asset problems come back as a failed <see cref="ConversionResult"/> so a
/// batch keeps going; only cancellation propagates.</remarks>
public static class GltfSmd
{
    /// <summary>The material a collision SMD uses once rebuilding has dropped its original one.</summary>
    public const string PhysicsMaterial = "physics_group_prop.wood_crate_material";

    /// <summary>Above this the greedy search costs more than the collision is worth.</summary>
    /// <remarks>A render mesh standing in for missing physics data runs to tens of thousands of faces, and every
    /// candidate cut clips all of them.</remarks>
    public const int MaxDecomposeFaces = 6000;

    private static string? Relative(string path, string root)
    {
        var relative = Path.GetRelativePath(root, path);
        return relative.StartsWith("..", StringComparison.Ordinal) || Path.IsPathRooted(relative) ? null : relative;
    }

    /// <summary>Where a set's SMDs land: the mirrored subtree, or flat under the root.</summary>
    public static string ResolveOutputDir(ModelSet modelSet, string inputRoot, string outputRoot, bool preserveFolders) =>
        preserveFolders && Relative(modelSet.BaseDir, inputRoot) is { } relative ? Path.GetFullPath(Path.Combine(outputRoot, relative)) : outputRoot;

    /// <summary>The QC's <c>$modelname</c>: an explicit prefix, else the output subtree.</summary>
    public static string ResolveModelname(string name, string outDir, string outputRoot, string prefix = "")
    {
        prefix = prefix.Trim().Replace('\\', '/');
        if (prefix.Length > 0) return (prefix.EndsWith('/') ? prefix : prefix + "/") + name + ".mdl";
        if (Relative(outDir, outputRoot) is not { } relative) return $"{name}.mdl";
        var text = relative.Replace('\\', '/');
        return text == "." ? $"{name}.mdl" : $"{text}/{name}.mdl";
    }

    /// <summary>Outputs already on disk that a run would overwrite.</summary>
    public static List<string> ExistingOutputs(ModelSet modelSet, string outDir, ConversionOptions options)
    {
        var found = new List<string>();
        void Check(string path)
        {
            if (Path.Exists(path)) found.Add(path);
        }
        Check(Path.Combine(outDir, $"{modelSet.Name}.smd"));
        if (modelSet.PhysicsPath is not null && options.ExportPhysics) Check(Path.Combine(outDir, $"{modelSet.Name}_physics.smd"));
        if (options.GenerateQc) Check(Path.Combine(outDir, $"{modelSet.Name}.qc"));
        return found;
    }

    /// <summary>Per-vertex normals averaged across every face touching that position.</summary>
    /// <remarks>Source reads vertex normals to decide how to decompose a collision mesh into convex pieces, and
    /// physics glTFs arrive pre-split with hard edges, so normals must be smoothed across the split seams. That
    /// cannot go by connected faces, because the pieces are disconnected. A face reaching one position through
    /// two of its vertices still counts once.</remarks>
    public static V3[]? SmoothNormalsByPosition(ModelMesh mesh, double tolerance = 1e-5)
    {
        if (mesh.Vertices.Length == 0 || mesh.FaceCount == 0) return null;
        V3[] faceNormals;
        try
        {
            faceNormals = mesh.FaceNormals();
        }
        catch (IndexOutOfRangeException)
        {
            return null;
        }
        var groups = new Dictionary<(long, long, long), int>();
        var inverse = new int[mesh.Vertices.Length];
        for (var index = 0; index < inverse.Length; index++)
        {
            var (x, y, z) = mesh.Vertices[index];
            var key = ((long)Math.Round(x / tolerance), (long)Math.Round(y / tolerance), (long)Math.Round(z / tolerance));
            if (!groups.TryGetValue(key, out var group)) groups[key] = group = groups.Count;
            inverse[index] = group;
        }
        var accumulated = new V3[groups.Count];
        for (var face = 0; face < faceNormals.Length; face++)
        {
            int a = inverse[mesh.Faces[face * 3]], b = inverse[mesh.Faces[face * 3 + 1]], c = inverse[mesh.Faces[face * 3 + 2]];
            accumulated[a] += faceNormals[face];
            if (b != a) accumulated[b] += faceNormals[face];
            if (c != a && c != b) accumulated[c] += faceNormals[face];
        }
        var normals = new V3[inverse.Length];
        for (var index = 0; index < normals.Length; index++)
        {
            var sum = accumulated[inverse[index]];
            var length = sum.Length;
            normals[index] = length > 1e-12 ? sum / length : V3.Zero;
        }
        return normals;
    }

    /// <summary>Whether a clip name reads as looping motion.</summary>
    public static bool IsLoopClip(string name)
    {
        var lower = name.ToLowerInvariant();
        return new[] { "idle", "loop", "walk", "run", "breathe", "cycle" }.Any(pattern => lower.Contains(pattern, StringComparison.Ordinal));
    }

    /// <summary>Makes a clip name safe to use inside an SMD filename.</summary>
    public static string SanitizeClipFilename(string name)
    {
        var safe = string.Concat(name.Select(character => char.IsLetterOrDigit(character) || char.GetUnicodeCategory(character) is System.Globalization.UnicodeCategory.OtherNumber or System.Globalization.UnicodeCategory.LetterNumber || character is '_' or '-' ? character : '_')).Trim('_');
        return safe.Length > 0 ? safe : "clip";
    }

    private static ConversionResult Failed(ConversionResult result, string reason, Stopwatch started)
    {
        result.Status = "failed";
        result.Reason = reason;
        result.Elapsed = started.Elapsed.TotalSeconds;
        return result;
    }

    /// <summary>Converts one glTF model set to SMDs and a QC.</summary>
    /// <remarks>Source 2 Viewer's conversion turns inches into metres and swaps the vmdl Z-up basis to glTF Y-up.
    /// Where it lives decides what is left to undo: on a node the geometry never saw it and needs neither, baked
    /// into the vertices it needs both, in its own basis rather than the +90 degrees about X a Blender export
    /// wants. A forced manual scale overrides only the scale.</remarks>
    public static ConversionResult ConvertModelSet(ModelSet modelSet, ConversionOptions options, string inputRoot, string outputRoot, PortContext? context = null)
    {
        var started = Stopwatch.StartNew();
        var name = modelSet.Name;
        var result = new ConversionResult(name) { Surfaceprop = options.StaticSurfaceprop, Skins = modelSet.Skins.Count };
        var outDir = ResolveOutputDir(modelSet, inputRoot, outputRoot, options.PreserveFolders);
        result.OutputDir = outDir;
        result.HasPhysics = modelSet.PhysicsPath is not null;
        void Log(string message) => context?.Log($"{name}: {message}");
        void Verbose(string message)
        {
            if (options.Verbose) Log(message);
        }
        context?.Check();
        if (!options.ReplaceExisting)
        {
            var clashes = ExistingOutputs(modelSet, outDir, options);
            if (clashes.Count > 0)
            {
                result.Status = "skipped";
                result.Reason = $"{clashes.Count} output(s) already exist";
                result.Elapsed = started.Elapsed.TotalSeconds;
                return result;
            }
        }
        if (options.DryRun)
        {
            result.Status = "dry-run";
            result.Reason = $"would write to {outDir}";
            result.Elapsed = started.Elapsed.TotalSeconds;
            return result;
        }
        try
        {
            Directory.CreateDirectory(outDir);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            return Failed(result, $"could not create {outDir}: {error.Message}", started);
        }
        var (ok, missing) = GltfMeshLoader.PreflightCheck(modelSet.RenderPath);
        if (!ok) return Failed(result, $"missing dependencies: {string.Join(", ", missing)}", started);
        var (renderMesh, loadError) = GltfMeshLoader.LoadMesh(modelSet.RenderPath);
        if (renderMesh is null) return Failed(result, $"could not load {Path.GetFileName(modelSet.RenderPath)}: {loadError}", started);
        context?.Check();
        double renderScale;
        var manual = options.ForceManualScale ? " (manual override)" : "";
        if (renderMesh.NativeUnits)
        {
            renderScale = options.ForceManualScale ? options.Scale : 1.0;
            Log($"Source-units glTF detected, using scale={Fmt.G(renderScale)}{manual}");
        }
        else if (renderMesh.MetreUnits)
        {
            renderScale = options.ForceManualScale ? options.Scale : GltfDocument.SourceUnitsPerMetre;
            Log($"Source 2 Viewer metre glTF detected, using scale={Fmt.G(renderScale)}{manual}");
        }
        else renderScale = options.Scale;

        GltfSkin? skin = null;
        var clips = new List<GltfClip>();
        int[]? vertexJoints = null;
        float[]? vertexWeights = null;
        if (!options.Rigid && renderMesh.GltfData.Get("skins").Bool())
        {
            var document = GltfDocument.Read(modelSet.RenderPath);
            skin = GltfSkin.Parse(document);
            if (skin is not null)
            {
                (vertexJoints, vertexWeights) = SmdExporter.ParseSkinVertexData(document);
                if (options.ExportAnimations && document.Root.Get("animations").Bool()) clips = GltfClip.ParseAll(document, skin);
            }
        }
        var hasSkin = skin is not null && vertexJoints is not null && vertexWeights is not null;
        var isAnimated = hasSkin && clips.Count > 0;
        result.Bones = hasSkin ? skin!.Joints.Count : 0;

        renderMesh.ApplyScale(renderScale);
        var applySwap = options.AxisConversion && !renderMesh.NativeUnits;
        if (applySwap)
        {
            if (renderMesh.MetreUnits) MeshProcessor.ApplySource2AxisConversion(renderMesh);
            else MeshProcessor.ApplyAxisConversion(renderMesh);
        }
        if (!hasSkin) MeshProcessor.Sanitize(renderMesh);
        result.Triangles = renderMesh.FaceCount;
        result.Vertices = renderMesh.Vertices.Length;
        var coordinates = new CoordinateMode(renderScale, applySwap, renderMesh.MetreUnits ? GltfDocument.Source2BasisQuaternion : null);
        var animations = new List<AnimMeta>();
        context?.Check();

        var renderSmd = Path.Combine(outDir, $"{name}.smd");
        if (options.GenerateSmd)
        {
            if (hasSkin)
            {
                var (success, warning) = SmdExporter.WriteSkinned(renderMesh, skin!, vertexJoints, vertexWeights, renderSmd, name, coordinates, options.FlipV, uvMode: options.UvMode);
                if (!success) return Failed(result, $"skinned SMD failed: {warning}", started);
                if (warning.Length > 0)
                {
                    result.Warnings.Add(warning);
                    Verbose(warning);
                }
                result.Files.Add(renderSmd);
                Log($"skinned reference SMD, {result.Bones} bones" + (isAnimated ? "" : ", no clips"));
                animations = WriteAnimations(modelSet, skin!, clips, outDir, coordinates, options, result, Log, context);
            }
            else
            {
                var (success, warning) = SmdExporter.WriteStatic(renderMesh, renderSmd, name, options.FlipV, false, uvMode: options.UvMode);
                if (!success) return Failed(result, "static SMD failed", started);
                if (warning.Length > 0)
                {
                    result.Warnings.Add(warning);
                    Verbose(warning);
                }
                result.Files.Add(renderSmd);
            }
        }
        result.Clips = animations.Count;

        var physicsMesh = BuildPhysicsMesh(modelSet, renderMesh, options, result, Log, Verbose, context);
        if (physicsMesh is not null && options.GenerateSmd)
        {
            var physicsSmd = Path.Combine(outDir, $"{name}_physics.smd");
            var rootBind = hasSkin ? SmdSkeleton.RootBind(skin!, coordinates) : ((V3 Position, (double Pitch, double Yaw, double Roll) Angles)?)null;
            var (success, warning) = SmdExporter.WriteStatic(physicsMesh, physicsSmd, PhysicsMaterial, options.FlipV, false, SmoothNormalsByPosition(physicsMesh), options.UvMode, rootBind);
            if (success)
            {
                result.Files.Add(physicsSmd);
                if (warning.Length > 0) Verbose($"(physics) {warning}");
            }
            else
            {
                physicsMesh = null;
                result.Warnings.Add("physics SMD failed");
                Log("physics SMD failed");
            }
        }
        context?.Check();

        var surfaceprop = ChooseSurfaceprop(modelSet, options, result, Log, Verbose);
        result.Surfaceprop = surfaceprop;

        var collision = physicsMesh is not null ? MeshProcessor.MeasureCollision(physicsMesh, options.WeldDistance) : new CollisionModel();
        double? mass = null;
        if (physicsMesh is not null && options.AutoMass) mass = MeshProcessor.CalculateSurfaceMass(collision, surfaceprop, options.MassModifier);
        else if (!options.AutoMass) mass = options.StaticMass;
        result.Mass = mass;

        if (physicsMesh is not null)
        {
            var (density, thickness) = StaticCollision.SurfacepropDensity(surfaceprop);
            var volume = collision.Pieces.Sum(piece => piece.Volume);
            var area = collision.Pieces.Sum(piece => piece.Area);
            Verbose($"collision: {collision.Pieces.Count} convex piece(s), volume {Fmt.Fixed(volume, 1)}in3, area {Fmt.Fixed(area, 1)}in2, surfaceprop '{surfaceprop}' ({Fmt.G(density)}kg/m3" +
                (thickness != 0 ? $", {Fmt.G(thickness)}in shell)" : ", solid)") + (mass is { } shown && shown != 0 ? $" -> $mass {Fmt.Fixed(shown, 2)}" : ""));
            foreach (var warning in StaticCollision.CheckCollisionModel(collision, mass ?? 0.0))
            {
                result.Warnings.Add(warning);
                Log($"collision: {warning}");
            }
        }

        if (options.GenerateQc)
        {
            var qcPath = Path.Combine(outDir, $"{name}.qc");
            QcWriter.WriteQc(qcPath, new()
            {
                Name = name,
                Modelname = ResolveModelname(name, outDir, outputRoot, options.ModelnamePrefix),
                Cdmaterials = options.Cdmaterials,
                Surfaceprop = surfaceprop,
                HasPhysics = physicsMesh is not null,
                Concave = options.Concave,
                Mass = mass,
                PieceCount = Math.Max(collision.Pieces.Count, 1),
                Remove2d = options.Remove2dCollision,
                Skin = hasSkin ? skin : null,
                Coordinates = hasSkin ? coordinates : null,
                Animations = hasSkin ? animations : null,
                MaterialSkins = modelSet.Skins.Count > 0 ? modelSet.Skins : null,
                VertCount = result.Vertices
            });
            result.Files.Add(qcPath);
        }
        result.Elapsed = started.Elapsed.TotalSeconds;
        return result;
    }

    private static string ChooseSurfaceprop(ModelSet modelSet, ConversionOptions options, ConversionResult result, Action<string> log, Action<string> verbose)
    {
        var name = modelSet.Name;
        if (Surfaceprops.SurfacepropOverrideFor(name, options.SurfacepropOverride) is { Length: > 0 } forced)
        {
            verbose($"$surfaceprop forced to '{forced}'");
            if (StaticCollision.ResolveSurfaceprop(forced) == "default" && !forced.Equals("default", StringComparison.OrdinalIgnoreCase))
            {
                var message = $"'{forced}' is not a surfaceprop this kit knows, so $mass falls back to the default density";
                result.Warnings.Add(message);
                log(message);
            }
            return forced;
        }
        if (!options.AutoSurfaceprop) return options.StaticSurfaceprop;
        var declared = Surfaceprops.GltfSurfaceprops(modelSet.PhysicsPath);
        var source = Surfaceprops.Strongest(declared);
        var surfaceprop = SurfacepropDetector.Detect(name, source2Prop: source);
        if (!string.IsNullOrEmpty(source) && StaticCollision.ResolveSurfaceprop(source) != "default")
        {
            verbose($"$surfaceprop '{surfaceprop}' from the source's '{source}'");
            if (declared.Count > 1)
            {
                var message = $"collision declares {declared.Count} surface properties ({string.Join(", ", declared.Keys)}); Source 1 takes one per model, so '{source}' won on coverage";
                result.Warnings.Add(message);
                log(message);
            }
        }
        else if (!string.IsNullOrEmpty(source) && !source.Equals("default", StringComparison.OrdinalIgnoreCase))
            verbose($"the source's '{source}' has no Source 1 equivalent; reading the name instead");
        return surfaceprop;
    }

    /// <summary>Writes one SMD per clip, deduplicated by sanitized name.</summary>
    /// <remarks>Source 2 Viewer exports some clips twice, once under an <c>@</c>-prefixed alias that sanitizes to
    /// the same filename. Writing both would emit duplicate <c>$animation</c> entries, which studiomdl rejects.</remarks>
    private static List<AnimMeta> WriteAnimations(ModelSet modelSet, GltfSkin skin, List<GltfClip> clips, string outDir, CoordinateMode coordinates,
        ConversionOptions options, ConversionResult result, Action<string> log, PortContext? context)
    {
        var animations = new List<AnimMeta>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var clip in clips)
        {
            context?.Check();
            var safe = SanitizeClipFilename(clip.Name);
            if (!seen.Add(safe))
            {
                log($"skipping duplicate clip {PythonText.Repr(clip.Name)} (resolves to {PythonText.Repr(safe)})");
                continue;
            }
            var path = Path.Combine(outDir, $"{modelSet.Name}_anim_{safe}.smd");
            var (frames, count) = clip.Sample(skin, options.AnimationFps);
            try
            {
                SmdSkeleton.WriteAnimation(skin, frames, path, coordinates);
            }
            catch (Exception error) when (error is InvalidDataException or IOException or UnauthorizedAccessException)
            {
                result.Warnings.Add($"animation {PythonText.Repr(clip.Name)}: Animation SMD write failed: {error.Message}");
                log($"animation {PythonText.Repr(clip.Name)} failed: Animation SMD write failed: {error.Message}");
                continue;
            }
            result.Files.Add(path);
            animations.Add(new(safe, Path.GetFileName(path), count, options.AnimationFps, options.AutoLoopDetect && IsLoopClip(clip.Name)));
        }
        if (animations.Count > 0) log($"wrote {animations.Count} animation SMD(s)");
        return animations;
    }

    /// <summary>Makes every element of a collision mesh something studiomdl can build.</summary>
    /// <remarks>Two things stop it, both from the same place: a Source 2 mesh shape is a surface, and Source 1 only
    /// has solids. An open frame's hull fills the opening it describes and no convex decomposition reopens it. And
    /// a piece too thin to make a solid of makes studiomdl hull the entire model into a single piece.</remarks>
    private static ModelMesh RepairCollision(ModelMesh mesh, ConversionOptions options, ConversionResult result, Action<string> log)
    {
        var weld = Math.Max(options.WeldDistance, 1e-4);
        var shells = StaticCollision.SlabOpenShells(mesh.ToTriMesh(), weld);
        if (shells is { Replaced: > 0, Mesh: not null })
        {
            var message = $"rebuilt {shells.Replaced} hollow open surface(s) as {shells.Slabs} slab(s) - hulling one fills the opening it describes, and no cut reopens it";
            result.Warnings.Add(message);
            log($"collision: {message}");
            mesh = ModelMesh.FromTriMesh(shells.Mesh);
        }
        var repair = StaticCollision.ThickenThinPieces(mesh.ToTriMesh(), weld);
        if (repair is not { Mesh: not null } || (repair.Thickened == 0 && repair.Dropped == 0)) return mesh;
        var parts = new List<string>();
        if (repair.Thickened > 0)
            parts.Add($"thickened {repair.Thickened} collision piece(s) to {Fmt.G(StaticCollision.ThickenTargetIn)}in (thinnest was {Fmt.Fixed(repair.Thinnest, 3)}in)");
        if (repair.Dropped > 0) parts.Add($"dropped {repair.Dropped} with no shape to thicken");
        var report = string.Join("; ", parts) + " - one piece under half an inch makes studiomdl hull the whole model into a single convex piece";
        result.Warnings.Add(report);
        log($"collision: {report}");
        return ModelMesh.FromTriMesh(repair.Mesh);
    }

    /// <summary>Cuts a concave collision mesh into pieces studiomdl can represent.</summary>
    /// <remarks>studiomdl hulls each connected element of the collision SMD, so a doorframe, grate or cable run
    /// compiles as a solid block with its openings filled in. Nothing in the compile reports that, so it has
    /// to be dealt with before the SMD is written.</remarks>
    private static ModelMesh DecomposeCollision(ModelMesh mesh, ConversionOptions options, ConversionResult result, Action<string> log, Action<string> verbose)
    {
        if (!options.DecomposeCollision) return mesh;
        if (mesh.FaceCount > MaxDecomposeFaces)
        {
            verbose($"collision decomposition skipped: {mesh.FaceCount} faces");
            return mesh;
        }
        var split = StaticCollision.DecomposeConcave(mesh.ToTriMesh(), Math.Max(1, options.MaxConvexPieces));
        if (split.PiecesAfter <= split.PiecesBefore || split.Mesh is null) return mesh;
        var recovered = split.VolumeBefore <= 0 ? 0.0 : 1.0 - split.VolumeAfter / split.VolumeBefore;
        log($"collision: cut {split.PiecesBefore} concave piece(s) into {split.PiecesAfter}, {Fmt.Fixed(recovered * 100, 0)}% of the hulled-over volume given back");
        if (split.Unsplit > 0)
        {
            var message = $"{split.Unsplit} collision piece(s) still concave at the {options.MaxConvexPieces}-piece budget";
            result.Warnings.Add(message);
            log($"collision: {message}");
        }
        return ModelMesh.FromTriMesh(split.Mesh);
    }

    /// <summary>The collision mesh: the physics glTF, or the render mesh as a fallback.</summary>
    /// <returns>Null means no <c>$collisionmodel</c> at all: physics disabled, or too large to give vphysics a hull safely.</returns>
    /// <remarks>
    /// <para>vphysics can crash the engine on collision meshes past the size threshold (Citadel, skyboxes,
    /// drop-ships), and huge static set-pieces are better off with no collision anyway.</para>
    /// <para>Thickening comes last: a cut can leave a sliver of its own, so the pass that guarantees every piece is
    /// a solid has to see the pieces that will actually be written.</para>
    /// <para>A stub physics glTF is not a failure to read one. It is Source 2 Viewer writing out that the model has
    /// no collision, which is how Half-Life Alyx ships decoration. Invented collision then earns the same passes a
    /// real one gets: without decomposition a render mesh is hulled per connected component, and a shell's hull is
    /// the box around it, which is the whole complaint about inventing collision in the first place.</para>
    /// </remarks>
    private static ModelMesh? BuildPhysicsMesh(ModelSet modelSet, ModelMesh renderMesh, ConversionOptions options, ConversionResult result,
        Action<string> log, Action<string> verbose, PortContext? context)
    {
        if (!options.ExportPhysics) return null;
        if (options.SkipPhysicsForLarge)
        {
            var extent = renderMesh.LargestExtent();
            if (extent > options.LargeModelThreshold)
            {
                var message = $"skipping physics - bbox {Fmt.Fixed(extent, 0)}u exceeds the {Fmt.Fixed(options.LargeModelThreshold, 0)}u threshold";
                result.Warnings.Add(message);
                log(message);
                return null;
            }
        }
        ModelMesh? loaded = null;
        var failure = "";
        if (modelSet.PhysicsPath is not null)
        {
            var (ok, missing) = GltfMeshLoader.PreflightCheck(modelSet.PhysicsPath);
            if (!ok) failure = $"missing dependencies: {string.Join(", ", missing)}";
            else
            {
                (loaded, var error) = GltfMeshLoader.LoadMesh(modelSet.PhysicsPath);
                if (loaded is null) failure = error.Length > 0 ? error : "empty physics glTF";
            }
        }
        if (loaded is not null)
        {
            var scale = loaded.NativeUnits && !options.ForceManualScale ? 1.0
                : loaded.MetreUnits && !options.ForceManualScale ? GltfDocument.SourceUnitsPerMetre
                : options.Scale;
            loaded.ApplyScale(scale);
            if (options.AxisConversion && !loaded.NativeUnits)
            {
                if (loaded.MetreUnits) MeshProcessor.ApplySource2AxisConversion(loaded);
                else MeshProcessor.ApplyAxisConversion(loaded);
            }
            verbose($"physics mesh: {loaded.FaceCount} faces");
            context?.Check();
            return RepairCollision(DecomposeCollision(loaded, options, result, log, verbose), options, result, log);
        }
        if (!GltfMeshLoader.DeclaresCollision(modelSet.PhysicsPath) && !options.CollisionFromRender)
        {
            const string message = "no collision in the source, so none written - the prop is not solid, which is what it is in Source 2 too";
            result.Warnings.Add(message);
            log(message);
            return null;
        }
        var fallback = renderMesh.Copy();
        fallback.FaceMaterials = null;
        fallback.MaterialNames = null;
        var reason = failure.Length > 0 ? failure : "no collision in the source";
        result.PhysicsFallback = true;
        result.Warnings.Add($"collision built from the render mesh ({reason})");
        log($"{reason}; building collision from the render mesh instead");
        context?.Check();
        return RepairCollision(DecomposeCollision(fallback, options, result, log, verbose), options, result, log);
    }
}
