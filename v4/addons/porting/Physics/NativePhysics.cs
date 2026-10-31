using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>The source cannot be represented by the kinematic convex-hull exporter.</summary>
public sealed class NativePhysicsException(string message) : Exception(message);

/// <summary>Generated collision QC and an audit of source parts and repaired hulls.</summary>
public sealed record NativePhysicsResult(List<string> QcLines, JsonObject Report, List<string> Warnings);

/// <summary>Writes rigidly weighted compound hulls for animated Source 1 bone followers, without inventing source joints.</summary>
/// <remarks>PHYS bind poses already map local hull vertices into Source-inch model space, so the collision SMD reuses the
/// native reference skeleton with no glTF axis or unit conversion. No explicit joint constraints are invented for
/// kinematic parts: studiomdl retains zero-limit defaults where it finds a physical parent, and prop_dynamic bone
/// followers provide the intended motion.</remarks>
public static partial class NativePhysics
{
    private sealed record Part(int Index, string Bone, int Joint, double[] Transform, JsonArray Hulls);

    [GeneratedRegex(@"^nodes\n(.*?)^end\n", RegexOptions.Multiline | RegexOptions.Singleline)]
    private static partial Regex NodesBlock();

    [GeneratedRegex(@"^\s*(\d+)\s+""([^""\n]+)""\s+(-?\d+)\s*$", RegexOptions.Multiline)]
    private static partial Regex NodeLine();

    /// <summary>Validates a name before embedding it in a quoted QC token.</summary>
    public static string QcName(string value)
    {
        if (value.Length == 0 || value.AsSpan().IndexOfAny("\"\\\n\r") >= 0) throw new NativePhysicsException($"Unsafe or empty collision name: '{value}'");
        return $"\"{value}\"";
    }

    /// <summary>Folds case the way Source compares bone names.</summary>
    public static string Fold(string value) => value.ToLowerInvariant();

    /// <summary>Reads text with Python's universal newlines, so a stray carriage return cannot split a parse.</summary>
    public static string ReadText(string path) => File.ReadAllText(path).Replace("\r\n", "\n").Replace('\r', '\n');

    /// <summary>Flattens nested numeric arrays, with NaN standing in for anything that is not a number.</summary>
    public static double[] Numbers(JsonNode? node)
    {
        var result = new List<double>();
        void Visit(JsonNode? item)
        {
            if (item is JsonArray array)
                foreach (var child in array) Visit(child);
            else if (item is JsonValue) result.Add(item.Dbl(double.NaN));
            else result.Add(double.NaN);
        }
        Visit(node);
        return result.ToArray();
    }

    /// <summary>Whether a value is within NumPy's allclose tolerance of a target.</summary>
    public static bool AllClose(double value, double target, double absolute, double relative = 1e-5) =>
        Math.Abs(value - target) <= absolute + relative * Math.Abs(target);

    /// <summary>Checks that the upper 3x3 of a row-major 3x4 is orthonormal within rounding.</summary>
    public static bool IsOrthonormal(double[] rows, double absolute)
    {
        for (var first = 0; first < 3; first++)
            for (var second = 0; second < 3; second++)
            {
                var dot = rows[first] * rows[second] + rows[4 + first] * rows[4 + second] + rows[8 + first] * rows[8 + second];
                if (!AllClose(dot, first == second ? 1.0 : 0.0, absolute)) return false;
            }
        return true;
    }

    /// <summary>The determinant of the upper 3x3 of a row-major 3x4.</summary>
    public static double Determinant(double[] rows) =>
        rows[0] * (rows[5] * rows[10] - rows[6] * rows[9]) - rows[1] * (rows[4] * rows[10] - rows[6] * rows[8]) + rows[2] * (rows[4] * rows[9] - rows[5] * rows[8]);

