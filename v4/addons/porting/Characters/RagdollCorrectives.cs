using System.Text;
using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>An authored procedural input cannot be represented or resolved precisely.</summary>
public sealed class CorrectiveException(string message) : Exception(message);

/// <summary>Resolves authored procedural constraints and precomputes Source 2 pose-space interpolation.</summary>
public static class RagdollCorrectives
{
    private const uint Multiplier = 0x5BD1E995;

    /// <summary>Computes Source 2's lowercase MurmurHash2 resource and bone-name token.</summary>
    public static uint StringToken(string value)
    {
        var data = Encoding.UTF8.GetBytes(value.ToLowerInvariant());
        var result = 0x31415926u ^ (uint)data.Length;
        var position = 0;
        unchecked
        {
            while (position + 4 <= data.Length)
            {
                var number = BitConverter.ToUInt32(data, position) * Multiplier;
                number ^= number >> 24;
                number *= Multiplier;
                result = (result * Multiplier) ^ number;
                position += 4;
            }
            if (position < data.Length)
            {
                var tail = 0u;
                for (var index = data.Length - 1; index >= position; index--) tail = (tail << 8) | data[index];
                result ^= tail;
                result *= Multiplier;
            }
            result ^= result >> 13;
            result *= Multiplier;
            return result ^ (result >> 15);
        }
    }

    /// <summary>Evaluates the kernels used by VRF's Source 2 pose-space constraint solver.</summary>
    public static double RbfKernel(double radius, int kernel, double falloff)
    {
        var scaled = falloff > 0 ? radius / falloff : radius;
        return kernel switch
        {
            1 => 1.0 / Math.Sqrt(1.0 + scaled * scaled),
            2 => Math.Exp(-scaled * scaled),
            3 => radius,
            4 => Math.Pow(radius, 3),
            5 => Math.Pow(radius, 5),
            6 => radius * radius * Math.Log(radius),
            _ => Math.Sqrt(1.0 + scaled * scaled)
        };
    }

    /// <summary>Solves the full interpolation matrix without normalizing or clamping basis weights.</summary>
    /// <remarks>The inverse and sample table go into the driver, whose evaluator applies them with no further solve.</remarks>
    public static JsonObject BuildRadialBasis(IReadOnlyList<double[]> samples, int kernel = 0, double falloff = 1.0)
    {
        if (samples.Count == 0 || samples.Any(sample => sample.Length != 3 || !sample.All(double.IsFinite)))
            throw new CorrectiveException("RBF samples must be finite three-dimensional positions");
        if (samples.Select(sample => (sample[0], sample[1], sample[2])).Distinct().Count() != samples.Count)
            throw new CorrectiveException("Duplicate RBF samples disable the source constraint");
        if (!double.IsFinite(falloff)) throw new CorrectiveException("Source RBF falloff is not finite");
        kernel = kernel is >= 0 and <= 6 ? kernel : 0;
        var count = samples.Count;
        var matrix = new double[count, count];
        for (var row = 0; row < count; row++)
            for (var column = 0; column < count; column++)
            {
                double x = samples[row][0] - samples[column][0], y = samples[row][1] - samples[column][1], z = samples[row][2] - samples[column][2];
                matrix[row, column] = RbfKernel(Math.Sqrt(x * x + y * y + z * z), kernel, falloff);
                if (!double.IsFinite(matrix[row, column])) throw new CorrectiveException("Source RBF kernel is undefined at zero sample distance");
            }
        double[,] inverse;
        try
        {
            inverse = M4.Invert(matrix);
        }
        catch (InvalidDataException)
        {
            throw new CorrectiveException("Singular source RBF interpolation matrix");
        }
        var rows = new JsonArray();
        for (var row = 0; row < count; row++) rows.Add(JsonKeys.Numbers(Enumerable.Range(0, count).Select(column => inverse[row, column]).ToArray()));
        return new()
        {
            ["samples"] = new JsonArray(samples.Select(sample => (JsonNode?)JsonKeys.Numbers(sample)).ToArray()), ["inverse"] = rows, ["kernel"] = kernel,
            ["falloff"] = falloff, ["condition_number"] = NumpyMath.SymmetricCondition(matrix)
        };
    }

