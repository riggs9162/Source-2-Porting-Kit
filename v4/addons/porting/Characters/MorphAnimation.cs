using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>Named output-weight curves and measured reductions suitable for addon playback.</summary>
public sealed class MorphAnimationResult
{
    public JsonObject Clips { get; } = [];
    public JsonObject Report { get; } = new() { ["clips"] = new JsonArray(), ["unsupported"] = new JsonArray() };
}

/// <summary>One scalar weight curve. A cubic spline holds in-tangent, value and out-tangent per key.</summary>
public sealed record MorphCurve(double[] Times, double[] Values, string Interpolation)
{
    public int Stride => Interpolation == "CUBICSPLINE" ? 3 : 1;

    /// <summary>The curve as the driver stores it, with each cubic key as a three-item list.</summary>
    public JsonObject ToJson()
    {
        var values = new JsonArray();
        for (var key = 0; key < Times.Length; key++)
            values.Add(Stride == 1 ? JsonValue.Create(Values[key]) : JsonKeys.Numbers(Values[key * 3], Values[key * 3 + 1], Values[key * 3 + 2]));
        return new() { ["times"] = JsonKeys.Numbers(Times), ["values"] = values, ["interpolation"] = Interpolation };
    }
}

/// <summary>Preserves exported morph-weight animation independently of skeletal channels.</summary>
public static class MorphAnimation
{
    private static int UpperBound(double[] keys, double value)
    {
        int low = 0, high = keys.Length;
        while (low < high)
        {
            var middle = (low + high) / 2;
            if (keys[middle] <= value) low = middle + 1;
            else high = middle;
        }
        return low;
    }

    /// <summary>Evaluates glTF scalar weights with the authored interpolation and tangent units.</summary>
    public static double[] SampleMorphCurve(MorphCurve curve, double[] times)
    {
        var (keys, values, mode) = curve;
        var result = new double[times.Length];
        if (keys.Length == 1)
        {
            Array.Fill(result, mode == "CUBICSPLINE" ? values[1] : values[0]);
            return result;
        }
        if (mode is not ("STEP" or "LINEAR" or "CUBICSPLINE")) throw new InvalidDataException($"Unsupported morph interpolation: {mode}");
        for (var index = 0; index < times.Length; index++)
        {
            var query = times[index];
            var found = UpperBound(keys, query) - 1;
            var lower = Math.Clamp(found, 0, keys.Length - 2);
            var interval = keys[lower + 1] - keys[lower];
            var amount = Math.Clamp((query - keys[lower]) / interval, 0.0, 1.0);
            if (mode == "STEP") result[index] = values[Math.Clamp(found, 0, keys.Length - 1)];
            else if (mode == "LINEAR") result[index] = values[lower] * (1.0 - amount) + values[lower + 1] * amount;
            else if (query <= keys[0]) result[index] = values[1];
            else if (query >= keys[^1]) result[index] = values[(keys.Length - 1) * 3 + 1];
            else
            {
                double square = amount * amount, cube = amount * amount * amount;
                result[index] = (2 * cube - 3 * square + 1) * values[lower * 3 + 1] + (cube - 2 * square + amount) * interval * values[lower * 3 + 2]
                    + (-2 * cube + 3 * square) * values[(lower + 1) * 3 + 1] + (cube - square) * interval * values[(lower + 1) * 3];
            }
        }
        return result;
    }

    /// <summary>Resolves each weight slot without depending on optional glTF targetNames extras.</summary>
    private static List<string> TargetNames(GltfDocument document, int nodeIndex)
    {
        var mesh = document.Meshes[document.Nodes[nodeIndex].Need("mesh").Int()];
        var primitives = mesh.Get("primitives").Arr();
        if (primitives.Count == 0) throw new InvalidDataException("An animated morph mesh has no primitives");
        var authored = mesh.Get("extras").Get("targetNames").Arr().Select(name => name.Str()).ToList();
        var targets = primitives[0].Get("targets").Arr();
        if (authored.Count == targets.Count) return authored;
        var names = new List<string>();
        var accessors = document.Root.Get("accessors").Arr();
        foreach (var target in targets)
        {
            var accessor = target.Has("POSITION") ? target.Get("POSITION") : target.Get("NORMAL");
            var name = accessor is not null ? accessors[accessor.Int()].Get("name").Str() : "";
            if (name.Length == 0) throw new InvalidDataException("An animated morph target has no authored name");
            names.Add(name);
        }
        if (names.Distinct(StringComparer.Ordinal).Count() != names.Count) throw new InvalidDataException("An animated morph mesh repeats a target name");
        return names;
    }

