namespace PortingKit.Addons.Porting;

/// <summary>A row-major 3x3 block, the rotation of a bone frame without its translation.</summary>
/// <remarks>The array library picks a different kernel for each shape of product, and each adds its terms in its own order.
/// The orders below were measured against it term by term. They decide nothing but the last bit, but that bit is the sign of
/// a bone offset that should be exactly zero, which prints as <c>-0.000000</c> in a QC and changes the driver data's hash.
/// Pick the method by the shape of the array expression, not by what it means geometrically.</remarks>
public readonly record struct M3(double M00, double M01, double M02, double M10, double M11, double M12, double M20, double M21, double M22)
{
    public static readonly M3 Identity = new(1, 0, 0, 0, 1, 0, 0, 0, 1);

    public V3 Column0 => new(M00, M10, M20);

    /// <summary>The upper-left block of a transform.</summary>
    public static M3 Of(M4 matrix) => new(matrix[0, 0], matrix[0, 1], matrix[0, 2], matrix[1, 0], matrix[1, 1], matrix[1, 2], matrix[2, 0], matrix[2, 1], matrix[2, 2]);

    /// <summary>The product of two matrices, or of any stack of them, one fused multiply-add per term in ascending order, as
    /// <see cref="M4"/> sums it.</summary>
    public static M3 operator *(in M3 a, in M3 b) => new(
        Ascending(a.M00, b.M00, a.M01, b.M10, a.M02, b.M20), Ascending(a.M00, b.M01, a.M01, b.M11, a.M02, b.M21), Ascending(a.M00, b.M02, a.M01, b.M12, a.M02, b.M22),
        Ascending(a.M10, b.M00, a.M11, b.M10, a.M12, b.M20), Ascending(a.M10, b.M01, a.M11, b.M11, a.M12, b.M21), Ascending(a.M10, b.M02, a.M11, b.M12, a.M12, b.M22),
        Ascending(a.M20, b.M00, a.M21, b.M10, a.M22, b.M20), Ascending(a.M20, b.M01, a.M21, b.M11, a.M22, b.M21), Ascending(a.M20, b.M02, a.M21, b.M12, a.M22, b.M22));

    private static double Ascending(double a0, double b0, double a1, double b1, double a2, double b2) =>
        Math.FusedMultiplyAdd(a2, b2, Math.FusedMultiplyAdd(a1, b1, Math.FusedMultiplyAdd(a0, b0, 0.0)));

    private static double Outer(double a0, double b0, double a1, double b1, double a2, double b2) =>
        Math.FusedMultiplyAdd(a2, b2, Math.FusedMultiplyAdd(a0, b0, a1 * b1));

    private static double Split(double a0, double b0, double a1, double b1, double a2, double b2) => a0 * b0 + a2 * b2 + a1 * b1;

    private static double Plain(double a0, double b0, double a1, double b1, double a2, double b2) => a0 * b0 + a1 * b1 + a2 * b2;

    /// <summary>A matrix times a vector, <c>m @ v</c>.</summary>
    public V3 Apply(V3 v) => new(Outer(M00, v.X, M01, v.Y, M02, v.Z), Outer(M10, v.X, M11, v.Y, M12, v.Z), Outer(M20, v.X, M21, v.Y, M22, v.Z));

    /// <summary>This matrix's transpose times a vector, <c>m.T @ v</c>.</summary>
    public V3 ApplyTransposed(V3 v) => new(Ascending(M00, v.X, M10, v.Y, M20, v.Z), Ascending(M01, v.X, M11, v.Y, M21, v.Z), Ascending(M02, v.X, M12, v.Y, M22, v.Z));

    /// <summary>A row of an array of points carried through the matrix, <c>points @ m.T</c>.</summary>
    public V3 ApplyToRow(V3 v) => new(Ascending(M00, v.X, M01, v.Y, M02, v.Z), Ascending(M10, v.X, M11, v.Y, M12, v.Z), Ascending(M20, v.X, M21, v.Y, M22, v.Z));

    /// <summary>One frame of a stack of matrices times a vector, <c>einsum("fij,fj->fi")</c>.</summary>
    public V3 ApplyInStack(V3 v) => new(Split(M00, v.X, M01, v.Y, M02, v.Z), Split(M10, v.X, M11, v.Y, M12, v.Z), Split(M20, v.X, M21, v.Y, M22, v.Z));

    /// <summary>One frame of a stack with swapped axes times a vector, which the summation cannot run through contiguously.</summary>
    public V3 ApplyTransposedInStack(V3 v) => new(Plain(M00, v.X, M10, v.Y, M20, v.Z), Plain(M01, v.X, M11, v.Y, M21, v.Z), Plain(M02, v.X, M12, v.Y, M22, v.Z));

    public M3 Transposed() => new(M00, M10, M20, M01, M11, M21, M02, M12, M22);

    public double Determinant => M00 * (M11 * M22 - M12 * M21) - M01 * (M10 * M22 - M12 * M20) + M02 * (M10 * M21 - M11 * M20);

    /// <summary>A transform with this rotation at a position.</summary>
    public M4 At(V3 position) => new([M00, M01, M02, position.X, M10, M11, M12, position.Y, M20, M21, M22, position.Z, 0, 0, 0, 1]);

    /// <summary>The same rotation as a transform with no translation.</summary>
    public M4 ToM4() => At(V3.Zero);
}

