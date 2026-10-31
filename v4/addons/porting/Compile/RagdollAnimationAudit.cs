using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>One local animation record of a compiled model.</summary>
public sealed record CompiledAnimation(string Name, double Fps, int Frames, double Duration, int Flags, bool Loop);

/// <summary>One sequence of a compiled model and the animations it blends.</summary>
public sealed record CompiledSequence(string Name, string Activity, int Flags, bool Loop, bool Delta, bool Autoplay, double LastFrame, List<int> AnimationIndices, List<CompiledAnimation> Animations);

/// <summary>Audits compiled source-clip timing and exact flex programs using Valve studio.h tables.</summary>
public static class RagdollAnimationAudit
{
    private static readonly Dictionary<string, int> Codes = new(StringComparer.Ordinal)
    {
        ["CONST"] = 1, ["FETCH1"] = 2, ["ADD"] = 4, ["SUB"] = 5, ["MUL"] = 6, ["DIV"] = 7, ["MAX"] = 13, ["MIN"] = 14
    };

    /// <summary>Whether a timing audit reports this failure instead of stopping. A missing file still stops it.</summary>
    private static bool IsTimingFault(Exception exception) => exception is CompiledDataError or InvalidDataException or FormatException or OverflowException or KeyNotFoundException;

    /// <summary>Whether a flex audit reports this failure. Flex programs also index a stack, so that counts too.</summary>
    private static bool IsFlexFault(Exception exception) => IsTimingFault(exception) || exception is ArgumentException or IndexOutOfRangeException or InvalidOperationException;

    private static bool Close(double a, double b, double relative, double absolute) =>
        a == b || (double.IsFinite(a) && double.IsFinite(b) && Math.Abs(a - b) <= Math.Max(relative * Math.Max(Math.Abs(a), Math.Abs(b)), absolute));

    /// <summary>Resolves local v48 and v49 sequence blend indices into 100-byte animation records.</summary>
    public static (List<CompiledAnimation> Animations, List<CompiledSequence> Sequences) ReadCompiledAnimationTiming(string modelPath)
    {
        var source = new CompiledBinary(modelPath);
        source.Require(0, 196);
        if (!source.HasMagic("IDST") || source.Int(4) is not (48 or 49)) throw new ModelAuditError("Animation timing requires a Source 1 v48/v49 studio model");
        var pair = source.Ints(180, 2);
        var animations = new List<CompiledAnimation>();
        foreach (var offset in source.Table(pair[0], pair[1], 100))
        {
            source.Require(offset, 20);
            var name = source.Int(offset + 4);
            var fps = source.Float(offset + 8);
            int flags = source.Int(offset + 12), frames = source.Int(offset + 16);
            if (!double.IsFinite(fps) || fps <= 0 || frames < 1) throw new ModelAuditError("Compiled animation has invalid FPS or frame count");
            animations.Add(new(source.String(offset + name), fps, frames, (frames - 1) / fps, flags, (flags & 1) != 0));
        }
        pair = source.Ints(188, 2);
        var sequences = new List<CompiledSequence>();
        foreach (var offset in source.Table(pair[0], pair[1], 212))
        {
            var head = source.Ints(offset + 4, 3);
            int label = head[0], activity = head[1], flags = head[2];
            var blendTable = source.Ints(offset + 56, 2);
            var size = source.Ints(offset + 68, 2);
            if (size[0] < 1 || size[1] < 1 || (long)size[0] * size[1] != blendTable[0]) throw new ModelAuditError("Compiled sequence blend dimensions do not match its count");
            var indices = source.Table(blendTable[0], offset + blendTable[1], 2).Select(blend => (int)source.Short(blend)).ToList();
            if (indices.Any(index => index < 0 || index >= animations.Count)) throw new ModelAuditError("Compiled sequence references an absent local animation");
            sequences.Add(new(source.String(offset + label), activity != 0 ? source.String(offset + activity) : "", flags, (flags & 1) != 0, (flags & 4) != 0, (flags & 8) != 0,
                source.Float(offset + 132), indices, indices.ConvertAll(index => animations[index])));
        }
        return (animations, sequences);
    }

