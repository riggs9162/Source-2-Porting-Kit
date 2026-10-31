using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>A source rig or physics aggregate cannot produce a valid ragdoll.</summary>
public sealed class RagdollPhysicsException(string message) : Exception(message);

/// <summary>One prepared convex collision shape, in Source-inch model space.</summary>
public sealed record PhysicsShape(int Part, TriMesh Mesh, JsonObject Report);

/// <summary>A moving helper bone with a fixed offset from its parent; a null parent means model space.</summary>
public sealed record RigidHelper(int Helper, int? Parent, M4 Local);

/// <summary>The rig and collision a compiled .phy is checked against, prepared now or reloaded from a manifest.</summary>
public class PreparedPhysics
{
    public required GltfSkin Skin { get; init; }

    /// <summary>Source part to combined-skin helper bone, in physical hierarchy order.</summary>
    public required Dictionary<int, int> HelperByPart { get; init; }

    public required List<JsonObject> BodyReports { get; set; }
    public required List<JsonObject> JointReports { get; set; }
    public required List<PhysicsShape> Shapes { get; set; }
    public required JsonObject Report { get; set; }

    /// <summary>Shapes as the compiler bootstrap saw them, before analytic thickness was removed again.</summary>
    public List<PhysicsShape> CompilerBootstrapShapes { get; set; } = [];

    public JsonArray GeometryRestorations { get; set; } = [];
    public List<JsonObject> CompilerBootstrapBodyReports { get; set; } = [];
}

/// <summary>A pivot-aligned rig, source-preserving remapping and collision compilation inputs.</summary>
public sealed class RagdollPhysicsResult : PreparedPhysics
{
    public required Dictionary<int, int> IndexMap { get; init; }
    public required string RootBone { get; init; }
    public required JsonObject SourceDocument { get; init; }
    public required List<string> Warnings { get; init; }
    public required GltfSkin OriginalSkin { get; init; }
    public required CoordinateMode OriginalCoordinates { get; init; }
    public required Dictionary<int, M4> HelperOffsets { get; init; }
    public required List<int> PartSources { get; init; }
    public double FrictionScale { get; init; } = 1.0;
    public List<RigidHelper> RigidHelpers { get; init; } = [];

    /// <summary>Preserves original animated bone worlds while adding moving physical helper frames.</summary>
    public JointPose[] RemapFrame(IReadOnlyList<JointPose> frame)
    {
        var source = RagdollPhysics.SourceWorld(OriginalSkin, frame, OriginalCoordinates);
        var worlds = new Dictionary<int, M4>();
        for (var index = 0; index < source.Length; index++) worlds[IndexMap[index]] = source[index];
        foreach (var (part, helper) in HelperByPart) worlds[helper] = source[PartSources[part]] * HelperOffsets[part];
        foreach (var (helper, parent, local) in RigidHelpers) worlds[helper] = parent is { } owner ? worlds[owner] * local : local;
        var result = new JointPose[Skin.Joints.Count];
        for (var index = 0; index < result.Length; index++)
        {
            var local = Skin.Joints[index].Parent is { } parent ? worlds[parent].Inverse() * worlds[index] : worlds[index];
            result[index] = new(local.Translation, local.ToQuaternion(), V3.One);
        }
        return result;
    }

    /// <summary>Replaces a Source-space native SMD bind header and remaps its intact triangle weights.</summary>
    public void RemapSmd(string path)
    {
        var text = NativePhysics.ReadText(path);
        var cut = text.IndexOf("triangles\n", StringComparison.Ordinal);
        var tail = cut >= 0 ? text[(cut + "triangles\n".Length)..] : null;
        SmdSkeleton.WriteAnimation(Skin, [Skin.BindPose()], path, new());
        if (tail is null) return;
        var lines = tail.Split('\n').ToList();
        if (lines.Count > 0 && lines[^1].Length == 0) lines.RemoveAt(lines.Count - 1);
        for (var start = 0; start < lines.Count - 1; start += 4)
        {
            if (lines.Count - start < 4) throw new RagdollPhysicsException($"Incomplete triangle in {path}");
            for (var index = start + 1; index < start + 4; index++)
            {
                var values = lines[index].Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
                var links = new List<(int Joint, double Weight)>();
                if (values.Length > 9)
                    for (var slot = 0; slot < int.Parse(values[9]); slot++)
                        links.Add((IndexMap[int.Parse(values[10 + slot * 2])], Parse(values[11 + slot * 2])));
                else links.Add((IndexMap[int.Parse(values[0])], 1.0));
                lines[index] = SmdSkeleton.VertexLine(new(Parse(values[1]), Parse(values[2]), Parse(values[3])),
                    new(Parse(values[4]), Parse(values[5]), Parse(values[6])), new(Parse(values[7]), Parse(values[8])), links).TrimEnd('\n');
            }
        }
        using var output = Fmt.OpenText(path, true);
        output.Write("triangles\n" + string.Join('\n', lines) + "\n");
    }

    private static double Parse(string value) => double.Parse(value, System.Globalization.CultureInfo.InvariantCulture);

    /// <summary>Writes separate rigid convex pieces and returns the calibrated ragdoll QC directives.</summary>
    public List<string> WriteCollision(string path)
    {
        SmdSkeleton.WriteAnimation(Skin, [Skin.BindPose()], path, new());
        var normals = NativePhysics.CollisionNormals(Shapes.Count);
        using (var output = Fmt.OpenText(path, true))
        {
            output.Write("triangles\n");
            for (var ordinal = 0; ordinal < Shapes.Count; ordinal++)
            {
                var (part, mesh, _) = Shapes[ordinal];
                var link = new (int, double)[] { (HelperByPart[part], 1.0) };
                for (var face = 0; face < mesh.FaceCount; face++)
                {
                    output.Write("physics\n");
                    for (var corner = 0; corner < 3; corner++)
                        output.Write(SmdSkeleton.VertexLine(mesh.Vertices[mesh.Faces[face * 3 + corner]], normals[ordinal], new(0, 0), link));
                }
            }
            output.Write("end\n");
        }
        return QcLines(Path.GetFileName(path));
    }

    /// <summary>Emits per-body mass and material settings and source-frame joint adaptations.</summary>
    public List<string> QcLines(string filename)
    {
        var lines = new List<string>();
        foreach (var body in BodyReports)
            lines.Add($"$jointsurfaceprop {NativePhysics.QcName(body.Get("helper").Str())} {NativePhysics.QcName(body.Get("target_surfaceprop").Str())}");
        lines.AddRange([$"$collisionjoints {NativePhysics.QcName(filename)}", "{", "    $concaveperjoint",
            $"    $maxconvexpieces {StaticCollision.ConvexPieceBudget(Shapes.Count)}", $"    $rootbone {NativePhysics.QcName(RootBone)}",
            $"    $mass {Fmt.F9(BodyReports.Sum(body => body.Get("target_mass_kg").Dbl()))}",
            "    $inertia 1", "    $damping 0", "    $rotdamping 0", "    $weldposition 0", "    $weldnormal 0.999"]);
        foreach (var body in BodyReports)
        {
            var helper = NativePhysics.QcName(body.Get("helper").Str());
            var bias = body.Get("target_mass_kg").Dbl() / body.Get("export_volume_in3").Dbl();
            lines.AddRange([$"    $jointmassbias {helper} {PhysicsMaths.General(bias, 12)}",
                $"    $jointinertia {helper} {PhysicsMaths.General(body.Get("target_inertia_scale").Dbl(), 9)}",
                $"    $jointdamping {helper} {PhysicsMaths.General(body.Get("target_linear_damping").Dbl(), 9)}",
                $"    $jointrotdamping {helper} {PhysicsMaths.General(body.Get("target_angular_damping").Dbl(), 9)}"]);
        }
        foreach (var joint in JointReports)
            foreach (var axis in joint.Get("target_axes").Arr())
            {
                var friction = joint.Get("target_friction").Dbl() / FrictionScale;
                lines.Add($"    $jointconstrain {NativePhysics.QcName(joint.Get("child_helper").Str())} {axis.Arr()[0].Str()} limit " +
                    $"{Fmt.F9(axis.Arr()[1].Dbl())} {Fmt.F9(axis.Arr()[2].Dbl())} {Fmt.F9(friction)}");
            }
        var pairs = Report.Get("collision_pairs").Arr();
        if (pairs.Count == 0) lines.Add("    $noselfcollisions");
        else
            foreach (var pair in pairs)
                lines.Add($"    $jointcollide {NativePhysics.QcName(pair.Arr()[0].Str())} {NativePhysics.QcName(pair.Arr()[1].Str())}");
        lines.Add("}");
        return lines;
    }
}