    /// <summary>Evaluates interpolating sample weights for local numeric verification and animation baking.</summary>
    public static double[] EvaluateRadialBasis(JsonNode? basis, V3 point)
    {
        var samples = basis.Need("samples").Arr();
        var kernels = new double[samples.Count];
        for (var index = 0; index < kernels.Length; index++)
            kernels[index] = RbfKernel((samples[index].Vec3() - point).Length, basis.Need("kernel").Int(), basis.Need("falloff").Dbl());
        var inverse = basis.Need("inverse").Arr();
        var weights = new double[inverse.Count];
        for (var row = 0; row < weights.Length; row++)
        {
            var coefficients = inverse[row].Doubles();
            for (var column = 0; column < kernels.Length; column++) weights[row] += coefficients[column] * kernels[column];
        }
        return weights;
    }

    /// <summary>Evaluates the authored output table, retaining unclamped extrapolation when requested.</summary>
    public static double[] EvaluateMorph(JsonNode? driver, V3 point)
    {
        var weights = EvaluateRadialBasis(driver.Need("basis"), point);
        var outputs = driver.Need("output_weights").Arr().Select(row => row.Doubles()).ToList();
        var result = new double[outputs[0].Length];
        for (var output = 0; output < result.Length; output++)
        {
            for (var sample = 0; sample < weights.Length; sample++) result[output] += weights[sample] * outputs[sample][output];
            if (driver.Need("clamp").Bool()) result[output] = Math.Clamp(result[output], 0.0, 1.0);
        }
        return result;
    }

    /// <summary>Evaluates raw positional weights and the source log-space quaternion average.</summary>
    public static List<M4> EvaluatePoseBone(JsonNode? driver, V3 point)
    {
        var weights = EvaluateRadialBasis(driver.Need("basis"), point);
        var outputs = driver.Need("output_transforms").Arr();
        var total = weights.Sum();
        var inverse = total > 0 ? 1.0 / total : 1.0;
        var result = new List<M4>();
        for (var slave = 0; slave < outputs[0].Arr().Count; slave++)
        {
            var values = outputs.Select(sample => sample.Arr()[slave].Doubles()).ToList();
            var position = V3.Zero;
            for (var sample = 0; sample < weights.Length; sample++) position += new V3(values[sample][0], values[sample][1], values[sample][2]) * weights[sample];
            var first = new Q4(values[0][3], values[0][4], values[0][5], values[0][6]);
            double lx = 0, ly = 0, lz = 0, lw = 0;
            for (var sample = 0; sample < weights.Length; sample++)
            {
                var quaternion = new Q4(values[sample][3], values[sample][4], values[sample][5], values[sample][6]);
                if (quaternion.Dot(first) < 0) quaternion = -quaternion;
                var length = new V3(quaternion.X, quaternion.Y, quaternion.Z).Length;
                var scale = length >= 1e-5 ? Math.Atan2(length, quaternion.W) / length : 0.0;
                lx += quaternion.X * scale * weights[sample] * inverse;
                ly += quaternion.Y * scale * weights[sample] * inverse;
                lz += quaternion.Z * scale * weights[sample] * inverse;
                lw += 0.5 * Math.Log(quaternion.Dot(quaternion)) * weights[sample] * inverse;
            }
            var angle = new V3(lx, ly, lz).Length;
            var magnitude = Math.Exp(lw);
            var factor = angle >= 1e-5 ? Math.Sin(angle) * magnitude / angle : 0.0;
            result.Add(M4.Trs(position, new(lx * factor, ly * factor, lz * factor, Math.Cos(angle) * magnitude)));
        }
        return result;
    }

    /// <summary>Measures the reference attachment point in the original source bone parent's frame.</summary>
    public static V3 SourcePosePoint(M4 boneWorld, M4 parentWorld, V3 offset) => (parentWorld.Inverse() * boneWorld).Point(offset);

    /// <summary>Resolves explicit names before hashes, matching compiled Source 2 constraint precedence.</summary>
    private static string ReferenceName(JsonNode? value, IReadOnlyDictionary<long, string> names)
    {
        var name = value.Get("m_sName").Bool() ? value.Get("m_sName").Str() : "";
        if (name.Length > 0) return name;
        return names.GetValueOrDefault(value.Has("m_nBoneHash") ? value.Get("m_nBoneHash").Lng() : -1, "");
    }

