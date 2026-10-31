using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>One skin joint mapped to a node, with its parent as an index into the skin's joint list.</summary>
public sealed record GltfJoint(string Name, int NodeIndex, int? Parent, M4 InverseBind, V3 RestTranslation, Q4 RestRotation, V3 RestScale);

/// <summary>A parent-local joint transform.</summary>
public readonly record struct JointPose(V3 Translation, Q4 Rotation, V3 Scale);

/// <summary>A skeleton read from a glTF skin; ragdoll physics may extend it with compatibility bones.</summary>
public sealed record GltfSkin(List<GltfJoint> Joints, int RootIndex, int SkinIndex)
{
    public IReadOnlyList<string> JointNames => Joints.ConvertAll(joint => joint.Name);

    /// <summary>The bind pose in joint order.</summary>
    public JointPose[] BindPose() => Joints.Select(joint => new JointPose(joint.RestTranslation, joint.RestRotation, joint.RestScale)).ToArray();

    /// <summary>World bind matrices from the rest transforms. Scale is dropped, because no Source bone can carry it.</summary>
    public M4[] WorldMatrices()
    {
        var worlds = new M4[Joints.Count];
        for (var index = 0; index < Joints.Count; index++)
        {
            var joint = Joints[index];
            var local = M4.Trs(joint.RestTranslation, joint.RestRotation);
            worlds[index] = joint.Parent is { } parent ? worlds[parent] * local : local;
        }
        return worlds;
    }

    /// <summary>Parses a skin, or returns null when the model has none.</summary>
    public static GltfSkin? Parse(GltfDocument document, int skinIndex = 0)
    {
        if (skinIndex >= document.Skins.Count) return null;
        var skin = document.Skins[skinIndex];
        var joints = skin.Get("joints").Arr().Select(item => item.Int()).ToArray();
        if (joints.Length == 0) return null;
        var nodes = document.Nodes;
        var parents = new Dictionary<int, int>();
        for (var index = 0; index < nodes.Count; index++)
            foreach (var child in nodes[index].Get("children").Arr())
                parents[child.Int()] = index;
        var jointByNode = new Dictionary<int, int>();
        for (var index = 0; index < joints.Length; index++) jointByNode[joints[index]] = index;
        var inverseBinds = skin.Has("inverseBindMatrices") ? document.Accessor(skin.Get("inverseBindMatrices").Int()) : null;
        var result = new List<GltfJoint>(joints.Length);
        int? root = null;
        for (var index = 0; index < joints.Length; index++)
        {
            var node = joints[index] < nodes.Count ? nodes[joints[index]] : null;
            var name = node.Get("name").Str();
            if (name.Length == 0) name = $"joint_{index}";
            int? parent = parents.TryGetValue(joints[index], out var parentNode) && jointByNode.TryGetValue(parentNode, out var parentJoint) ? parentJoint : null;
            if (parent is null) root ??= index;
            var inverse = M4.Identity;
            if (inverseBinds is not null)
            {
                var cells = new double[16];
                for (var column = 0; column < 4; column++)
                    for (var row = 0; row < 4; row++)
                        cells[row * 4 + column] = inverseBinds[index, column * 4 + row];
                inverse = new(cells);
            }
            var translation = node.Has("translation") ? node.Get("translation").Vec3().Single() : V3.Zero;
            var rotation = node.Has("rotation") ? node.Get("rotation").Quat().Single() : Q4.Identity;
            var scale = node.Has("scale") ? node.Get("scale").Vec3().Single() : V3.One;
            result.Add(new(name, joints[index], parent, inverse, translation, rotation, scale));
        }
        return new(result, root ?? 0, skinIndex);
    }
}

/// <summary>One sampler's keys for one joint path.</summary>
public sealed record GltfChannel(double[] Times, AccessorData Values, string Interpolation);

