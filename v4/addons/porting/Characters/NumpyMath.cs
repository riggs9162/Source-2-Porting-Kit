namespace PortingKit.Addons.Porting;

/// <summary>Eigenvalue arithmetic behind the rotation and condition numbers the runtime configuration stores.</summary>
/// <remarks>numpy's eigensolver and SVD leave rounding noise in their last bits that this code does not reproduce, so a
/// value computed here matches v3 to within about 1e-15, not bit for bit. Products and inverses, which can be made
/// exact, live on <see cref="M4"/>.</remarks>
internal static class NumpyMath
{
    /// <summary>The eigenvalues and column eigenvectors of a symmetric matrix, by cyclic Jacobi rotations.</summary>
    public static (double[] Values, double[,] Vectors) SymmetricEigen(double[,] matrix)
    {
        var n = matrix.GetLength(0);
        var a = (double[,])matrix.Clone();
        var vectors = new double[n, n];
        for (var i = 0; i < n; i++) vectors[i, i] = 1.0;
        for (var sweep = 0; sweep < 100; sweep++)
        {
            var off = 0.0;
            for (var p = 0; p < n; p++)
                for (var q = p + 1; q < n; q++)
                    off += a[p, q] * a[p, q];
            if (off == 0.0) break;
            for (var p = 0; p < n; p++)
                for (var q = p + 1; q < n; q++)
                {
                    if (a[p, q] == 0.0) continue;
                    var theta = (a[q, q] - a[p, p]) / (2.0 * a[p, q]);
                    var t = (theta >= 0 ? 1.0 : -1.0) / (Math.Abs(theta) + Math.Sqrt(theta * theta + 1.0));
                    var c = 1.0 / Math.Sqrt(t * t + 1.0);
                    var s = t * c;
                    for (var k = 0; k < n; k++)
                    {
                        double akp = a[k, p], akq = a[k, q];
                        a[k, p] = c * akp - s * akq;
                        a[k, q] = s * akp + c * akq;
                    }
                    for (var k = 0; k < n; k++)
                    {
                        double apk = a[p, k], aqk = a[q, k];
                        a[p, k] = c * apk - s * aqk;
                        a[q, k] = s * apk + c * aqk;
                    }
                    for (var k = 0; k < n; k++)
                    {
                        double vkp = vectors[k, p], vkq = vectors[k, q];
                        vectors[k, p] = c * vkp - s * vkq;
                        vectors[k, q] = s * vkp + c * vkq;
                    }
                }
        }
        var values = new double[n];
        for (var i = 0; i < n; i++) values[i] = a[i, i];
        return (values, vectors);
    }

    /// <summary>The 2-norm condition number of a symmetric matrix, whose singular values are its absolute eigenvalues.</summary>
    public static double SymmetricCondition(double[,] matrix)
    {
        var values = SymmetricEigen(matrix).Values.Select(Math.Abs).ToArray();
        return values.Max() / values.Min();
    }

    /// <summary>The unit quaternion, x y z w, of a rigid transform, as <c>trimesh.transformations.quaternion_from_matrix</c> finds it.</summary>
    /// <remarks>That routine takes the dominant eigenvector of a symmetric 4x4 matrix rather than reading the diagonal, so a
    /// slightly non-orthonormal input gives the best-fit rotation, and w is non-negative. numpy's eigensolver leaves its
    /// own rounding noise in the last bits, which this one does not reproduce.</remarks>
    public static Q4 QuaternionFromMatrix(M4 m)
    {
        double m00 = m[0, 0], m01 = m[0, 1], m02 = m[0, 2];
        double m10 = m[1, 0], m11 = m[1, 1], m12 = m[1, 2];
        double m20 = m[2, 0], m21 = m[2, 1], m22 = m[2, 2];
        double[,] k =
        {
            { m00 - m11 - m22, m01 + m10, m02 + m20, m21 - m12 },
            { m01 + m10, m11 - m00 - m22, m12 + m21, m02 - m20 },
            { m02 + m20, m12 + m21, m22 - m00 - m11, m10 - m01 },
            { m21 - m12, m02 - m20, m10 - m01, m00 + m11 + m22 }
        };
        for (var row = 0; row < 4; row++)
            for (var column = 0; column < 4; column++)
                k[row, column] /= 3.0;
        var (values, vectors) = SymmetricEigen(k);
        var best = 0;
        for (var index = 1; index < 4; index++)
            if (values[index] > values[best]) best = index;
        var q = new Q4(vectors[0, best], vectors[1, best], vectors[2, best], vectors[3, best]);
        return q.W < 0.0 ? -q : q;
    }

    /// <summary>The static XYZ Euler angles of a rotation in radians, as <c>euler_from_matrix(m, "sxyz")</c> returns them.</summary>
    public static V3 EulerXyz(M4 m)
    {
        const double epsilon = 8.881784197001252e-16;
        var cy = Math.Sqrt(m[0, 0] * m[0, 0] + m[1, 0] * m[1, 0]);
        return cy > epsilon
            ? new(Math.Atan2(m[2, 1], m[2, 2]), Math.Atan2(-m[2, 0], cy), Math.Atan2(m[1, 0], m[0, 0]))
            : new(Math.Atan2(-m[1, 2], m[1, 1]), Math.Atan2(-m[2, 0], cy), 0.0);
    }
}
