using System.Buffers.Binary;
using System.Reflection;
using System.Resources;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>Exact native serialization is unavailable or failed independent geometry verification.</summary>
public sealed class GeometryFinalizationException(string message) : Exception(message);

/// <summary>Preserves prepared convex geometry through the installed native VPhysics serializer.</summary>
/// <remarks>
/// studiomdl rebuilds every convex, so a compiled ragdoll drifts from the source shapes. The fix replaces the compact
/// surfaces in the .phy with ones serialized from the exact prepared polyhedra. That needs the compiler's own 32-bit
/// vphysics, and this add-on runs 64-bit, so the work happens in a separate x86 helper process. The helper is built on
/// first use from a source carried in this assembly, using the .NET Framework compiler that ships with Windows, and is
/// cached by content hash. It is not part of the add-on's own compile because it targets a different framework and bitness.
/// </remarks>
public static class PhyGeometry
{
    private const string ResourceSuffix = ".ConvexSerializerSource.resources";
    private const string ResourceKey = "convex_serializer";

    private static string Digest(string path)
    {
        using var stream = File.OpenRead(path);
        return Convert.ToHexStringLower(SHA256.HashData(stream));
    }

    /// <summary>The packaged x86 bridge source used by the exact collision finalizer.</summary>
    public static byte[] SerializerSource()
    {
        var assembly = Assembly.GetExecutingAssembly();
        var name = assembly.GetManifestResourceNames().FirstOrDefault(item => item.EndsWith(ResourceSuffix, StringComparison.Ordinal))
            ?? throw new GeometryFinalizationException("The packaged native helper source is missing; reinstall the porting kit");
        using var stream = assembly.GetManifestResourceStream(name)!;
        using var reader = new ResourceReader(stream);
        reader.GetResourceData(ResourceKey, out _, out var data);
        var length = 0;
        var at = 0;
        var shift = 0;
        while (true)
        {
            var part = data[at++];
            length |= (part & 0x7F) << shift;
            if ((part & 0x80) == 0) break;
            shift += 7;
        }
        return data.AsSpan(at, length).ToArray();
    }

    /// <summary>Recognizes complete x86 Windows PE files used by the bridge and compiler library.</summary>
    public static bool IsX86Binary(string path)
    {
        if (!File.Exists(path)) return false;
        var binary = File.ReadAllBytes(path);
        if (binary.Length < 64 || binary[0] != (byte)'M' || binary[1] != (byte)'Z') return false;
        var header = (long)BinaryPrimitives.ReadUInt32LittleEndian(binary.AsSpan(60));
        return header + 24 <= binary.Length && binary.AsSpan((int)header, 4).SequenceEqual("PE\0\0"u8) &&
            BinaryPrimitives.ReadUInt16LittleEndian(binary.AsSpan((int)header + 4)) == 0x14C;
    }

    /// <summary>Runs an isolated helper with cancellation and a bounded lifetime while collecting small diagnostics.</summary>
    private static string Run(string executable, IReadOnlyList<string> arguments, string directory, PortContext context, double timeoutSeconds = 180)
    {
        var message = new StringBuilder();
        ProcessResult outcome;
        try
        {
            outcome = context.Processes.RunAsync(new(executable, arguments, directory, TimeSpan.FromSeconds(timeoutSeconds)),
                line => message.AppendLine(line.Text), context.Token).GetAwaiter().GetResult();
        }
        catch (TimeoutException)
        {
            throw new GeometryFinalizationException("Native collision serialization timed out; the existing PHY was preserved");
        }
        var text = message.ToString().Trim();
        if (outcome.ExitCode != 0)
            throw new GeometryFinalizationException($"Native collision helper exited with {outcome.ExitCode}: {(text.Length > 3000 ? text[^3000..] : text)}");
        return text;
    }