/// <summary>Preserves authored articulated physics with a separate pivot-aligned skeleton.</summary>
public static class RagdollPhysics
{
    public const double MaxShapeErrorIn = 0.05;
    public const double MaxShapeVolumeError = 0.01;
    public const int MaxBones = 256;
    public const int MaxBodies = 32;

    private static readonly Dictionary<long, (string Name, double Density, double Friction, double Elasticity)> SourceSurfaces = new()
    {
        [1977497166] = ("default", 2000.0, 0.8, 0.25),
        [3722016785] = ("npc_combine_soldier", 2000.0, 0.5, 0.0),
        [2084844629] = ("npc_combine_soldier_gear", 2000.0, 0.8, 0.25),
        [2254283372] = ("npc_headcrab_flesh", 900.0, 0.4, 0.02),
        [4210780020] = ("npc_zombie_flesh", 900.0, 0.6, 0.0)
    };

    private static readonly double[] IcosahedronVertices =
    [
        -0.5257311121191336, 0.85065080835204, 0.0, 0.5257311121191336, 0.85065080835204, 0.0, -0.5257311121191336, -0.85065080835204, 0.0,
        0.5257311121191336, -0.85065080835204, 0.0, 0.0, -0.5257311121191336, 0.85065080835204, 0.0, 0.5257311121191336, 0.85065080835204,
        0.0, -0.5257311121191336, -0.85065080835204, 0.0, 0.5257311121191336, -0.85065080835204, 0.85065080835204, 0.0, -0.5257311121191336,
        0.85065080835204, 0.0, 0.5257311121191336, -0.85065080835204, 0.0, -0.5257311121191336, -0.85065080835204, 0.0, 0.5257311121191336
    ];

    private static readonly int[] IcosahedronFaces =
    [
        0, 11, 5, 0, 5, 1, 0, 1, 7, 0, 7, 10, 0, 10, 11, 1, 5, 9, 5, 11, 4, 11, 10, 2, 10, 7, 6, 7, 1, 8,
        3, 9, 4, 3, 4, 2, 3, 2, 6, 3, 6, 8, 3, 8, 9, 4, 9, 5, 2, 4, 11, 6, 2, 10, 8, 6, 7, 9, 8, 1
    ];

    private static readonly M4 AxisTurn = new([1, 0, 0, 0, 0, 0, -1, 0, 0, 1, 0, 0, 0, 0, 0, 1]);

    private static M4 Matrix(V3 position, Q4 rotation)
    {
        var matrix = M4.Trs(position, rotation);
        return PhysicsMaths.IsFinite(matrix) ? matrix : throw new RagdollPhysicsException("Non-finite rigid transform");
    }

    /// <summary>Normalizes rounded PHYS rotations while rejecting scale and reflection.</summary>
    public static M4 Rigid(JsonNode? value, string label)
    {
        var rows = NativePhysics.Numbers(value);
        if (rows.Length != 12 || !rows.All(double.IsFinite)) throw new RagdollPhysicsException($"{label}: expected a finite 3x4 bind transform");
        if (!NativePhysics.IsOrthonormal(rows, 2e-5) || NativePhysics.Determinant(rows) < 0.99998)
            throw new RagdollPhysicsException($"{label}: bind transform contains scale or reflection");
        var nearest = PhysicsMaths.NearestRotation(new[,] { { rows[0], rows[1], rows[2] }, { rows[4], rows[5], rows[6] }, { rows[8], rows[9], rows[10] } });
        return new([nearest[0, 0], nearest[0, 1], nearest[0, 2], rows[3], nearest[1, 0], nearest[1, 1], nearest[1, 2], rows[7],
            nearest[2, 0], nearest[2, 1], nearest[2, 2], rows[11], 0, 0, 0, 1]);
    }

    /// <summary>Reads a PHYS joint anchor and quaternion without losing its local origin.</summary>
    public static M4 Frame(JsonNode? value)
    {
        var numbers = NativePhysics.Numbers(value);
        if (numbers.Length != 7 || !numbers.All(double.IsFinite) || new Q4(numbers[3], numbers[4], numbers[5], numbers[6]).Length < 1e-8)
            throw new RagdollPhysicsException("Invalid Source 2 joint frame");
        return Matrix(new(numbers[0], numbers[1], numbers[2]), new(numbers[3], numbers[4], numbers[5], numbers[6]));
    }

    /// <summary>Evaluates a source local pose and converts all model transforms into Source inches.</summary>
    public static M4[] SourceWorld(GltfSkin skin, IReadOnlyList<JointPose> frame, CoordinateMode coordinates)
    {
        var transform = coordinates.SwapAxes ? M4.FromQuaternion(coordinates.Rotation) : M4.Identity;
        var world = new M4?[skin.Joints.Count];
        var pending = Enumerable.Range(0, skin.Joints.Count).ToList();
        while (pending.Count > 0)
        {
            var remaining = new List<int>();
            foreach (var index in pending)
            {
                var parent = skin.Joints[index].Parent;
                if (parent is { } owner && world[owner] is null)
                {
                    remaining.Add(index);
                    continue;
                }
                var (position, rotation, scale) = frame[index];
                if (!NativePhysics.AllClose(scale.X, 1.0, 1e-5) || !NativePhysics.AllClose(scale.Y, 1.0, 1e-5) || !NativePhysics.AllClose(scale.Z, 1.0, 1e-5))
                    throw new RagdollPhysicsException("Ragdoll animation cannot preserve scaled bones");
                var local = Matrix(coordinates.Scale == 1.0 ? position : coordinates.ApplyToChild(position), rotation);
                world[index] = parent is { } above ? world[above]! * local : transform * local;
            }
            if (remaining.Count == pending.Count) throw new RagdollPhysicsException("Source skeleton has a cycle or missing parent");
            pending = remaining;
        }
        return world.Select(matrix => matrix!).ToArray();
    }

    /// <summary>Reads resolved Source 2 material density and physical properties from a VSURF DATA dump.</summary>
    public static Dictionary<long, JsonObject> SurfacePropertiesFromDump(string text)
    {
        var result = new Dictionary<long, JsonObject>();
        foreach (var document in Kv3.ParseDocuments(text))
            foreach (var item in document.Get("SurfacePropertiesList").Arr())
                if (item.Has("m_nameHash")) result[item.Get("m_nameHash").Lng()] = item.Obj();
        return result;
    }

    /// <summary>Resolves an exact VSURF entry, or the verified Alyx defaults for known hashes.</summary>
    private static JsonObject Surface(long hash, IReadOnlyDictionary<long, JsonObject> properties)
    {
        if (properties.TryGetValue(hash, out var entry) && entry.Count > 0) return entry.DeepClone().AsObject();
        if (SourceSurfaces.TryGetValue(hash, out var known))
            return new()
            {
                ["surfacePropertyName"] = known.Name, ["m_nameHash"] = hash,
                ["physics"] = new JsonObject { ["density"] = known.Density, ["friction"] = known.Friction, ["elasticity"] = known.Elasticity }
            };
        return new()
        {
            ["surfacePropertyName"] = "default", ["m_nameHash"] = hash, ["unresolved"] = true,
            ["physics"] = new JsonObject { ["density"] = 2000.0, ["friction"] = 0.8, ["elasticity"] = 0.25 }
        };
    }