    /// <summary>Removes redundant linear keys with a bounded maximum weight error at source keys.</summary>
    public static (MorphCurve Curve, double Error) ReduceCurve(MorphCurve curve, double tolerance)
    {
        var (times, values, mode) = curve;
        if (mode != "LINEAR" || times.Length <= 2) return (curve, 0.0);
        var retained = new SortedSet<int> { 0, times.Length - 1 };
        var pending = new Stack<(int First, int Last)>();
        pending.Push((0, times.Length - 1));
        while (pending.Count > 0)
        {
            var (first, last) = pending.Pop();
            if (last - first <= 1) continue;
            var largest = first + 1;
            var worst = double.NegativeInfinity;
            for (var index = first + 1; index < last; index++)
            {
                var amount = (times[index] - times[first]) / (times[last] - times[first]);
                var expected = values[first] * (1.0 - amount) + values[last] * amount;
                var difference = Math.Abs(values[index] - expected);
                if (difference <= worst) continue;
                worst = difference;
                largest = index;
            }
            if (worst <= tolerance) continue;
            retained.Add(largest);
            pending.Push((first, largest));
            pending.Push((largest, last));
        }
        var indices = retained.ToArray();
        var reduced = curve with { Times = Array.ConvertAll(indices, index => times[index]), Values = Array.ConvertAll(indices, index => values[index]) };
        var sampled = SampleMorphCurve(reduced, times);
        var maximum = 0.0;
        for (var index = 0; index < times.Length; index++) maximum = Math.Max(maximum, Math.Abs(sampled[index] - values[index]));
        return (reduced, maximum);
    }

    /// <summary>Expresses each piece as a cubic in normalized time over the supplied intervals.</summary>
    private static double[,] IntervalCoefficients(MorphCurve curve, double[] edges)
    {
        var (keys, values, mode) = curve;
        var pieces = edges.Length - 1;
        var starts = edges[..^1];
        var ends = edges[1..];
        var middle = new double[pieces];
        for (var index = 0; index < pieces; index++) middle[index] = starts[index] + (ends[index] - starts[index]) * 0.5;
        var coefficients = new double[pieces, 4];
        var centre = SampleMorphCurve(curve, middle);
        for (var index = 0; index < pieces; index++) coefficients[index, 0] = centre[index];
        if (keys.Length == 1 || mode == "STEP") return coefficients;
        if (mode == "LINEAR")
        {
            double[] first = SampleMorphCurve(curve, starts), last = SampleMorphCurve(curve, ends);
            for (var index = 0; index < pieces; index++)
            {
                coefficients[index, 0] = first[index];
                coefficients[index, 1] = last[index] - first[index];
            }
            return coefficients;
        }
        for (var index = 0; index < pieces; index++)
        {
            if (middle[index] < keys[0] || middle[index] > keys[^1]) continue;
            var lower = Math.Clamp(UpperBound(keys, middle[index]) - 1, 0, keys.Length - 2);
            var duration = keys[lower + 1] - keys[lower];
            double first = values[lower * 3 + 1], last = values[(lower + 1) * 3 + 1];
            double outgoing = values[lower * 3 + 2] * duration, incoming = values[(lower + 1) * 3] * duration;
            double p0 = first, p1 = outgoing, p2 = -3 * first - 2 * outgoing + 3 * last - incoming, p3 = 2 * first + outgoing - 2 * last + incoming;
            var origin = (starts[index] - keys[lower]) / duration;
            var scale = (ends[index] - starts[index]) / duration;
            coefficients[index, 0] = p0 + p1 * origin + p2 * (origin * origin) + p3 * (origin * origin * origin);
            coefficients[index, 1] = scale * (p1 + 2 * p2 * origin + 3 * p3 * (origin * origin));
            coefficients[index, 2] = scale * scale * (p2 + 3 * p3 * origin);
            coefficients[index, 3] = scale * scale * scale * p3;
        }
        return coefficients;
    }

