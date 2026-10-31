using System.Numerics;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>One numbered choice of an authored bodygroup, tied to its bit in the source mesh-group mask.</summary>
public sealed class BodygroupChoice(long value, int bit)
{
    public long Value { get; } = value;
    public int Bit { get; } = bit;
    public List<int> Meshes { get; } = [];
}

/// <summary>One authored bodygroup with the choice the source shows by default.</summary>
public sealed record AuthoredBodygroup(string Name, long Default, List<BodygroupChoice> Choices);

public static partial class RagdollAppearance
{
    [GeneratedRegex(@"\A(.+)_@(\d+)\z")]
    private static partial Regex NumberedGroup();

    [GeneratedRegex(@"\A[A-Za-z_][A-Za-z0-9_]*\z")]
    private static partial Regex ControllerIdentifier();

    /// <summary>Tests a mask bit with Python's unbounded two's complement integers.</summary>
    private static bool HasBit(long mask, int bit) => bit >= 64 ? mask < 0 : (mask & (1L << bit)) != 0;

    /// <summary>Partitions source mesh masks into automatic geometry and numbered choices.</summary>
    /// <remarks>A group named <c>slot_@2</c> is choice 2 of bodygroup <c>slot</c>, and <c>autodefault</c> marks geometry that is
    /// always on. Choices must be contiguous and have exactly one default, because Source 1 encodes the body index as a
    /// mixed radix of each group's choice count.</remarks>
    public static (List<int> Automatic, List<AuthoredBodygroup> Bodygroups) AuthoredBodygroups(JsonObject fidelity)
    {
        var groups = new OrderedDictionary<string, OrderedDictionary<long, int>>(StringComparer.Ordinal);
        var automaticMask = 0L;
        var groupNames = fidelity.Need("groups").Arr();
        for (var bit = 0; bit < groupNames.Count; bit++)
        {
            var name = groupNames[bit].Str();
            var match = NumberedGroup().Match(name);
            if (match.Success)
            {
                var slot = match.Groups[1].Value;
                if (!groups.TryGetValue(slot, out var choices)) groups[slot] = choices = [];
                choices[long.Parse(match.Groups[2].ValueSpan)] = bit;
            }
            else if (name.Equals("autodefault", StringComparison.OrdinalIgnoreCase)) automaticMask |= bit < 64 ? 1L << bit : 0;
            else groups[name] = new() { [0] = bit };
        }
        var defaultMask = fidelity.Need("default_mask").Lng();
        var bodygroups = new List<AuthoredBodygroup>();
        foreach (var (slot, choices) in groups)
        {
            var ordered = choices.Keys.Order().ToList();
            if (!ordered.SequenceEqual(Enumerable.Range(0, (int)ordered[^1] + 1).Select(value => (long)value))) throw new InvalidDataException($"Authored bodygroup {slot} has non-contiguous choices");
            var selected = choices.Where(pair => HasBit(defaultMask, pair.Value)).Select(pair => pair.Key).ToList();
            if (selected.Count != 1) throw new InvalidDataException($"Authored bodygroup {slot} has no unique default");
            bodygroups.Add(new(slot, selected[0], ordered.ConvertAll(choice => new BodygroupChoice(choice, choices[choice]))));
        }
        var automatic = new List<int>();
        foreach (var mesh in fidelity.Need("meshes").Arr())
        {
            var maskNode = mesh.Need("group_mask");
            var mask = maskNode.Lng();
            var unmasked = maskNode is JsonValue value && value.TryGetValue<long>(out var signed) && signed == -1;
            if (groupNames.Count == 0 || unmasked || (mask & automaticMask) != 0)
            {
                automatic.Add(mesh.Need("index").Int());
                continue;
            }
            var memberships = new List<string>();
            foreach (var group in bodygroups)
                foreach (var choice in group.Choices)
                {
                    if (!HasBit(mask, choice.Bit)) continue;
                    choice.Meshes.Add(mesh.Need("index").Int());
                    memberships.Add(group.Name);
                }
            if (memberships.Distinct(StringComparer.Ordinal).Count() > 1) throw new InvalidDataException($"Mesh {mesh.Need("name").Str()} has non-separable membership across bodygroups");
            if (memberships.Count == 0) throw new InvalidDataException($"Mesh {mesh.Need("name").Str()} has no authored bodygroup membership");
        }
        var combinations = bodygroups.Aggregate(BigInteger.One, (product, group) => product * group.Choices.Count);
        if (bodygroups.Count + (automatic.Count > 0 ? 1 : 0) >= 32 || combinations >= BigInteger.Pow(2, 31)) throw new InvalidDataException("Authored bodygroup layout exceeds Source 1's bodypart encoding limits");
        return (automatic, bodygroups);
    }

    /// <summary>Applies the native Source 2 Viewer coordinate conversion to one position.</summary>
    /// <remarks>numpy's matrix product sums from zero, so a negative zero does not survive the axis swap.</remarks>
    public static V3 GeometryPosition(V3 value, CoordinateMode coord) => GeometryDirection(value * coord.Scale, coord);