    /// <summary>Builds the deterministic inscribed sphere shared by capsule shapes.</summary>
    /// <remarks>Follows trimesh's icosphere: subdivide at edge midpoints, then push every vertex back out to the radius.
    /// Those exact coordinates decide which points survive into a capsule's hull.</remarks>
    public static TriMesh UnitSphere(int subdivisions)
    {
        var vertices = new List<V3>(12);
        for (var index = 0; index < 12; index++) vertices.Add(new(IcosahedronVertices[index * 3], IcosahedronVertices[index * 3 + 1], IcosahedronVertices[index * 3 + 2]));
        var faces = IcosahedronFaces;
        for (var level = 0; level < subdivisions; level++)
        {
            var midpoints = new Dictionary<(int, int), int>();
            int Midpoint(int a, int b)
            {
                var key = a < b ? (a, b) : (b, a);
                if (midpoints.TryGetValue(key, out var existing)) return existing;
                var created = vertices.Count;
                vertices.Add((vertices[key.Item1] + vertices[key.Item2]) / 2.0);
                midpoints[key] = created;
                return created;
            }
            var next = new int[faces.Length * 4];
            for (var face = 0; face < faces.Length / 3; face++)
            {
                int a = faces[face * 3], b = faces[face * 3 + 1], c = faces[face * 3 + 2];
                int ab = Midpoint(a, b), bc = Midpoint(b, c), ca = Midpoint(c, a);
                int[] children = [a, ab, ca, ab, b, bc, ca, bc, c, ab, bc, ca];
                children.CopyTo(next, face * 12);
            }
            faces = next;
            for (var index = 0; index < vertices.Count; index++)
            {
                var vector = vertices[index];
                var length = Math.Sqrt(vector.X * vector.X + vector.Y * vector.Y + vector.Z * vector.Z);
                vertices[index] = vector + vector / length * (1.0 - length);
            }
        }
        return new(vertices.ToArray(), faces);
    }

    /// <summary>Tessellates analytic spheres and capsules against an explicit geometric error budget.</summary>
    public static (TriMesh Mesh, JsonObject Report) PrimitiveMesh(string kind, JsonNode? data)
    {
        var source = data.Get(kind == "sphere" ? "m_Sphere" : "m_Capsule");
        var radius = source.Get("m_flRadius").Dbl(double.NaN);
        var numbers = NativePhysics.Numbers(source.Get("m_vCenter"));
        if (radius <= 0 || !double.IsFinite(radius) || !numbers.All(double.IsFinite) || numbers.Length % 3 != 0)
            throw new RagdollPhysicsException("Source primitive has an invalid radius or center");
        var centers = new V3[numbers.Length / 3];
        for (var index = 0; index < centers.Length; index++) centers[index] = new(numbers[index * 3], numbers[index * 3 + 1], numbers[index * 3 + 2]);
        if (centers.Length != (kind == "sphere" ? 1 : 2)) throw new RagdollPhysicsException("Source primitive has the wrong number of endpoints");
        var length = kind == "sphere" ? 0.0 : (centers[1] - centers[0]).Length;
        var exactVolume = Math.PI * radius * radius * length + 4.0 * Math.PI * (radius * radius * radius) / 3.0;
        for (var subdivisions = 1; subdivisions < 5; subdivisions++)
        {
            var sphere = UnitSphere(subdivisions);
            var normals = MeshOps.FaceNormals(sphere);
            var nearest = double.PositiveInfinity;
            for (var face = 0; face < normals.Length; face++) nearest = Math.Min(nearest, normals[face].Dot(sphere.Vertices[sphere.Faces[face * 3]]));
            var error = radius * (1.0 - nearest);
            if (error > MaxShapeErrorIn) continue;
            var mesh = StaticCollision.HullOfPoints(centers.SelectMany(center => sphere.Vertices.Select(vertex => vertex * radius + center)).ToArray());
            if (mesh is null) continue;
            var volumeError = Math.Abs(MeshOps.Volume(mesh) / exactVolume - 1.0);
            if (volumeError > MaxShapeVolumeError) continue;
            return (mesh, new JsonObject
            {
                ["kind"] = kind, ["source_radius_in"] = radius, ["source_length_in"] = length, ["source_volume_in3"] = exactVolume,
                ["tessellation_subdivisions"] = subdivisions, ["tessellation_error_in"] = error, ["tessellation_volume_error"] = volumeError
            });
        }
        throw new RagdollPhysicsException("Source primitive exceeds the tessellation fidelity budget");
    }

    /// <summary>Keeps each authored shape separate and reports any compiler thickness repair.</summary>
    public static (TriMesh Mesh, JsonObject Report) PrepareShape(string kind, JsonNode? data)
    {
        TriMesh mesh;
        JsonObject report;
        if (kind is "sphere" or "capsule") (mesh, report) = PrimitiveMesh(kind, data);
        else
        {
            var hull = data.Get("m_Hull");
            var flat = NativePhysics.Numbers(hull.Get("m_Vertices"));
            var vertices = new V3[flat.Length / 3];
            for (var index = 0; index < vertices.Length; index++) vertices[index] = new(flat[index * 3], flat[index * 3 + 1], flat[index * 3 + 2]);
            mesh = StaticCollision.HullOfPoints(vertices) ?? throw new RagdollPhysicsException("Source convex hull has no closed volume");
            var authored = hull.Get("m_flVolume").Dbl();
            report = new()
            {
                ["kind"] = kind, ["source_volume_in3"] = authored != 0 ? authored : MeshOps.Volume(mesh),
                ["tessellation_error_in"] = 0.0, ["tessellation_volume_error"] = 0.0
            };
        }
        var originalBounds = MeshOps.Bounds(mesh);
        var originalVolume = MeshOps.Volume(mesh);
        var repaired = StaticCollision.ThickenThinPieces(mesh);
        if (repaired.Mesh is null || repaired.Dropped > 0 || !MeshOps.IsWatertight(repaired.Mesh))
            throw new RagdollPhysicsException("Compiler thickness repair lost source collision geometry");
        mesh = repaired.Mesh;
        var bounds = MeshOps.Bounds(mesh);
        var growth = 0.0;
        for (var axis = 0; axis < 3; axis++)
            growth = Math.Max(growth, Math.Max(Math.Abs(bounds.Low[axis] - originalBounds.Low[axis]), Math.Abs(bounds.High[axis] - originalBounds.High[axis])));
        report["export_volume_in3"] = MeshOps.Volume(mesh);
        report["source_mesh_volume_in3"] = originalVolume;
        report["thickened"] = repaired.Thickened > 0;
        report["minimum_width_in"] = StaticCollision.ThinnestDirection(mesh).Width;
        report["thickness_bounds_growth_in"] = growth;
        report["collision_attribute_index"] = data.Get("m_nCollisionAttributeIndex").Int();
        report["surface_property_index"] = data.Get("m_nSurfacePropertyIndex").Int();
        report["export_vertices"] = mesh.Vertices.Length;
        report["export_triangles"] = mesh.FaceCount;
        return (mesh, report);
    }

    /// <summary>Finds the closest source ancestor represented by a generated fallback body.</summary>
    private static int? NearestPhysicalParent(int index, GltfSkin skin, HashSet<int> selected)
    {
        var seen = new HashSet<int>();
        var parent = skin.Joints[index].Parent;
        while (parent is { } current)
        {
            if (!seen.Add(current)) throw new RagdollPhysicsException("Cyclic source skeleton");
            if (selected.Contains(current)) return current;
            parent = skin.Joints[current].Parent;
        }
        return null;
    }

