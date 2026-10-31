using System.Text;

namespace PortingKit.Addons.Porting;

/// <summary>How joint transforms map into Source space.</summary>
/// <param name="Scale">Applied to every translation; 1 for an export already in inches.</param>
/// <param name="SwapAxes">Whether the root carries an axis conversion.</param>
/// <param name="SwapQuaternion">That conversion. Null means the +90 degrees about X a Y-up export needs.</param>
public sealed record CoordinateMode(double Scale = 1.0, bool SwapAxes = false, Q4? SwapQuaternion = null)
{
    private static readonly Q4 YUpSwap = new(Math.Sin(Math.PI / 4), 0.0, 0.0, Math.Cos(Math.PI / 4));

    public Q4 Rotation => SwapQuaternion ?? YUpSwap.Single();

    /// <summary>Converts the mesh and skeleton together from a Source 2 Viewer export's measured space.</summary>
    public static CoordinateMode For(GltfDocument document) => document.PositionSpace() switch
    {
        GltfDocument.SpaceMetres => new(GltfDocument.SourceUnitsPerMetre, true, GltfDocument.Source2BasisQuaternion),
        "" => throw new InvalidDataException("Native port requires a Source 2 Viewer export with known units"),
        _ => new()
    };

    /// <summary>Applies scale and the axis swap to a root transform; children inherit it through the hierarchy.</summary>
    /// <remarks>The swap runs in single precision throughout, operation by operation, because the original's
    /// arrays are all single precision here. Rotating in double and rounding at the end misses the sixth decimal
    /// of root bones, which shows up in the QC's <c>$definebone</c> lines.</remarks>
    public (V3 Translation, Q4 Rotation) ApplyToRoot(V3 translation, Q4 rotation)
    {
        var moved = ApplyToChild(translation);
        return !SwapAxes ? (moved, rotation.Single()) : (RotateSingle(Rotation, moved), MultiplySingle(Rotation, rotation));
    }

    private static (float X, float Y, float Z) CrossSingle((float X, float Y, float Z) a, (float X, float Y, float Z) b) =>
        (a.Y * b.Z - a.Z * b.Y, a.Z * b.X - a.X * b.Z, a.X * b.Y - a.Y * b.X);

    private static V3 RotateSingle(Q4 rotation, V3 vector)
    {
        (float X, float Y, float Z) axis = ((float)rotation.X, (float)rotation.Y, (float)rotation.Z), value = ((float)vector.X, (float)vector.Y, (float)vector.Z);
        var first = CrossSingle(axis, value);
        var second = CrossSingle(axis, first);
        var twice = (float)(2.0 * (float)rotation.W);
        return new(value.X + twice * first.X + 2f * second.X, value.Y + twice * first.Y + 2f * second.Y, value.Z + twice * first.Z + 2f * second.Z);
    }

    private static Q4 MultiplySingle(Q4 left, Q4 right)
    {
        float ax = (float)left.X, ay = (float)left.Y, az = (float)left.Z, aw = (float)left.W;
        float bx = (float)right.X, by = (float)right.Y, bz = (float)right.Z, bw = (float)right.W;
        return new(aw * bx + ax * bw + ay * bz - az * by, aw * by - ax * bz + ay * bw + az * bx, aw * bz + ax * by - ay * bx + az * bw, aw * bw - ax * bx - ay * by - az * bz);
    }

    /// <summary>Scales in single precision, the precision the glTF stream and the compiled bones both use.</summary>
    public V3 ApplyToChild(V3 translation)
    {
        var scale = (float)Scale;
        return new((float)translation.X * scale, (float)translation.Y * scale, (float)translation.Z * scale);
    }
}

/// <summary>Writes SMD skeleton data and the QC bone definitions that must agree with it.</summary>
public static class SmdSkeleton
{
    /// <summary>One vertex line with every field tab-separated for StudioMDL CE's parser.</summary>
    /// <remarks>CE reads the weight block as its own tab-delimited field. Joining it to the UV with a space
    /// leaves that field empty and aborts the compile with "Bone weight 0 not found".</remarks>
    public static string VertexLine(V3 position, V3 normal, V2 texcoord, IReadOnlyList<(int Joint, double Weight)> links)
    {
        var line = new StringBuilder();
        line.Append(Fmt.Int(links[0].Joint)).Append('\t')
            .Append(Fmt.F9(position.X)).Append(' ').Append(Fmt.F9(position.Y)).Append(' ').Append(Fmt.F9(position.Z)).Append('\t')
            .Append(Fmt.F9(normal.X)).Append(' ').Append(Fmt.F9(normal.Y)).Append(' ').Append(Fmt.F9(normal.Z)).Append('\t')
            .Append(Fmt.F9(texcoord.X)).Append(' ').Append(Fmt.F9(texcoord.Y));
        if (links.Count > 1)
        {
            var total = links.Sum(link => link.Weight);
            line.Append('\t').Append(Fmt.Int(links.Count));
            foreach (var (joint, weight) in links) line.Append(' ').Append(Fmt.Int(joint)).Append(' ').Append(Fmt.F9(weight / total));
        }
        return line.Append('\n').ToString();
    }