    /// <summary>Whether two numbers agree as Python's math.isclose judges it.</summary>
    public static bool IsClose(double first, double second, double relative = 1e-9, double absolute = 0.0) =>
        first == second || Math.Abs(first - second) <= Math.Max(relative * Math.Max(Math.Abs(first), Math.Abs(second)), absolute);

    /// <summary>Checks bone indices and retains the authored hierarchy for collision constraints.</summary>
    private static (string Header, Dictionary<int, int> Parents) ReferenceHeader(string referencePath, IReadOnlyList<string> boneNames)
    {
        var text = ReadText(referencePath);
        var cut = text.IndexOf("triangles\n", StringComparison.Ordinal);
        var header = cut >= 0 ? text[..cut] : text;
        var match = NodesBlock().Match(header);
        if (!match.Success || !header.Contains("\nskeleton\n", StringComparison.Ordinal))
            throw new NativePhysicsException("Reference SMD lacks its native skeleton header");
        var nodes = new Dictionary<int, string>();
        var parents = new Dictionary<int, int>();
        foreach (Match entry in NodeLine().Matches(match.Groups[1].Value))
        {
            var index = int.Parse(entry.Groups[1].Value);
            nodes[index] = entry.Groups[2].Value;
            parents[index] = int.Parse(entry.Groups[3].Value);
        }
        if (boneNames.Select(Fold).Distinct().Count() != boneNames.Count)
            throw new NativePhysicsException("Collision bone names must be unique without regard to case");
        for (var index = 0; index < boneNames.Count; index++)
            if (!nodes.TryGetValue(index, out var found) || found != boneNames[index])
                throw new NativePhysicsException($"Reference bone {index} does not match '{boneNames[index]}'");
        return (header, parents);
    }

    /// <summary>Finds a selected collision ancestor without reparenting independent skeleton roots.</summary>
    private static int? PhysicalAncestor(int joint, Dictionary<int, int> parents, HashSet<int> physicalBones)
    {
        var visited = new HashSet<int> { joint };
        var parent = parents[joint];
        while (parent >= 0)
        {
            if (visited.Contains(parent) || !parents.ContainsKey(parent)) throw new NativePhysicsException("Reference skeleton has a cyclic or missing parent");
            if (physicalBones.Contains(parent)) return parent;
            visited.Add(parent);
            parent = parents[parent];
        }
        return null;
    }

    /// <summary>Resolves the PHYS part-to-bone indirection and rejects unsupported shapes.</summary>
    /// <remarks>A part named with an empty bone follows the model origin and binds to the origin bone.</remarks>
    private static List<Part> SourceParts(JsonObject document, IReadOnlyList<string> boneNames, string rootBone, string originBone)
    {
        if (document.Get("m_joints").Bool() || document.Get("m_constraints2").Bool())
            throw new NativePhysicsException("Jointed Source 2 physics needs explicit joint-limit conversion");
        if (document.Get("m_pFeModel").Bool()) throw new NativePhysicsException("Soft-body Source 2 physics is unsupported");
        var parts = document.Get("m_parts").Arr();
        var names = document.Get("m_boneNames").Arr();
        var parents = document.Get("m_boneParents").Arr();
        var poses = document.Get("m_bindPose").Arr();
        if (parts.Count is < 1 or > 32) throw new NativePhysicsException("Kinematic physics requires between 1 and 32 named parts");
        if (parents.Count != parts.Count || poses.Count != parts.Count) throw new NativePhysicsException("PHYS part, bone-index and bind-pose counts differ");
        var lookup = new Dictionary<string, int>();
        for (var index = 0; index < boneNames.Count; index++) lookup[boneNames[index]] = index;
        var resolved = new List<Part>();
        var used = new HashSet<string>();
        for (var index = 0; index < parts.Count; index++)
        {
            var boneIndex = parents[index].Int();
            if (boneIndex < 0 || boneIndex >= names.Count) throw new NativePhysicsException($"PHYS part {index} has an invalid bone-name index");
            var bone = names[boneIndex].Str();
            if (bone.Length == 0) bone = originBone;
            QcName(bone);
            if (!lookup.ContainsKey(bone) || used.Contains(Fold(bone))) throw new NativePhysicsException($"PHYS bone '{bone}' is missing from the skeleton or duplicated");
            used.Add(Fold(bone));
            var transform = Numbers(poses[index]);
            if (transform.Length != 12 || !transform.All(double.IsFinite)) throw new NativePhysicsException($"PHYS bone '{bone}' has an invalid 3x4 bind pose");
            if (!IsOrthonormal(transform, 2e-5) || !IsClose(Determinant(transform), 1.0, 1e-9, 2e-5))
                throw new NativePhysicsException($"PHYS bone '{bone}' bind pose contains scaling or reflection");
            var shape = parts[index].Get("m_rnShape");
            foreach (var kind in new[] { "m_spheres", "m_capsules", "m_meshes" })
                if (shape.Get(kind).Bool()) throw new NativePhysicsException($"PHYS bone '{bone}' contains unsupported {kind}");
            var hulls = shape.Get("m_hulls").Arr();
            if (hulls.Count == 0) throw new NativePhysicsException($"PHYS bone '{bone}' has no convex hull geometry");
            resolved.Add(new(index, bone, lookup[bone], transform, hulls));
        }
        if (!used.Contains(Fold(rootBone))) throw new NativePhysicsException($"Physics root '{rootBone}' must have authored collision geometry");
        return resolved;
    }

