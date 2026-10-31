namespace PortingKit.Addons.Porting;

/// <summary>A row-major 3x3 block, the rotation of a bone frame without its translation.</summary>
public readonly record struct M3(double M00, double M01, double M02, double M10, double M11, double M12, double M20, double M21, double M22)
{
    public static readonly M3 Identity = new(1, 0, 0, 0, 1, 0, 0, 0, 1);

    public V3 Column0 => new(M00, M10, M20);

    /// <summary>The upper-left block of a transform.</summary>
    public static M3 Of(M4 matrix) => new(matrix[0, 0], matrix[0, 1], matrix[0, 2], matrix[1, 0], matrix[1, 1], matrix[1, 2], matrix[2, 0], matrix[2, 1], matrix[2, 2]);

    /// <summary>The matrix product, one fused multiply-add per term in ascending order, as <see cref="M4"/> sums it.</summary>
    public static M3 operator *(in M3 a, in M3 b) => new(
        Sum(a.M00, b.M00, a.M01, b.M10, a.M02, b.M20), Sum(a.M00, b.M01, a.M01, b.M11, a.M02, b.M21), Sum(a.M00, b.M02, a.M01, b.M12, a.M02, b.M22),
        Sum(a.M10, b.M00, a.M11, b.M10, a.M12, b.M20), Sum(a.M10, b.M01, a.M11, b.M11, a.M12, b.M21), Sum(a.M10, b.M02, a.M11, b.M12, a.M12, b.M22),
        Sum(a.M20, b.M00, a.M21, b.M10, a.M22, b.M20), Sum(a.M20, b.M01, a.M21, b.M11, a.M22, b.M21), Sum(a.M20, b.M02, a.M21, b.M12, a.M22, b.M22));

    private static double Sum(double a0, double b0, double a1, double b1, double a2, double b2) =>
        Math.FusedMultiplyAdd(a2, b2, Math.FusedMultiplyAdd(a1, b1, Math.FusedMultiplyAdd(a0, b0, 0.0)));

    /// <summary>The matrix and vector product.</summary>
    public V3 Apply(V3 vector) => new(Sum(M00, vector.X, M01, vector.Y, M02, vector.Z), Sum(M10, vector.X, M11, vector.Y, M12, vector.Z), Sum(M20, vector.X, M21, vector.Y, M22, vector.Z));

    public M3 Transposed() => new(M00, M10, M20, M01, M11, M21, M02, M12, M22);

    public double Determinant => M00 * (M11 * M22 - M12 * M21) - M01 * (M10 * M22 - M12 * M20) + M02 * (M10 * M21 - M11 * M20);

    /// <summary>A transform with this rotation at a position.</summary>
    public M4 At(V3 position) => new([M00, M01, M02, position.X, M10, M11, M12, position.Y, M20, M21, M22, position.Z, 0, 0, 0, 1]);

    /// <summary>The same rotation as a transform with no translation.</summary>
    public M4 ToM4() => At(V3.Zero);
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
        var quaternion = new Q4(Math.Sin(x / 2.0), 0.0, 0.0, Math.Cos(x / 2.0));
        quaternion = Compose(new(0.0, Math.Sin(y / 2.0), 0.0, Math.Cos(y / 2.0)), quaternion);
        return Compose(new(0.0, 0.0, Math.Sin(z / 2.0), Math.Cos(z / 2.0)), quaternion);
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