/// <summary>One animation clip with channels keyed by joint index and glTF path.</summary>
public sealed record GltfClip(string Name, double Duration, Dictionary<(int Joint, string Path), GltfChannel> Channels)
{
    /// <summary>Parses all animations. Channels on nodes outside the skin are dropped because SMD cannot hold them.</summary>
    public static List<GltfClip> ParseAll(GltfDocument document, GltfSkin skin)
    {
        var jointByNode = new Dictionary<int, int>();
        for (var index = 0; index < skin.Joints.Count; index++) jointByNode[skin.Joints[index].NodeIndex] = index;
        var clips = new List<GltfClip>();
        var animations = document.Root.Get("animations").Arr();
        for (var index = 0; index < animations.Count; index++)
        {
            var animation = animations[index];
            var name = animation.Get("name").Str();
            if (name.Length == 0) name = $"anim_{index}";
            var channels = new Dictionary<(int, string), GltfChannel>();
            var duration = 0.0;
            var samplers = animation.Get("samplers").Arr();
            foreach (var channel in animation.Get("channels").Arr())
            {
                var target = channel.Get("target");
                var path = target.Get("path").Str();
                if (!target.Has("node") || path is not ("translation" or "rotation" or "scale")) continue;
                if (!jointByNode.TryGetValue(target.Get("node").Int(), out var joint)) continue;
                if (!channel.Has("sampler") || channel.Get("sampler").Int() >= samplers.Count) continue;
                var sampler = samplers[channel.Get("sampler").Int()];
                if (!sampler.Has("input") || !sampler.Has("output")) continue;
                var times = document.Accessor(sampler.Get("input").Int());
                if (times.Width != 1 || times.Count == 0) continue;
                var interpolation = sampler.Has("interpolation") ? sampler.Get("interpolation").Str() : "LINEAR";
                channels[(joint, path)] = new(times.Values, document.Accessor(sampler.Get("output").Int()), interpolation);
                duration = Math.Max(duration, times.Values[^1]);
            }
            clips.Add(new(name, duration, channels));
        }
        return clips;
    }

    private static int Segment(double[] times, double t)
    {
        var at = Array.BinarySearch(times, t);
        var upper = at >= 0 ? at + 1 : ~at;
        while (at >= 0 && upper < times.Length && times[upper] <= t) upper++;
        return Math.Clamp(upper - 1, 0, times.Length - 2);
    }

    private static float[] Row(AccessorData values, int row)
    {
        var result = new float[values.Width];
        for (var column = 0; column < result.Length; column++) result[column] = (float)values[row, column];
        return result;
    }

    /// <summary>A short single-precision dot product as the BLAS kernel computes it.</summary>
    /// <remarks>Each product rounds to single precision but the running sum is double, and the last bit of this
    /// decides how a rotation near gimbal lock splits between roll and yaw.</remarks>
    private static float Dot(float[] a, float[] b)
    {
        var sum = 0.0;
        for (var i = 0; i < a.Length; i++) sum += a[i] * b[i];
        return (float)sum;
    }

    /// <summary>The parameter across one key segment, computed in single precision like the time stream itself.</summary>
    private static float Fraction(double[] times, int k, double t)
    {
        var dt = (float)times[k + 1] - (float)times[k];
        return dt <= 0f ? 0f : ((float)t - (float)times[k]) / dt;
    }

    /// <summary>Samples one channel in single precision, operation by operation.</summary>
    /// <remarks>Keys are single precision, and the sampled result differs from a double-precision blend by enough
    /// to matter: on long root motion the two drift 1.5e-5 apart, and a rotation that rounds to a tiny negative
    /// wraps to 2 pi in the SMD instead of 0. End tests compare in single precision too, but the segment search
    /// uses the exact time.</remarks>
    private static float[] Sample(GltfChannel channel, double t)
    {
        var (times, values, interpolation) = channel;
        var count = times.Length;
        var cubic = interpolation == "CUBICSPLINE";
        if ((float)t <= (float)times[0]) return Row(values, cubic ? 1 : 0);
        if ((float)t >= (float)times[^1]) return Row(values, cubic ? (count - 1) * 3 + 1 : count - 1);
        var k = Segment(times, t);
        if (interpolation == "STEP") return Row(values, k);
        var u = Fraction(times, k, t);
        var result = new float[values.Width];
        if (cubic)
        {
            var dt = (float)times[k + 1] - (float)times[k];
            float u2 = u * u, u3 = u2 * u;
            float[] p0 = Row(values, k * 3 + 1), m0 = Row(values, k * 3 + 2), p1 = Row(values, (k + 1) * 3 + 1), m1 = Row(values, (k + 1) * 3);
            for (var i = 0; i < result.Length; i++)
                result[i] = (2 * u3 - 3 * u2 + 1) * p0[i] + (u3 - 2 * u2 + u) * (m0[i] * dt) + (-2 * u3 + 3 * u2) * p1[i] + (u3 - u2) * (m1[i] * dt);
            return result;
        }
        float[] from = Row(values, k), to = Row(values, k + 1);
        for (var i = 0; i < result.Length; i++) result[i] = (1f - u) * from[i] + u * to[i];
        return result;
    }

    private static Q4 Normalized(float[] row)
    {
        var length = MathF.Sqrt(Dot(row, row));
        var divisor = length > 1e-12 ? length : (float)1e-12;
        return new(row[0] / divisor, row[1] / divisor, row[2] / divisor, row[3] / divisor);
    }