    /// <summary>Deduplicates names; glTF allows repeats but Source does not.</summary>
    public static string[] BoneNames(GltfSkin skin)
    {
        var used = new HashSet<string>(StringComparer.Ordinal);
        var names = new string[skin.Joints.Count];
        for (var index = 0; index < names.Length; index++)
        {
            var name = skin.Joints[index].Name;
            if (!used.Add(name)) name = $"{skin.Joints[index].Name}_{index}";
            used.Add(name);
            names[index] = name;
        }
        return names;
    }

    /// <summary>Assigns stable node indices with every parent ahead of its children.</summary>
    public static int[] NodeOrder(GltfSkin skin)
    {
        var indices = Enumerable.Repeat(-1, skin.Joints.Count).ToArray();
        var pending = Enumerable.Range(0, skin.Joints.Count).ToList();
        var next = 0;
        while (pending.Count > 0)
        {
            var remaining = new List<int>();
            foreach (var joint in pending)
            {
                if (skin.Joints[joint].Parent is not { } parent || indices[parent] >= 0) indices[joint] = next++;
                else remaining.Add(joint);
            }
            if (remaining.Count == pending.Count) throw new InvalidDataException("Loop in bone parenting.");
            pending = remaining;
        }
        return indices;
    }

    /// <summary>Writes the nodes block and one skeleton block entry per frame.</summary>
    /// <remarks>Rotations land in columns X, Y, Z as roll, pitch and yaw, each wrapped into 0 to 2 pi the way
    /// the Source angle type wraps them. Bones in the wrong columns sit at cycled world transforms even though
    /// skinning still cancels, which shows up as bones drawn far from the mesh.</remarks>
    public static void WriteSkeleton(TextWriter output, GltfSkin skin, IReadOnlyList<JointPose[]> frames, CoordinateMode coordinates)
    {
        var names = BoneNames(skin);
        var order = NodeOrder(skin);
        output.Write("version 1\nnodes\n");
        foreach (var joint in Enumerable.Range(0, order.Length).OrderBy(index => order[index]))
        {
            if (!Ascii.IsValid(names[joint])) throw new InvalidDataException($"Bone name is not ASCII: {names[joint]}");
            var parent = skin.Joints[joint].Parent is { } index ? order[index] : -1;
            output.Write($"{Fmt.Int(order[joint])} \"{names[joint]}\" {Fmt.Int(parent)}\n");
        }
        output.Write("end\nskeleton\n");
        for (var time = 0; time < frames.Count; time++)
        {
            output.Write($"time {Fmt.Int(time)}\n");
            for (var joint = 0; joint < skin.Joints.Count; joint++)
            {
                var (translation, rotation, _) = frames[time][joint];
                if (skin.Joints[joint].Parent is null) (translation, rotation) = coordinates.ApplyToRoot(translation, rotation);
                else translation = coordinates.ApplyToChild(translation);
                var (pitch, yaw, roll) = rotation.ToQAngle();
                output.Write(string.Join(' ', Fmt.Int(order[joint]), Fmt.F6(translation.X), Fmt.F6(translation.Y), Fmt.F6(translation.Z)));
                output.Write("  ");
                output.Write(string.Join(' ', Fmt.F6(Fmt.Radians(Fmt.WrapDegrees(roll))), Fmt.F6(Fmt.Radians(Fmt.WrapDegrees(pitch))), Fmt.F6(Fmt.Radians(Fmt.WrapDegrees(yaw)))));
                output.Write('\n');
            }
        }
        output.Write("end\n");
    }

    /// <summary>Writes an animation-only SMD: nodes and skeleton with no triangles.</summary>
    public static void WriteAnimation(GltfSkin skin, IReadOnlyList<JointPose[]> frames, string path, CoordinateMode coordinates)
    {
        if (skin.Joints.Count == 0) throw new InvalidDataException("Skin has no joints");
        if (frames.Count == 0) throw new InvalidDataException("No animation frames to write");
        using var output = Fmt.OpenText(path);
        WriteSkeleton(output, skin, frames, coordinates);
    }

    /// <summary>The skin root's bind as position and pitch, yaw, roll degrees.</summary>
    /// <remarks>A static physics SMD's root bone must match this, or studiomdl bakes physics vertices against
    /// the identity and the runtime applies the skeleton's real root rotation on top, tilting the collision.</remarks>
    public static (V3 Position, (double Pitch, double Yaw, double Roll) Angles) RootBind(GltfSkin skin, CoordinateMode coordinates)
    {
        var root = skin.Joints[skin.RootIndex];
        var (translation, rotation) = coordinates.ApplyToRoot(root.RestTranslation, root.RestRotation);
        return (translation, rotation.ToQAngle());
    }

