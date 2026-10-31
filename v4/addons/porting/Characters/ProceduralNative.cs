using System.Buffers.Binary;
using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>What a native procedural pass wrote and which authored constraints it took over or declined.</summary>
public sealed record NativeProcedurals(List<string> QcLines, List<string> Files, JsonArray Records, JsonArray Rejected);

/// <summary>Compiles exactly representable authored bone constraints into native procedural data.</summary>
public static class ProceduralNative
{
    private static M4 Matrix(JsonNode? position, JsonNode? quaternion) => M4.Trs(position.Vec3(), quaternion.Quat());

    /// <summary>Uses the compiler's namespace-stripped procedural-bone identifier.</summary>
    /// <remarks>A <c>ValveBiped.</c> prefix inside a VRD compiles clean and silently produces no procedural bone at all.</remarks>
    private static string VrdName(string name)
    {
        if (name.Length == 0 || name.Any(char.IsWhiteSpace) || name.AsSpan().IndexOfAny("\"\0\r\n") >= 0)
            throw new InvalidDataException("Native procedural bone names must be single safe tokens");
        return name.StartsWith("ValveBiped.", StringComparison.Ordinal) ? name["ValveBiped.".Length..] : name;
    }

    /// <summary>Rejects degenerate axes rather than assigning a new authored orientation.</summary>
    private static V3 Normalized(V3 vector, string label)
    {
        var length = vector.Length;
        if (!vector.IsFinite || length < 1e-8) throw new InvalidDataException($"{label} is not a finite nonzero three-dimensional vector");
        return vector / length;
    }

    private static V3 Normalized(JsonNode? value, string label)
    {
        var numbers = value.Doubles();
        if (numbers.Length != 3) throw new InvalidDataException($"{label} is not a finite nonzero three-dimensional vector");
        return Normalized(new V3(numbers[0], numbers[1], numbers[2]), label);
    }

    /// <summary>Requires every exported translation key and cubic tangent to retain the source bind origin.</summary>
    /// <returns>Whether the origin stays put, and the largest component offset found.</returns>
    private static (bool Constant, double Maximum) ConstantTranslation(GltfDocument gltf, string sourceName)
    {
        var nodes = new Dictionary<int, JsonNode?>();
        for (var index = 0; index < gltf.Nodes.Count; index++)
            if (gltf.Nodes[index].Has("name") && gltf.Nodes[index].Get("name").Str() == sourceName) nodes[index] = gltf.Nodes[index];
        if (nodes.Count != 1) throw new InvalidDataException("Native procedural animation verification needs one unambiguous source bone");
        var maximum = 0.0;
        foreach (var animation in gltf.Root.Get("animations").Arr())
            foreach (var channel in animation.Get("channels").Arr())
            {
                var target = channel.Get("target");
                if (!target.Has("node") || !nodes.TryGetValue(target.Get("node").Int(), out var node) || target.Get("path").Str() != "translation") continue;
                var neutral = node.Has("matrix") ? new V3(node.Get("matrix").Doubles()[12], node.Get("matrix").Doubles()[13], node.Get("matrix").Doubles()[14])
                    : node.Has("translation") ? node.Get("translation").Vec3() : V3.Zero;
                var sampler = animation.Need("samplers").Arr()[channel.Need("sampler").Int()];
                var values = gltf.Accessor(sampler.Need("output").Int());
                if (values.Width != 3 || !values.Values.All(double.IsFinite)) throw new InvalidDataException("Native procedural translation keys are not finite three-dimensional values");
                var mode = sampler.Has("interpolation") ? sampler.Get("interpolation").Str() : "LINEAR";
                if (mode is not ("LINEAR" or "STEP" or "CUBICSPLINE")) throw new InvalidDataException("Native procedural translation uses an unsupported interpolation mode");
                if (mode == "CUBICSPLINE" && values.Count % 3 != 0) throw new InvalidDataException("Native procedural translation has invalid cubic keys");
                for (var row = 0; row < values.Count; row++)
                {
                    var tangent = mode == "CUBICSPLINE" && row % 3 != 1;
                    var offset = tangent ? values.Vector(row) : values.Vector(row) - neutral;
                    maximum = Math.Max(maximum, Math.Max(Math.Abs(offset.X), Math.Max(Math.Abs(offset.Y), Math.Abs(offset.Z))));
                }
            }
        return (maximum <= 1e-7, maximum);
    }