    /// <summary>Applies the native Source 2 Viewer axis conversion to one direction.</summary>
    public static V3 GeometryDirection(V3 value, CoordinateMode coord) => coord.SwapAxes ? new(value.Z + 0.0, value.X + 0.0, value.Y + 0.0) : value;

    /// <summary>Refuses a mesh transform a morph delta cannot follow.</summary>
    internal static void RequireUniformScale(M4 transform)
    {
        double[] scales = [Column(transform, 0).Length, Column(transform, 1).Length, Column(transform, 2).Length];
        if (!scales.All(scale => Math.Abs(scale - scales[0]) <= 1e-8 + 1e-5 * Math.Abs(scales[0])) || scales[0] <= 0)
            throw new InvalidDataException("Flex conversion requires a rigid or uniformly scaled mesh transform");
    }

    private static V3 Column(M4 matrix, int column) => new(matrix[0, column], matrix[1, column], matrix[2, column]);

    /// <summary>A morph position delta in compiled model space.</summary>
    internal static V3 PositionDelta(V3 value, CoordinateMode coord, M4? transform) => transform is null ? GeometryPosition(value, coord) : transform.Direction(value);

    /// <summary>A morph normal delta in compiled model space, with a uniform scale removed.</summary>
    internal static V3 NormalDelta(V3 value, CoordinateMode coord, M4? transform) =>
        transform is null ? GeometryDirection(value, coord) : transform.Direction(value) / Column(transform, 0).Length;

    private static double LargestDifference(MorphDelta first, MorphDelta second)
    {
        var maximum = 0.0;
        void Note(V3 difference) => maximum = Math.Max(maximum, Math.Max(Math.Abs(difference.X), Math.Max(Math.Abs(difference.Y), Math.Abs(difference.Z))));
        int a = 0, b = 0;
        while (a < first.Indices.Length || b < second.Indices.Length)
        {
            var left = a < first.Indices.Length ? first.Indices[a] : int.MaxValue;
            var right = b < second.Indices.Length ? second.Indices[b] : int.MaxValue;
            if (left == right) Note(first.Values[a++] - second.Values[b++]);
            else if (left < right) Note(first.Values[a++]);
            else Note(second.Values[b++]);
        }
        return maximum;
    }

    /// <summary>Restores omitted base normal deltas only after atlas positions independently match exported morphs.</summary>
    /// <remarks>VRF's glTF leaves out morph normals. They exist in the retained atlas, and the positions the glTF does carry
    /// prove the atlas lines up with these vertices before anything is taken from it.</remarks>
    private static JsonArray RestoreBaseMorphChannels(JsonObject fidelity, IReadOnlyDictionary<int, List<AppearancePrimitive>> lookup, CoordinateMode coord)
    {
        var reports = new JsonArray();
        foreach (var mesh in fidelity.Need("meshes").Arr())
        {
            if (!lookup.TryGetValue(mesh.Need("index").Int(), out var primitives)) continue;
            if (MorphAtlas.LoadMeshMorphs(fidelity, mesh) is not { } atlas) continue;
            var (targets, mdat, path) = atlas;
            var streams = new OrderedDictionary<string, List<AppearancePrimitive>>(StringComparer.Ordinal);
            foreach (var primitive in primitives)
            {
                if (!streams.TryGetValue(primitive.WeightStream, out var list)) streams[primitive.WeightStream] = list = [];
                list.Add(primitive);
            }
            var entries = MorphAtlas.MorphBufferLayout(mdat);
            if (streams.Count != entries.Count) throw new InvalidDataException("Base glTF morph buffers differ from original MDAT draw calls");
            var maximumError = 0.0;
            var recovered = new SortedSet<string>(StringComparer.Ordinal);
            for (var slot = 0; slot < entries.Count; slot++)
            {
                var stream = streams.GetAt(slot).Value;
                var entry = entries[slot];
                var (count, offset) = (stream[0].Vertices.Length, entry.Offset);
                if (count != entry.Extent || !stream.Select(primitive => primitive.Material).Order(StringComparer.Ordinal).SequenceEqual(entry.Materials.Order(StringComparer.Ordinal)))
                    throw new InvalidDataException("Base glTF morph vertex extent or material membership differs from MDAT");
                foreach (var primitive in stream)
                    foreach (var (name, channels) in targets)
                    {
                        if ((channels.Position is { } grid && offset + count > grid.Length) || (channels.Normal is { } other && offset + count > other.Length))
                            throw new InvalidDataException("Base glTF morph vertices exceed their authored atlas grid");
                        primitive.Targets.TryGetValue(name, out var existing);
                        foreach (var channel in new[] { MorphTarget.PositionChannel, MorphTarget.NormalChannel })
                        {
                            if (channels[channel] is not { } values) continue;
                            var decoded = values.Slice(offset, count);
                            var direction = channel == MorphTarget.NormalChannel;
                            if (existing?[channel] is { } present)
                            {
                                var actual = present.Map(value => direction ? GeometryDirection(value, coord) : GeometryPosition(value, coord));
                                var error = LargestDifference(actual, decoded);
                                maximumError = Math.Max(maximumError, error);
                                if (error > 1e-5) throw new InvalidDataException($"Base morph atlas does not match glTF {name} {channel}: {PhysicsMaths.General(error, 9)}");
                                continue;
                            }
                            var restored = coord.SwapAxes ? decoded.Map(Unswap) : decoded;
                            if (!direction) restored = restored.Map(value => value / coord.Scale);
                            if (!primitive.Targets.TryGetValue(name, out var target)) primitive.Targets[name] = target = new();
                            target[channel] = restored;
                            recovered.Add(channel);
                        }
                    }
            }
            reports.Add(new JsonObject
            {
                ["mesh"] = mesh.Need("index")?.DeepClone(), ["atlas"] = path, ["target_names"] = JsonKeys.Strings(targets.Keys), ["recovered_channels"] = JsonKeys.Strings(recovered),
                ["position_comparison_maximum"] = maximumError, ["buffer_slices"] = new JsonArray(entries.Select(entry => (JsonNode?)JsonKeys.Integers([entry.Offset, entry.Extent])).ToArray())
            });
        }
        return reports;
    }