    /// <summary>Checks each generated original clip against its authored FPS, frame count and loop flag.</summary>
    public static JsonObject AuditSourceClipTiming(string modelPath, JsonObject report)
    {
        JsonArray errors = [], clips = [], intentional = [];
        var result = new JsonObject { ["ok"] = false, ["errors"] = errors, ["clips"] = clips, ["intentional_sequences"] = intentional };
        try
        {
            var (animations, sequences) = ReadCompiledAnimationTiming(modelPath);
            var records = report.Get("animations");
            var expected = (records.Has("clips") ? records.Get("clips") : records.Get("source_clips")).Arr();
            var lookup = new Dictionary<string, CompiledSequence>(StringComparer.Ordinal);
            foreach (var sequence in sequences) lookup[sequence.Name] = sequence;
            if (lookup.Count != sequences.Count) errors.Add("Compiled sequence names are not unique");
            foreach (var authored in expected)
            {
                if (!authored.Has("target_name")) continue;
                var name = authored.Get("target_name").Str();
                if (!lookup.TryGetValue(name, out var sequence))
                {
                    errors.Add($"Missing compiled source sequence {name}");
                    continue;
                }
                if (sequence.Animations.Count != 1)
                {
                    errors.Add($"Source sequence {name} unexpectedly contains blended animations");
                    continue;
                }
                var animation = sequence.Animations[0];
                var frames = authored.Key("frames");
                var fps = authored.Key("fps");
                if (animation.Frames != frames.Dbl()) errors.Add($"Source sequence {name} has {animation.Frames} frames versus authored {frames.Str()}");
                if (!Close(animation.Fps, fps.Dbl(), 1e-6, 1e-5)) errors.Add($"Source sequence {name} FPS differs from authored {fps.Str()}");
                if (sequence.Loop != authored.Key("loop").Bool()) errors.Add($"Source sequence {name} loop flag differs from authored metadata");
                if (sequence.Delta) errors.Add($"Source sequence {name} is marked delta despite absolute exported poses");
                if (authored.Has("autoplay") && sequence.Autoplay != authored.Get("autoplay").Bool()) errors.Add($"Source sequence {name} autoplay flag differs from the selected playback policy");
                clips.Add(new JsonObject
                {
                    ["source_name"] = authored.Key("name")?.DeepClone(), ["target_name"] = name, ["frames"] = animation.Frames, ["fps"] = animation.Fps, ["loop"] = sequence.Loop,
                    ["duration"] = animation.Duration, ["compiled_last_frame"] = sequence.LastFrame, ["authored_frames"] = frames?.DeepClone(), ["authored_fps"] = fps?.DeepClone(),
                    ["sampling"] = authored.Get("sampling")?.DeepClone() ?? new JsonObject()
                });
            }
            if (lookup.TryGetValue("ragdoll", out var nativeBind))
                intentional.Add(new JsonObject { ["name"] = "ragdoll", ["purpose"] = "Native physics bind sequence", ["frames"] = CompiledText.Integers(nativeBind.Animations.Select(item => item.Frames)) });
            if (lookup.TryGetValue("source_ragdoll", out var renamed))
                intentional.Add(new JsonObject
                {
                    ["name"] = "source_ragdoll", ["source_name"] = "ragdoll", ["purpose"] = "Original authored clip renamed to preserve the native physics bind",
                    ["frames"] = CompiledText.Integers(renamed.Animations.Select(item => item.Frames))
                });
            result["compiled_sequence_count"] = sequences.Count;
            result["compiled_animation_count"] = animations.Count;
            result["checked_source_clips"] = clips.Count;
        }
        catch (Exception exception) when (IsTimingFault(exception))
        {
            errors.Add(exception.Message);
        }
        result["ok"] = errors.Count == 0;
        return result;
    }

    /// <summary>Translates authored postfix expressions independently for a binary equality check.</summary>
    /// <remarks>Source's flex program has no negate or absolute opcode, so both expand here the way the compiler
    /// expands them: negation is <c>0 - x</c>, and absolute is <c>max(x, 0 - x)</c>.</remarks>
    private static List<(int Code, uint Operand)> ExpectedProgram(JsonArray operations, Dictionary<string, int> controllers)
    {
        var stack = new List<List<(int Code, uint Operand)>>();
        List<(int Code, uint Operand)> Pop()
        {
            if (stack.Count == 0) throw new CompiledDataError("pop from empty list");
            var top = stack[^1];
            stack.RemoveAt(stack.Count - 1);
            return top;
        }
        foreach (var operation in operations)
        {
            var name = operation.Key("opcode").Str();
            if (name is "CONST" or "FETCH1")
            {
                uint operand;
                if (name == "CONST") operand = unchecked((uint)operation.Key("operand").Lng());
                else
                {
                    var controller = operation.Key("controller").Str();
                    operand = (uint)(controllers.TryGetValue(controller, out var index) ? index : throw new KeyNotFoundException(PythonText.Repr(controller)));
                }
                stack.Add([(Codes[name], operand)]);
            }
            else if (name is "NEG" or "ABS")
            {
                var value = Pop();
                List<(int, uint)> negated = [(1, 0), .. value, (5, 0)];
                stack.Add(name == "NEG" ? negated : [.. value, .. negated, (13, 0)]);
            }
            else
            {
                List<(int, uint)> second = Pop(), first = Pop();
                if (!Codes.TryGetValue(name, out var code)) throw new KeyNotFoundException(PythonText.Repr(name));
                stack.Add([.. first, .. second, (code, 0)]);
            }
        }
        if (stack.Count != 1) throw new CompiledDataError("Authored flex program has an invalid operand stack");
        return stack[0];
    }