/// <summary>Sine and cosine as the x87 floating point unit gives them, which is what the rotation library's Windows build runs on.</summary>
/// <remarks>The unit reduces an angle by a 66-bit pi, rounds to a 64-bit mantissa and then, on storing, to a double. So its
/// cosine of a double pi over two is 6.1230317e-17 where the C runtime says 6.1232340e-17, and away from such angles the two
/// still part in the last bit about 3 per cent of the time. That bit is the sign of a bone offset that should be zero, which
/// prints as <c>-0.000000</c> in a QC. Evaluated here in double-double arithmetic, this differs from the library on 5 of
/// 440,000 sampled angles, in the last bit, where the unit's own rounding is not quite correct.</remarks>
internal static class ExtendedSine
{
    private const double HalfPi = 1.5707963267948966;

    /// <summary>What a 66-bit pi over two adds to <see cref="HalfPi"/>.</summary>
    private const double HalfPiTail = 6.123031769111886e-17;

    private const double QuarterPi = 0.78539816339744828;
    private const int Terms = 14;

    private static readonly Pair[] SineTerms = Factorials(1);
    private static readonly Pair[] CosineTerms = Factorials(0);

    /// <summary>An unevaluated sum of two doubles with about 106 bits of precision.</summary>
    private readonly record struct Pair(double High, double Low)
    {
        public static Pair Sum(double a, double b)
        {
            var sum = a + b;
            var other = sum - a;
            return new(sum, a - (sum - other) + (b - other));
        }

        private static Pair Quick(double a, double b)
        {
            var sum = a + b;
            return new(sum, b - (sum - a));
        }

        public static Pair operator +(Pair a, Pair b)
        {
            var first = Sum(a.High, b.High);
            var second = Sum(a.Low, b.Low);
            var carried = Quick(first.High, first.Low + second.High);
            return Quick(carried.High, carried.Low + second.Low);
        }

        public static Pair operator *(Pair a, Pair b)
        {
            var high = a.High * b.High;
            var low = Math.FusedMultiplyAdd(a.High, b.High, -high);
            return Quick(high, low + a.High * b.Low + a.Low * b.High);
        }

        public static Pair operator /(Pair a, double b)
        {
            var first = a.High / b;
            var product = first * b;
            var low = Math.FusedMultiplyAdd(first, b, -product);
            var remainder = a.High - product - low + a.Low;
            return Quick(first, remainder / b);
        }

        public static Pair operator -(Pair a) => new(-a.High, -a.Low);
    }

    /// <summary>One over the odd or even factorials with alternating signs, for a Taylor series.</summary>
    private static Pair[] Factorials(int first)
    {
        var terms = new Pair[Terms];
        var value = new Pair(1.0, 0.0);
        for (var index = 0; index < Terms; index++)
        {
            terms[index] = index % 2 == 0 ? value : -value;
            value = value / (first + 2 * index + 1) / (first + 2 * index + 2);
        }
        return terms;
    }

    private static Pair Series(Pair[] terms, Pair square)
    {
        var total = terms[Terms - 1];
        for (var index = Terms - 2; index >= 0; index--) total = total * square + terms[index];
        return total;
    }

    /// <summary>Rounds to a 64-bit mantissa and then to a double.</summary>
    /// <remarks>Plain rounding differs only where the first step lands exactly on a midpoint between two doubles, and the
    /// second step then ties to the even one.</remarks>
    private static double Store(Pair value)
    {
        var (high, low) = value;
        if (low == 0.0) return high;
        var size = Math.Abs(high);
        var outward = low > 0.0 == high > 0.0;
        var spacing = outward ? Math.BitIncrement(size) - size : size - Math.BitDecrement(size);
        if (Math.Round(Math.Abs(low) / spacing * 2048.0, MidpointRounding.ToEven) != 1024.0) return high;
        if ((BitConverter.DoubleToInt64Bits(high) & 1) == 0) return high;
        return low > 0.0 ? Math.BitIncrement(high) : Math.BitDecrement(high);
    }