    /// <summary>Builds $definebone lines from the bind pose, the same data as frame 0 of an animation SMD.</summary>
    /// <param name="compilerYaw">The model-space rotation studiomdl applies to loaded SMD roots but not to
    /// $definebone roots. Matching them keeps zero-weight sequence bones in the same orientation.</param>
    public static List<string> DefineBoneLines(GltfSkin skin, CoordinateMode coordinates, double compilerYaw = 0.0)
    {
        var half = Fmt.Radians(compilerYaw) * 0.5;
        var compiler = new Q4(0.0, 0.0, Math.Sin(half), Math.Cos(half));
        var lines = new List<string>(skin.Joints.Count);
        foreach (var joint in skin.Joints)
        {
            var parent = joint.Parent is { } index ? skin.Joints[index].Name : "";
            V3 translation;
            Q4 rotation;
            if (joint.Parent is null)
            {
                (translation, rotation) = coordinates.ApplyToRoot(joint.RestTranslation, joint.RestRotation);
                translation = compiler.Rotate(translation).Single();
                rotation = (compiler * rotation).Single();
            }
            else
            {
                translation = coordinates.ApplyToChild(joint.RestTranslation);
                rotation = joint.RestRotation;
            }
            var (pitch, yaw, roll) = rotation.ToQAngle();
            lines.Add($"$definebone \"{joint.Name}\" \"{parent}\" {Fmt.F6(translation.X)} {Fmt.F6(translation.Y)} {Fmt.F6(translation.Z)} " +
                $"{Fmt.F6(pitch)} {Fmt.F6(yaw)} {Fmt.F6(roll)} 0 0 0 0 0 0");
        }
        return lines;
    }

    /// <summary>Appends an identity root to every frame without moving authored bones.</summary>
    public static void AddModelOriginBone(string path, int index, string name)
    {
        var text = File.ReadAllText(path);
        var nodesAt = text.IndexOf("nodes\n", StringComparison.Ordinal);
        var prefix = text[..nodesAt];
        var nodes = text[(nodesAt + "nodes\n".Length)..];
        var endAt = nodes.IndexOf("end\n", StringComparison.Ordinal);
        var nodeLines = nodes[..endAt];
        var remainder = nodes[(endAt + "end\n".Length)..];
        if (nodeLines.Split('\n', StringSplitOptions.RemoveEmptyEntries).Any(line => line.Trim().Length > 0 && int.Parse(line.Split(' ', StringSplitOptions.RemoveEmptyEntries)[0]) == index))
            throw new InvalidDataException($"Compatibility bone index {index} already exists in {path}");
        var skeletonAt = remainder.IndexOf("skeleton\n", StringComparison.Ordinal);
        var before = remainder[..skeletonAt];
        var skeleton = remainder[(skeletonAt + "skeleton\n".Length)..];
        var framesEnd = skeleton.IndexOf("end\n", StringComparison.Ordinal);
        var frames = skeleton[..framesEnd];
        var tail = skeleton[(framesEnd + "end\n".Length)..];
        var output = new List<string>();
        var inFrame = false;
        foreach (var line in frames.Split('\n'))
        {
            if (line.Length == 0) continue;
            if (line.StartsWith("time ", StringComparison.Ordinal))
            {
                if (inFrame) output.Add($"{index} 0 0 0 0 0 0");
                inFrame = true;
            }
            output.Add(line);
        }
        if (!inFrame) throw new InvalidDataException($"SMD has no animation frames: {path}");
        output.Add($"{index} 0 0 0 0 0 0");
        Fmt.WriteText(path, prefix + "nodes\n" + nodeLines + $"{index} \"{name}\" -1\nend\n" + before + "skeleton\n" + string.Join('\n', output) + "\nend\n" + tail);
    }

    /// <summary>Rejects data an SMD cannot carry instead of silently changing a native skeleton.</summary>
    public static void ValidateNativeData(GltfDocument document, GltfSkin skin, IEnumerable<GltfClip> clips)
    {
        if (skin.Joints.Count > 256) throw new InvalidDataException("Garry's Mod supports at most 256 bones");
        var names = skin.Joints.Select(joint => joint.Name.ToLowerInvariant()).ToList();
        if (names.Count != names.Distinct(StringComparer.Ordinal).Count()) throw new InvalidDataException("Source 1 requires unique case-insensitive bone names");
        foreach (var joint in skin.Joints)
            if (Math.Abs(joint.RestScale.X - 1) > 1e-5 || Math.Abs(joint.RestScale.Y - 1) > 1e-5 || Math.Abs(joint.RestScale.Z - 1) > 1e-5)
                throw new InvalidDataException($"SMD cannot preserve bind scale on {joint.Name}");
        foreach (var clip in clips)
            foreach (var ((_, path), channel) in clip.Channels)
            {
                if (path == "scale" && channel.Values.Values.Any(value => Math.Abs(value - 1) > 1e-5))
                    throw new InvalidDataException($"SMD cannot preserve animated scale in {clip.Name}");
                if (channel.Interpolation is not ("LINEAR" or "STEP"))
                    throw new InvalidDataException($"Unsupported interpolation {channel.Interpolation} in {clip.Name}");
            }
        foreach (var mesh in document.Meshes)
            foreach (var primitive in mesh.Get("primitives").Arr())
                if (primitive.Get("attributes").Has("JOINTS_1"))
                    throw new InvalidDataException("Extra skin influence sets require explicit weight reduction");
    }
}
