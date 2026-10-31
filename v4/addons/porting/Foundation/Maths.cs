namespace PortingKit.Addons.Porting;

/// <summary>A double-precision texture coordinate.</summary>
public readonly record struct V2(double X, double Y);

/// <summary>A double-precision vector. Bone chains accumulate error too quickly for single precision.</summary>
public readonly record struct V3(double X, double Y, double Z)
{
    public static readonly V3 Zero = new(0, 0, 0);
    public static readonly V3 One = new(1, 1, 1);

    public double this[int axis] => axis switch
    {
        0 => X, 1 => Y, 2 => Z,
        _ => throw new ArgumentOutOfRangeException(nameof(axis))
    };

    public static V3 operator +(V3 a, V3 b) => new(a.X + b.X, a.Y + b.Y, a.Z + b.Z);
    public static V3 operator -(V3 a, V3 b) => new(a.X - b.X, a.Y - b.Y, a.Z - b.Z);
    public static V3 operator -(V3 a) => new(-a.X, -a.Y, -a.Z);
    public static V3 operator *(V3 a, double scale) => new(a.X * scale, a.Y * scale, a.Z * scale);
    public static V3 operator *(double scale, V3 a) => a * scale;
    public static V3 operator /(V3 a, double scale) => new(a.X / scale, a.Y / scale, a.Z / scale);

    public double Dot(V3 other) => X * other.X + Y * other.Y + Z * other.Z;
    public V3 Cross(V3 other) => new(Y * other.Z - Z * other.Y, Z * other.X - X * other.Z, X * other.Y - Y * other.X);
    public double Length => Math.Sqrt(Dot(this));
    public V3 Normalized() => this / Math.Max(Length, 1e-12);
    public bool IsFinite => double.IsFinite(X) && double.IsFinite(Y) && double.IsFinite(Z);

    /// <summary>Rounds through single precision, matching values read from a glTF float stream.</summary>
    public V3 Single() => new((float)X, (float)Y, (float)Z);
}

/// <summary>A quaternion stored as glTF stores it: x, y, z, w.</summary>
public readonly record struct Q4(double X, double Y, double Z, double W)
{
    public static readonly Q4 Identity = new(0, 0, 0, 1);

    /// <summary>The Hamilton product, applying <paramref name="b"/> first.</summary>
    public static Q4 operator *(Q4 a, Q4 b) => new(
        a.W * b.X + a.X * b.W + a.Y * b.Z - a.Z * b.Y,
        a.W * b.Y - a.X * b.Z + a.Y * b.W + a.Z * b.X,
        a.W * b.Z + a.X * b.Y - a.Y * b.X + a.Z * b.W,
        a.W * b.W - a.X * b.X - a.Y * b.Y - a.Z * b.Z);

    public static Q4 operator -(Q4 a) => new(-a.X, -a.Y, -a.Z, -a.W);
    public double Dot(Q4 other) => X * other.X + Y * other.Y + Z * other.Z + W * other.W;
    public double Length => Math.Sqrt(Dot(this));

    public Q4 Normalized()
    {
        var length = Math.Max(Length, 1e-12);
        return new(X / length, Y / length, Z / length, W / length);
    }

    /// <summary>Rounds through single precision, matching values read from a glTF float stream.</summary>
    public Q4 Single() => new((float)X, (float)Y, (float)Z, (float)W);

    public V3 Rotate(V3 vector)
    {
        var axis = new V3(X, Y, Z);
        var first = axis.Cross(vector);
        var second = axis.Cross(first);
        return vector + first * (2.0 * W) + second * 2.0;
    }

    /// <summary>Converts to Source pitch, yaw and roll in degrees, retaining yaw at Euler singularities.</summary>
    public (double Pitch, double Yaw, double Roll) ToQAngle()
    {
        var length = Length;
        if (length < 1e-12) throw new InvalidDataException("Cannot convert a zero quaternion.");
        double x = X / length, y = Y / length, z = Z / length, w = W / length;
        var r00 = 1 - 2 * (y * y + z * z);
        var r10 = 2 * (x * y + w * z);
        var r20 = 2 * (x * z - w * y);
        var r21 = 2 * (y * z + w * x);
        var r22 = 1 - 2 * (x * x + y * y);
        var horizontal = Fmt.Hypot(r00, r10);
        var pitch = Math.Atan2(-r20, horizontal);
        double yaw, roll;
        if (horizontal > 1e-8)
        {
            yaw = Math.Atan2(r10, r00);
            roll = Math.Atan2(r21, r22);
        }
        else
        {
            yaw = Math.Atan2(-(2 * (x * y - w * z)), 1 - 2 * (x * x + z * z));
            roll = 0.0;
        }
        return (Fmt.Degrees(pitch), Fmt.Degrees(yaw), Fmt.Degrees(roll));
    }
}