    /// <summary>Keeps the strongest three merged influences, with authored slot order breaking ties.</summary>
    public static List<(int Joint, double Weight)> Source1SkinLinks(IEnumerable<(int Joint, double Weight)> links)
    {
        var merged = new List<(int Joint, double Weight)>(8);
        foreach (var (joint, weight) in links)
        {
            if (!double.IsFinite(weight) || weight < 0) throw new InvalidDataException("A source vertex contains an invalid bone weight");
            if (weight == 0) continue;
            var at = merged.FindIndex(item => item.Joint == joint);
            if (at >= 0) merged[at] = (joint, merged[at].Weight + weight);
            else merged.Add((joint, weight));
        }
        var selected = merged.OrderBy(item => -item.Weight).Take(3).ToList();
        var total = 0.0;
        foreach (var (_, weight) in selected) total += weight;
        if (selected.Count == 0 || total <= 0) throw new InvalidDataException("A bodygroup mesh has an unweighted vertex");
        return selected.ConvertAll(item => (item.Joint, item.Weight / total));
    }

    /// <summary>Measures what the Source 1 influence limit costs, across every authored vertex stream.</summary>
    internal sealed class SkinWeightAudit
    {
        private sealed class Source
        {
            public int VertexCount, AffectedVertices;
            public double MaxDiscarded, DiscardedSum, MeanDiscarded, MaxNormalization;
        }

        private readonly OrderedDictionary<string, Source> sources = new(StringComparer.Ordinal);
        private int vertexCount, affected, sourceMaxInfluences;
        private double discardedSum, maxDiscarded, maxNormalization, normalizationSum, sumDeviation;

        public void Measure(bool fromDmx, IReadOnlyList<(int Joint, double Weight)> merged, double total, IReadOnlyList<(int Joint, double Weight)> reduced)
        {
            var kind = fromDmx ? "authored_lod_dmx" : "base_glb";
            if (!sources.TryGetValue(kind, out var source)) sources[kind] = source = new();
            source.VertexCount++;
            vertexCount++;
            sourceMaxInfluences = Math.Max(sourceMaxInfluences, merged.Count);
            sumDeviation = Math.Max(sumDeviation, Math.Abs(total - 1));
            if (merged.Count <= 3) return;
            double Merged(int joint) => merged.First(item => item.Joint == joint).Weight;
            var kept = 0.0;
            foreach (var (joint, _) in reduced) kept += Merged(joint);
            var discarded = Math.Max(0.0, 1.0 - kept / total);
            var normalization = reduced.Max(item => Math.Abs(item.Weight - Merged(item.Joint) / total));
            affected++;
            source.AffectedVertices++;
            source.DiscardedSum += discarded;
            source.MaxDiscarded = Math.Max(source.MaxDiscarded, discarded);
            source.MaxNormalization = Math.Max(source.MaxNormalization, normalization);
            source.MeanDiscarded = source.DiscardedSum / source.AffectedVertices;
            discardedSum += discarded;
            maxDiscarded = Math.Max(maxDiscarded, discarded);
            normalizationSum += normalization;
            maxNormalization = Math.Max(maxNormalization, normalization);
        }

        public int Affected => affected;
        public double MaxDiscarded => maxDiscarded;
        public double MeanDiscarded => affected > 0 ? discardedSum / affected : 0.0;

