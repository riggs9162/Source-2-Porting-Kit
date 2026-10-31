using System.Buffers.Binary;
using System.Diagnostics;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>Compiled physics is malformed or differs from its intended source representation.</summary>
public sealed class PhyAuditException(string message) : Exception(message);

/// <summary>One flat keyvalue block from a .phy text section. Repeated keys keep the last value, and <see cref="Pairs"/> keeps them all.</summary>
public class PhyBlock
{
    public Dictionary<string, string> Values { get; } = [];
    public List<(string Key, string Value)> Pairs { get; } = [];
    public string this[string key] => Values[key];
    public double Number(string key) => double.Parse(Values[key], System.Globalization.CultureInfo.InvariantCulture);
}

/// <summary>One compiled body with its separate convex clouds, in Source-local inches.</summary>
public sealed class PhySolid : PhyBlock
{
    public List<V3[]> Convexes { get; set; } = [];

    /// <summary>How many convexes the binary held, or null when geometry was not read.</summary>
    public int? ConvexCount { get; set; }
}

/// <summary>A parsed compiled .phy file.</summary>
public sealed record PhyData(uint Checksum, int SolidCount, List<PhySolid> Solids, List<PhyBlock> Constraints, List<PhyBlock> CollisionRules, string Text);

/// <summary>Inspects compiled VPhysics solids and calibrates compiler ragdoll parameter semantics.</summary>
public static partial class PhyAudit
{
    private static readonly JsonSerializerOptions Compact = new() { MaxDepth = 512, Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };

    [GeneratedRegex(@"([A-Za-z0-9_]+)\s*\{([^{}]*)\}", RegexOptions.Singleline)]
    private static partial Regex BlockPattern();

    [GeneratedRegex(@"""([^""\r\n]+)""\s*""([^""\r\n]*)""")]
    private static partial Regex PairPattern();

    [GeneratedRegex(@"\bsolid\s*\{[^{}]*\}", RegexOptions.Singleline)]
    private static partial Regex SolidPattern();

    [GeneratedRegex(@"""name""\s*""([^""\r\n]+)""")]
    private static partial Regex NamePattern();

    [GeneratedRegex(@"""mass""\s*""([^""\r\n]+)""")]
    private static partial Regex MassPattern();

    private static JsonArray Row(V3 vector) => new(vector.X, vector.Y, vector.Z);
    private static JsonArray Row(Q4 rotation) => new(rotation.X, rotation.Y, rotation.Z, rotation.W);
    private static JsonArray Box(V3 low, V3 high) => new(Row(low), Row(high));

    /// <summary>Persists the exact rig and collision inputs needed for later independent compiled audits.</summary>
    public static string WriteAuditManifest(string path, PreparedPhysics result)
    {
        var skeleton = new JsonArray(result.Skin.Joints.Select(joint => (JsonNode?)new JsonObject
        {
            ["name"] = joint.Name, ["parent"] = joint.Parent, ["translation"] = Row(joint.RestTranslation), ["rotation"] = Row(joint.RestRotation)
        }).ToArray());
        var shapes = new JsonArray(result.Shapes.Select(shape => (JsonNode?)new JsonObject
        {
            ["part"] = shape.Part,
            ["vertices"] = new JsonArray(shape.Mesh.Vertices.Select(vertex => (JsonNode?)Row(vertex)).ToArray()),
            ["report"] = shape.Report.DeepClone()
        }).ToArray());
        var helpers = new JsonObject();
        foreach (var (part, helper) in result.HelperByPart) helpers[Fmt.Int(part)] = helper;
        var data = new JsonObject
        {
            ["version"] = 1, ["skeleton"] = skeleton, ["root_joint_idx"] = result.Skin.RootIndex, ["helper_by_part"] = helpers,
            ["bodies"] = new JsonArray(result.BodyReports.Select(body => body.DeepClone()).ToArray()),
            ["joints"] = new JsonArray(result.JointReports.Select(joint => joint.DeepClone()).ToArray()),
            ["shapes"] = shapes, ["report"] = result.Report.DeepClone()
        };
        Fmt.WriteText(path, JsonSerializer.Serialize(data, Compact) + "\n");
        return path;
    }