/// <summary>A row-major affine or projective transform.</summary>
public sealed class M4(double[] rowMajor)
{
    private readonly double[] cells = rowMajor.Length == 16 ? rowMajor : throw new ArgumentException("A matrix needs 16 cells.", nameof(rowMajor));

    public static M4 Identity => new([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]);
    public double this[int row, int column] => cells[row * 4 + column];
    public V3 Translation => new(cells[3], cells[7], cells[11]);

    /// <summary>The matrix product, summed with one fused multiply-add per term in ascending order.</summary>
    /// <remarks>That is numpy's <c>@</c> on this size, and it decides the sign of a value that should be zero. A bone with no
    /// real rotation lands on 0 or just under 2 pi in an SMD, and on 0 or -0 in a QC, according to that sign.</remarks>
    public static M4 operator *(M4 a, M4 b)
    {
        var result = new double[16];
        for (var row = 0; row < 4; row++)
            for (var column = 0; column < 4; column++)
            {
                var sum = 0.0;
                for (var k = 0; k < 4; k++) sum = Math.FusedMultiplyAdd(a[row, k], b[k, column], sum);
                result[row * 4 + column] = sum;
            }
        return new(result);
    }

    public V3 Point(V3 p) => new(
        cells[0] * p.X + cells[1] * p.Y + cells[2] * p.Z + cells[3],
        cells[4] * p.X + cells[5] * p.Y + cells[6] * p.Z + cells[7],
        cells[8] * p.X + cells[9] * p.Y + cells[10] * p.Z + cells[11]);

    public V3 Direction(V3 d) => new(
        cells[0] * d.X + cells[1] * d.Y + cells[2] * d.Z,
        cells[4] * d.X + cells[5] * d.Y + cells[6] * d.Z,
        cells[8] * d.X + cells[9] * d.Y + cells[10] * d.Z);

    public M4 Transposed()
    {
        var result = new double[16];
        for (var row = 0; row < 4; row++)
            for (var column = 0; column < 4; column++)
                result[column * 4 + row] = this[row, column];
        return new(result);
    }

    /// <summary>Builds the rotation matrix, or the identity for a near-zero quaternion.</summary>
    public static M4 FromQuaternion(Q4 q)
    {
        var n = q.Dot(q);
        if (n < 1e-12) return Identity;
        var s = 2.0 / n;
        double xs = q.X * s, ys = q.Y * s, zs = q.Z * s;
        return new([
            1.0 - (q.Y * ys + q.Z * zs), q.X * ys - q.W * zs, q.X * zs + q.W * ys, 0,
            q.X * ys + q.W * zs, 1.0 - (q.X * xs + q.Z * zs), q.Y * zs - q.W * xs, 0,
            q.X * zs - q.W * ys, q.Y * zs + q.W * xs, 1.0 - (q.X * xs + q.Y * ys), 0,
            0, 0, 0, 1]);
    }

    public static M4 Trs(V3 translation, Q4 rotation)
    {
        var matrix = FromQuaternion(rotation).cells;
        var result = (double[])matrix.Clone();
        result[3] = translation.X;
        result[7] = translation.Y;
        result[11] = translation.Z;
        return new(result);
    }