    /// <summary>Reduces a full-weight aim at its own parent to an exact constant local transform.</summary>
    /// <remarks>When the aim target and the up frame both ride the slave's parent, the aimed orientation never changes in
    /// that parent's frame, so a one-trigger quaternion procedure with a full-turn tolerance reproduces the constraint
    /// without any runtime evaluation. Anything less exact is left to the Lua driver.</remarks>
    private static JsonObject ParentLocalAim(JsonNode? constraint, JsonObject frames, GltfSkin skin)
    {
        var slaves = constraint.Need("slaves").Arr();
        var targets = constraint.Need("targets").Arr();
        var aimTargets = targets.Count > 1 ? targets.Take(targets.Count - 1).ToList() : targets.ToList();
        if (constraint.Need("up_type").Int() != 3 || slaves.Count != 1 || aimTargets.Count != 1)
            throw new InvalidDataException("Native constant aim requires one slave, one aim target and parent-relative up");
        var slave = slaves[0];
        var target = aimTargets[0];
        if (Math.Abs(slave.Need("weight").Dbl() - 1) > 1e-7 || Math.Abs(target.Need("weight").Dbl() - 1) > 1e-7)
            throw new InvalidDataException("Native constant aim does not reproduce partial constraint blending");
        if (target.Need("bone").Str() != slave.Need("parent").Str()) throw new InvalidDataException("Aim target does not share the constrained bone's authored parent frame");
        var joints = new Dictionary<string, GltfJoint>(StringComparer.Ordinal);
        foreach (var joint in skin.Joints) joints[joint.Name] = joint;
        JsonNode? slaveFrame = frames.Need(slave.Need("bone").Str()), parentFrame = frames.Need(slave.Need("parent").Str());
        string name = slaveFrame.Need("target").Str(), parentName = parentFrame.Need("target").Str();
        if (!joints.TryGetValue(name, out var bone)) throw new KeyNotFoundException($"'{name}'");
        if (!joints.TryGetValue(parentName, out var parent)) throw new KeyNotFoundException($"'{parentName}'");
        var actualParent = bone.Parent is { } owner ? skin.Joints[owner].Name : "";
        if (actualParent != parentName || parent.Parent is null) throw new InvalidDataException("Native control hierarchy differs from the authored parent-relative frame");
        var sourceLocal = JsonKeys.Matrix(parentFrame.Need("source_bind_world")).Inverse() * JsonKeys.Matrix(slaveFrame.Need("source_bind_world"));
        var attachment = Matrix(target.Need("attachment_position"), target.Need("attachment_rotation")) * Matrix(target.Need("position_offset"), target.Need("rotation_offset"));
        var direction = Normalized(attachment.Translation - sourceLocal.Translation, "Authored aim direction");
        var up = Normalized(constraint.Need("up_vector"), "Authored up vector");
        up = Normalized(up - direction * up.Dot(direction), "Projected authored up vector");
        var side = Normalized(direction.Cross(up), "Authored aim side vector");
        var look = new M4([side.X, direction.X, up.X, 0, side.Y, direction.Y, up.Y, 0, side.Z, direction.Z, up.Z, 0, 0, 0, 0, 1]);
        var rotated = look * M4.FromQuaternion(constraint.Need("aim_offset").Quat());
        var translation = sourceLocal.Translation;
        var desired = new M4([rotated[0, 0], rotated[0, 1], rotated[0, 2], translation.X, rotated[1, 0], rotated[1, 1], rotated[1, 2], translation.Y,
            rotated[2, 0], rotated[2, 1], rotated[2, 2], translation.Z, 0, 0, 0, 1]);
        var local = JsonKeys.Matrix(parentFrame.Need("target_to_source")) * desired * JsonKeys.Matrix(slaveFrame.Need("source_to_target"));
        var euler = NumpyMath.EulerXyz(local);
        return new()
        {
            ["source_index"] = constraint.Need("source_index").Int(), ["source_type"] = constraint.Need("source_type")?.DeepClone(), ["source_slave"] = slave.Need("bone")?.DeepClone(),
            ["bone"] = name, ["parent"] = parentName, ["control"] = parentName, ["control_parent"] = skin.Joints[parent.Parent.Value].Name,
            ["position"] = JsonKeys.Vector(local.Translation), ["quaternion"] = JsonKeys.Quaternion(NumpyMath.QuaternionFromMatrix(local)),
            ["euler_xyz_degrees"] = JsonKeys.Numbers(Fmt.Degrees(euler.X), Fmt.Degrees(euler.Y), Fmt.Degrees(euler.Z)),
            ["trigger_quaternion"] = JsonKeys.Numbers(0.0, 0.0, 0.0, 1.0), ["tolerance_degrees"] = 360.0, ["method"] = "exact_parent_local_aim_quatinterp",
            ["derivation"] = "Full-weight parent-up aim and target share the same moving parent frame"
        };
    }