    /// <summary>Accepts named attachment maps or the portable model metadata attachment list.</summary>
    private static JsonObject AttachmentMap(JsonNode? value)
    {
        if (value is JsonObject map) return map;
        var result = new JsonObject();
        foreach (var item in value.Arr()) result[item.Need("name").Str()] = item?.DeepClone();
        return result;
    }

    /// <summary>A list field copied as authored, or the default when it is absent or empty.</summary>
    private static JsonNode Listed(JsonNode? value, params int[] fallback) => value.Bool() ? value!.DeepClone() : JsonKeys.Integers(fallback);

    /// <summary>A field read with a default that was itself a whole number, so it prints as one.</summary>
    private static JsonNode OneOf(JsonNode? value, JsonNode? other, params int[] fallback) =>
        value?.DeepClone() ?? other?.DeepClone() ?? JsonKeys.Integers(fallback);

    /// <summary>Resolves one local bone or single-influence attachment target and its authored offset.</summary>
    private static JsonObject Target(JsonNode? value, JsonObject frames, JsonObject attachments, IReadOnlyDictionary<long, string> bonesByHash, IReadOnlyDictionary<long, string> attachmentsByHash)
    {
        string name, bone;
        JsonNode position, rotation;
        var isAttachment = value.Get("m_bIsAttachment").Bool();
        if (isAttachment)
        {
            name = ReferenceName(value, attachmentsByHash);
            if (attachments.Get(name) is not { } attachment)
                throw new CorrectiveException($"Missing attachment dependency {(name.Length > 0 ? name : JsonKeys.Shown(value.Get("m_nBoneHash")))}");
            if ((attachment.Has("influences") ? attachment.Get("influences").Int() : 1) != 1)
                throw new CorrectiveException($"Weighted attachment dependency {name} needs multi-influence evaluation");
            if (attachment.Get("ignore_rotation").Bool()) throw new CorrectiveException($"Attachment dependency {name} ignores bone rotation and requires a separate evaluator");
            bone = attachment.Get("bone").Bool() ? attachment.Get("bone").Str() : "";
            if (!frames.ContainsKey(bone)) throw new CorrectiveException($"Attachment {name} depends on absent or model-root bone {PythonText.Repr(bone)}");
            position = OneOf(attachment.Get("position"), attachment.Get("offset"), 0, 0, 0);
            rotation = OneOf(attachment.Get("rotation"), null, 0, 0, 0, 1);
        }
        else
        {
            name = ReferenceName(value, bonesByHash);
            bone = name;
            if (!frames.ContainsKey(bone)) throw new CorrectiveException($"Missing source bone dependency {(name.Length > 0 ? name : JsonKeys.Shown(value.Get("m_nBoneHash")))}");
            position = JsonKeys.Integers([0, 0, 0]);
            rotation = JsonKeys.Integers([0, 0, 0, 1]);
        }
        return new()
        {
            ["bone"] = bone, ["attachment"] = isAttachment ? name : "", ["attachment_position"] = position, ["attachment_rotation"] = rotation,
            ["position_offset"] = Listed(value.Get("m_vOffset"), 0, 0, 0), ["rotation_offset"] = Listed(value.Get("m_qOffset"), 0, 0, 0, 1),
            ["weight"] = value.Has("m_flWeight") ? value.Get("m_flWeight").Dbl() : 1.0
        };
    }

    /// <summary>Resolves constrained bones against original source names without changing the target rig.</summary>
    private static JsonArray Slaves(JsonNode? definition, JsonObject frames, IReadOnlyDictionary<long, string> names)
    {
        var output = new JsonArray();
        foreach (var value in definition.Get("m_slaves").Arr())
        {
            var name = ReferenceName(value, names);
            if (!frames.ContainsKey(name)) throw new CorrectiveException($"Missing constrained bone {(name.Length > 0 ? name : JsonKeys.Shown(value.Get("m_nBoneHash")))}");
            output.Add(new JsonObject
            {
                ["bone"] = name, ["parent"] = frames.Get(name).Has("parent") ? frames.Get(name).Get("parent")?.DeepClone() : "",
                ["weight"] = value.Has("m_flWeight") ? value.Get("m_flWeight").Dbl() : 1.0
            });
        }
        return output;
    }