    /// <summary>Extracts the rotation with w kept non-negative, branching on the largest diagonal term.</summary>
    public Q4 ToQuaternion()
    {
        double m00 = this[0, 0], m01 = this[0, 1], m02 = this[0, 2];
        double m10 = this[1, 0], m11 = this[1, 1], m12 = this[1, 2];
        double m20 = this[2, 0], m21 = this[2, 1], m22 = this[2, 2];
        var trace = m00 + m11 + m22;
        Q4 q;
        if (trace > 0.0)
        {
            var s = 0.5 / Math.Sqrt(trace + 1.0);
            q = new((m21 - m12) * s, (m02 - m20) * s, (m10 - m01) * s, 0.25 / s);
        }
        else if (m00 > m11 && m00 > m22)
        {
            var s = 2.0 * Math.Sqrt(Math.Max(1e-12, 1.0 + m00 - m11 - m22));
            q = new(0.25 * s, (m01 + m10) / s, (m02 + m20) / s, (m21 - m12) / s);
        }
        else if (m11 > m22)
        {
            var s = 2.0 * Math.Sqrt(Math.Max(1e-12, 1.0 + m11 - m00 - m22));
            q = new((m01 + m10) / s, 0.25 * s, (m12 + m21) / s, (m02 - m20) / s);
        }
        else
        {
            var s = 2.0 * Math.Sqrt(Math.Max(1e-12, 1.0 + m22 - m00 - m11));
            q = new((m02 + m20) / s, (m12 + m21) / s, 0.25 * s, (m10 - m01) / s);
        }
        q = q.Normalized();
        return q.W < 0.0 ? -q : q;
    }

    /// <summary>The inverse <c>np.linalg.inv</c> returns.</summary>
    public M4 Inverse()
    {
        var a = new double[4, 4];
        for (var row = 0; row < 4; row++)
            for (var column = 0; column < 4; column++)
                a[row, column] = this[row, column];
        var inverse = Invert(a);
        var result = new double[16];
        for (var row = 0; row < 4; row++)
            for (var column = 0; column < 4; column++)
                result[row * 4 + column] = inverse[row, column];
        return new(result);
    }

    /// <summary>Inverts a square matrix the way numpy's bundled OpenBLAS does, or fails on an exactly zero pivot.</summary>
    /// <remarks>A left-looking LU with partial pivoting and sum-first updates, then right-looking substitution against
    /// reciprocal diagonals, all fused. Up to 4x4 this matches numpy to the last bit; above that numpy's blocked kernels
    /// order the same arithmetic differently, so a larger result is accurate rather than identical.</remarks>
    public static double[,] Invert(double[,] matrix)
    {
        var n = matrix.GetLength(0);
        var a = (double[,])matrix.Clone();
        var pivots = new int[n];
        var column = new double[n];
        for (var j = 0; j < n; j++)
        {
            for (var row = 0; row < n; row++) column[row] = a[row, j];
            for (var i = 0; i < j; i++)
                if (pivots[i] != i) (column[i], column[pivots[i]]) = (column[pivots[i]], column[i]);
            for (var i = 1; i < j; i++)
            {
                var sum = 0.0;
                for (var k = 0; k < i; k++) sum = Math.FusedMultiplyAdd(column[k], a[i, k], sum);
                column[i] -= sum;
            }
            for (var row = j; row < n; row++)
            {
                var sum = 0.0;
                for (var k = 0; k < j; k++) sum = Math.FusedMultiplyAdd(a[row, k], column[k], sum);
                column[row] -= sum;
            }
            var pivot = j;
            for (var row = j + 1; row < n; row++)
                if (Math.Abs(column[row]) > Math.Abs(column[pivot])) pivot = row;
            pivots[j] = pivot;
            for (var row = 0; row < n; row++) a[row, j] = column[row];
            if (a[pivot, j] == 0.0) throw new InvalidDataException("Matrix is singular.");
            if (pivot != j)
                for (var k = 0; k <= j; k++) (a[j, k], a[pivot, k]) = (a[pivot, k], a[j, k]);
            var reciprocal = 1.0 / a[j, j];
            for (var row = j + 1; row < n; row++) a[row, j] *= reciprocal;
        }
        var x = new double[n, n];
        for (var i = 0; i < n; i++) x[i, i] = 1.0;
        for (var i = 0; i < n; i++)
            if (pivots[i] != i)
                for (var k = 0; k < n; k++) (x[i, k], x[pivots[i], k]) = (x[pivots[i], k], x[i, k]);
        for (var c = 0; c < n; c++)
            for (var i = 0; i < n; i++)
            {
                var value = x[i, c];
                for (var k = i + 1; k < n; k++) x[k, c] = Math.FusedMultiplyAdd(-value, a[k, i], x[k, c]);
            }
        for (var c = 0; c < n; c++)
            for (var i = n - 1; i >= 0; i--)
            {
                var value = x[i, c] * (1.0 / a[i, i]);
                x[i, c] = value;
                for (var k = 0; k < i; k++) x[k, c] = Math.FusedMultiplyAdd(-value, a[k, i], x[k, c]);
            }
        return x;
    }
}