    /// <summary>Generates explicitly adapted humanoid collision from authored AO proxy capsules.</summary>
    private static JsonObject AoFallback(JsonObject modelData, GltfSkin skin, M4[] worlds)
    {
        var keys = Kv3.ParseDocuments(modelData.Get("m_modelInfo").Get("m_keyValueText").Str());
        var capsules = keys.Count > 0 ? keys[0].Get("ao_proxy_capsule_list").Arr() : [];
        if (capsules.Count == 0) throw new RagdollPhysicsException("Model has no source physics or authored AO proxy capsules");
        var lookup = new Dictionary<string, int>();
        for (var index = 0; index < skin.Joints.Count; index++) lookup[skin.Joints[index].Name] = index;
        var names = capsules.Select(item => item.Get("bonename").Str()).Distinct().ToList();
        if (names.Exists(name => !lookup.ContainsKey(name))) throw new RagdollPhysicsException("AO proxy refers to a bone absent from the source skeleton");
        var indices = names.ConvertAll(name => lookup[name]);
        var selected = indices.ToHashSet();
        var parts = new JsonArray();
        foreach (var name in names)
        {
            var shapes = new JsonArray();
            foreach (var item in capsules.Where(item => item.Get("bonename").Str() == name))
                shapes.Add(new JsonObject
                {
                    ["m_nSurfacePropertyIndex"] = 0, ["m_nCollisionAttributeIndex"] = 0,
                    ["m_Capsule"] = new JsonObject
                    {
                        ["m_vCenter"] = new JsonArray(item.Get("point0")?.DeepClone(), item.Get("point1")?.DeepClone()), ["m_flRadius"] = item.Get("radius").Dbl()
                    }
                });
            parts.Add(new JsonObject
            {
                ["m_flMass"] = 0.0, ["m_flInertiaScale"] = 2.0, ["m_flLinearDamping"] = 0.01, ["m_flAngularDamping"] = 0.4,
                ["m_rnShape"] = new JsonObject { ["m_capsules"] = shapes }
            });
        }
        var joints = new JsonArray();
        for (var part = 0; part < indices.Count; part++)
        {
            var source = indices[part];
            if (NearestPhysicalParent(source, skin, selected) is not { } parent) continue;
            var relative = worlds[parent].Inverse() * worlds[source];
            var lower = skin.Joints[source].Name.ToLowerInvariant();
            var hinge = lower.Contains("arm_lower") || lower.Contains("leg_lower");
            var (minimum, maximum) = lower.Contains("arm_lower") ? (-149.0, 4.0) : (-8.0, 126.0);
            var (translation, rotation) = (relative.Translation, relative.ToQuaternion());
            joints.Add(new JsonObject
            {
                ["m_nType"] = hinge ? 3 : 4, ["m_nBody1"] = indices.IndexOf(parent), ["m_nBody2"] = part,
                ["m_Frame1"] = new JsonArray(translation.X, translation.Y, translation.Z, rotation.X, rotation.Y, rotation.Z, rotation.W),
                ["m_Frame2"] = new JsonArray(0, 0, 0, 0, 0, 0, 1),
                ["m_bEnableCollision"] = false, ["m_bEnableSwingLimit"] = !hinge,
                ["m_SwingLimit"] = new JsonObject { ["m_flMin"] = 0, ["m_flMax"] = Fmt.Radians(35) },
                ["m_bEnableTwistLimit"] = true,
                ["m_TwistLimit"] = new JsonObject { ["m_flMin"] = Fmt.Radians(hinge ? minimum : -25), ["m_flMax"] = Fmt.Radians(hinge ? maximum : 25) },
                ["_generated"] = true
            });
        }
        return new()
        {
            ["m_boneNames"] = NativePhysics.Strings(names),
            ["m_boneParents"] = new JsonArray(Enumerable.Range(0, names.Count).Select(index => (JsonNode?)index).ToArray()),
            ["m_bindPose"] = new JsonArray(indices.Select(index => (JsonNode?)Doubles(PhysicsMaths.Rows(worlds[index]))).ToArray()),
            ["m_parts"] = parts, ["m_joints"] = joints, ["m_surfacePropertyHashes"] = new JsonArray(3722016785L),
            ["m_collisionAttributes"] = new JsonArray(), ["_generated_from_ao_proxy"] = true
        };
    }

    /// <summary>A JSON array of numbers.</summary>
    public static JsonArray Doubles(IEnumerable<double> values) => new(values.Select(value => (JsonNode?)value).ToArray());

    private static JsonArray MatrixRows(M4 matrix) => new(Enumerable.Range(0, 4)
        .Select(row => (JsonNode?)Doubles(Enumerable.Range(0, 4).Select(column => matrix[row, column]))).ToArray());

    /// <summary>Reads one source physics aggregate from its lossless exported sidecar.</summary>
    private static JsonObject ReadDocument(string? path)
    {
        if (path is null) return [];
        var documents = Kv3.ParseDocuments(File.ReadAllText(path));
        return documents.Count == 1 ? documents[0] : throw new RagdollPhysicsException("Expected exactly one PHYS aggregate");
    }

    /// <summary>Requires a single rooted articulated tree without discarding source constraints.</summary>
    private static (Dictionary<int, JsonNode> Incoming, int Root, List<int> Order) PartHierarchy(JsonObject document)
    {
        var count = document.Get("m_parts").Arr().Count;
        var incoming = new Dictionary<int, JsonNode>();
        if (document.Get("m_constraints2").Bool()) throw new RagdollPhysicsException("Alternate Source 2 constraints need explicit adaptation");
        foreach (var joint in document.Get("m_joints").Arr())
        {
            int parent = joint.Get("m_nBody1").Int(), child = joint.Get("m_nBody2").Int();
            if (parent < 0 || parent >= count || child < 0 || child >= count || child == parent || incoming.ContainsKey(child))
                throw new RagdollPhysicsException("PHYS joints are not a valid body tree");
            if (joint.Get("m_nType").Int() is not (3 or 4 or 6)) throw new RagdollPhysicsException($"Unsupported PHYS joint type {joint.Get("m_nType").Int()}");
            incoming[child] = joint!;
        }
        var roots = Enumerable.Range(0, count).Where(part => !incoming.ContainsKey(part)).ToList();
        if (roots.Count != 1) throw new RagdollPhysicsException("Ragdoll needs one physical root and a connected joint tree");
        var order = new List<int> { roots[0] };
        while (order.Count < count)
        {
            var pending = Enumerable.Range(0, count).Where(part => !order.Contains(part) && order.Contains(incoming[part].Get("m_nBody1").Int())).ToList();
            if (pending.Count == 0) throw new RagdollPhysicsException("Cyclic source physics hierarchy");
            order.AddRange(pending);
        }
        return (incoming, roots[0], order);
    }

    /// <summary>Measures Source MatrixAngles' roll-zero canonicalization below its 0.001 horizontal threshold.</summary>
    public static double CompilerGimbalLoss(M4 rotation)
    {
        var horizontal = Fmt.Hypot(rotation[0, 0], rotation[1, 0]);
        if (horizontal > 0.001) return 0.0;
        var pitch = Math.Atan2(-rotation[2, 0], horizontal);
        var yaw = Math.Atan2(-rotation[0, 1], rotation[1, 1]);
        double cp = Math.Cos(pitch), sp = Math.Sin(pitch), cy = Math.Cos(yaw), sy = Math.Sin(yaw);
        double[,] canonical = { { cp * cy, -sy, sp * cy }, { cp * sy, cy, sp * sy }, { -sp, 0, cp } };
        var loss = 0.0;
        for (var row = 0; row < 3; row++)
            for (var column = 0; column < 3; column++)
                loss = Math.Max(loss, Math.Abs(canonical[row, column] - rotation[row, column]));
        return loss;
    }

    /// <summary>Avoids both MatrixAngles canonicalization and observed near-singular compiler roundoff on physical frames.</summary>
    private static bool PhysicalAxisFactorRequired(M4 rotation)
    {
        var horizontal = Fmt.Hypot(rotation[0, 0], rotation[1, 0]);
        return CompilerGimbalLoss(rotation) > 0.000001 || horizontal is > 0.001 and < 0.01;
    }

    private sealed record Combined(GltfSkin Skin, Dictionary<int, int> IndexMap, Dictionary<int, int> HelperMap, Dictionary<int, M4> Offsets, List<RigidHelper> RigidHelpers);

    private const string HelperKind = "helper", SourceKind = "source", AxisKind = "axis", PhysicalAxisKind = "physical_axis";

    private static string Unique(string name, HashSet<string> names)
    {
        while (names.Contains(NativePhysics.Fold(name))) name += "_";
        names.Add(NativePhysics.Fold(name));
        return name;
    }