    /// <summary>Requires native helpers to have a single authored writer across the complete source graph.</summary>
    private static bool ExclusiveSlave(JsonObject data, JsonObject record)
    {
        var name = record.Get("source_slave").Str();
        long token = RagdollCorrectives.StringToken(name);
        var definitions = data.Get("source_constraints").Arr();
        for (var index = 0; index < definitions.Count; index++)
        {
            if (index == record.Get("source_index").Int()) continue;
            foreach (var slave in definitions[index].Get("m_slaves").Arr())
                if ((slave.Has("m_sName") && slave.Get("m_sName").Str() == name) || (slave.Has("m_nBoneHash") && slave.Get("m_nBoneHash").Lng() == token)) return false;
        }
        return true;
    }

    /// <summary>Selects parent-local aims with fixed origins and exact singularity-free native equivalents.</summary>
    /// <param name="gltf">The source animations to check for a moving aim origin, or null when none are exported.</param>
    public static (JsonArray Selected, JsonArray Rejected) AuthoredNativeAims(JsonObject data, GltfSkin skin, GltfDocument? gltf = null)
    {
        JsonArray selected = [], rejected = [];
        foreach (var constraint in data.Get("runtime_constraints").Arr())
        {
            if (constraint.Get("kind").Str() != "aim") continue;
            var index = constraint.Need("source_index").Int();
            try
            {
                var record = ParentLocalAim(constraint, data.Get("source_frames").Obj(), skin);
                if (!ExclusiveSlave(data, record)) throw new InvalidDataException("Another authored constraint also writes this native helper");
                if (gltf is not null)
                {
                    var (constant, error) = ConstantTranslation(gltf, record.Get("source_slave").Str());
                    if (!constant) throw new InvalidDataException("Authored translation animation changes the constrained bone's aim origin");
                    record["maximum_authored_translation_key_error"] = error;
                }
                selected.Add(record);
            }
            catch (Exception error) when (error is KeyNotFoundException or InvalidDataException)
            {
                rejected.Add(new JsonObject { ["source_index"] = index, ["reason"] = error.Message });
            }
        }
        return (selected, rejected);
    }

    /// <summary>Writes exact authored VRD quaternion helpers without modifying runtime ownership.</summary>
    public static NativeProcedurals WriteNativeProcedurals(JsonObject data, GltfSkin skin, string output, GltfDocument? gltf = null)
    {
        var (selected, rejected) = AuthoredNativeAims(data, skin, gltf);
        if (selected.Count == 0) return new([], [], selected, rejected);
        var lines = new List<string>();
        foreach (var record in selected)
        {
            var names = new[] { "bone", "parent", "control_parent", "control" }.Select(key => VrdName(record.Get(key).Str()));
            lines.Add("<helper> " + string.Join(' ', names));
            lines.Add("<basepos> " + string.Join(' ', record.Get("position").Doubles().Select(Fmt.F9)));
            double[] values = [record.Get("tolerance_degrees").Dbl(), 0, 0, 0, .. record.Get("euler_xyz_degrees").Doubles(), 0, 0, 0];
            lines.Add("<trigger> " + string.Join(' ', values.Select(Fmt.F9)));
            lines.Add("");
        }
        var path = Path.Combine(output, "authored_procedurals.vrd");
        PythonText.Write(path, string.Join('\n', lines));
        return new([$"$proceduralbones \"{Path.GetFileName(path)}\""], [path], selected, rejected);
    }