    /// <summary>Compares every controller, range, UI pointer, rule opcode and operand to its source rig.</summary>
    public static JsonObject AuditSourceFlexPrograms(string modelPath, JsonObject rig)
    {
        JsonArray errors = [], controllersOut = [], rulesOut = [];
        var result = new JsonObject { ["ok"] = false, ["errors"] = errors, ["controllers"] = controllersOut, ["rules"] = rulesOut };
        try
        {
            var source = new CompiledBinary(modelPath);
            source.Require(0, 392);
            var expectedControllers = rig.Get("controllers").Arr();
            var expectedRules = rig.Get("rules").Obj();
            var pair = source.Ints(260, 2);
            var descriptors = source.Table(pair[0], pair[1], 4).Select(offset => source.String(offset + source.Int(offset))).ToList();
            pair = source.Ints(268, 2);
            var controllerOffsets = source.Table(pair[0], pair[1], 20).ToList();
            var compiledNames = new List<string>();
            var compiled = new List<(string Name, string Type, double Minimum, double Maximum)>();
            foreach (var offset in controllerOffsets)
            {
                source.Require(offset, 20);
                int kind = source.Int(offset), name = source.Int(offset + 4);
                double low = source.Float(offset + 12), high = source.Float(offset + 16);
                var record = (source.String(offset + name), source.String(offset + kind), low, high);
                compiled.Add(record);
                compiledNames.Add(record.Item1);
                controllersOut.Add(new JsonObject { ["name"] = record.Item1, ["type"] = record.Item2, ["minimum"] = low, ["maximum"] = high });
            }
            if (compiled.Count != expectedControllers.Count) errors.Add("Compiled controller count differs from the authored rig");
            foreach (var (actual, expected) in compiled.Zip(expectedControllers))
            {
                var name = expected.Key("m_szName").Str();
                double wantedLow = (float)(expected.Has("min") ? expected.Get("min").Dbl() : 0), wantedHigh = (float)(expected.Has("max") ? expected.Get("max").Dbl() : 1);
                var type = expected.Has("m_szType") ? expected.Get("m_szType").Str() : "default";
                if (actual.Name != name || actual.Type != type || actual.Minimum != wantedLow || actual.Maximum != wantedHigh) errors.Add($"Controller order/type/range differs for {name}");
            }
            var controllerIndices = new Dictionary<string, int>(StringComparer.Ordinal);
            for (var index = 0; index < expectedControllers.Count; index++) controllerIndices[expectedControllers[index].Key("m_szName").Str()] = index;
            pair = source.Ints(276, 2);
            var programs = new Dictionary<string, List<(int Code, uint Operand)>>(StringComparer.Ordinal);
            foreach (var offset in source.Table(pair[0], pair[1], 12))
            {
                var rule = source.Ints(offset, 3);
                if (rule[0] < 0 || rule[0] >= descriptors.Count) throw new ModelAuditError("Flex rule references an absent morph description");
                var name = descriptors[rule[0]];
                if (programs.ContainsKey(name)) errors.Add($"Compiled model repeats flex rule {name}");
                programs[name] = source.Table(rule[1], offset + rule[2], 8).Select(operation =>
                {
                    source.Require(operation, 8);
                    return (source.Int(operation), source.UInt(operation + 4));
                }).ToList();
            }
            if (!programs.Keys.ToHashSet(StringComparer.Ordinal).SetEquals(expectedRules.Select(pairing => pairing.Key))) errors.Add("Compiled flex-rule targets differ from the source graph");
            foreach (var (name, operations) in expectedRules)
            {
                var expected = ExpectedProgram(operations.Arr(), controllerIndices);
                var exact = programs.TryGetValue(name, out var actual) && actual.SequenceEqual(expected);
                if (!exact) errors.Add($"Compiled flex opcodes or operands differ for {name}");
                rulesOut.Add(new JsonObject { ["name"] = name, ["operations"] = expected.Count, ["exact"] = exact });
            }
            pair = source.Ints(384, 2);
            var uiCount = pair[0];
            if (uiCount != expectedControllers.Count) errors.Add("Compiled flex UI count differs from the authored controller count");
            var ui = 0;
            foreach (var offset in source.Table(uiCount, pair[1], 20))
            {
                source.Require(offset, 18);
                int name = source.Int(offset), controller = source.Int(offset + 4), remap = source.Byte(offset + 16), stereo = source.Byte(offset + 17);
                if (ui >= controllerOffsets.Count || offset + controller != controllerOffsets[ui] || source.String(offset + name) != compiledNames[ui] || remap != 0 || stereo != 0)
                    errors.Add($"Compiled UI record {ui} does not reference its authored controller");
                ui++;
            }
            result["controller_count"] = compiled.Count;
            result["rule_count"] = programs.Count;
            result["operation_count"] = programs.Values.Sum(program => program.Count);
            result["ui_count"] = uiCount;
        }
        catch (Exception exception) when (IsFlexFault(exception))
        {
            errors.Add(CompiledText.Describe(exception));
        }
        result["ok"] = errors.Count == 0;
        return result;
    }
}