    /// <summary>Resolves the source-local measuring bone and attachment offset for an RBF input.</summary>
    private static void PoseInput(JsonObject record, string bone, JsonNode? attachment, JsonObject frames)
    {
        if (!frames.ContainsKey(bone)) throw new CorrectiveException($"Missing pose-space measuring bone {bone}");
        var parent = frames.Get(bone).Get("parent").Str();
        if (parent.Length == 0 || !frames.ContainsKey(parent)) throw new CorrectiveException($"Pose-space measuring bone {bone} lacks its original source parent");
        if ((attachment.Has("influences") ? attachment.Get("influences").Int() : 1) != 1) throw new CorrectiveException("Pose-space attachment must have one authored influence");
        record["bone"] = bone;
        record["parent"] = parent;
        record["offset"] = OneOf(attachment.Get("position"), attachment.Get("offset"), 0, 0, 0);
    }

    private static JsonObject Basis(JsonNode? definition, JsonArray inputs) => BuildRadialBasis(
        inputs.Select(item => item.Need("m_inputValue").Doubles()).ToList(),
        definition.Has("m_eRbfType") ? definition.Get("m_eRbfType").Int() : 0,
        definition.Has("m_flFalloff") ? definition.Get("m_flFalloff").Dbl() : 1.0);

    /// <summary>Compiles resolvable procedural records into a source-ordered self-contained runtime graph.</summary>
    /// <remarks>An unresolvable constraint is recorded with its reason rather than failing the port, and a constraint with
    /// no slaves or targets is inert. A constraint owned by a native procedural bone or a tilt-twist driver is not compiled
    /// again, so every source index ends up in exactly one list.</remarks>
    public static JsonObject PrepareCorrectiveDrivers(JsonObject data)
    {
        var frames = data.Get("source_frames").Obj();
        var attachments = AttachmentMap(data.Get("source_attachments"));
        var bonesByHash = new Dictionary<long, string>();
        foreach (var (name, _) in frames) bonesByHash[StringToken(name)] = name;
        var attachmentsByHash = new Dictionary<long, string>();
        foreach (var (name, _) in attachments) attachmentsByHash[StringToken(name)] = name;
        var available = data.Get("available_flexes").Arr().Select(item => item.Str()).ToHashSet(StringComparer.Ordinal);
        var twists = new Dictionary<int, JsonNode>();
        foreach (var item in data.Get("twist_constraints").Arr()) twists[item.Need("source_index").Int()] = item!;
        var native = new Dictionary<int, JsonNode>();
        foreach (var item in data.Get("native_procedurals").Arr()) native[item.Need("source_index").Int()] = item!;
        var definitions = data.Get("source_constraints").Arr();
        if (native.Count != data.Get("native_procedurals").Arr().Count) throw new CorrectiveException("Native procedural ownership contains duplicate source indices");
        foreach (var (index, record) in native)
            if (index < 0 || index >= definitions.Count || definitions[index].Get("_class").Str() != record.Get("source_type").Str())
                throw new CorrectiveException("Native procedural ownership does not match its preserved source constraint");
        JsonArray constraints = [], unsupported = [], inert = [];
        for (var index = 0; index < definitions.Count; index++)
        {
            var definition = definitions[index];
            var kind = definition.Get("_class").Str();
            try
            {
                if (native.ContainsKey(index)) continue;
                if (twists.TryGetValue(index, out var twist))
                {
                    var copied = twist.DeepClone().AsObject();
                    copied["kind"] = "twist";
                    constraints.Add(copied);
                    continue;
                }
                if (kind == "CTiltTwistConstraint") throw new CorrectiveException("Tilt/twist source references did not resolve against the target rig");
                var record = new JsonObject { ["source_index"] = index, ["source_type"] = kind };
                if (kind == "CBoneConstraintPoseSpaceMorph")
                {
                    var name = definition.Get("m_sAttachmentName").Str();
                    if (attachments.Get(name) is not { } attachment) throw new CorrectiveException($"Missing pose-space reference attachment {name}");
                    PoseInput(record, definition.Get("m_sBoneName").Str(), attachment, frames);
                    var outputs = definition.Get("m_outputMorph").Arr().Select(item => item.Str()).ToList();
                    if (available.Count > 0 && outputs.Exists(output => !available.Contains(output)))
                        throw new CorrectiveException("Pose-space output flex controller is absent from the compiled expression rig");
                    var inputs = definition.Get("m_inputList").Arr();
                    var weights = new JsonArray();
                    foreach (var item in inputs)
                    {
                        var list = item.Get("m_outputWeightList").Doubles();
                        weights.Add(JsonKeys.Numbers(Enumerable.Range(0, outputs.Count).Select(slot => slot < list.Length ? list[slot] : 0.0).ToArray()));
                    }
                    record["kind"] = "pose_morph";
                    record["outputs"] = JsonKeys.Strings(outputs);
                    record["output_weights"] = weights;
                    record["clamp"] = definition.Get("m_bClamp").Bool();
                    record["basis"] = Basis(definition, inputs);
                }
                else if (kind == "CBoneConstraintPoseSpaceBone")
                {
                    var targets = definition.Get("m_targets").Arr();
                    if (targets.Count != 1 || !targets[0].Get("m_bIsAttachment").Bool()) throw new CorrectiveException("Pose-space bone constraint requires one local attachment target");
                    var target = Target(targets[0], frames, attachments, bonesByHash, attachmentsByHash);
                    var inputs = definition.Get("m_inputList").Arr();
                    var slaves = Slaves(definition, frames, bonesByHash);
                    if (slaves.Count == 0)
                    {
                        inert.Add(record);
                        continue;
                    }
                    if (inputs.Any(item => item.Get("m_outputTransformList").Arr().Count < slaves.Count)) throw new CorrectiveException("Pose-space output transform table is incomplete");
                    if (inputs.Any(item => item.Need("m_outputTransformList").Arr().Any(transform => transform.Arr().Count != 7)))
                        throw new CorrectiveException("Pose-space output transform needs a position and XYZW quaternion");
                    var rotation = M4.FromQuaternion(target.Get("attachment_rotation").Quat());
                    var offset = target.Get("attachment_position").Vec3() + rotation.Direction(target.Get("position_offset").Vec3());
                    PoseInput(record, target.Get("bone").Str(), new JsonObject { ["position"] = JsonKeys.Vector(offset) }, frames);
                    record["kind"] = "pose_bone";
                    record["slaves"] = slaves;
                    record["output_transforms"] = new JsonArray(inputs.Select(item => item.Need("m_outputTransformList")?.DeepClone()).ToArray());
                    record["basis"] = Basis(definition, inputs);
                }
                else if (kind is "CPointConstraint" or "COrientConstraint" or "CParentConstraint" or "CAimConstraint")
                {
                    var slaves = Slaves(definition, frames, bonesByHash);
                    if (slaves.Count == 0)
                    {
                        inert.Add(record);
                        continue;
                    }
                    var targets = definition.Get("m_targets").Arr().Select(value => (JsonNode?)Target(value, frames, attachments, bonesByHash, attachmentsByHash)).ToArray();
                    if (targets.Length == 0)
                    {
                        inert.Add(record);
                        continue;
                    }
                    record["kind"] = kind switch { "CPointConstraint" => "point", "COrientConstraint" => "orient", "CParentConstraint" => "parent", _ => "aim" };
                    record["slaves"] = slaves;
                    record["targets"] = new JsonArray(targets);
                    record["up_vector"] = Listed(definition.Get("m_vUpVector"), 0, 0, 1);
                    record["up_type"] = definition.Has("m_nUpType") ? definition.Get("m_nUpType").Int() : 1;
                    record["aim_offset"] = Listed(definition.Get("m_qAimOffset"), 0, 0, 0, 1);
                }
                else throw new CorrectiveException($"Procedural type {kind} has no implemented evaluator");
                constraints.Add(record);
            }
            catch (CorrectiveException error)
            {
                unsupported.Add(new JsonObject { ["source_index"] = index, ["type"] = kind, ["reason"] = error.Message });
            }
        }
        return new()
        {
            ["runtime_constraints"] = constraints, ["unsupported_runtime_constraints"] = unsupported, ["inert_runtime_constraints"] = inert,
            ["runtime_constraint_summary"] = new JsonObject
            {
                ["resolved"] = constraints.Count, ["inert"] = inert.Count, ["native"] = native.Count, ["unsupported"] = unsupported.Count,
                ["pose_morphs"] = constraints.Count(item => item.Get("kind").Str() == "pose_morph"), ["pose_bones"] = constraints.Count(item => item.Get("kind").Str() == "pose_bone")
            }
        };
    }
}
