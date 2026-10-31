namespace PortingKit.Addons.Porting;

/// <summary>The single similarity transform that carries the source rig onto the donor's.</summary>
/// <remarks>One turn about Z, one uniform scale, one offset. It is deliberately not per-bone: transporting flesh across a
/// real disagreement between two rigs stretches it, and every per-bone variant tried tore the model.</remarks>
public readonly record struct RigAlignment(double Yaw, double Scale, V3 Translation)
{
    public static readonly RigAlignment None = new(0.0, 1.0, V3.Zero);
}

/// <summary>The measurements the playermodel rig is built from.</summary>
/// <remarks>The player path keeps the source's bone positions and only needs these. Rebuilding a skeleton as the donor's,
/// the rest of the v3 module, serves a separate route the workflow does not take.</remarks>
public static class SkeletonRetarget
{
    /// <summary>A rotation of the given degrees about Z.</summary>
    public static M3 YawMatrix(double degrees)
    {
        double c = Math.Cos(Fmt.Radians(degrees)), s = Math.Sin(Fmt.Radians(degrees));
        return new(c, -s, 0.0, s, c, 0.0, 0.0, 0.0, 1.0);
    }

    /// <summary>The total of a run the way the array library adds one up: in eight-way blocks, halving past 128 items.</summary>
    /// <remarks>Past eight items this differs from plain addition in the last bits, and those bits decide a fit's result when
    /// two candidate turns score almost alike.</remarks>
    private static double PairwiseSum(double[] values, int start, int count)
    {
        if (count < 8)
        {
            var sum = 0.0;
            for (var index = 0; index < count; index++) sum += values[start + index];
            return sum;
        }
        if (count > 128)
        {
            var half = count / 2;
            half -= half % 8;
            return PairwiseSum(values, start, half) + PairwiseSum(values, start + half, count - half);
        }
        var blocks = new double[8];
        Array.Copy(values, start, blocks, 0, 8);
        var at = 8;
        for (; at < count - count % 8; at += 8)
            for (var lane = 0; lane < 8; lane++) blocks[lane] += values[start + at + lane];
        var total = blocks[0] + blocks[1] + (blocks[2] + blocks[3]) + (blocks[4] + blocks[5] + (blocks[6] + blocks[7]));
        for (; at < count; at++) total += values[start + at];
        return total;
    }

    private static double PairwiseSum(double[] values) => PairwiseSum(values, 0, values.Length);

    /// <summary>The turn, scale and offset that best carry the source rig onto the donor's.</summary>
    /// <remarks>
    /// <para>Source 2 character rigs and ValveBiped disagree about which way the world's X axis points, so a Source 2 skeleton
    /// dropped into Source 1 sits a quarter turn off, and the two builds rarely stand the same height. Both fall out of one
    /// least-squares fit over every bone the rigs share.</para>
    /// <para>The turn is constrained to yaw. A free fit will happily tilt a character a degree or two to shave residual off a
    /// mismatched shoulder, and a tilted bind is far more visible than the residual it buys. It is measured rather than
    /// assumed: the closed form is solved first, then checked against the four cardinal turns, and whichever genuinely fits
    /// best wins. A rig that already matches scores yaw 0, scale 1 and nothing happens.</para>
    /// <para><paramref name="weights"/> says how much flesh each bone carries. The two builds differ non-uniformly, so a
    /// single scale is a compromise, and which compromise you get depends on who votes. Counting every bone equally lets the
    /// fingers decide and lands 6 per cent out; weighting by flesh puts it within 0.6 per cent of what a hand port chose.</para>
    /// </remarks>
    public static RigAlignment MeasureAlignment(IEnumerable<KeyValuePair<string, M4>> ours, IReadOnlyDictionary<string, M4> theirs, string root, IReadOnlyDictionary<string, double>? weights = null)
    {
        var shared = ours.Where(pair => theirs.ContainsKey(pair.Key) && pair.Key != root).ToList();
        if (shared.Count < 3) return RigAlignment.None;
        var oursPoints = shared.ConvertAll(pair => pair.Value.Translation);
        var theirsPoints = shared.ConvertAll(pair => theirs[pair.Key].Translation);
        var share = shared.ConvertAll(pair => Math.Max(0.0, weights is null ? 1.0 : weights.GetValueOrDefault(pair.Key, 1.0))).ToArray();
        var total = PairwiseSum(share);
        if (total <= 0.0)
        {
            Array.Fill(share, 1.0);
            total = share.Length;
        }
        for (var index = 0; index < share.Length; index++) share[index] /= total;
        V3 oursMiddle = V3.Zero, theirsMiddle = V3.Zero;
        for (var index = 0; index < share.Length; index++)
        {
            oursMiddle += oursPoints[index] * share[index];
            theirsMiddle += theirsPoints[index] * share[index];
        }
        var o = oursPoints.ConvertAll(point => point - oursMiddle);
        var t = theirsPoints.ConvertAll(point => point - theirsMiddle);
        var cross = PairwiseSum(share.Select((value, index) => value * (t[index].Y * o[index].X - t[index].X * o[index].Y)).ToArray());
        var dot = PairwiseSum(share.Select((value, index) => value * (t[index].X * o[index].X + t[index].Y * o[index].Y)).ToArray());
        var solved = Fmt.Degrees(Math.Atan2(-cross, dot));
        double Error(double degrees)
        {
            var turn = YawMatrix(degrees);
            return PairwiseSum(share.Select((value, index) => value * (t[index] - turn.Apply(o[index])).Length).ToArray());
        }
        var yaw = 0.0;
        var best = Error(0.0);
        foreach (var candidate in new[] { 90.0, -90.0, 180.0, solved, -solved })
        {
            var error = Error(candidate);
            if (error >= best) continue;
            best = error;
            yaw = candidate;
        }
        var rotation = YawMatrix(yaw);
        var spreadTerms = new double[share.Length * 3];
        var alongTerms = new double[share.Length * 3];
        for (var index = 0; index < share.Length; index++)
        {
            var turned = rotation.Apply(o[index]);
            for (var axis = 0; axis < 3; axis++)
            {
                spreadTerms[index * 3 + axis] = share[index] * turned[axis] * turned[axis];
                alongTerms[index * 3 + axis] = share[index] * turned[axis] * t[index][axis];
            }
        }
        var spread = PairwiseSum(spreadTerms);
        var along = PairwiseSum(alongTerms);
        var scale = spread > 1e-12 ? along / spread : 1.0;
        if (!double.IsFinite(scale) || scale <= 0.0) scale = 1.0;
        return new(yaw, scale, theirsMiddle - rotation.Apply(oursMiddle) * scale);
    }