    /// <summary>Gives each hull a distinct smoothing group without moving its vertices.</summary>
    /// <remarks>studiomdl welds collision vertices when positions match and the normal dot product exceeds 0.999. A
    /// constant unit normal connects every face within a hull, and separated normals prevent touching source hulls
    /// from becoming one. These normals describe connectivity only, because collision is not drawn.</remarks>
    public static V3[] CollisionNormals(int count)
    {
        var normals = new V3[count];
        for (var index = 0; index < count; index++)
        {
            var z = 1.0 - 2.0 * (index + 0.5) / count;
            var angle = index * (Math.PI * (3.0 - Math.Sqrt(5.0)));
            var radius = Math.Sqrt(1.0 - z * z);
            normals[index] = new(radius * Math.Cos(angle), radius * Math.Sin(angle), z);
        }
        for (var first = 0; first < count; first++)
            for (var second = 0; second < count; second++)
                if (first != second && normals[first].Dot(normals[second]) >= 0.998)
                    throw new NativePhysicsException("Too many authored hulls for isolated collision smoothing groups");
        return normals;
    }

    /// <summary>Triangulates one authored cloud, repairs only that hull, and audits its volume.</summary>
    private static (TriMesh Mesh, JsonObject Report) PrepareHull(JsonNode? data, string label)
    {
        var shape = data.Get("m_Hull");
        var cloud = shape.Get("m_Vertices") as JsonArray;
        var flat = Numbers(cloud);
        if (cloud is null || cloud.Any(item => item is not JsonArray { Count: 3 }) || !flat.All(double.IsFinite))
            throw new NativePhysicsException($"{label}: invalid convex hull vertex cloud");
        var vertices = new V3[cloud.Count];
        for (var index = 0; index < vertices.Length; index++) vertices[index] = new(flat[index * 3], flat[index * 3 + 1], flat[index * 3 + 2]);
        var hull = StaticCollision.HullOfPoints(vertices);
        if (hull is null || !MeshOps.IsWatertight(hull)) throw new NativePhysicsException($"{label}: convex hull has no closed three-dimensional volume");
        var originalVolume = MeshOps.Volume(hull);
        var repair = StaticCollision.ThickenThinPieces(hull);
        var repaired = repair.Mesh;
        if (repair.Dropped > 0 || repaired is null || !MeshOps.IsWatertight(repaired))
            throw new NativePhysicsException($"{label}: thickness repair lost convex hull geometry");
        var minimumWidth = StaticCollision.ThinnestDirection(repaired).Width;
        if (minimumWidth <= StaticCollision.MinPieceThicknessIn) throw new NativePhysicsException($"{label}: hull is still below the compiler thickness limit");
        double? authored = null;
        if (shape.Get("m_flVolume") is { } volume)
        {
            authored = volume.Dbl(double.NaN);
            if (!double.IsFinite(authored.Value) || authored <= 0) throw new NativePhysicsException($"{label}: invalid authored collision volume");
        }
        return (repaired, new JsonObject
        {
            ["source_vertices"] = vertices.Length,
            ["export_vertices"] = repaired.Vertices.Length,
            ["triangles"] = repaired.FaceCount,
            ["authored_volume_in3"] = authored,
            ["source_cloud_volume_in3"] = originalVolume,
            ["export_local_volume_in3"] = MeshOps.Volume(repaired),
            ["minimum_width_before_in"] = repair.Thinnest,
            ["minimum_width_after_in"] = minimumWidth,
            ["thickened"] = repair.Thickened > 0,
            ["collision_attribute_index"] = data.Get("m_nCollisionAttributeIndex")?.DeepClone(),
            ["surface_property_index"] = data.Get("m_nSurfacePropertyIndex")?.DeepClone()
        });
    }