        public JsonObject ToJson()
        {
            var bySource = new JsonObject();
            foreach (var (kind, source) in sources)
                bySource[kind] = new JsonObject
                {
                    ["vertex_count"] = source.VertexCount, ["affected_vertices"] = source.AffectedVertices, ["max_discarded_weight"] = source.MaxDiscarded,
                    ["discarded_weight_sum"] = source.DiscardedSum, ["mean_discarded_weight"] = source.MeanDiscarded, ["max_normalization_difference"] = source.MaxNormalization
                };
            var report = new JsonObject
            {
                ["status"] = affected > 0 ? "adapted" : "preserved", ["target_max_influences"] = 3, ["vertex_count"] = vertexCount, ["affected_vertices"] = affected,
                ["source_max_influences"] = sourceMaxInfluences, ["discarded_weight_sum"] = discardedSum, ["max_discarded_weight"] = maxDiscarded,
                ["mean_discarded_weight"] = MeanDiscarded, ["max_normalization_difference"] = maxNormalization, ["normalization_difference_sum"] = normalizationSum,
                ["mean_normalization_difference"] = affected > 0 ? normalizationSum / affected : 0.0, ["source_weight_sum_max_deviation"] = sumDeviation,
                ["tie_policy"] = "First authored influence slot", ["source_weights_retained_in_extraction"] = true,
                ["geometry_sources"] = "Base GLB plus authored lower-LOD DMX meshes",
                ["normalization_metric"] = "Maximum surviving influence increase from its original normalized weight", ["by_geometry_source"] = bySource,
                ["vertex_scope"] = "Unique referenced vertices per source geometry and skin-weight stream"
            };
            if (affected > 0)
            {
                report["adaptation"] = "Source 1 supports three bone influences; strongest three retained and renormalized";
                report["mean_population"] = "Affected vertices";
            }
            return report;
        }
    }

    /// <summary>Measures target influence loss once per authored vertex stream without modifying it.</summary>
    private static void RecordSkinWeights(IEnumerable<AppearancePrimitive> primitives, AppearanceResult result)
    {
        var audit = result.SkinWeights ??= new();
        foreach (var primitive in primitives)
        {
            if (!result.WeightSources.TryGetValue(primitive.WeightStream, out var seen)) result.WeightSources[primitive.WeightStream] = seen = [];
            var sourceIndices = primitive.WeightVertexIndices;
            foreach (var index in primitive.Indices.Distinct().Order())
            {
                if (!seen.Add(sourceIndices is not null ? sourceIndices[index] : index)) continue;
                var merged = new List<(int Joint, double Weight)>(primitive.Slots);
                for (var slot = 0; slot < primitive.Slots; slot++)
                {
                    var weight = primitive.Weights[index * primitive.Slots + slot];
                    if (weight <= 0) continue;
                    var joint = primitive.Joints[index * primitive.Slots + slot];
                    var at = merged.FindIndex(item => item.Joint == joint);
                    if (at >= 0) merged[at] = (joint, merged[at].Weight + weight);
                    else merged.Add((joint, weight));
                }
                var total = 0.0;
                foreach (var (_, weight) in merged) total += weight;
                audit.Measure(primitive.FromDmx, merged, total, Source1SkinLinks(merged));
            }
        }
        result.Report["skin_weights"] = audit.ToJson();
    }

    /// <summary>Writes selected meshes on the complete source skeleton with target-compatible links.</summary>
    public static void WriteReference(string path, GltfSkin skin, IReadOnlyList<AppearancePrimitive> primitives, CoordinateMode coord)
    {
        SmdSkeleton.WriteAnimation(skin, [skin.BindPose()], path, coord);
        using var output = Fmt.OpenText(path, true);
        output.Write("triangles\n");
        foreach (var primitive in primitives)
        {
            var links = new (int, double)[primitive.Slots];
            for (var corner = 0; corner < primitive.Indices.Length; corner++)
            {
                if (corner % 3 == 0)
                {
                    output.Write(primitive.Material);
                    output.Write('\n');
                }
                var index = primitive.Indices[corner];
                for (var slot = 0; slot < primitive.Slots; slot++) links[slot] = (primitive.Joints[index * primitive.Slots + slot], primitive.Weights[index * primitive.Slots + slot]);
                var uv = primitive.Uv[index];
                output.Write(SmdSkeleton.VertexLine(GeometryPosition(primitive.Vertices[index], coord), GeometryDirection(primitive.Normals[index], coord), new(uv.X, 1.0 - uv.Y), Source1SkinLinks(links)));
            }
        }
        output.Write("end\n");
    }