    /// <summary>The sine and cosine of an angle in radians.</summary>
    public static (double Sine, double Cosine) Of(double angle)
    {
        if (angle == 0.0) return (angle, 1.0);
        var quarter = Math.Abs(angle) <= QuarterPi ? 0.0 : Math.Round(angle / HalfPi);
        var reduced = new Pair(angle, 0.0);
        if (quarter != 0.0)
        {
            var high = quarter * HalfPi;
            var low = Math.FusedMultiplyAdd(quarter, HalfPi, -high);
            var first = Pair.Sum(angle, -high);
            reduced = Pair.Sum(first.High, first.Low - low - quarter * HalfPiTail);
        }
        var square = reduced * reduced;
        var sine = Series(SineTerms, square) * reduced;
        var cosine = Series(CosineTerms, square);
        return ((int)(((long)quarter % 4 + 4) % 4)) switch
        {
            0 => (Store(sine), Store(cosine)),
            1 => (Store(cosine), Store(-sine)),
            2 => (Store(-sine), Store(-cosine)),
            _ => (Store(-cosine), Store(sine))
        };
    }
}

/// <summary>Rotation conversions in the order and with the arithmetic of the rotation library the Python stages used.</summary>
/// <remarks>These are not interchangeable with <see cref="M4.ToQuaternion"/> or <see cref="Q4.ToQAngle"/>. Those pick another
/// branch for the same rotation, so a bone frame near a half turn lands on the other sign of pi and the animation file's text
/// changes. Euler angles here are extrinsic x, y, z, which is the SMD column order, in the range minus pi to pi.</remarks>
public static class RotationMath
{
    /// <summary>A unit quaternion from a stored one, refusing the zero quaternion.</summary>
    public static Q4 FromQuat(Q4 q)
    {
        var norm = Math.Sqrt(q.X * q.X + q.Y * q.Y + q.Z * q.Z + q.W * q.W);
        if (double.IsNaN(norm) || norm == 0.0) throw new InvalidDataException("Found zero norm quaternions in `quat`.");
        return new(q.X / norm, q.Y / norm, q.Z / norm, q.W / norm);
    }

    /// <summary>The rotation matrix of a unit quaternion.</summary>
    public static M3 AsMatrix(Q4 q)
    {
        double x = q.X, y = q.Y, z = q.Z, w = q.W;
        double x2 = x * x, y2 = y * y, z2 = z * z, w2 = w * w;
        double xy = x * y, zw = z * w, xz = x * z, yw = y * w, yz = y * z, xw = x * w;
        return new(x2 - y2 - z2 + w2, 2 * (xy - zw), 2 * (xz + yw), 2 * (xy + zw), -x2 + y2 - z2 + w2, 2 * (yz - xw), 2 * (xz - yw), 2 * (yz + xw), -x2 - y2 + z2 + w2);
    }

    /// <summary>The closest orthonormal matrix, for input that is further from a rotation than rounding explains.</summary>
    private static M3 Orthogonalized(in M3 matrix)
    {
        var current = matrix;
        for (var iteration = 0; iteration < 64; iteration++)
        {
            var d = current.Determinant;
            var inverseTranspose = new M3(
                (current.M11 * current.M22 - current.M12 * current.M21) / d, (current.M12 * current.M20 - current.M10 * current.M22) / d, (current.M10 * current.M21 - current.M11 * current.M20) / d,
                (current.M02 * current.M21 - current.M01 * current.M22) / d, (current.M00 * current.M22 - current.M02 * current.M20) / d, (current.M01 * current.M20 - current.M00 * current.M21) / d,
                (current.M01 * current.M12 - current.M02 * current.M11) / d, (current.M02 * current.M10 - current.M00 * current.M12) / d, (current.M00 * current.M11 - current.M01 * current.M10) / d);
            var next = new M3(
                (current.M00 + inverseTranspose.M00) * 0.5, (current.M01 + inverseTranspose.M01) * 0.5, (current.M02 + inverseTranspose.M02) * 0.5,
                (current.M10 + inverseTranspose.M10) * 0.5, (current.M11 + inverseTranspose.M11) * 0.5, (current.M12 + inverseTranspose.M12) * 0.5,
                (current.M20 + inverseTranspose.M20) * 0.5, (current.M21 + inverseTranspose.M21) * 0.5, (current.M22 + inverseTranspose.M22) * 0.5);
            var done = next == current;
            current = next;
            if (done) break;
        }
        return current;
    }

    private static bool NearlyOrthogonal(in M3 matrix)
    {
        var product = matrix * matrix.Transposed();
        return Close(product.M00, 1.0) && Close(product.M11, 1.0) && Close(product.M22, 1.0)
            && Close(product.M01, 0.0) && Close(product.M02, 0.0) && Close(product.M10, 0.0)
            && Close(product.M12, 0.0) && Close(product.M20, 0.0) && Close(product.M21, 0.0);
    }