    /// <summary>Inserts physical helpers without changing the world bind of any original bone.</summary>
    private static Combined CombinedSkin(GltfSkin skin, M4[] sourceWorlds, List<int> partSources, M4[] bind, Dictionary<int, JsonNode> incoming, int root, List<int> order)
    {
        var names = skin.JointNames.Select(NativePhysics.Fold).ToHashSet();
        var helperNames = new Dictionary<int, string>();
        var offsets = new Dictionary<int, M4>();
        var helperWorlds = new Dictionary<int, M4>();
        foreach (var part in order)
        {
            helperNames[part] = Unique($"__s2_phys_{part:00}", names);
            var frame = incoming.TryGetValue(part, out var joint) ? Frame(joint.Get("m_Frame2")) : M4.Identity;
            helperWorlds[part] = bind[part] * frame;
            offsets[part] = sourceWorlds[partSources[part]].Inverse() * helperWorlds[part];
        }
        var originalsBySource = new Dictionary<int, int>();
        for (var part = 0; part < partSources.Count; part++) originalsBySource[partSources[part]] = part;
        var sourceParents = new Dictionary<int, (string Kind, int Index)>();
        for (var index = 0; index < skin.Joints.Count; index++)
            sourceParents[index] = originalsBySource.TryGetValue(index, out var owner) ? (HelperKind, owner)
                : skin.Joints[index].Parent is { } parent ? (SourceKind, parent) : (HelperKind, root);
        var bindWorlds = new Dictionary<(string Kind, int Index), M4>();
        for (var index = 0; index < sourceWorlds.Length; index++) bindWorlds[(SourceKind, index)] = sourceWorlds[index];
        foreach (var (part, world) in helperWorlds) bindWorlds[(HelperKind, part)] = world;
        var physicalParents = new Dictionary<int, (string Kind, int Index)?>();
        foreach (var part in order) physicalParents[part] = incoming.TryGetValue(part, out var joint) ? (HelperKind, joint.Get("m_nBody1").Int()) : null;
        var physicalAxisNames = new Dictionary<int, string>();
        var physicalAxisWorlds = new Dictionary<int, M4>();
        foreach (var (part, parentKey) in physicalParents)
        {
            var parentWorld = parentKey is { } key ? bindWorlds[key] : M4.Identity;
            if (!PhysicalAxisFactorRequired(parentWorld.Inverse() * helperWorlds[part])) continue;
            physicalAxisNames[part] = Unique($"__s2_phys_bind_{part:00}", names);
            physicalAxisWorlds[part] = parentWorld * AxisTurn;
        }
        var axisNames = new Dictionary<int, string>();
        var axisWorlds = new Dictionary<int, M4>();
        foreach (var (index, parentKey) in sourceParents)
        {
            if (CompilerGimbalLoss(bindWorlds[parentKey].Inverse() * sourceWorlds[index]) <= 0.000001) continue;
            axisNames[index] = Unique($"__s2_bind_{index:000}", names);
            axisWorlds[index] = bindWorlds[parentKey] * AxisTurn;
        }
        if (skin.Joints.Count + helperNames.Count + axisNames.Count + physicalAxisNames.Count > MaxBones)
            throw new RagdollPhysicsException("Physical and bind-axis helpers exceed the 256-bone limit");
        var pending = order.Select(part => (HelperKind, part)).Concat(physicalAxisNames.Keys.Select(part => (PhysicalAxisKind, part)))
            .Concat(axisNames.Keys.Select(index => (AxisKind, index))).Concat(Enumerable.Range(0, skin.Joints.Count).Select(index => (SourceKind, index))).ToList();
        var newIndices = new Dictionary<(string Kind, int Index), int>();
        var worlds = new List<M4>();
        var joints = new List<GltfJoint>();
        while (pending.Count > 0)
        {
            var remaining = new List<(string, int)>();
            foreach (var (kind, index) in pending)
            {
                (string Kind, int Index)? parentKey;
                string name;
                M4 world;
                var node = -1;
                switch (kind)
                {
                    case HelperKind:
                        parentKey = physicalAxisNames.ContainsKey(index) ? (PhysicalAxisKind, index) : physicalParents[index];
                        (name, world) = (helperNames[index], helperWorlds[index]);
                        break;
                    case PhysicalAxisKind:
                        parentKey = physicalParents[index];
                        (name, world) = (physicalAxisNames[index], physicalAxisWorlds[index]);
                        break;
                    case AxisKind:
                        parentKey = sourceParents[index];
                        (name, world) = (axisNames[index], axisWorlds[index]);
                        break;
                    default:
                        parentKey = axisNames.ContainsKey(index) ? (AxisKind, index) : sourceParents[index];
                        (name, world, node) = (skin.Joints[index].Name, sourceWorlds[index], skin.Joints[index].NodeIndex);
                        break;
                }
                if (parentKey is { } wanted && !newIndices.ContainsKey(wanted))
                {
                    remaining.Add((kind, index));
                    continue;
                }
                int? parent = parentKey is { } found ? newIndices[found] : null;
                var local = parent is { } above ? worlds[above].Inverse() * world : world;
                newIndices[(kind, index)] = joints.Count;
                joints.Add(new(name, node, parent, world.Inverse(), local.Translation, local.ToQuaternion(), V3.One));
                worlds.Add(world);
            }
            if (remaining.Count == pending.Count) throw new RagdollPhysicsException("Cannot construct an acyclic physical helper skeleton");
            pending = remaining;
        }
        var rootKey = physicalAxisNames.ContainsKey(root) ? (PhysicalAxisKind, root) : (HelperKind, root);
        var combined = new GltfSkin(joints, newIndices[rootKey], skin.SkinIndex);
        var rigidHelpers = axisNames.Keys.Select(index => new RigidHelper(newIndices[(AxisKind, index)], newIndices[sourceParents[index]], AxisTurn)).ToList();
        foreach (var part in physicalAxisNames.Keys)
            rigidHelpers.Add(new(newIndices[(PhysicalAxisKind, part)], physicalParents[part] is { } key ? newIndices[key] : null, AxisTurn));
        var indexMap = new Dictionary<int, int>();
        for (var index = 0; index < skin.Joints.Count; index++) indexMap[index] = newIndices[(SourceKind, index)];
        var helperMap = new Dictionary<int, int>();
        foreach (var part in order) helperMap[part] = newIndices[(HelperKind, part)];
        return new(combined, indexMap, helperMap, offsets, rigidHelpers);
    }