    /// <summary>Translates the supported compiled stack program without changing its arithmetic.</summary>
    public static string FlexExpression(JsonNode? rule, JsonArray controllers)
    {
        var stack = new List<string>();
        var binary = new Dictionary<string, string> { ["ADD"] = "+", ["SUB"] = "-", ["MUL"] = "*", ["DIV"] = "/" };
        foreach (var operation in rule.Get("m_FlexOps").Arr())
        {
            var opcode = Opcode(operation);
            var operand = operation.Get("m_Data").Lng();
            if (opcode == "CONST")
            {
                var value = BitConverter.UInt32BitsToSingle(unchecked((uint)operand));
                if (!float.IsFinite(value)) throw new InvalidDataException("A flex expression contains a non-finite constant");
                stack.Add(PhysicsMaths.General(value, 9));
            }
            else if (opcode == "FETCH1")
            {
                if (operand < 0 || operand >= controllers.Count) throw new InvalidDataException("A flex expression references an absent controller");
                var name = controllers[(int)operand].Need("m_szName").Str();
                if (!ControllerIdentifier().IsMatch(name)) throw new InvalidDataException($"Source 1 cannot preserve flex-controller identifier {PythonText.Repr(name)}");
                stack.Add(name);
            }
            else if (binary.ContainsKey(opcode) || opcode is "MIN" or "MAX")
            {
                if (stack.Count < 2) throw new InvalidDataException("A flex expression has an invalid operand stack");
                var (second, first) = (stack[^1], stack[^2]);
                stack.RemoveRange(stack.Count - 2, 2);
                stack.Add(binary.TryGetValue(opcode, out var symbol) ? $"({first}{symbol}{second})" : $"{opcode.ToLowerInvariant()}({first},{second})");
            }
            else if (opcode is "NEG" or "ABS")
            {
                if (stack.Count == 0) throw new InvalidDataException("A flex expression has an invalid operand stack");
                var value = stack[^1];
                stack.RemoveAt(stack.Count - 1);
                stack.Add(opcode == "NEG" ? $"(-{value})" : $"abs({value})");
            }
            else throw new InvalidDataException($"Unsupported source flex opcode {opcode}");
        }
        if (stack.Count != 1) throw new InvalidDataException("A flex expression does not produce one value");
        return stack[0];
    }

    private static string Opcode(JsonNode? operation)
    {
        var text = operation.Need("m_OpCode").Str();
        return text.StartsWith("FLEX_OP_", StringComparison.Ordinal) ? text["FLEX_OP_".Length..] : text;
    }

    /// <summary>Unifies named morph controllers while retaining unsupported rule diagnostics.</summary>
    /// <returns>A rig with <c>controllers</c>, <c>expressions</c> and <c>rules</c>, and the rules it could not translate.</returns>
    public static (JsonObject Rig, JsonArray Unsupported) FlexRig(JsonObject fidelity)
    {
        var controllers = new JsonObject();
        var expressions = new JsonObject();
        var rules = new JsonObject();
        var unsupported = new JsonArray();
        var morphs = fidelity.Get("morphs").Arr();
        for (var morphIndex = 0; morphIndex < morphs.Count; morphIndex++)
        {
            var morph = morphs[morphIndex];
            var inputs = morph.Get("m_FlexControllers").Arr();
            foreach (var controller in inputs)
            {
                var name = controller.Need("m_szName").Str();
                if (controllers.Get(name) is { } known && !JsonNode.DeepEquals(known, controller)) throw new InvalidDataException($"Source morph sets disagree about controller {name}");
                controllers[name] = controller?.DeepClone();
            }
            var lastRules = new Dictionary<int, JsonNode?>();
            foreach (var rule in morph.Get("m_FlexRules").Arr()) lastRules[rule.Need("m_nFlex").Int()] = rule;
            var descriptors = morph.Get("m_FlexDesc").Arr();
            for (var descriptorIndex = 0; descriptorIndex < descriptors.Count; descriptorIndex++)
            {
                var name = descriptors[descriptorIndex].Need("m_szFacs").Str();
                if (!lastRules.TryGetValue(descriptorIndex, out var rule))
                {
                    unsupported.Add(new JsonObject { ["feature"] = "flex_rule", ["name"] = name, ["reason"] = "No authored rule", ["morph"] = morphIndex });
                    continue;
                }
                string expression;
                try
                {
                    expression = FlexExpression(rule, inputs);
                }
                catch (InvalidDataException exception)
                {
                    unsupported.Add(new JsonObject { ["feature"] = "flex_rule", ["name"] = name, ["reason"] = exception.Message, ["morph"] = morphIndex });
                    continue;
                }
                if (expressions.Get(name) is { } existing && existing.Str() != expression) throw new InvalidDataException($"Source morph sets disagree about rule {name}");
                expressions[name] = expression;
                var operations = new JsonArray();
                foreach (var operation in rule.Need("m_FlexOps").Arr())
                {
                    var opcode = Opcode(operation);
                    var operand = operation.Get("m_Data").Lng();
                    operations.Add(new JsonObject
                    {
                        ["opcode"] = opcode, ["operand"] = operand, ["controller"] = opcode == "FETCH1" ? inputs[(int)operand].Need("m_szName").Str() : null
                    });
                }
                if (rules.Get(name) is { } compiled && !JsonNode.DeepEquals(compiled, operations)) throw new InvalidDataException($"Source morph sets disagree about compiled rule {name}");
                rules[name] = operations;
            }
        }
        if (controllers.Count > 96) throw new InvalidDataException("Source flex rig exceeds Source 1's 96-controller limit");
        return (new()
        {
            ["controllers"] = new JsonArray(controllers.Select(pair => pair.Value?.DeepClone()).ToArray()), ["expressions"] = expressions, ["rules"] = rules
        }, unsupported);
    }

    private static string G9(double value) => PhysicsMaths.General(value, 9);