    /// <summary>Just the turn from <see cref="MeasureAlignment"/>, in degrees.</summary>
    public static double MeasureAlignmentYaw(IEnumerable<KeyValuePair<string, M4>> ours, IReadOnlyDictionary<string, M4> theirs, string root) => MeasureAlignment(ours, theirs, root).Yaw;

    /// <summary>World bind matrix per joint, from the skin's rest transforms.</summary>
    /// <remarks>Scale is deliberately dropped: a bind frame carrying non-unit scale cannot be expressed as a Source bone, and
    /// every Source 2 Viewer export handled here is unscaled. A joint listed ahead of its parent sits against the identity.</remarks>
    public static M4[] SkinWorldMatrices(GltfSkin skin)
    {
        var worlds = new M4[skin.Joints.Count];
        Array.Fill(worlds, M4.Identity);
        for (var index = 0; index < worlds.Length; index++)
        {
            var joint = skin.Joints[index];
            var local = M4.Trs(joint.RestTranslation, joint.RestRotation);
            worlds[index] = joint.Parent is { } parent ? worlds[parent] * local : local;
        }
        return worlds;
    }

    /// <summary>The minimal rotation carrying one direction onto another.</summary>
    /// <remarks>Antiparallel directions have no unique answer, so any perpendicular axis serves as a half turn.</remarks>
    public static M3 Swing(V3 fromDirection, V3 toDirection)
    {
        double na = fromDirection.Length, nb = toDirection.Length;
        if (na < 1e-12 || nb < 1e-12) return M3.Identity;
        V3 a = fromDirection / na, b = toDirection / nb;
        var axis = a.Cross(b);
        var sine = axis.Length;
        var cosine = a.Dot(b);
        if (sine < 1e-12)
        {
            if (cosine > 0.0) return M3.Identity;
            var seed = Math.Abs(a.X) < 0.9 ? new V3(1.0, 0.0, 0.0) : new V3(0.0, 1.0, 0.0);
            axis = a.Cross(seed);
            axis /= axis.Length;
            sine = 0.0;
            cosine = -1.0;
        }
        else axis /= sine;
        var (x, y, z) = axis;
        var cross = new M3(0.0, -z, y, z, 0.0, -x, -y, x, 0.0);
        var square = cross * cross;
        var fall = 1.0 - cosine;
        return new(1.0 + sine * cross.M00 + fall * square.M00, sine * cross.M01 + fall * square.M01, sine * cross.M02 + fall * square.M02,
            sine * cross.M10 + fall * square.M10, 1.0 + sine * cross.M11 + fall * square.M11, sine * cross.M12 + fall * square.M12,
            sine * cross.M20 + fall * square.M20, sine * cross.M21 + fall * square.M21, 1.0 + sine * cross.M22 + fall * square.M22);
    }
}