    /// <summary>Expresses helper limits and converts inch-based maximum torque to Nm per child kilogram.</summary>
    /// <remarks>The installed GMod solver applies maximum torque equal to PHY friction times child mass, which independent
    /// installed-engine probes confirmed. Source maximum torque therefore converts by 0.0254 squared before division by
    /// target child mass; the zero-velocity motor control law remains adapted.</remarks>
    private static JsonObject JointReport(JsonNode? joint, M4[] bind, Dictionary<int, string> helpers, List<JsonObject> bodies)
    {
        int parent = joint.Get("m_nBody1").Int(), child = joint.Get("m_nBody2").Int();
        M4 first = bind[parent] * Frame(joint.Get("m_Frame1")), second = bind[child] * Frame(joint.Get("m_Frame2"));
        var difference = PhysicsMaths.RotationOf(first).Transposed() * PhysicsMaths.RotationOf(second);
        var bindTwist = Math.Atan2(difference[1, 0], difference[0, 0]);
        var bindSwing = Math.Atan2(Math.Sqrt(difference[0, 2] * difference[0, 2] + difference[1, 2] * difference[1, 2]), difference[2, 2]);
        var kind = joint.Get("m_nType").Int();
        var twist = joint.Get("m_TwistLimit");
        var swing = joint.Get("m_SwingLimit");
        (double Minimum, double Maximum)[] intervals;
        if (kind == 6) intervals = [(0.0, 0.0), (0.0, 0.0), (0.0, 0.0)];
        else
        {
            var twistInterval = joint.Get("m_bEnableTwistLimit").Bool()
                ? (Fmt.Degrees(twist.Get("m_flMin").Dbl() - bindTwist), Fmt.Degrees(twist.Get("m_flMax").Dbl() - bindTwist))
                : (-360.0, 360.0);
            var cone = joint.Get("m_bEnableSwingLimit").Bool() ? Fmt.Degrees(swing.Get("m_flMax").Dbl()) : 180.0;
            var swingInterval = kind == 3 ? (0.0, 0.0) : (-cone, cone);
            intervals = [swingInterval, swingInterval, twistInterval];
        }
        var axes = new JsonArray();
        for (var axis = 0; axis < 3; axis++)
        {
            var (minimum, maximum) = intervals[axis];
            if (axis < 2) (minimum, maximum) = (-maximum, -minimum);
            axes.Add(new JsonArray("xyz"[axis].ToString(), minimum, maximum));
        }
        var angularMotor = joint.Get("m_bEnableAngularMotor").Bool();
        var torque = angularMotor ? joint.Get("m_flMaxTorque").Dbl() : 0.0;
        var childMass = bodies[child].Get("target_mass_kg").Dbl();
        var squareInches = Math.Pow(0.0254, 2);
        var friction = torque * squareInches / childMass;
        if (joint.Get("_generated").Bool()) friction = 0.2;
        return new()
        {
            ["source_type"] = kind, ["source_parent"] = parent, ["source_child"] = child,
            ["parent_helper"] = helpers[parent], ["child_helper"] = helpers[child],
            ["source_frames"] = new JsonArray(joint.Get("m_Frame1")?.DeepClone(), joint.Get("m_Frame2")?.DeepClone()),
            ["bind_twist_deg"] = Fmt.Degrees(bindTwist), ["bind_swing_deg"] = Fmt.Degrees(bindSwing),
            ["source_pivot_mismatch_in"] = (first.Translation - second.Translation).Length,
            ["target_axes"] = axes, ["target_friction"] = friction,
            ["source_angular_motor_enabled"] = angularMotor,
            ["source_angular_target_velocity"] = joint.Has("m_vAngularTargetVelocity") ? joint.Get("m_vAngularTargetVelocity")?.DeepClone() : new JsonArray(0, 0, 0),
            ["source_max_torque"] = torque,
            ["target_friction_units"] = "Nm_per_child_kg",
            ["target_maximum_torque_nm"] = friction * childMass,
            ["source_maximum_torque_nm"] = torque * squareInches,
            ["angular_motor_adaptation"] = "zero_velocity_torque_surrogate_inch2_to_metre2_per_child_mass",
            ["source_linear_motor_enabled"] = joint.Get("m_bEnableLinearMotor").Bool(),
            ["source_max_force"] = joint.Has("m_flMaxForce") ? joint.Get("m_flMaxForce")?.DeepClone() : 0,
            ["source_joint"] = joint?.DeepClone(),
            ["limit_adaptation"] = kind == 4 ? "coupled_cone_to_independent_axis_envelope" : "bind_referenced",
            ["generated"] = joint.Get("_generated").Bool()
        };
    }

    /// <inheritdoc cref="Prepare(JsonObject?, GltfSkin, CoordinateMode, JsonObject?, IReadOnlyDictionary{long, JsonObject}?, double)"/>
    /// <param name="physPath">The exported PHYS sidecar, or null when the model has none.</param>
    public static RagdollPhysicsResult Prepare(string? physPath, GltfSkin skin, CoordinateMode coordinates, JsonObject? modelData = null,
        IReadOnlyDictionary<long, JsonObject>? surfaceProperties = null, double frictionScale = 1.0) =>
        Prepare(ReadDocument(physPath), skin, coordinates, modelData, surfaceProperties, frictionScale);