    private static int Int32(byte[] data, int offset) => BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(offset));
    private static float Float(byte[] data, int offset) => BinaryPrimitives.ReadSingleLittleEndian(data.AsSpan(offset));

    /// <summary>Requires actual native quaternion procedures, exact controls, flags and one authored trigger.</summary>
    /// <param name="readCompiledModel">Reads a compiled model's bone table, each bone with <c>name</c>, <c>parent</c>,
    /// <c>flags</c>, <c>procedural_type</c> and <c>procedural_offset</c>.</param>
    public static JsonObject AuditNativeProcedurals(string model, JsonArray records, Func<string, JsonObject> readCompiledModel)
    {
        if (records.Count == 0) return new() { ["ok"] = true, ["errors"] = new JsonArray(), ["bones"] = new JsonArray() };
        var metadata = readCompiledModel(model);
        var data = File.ReadAllBytes(model);
        var boneOffset = Int32(data, 160);
        var bones = metadata.Need("bones").Arr();
        var lookup = new Dictionary<string, int>(StringComparer.Ordinal);
        for (var index = 0; index < bones.Count; index++) lookup[bones[index].Need("name").Str()] = index;
        var errors = new JsonArray();
        var audited = new JsonArray();
        foreach (var record in records)
        {
            var name = record.Need("bone").Str();
            if (!lookup.TryGetValue(name, out var index))
            {
                errors.Add($"Authored native helper is absent: {name}");
                continue;
            }
            var bone = bones[index];
            if (bone.Need("procedural_type").Int() != 2 || !bone.Need("procedural_offset").Bool() || (bone.Need("flags").Int() & 4) == 0)
            {
                errors.Add($"Authored native quaternion procedure was lost: {name}");
                continue;
            }
            var parent = bone.Need("parent").Int();
            if (parent < 0 || parent >= bones.Count || bones[parent].Need("name").Str() != record.Need("parent").Str()) errors.Add($"Native helper parent differs: {name}");
            var offset = boneOffset + index * 216 + bone.Need("procedural_offset").Int();
            if (offset < 0 || offset > data.Length - 12)
            {
                errors.Add($"Native helper procedure is truncated: {name}");
                continue;
            }
            int control = Int32(data, offset), count = Int32(data, offset + 4), triggerOffset = Int32(data, offset + 8);
            if (control < 0 || control >= bones.Count || bones[control].Need("name").Str() != record.Need("control").Str()) errors.Add($"Native helper control differs: {name}");
            else
            {
                var controlParent = bones[control].Need("parent").Int();
                if (controlParent < 0 || bones[controlParent].Need("name").Str() != record.Need("control_parent").Str()) errors.Add($"Native helper control-parent frame differs: {name}");
            }
            if (count != 1)
            {
                errors.Add($"Native helper has {count} triggers instead of one: {name}");
                continue;
            }
            var at = offset + triggerOffset;
            if (at < 0 || at > data.Length - 48)
            {
                errors.Add($"Native helper trigger is truncated: {name}");
                continue;
            }
            var values = Enumerable.Range(0, 12).Select(item => (double)Float(data, at + item * 4)).ToArray();
            var inverseTolerance = values[0];
            var trigger = new Q4(values[1], values[2], values[3], values[4]);
            var position = new V3(values[5], values[6], values[7]);
            var quaternion = new Q4(values[8], values[9], values[10], values[11]);
            if (!values.All(double.IsFinite) || Math.Abs(trigger.Length - 1) > 1e-5 || Math.Abs(quaternion.Length - 1) > 1e-5)
            {
                errors.Add($"Native helper trigger has invalid numeric values: {name}");
                continue;
            }
            var expected = record.Need("position").Vec3();
            var positionError = Math.Max(Math.Abs(position.X - expected.X), Math.Max(Math.Abs(position.Y - expected.Y), Math.Abs(position.Z - expected.Z)));
            var rotationError = Largest(M4.FromQuaternion(quaternion), M4.FromQuaternion(record.Need("quaternion").Quat()));
            var triggerError = Largest(M4.FromQuaternion(trigger), M4.Identity);
            var toleranceError = Math.Abs(inverseTolerance - 1 / Fmt.Radians(record.Need("tolerance_degrees").Dbl()));
            if (Math.Max(Math.Max(positionError, rotationError), Math.Max(triggerError, toleranceError)) > 1e-5) errors.Add($"Native helper encoded trigger differs: {name}");
            audited.Add(new JsonObject
            {
                ["bone"] = name, ["source_index"] = record.Need("source_index")?.DeepClone(), ["procedural_type"] = bone.Need("procedural_type")?.DeepClone(), ["control"] = control,
                ["triggers"] = count, ["maximum_position_component_error"] = positionError, ["maximum_rotation_component_error"] = rotationError,
                ["maximum_trigger_component_error"] = triggerError, ["inverse_tolerance_error"] = toleranceError
            });
        }
        return new() { ["ok"] = errors.Count == 0, ["errors"] = errors, ["bones"] = audited };
    }

    private static double Largest(M4 first, M4 second)
    {
        var maximum = 0.0;
        for (var row = 0; row < 3; row++)
            for (var column = 0; column < 3; column++)
                maximum = Math.Max(maximum, Math.Abs(first[row, column] - second[row, column]));
        return maximum;
    }
}