    /// <summary>Builds the content-addressed bridge with Windows .NET and selects the compiler's x86 physics library.</summary>
    private static (string Executable, string Library, JsonObject Provenance) Helper(string compiler, string cache, PortContext context)
    {
        if (!OperatingSystem.IsWindows())
            throw new GeometryFinalizationException("Exact physics serialization currently requires Windows and an x86 Source compiler VPhysics DLL");
        var source = SerializerSource();
        var framework = Path.Combine(Environment.GetEnvironmentVariable("WINDIR") ?? "C:/Windows", "Microsoft.NET", "Framework", "v4.0.30319", "csc.exe");
        if (!File.Exists(framework))
            throw new GeometryFinalizationException("Exact physics serialization needs the Windows .NET Framework 4 C# compiler; install/enable .NET Framework 4.8");
        compiler = Path.GetFullPath(compiler);
        var folder = Path.GetDirectoryName(compiler) ?? "";
        string[] names = Path.GetFileName(compiler).StartsWith("ce", StringComparison.OrdinalIgnoreCase) ? ["cevphysics.dll", "vphysics.dll"] : ["vphysics.dll", "cevphysics.dll"];
        var library = names.Select(name => Path.Combine(folder, name)).FirstOrDefault(IsX86Binary)
            ?? throw new GeometryFinalizationException($"No compatible x86 vphysics.dll/cevphysics.dll was found beside {compiler}; select the 32-bit StudioMDL CE installation");
        var sourceHash = Convert.ToHexStringLower(SHA256.HashData(source));
        var frameworkHash = Digest(framework);
        var libraryHash = Digest(library);
        var identity = Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(sourceHash + frameworkHash)))[..20];
        var directory = Path.Combine(Path.GetFullPath(cache), identity);
        Directory.CreateDirectory(directory);
        var executable = Path.Combine(directory, "convex_serializer.exe");
        var sourcePath = Path.Combine(directory, "convex_serializer.cs");
        if (!IsX86Binary(executable))
        {
            File.WriteAllBytes(sourcePath, source);
            var scratch = Directory.CreateTempSubdirectory(Path.Combine(directory, "csharp-")).FullName;
            try
            {
                var candidate = Path.Combine(scratch, "convex_serializer.exe");
                Run(framework, ["/nologo", "/optimize+", "/platform:x86", "/out:" + candidate, sourcePath], directory, context, 60);
                if (!IsX86Binary(candidate)) throw new GeometryFinalizationException("The C# compiler did not produce a complete x86 collision helper");
                File.Move(candidate, executable, true);
            }
            finally
            {
                Directory.Delete(scratch, true);
            }
        }
        return (executable, library, new JsonObject
        {
            ["source"] = sourcePath, ["source_sha256"] = sourceHash, ["helper"] = executable, ["helper_sha256"] = Digest(executable),
            ["csharp_compiler"] = framework, ["csharp_compiler_sha256"] = frameworkHash, ["physics_library"] = library, ["physics_library_sha256"] = libraryHash,
            ["compiler"] = compiler, ["compiler_sha256"] = Digest(compiler), ["interface"] = "VPhysicsCollision007", ["pointer_bits"] = 32,
            ["construction"] = "ConvexFromConvexPolyhedron", ["serialization"] = "ConvertConvexToCollide/CollideWrite"
        });
    }

    /// <summary>Encodes a closed convex triangulation in the SDK CPolyhedron array layout with clockwise outward faces.</summary>
    public static (byte[] Encoded, TriMesh Hull) Polyhedron(IReadOnlyList<V3> points)
    {
        var hull = ConvexHull.Of(points);
        if (hull is null || !MeshOps.IsWatertight(hull) || !IsConvex(hull) || !hull.Vertices.All(vertex => vertex.IsFinite) || MeshOps.Volume(hull) <= 0)
            throw new GeometryFinalizationException("Prepared physics shape is not a finite closed positive-volume convex hull");
        var normals = MeshOps.FaceNormals(hull);
        var edges = new Dictionary<(int, int), int>();
        var edgeList = new List<(int, int)>();
        var references = new List<(int Index, int Endpoint)>();
        var polygons = new List<(int Start, V3 Normal)>();
        for (var face = 0; face < hull.FaceCount; face++)
        {
            int[] corners = [hull.Faces[face * 3 + 2], hull.Faces[face * 3 + 1], hull.Faces[face * 3]];
            var start = references.Count;
            for (var corner = 0; corner < 3; corner++)
            {
                int first = corners[corner], second = corners[(corner + 1) % 3];
                var edge = first < second ? (first, second) : (second, first);
                if (!edges.TryGetValue(edge, out var index))
                {
                    index = edges.Count;
                    edges[edge] = index;
                    edgeList.Add(edge);
                }
                references.Add((index, second != edge.Item1 ? 1 : 0));
            }
            polygons.Add((start, normals[face]));
        }
        int[] counts = [hull.Vertices.Length, edges.Count, references.Count, polygons.Count];
        if (counts.Any(count => count > 65535)) throw new GeometryFinalizationException($"Prepared convex exceeds SDK 16-bit polyhedron counts: ({string.Join(", ", counts)})");
        using var stream = new MemoryStream();
        using var writer = new BinaryWriter(stream);
        foreach (var count in counts) writer.Write((ushort)count);
        foreach (var vertex in hull.Vertices)
        {
            writer.Write((float)vertex.X);
            writer.Write((float)vertex.Y);
            writer.Write((float)vertex.Z);
        }
        foreach (var (first, second) in edgeList)
        {
            writer.Write((ushort)first);
            writer.Write((ushort)second);
        }
        foreach (var (index, endpoint) in references)
        {
            writer.Write((ushort)index);
            writer.Write((byte)endpoint);
            writer.Write((byte)0);
        }
        foreach (var (start, normal) in polygons)
        {
            writer.Write((ushort)start);
            writer.Write((ushort)3);
            writer.Write((float)normal.X);
            writer.Write((float)normal.Y);
            writer.Write((float)normal.Z);
        }
        writer.Flush();
        return (stream.ToArray(), hull);
    }

    /// <summary>Whether a closed mesh is convex, by the height of each face's opposite vertex above its neighbour.</summary>
    /// <remarks>Same test as trimesh, with its scale-relative threshold and zero-area faces left out.</remarks>
    public static bool IsConvex(TriMesh mesh)
    {
        const double planar = 1e-5;
        var (low, high) = MeshOps.Bounds(mesh);
        var threshold = planar * (high - low).Length;
        var normals = MeshOps.FaceNormals(mesh);
        var areas = new double[mesh.FaceCount];
        for (var face = 0; face < areas.Length; face++) areas[face] = MeshOps.FaceCross(mesh, face).Length / 2.0;
        var owners = new Dictionary<(int, int), (int Face, int Opposite)>();
        var tested = false;
        for (var face = 0; face < mesh.FaceCount; face++)
            for (var corner = 0; corner < 3; corner++)
            {
                int first = mesh.Faces[face * 3 + corner], second = mesh.Faces[face * 3 + (corner + 1) % 3], opposite = mesh.Faces[face * 3 + (corner + 2) % 3];
                var edge = first < second ? (first, second) : (second, first);
                if (!owners.TryGetValue(edge, out var other))
                {
                    owners[edge] = (face, opposite);
                    continue;
                }
                if (areas[face] <= 1e-12 || areas[other.Face] <= 1e-12) continue;
                tested = true;
                if ((mesh.Vertices[opposite] - mesh.Vertices[first]).Dot(normals[other.Face]) >= threshold) return false;
                if ((mesh.Vertices[other.Opposite] - mesh.Vertices[first]).Dot(normals[face]) >= threshold) return false;
            }
        return tested;
    }

    /// <summary>Decodes every terminal serialized ledge including face connectivity after bounded cloud validation.</summary>
    private static List<TriMesh> SolidMeshes(byte[] blob)
    {
        PhyAudit.ConvexClouds(blob);
        var root = 28 + (long)BinaryPrimitives.ReadUInt32LittleEndian(blob.AsSpan(60));
        var pending = new Stack<long>([root]);
        var meshes = new List<TriMesh>();
        while (pending.TryPop(out var node))
        {
            var right = BinaryPrimitives.ReadInt32LittleEndian(blob.AsSpan((int)node));
            var ledge = BinaryPrimitives.ReadInt32LittleEndian(blob.AsSpan((int)node + 4));
            if (right != 0)
            {
                pending.Push(node + 28);
                pending.Push(node + right);
                continue;
            }
            var offset = (int)node + ledge;
            var relative = BinaryPrimitives.ReadInt32LittleEndian(blob.AsSpan(offset));
            int count = BinaryPrimitives.ReadUInt16LittleEndian(blob.AsSpan(offset + 12));
            var triangles = new int[count * 3];
            for (var triangle = 0; triangle < count; triangle++)
                for (var corner = 0; corner < 3; corner++)
                    triangles[triangle * 3 + corner] = BinaryPrimitives.ReadUInt16LittleEndian(blob.AsSpan(offset + 20 + triangle * 16 + corner * 4));
            var indices = triangles.Distinct().Order().ToArray();
            var vertices = Array.ConvertAll(indices, index => PhyAudit.ReadPoint(blob, offset + relative + index * 16));
            meshes.Add(new(vertices, Array.ConvertAll(triangles, index => Array.BinarySearch(indices, index))));
        }
        return meshes;
    }

    private static (double Distance, int[] Mapping) Nearest(V3[] tree, V3[] queries)
    {
        var mapping = new int[queries.Length];
        var furthest = 0.0;
        for (var query = 0; query < queries.Length; query++)
        {
            var best = double.PositiveInfinity;
            for (var candidate = 0; candidate < tree.Length; candidate++)
            {
                var offset = tree[candidate] - queries[query];
                var distance = offset.Dot(offset);
                if (distance >= best) continue;
                best = distance;
                mapping[query] = candidate;
            }
            furthest = Math.Max(furthest, Math.Sqrt(best));
        }
        return (furthest, mapping);
    }

    /// <summary>Bounds convex surface Hausdorff distance by the symmetric nearest-vertex distances of two hulls.</summary>
    private static (double Error, int[] Mapping) ShapeError(TriMesh expected, TriMesh actual)
    {
        var (distance, mapping) = Nearest(expected.Vertices, actual.Vertices);
        return (Math.Max(distance, Nearest(actual.Vertices, expected.Vertices).Distance), mapping);
    }

    private static HashSet<(int, int, int)> FaceSet(TriMesh mesh, int[]? mapping = null)
    {
        var faces = new HashSet<(int, int, int)>();
        for (var face = 0; face < mesh.FaceCount; face++)
        {
            int[] corners = [mesh.Faces[face * 3], mesh.Faces[face * 3 + 1], mesh.Faces[face * 3 + 2]];
            if (mapping is not null)
                for (var corner = 0; corner < 3; corner++) corners[corner] = mapping[corners[corner]];
            Array.Sort(corners);
            faces.Add((corners[0], corners[1], corners[2]));
        }
        return faces;
    }

    /// <summary>Matches and independently verifies every serialized source shape, surface bound, volume and triangulation.</summary>
    private static JsonObject GeometryReport(string name, List<TriMesh> expected, byte[] blob, double maximumError, double maximumVolumeError, float[]? nativeBounds)
    {
        var actual = SolidMeshes(blob);
        var (low, high) = MeshOps.Bounds(expected.SelectMany(mesh => mesh.Vertices));
        double? nativeBoundsError = null;
        if (nativeBounds is not null)
        {
            var error = 0.0;
            for (var axis = 0; axis < 3; axis++)
                error = Math.Max(error, Math.Max(Math.Abs(nativeBounds[axis] - low[axis]), Math.Abs(nativeBounds[3 + axis] - high[axis])));
            nativeBoundsError = error;
            if (!nativeBounds.All(float.IsFinite) || error > maximumError)
                throw new GeometryFinalizationException($"{name}: native collision support AABB differs by {PhysicsMaths.General(error, 9)} in; invalid convex edge traversal");
        }
        if (expected.Count != actual.Count) throw new GeometryFinalizationException($"{name}: native serialization changed convex separation {expected.Count}→{actual.Count}");
        var costs = new double[expected.Count, actual.Count];
        for (var source = 0; source < expected.Count; source++)
            for (var target = 0; target < actual.Count; target++)
                costs[source, target] = ShapeError(expected[source], actual[target]).Error;
        var assignment = PhysicsMaths.Assign(costs);
        var shapes = new JsonArray();
        var worstError = 0.0;
        var worstVolume = 0.0;
        for (var index = 0; index < assignment.Length; index++)
        {
            var (source, target) = (expected[index], actual[assignment[index]]);
            var (error, mapping) = ShapeError(source, target);
            var targetVolume = ConvexHull.Of(target.Vertices) is { } hull ? Math.Abs(MeshOps.Volume(hull)) : 0.0;
            var sourceVolume = MeshOps.Volume(source);
            var volumeError = Math.Abs(targetVolume / sourceVolume - 1);
            if (error > maximumError || volumeError > maximumVolumeError)
                throw new GeometryFinalizationException($"{name} shape {index}: decoded native geometry exceeds tolerance ({PhysicsMaths.General(error, 9)} in, {PhysicsMaths.General(volumeError, 9)} relative volume)");
            var exact = FaceSet(source).SetEquals(FaceSet(target, mapping));
            if (source.Vertices.Length != target.Vertices.Length || source.FaceCount != target.FaceCount || !exact)
                throw new GeometryFinalizationException($"{name} shape {index}: native serialization changed the prepared convex triangulation");
            worstError = Math.Max(worstError, error);
            worstVolume = Math.Max(worstVolume, volumeError);
            shapes.Add(new JsonObject
            {
                ["source_shape"] = index, ["serialized_shape"] = assignment[index],
                ["source_vertices"] = source.Vertices.Length, ["serialized_vertices"] = target.Vertices.Length,
                ["source_triangles"] = source.FaceCount, ["serialized_triangles"] = target.FaceCount,
                ["triangle_topology_exact"] = exact, ["maximum_surface_error_bound_in"] = error,
                ["source_volume_in3"] = sourceVolume, ["serialized_volume_in3"] = targetVolume, ["relative_volume_error"] = volumeError
            });
        }
        return new()
        {
            ["bone"] = name, ["source_shape_count"] = expected.Count, ["serialized_shape_count"] = actual.Count, ["shapes"] = shapes,
            ["native_support_bounds_in"] = nativeBounds is null ? null : new JsonArray(RagdollPhysics.Doubles(nativeBounds.Take(3).Select(value => (double)value)), RagdollPhysics.Doubles(nativeBounds.Skip(3).Select(value => (double)value))),
            ["maximum_native_support_bounds_error_in"] = nativeBoundsError,
            ["maximum_surface_error_bound_in"] = worstError, ["maximum_relative_shape_volume_error"] = worstVolume
        };
    }

    /// <summary>Atomically replaces validated geometry using native serialization while preserving checksum, header and keyvalues.</summary>
    /// <remarks>Errors preserve the original file and identify missing runtime or compiler dependencies. The caller owns
    /// fallback policy and runs mass finalization and parameter audits afterward. Surface tolerances apply to prepared
    /// source convexes; analytic tessellation and thickness repairs remain separately recorded in the source report.</remarks>
    public static JsonObject RebuildCompiledPhyGeometry(string phyPath, PreparedPhysics physics, string compilerPath, string cacheDirectory, PortContext context,
        double maximumSurfaceErrorIn = 1e-4, double maximumRelativeVolumeError = 1e-5)
    {
        if (!double.IsFinite(maximumSurfaceErrorIn) || maximumSurfaceErrorIn <= 0 || !double.IsFinite(maximumRelativeVolumeError) || maximumRelativeVolumeError <= 0)
            throw new GeometryFinalizationException("Native geometry tolerances must be finite and positive");
        var original = File.ReadAllBytes(phyPath);
        var compiled = PhyAudit.ReadPhy(phyPath, false);
        if (!compiled.Solids.Select(solid => int.Parse(solid["index"])).SequenceEqual(Enumerable.Range(0, compiled.SolidCount)))
            throw new GeometryFinalizationException("Compiled PHY solid keyvalues must appear in contiguous binary index order");
        var worlds = physics.Skin.WorldMatrices();
        var bodyByName = new Dictionary<string, JsonObject>();
        foreach (var item in physics.BodyReports) bodyByName[item.Get("helper").Str()] = item;
        var prepared = new List<(string Name, List<TriMesh> Meshes)>();
        using var payload = new MemoryStream();
        using var writer = new BinaryWriter(payload);
        writer.Write("S2CV"u8);
        writer.Write(1);
        writer.Write(compiled.SolidCount);
        foreach (var solid in compiled.Solids)
        {
            context.Check();
            var name = solid["name"];
            if (!bodyByName.TryGetValue(name, out var body)) throw new GeometryFinalizationException($"Compiled physics body is absent from the source audit manifest: {name}");
            var part = body.Get("part").Int();
            var inverse = worlds[physics.HelperByPart[part]].Inverse();
            var shapes = physics.Shapes.Where(shape => shape.Part == part).ToList();
            if (shapes.Count == 0) throw new GeometryFinalizationException($"Prepared physics body has no source convex shapes: {name}");
            writer.Write(shapes.Count);
            var meshes = new List<TriMesh>();
            foreach (var shape in shapes)
            {
                var (encoded, hull) = Polyhedron(Array.ConvertAll(shape.Mesh.Vertices, inverse.Point));
                writer.Write(encoded);
                meshes.Add(hull);
            }
            prepared.Add((name, meshes));
        }
        writer.Flush();
        var (executable, library, provenance) = Helper(compilerPath, cacheDirectory, context);
        var cache = Path.GetFullPath(cacheDirectory);
        Directory.CreateDirectory(cache);
        var work = Directory.CreateTempSubdirectory(Path.Combine(cache, "native-geometry-")).FullName;
        byte[] replacement;
        var reports = new List<JsonObject>();
        try
        {
            var inputPath = Path.Combine(work, "source.convexes");
            var outputPath = Path.Combine(work, "native.surfaces");
            File.WriteAllBytes(inputPath, payload.ToArray());
            Run(executable, [library, inputPath, outputPath], work, context);
            var data = File.ReadAllBytes(outputPath);
            if (data.Length < 12 || !data.AsSpan(0, 4).SequenceEqual("S2PV"u8) || BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(4)) != 2 ||
                BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(8)) != prepared.Count)
                throw new GeometryFinalizationException("Native helper returned an invalid surface container");
            var position = 12L;
            var blobs = new List<byte[]>();
            foreach (var (name, expected) in prepared)
            {
                if (position + 4 > data.Length) throw new GeometryFinalizationException("Native helper omitted a physics body");
                var size = BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan((int)position));
                position += 4;
                if (position + size > data.Length) throw new GeometryFinalizationException("Native helper truncated a physics body");
                var blob = data.AsSpan((int)position, (int)size).ToArray();
                position += size;
                if (position + 24 > data.Length) throw new GeometryFinalizationException("Native helper omitted collision support bounds");
                var bounds = new float[6];
                for (var index = 0; index < 6; index++) bounds[index] = BinaryPrimitives.ReadSingleLittleEndian(data.AsSpan((int)position + index * 4));
                position += 24;
                reports.Add(GeometryReport(name, expected, blob, maximumSurfaceErrorIn, maximumRelativeVolumeError, bounds));
                blobs.Add(blob);
            }
            if (position != data.Length) throw new GeometryFinalizationException("Native helper returned unexpected trailing surfaces");
            long headerSize = BinaryPrimitives.ReadUInt32LittleEndian(original);
            var tailOffset = headerSize;
            for (var index = 0; index < compiled.SolidCount; index++) tailOffset += 4 + BinaryPrimitives.ReadUInt32LittleEndian(original.AsSpan((int)tailOffset));
            using (var candidate = new MemoryStream())
            {
                candidate.Write(original.AsSpan(0, (int)headerSize));
                var sizes = new byte[4];
                foreach (var blob in blobs)
                {
                    BinaryPrimitives.WriteUInt32LittleEndian(sizes, (uint)blob.Length);
                    candidate.Write(sizes);
                    candidate.Write(blob);
                }
                candidate.Write(original.AsSpan((int)tailOffset));
                replacement = candidate.ToArray();
            }
            var checkPath = Path.Combine(work, "verified.phy");
            File.WriteAllBytes(checkPath, replacement);
            var verified = PhyAudit.ReadPhy(checkPath);
            if (verified.Checksum != compiled.Checksum || verified.Text != compiled.Text)
                throw new GeometryFinalizationException("Native replacement changed the original checksum or physics keyvalues");
            context.Check();
            if (!File.ReadAllBytes(phyPath).AsSpan().SequenceEqual(original))
                throw new GeometryFinalizationException("Compiled PHY changed during geometry finalization; refusing to overwrite it");
            var pending = phyPath + "." + Guid.NewGuid().ToString("N") + ".verified";
            try
            {
                File.WriteAllBytes(pending, replacement);
                File.Move(pending, phyPath, true);
            }
            finally
            {
                File.Delete(pending);
            }
        }
        finally
        {
            Directory.Delete(work, true);
        }
        var worstSurface = reports.Max(item => item.Get("maximum_surface_error_bound_in").Dbl());
        var worstVolume = reports.Max(item => item.Get("maximum_relative_shape_volume_error").Dbl());
        var result = new JsonObject
        {
            ["status"] = "preserved", ["method"] = "native_exact_convex_polyhedra", ["provenance"] = provenance,
            ["compiler_bootstrap_geometry_restorations"] = physics.GeometryRestorations.DeepClone(),
            ["body_count"] = reports.Count, ["bodies"] = RagdollPhysics.Nodes(reports), ["checksum_preserved"] = true, ["header_preserved"] = true,
            ["keyvalues_preserved"] = true, ["original_phy_sha256"] = Convert.ToHexStringLower(SHA256.HashData(original)),
            ["finalized_phy_sha256"] = Convert.ToHexStringLower(SHA256.HashData(replacement)),
            ["maximum_surface_error_bound_in"] = worstSurface,
            ["maximum_native_support_bounds_error_in"] = reports.Max(item => item.Get("maximum_native_support_bounds_error_in").Dbl()),
            ["maximum_relative_shape_volume_error"] = worstVolume,
            ["tolerance_reference"] = "Prepared convex surfaces; analytic tessellation and thickness repairs remain separate source adaptations",
            ["tolerances"] = new JsonObject { ["surface_error_in"] = maximumSurfaceErrorIn, ["relative_volume_error"] = maximumRelativeVolumeError }
        };
        context.Log($"Native serialized {reports.Count} physics bodies; max surface bound {PhysicsMaths.General(worstSurface, 8)}in, max volume error {PhysicsMaths.General(worstVolume, 8)}", LogLevel.Success);
        return result;
    }
}