    /// <summary>Prepares an authored ragdoll or a clearly identified AO-proxy humanoid fallback.</summary>
    /// <remarks>The returned skin and remapped animation frames are already in Source-inch model space and must be written
    /// with an identity coordinate mode. Original rendering bones retain their names and world bind transforms. The
    /// source document is retained verbatim alongside evaluated values and adaptations.</remarks>
    public static RagdollPhysicsResult Prepare(JsonObject? phys, GltfSkin skin, CoordinateMode coordinates, JsonObject? modelData = null,
        IReadOnlyDictionary<long, JsonObject>? surfaceProperties = null, double frictionScale = 1.0)
    {
        if (!double.IsFinite(frictionScale) || frictionScale <= 0) throw new RagdollPhysicsException("Compiler friction calibration must be positive");
        if (skin.JointNames.Select(NativePhysics.Fold).Distinct().Count() != skin.Joints.Count) throw new RagdollPhysicsException("Ragdoll source bone names must be unique");
        foreach (var name in skin.JointNames) NativePhysics.QcName(name);
        var sourceWorlds = SourceWorld(skin, skin.BindPose(), coordinates);
        var document = phys?.DeepClone().AsObject() ?? [];
        var generated = !document.Get("m_parts").Bool();
        if (generated) document = AoFallback(modelData ?? [], skin, sourceWorlds);
        var parts = document.Get("m_parts").Arr();
        if (parts.Count is < 1 or > MaxBodies) throw new RagdollPhysicsException("Garry's Mod supports between 1 and 32 ragdoll bodies");
        if (skin.Joints.Count + parts.Count > MaxBones) throw new RagdollPhysicsException("Source bones plus physical helpers exceed Garry's Mod's 256 bones");
        var (incoming, root, order) = PartHierarchy(document);
        var lookup = new Dictionary<string, int>();
        for (var index = 0; index < skin.Joints.Count; index++) lookup[skin.Joints[index].Name] = index;
        var names = document.Get("m_boneNames").Arr();
        var parents = document.Get("m_boneParents").Arr();
        var poses = document.Get("m_bindPose").Arr();
        if (parents.Count != parts.Count || poses.Count != parts.Count) throw new RagdollPhysicsException("PHYS part, bind pose and bone indirection counts differ");
        var partNames = new List<string>();
        var partSources = new List<int>();
        foreach (var item in parents)
        {
            var index = item.Int();
            if (index < 0) index += names.Count;
            if (index < 0 || index >= names.Count || !lookup.TryGetValue(names[index].Str(), out var source))
                throw new RagdollPhysicsException("PHYS part refers to an absent rendering bone");
            partNames.Add(names[index].Str());
            partSources.Add(source);
        }
        if (partSources.Distinct().Count() != parts.Count) throw new RagdollPhysicsException("Multiple physics bodies bound to the same source bone");
        var bind = new M4[parts.Count];
        for (var part = 0; part < bind.Length; part++) bind[part] = Rigid(poses[part], $"part {part}");
        for (var part = 0; part < bind.Length; part++)
            for (var row = 0; row < 4; row++)
                for (var column = 0; column < 4; column++)
                    if (!NativePhysics.AllClose(bind[part][row, column], sourceWorlds[partSources[part]][row, column], 0.005, 0.0))
                        throw new RagdollPhysicsException($"PHYS/render bind disagreement on {partNames[part]}");
        var combined = CombinedSkin(skin, sourceWorlds, partSources, bind, incoming, root, order);
        var hashes = document.Get("m_surfacePropertyHashes").Arr();
        var properties = surfaceProperties ?? new Dictionary<long, JsonObject>();
        var shapes = new List<PhysicsShape>();
        var bodies = new List<JsonObject>();
        var warnings = new List<string>();
        for (var partIndex = 0; partIndex < parts.Count; partIndex++)
        {
            var part = parts[partIndex];
            var shape = part.Get("m_rnShape");
            if (shape.Get("m_meshes").Bool()) throw new RagdollPhysicsException("Dynamic triangle-soup physics requires an explicit convex adaptation");
            var bodyShapes = new List<JsonObject>();
            foreach (var (field, kind) in new[] { ("m_spheres", "sphere"), ("m_capsules", "capsule"), ("m_hulls", "hull") })
                foreach (var data in shape.Get(field).Arr())
                {
                    var (mesh, report) = PrepareShape(kind, data);
                    var surfaceIndex = report.Get("surface_property_index").Int();
                    if (surfaceIndex < 0 || surfaceIndex >= hashes.Count) throw new RagdollPhysicsException("Shape has an invalid source surface property index");
                    var surface = Surface(hashes[surfaceIndex].Lng(), properties);
                    var density = surface.Get("physics").Get("density").Dbl(double.NaN);
                    if (!double.IsFinite(density) || density <= 0) throw new RagdollPhysicsException("Source surface density must be finite and positive");
                    report["part"] = partIndex;
                    report["source_surface"] = surface;
                    var model = MeshOps.Transform(mesh, bind[partIndex]);
                    var (low, high) = MeshOps.Bounds(model);
                    report["bounds_model_in"] = new JsonArray(new JsonArray(low.X, low.Y, low.Z), new JsonArray(high.X, high.Y, high.Z));
                    shapes.Add(new(partIndex, model, report));
                    bodyShapes.Add(report);
                }
            if (bodyShapes.Count == 0) throw new RagdollPhysicsException($"PHYS body {partIndex} has no supported shapes");
            var sourceMass = part.Get("m_flMass").Dbl();
            var inertia = part.Has("m_flInertiaScale") ? part.Get("m_flInertiaScale").Dbl(double.NaN) : 1.0;
            var linear = part.Get("m_flLinearDamping").Dbl();
            var angular = part.Get("m_flAngularDamping").Dbl();
            if (new[] { sourceMass, inertia, linear, angular }.Any(value => !double.IsFinite(value) || value < 0)) throw new RagdollPhysicsException("Invalid source body mass, inertia or damping");
            var automatic = 0.0;
            foreach (var item in bodyShapes)
                automatic += item.Get("source_volume_in3").Dbl() * item.Get("source_surface").Get("physics").Get("density").Dbl() * StaticCollision.CubicInchInCubicMetres;
            var wantedMass = sourceMass > 0 ? sourceMass : automatic;
            var targetMass = Math.Max(1.0, wantedMass);
            var dominant = bodyShapes[0];
            foreach (var item in bodyShapes.Skip(1))
                if (item.Get("source_volume_in3").Dbl() > dominant.Get("source_volume_in3").Dbl()) dominant = item;
            var surfaceName = dominant.Get("source_surface").Get("surfacePropertyName").Str();
            var exportVolume = 0.0;
            foreach (var item in bodyShapes) exportVolume += item.Get("export_volume_in3").Dbl();
            bodies.Add(new()
            {
                ["part"] = partIndex, ["source_bone"] = partNames[partIndex], ["helper"] = combined.Skin.Joints[combined.HelperMap[partIndex]].Name,
                ["source_mass_kg"] = sourceMass, ["evaluated_source_mass_kg"] = wantedMass,
                ["mass_mode"] = sourceMass > 0 ? "explicit" : "source_density",
                ["target_mass_kg"] = targetMass, ["mass_floor_delta_kg"] = targetMass - wantedMass,
                ["export_volume_in3"] = exportVolume,
                ["source_surfaceprop"] = surfaceName,
                ["target_surfaceprop"] = surfaceName.Contains("npc_") ? "flesh" : "default",
                ["mixed_shape_surfaces"] = bodyShapes.Select(item => item.Get("surface_property_index").Int()).Distinct().Count() > 1,
                ["target_inertia_scale"] = inertia, ["target_linear_damping"] = linear, ["target_angular_damping"] = angular,
                ["mass_center_override_unsupported"] = part.Get("m_bOverrideMassCenter").Bool(),
                ["source_part"] = part?.DeepClone(), ["shape_count"] = bodyShapes.Count
            });
        }
        var helpers = combined.HelperMap.ToDictionary(pair => pair.Key, pair => combined.Skin.Joints[pair.Value].Name);
        var sourceJoints = document.Get("m_joints").Arr();
        var joints = sourceJoints.Select(joint => JointReport(joint, bind, helpers, bodies)).ToList();
        ContactPolicyResult contacts;
        try
        {
            contacts = RagdollContacts.Derive(bodies, sourceJoints);
        }
        catch (InvalidDataException error)
        {
            throw new RagdollPhysicsException(error.Message);
        }
        if (contacts.Report.Get("status").Str() == "adapted")
            warnings.Add("Source 1 humanoid self-contact is adapted to a tested semantic support-pair policy; all authored joint collision flags are preserved, and excluded inferred pairs are recorded.");
        if (generated) warnings.Add("No source PHYS collision exists: generated humanoid physics uses authored AO proxies and adapted anatomical limits.");
        if (joints.Exists(item => item.Get("source_type").Int() == 4)) warnings.Add("Source coupled swing cones are represented by independent Source 1 angular-limit envelopes.");
        if (joints.Exists(item => item.Get("source_linear_motor_enabled").Bool())) warnings.Add("Source linear joint motors have no ordinary Source 1 ragdoll QC equivalent.");
        if (joints.Exists(item => item.Get("source_angular_motor_enabled").Bool())) warnings.Add("Angular motors use a unit-converted zero-velocity friction surrogate; solver equivalence is unverified.");
        if (shapes.Exists(item => item.Report.Get("thickened").Bool()))
            warnings.Add($"Thin authored collision pieces are individually thickened to {Fmt.G(StaticCollision.ThickenTargetIn)} inches; per-shape growth is recorded.");
        if (bodies.Exists(body => body.Get("mixed_shape_surfaces").Bool())) warnings.Add("Source bodies with mixed shape surface properties use one dominant Source 1 body surface property.");
        if (bodies.Exists(body => body.Get("mass_floor_delta_kg").Dbl() > 0)) warnings.Add("Source 1 enforces a 1 kg minimum mass per simulated body; mass increases are recorded.");
        if (bodies.Exists(body => body.Get("mass_center_override_unsupported").Bool()))
            warnings.Add("Authored per-body mass center overrides have no ordinary jointed QC equivalent and are retained as unsupported metadata.");
        if (shapes.Exists(item => item.Report.Get("source_surface").Get("unresolved").Bool()))
            warnings.Add("An unresolved source surface uses the explicit default density of 2000 kg/m3; automatic mass fidelity is unverified for that surface.");
        if (document.Get("m_pFeModel").Bool()) warnings.Add("Source soft-body simulation is not part of rigid ragdoll physics; its authored data is retained for appearance conversion.");
        warnings.Add("Source surface density determines automatic masses; stock Source 1 surface response and Rubikon solver behavior differ.");
        var attributes = document.Get("m_collisionAttributes").Arr();
        var report2 = new JsonObject
        {
            ["mode"] = generated ? "generated_from_ao_proxy" : "authored_helper_ragdoll",
            ["source_body_count"] = parts.Count, ["helper_bone_count"] = parts.Count,
            ["bind_axis_helper_count"] = combined.RigidHelpers.Count,
            ["bind_axis_helpers"] = new JsonArray(combined.RigidHelpers.Select(item => (JsonNode?)new JsonObject
            {
                ["name"] = combined.Skin.Joints[item.Helper].Name,
                ["parent"] = item.Parent is { } owner ? combined.Skin.Joints[owner].Name : "",
                ["local_transform"] = MatrixRows(item.Local),
                ["purpose"] = "Avoid Source 1 near-gimbal bind loss without moving source or physical frames"
            }).ToArray()),
            ["original_bone_count"] = skin.Joints.Count, ["combined_bone_count"] = combined.Skin.Joints.Count,
            ["root_bone"] = helpers[root], ["source_root_part"] = root,
            ["shape_count"] = shapes.Count, ["source_joint_count"] = joints.Count,
            ["bodies"] = Nodes(bodies), ["joints"] = Nodes(joints), ["shapes"] = Nodes(shapes.Select(item => item.Report)),
            ["collision_pairs"] = new JsonArray(contacts.Pairs.Select(pair => (JsonNode?)NativePhysics.Strings(pair)).ToArray()),
            ["contact_policy"] = contacts.Report,
            ["source_collision_attributes"] = attributes.DeepClone(),
            ["unsupported_collision_attribute_filters"] = attributes.Any(item => new[] { "m_InteractAs", "m_InteractWith", "m_InteractExclude" }.Any(key => item.Get(key).Bool())),
            ["friction_compile_scale"] = frictionScale,
            ["maximum_tessellation_error_in"] = MaxShapeErrorIn,
            ["maximum_tessellation_volume_error"] = MaxShapeVolumeError,
            ["source_total_mass_kg"] = bodies.Sum(body => body.Get("evaluated_source_mass_kg").Dbl()),
            ["target_total_mass_kg"] = bodies.Sum(body => body.Get("target_mass_kg").Dbl()),
            ["source_softbody_present"] = document.Get("m_pFeModel").Bool(),
            ["warnings"] = NativePhysics.Strings(warnings)
        };
        return new()
        {
            Skin = combined.Skin, IndexMap = combined.IndexMap, RootBone = helpers[root], SourceDocument = document, Report = report2, Warnings = warnings,
            HelperByPart = combined.HelperMap, OriginalSkin = skin, OriginalCoordinates = coordinates, HelperOffsets = combined.Offsets, PartSources = partSources,
            Shapes = shapes, BodyReports = bodies, JointReports = joints, FrictionScale = frictionScale, RigidHelpers = combined.RigidHelpers
        };
    }