    /// <summary>The real roots of a polynomial of degree two or less, with leading and trailing zero terms removed.</summary>
    private static IEnumerable<double> RealRoots(double a, double b, double c)
    {
        if (a != 0)
        {
            var discriminant = b * b - 4 * a * c;
            if (discriminant < 0) yield break;
            var root = Math.Sqrt(discriminant);
            yield return (-b + root) / (2 * a);
            yield return (-b - root) / (2 * a);
        }
        else if (b != 0) yield return -c / b;
    }

    /// <summary>Compares complete curves, including step left limits and cubic interior extrema.</summary>
    private static bool CurvesAgree(MorphCurve first, MorphCurve second, double tolerance = 1e-6)
    {
        if (first.Interpolation == second.Interpolation && first.Times.SequenceEqual(second.Times) && first.Values.SequenceEqual(second.Values)) return true;
        var edges = first.Times.Concat(second.Times).Distinct().Order().ToArray();
        double[] left = SampleMorphCurve(first, edges), right = SampleMorphCurve(second, edges);
        for (var index = 0; index < edges.Length; index++)
            if (!(Math.Abs(left[index] - right[index]) <= tolerance)) return false;
        if (edges.Length < 2 || (first.Interpolation == "LINEAR" && second.Interpolation == "LINEAR")) return true;
        double[,] a = IntervalCoefficients(first, edges), b = IntervalCoefficients(second, edges);
        for (var piece = 0; piece < edges.Length - 1; piece++)
        {
            double c0 = a[piece, 0] - b[piece, 0], c1 = a[piece, 1] - b[piece, 1], c2 = a[piece, 2] - b[piece, 2], c3 = a[piece, 3] - b[piece, 3];
            if (Math.Abs(c0) > tolerance || Math.Abs(c0 + c1 + c2 + c3) > tolerance) return false;
            foreach (var critical in RealRoots(3 * c3, 2 * c2, c1))
                if (critical is > 0 and < 1 && Math.Abs(c0 + critical * (c1 + critical * (c2 + critical * c3))) > tolerance) return false;
        }
        return true;
    }