    /// <summary>Loads prepared collision geometry without exporting meshes or sampling animations again.</summary>
    public static PreparedPhysics LoadAuditManifest(string path)
    {
        var data = Json.Load(path);
        if (data.Get("version").Int() != 1) throw new PhyAuditException("Unsupported ragdoll physics audit manifest version");
        var skeleton = data.Get("skeleton").Arr();
        var joints = new List<GltfJoint>();
        for (var index = 0; index < skeleton.Count; index++)
        {
            var item = skeleton[index];
            int? parent = item.Get("parent") is { } value ? value.Int() : null;
            joints.Add(new(item.Get("name").Str(), index, parent, M4.Identity, item.Get("translation").Vec3(), item.Get("rotation").Quat(), V3.One));
        }
        var shapes = data.Get("shapes").Arr().Select(item =>
        {
            var flat = NativePhysics.Numbers(item.Get("vertices"));
            var vertices = new V3[flat.Length / 3];
            for (var index = 0; index < vertices.Length; index++) vertices[index] = new(flat[index * 3], flat[index * 3 + 1], flat[index * 3 + 2]);
            return new PhysicsShape(item.Get("part").Int(), new(vertices, []), item.Get("report").Obj());
        }).ToList();
        var result = new PreparedPhysics
        {
            Skin = new(joints, data.Get("root_joint_idx").Int(), 0),
            HelperByPart = data.Get("helper_by_part").Obj().ToDictionary(pair => int.Parse(pair.Key), pair => pair.Value.Int()),
            BodyReports = data.Get("bodies").Arr().Select(body => body.Obj()).ToList(),
            JointReports = data.Get("joints").Arr().Select(joint => joint.Obj()).ToList(),
            Shapes = shapes, Report = data.Get("report").Obj()
        };
        RestoreAuthoredAnalyticShapes(result);
        return result;
    }

    /// <summary>Recovers a rigid source-to-model bind from deterministic compiler-bootstrap vertex correspondences.</summary>
    private static (double[,] Rotation, V3 Translation) FitShapeBind(V3[] source, V3[] target)
    {
        if (source.Length != target.Length) throw new PhyAuditException("Cannot restore original analytic collision: compiler-bootstrap vertex counts changed");
        var (rotation, translation, _) = PhysicsMaths.FitRigid(source, target);
        var error = 0.0;
        for (var index = 0; index < source.Length; index++)
            error = Math.Max(error, (PhysicsMaths.Apply(rotation, source[index]) + translation - target[index]).Length);
        if (!translation.IsFinite || !double.IsFinite(error) || error > 1e-5)
            throw new PhyAuditException($"Cannot restore original analytic collision: bootstrap bind fit differs by {PhysicsMaths.General(error, 9)} in");
        return (rotation, translation);
    }