    /// <summary>Translates genuine source jiggle bones and enumerates cloth and rig adaptations.</summary>
    private static (List<string> Lines, JsonObject Report) ProceduralQc(JsonObject fidelity, IReadOnlyDictionary<string, string> boneNameMap, IReadOnlySet<string> targetNames)
    {
        var lines = new List<string>();
        JsonArray jiggleBones = [], adaptedCloth = [], unsupportedConstraints = [];
        (string Target, string Source)[] fields =
        [
            ("length", "m_flLength"), ("tip_mass", "m_flTipMass"), ("yaw_stiffness", "m_flYawStiffness"), ("yaw_damping", "m_flYawDamping"), ("pitch_stiffness", "m_flPitchStiffness"),
            ("pitch_damping", "m_flPitchDamping"), ("along_stiffness", "m_flAlongStiffness"), ("along_damping", "m_flAlongDamping")
        ];
        foreach (var aggregate in fidelity.Get("physics").Arr())
        {
            var finiteElements = aggregate.Get("m_pFeModel").Bool() ? aggregate.Get("m_pFeModel") : null;
            var names = finiteElements.Get("m_CtrlName").Arr();
            foreach (var entry in finiteElements.Get("m_JiggleBones").Arr())
            {
                var sourceIndex = (entry.Get("m_nNode") ?? entry.Get("m_nNodeIndex") ?? entry.Get("m_nIndex"))?.Int() ?? -1;
                var data = entry.Has("m_JiggleBone") ? entry.Get("m_JiggleBone") : entry.Get("m_jiggleBone");
                if (sourceIndex < 0 || sourceIndex >= names.Count || !data.Bool())
                {
                    adaptedCloth.Add(new JsonObject { ["feature"] = "jiggle", ["reason"] = "Unknown compiled jiggle representation", ["source"] = entry?.DeepClone() });
                    continue;
                }
                var original = names[sourceIndex].Str();
                var bone = boneNameMap.GetValueOrDefault(original, original);
                if (!targetNames.Contains(bone)) throw new InvalidDataException($"Jiggle bone {original} has no target bone");
                var flags = data.Get("m_nFlags").Int();
                var kind = (flags & 2) != 0 ? "is_rigid" : (flags & 1) != 0 ? "is_flexible" : "";
                if (kind.Length == 0)
                {
                    adaptedCloth.Add(new JsonObject { ["feature"] = "jiggle", ["name"] = original, ["reason"] = "Neither rigid nor flexible" });
                    continue;
                }
                lines.AddRange([$"$jigglebone {Quoted(bone)}", "{", $"    {kind}", "    {"]);
                foreach (var (target, source) in fields)
                    if (data.Has(source)) lines.Add($"        {target} {G9(data.Get(source).Dbl())}");
                if ((flags & 16) != 0) lines.Add($"        angle_constraint {G9(Fmt.Degrees(data.Need("m_flAngleLimit").Dbl()))}");
                foreach (var (flag, label, prefix) in new[] { (4, "yaw", "Yaw"), (8, "pitch", "Pitch") })
                {
                    if ((flags & flag) == 0) continue;
                    double low = Fmt.Degrees(data.Get("m_flMin" + prefix).Dbl()), high = Fmt.Degrees(data.Get("m_flMax" + prefix).Dbl());
                    lines.Add($"        {label}_constraint {G9(low)} {G9(high)}");
                    foreach (var parameter in new[] { "Friction", "Bounce" })
                    {
                        var key = "m_fl" + prefix + parameter;
                        if (data.Has(key)) lines.Add($"        {label}_{parameter.ToLowerInvariant()} {G9(data.Get(key).Dbl())}");
                    }
                }
                if ((flags & 32) == 0) lines.Add("        allow_length_flex");
                lines.Add("    }");
                if ((flags & 64) != 0)
                {
                    lines.AddRange(["    has_base_spring", "    {"]);
                    foreach (var (target, source) in new[] { ("base_mass", "Mass"), ("stiffness", "Stiffness"), ("damping", "Damping") })
                        lines.Add($"        {target} {G9(data.Need("m_flBase" + source).Dbl())}");
                    foreach (var label in new[] { "Left", "Up", "Forward" })
                    {
                        double low = data.Need("m_flBaseMin" + label).Dbl(), high = data.Need("m_flBaseMax" + label).Dbl();
                        lines.Add($"        {label.ToLowerInvariant()}_constraint {G9(low)} {G9(high)}");
                        lines.Add($"        {label.ToLowerInvariant()}_friction {G9(data.Need("m_flBase" + label + "Friction").Dbl())}");
                    }
                    lines.Add("    }");
                }
                lines.Add("}");
                jiggleBones.Add(new JsonObject { ["name"] = original, ["target"] = bone, ["flags"] = flags, ["adapted_flags"] = flags & (0x80 | 0x300) });
            }
            if (finiteElements.Bool() && (finiteElements.Get("m_Rods").Bool() || finiteElements.Get("m_Tris").Bool() || finiteElements.Get("m_Quads").Bool()))
                adaptedCloth.Add(new JsonObject
                {
                    ["feature"] = "finite_element_cloth", ["nodes"] = finiteElements.Get("m_nNodeCount").Int(), ["rods"] = finiteElements.Get("m_Rods").Arr().Count,
                    ["triangles"] = finiteElements.Get("m_Tris").Arr().Count,
                    ["adaptation"] = "Authored anchor/bind follows are retained; coupled Source 2 cloth solver is unavailable in Source 1"
                });
        }
        var constraints = fidelity.Get("constraints").Arr();
        for (var index = 0; index < constraints.Count; index++)
            unsupportedConstraints.Add(new JsonObject
            {
                ["index"] = index, ["type"] = constraints[index].Get("_class").Str(), ["reason"] = "Retained in source graph; requires runtime or baked procedural evaluation"
            });
        return (lines, new() { ["jiggle_bones"] = jiggleBones, ["adapted_cloth"] = adaptedCloth, ["unsupported_constraints"] = unsupportedConstraints });
    }