    /// <summary>Spherical interpolation that stays in single precision until the angle is involved.</summary>
    /// <remarks>Close quaternions blend linearly on single-precision keys. The far branch really does run in
    /// double precision, because its weights come out of an arccosine of a Python float.</remarks>
    private static Q4 Slerp(float[] from, float[] to, double t)
    {
        double dot = Dot(from, to);
        if (dot < 0.0)
        {
            to = Array.ConvertAll(to, value => -value);
            dot = -dot;
        }
        if (dot > 0.9995)
        {
            float a = (float)(1.0 - t), b = (float)t;
            return Normalized([a * from[0] + b * to[0], a * from[1] + b * to[1], a * from[2] + b * to[2], a * from[3] + b * to[3]]);
        }
        var angle = Math.Acos(dot);
        var sine = Math.Sin(angle);
        var theta = angle * t;
        double p = Math.Sin(angle - theta) / sine, q = Math.Sin(theta) / sine;
        return new(p * from[0] + q * to[0], p * from[1] + q * to[1], p * from[2] + q * to[2], p * from[3] + q * to[3]);
    }

    private static Q4 SampleRotation(GltfChannel channel, double t)
    {
        var (times, values, interpolation) = channel;
        if (interpolation == "LINEAR" && (float)t > (float)times[0] && (float)t < (float)times[^1])
        {
            var k = Segment(times, t);
            return Slerp(Row(values, k), Row(values, k + 1), Fraction(times, k, t));
        }
        return Normalized(Sample(channel, t));
    }

    private static V3 SampleVector(GltfChannel channel, double t)
    {
        var row = Sample(channel, t);
        return new(row[0], row[1], row[2]);
    }

    /// <summary>Samples at a uniform rate; joints with no channel for a path keep their bind value.</summary>
    public (List<JointPose[]> Frames, int Count) Sample(GltfSkin skin, double fps)
    {
        var count = Duration <= 0.0 ? 1 : Math.Max(1, (int)Math.Round(Duration * fps) + 1);
        var bind = skin.BindPose();
        var frames = new List<JointPose[]>(count);
        for (var frame = 0; frame < count; frame++)
        {
            var t = fps > 0 ? frame / fps : 0.0;
            if (t > Duration) t = Duration;
            var poses = new JointPose[bind.Length];
            for (var joint = 0; joint < bind.Length; joint++)
            {
                var translation = Channels.TryGetValue((joint, "translation"), out var a) ? SampleVector(a, t).Single() : bind[joint].Translation;
                var rotation = Channels.TryGetValue((joint, "rotation"), out var b) ? SampleRotation(b, t).Single() : bind[joint].Rotation;
                var scale = Channels.TryGetValue((joint, "scale"), out var c) ? SampleVector(c, t).Single() : bind[joint].Scale;
                poses[joint] = new(translation, rotation, scale);
            }
            frames.Add(poses);
        }
        return (frames, count);
    }

    /// <summary>Samples on the authored frame grid, restoring constant tails glTF leaves out.</summary>
    /// <remarks>glTF holds every sampler's last key, so an exporter may omit redundant terminal keys.
    /// The compiled metadata keeps the full duration; sampling on that grid invents no new motion.</remarks>
    public (List<JointPose[]> Frames, int Count, JsonObject Report) SampleAuthored(GltfSkin skin, AnimationMetadata authored)
    {
        var fps = authored.Fps;
        var frames = authored.Frames;
        if (!double.IsFinite(fps) || fps <= 0 || frames < 1)
            throw new InvalidDataException($"{Name}: authored animation requires positive FPS and frame count");
        var duration = (frames - 1) / fps;
        if (!double.IsFinite(Duration) || Duration < 0) throw new InvalidDataException($"{Name}: exported animation duration is invalid");
        var tolerance = Math.Max(1e-5, duration * 4.0 * 1.1920928955078125e-7);
        if (Duration > duration + tolerance)
            throw new InvalidDataException($"{Name}: exported keys end at {Fmt.G(Duration)}s beyond the authored {frames}-frame duration {Fmt.G(duration)}s");
        var (samples, count) = (this with { Duration = duration }).Sample(skin, fps);
        if (count != frames) throw new InvalidDataException($"{Name}: authored frame sampling produced {count} frames instead of {frames}");
        var tail = Math.Max(0.0, duration - Duration);
        return (samples, count, new JsonObject
        {
            ["authored_frames"] = frames,
            ["authored_duration"] = duration,
            ["exported_last_key_time"] = Duration,
            ["constant_tail_restored"] = tail > tolerance ? tail : 0.0,
            ["method"] = "authored_frame_grid_with_sampler_endpoint_hold"
        });
    }
}