    /// <summary>Removes compiler-only analytic inflation from final collision while retaining its bootstrap inputs separately.</summary>
    /// <remarks>Existing manifests keep their thickened meshes as durable compiler input. Deterministic bootstrap
    /// correspondences recover the exact source-to-model shape bind for both native and renamed playermodel rigs without
    /// changing rendering or animation transforms.</remarks>
    private static void RestoreAuthoredAnalyticShapes(PreparedPhysics result)
    {
        result.CompilerBootstrapShapes = [];
        result.GeometryRestorations = [];
        bool Analytic(JsonObject report) => report.Get("thickened").Bool() && report.Get("kind").Str() is "sphere" or "capsule";
        if (!result.Shapes.Exists(shape => Analytic(shape.Report))) return;
        result.CompilerBootstrapBodyReports = result.BodyReports.ConvertAll(body => body.DeepClone().AsObject());
        var bodies = new Dictionary<int, JsonObject>();
        foreach (var body in result.BodyReports) bodies[body.Get("part").Int()] = body;
        var ordinals = new Dictionary<(int, string), int>();
        var shapes = new List<PhysicsShape>();
        foreach (var (part, mesh, report) in result.Shapes)
        {
            var kind = report.Get("kind").Str();
            var key = (part, kind);
            var ordinal = ordinals.GetValueOrDefault(key);
            ordinals[key] = ordinal + 1;
            if (!Analytic(report))
            {
                shapes.Add(new(part, mesh, report));
                continue;
            }
            var body = bodies[part];
            var source = body.Get("source_part").Get("m_rnShape").Get(kind == "sphere" ? "m_spheres" : "m_capsules").Arr();
            if (ordinal >= source.Count) throw new PhyAuditException($"Cannot restore original analytic collision for part {part}: retained source shape is absent");
            var (authored, analytic) = RagdollPhysics.PrimitiveMesh(kind, source[ordinal]);
            var (bootstrap, _) = RagdollPhysics.PrepareShape(kind, source[ordinal]);
            var (rotation, translation) = FitShapeBind(bootstrap.Vertices, mesh.Vertices);
            var bindError = 0.0;
            for (var index = 0; index < bootstrap.Vertices.Length; index++)
                bindError = Math.Max(bindError, (PhysicsMaths.Apply(rotation, bootstrap.Vertices[index]) + translation - mesh.Vertices[index]).Length);
            var restored = new TriMesh(Array.ConvertAll(authored.Vertices, vertex => PhysicsMaths.Apply(rotation, vertex) + translation), authored.Faces);
            result.CompilerBootstrapShapes.Add(new(part, new(mesh.Vertices, mesh.Faces), report.DeepClone().AsObject()));
            var authoredVolume = MeshOps.Volume(authored);
            var (bootstrapLow, bootstrapHigh) = MeshOps.Bounds(mesh.Vertices);
            var (restoredLow, restoredHigh) = MeshOps.Bounds(restored);
            var restoration = new JsonObject
            {
                ["part"] = part, ["source_bone"] = body.Get("source_bone")?.DeepClone(), ["kind"] = kind, ["shape_ordinal"] = ordinal,
                ["source_radius_in"] = analytic.Get("source_radius_in").Dbl(),
                ["analytic_source_volume_in3"] = analytic.Get("source_volume_in3").Dbl(),
                ["compiler_bootstrap_volume_in3"] = report.Get("export_volume_in3").Dbl(),
                ["restored_tessellated_volume_in3"] = authoredVolume,
                ["compiler_bootstrap_vertices"] = mesh.Vertices.Length, ["restored_vertices"] = authored.Vertices.Length,
                ["restored_source_to_model_bind"] = new JsonArray(Enumerable.Range(0, 4).Select(row => (JsonNode?)RagdollPhysics.Doubles(BindRow(rotation, translation, row))).ToArray()),
                ["bootstrap_bind_fit_maximum_error_in"] = bindError,
                ["compiler_bootstrap_bounds_model_in"] = Box(bootstrapLow, bootstrapHigh),
                ["restored_bounds_model_in"] = Box(restoredLow, restoredHigh),
                ["removed_thickness_bounds_growth_in"] = report.Has("thickness_bounds_growth_in") ? report.Get("thickness_bounds_growth_in")?.DeepClone() : 0
            };
            result.GeometryRestorations.Add(restoration);
            var current = report.DeepClone().AsObject();
            foreach (var (field, value) in analytic) current[field] = value?.DeepClone();
            current["export_volume_in3"] = authoredVolume;
            current["source_mesh_volume_in3"] = authoredVolume;
            current["thickened"] = false;
            current["thickness_bounds_growth_in"] = 0.0;
            current["minimum_width_in"] = StaticCollision.ThinnestDirection(authored).Width;
            current["export_vertices"] = authored.Vertices.Length;
            current["export_triangles"] = authored.FaceCount;
            current["bounds_model_in"] = Box(restoredLow, restoredHigh);
            current["compiler_bootstrap_restoration"] = restoration.DeepClone();
            shapes.Add(new(part, restored, current));
        }
        result.Shapes = shapes;
        foreach (var body in result.BodyReports)
        {
            var total = 0.0;
            foreach (var (part, _, report) in shapes)
                if (part == body.Get("part").Int()) total += report.Get("export_volume_in3").Dbl();
            body["export_volume_in3"] = total;
        }
        result.Report["bodies"] = new JsonArray(result.BodyReports.Select(body => body.DeepClone()).ToArray());
        result.Report["shapes"] = new JsonArray(shapes.Select(shape => shape.Report.DeepClone()).ToArray());
        result.Report["compiler_bootstrap_geometry_restorations"] = result.GeometryRestorations.DeepClone();
        var warnings = result.Report.Get("warnings").Arr().Select(warning => warning.Str())
            .Where(warning => !warning.StartsWith("Thin authored collision pieces are individually thickened", StringComparison.Ordinal)).ToList();
        if (shapes.Exists(shape => shape.Report.Get("thickened").Bool())) warnings.Add("Remaining compiler-thickened non-analytic shapes are retained as reported adaptations.");
        warnings.Add("Compiler-bootstrap analytic thickness repairs are removed from final native collision; original source radii are restored.");
        result.Report["warnings"] = NativePhysics.Strings(warnings);
    }

    private static double[] BindRow(double[,] rotation, V3 translation, int row) =>
        row == 3 ? [0, 0, 0, 1] : [rotation[row, 0], rotation[row, 1], rotation[row, 2], translation[row]];

    /// <summary>Reads the pairs of one flat keyvalue block as ordinary Source 1 PHY files emit them.</summary>
    private static void Fill(PhyBlock block, string body)
    {
        foreach (Match pair in PairPattern().Matches(body))
        {
            block.Values[pair.Groups[1].Value] = pair.Groups[2].Value;
            block.Pairs.Add((pair.Groups[1].Value, pair.Groups[2].Value));
        }
    }