    /// <summary>Collects sequence-synchronised curves; conflicting per-mesh weights remain explicit.</summary>
    /// <param name="names">Source clip to compiled sequence. Clips absent from it are skipped; null keeps every clip.</param>
    /// <param name="durations">Source clip to authored duration, which may run past the last exported key.</param>
    public static MorphAnimationResult ExportMorphAnimations(GltfDocument document, IReadOnlyDictionary<string, string>? names = null,
        IReadOnlyDictionary<string, double>? durations = null, double tolerance = 1e-6, PortContext? context = null)
    {
        var result = new MorphAnimationResult();
        foreach (var animation in document.Root.Get("animations").Arr())
        {
            context?.Check();
            var originalName = animation.Get("name").Str();
            if (names is not null && !names.ContainsKey(originalName)) continue;
            var curves = new OrderedDictionary<string, MorphCurve>(StringComparer.Ordinal);
            var allTargets = new HashSet<string>(StringComparer.Ordinal);
            long sourceKeys = 0;
            var conflicts = new SortedSet<string>(StringComparer.Ordinal);
            var samplers = animation.Need("samplers").Arr();
            foreach (var channel in animation.Get("channels").Arr())
            {
                var target = channel.Need("target");
                if (target.Need("path").Str() != "weights") continue;
                var targets = TargetNames(document, target.Need("node").Int());
                var sampler = samplers[channel.Need("sampler").Int()];
                var times = document.Accessor(sampler.Need("input").Int()).Values;
                var increasing = times.Length > 0 && times.All(double.IsFinite);
                for (var index = 1; increasing && index < times.Length; index++) increasing = times[index] > times[index - 1];
                if (!increasing) throw new InvalidDataException($"{originalName}: morph times must be finite and increasing");
                var mode = sampler.Has("interpolation") ? sampler.Get("interpolation").Str() : "LINEAR";
                if (mode is not ("LINEAR" or "STEP" or "CUBICSPLINE")) throw new InvalidDataException($"{originalName}: unsupported morph interpolation {mode}");
                var multiplier = mode == "CUBICSPLINE" ? 3 : 1;
                var values = document.Accessor(sampler.Need("output").Int()).Values;
                if (targets.Count == 0 || values.Length != times.Length * targets.Count * multiplier) throw new InvalidDataException($"{originalName}: morph weights do not match target and key counts");
                if (!values.All(double.IsFinite)) throw new InvalidDataException($"{originalName}: morph weights contain non-finite values");
                for (var index = 0; index < targets.Count; index++)
                {
                    var name = targets[index];
                    allTargets.Add(name);
                    var extracted = new double[times.Length * multiplier];
                    for (var slot = 0; slot < extracted.Length; slot++) extracted[slot] = values[slot * targets.Count + index];
                    var curve = new MorphCurve(times, extracted, mode);
                    sourceKeys += times.Length;
                    if (curves.TryGetValue(name, out var existing))
                    {
                        if (!CurvesAgree(curve, existing)) conflicts.Add(name);
                    }
                    else curves[name] = curve;
                }
            }
            if (allTargets.Count == 0) continue;
            if (conflicts.Count > 0)
            {
                result.Report["unsupported"]!.AsArray().Add(new JsonObject
                {
                    ["name"] = originalName, ["reason"] = "Per-mesh weights disagree for shared Source 1 morph outputs", ["targets"] = JsonKeys.Strings(conflicts)
                });
                continue;
            }
            var reduced = new JsonObject();
            var maximum = 0.0;
            long retainedKeys = 0;
            foreach (var (name, source) in curves)
            {
                var (curve, error) = ReduceCurve(source, tolerance);
                maximum = Math.Max(maximum, error);
                if (!curve.Values.Any(value => Math.Abs(value) > 0)) continue;
                reduced[name] = curve.ToJson();
                retainedKeys += curve.Times.Length;
            }
            var targetName = names?.GetValueOrDefault(originalName, originalName) ?? originalName;
            var lastKey = curves.Values.Max(curve => curve.Times[^1]);
            var duration = durations is not null && durations.TryGetValue(originalName, out var authored) ? authored : lastKey;
            if (!double.IsFinite(duration) || duration < 0) throw new InvalidDataException($"{originalName}: authored morph duration must be finite and nonnegative");
            var allowance = Math.Max(1e-5, Math.Abs(duration) * 4.0 * 1.1920928955078125e-7);
            if (lastKey > duration + allowance) throw new InvalidDataException($"{originalName}: morph keys extend beyond the authored sequence duration");
            result.Clips[targetName] = new JsonObject
            {
                ["duration"] = duration, ["targets"] = JsonKeys.Strings(allTargets.Order(StringComparer.Ordinal)), ["curves"] = reduced
            };
            result.Report["clips"]!.AsArray().Add(new JsonObject
            {
                ["name"] = originalName, ["target_name"] = targetName, ["status"] = "adapted", ["method"] = "sequence_cycle_flex_output_callback",
                ["duration"] = duration, ["targets"] = allTargets.Count, ["exported_last_key_time"] = lastKey,
                ["constant_tail_restored"] = Math.Max(0.0, duration - lastKey), ["source_keys"] = sourceKeys, ["retained_keys"] = retainedKeys, ["maximum_weight_error"] = maximum
            });
        }
        return result;
    }
}