    private static bool Close(double value, double expected) => Math.Abs(value - expected) <= 1e-12 + 1e-5 * Math.Abs(expected);

    /// <summary>The unit quaternion of a rotation matrix, branching on the largest of the diagonal and the trace.</summary>
    public static Q4 FromMatrix(in M3 source)
    {
        if (source.Determinant <= 0) throw new InvalidDataException($"Non-positive determinant (left-handed or null coordinate frame) in rotation matrix 0: {source}.");
        var m = NearlyOrthogonal(source) ? source : Orthogonalized(source);
        var trace = m.M00 + m.M11 + m.M22;
        var choice = 0;
        var largest = m.M00;
        if (m.M11 > largest)
        {
            choice = 1;
            largest = m.M11;
        }
        if (m.M22 > largest)
        {
            choice = 2;
            largest = m.M22;
        }
        if (trace > largest) choice = 3;
        Q4 raw = choice switch
        {
            0 => new(1 - trace + 2 * m.M00, m.M10 + m.M01, m.M20 + m.M02, m.M21 - m.M12),
            1 => new(m.M10 + m.M01, 1 - trace + 2 * m.M11, m.M21 + m.M12, m.M02 - m.M20),
            2 => new(m.M20 + m.M02, m.M21 + m.M12, 1 - trace + 2 * m.M22, m.M10 - m.M01),
            _ => new(m.M21 - m.M12, m.M02 - m.M20, m.M10 - m.M01, 1 + trace)
        };
        return FromQuat(raw);
    }

    private static Q4 Compose(Q4 p, Q4 q)
    {
        double cx = p.Y * q.Z - p.Z * q.Y, cy = p.Z * q.X - p.X * q.Z, cz = p.X * q.Y - p.Y * q.X;
        return new(p.W * q.X + q.W * p.X + cx, p.W * q.Y + q.W * p.Y + cy, p.W * q.Z + q.W * p.Z + cz, p.W * q.W - p.X * q.X - p.Y * q.Y - p.Z * q.Z);
    }

    /// <summary>The quaternion of extrinsic x, y, z angles in radians.</summary>
    public static Q4 FromEuler(double x, double y, double z)
    {
        var (sine, cosine) = ExtendedSine.Of(x / 2.0);
        var quaternion = new Q4(sine, 0.0, 0.0, cosine);
        (sine, cosine) = ExtendedSine.Of(y / 2.0);
        quaternion = Compose(new(0.0, sine, 0.0, cosine), quaternion);
        (sine, cosine) = ExtendedSine.Of(z / 2.0);
        return Compose(new(0.0, 0.0, sine, cosine), quaternion);
    }

    /// <summary>The extrinsic x, y, z angles of a unit quaternion in radians, from minus pi to pi.</summary>
    /// <remarks>Gimbal lock zeroes the third angle. A rotation this close to one has no unique answer, and the library warns
    /// and carries on rather than refusing it.</remarks>
    public static V3 AsEuler(Q4 q)
    {
        double a = q.W - q.Y, b = q.X + q.Z, c = q.Y + q.W, d = q.Z - q.X;
        var middle = 2 * Math.Atan2(double.Hypot(c, d), double.Hypot(a, b));
        var singular = Math.Abs(middle) <= 1e-7 ? 1 : Math.Abs(middle - Math.PI) <= 1e-7 ? 2 : 0;
        double halfSum = Math.Atan2(b, a), halfDifference = Math.Atan2(d, c);
        double first, third;
        if (singular == 0)
        {
            first = halfSum - halfDifference;
            third = halfSum + halfDifference;
        }
        else
        {
            third = 0.0;
            first = singular == 1 ? 2 * halfSum : 2 * halfDifference * -1;
        }
        return new(Wrapped(first), Wrapped(middle - Math.PI / 2), Wrapped(third));
    }

    private static double Wrapped(double angle) => angle < -Math.PI ? angle + 2 * Math.PI : angle > Math.PI ? angle - 2 * Math.PI : angle;

    /// <summary>The rotation angle of a unit quaternion in radians.</summary>
    public static double Magnitude(Q4 q) => 2 * Math.Atan2(Math.Sqrt(q.X * q.X + q.Y * q.Y + q.Z * q.Z), Math.Abs(q.W));

    /// <summary>The rotation matrix of a stored quaternion.</summary>
    public static M3 MatrixOf(Q4 q) => AsMatrix(FromQuat(q));

    /// <summary>The rotation matrix of extrinsic x, y, z angles in radians.</summary>
    public static M3 MatrixOf(double x, double y, double z) => AsMatrix(FromEuler(x, y, z));
}