    /// <summary>Reads compact-surface leaf vertices and converts IVP metres into Source-local inches.</summary>
    /// <remarks>A VPHY surface owns a relative-offset binary ledge tree. Internal ledges describe acceleration bounds, while
    /// terminal ledges are the actual convex collision pieces. The layout uses 28-byte tree nodes, 16-byte ledge headers
    /// and 16-byte edge triangles. Every offset is checked against its solid boundary before any vertices are read.</remarks>
    public static List<V3[]> ConvexClouds(ReadOnlySpan<byte> blob)
    {
        if (blob.Length < 76 || !blob[..4].SequenceEqual("VPHY"u8)) throw new PhyAuditException("Unsupported collision solid header");
        const int surface = 28;
        if (!blob.Slice(surface + 44, 4).SequenceEqual("IVPS"u8)) throw new PhyAuditException("Only ordinary IVPS compact collision surfaces are supported");
        var root = surface + (long)BinaryPrimitives.ReadUInt32LittleEndian(blob[(surface + 32)..]);
        var output = new List<V3[]>();
        var pending = new Stack<long>([root]);
        var seen = new HashSet<long>();
        while (pending.TryPop(out var node))
        {
            if (!seen.Add(node) || node < 0 || node > blob.Length - 28) throw new PhyAuditException("Invalid or cyclic compact collision tree");
            var right = BinaryPrimitives.ReadInt32LittleEndian(blob[(int)node..]);
            var ledge = BinaryPrimitives.ReadInt32LittleEndian(blob[((int)node + 4)..]);
            if (right != 0)
            {
                pending.Push(node + 28);
                pending.Push(node + right);
                continue;
            }
            var offset = node + ledge;
            if (ledge == 0 || offset < 0 || offset > blob.Length - 16) throw new PhyAuditException("Invalid terminal collision ledge");
            var verticesOffset = BinaryPrimitives.ReadInt32LittleEndian(blob[(int)offset..]);
            int triangleCount = BinaryPrimitives.ReadUInt16LittleEndian(blob[((int)offset + 12)..]);
            if (triangleCount < 4 || offset + 16 + triangleCount * 16L > blob.Length) throw new PhyAuditException("Invalid convex triangle count");
            var indices = new SortedSet<int>();
            for (var triangle = 0; triangle < triangleCount; triangle++)
                for (var edge = 0; edge < 3; edge++)
                    indices.Add(BinaryPrimitives.ReadUInt16LittleEndian(blob[((int)offset + 20 + triangle * 16 + edge * 4)..]));
            var start = offset + verticesOffset;
            if (start < 0 || start + (indices.Max + 1) * 16L > blob.Length) throw new PhyAuditException("Convex vertex data escapes its solid boundary");
            var points = new V3[indices.Count];
            var write = 0;
            foreach (var index in indices)
            {
                points[write] = ReadPoint(blob, (int)start + index * 16);
                if (!points[write].IsFinite) throw new PhyAuditException("Non-finite compiled collision vertices");
                write++;
            }
            output.Add(points);
        }
        return output;
    }

    /// <summary>Converts one serialized IVP vertex from metres in its own axis order to Source inches.</summary>
    public static V3 ReadPoint(ReadOnlySpan<byte> blob, int at)
    {
        double x = BinaryPrimitives.ReadSingleLittleEndian(blob[at..]);
        double y = BinaryPrimitives.ReadSingleLittleEndian(blob[(at + 4)..]);
        double z = BinaryPrimitives.ReadSingleLittleEndian(blob[(at + 8)..]);
        return new(x / 0.0254, z / 0.0254, y / 0.0254 * -1.0);
    }