    private static double[] SingleRotation(Q4 rotation)
    {
        float x = (float)rotation.X, y = (float)rotation.Y, z = (float)rotation.Z, w = (float)rotation.W;
        var n = MathF.Sqrt(x * x + y * y + z * z + w * w);
        if (n == 0f) n = 1f;
        x /= n;
        y /= n;
        z /= n;
        w /= n;
        return [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y), 2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x), 2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)];
    }

    private static double[] DoubleRotation(Q4 rotation)
    {
        var (x, y, z, w) = rotation;
        var n = Math.Sqrt(x * x + y * y + z * z + w * w);
        if (n == 0.0) n = 1.0;
        x /= n;
        y /= n;
        z /= n;
        w /= n;
        return [1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y), 2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x), 2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)];
    }

    private static M4 Local(double[] r, V3 p) => new([r[0], r[1], r[2], p.X, r[3], r[4], r[5], p.Y, r[6], r[7], r[8], p.Z, 0, 0, 0, 1]);

    /// <summary>Expresses each source or target bind frame in the coordinate space written to SMD.</summary>
    /// <remarks>A root goes through the single-precision root conversion, so its rotation builds from float32 scalars as
    /// well. Children stay in double precision. These are the numbers behind the runtime frames, so they stay exact.</remarks>
    public static List<M4> BindWorlds(GltfSkin skin, CoordinateMode coord)
    {
        var worlds = new List<M4>(skin.Joints.Count);
        foreach (var joint in skin.Joints)
        {
            var parent = joint.Parent;
            M4 local;
            if (parent is null)
            {
                var (position, _) = coord.ApplyToRoot(joint.RestTranslation, joint.RestRotation);
                var rotation = coord.SwapAxes ? (coord.Rotation.Single() * joint.RestRotation).Single() : joint.RestRotation.Single();
                local = Local(SingleRotation(rotation), position);
            }
            else local = Local(DoubleRotation(joint.RestRotation), joint.RestTranslation * coord.Scale);
            if (parent >= worlds.Count) throw new InvalidDataException("Procedural source bind requires parent-first bones");
            worlds.Add(parent is { } owner ? worlds[owner] * local : local);
        }
        return worlds;
    }

    private static bool Close(double value, double expected, double absolute) => Math.Abs(value - expected) <= absolute + 1e-5 * Math.Abs(expected);

    /// <summary>Normalizes a caller-supplied rigid source-model alignment to a homogeneous matrix.</summary>
    private static M4 SourceRotationMatrix(M4? value)
    {
        if (value is null) return M4.Identity;
        var rigid = PhysicsMaths.IsFinite(value);
        for (var column = 0; column < 4; column++) rigid &= Close(value[3, column], column == 3 ? 1.0 : 0.0, 1e-8);
        var rotation = PhysicsMaths.RotationOf(value);
        var product = rotation.Transposed() * rotation;
        for (var row = 0; row < 3; row++)
            for (var column = 0; column < 3; column++)
                rigid &= Close(product[row, column], row == column ? 1.0 : 0.0, 1e-7);
        var determinant = NativePhysics.Determinant(PhysicsMaths.Rows(value));
        if (!rigid || !Close(determinant, 1.0, 1e-7)) throw new InvalidDataException("Source model alignment must be a finite rigid transform");
        return value;
    }

    /// <summary>Resolves original CTiltTwist equations and native-frame corrections for GMod.</summary>
    public static (JsonArray Records, JsonObject Corrections) SourceTwistDrivers(GltfDocument gltf, GltfSkin targetSkin, CoordinateMode targetCoord, JsonObject fidelity,
        IReadOnlyDictionary<string, string> boneNameMap, M4? sourceModelRotation = null) =>
        SourceTwistDrivers(GltfSkin.Parse(gltf) ?? throw new InvalidDataException("A ragdoll requires a skinned source model"), CoordinateMode.For(gltf), targetSkin, targetCoord, fidelity, boneNameMap, sourceModelRotation);

    /// <inheritdoc cref="SourceTwistDrivers(GltfDocument, GltfSkin, CoordinateMode, JsonObject, IReadOnlyDictionary{string, string}, M4?)"/>
    /// <remarks>Each frame maps a compiled bone back onto the original Source 2 bone, so runtime constraints can reason in
    /// the frames they were authored in whatever the compiled rig did to its axes.</remarks>
    public static (JsonArray Records, JsonObject Corrections) SourceTwistDrivers(GltfSkin sourceSkin, CoordinateMode sourceCoord, GltfSkin targetSkin, CoordinateMode targetCoord, JsonObject fidelity,
        IReadOnlyDictionary<string, string> boneNameMap, M4? sourceModelRotation = null)
    {
        var alignment = SourceRotationMatrix(sourceModelRotation);
        var sourceWorlds = BindWorlds(sourceSkin, sourceCoord).ConvertAll(world => alignment * world);
        var targetWorlds = BindWorlds(targetSkin, targetCoord);
        var sourceByName = new OrderedDictionary<string, int>(StringComparer.Ordinal);
        for (var index = 0; index < sourceSkin.Joints.Count; index++) sourceByName[sourceSkin.Joints[index].Name] = index;
        var targetByName = new Dictionary<string, int>(StringComparer.Ordinal);
        for (var index = 0; index < targetSkin.Joints.Count; index++) targetByName[targetSkin.Joints[index].Name] = index;
        var sourceByHash = new Dictionary<long, int>();
        foreach (var (name, index) in sourceByName) sourceByHash[RagdollCorrectives.StringToken(name)] = index;
        var corrections = new JsonObject();
        foreach (var (name, index) in sourceByName)
        {
            var targetName = boneNameMap.GetValueOrDefault(name, name);
            if (!targetByName.TryGetValue(targetName, out var target)) continue;
            var correction = targetWorlds[target].Inverse() * sourceWorlds[index];
            corrections[name] = new JsonObject
            {
                ["target"] = targetName, ["target_to_source"] = JsonKeys.Rows(correction), ["source_to_target"] = JsonKeys.Rows(correction.Inverse()),
                ["source_bind_world"] = JsonKeys.Rows(sourceWorlds[index]), ["parent"] = sourceSkin.Joints[index].Parent is { } parent ? sourceSkin.Joints[parent].Name : ""
            };
        }
        M4 LocalBind(int index) => sourceSkin.Joints[index].Parent is { } parent ? sourceWorlds[parent].Inverse() * sourceWorlds[index] : sourceWorlds[index];

        var records = new JsonArray();
        var constraints = fidelity.Get("constraints").Arr();
        for (var index = 0; index < constraints.Count; index++)
        {
            var definition = constraints[index];
            if (definition.Get("_class").Str() != "CTiltTwistConstraint") continue;
            var slaves = definition.Get("m_slaves").Arr();
            var targets = definition.Get("m_targets").Arr();
            if (slaves.Count != 1 || targets.Count != 1 || targets[0].Get("m_bIsAttachment").Bool()) continue;
            if (!sourceByHash.TryGetValue(slaves[0].Need("m_nBoneHash").Lng(), out var slaveIndex) || !sourceByHash.TryGetValue(targets[0].Need("m_nBoneHash").Lng(), out var targetIndex))
                throw new InvalidDataException("Source twist bone hash cannot be resolved against the original skeleton");
            GltfJoint slave = sourceSkin.Joints[slaveIndex], target = sourceSkin.Joints[targetIndex];
            var slaveParent = slave.Parent is { } slaveOwner ? sourceSkin.Joints[slaveOwner].Name : "";
            var targetParent = target.Parent is { } targetOwner ? sourceSkin.Joints[targetOwner].Name : "";
            List<string> required = [slave.Name, target.Name];
            if (slaveParent.Length > 0) required.Add(slaveParent);
            if (targetParent.Length > 0) required.Add(targetParent);
            if (required.Exists(name => !corrections.ContainsKey(name))) throw new InvalidDataException("Source twist depends on a bone absent from the target skeleton");
            M4 slaveLocal = LocalBind(slaveIndex), targetLocal = LocalBind(targetIndex);
            records.Add(new JsonObject
            {
                ["source_index"] = index, ["type"] = "CTiltTwistConstraint", ["slave"] = slave.Name, ["slave_parent"] = slaveParent, ["orig_target_bone"] = target.Name,
                ["orig_parent_bone"] = targetParent, ["offset_quaternion"] = targets[0].Need("m_qOffset")?.DeepClone(),
                ["target_axis"] = definition.Has("m_nTargetAxis") ? definition.Get("m_nTargetAxis").Int() : 0,
                ["slave_axis"] = definition.Has("m_nSlaveAxis") ? definition.Get("m_nSlaveAxis").Int() : 0,
                ["share"] = slaves[0].Has("m_flWeight") ? slaves[0].Get("m_flWeight").Dbl() : 1.0,
                ["original_bind_local"] = new JsonObject { ["position"] = JsonKeys.Vector(slaveLocal.Translation), ["quaternion"] = JsonKeys.Quaternion(NumpyMath.QuaternionFromMatrix(slaveLocal)) },
                ["original_target_bind_local"] = new JsonObject { ["position"] = JsonKeys.Vector(targetLocal.Translation), ["quaternion"] = JsonKeys.Quaternion(NumpyMath.QuaternionFromMatrix(targetLocal)) }
            });
        }
        return (records, corrections);
    }
}