    /// <summary>Writes rigidly weighted compound hulls without inventing source joints.</summary>
    /// <remarks>Each source part maps through m_boneParents into m_boneNames, and bind poses are indexed by part. All hulls
    /// keep their own connected component. The report separates source volume, thickness repairs and physical ancestry.
    /// studiomdl can lose physical ancestors while skipping nonphysical bones, so explicit constraints would reject
    /// otherwise valid bone-follower parts. This is a kinematic prop, not a usable ragdoll. The caller supplies a
    /// Source-inch native reference and retains its bones. Parts bound to the model origin follow the origin bone,
    /// normally the skeleton root.</remarks>
    public static NativePhysicsResult WriteKinematicPhysics(string physPath, string smdPath, string referencePath, IReadOnlyList<string> boneNames, string rootBone, string originBone = "")
    {
        var (header, parents) = ReferenceHeader(referencePath, boneNames);
        var documents = Kv3.ParseDocuments(File.ReadAllText(physPath));
        if (documents.Count != 1) throw new NativePhysicsException("Expected one Source 2 PHYS aggregate");
        var document = documents[0];
        var parts = SourceParts(document, boneNames, rootBone, originBone);
        var physicalBones = parts.Select(part => part.Joint).ToHashSet();
        var ancestors = physicalBones.ToDictionary(joint => joint, joint => PhysicalAncestor(joint, parents, physicalBones));
        var independent = parts.Where(part => ancestors[part.Joint] is null).Select(part => part.Bone).ToList();
        var hullCount = parts.Sum(part => part.Hulls.Count);
        var normals = CollisionNormals(hullCount);
        var prepared = new List<(int Joint, TriMesh Mesh, V3 Normal)>();
        var reports = new List<JsonObject>();
        var warnings = new List<string>
        {
            "Source 2 kinematic parts require prop_dynamic bone followers for animated collision; this collision model is not a ragdoll.",
            "Collision bodies without an authored physical ancestor have no constraints: " + string.Join(", ", independent),
            "No explicit joint constraints are emitted; compiler zero-limit defaults apply only where physical parents survive."
        };
        foreach (var part in parts)
        {
            var transform = new M4([.. part.Transform, 0, 0, 0, 1]);
            for (var hullIndex = 0; hullIndex < part.Hulls.Count; hullIndex++)
            {
                var (mesh, report) = PrepareHull(part.Hulls[hullIndex], $"{part.Bone} hull {hullIndex}");
                var model = MeshOps.Transform(mesh, transform);
                var (low, high) = MeshOps.Bounds(model);
                report["part"] = part.Index;
                report["bone"] = part.Bone;
                report["bone_index"] = part.Joint;
                report["hull"] = hullIndex;
                report["export_model_volume_in3"] = MeshOps.Volume(model);
                report["bounds_model_in"] = new JsonArray(new JsonArray(low.X, low.Y, low.Z), new JsonArray(high.X, high.Y, high.Z));
                reports.Add(report);
                prepared.Add((part.Joint, model, normals[prepared.Count]));
            }
        }
        var thickened = reports.Count(report => report.Get("thickened").Bool());
        if (thickened > 0)
            warnings.Add($"{thickened} authored hulls were individually thickened to at least {Fmt.G(StaticCollision.ThickenTargetIn)} inches for VPhysics; dimensions are recorded per hull.");
        foreach (var report in reports)
        {
            var authored = report.Get("authored_volume_in3").Dbl();
            if (authored != 0 && !IsClose(authored, report.Get("source_cloud_volume_in3").Dbl(), 1e-3, 1e-4))
                warnings.Add($"{report.Get("bone").Str()} hull {report.Get("hull").Int()}: vertex-cloud volume differs from authored PHYS volume");
        }
        using (var output = Fmt.OpenText(smdPath))
        {
            output.Write(header + "triangles\n");
            foreach (var (joint, mesh, normal) in prepared)
                for (var face = 0; face < mesh.FaceCount; face++)
                {
                    output.Write("physics\n");
                    for (var corner = 0; corner < 3; corner++)
                        output.Write(SmdSkeleton.VertexLine(mesh.Vertices[mesh.Faces[face * 3 + corner]], normal, new(0, 0), [(joint, 1.0)]));
                }
            output.Write("end\n");
        }
        var budget = StaticCollision.ConvexPieceBudget(hullCount);
        List<string> qc =
        [
            $"$collisionjoints {QcName(Path.GetFileName(smdPath))}", "{", "    $concaveperjoint", $"    $maxconvexpieces {budget}", "    $automass",
            $"    $rootbone {QcName(rootBone)}", "    $noselfcollisions", "    $weldposition 0", "    $weldnormal 0.999", "}"
        ];
        var summary = new JsonObject
        {
            ["mode"] = "kinematic_bone_followers",
            ["source"] = physPath.Replace('/', Path.DirectorySeparatorChar),
            ["part_count"] = parts.Count,
            ["hull_count"] = hullCount,
            ["part_bones"] = Strings(parts.Select(part => part.Bone)),
            ["root_bone"] = rootBone,
            ["source_joint_count"] = 0,
            ["fallback_constraints"] = "compiler_zero_limits_where_physical_parent_survives",
            ["independent_bodies"] = Strings(independent),
            ["constrained_bodies"] = new JsonArray(),
            ["source_parented_bodies"] = Strings(parts.Where(part => ancestors[part.Joint] is not null).Select(part => part.Bone)),
            ["thickened_hulls"] = thickened,
            ["max_convex_pieces"] = budget,
            ["triangles"] = reports.Sum(report => report.Get("triangles").Int()),
            ["source_cloud_volume_in3"] = reports.Sum(report => report.Get("source_cloud_volume_in3").Dbl()),
            ["export_volume_in3"] = reports.Sum(report => report.Get("export_model_volume_in3").Dbl()),
            ["surface_property_hashes"] = document.Has("m_surfacePropertyHashes") ? document.Get("m_surfacePropertyHashes")!.DeepClone() : new JsonArray(),
            ["hulls"] = new JsonArray(reports.Select(report => (JsonNode?)report).ToArray())
        };
        return new(qc, summary, warnings);
    }

    /// <summary>A JSON array of strings.</summary>
    public static JsonArray Strings(IEnumerable<string> values) => new(values.Select(value => (JsonNode?)value).ToArray());
}