    /// <summary>Reads compiled bodies, constraints, collision rules and separate convex clouds.</summary>
    public static PhyData ReadPhy(string path, bool geometry = true)
    {
        var data = File.ReadAllBytes(path);
        if (data.Length < 16) throw new PhyAuditException("Truncated PHY header");
        var size = BinaryPrimitives.ReadUInt32LittleEndian(data);
        var solidCount = BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(8));
        var checksum = BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(12));
        if (size < 16 || size > data.Length || solidCount > 256) throw new PhyAuditException("Invalid PHY header");
        var position = (long)size;
        var clouds = new List<List<V3[]>>();
        for (var index = 0; index < solidCount; index++)
        {
            if (position + 4 > data.Length) throw new PhyAuditException("Missing serialized PHY solid");
            var length = BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan((int)position));
            var end = position + 4 + length;
            if (length < 76 || end > data.Length) throw new PhyAuditException("Truncated serialized PHY solid");
            clouds.Add(geometry ? ConvexClouds(data.AsSpan((int)position + 4, (int)length)) : []);
            position = end;
        }
        var text = TailText(data, position);
        var solids = new List<PhySolid>();
        var constraints = new List<PhyBlock>();
        var rules = new List<PhyBlock>();
        foreach (Match match in BlockPattern().Matches(text))
        {
            var name = match.Groups[1].Value.ToLowerInvariant();
            PhyBlock block = name == "solid" ? new PhySolid() : new PhyBlock();
            Fill(block, match.Groups[2].Value);
            if (block is PhySolid solid) solids.Add(solid);
            else if (name == "ragdollconstraint") constraints.Add(block);
            else if (name == "collisionrules") rules.Add(block);
        }
        if (solids.Count != solidCount) throw new PhyAuditException("PHY solid descriptions disagree with the binary header");
        for (var index = 0; index < solids.Count; index++)
        {
            solids[index].Convexes = clouds[index];
            solids[index].ConvexCount = geometry ? clouds[index].Count : null;
        }
        return new(checksum, (int)solidCount, solids, constraints, rules, text);
    }

    private static string TailText(byte[] data, long position)
    {
        var tail = data.AsSpan((int)position);
        var separator = tail.IndexOf((byte)0);
        var text = separator >= 0 ? tail[..separator] : tail;
        if (!Ascii.IsValid(text)) throw new PhyAuditException("PHY keyvalue text is not ASCII");
        return Encoding.ASCII.GetString(text);
    }

    /// <summary>Compares actual compiled collision geometry and parameters with the prepared physics.</summary>
    public static JsonObject AuditCompiledPhy(string path, PreparedPhysics result, double massToleranceKg = 0.02, double maximumBoundsErrorIn = 0.06,
        double maximumVolumeError = 0.02, bool allowGeometryAdaptation = true)
    {
        var compiled = ReadPhy(path);
        var expected = new Dictionary<string, JsonObject>();
        foreach (var body in result.BodyReports) expected[body.Get("helper").Str()] = body;
        var actual = new Dictionary<string, PhySolid>();
        foreach (var solid in compiled.Solids) actual[solid["name"]] = solid;
        var errors = new List<string>();
        var bodies = new JsonArray();
        var adaptations = new List<string>();
        var missing = expected.Keys.Except(actual.Keys).Order(StringComparer.Ordinal).ToList();
        var extra = actual.Keys.Except(expected.Keys).Order(StringComparer.Ordinal).ToList();
        if (missing.Count > 0 || extra.Count > 0)
            errors.Add($"Compiled physics bones differ: missing=[{string.Join(", ", missing.Select(name => $"'{name}'"))}], extra=[{string.Join(", ", extra.Select(name => $"'{name}'"))}]");
        var worlds = result.Skin.WorldMatrices();
        var inverse = result.HelperByPart.ToDictionary(pair => pair.Key, pair => worlds[pair.Value].Inverse());
        foreach (var name in expected.Keys.Intersect(actual.Keys).Order(StringComparer.Ordinal))
        {
            var (wanted, found) = (expected[name], actual[name]);
            var part = wanted.Get("part").Int();
            var sourcePoints = result.Shapes.Where(shape => shape.Part == part).SelectMany(shape => shape.Mesh.Vertices.Select(inverse[part].Point)).ToList();
            var compiledClouds = found.Convexes;
            var volume = 0.0;
            foreach (var cloud in compiledClouds)
                volume += ConvexHull.Of(cloud) is { } hull ? Math.Abs(MeshOps.Volume(hull)) : 0.0;
            var volumeError = Math.Abs(volume / wanted.Get("export_volume_in3").Dbl() - 1.0);
            var (sourceLow, sourceHigh) = MeshOps.Bounds(sourcePoints);
            var (compiledLow, compiledHigh) = MeshOps.Bounds(compiledClouds.SelectMany(cloud => cloud));
            var boundsError = 0.0;
            for (var axis = 0; axis < 3; axis++)
                boundsError = Math.Max(boundsError, Math.Max(Math.Abs(sourceLow[axis] - compiledLow[axis]), Math.Abs(sourceHigh[axis] - compiledHigh[axis])));
            var massError = Math.Abs(found.Number("mass") - wanted.Get("target_mass_kg").Dbl());
            if (compiledClouds.Count != wanted.Get("shape_count").Int())
                errors.Add($"{name}: compiled {compiledClouds.Count} convex pieces, expected {wanted.Get("shape_count").Int()}");
            if (massError > massToleranceKg) errors.Add($"{name}: compiled mass differs by {Fmt.F6(massError)} kg");
            if (boundsError > maximumBoundsErrorIn || volumeError > maximumVolumeError)
            {
                var message = $"{name}: compiler convex rebuilding changed bounds by {Fmt.F6(boundsError)} in and volume by {PhysicsMaths.Percent(volumeError, 3)}";
                if (allowGeometryAdaptation) adaptations.Add(message);
                else errors.Add(message);
            }
            foreach (var (field, key) in new[] { ("inertia", "target_inertia_scale"), ("damping", "target_linear_damping"), ("rotdamping", "target_angular_damping") })
            {
                var value = found.Values.TryGetValue(field, out var text) ? double.Parse(text, System.Globalization.CultureInfo.InvariantCulture) : 0.0;
                if (!NativePhysics.IsClose(value, wanted.Get(key).Dbl(), 1e-9, 1e-5)) errors.Add($"{name}: compiled {field} differs from target");
            }
            bodies.Add(new JsonObject
            {
                ["bone"] = name, ["compiled_convex_count"] = compiledClouds.Count, ["compiled_mass_kg"] = found.Number("mass"), ["mass_error_kg"] = massError,
                ["compiled_volume_in3"] = volume, ["relative_volume_error"] = volumeError, ["maximum_bounds_error_in"] = boundsError
            });
        }
        var indexed = new Dictionary<int, string>();
        foreach (var solid in compiled.Solids) indexed[int.Parse(solid["index"])] = solid["name"];
        var constraints = new Dictionary<(string, string), PhyBlock>();
        foreach (var item in compiled.Constraints) constraints[(indexed[int.Parse(item["parent"])], indexed[int.Parse(item["child"])])] = item;
        if (constraints.Count != result.JointReports.Count) errors.Add("Compiled joint count differs from the prepared physics tree");
        foreach (var wanted in result.JointReports)
        {
            var pair = (wanted.Get("parent_helper").Str(), wanted.Get("child_helper").Str());
            if (!constraints.TryGetValue(pair, out var found))
            {
                errors.Add($"Missing compiled constraint ('{pair.Item1}', '{pair.Item2}')");
                continue;
            }
            foreach (var entry in wanted.Get("target_axes").Arr())
            {
                var axis = entry.Arr()[0].Str();
                foreach (var (suffix, target) in new[] { ("min", entry.Arr()[1].Dbl()), ("max", entry.Arr()[2].Dbl()), ("friction", wanted.Get("target_friction").Dbl()) })
                    if (!NativePhysics.IsClose(found.Number(axis + suffix), target, 1e-9, 2e-4))
                        errors.Add($"{pair.Item2}: compiled {axis}{suffix} differs from target");
            }
        }
        var expectedPairs = result.Report.Get("collision_pairs").Arr().Select(item => Sorted(item.Arr()[0].Str(), item.Arr()[1].Str())).ToHashSet();
        var actualPairs = new HashSet<(string, string)>();
        foreach (var rules in compiled.CollisionRules)
            foreach (var (key, value) in rules.Pairs)
                if (key.Equals("collisionpair", StringComparison.OrdinalIgnoreCase))
                {
                    var indices = value.Split(',');
                    actualPairs.Add(Sorted(indexed[int.Parse(indices[0])], indexed[int.Parse(indices[1])]));
                }
        if (!actualPairs.SetEquals(expectedPairs)) errors.Add("Compiled collision pair whitelist differs from the prepared source filters");
        return new()
        {
            ["ok"] = errors.Count == 0, ["errors"] = NativePhysics.Strings(errors), ["solid_count"] = compiled.SolidCount, ["constraint_count"] = compiled.Constraints.Count,
            ["bodies"] = bodies, ["geometry_verified"] = true, ["geometry_within_tolerance"] = adaptations.Count == 0,
            ["adaptations"] = NativePhysics.Strings(adaptations), ["checksum"] = compiled.Checksum
        };
    }

    private static (string, string) Sorted(string first, string second) => string.CompareOrdinal(first, second) <= 0 ? (first, second) : (second, first);

    /// <summary>Restores requested per-body masses after compiler convex rebuilding redistributes them.</summary>
    /// <remarks>Only solid mass keyvalues are rewritten. The compiler's collision binary, model checksum, constraint values
    /// and collision whitelist remain intact, and the audit record retains the compiler's original masses.</remarks>
    public static JsonObject FinalizeCompiledPhy(string path, PreparedPhysics result)
    {
        var data = File.ReadAllBytes(path);
        var parsed = ReadPhy(path, false);
        var desired = new Dictionary<string, double>();
        foreach (var body in result.BodyReports) desired[body.Get("helper").Str()] = body.Get("target_mass_kg").Dbl();
        long position = BinaryPrimitives.ReadUInt32LittleEndian(data);
        for (var index = 0; index < parsed.SolidCount; index++) position += 4 + BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan((int)position));
        var text = TailText(data, position);
        var changes = new JsonArray();
        var rewritten = SolidPattern().Replace(text, match =>
        {
            var block = match.Value;
            var name = NamePattern().Match(block);
            if (!name.Success || !desired.TryGetValue(name.Groups[1].Value, out var target)) throw new PhyAuditException("Cannot finalize an unexpected compiled physics bone");
            var mass = MassPattern().Match(block);
            if (!mass.Success) throw new PhyAuditException("Compiled body has no mass keyvalue");
            changes.Add(new JsonObject
            {
                ["bone"] = name.Groups[1].Value,
                ["compiler_mass_kg"] = double.Parse(mass.Groups[1].Value, System.Globalization.CultureInfo.InvariantCulture),
                ["final_mass_kg"] = target
            });
            var value = mass.Groups[1];
            return block[..value.Index] + Fmt.F9(target) + block[(value.Index + value.Length)..];
        });
        if (changes.Count != desired.Count) throw new PhyAuditException("Cannot finalize masses with missing or additional compiled bodies");
        using (var stream = new MemoryStream())
        {
            stream.Write(data.AsSpan(0, (int)position));
            stream.Write(Encoding.ASCII.GetBytes(rewritten));
            stream.Write(data.AsSpan((int)position + Encoding.ASCII.GetByteCount(text)));
            File.WriteAllBytes(path, stream.ToArray());
        }
        var report = new JsonObject
        {
            ["method"] = "compiled_phy_solid_mass_keyvalues", ["bodies"] = changes, ["binary_collision_unchanged"] = true, ["checksum"] = parsed.Checksum
        };
        result.Report["compiled_mass_finalization"] = report.DeepClone();
        return report;
    }

    /// <summary>Compiles a QC the way v3 does: studiomdl returns 0 on several failures, so success also needs "Completed".</summary>
    /// <remarks>Source SDK variables override -game when set, so they are removed from the child's environment, which the
    /// shared process runner cannot do. -game must be the folder holding gameinfo.txt; the install root crashes the
    /// compiler outright. studiomdl also says Completed when a locked file was left stale, so siblings are compared.</remarks>
    private static bool CompileQc(string qc, string studiomdl, string game, PortContext context)
    {
        var resolved = File.Exists(Path.Combine(game, "gameinfo.txt")) ? game : Path.Combine(game, "csgo");
        if (!File.Exists(Path.Combine(resolved, "gameinfo.txt")))
        {
            context.Log($"-game is invalid: {game} has no gameinfo.txt (and no csgo subfolder with one) - studiomdl would crash", LogLevel.Error);
            return false;
        }
        var start = new ProcessStartInfo(studiomdl)
        {
            UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true,
            WorkingDirectory = Path.GetDirectoryName(Path.GetFullPath(qc)) ?? ""
        };
        foreach (var argument in new[] { "-game", resolved, "-nop4", qc }) start.ArgumentList.Add(argument);
        foreach (var name in new[] { "VPROJECT", "SteamAppId", "SteamGameId" }) start.Environment.Remove(name);
        using var process = Process.Start(start) ?? throw new PhyAuditException("studiomdl could not be started");
        using var kill = context.Token.Register(() =>
        {
            try
            {
                if (!process.HasExited) process.Kill(true);
            }
            catch (InvalidOperationException)
            {
            }
        });
        var errors = process.StandardError.ReadToEndAsync();
        var output = process.StandardOutput.ReadToEnd();
        process.WaitForExit();
        context.Token.ThrowIfCancellationRequested();
        var transcript = output + errors.GetAwaiter().GetResult();
        if (process.ExitCode != 0 || !transcript.Contains("Completed", StringComparison.Ordinal)) return false;
        var target = ModelPathFor(qc, resolved);
        if (target is null || !File.Exists(target)) return true;
        var checksum = Checksum(target, 8);
        var siblings = new (string Path, int Offset)[]
        {
            (Path.ChangeExtension(target, ".vvd"), 8), (Path.ChangeExtension(target, ".phy"), 12), (Path.ChangeExtension(target, ".dx90.vtx"), 16),
            (Path.ChangeExtension(target, ".dx80.vtx"), 16), (Path.ChangeExtension(target, ".sw.vtx"), 16)
        };
        foreach (var (sibling, offset) in siblings)
            if (File.Exists(sibling) && Checksum(sibling, offset) != checksum)
            {
                context.Log($"{Path.GetFileName(sibling)} is left over from an earlier compile - close whatever has it open and compile again", LogLevel.Error);
                return false;
            }
        return true;
    }

    private static int? Checksum(string path, int offset)
    {
        using var stream = File.OpenRead(path);
        var bytes = new byte[4];
        stream.Seek(offset, SeekOrigin.Begin);
        return stream.Read(bytes) == 4 ? BinaryPrimitives.ReadInt32LittleEndian(bytes) : null;
    }

    private static string? ModelPathFor(string qc, string game)
    {
        foreach (var line in File.ReadLines(qc))
        {
            var stripped = line.Trim();
            if (!stripped.StartsWith("$modelname", StringComparison.OrdinalIgnoreCase)) continue;
            var name = stripped["$modelname".Length..].Trim().Trim('"').Trim();
            if (name.Length == 0) return null;
            var target = Path.Combine(game, "models", name);
            return Path.HasExtension(target) ? target : target + ".mdl";
        }
        return null;
    }

    /// <summary>Compiles an isolated known ragdoll to measure the installed compiler's friction scale.</summary>
    public static JsonObject CalibrateCompiler(string studiomdl, string game, string scratchDirectory, PortContext context)
    {
        scratchDirectory = Path.GetFullPath(scratchDirectory);
        Directory.CreateDirectory(scratchDirectory);
        var cachePath = Path.Combine(scratchDirectory, "compiler_calibration.json");
        var compiler = new FileInfo(Path.GetFullPath(studiomdl));
        var modified = (compiler.LastWriteTimeUtc - DateTime.UnixEpoch).Ticks * 100;
        if (File.Exists(cachePath))
        {
            var cached = Json.Load(cachePath);
            if (cached.Get("compiler").Str() == compiler.FullName && cached.Get("compiler_size").Lng() == compiler.Length &&
                cached.Get("compiler_modified_ns").Lng() == modified && cached.Get("probe_version").Int() == 2)
                return cached;
        }
        var skin = new GltfSkin([
            new("root", 0, null, M4.Identity, V3.Zero, Q4.Identity, V3.One),
            new("child", 1, 0, M4.Identity, new(6, 0, 0), Q4.Identity, V3.One)], 0, 0);
        var shape = new JsonArray(new JsonObject
        {
            ["m_Sphere"] = new JsonObject { ["m_vCenter"] = new JsonArray(0, 0, 0), ["m_flRadius"] = 2 }, ["m_nSurfacePropertyIndex"] = 0
        });
        JsonObject Part(int mass) => new() { ["m_flMass"] = mass, ["m_rnShape"] = new JsonObject { ["m_spheres"] = shape.DeepClone() } };
        var document = new JsonObject
        {
            ["m_boneNames"] = NativePhysics.Strings(["root", "child"]), ["m_boneParents"] = new JsonArray(0, 1),
            ["m_bindPose"] = new JsonArray(new JsonArray(1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0), new JsonArray(1, 0, 0, 6, 0, 1, 0, 0, 0, 0, 1, 0)),
            ["m_surfacePropertyHashes"] = new JsonArray(1977497166L), ["m_parts"] = new JsonArray(Part(2), Part(3)),
            ["m_joints"] = new JsonArray(new JsonObject
            {
                ["m_nType"] = 3, ["m_nBody1"] = 0, ["m_nBody2"] = 1, ["m_Frame1"] = new JsonArray(6, 0, 0, 0, 0, 0, 1), ["m_Frame2"] = new JsonArray(0, 0, 0, 0, 0, 0, 1),
                ["m_bEnableTwistLimit"] = true, ["m_TwistLimit"] = new JsonObject { ["m_flMin"] = -0.2, ["m_flMax"] = 0.3 }
            })
        };
        var result = RagdollPhysics.Prepare(document, skin, new());
        result.JointReports[0]["target_friction"] = 1.0;
        var qcLines = result.WriteCollision(Path.Combine(scratchDirectory, "collision.smd"));
        SmdSkeleton.WriteAnimation(result.Skin, [result.Skin.BindPose()], Path.Combine(scratchDirectory, "bind.smd"), new());
        const string modelName = "s2kit_calibration/ragdoll_parameter_probe.mdl";
        var qc = Path.Combine(scratchDirectory, "probe.qc");
        Fmt.WriteText(qc, string.Join('\n', new[] { $"$modelname \"{modelName}\"", "$body body \"collision.smd\"", "$surfaceprop \"default\"" }
            .Concat(SmdSkeleton.DefineBoneLines(result.Skin, new(), 90)).Concat([$"$root \"{result.RootBone}\"", "$sequence idle \"bind.smd\""]).Concat(qcLines)) + "\n");
        if (!CompileQc(qc, studiomdl, game, context)) throw new PhyAuditException("Compiler calibration probe failed");
        var resolved = File.Exists(Path.Combine(game, "gameinfo.txt")) ? game : Path.Combine(game, "csgo");
        var modelPath = ModelPathFor(qc, resolved) ?? throw new PhyAuditException("Could not locate compiled calibration model");
        var phy = Path.ChangeExtension(modelPath, ".phy");
        var actual = ReadPhy(phy);
        var scale = actual.Constraints[0].Number("zfriction");
        if (!double.IsFinite(scale) || scale <= 0) throw new PhyAuditException("Compiler produced invalid calibration friction");
        var compilerMasses = RagdollPhysics.Doubles(actual.Solids.Select(item => item.Number("mass")));
        FinalizeCompiledPhy(phy, result);
        result.JointReports[0]["target_friction"] = scale;
        var calibration = new JsonObject
        {
            ["compiler"] = compiler.FullName, ["compiler_size"] = compiler.Length, ["compiler_modified_ns"] = modified, ["probe_version"] = 2,
            ["friction_compile_scale"] = scale, ["probe_phy"] = phy, ["solid_count"] = actual.SolidCount, ["compiled_masses_kg"] = compilerMasses,
            ["geometry_audit"] = AuditCompiledPhy(phy, result)
        };
        Fmt.WriteText(cachePath, JsonSerializer.Serialize(calibration, Json.Options) + "\n");
        return calibration;
    }
}