    /// <summary>Wraps report nodes in an array without copying, so later edits show through both references.</summary>
    public static JsonArray Nodes(IEnumerable<JsonObject> items) => new(items.Select(item => (JsonNode?)item).ToArray());

    /// <summary>Binds unchanged source collision shapes to a standard humanoid animation hierarchy.</summary>
    /// <remarks>The target rig is in Source inches, and its bone origins and axes remain those the playermodel animation and
    /// IK pipeline requires. Projected constraint axes and displaced source anchors are measured per joint, while the
    /// native companion retains its dedicated physical helpers and source pivots.</remarks>
    public static RagdollPhysicsResult AdaptToPlayermodel(RagdollPhysicsResult native, GltfSkin targetSkin, IReadOnlyDictionary<string, string> boneNameMap,
        M4? modelRotation = null, V3? modelTranslation = null)
    {
        var rotation = modelRotation is null ? M4.Identity : PhysicsMaths.RotationOf(modelRotation);
        var translation = modelTranslation ?? V3.Zero;
        var rows = PhysicsMaths.Rows(rotation);
        if (!NativePhysics.IsOrthonormal(rows, 1e-6) || NativePhysics.Determinant(rows) < 0.999999)
            throw new RagdollPhysicsException("Playermodel physics requires a rigid model-space transform");
        var lookup = new Dictionary<string, int>();
        for (var index = 0; index < targetSkin.Joints.Count; index++) lookup[targetSkin.Joints[index].Name] = index;
        var sourceWorlds = SourceWorld(targetSkin, targetSkin.BindPose(), new());
        var mapped = new Dictionary<int, int>();
        var bodies = native.BodyReports.ConvertAll(body => body.DeepClone().AsObject());
        var nameMap = new Dictionary<string, string>();
        foreach (var body in bodies)
        {
            if (!boneNameMap.TryGetValue(body.Get("source_bone").Str(), out var name) || !lookup.TryGetValue(name, out var joint) || mapped.ContainsValue(joint))
                throw new RagdollPhysicsException($"Playermodel lacks a unique physics bone for {body.Get("source_bone").Str()}");
            nameMap[body.Get("helper").Str()] = name;
            body["helper"] = name;
            mapped[body.Get("part").Int()] = joint;
        }
        var shapes = new List<PhysicsShape>();
        var moved = new M4([rotation[0, 0], rotation[0, 1], rotation[0, 2], translation.X, rotation[1, 0], rotation[1, 1], rotation[1, 2], translation.Y,
            rotation[2, 0], rotation[2, 1], rotation[2, 2], translation.Z, 0, 0, 0, 1]);
        foreach (var (part, mesh, report) in native.Shapes)
        {
            var transformed = MeshOps.Transform(mesh, moved);
            var item = report.DeepClone().AsObject();
            var (low, high) = MeshOps.Bounds(transformed);
            item["bounds_model_in"] = new JsonArray(new JsonArray(low.X, low.Y, low.Z), new JsonArray(high.X, high.Y, high.Z));
            shapes.Add(new(part, transformed, item));
        }
        var joints = native.JointReports.ConvertAll(joint => joint.DeepClone().AsObject());
        var poses = native.SourceDocument.Get("m_bindPose").Arr();
        var bindPoses = new M4[poses.Count];
        for (var index = 0; index < bindPoses.Length; index++) bindPoses[index] = Rigid(poses[index], $"part {index}");
        foreach (var item in joints)
        {
            var child = item.Get("source_child").Int();
            var originalFrame = bindPoses[child] * Frame(item.Get("source_frames").Arr()[1]);
            var wantedRotation = rotation * PhysicsMaths.RotationOf(originalFrame);
            var wantedAnchor = rotation.Point(originalFrame.Translation) + translation;
            var target = sourceWorlds[mapped[child]];
            var basis = PhysicsMaths.RotationOf(target).Transposed() * wantedRotation;
            var available = new List<int> { 0, 1, 2 };
            var assignments = new int[3];
            foreach (var sourceAxis in new[] { 2, 0, 1 })
            {
                var axis = available[0];
                foreach (var candidate in available)
                    if (Math.Abs(basis[candidate, sourceAxis]) > Math.Abs(basis[axis, sourceAxis])) axis = candidate;
                available.Remove(axis);
                assignments[sourceAxis] = axis;
            }
            var adapted = new JsonNode?[3];
            var axes = item.Get("target_axes").Arr();
            for (var sourceAxis = 0; sourceAxis < 3; sourceAxis++)
            {
                double low = axes[sourceAxis].Arr()[1].Dbl(), high = axes[sourceAxis].Arr()[2].Dbl();
                if (sourceAxis < 2) (low, high) = (-high, -low);
                var axis = assignments[sourceAxis];
                if (basis[axis, sourceAxis] < 0) (low, high) = (-high, -low);
                if (axis < 2) (low, high) = (-high, -low);
                adapted[axis] = new JsonArray("xyz"[axis].ToString(), low, high);
            }
            item["target_axes"] = new JsonArray(adapted);
            item["parent_helper"] = nameMap[item.Get("parent_helper").Str()];
            item["child_helper"] = nameMap[item.Get("child_helper").Str()];
            item["playermodel_anchor_error_in"] = (wantedAnchor - target.Translation).Length;
            item["playermodel_primary_axis_projection_error_deg"] = Fmt.Degrees(Math.Acos(Math.Min(1.0, Math.Abs(basis[assignments[2], 2]))));
            item["playermodel_source_joint_frame_in_target"] = new JsonArray(Enumerable.Range(0, 3)
                .Select(row => (JsonNode?)Doubles(Enumerable.Range(0, 3).Select(column => basis[row, column]))).ToArray());
            item["limit_adaptation"] = item.Get("limit_adaptation").Str() + "_projected_to_playermodel_bone_axes";
        }
        var warnings = new List<string>(native.Warnings) { "Playermodel physics keeps canonical animation and IK chains; source joint pivots and axes are adapted and measured per joint." };
        var report2 = native.Report.DeepClone().AsObject();
        var rootBone = nameMap[native.RootBone];
        report2["mode"] = "playermodel_adapted_source_physics";
        report2["helper_bone_count"] = 0;
        report2["combined_bone_count"] = targetSkin.Joints.Count;
        report2["bodies"] = Nodes(bodies);
        report2["joints"] = Nodes(joints);
        report2["shapes"] = Nodes(shapes.Select(item => item.Report));
        report2["root_bone"] = rootBone;
        report2["collision_pairs"] = new JsonArray(native.Report.Get("collision_pairs").Arr()
            .Select(pair => (JsonNode?)new JsonArray(nameMap[pair.Arr()[0].Str()], nameMap[pair.Arr()[1].Str()])).ToArray());
        report2["warnings"] = NativePhysics.Strings(warnings);
        if (report2.ContainsKey("contact_policy")) report2["contact_policy"] = RagdollContacts.RemapReport(report2.Get("contact_policy").Obj(), nameMap);
        return new()
        {
            Skin = targetSkin, IndexMap = [], RootBone = rootBone, SourceDocument = native.SourceDocument, Report = report2, Warnings = warnings,
            HelperByPart = mapped, OriginalSkin = native.OriginalSkin, OriginalCoordinates = native.OriginalCoordinates, HelperOffsets = [],
            PartSources = native.PartSources, Shapes = shapes, BodyReports = bodies, JointReports = joints, FrictionScale = native.FrictionScale
        };
    }
}
