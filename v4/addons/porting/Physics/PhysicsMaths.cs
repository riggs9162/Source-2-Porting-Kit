using System.Globalization;

namespace PortingKit.Addons.Porting;

/// <summary>The small linear algebra and number formatting the physics stages need.</summary>
public static class PhysicsMaths
{
    /// <summary>The upper 3x3 of a transform, with no translation.</summary>
    public static M4 RotationOf(M4 matrix) => new([
        matrix[0, 0], matrix[0, 1], matrix[0, 2], 0,
        matrix[1, 0], matrix[1, 1], matrix[1, 2], 0,
        matrix[2, 0], matrix[2, 1], matrix[2, 2], 0,
        0, 0, 0, 1]);

    /// <summary>The first three rows of a transform as a flat row-major list.</summary>
    public static double[] Rows(M4 matrix) => [
        matrix[0, 0], matrix[0, 1], matrix[0, 2], matrix[0, 3],
        matrix[1, 0], matrix[1, 1], matrix[1, 2], matrix[1, 3],
        matrix[2, 0], matrix[2, 1], matrix[2, 2], matrix[2, 3]];

    public static bool IsFinite(M4 matrix)
    {
        for (var row = 0; row < 4; row++)
            for (var column = 0; column < 4; column++)
                if (!double.IsFinite(matrix[row, column])) return false;
        return true;
    }

    /// <summary>Singular value decomposition of a 3x3 by one-sided Jacobi rotations, singular values descending.</summary>
    /// <remarks>One-sided Jacobi works on the matrix itself rather than its square, so small singular values keep
    /// their accuracy. A zero singular value gets a left vector completed to an orthonormal frame.</remarks>
    public static (double[,] Left, double[] Values, double[,] Right) Svd(double[,] input)
    {
        var work = (double[,])input.Clone();
        var right = new double[,] { { 1, 0, 0 }, { 0, 1, 0 }, { 0, 0, 1 } };
        for (var sweep = 0; sweep < 64; sweep++)
        {
            var rotated = false;
            for (var first = 0; first < 2; first++)
                for (var second = first + 1; second < 3; second++)
                {
                    double alpha = 0, beta = 0, gamma = 0;
                    for (var row = 0; row < 3; row++)
                    {
                        alpha += work[row, first] * work[row, first];
                        beta += work[row, second] * work[row, second];
                        gamma += work[row, first] * work[row, second];
                    }
                    if (gamma == 0 || Math.Abs(gamma) <= 1e-17 * Math.Sqrt(alpha * beta)) continue;
                    rotated = true;
                    var zeta = (beta - alpha) / (2.0 * gamma);
                    var tangent = (zeta >= 0 ? 1.0 : -1.0) / (Math.Abs(zeta) + Math.Sqrt(1.0 + zeta * zeta));
                    var cosine = 1.0 / Math.Sqrt(1.0 + tangent * tangent);
                    var sine = cosine * tangent;
                    for (var row = 0; row < 3; row++)
                    {
                        (work[row, first], work[row, second]) = (cosine * work[row, first] - sine * work[row, second], sine * work[row, first] + cosine * work[row, second]);
                        (right[row, first], right[row, second]) = (cosine * right[row, first] - sine * right[row, second], sine * right[row, first] + cosine * right[row, second]);
                    }
                }
            if (!rotated) break;
        }
        var norms = new double[3];
        for (var column = 0; column < 3; column++)
            norms[column] = Math.Sqrt(work[0, column] * work[0, column] + work[1, column] * work[1, column] + work[2, column] * work[2, column]);
        int[] order = [0, 1, 2];
        Array.Sort(order, (a, b) => norms[b].CompareTo(norms[a]));
        var left = new double[3, 3];
        var sortedRight = new double[3, 3];
        var values = new double[3];
        var limit = norms[order[0]] * 1e-300;
        for (var column = 0; column < 3; column++)
        {
            var source = order[column];
            values[column] = norms[source];
            for (var row = 0; row < 3; row++)
            {
                sortedRight[row, column] = right[row, source];
                left[row, column] = norms[source] > limit ? work[row, source] / norms[source] : 0.0;
            }
        }
        if (values[2] <= limit)
        {
            var completed = new V3(left[0, 0], left[1, 0], left[2, 0]).Cross(new(left[0, 1], left[1, 1], left[2, 1]));
            for (var row = 0; row < 3; row++) left[row, 2] = completed[row];
        }
        return (left, values, sortedRight);
    }

    /// <summary>The rotation closest to a nearly orthonormal 3x3, which cleans rounding out of authored bind poses.</summary>
    public static double[,] NearestRotation(double[,] rotation)
    {
        var (left, _, right) = Svd(rotation);
        var result = new double[3, 3];
        for (var row = 0; row < 3; row++)
            for (var column = 0; column < 3; column++)
                for (var k = 0; k < 3; k++)
                    result[row, column] += left[row, k] * right[column, k];
        return result;
    }

    /// <summary>The rigid transform best carrying one point list onto another, by the Kabsch construction.</summary>
    public static (double[,] Rotation, V3 Translation, double Determinant) FitRigid(IReadOnlyList<V3> source, IReadOnlyList<V3> target)
    {
        var sourceCentre = V3.Zero;
        var targetCentre = V3.Zero;
        for (var index = 0; index < source.Count; index++)
        {
            sourceCentre += source[index];
            targetCentre += target[index];
        }
        sourceCentre /= source.Count;
        targetCentre /= source.Count;
        var covariance = new double[3, 3];
        for (var index = 0; index < source.Count; index++)
        {
            V3 from = source[index] - sourceCentre, to = target[index] - targetCentre;
            for (var row = 0; row < 3; row++)
                for (var column = 0; column < 3; column++)
                    covariance[row, column] += from[row] * to[column];
        }
        var (left, _, right) = Svd(covariance);
        var rotation = Compose(right, left);
        if (Determinant(rotation) < 0)
        {
            for (var row = 0; row < 3; row++) right[row, 2] = -right[row, 2];
            rotation = Compose(right, left);
        }
        return (rotation, targetCentre - Apply(rotation, sourceCentre), Determinant(rotation));
    }

    private static double[,] Compose(double[,] right, double[,] left)
    {
        var result = new double[3, 3];
        for (var row = 0; row < 3; row++)
            for (var column = 0; column < 3; column++)
                for (var k = 0; k < 3; k++)
                    result[row, column] += right[row, k] * left[column, k];
        return result;
    }

    public static V3 Apply(double[,] rotation, V3 point) => new(
        rotation[0, 0] * point.X + rotation[0, 1] * point.Y + rotation[0, 2] * point.Z,
        rotation[1, 0] * point.X + rotation[1, 1] * point.Y + rotation[1, 2] * point.Z,
        rotation[2, 0] * point.X + rotation[2, 1] * point.Y + rotation[2, 2] * point.Z);

    public static double Determinant(double[,] m) =>
        m[0, 0] * (m[1, 1] * m[2, 2] - m[1, 2] * m[2, 1]) - m[0, 1] * (m[1, 0] * m[2, 2] - m[1, 2] * m[2, 0]) + m[0, 2] * (m[1, 0] * m[2, 1] - m[1, 1] * m[2, 0]);

    /// <summary>The cheapest one-to-one assignment of rows to columns of a square cost matrix.</summary>
    /// <remarks>The shortest augmenting path solver SciPy uses, including its reverse column order and its preference
    /// for a fresh sink on ties, so equal costs resolve to the same assignment. Returns the column for each row.</remarks>
    public static int[] Assign(double[,] cost)
    {
        var size = cost.GetLength(0);
        var u = new double[size];
        var v = new double[size];
        var path = new int[size];
        var columnOfRow = new int[size];
        var rowOfColumn = new int[size];
        Array.Fill(path, -1);
        Array.Fill(columnOfRow, -1);
        Array.Fill(rowOfColumn, -1);
        var shortest = new double[size];
        var seenRows = new bool[size];
        var seenColumns = new bool[size];
        var remaining = new int[size];
        for (var current = 0; current < size; current++)
        {
            var minimum = 0.0;
            var count = size;
            for (var at = 0; at < size; at++) remaining[at] = size - at - 1;
            Array.Fill(seenRows, false);
            Array.Fill(seenColumns, false);
            Array.Fill(shortest, double.PositiveInfinity);
            var sink = -1;
            var row = current;
            while (sink == -1)
            {
                var index = -1;
                var lowest = double.PositiveInfinity;
                seenRows[row] = true;
                for (var at = 0; at < count; at++)
                {
                    var column = remaining[at];
                    var reduced = minimum + cost[row, column] - u[row] - v[column];
                    if (reduced < shortest[column])
                    {
                        path[column] = row;
                        shortest[column] = reduced;
                    }
                    if (shortest[column] < lowest || (shortest[column] == lowest && rowOfColumn[column] == -1))
                    {
                        lowest = shortest[column];
                        index = at;
                    }
                }
                minimum = lowest;
                if (double.IsPositiveInfinity(minimum)) throw new InvalidDataException("The assignment cost matrix is infeasible.");
                var reached = remaining[index];
                if (rowOfColumn[reached] == -1) sink = reached;
                else row = rowOfColumn[reached];
                seenColumns[reached] = true;
                remaining[index] = remaining[--count];
            }
            u[current] += minimum;
            for (var other = 0; other < size; other++)
                if (seenRows[other] && other != current) u[other] += minimum - shortest[columnOfRow[other]];
            for (var column = 0; column < size; column++)
                if (seenColumns[column]) v[column] -= minimum - shortest[column];
            var cursor = sink;
            while (true)
            {
                var owner = path[cursor];
                rowOfColumn[cursor] = owner;
                (columnOfRow[owner], cursor) = (cursor, columnOfRow[owner]);
                if (owner == current) break;
            }
        }
        return columnOfRow;
    }

    /// <summary>Significant-digit notation without trailing zeros, as in <c>%.Ng</c>.</summary>
    public static string General(double value, int precision)
    {
        if (double.IsNaN(value)) return "nan";
        if (double.IsInfinity(value)) return value > 0 ? "inf" : "-inf";
        if (value == 0) return double.IsNegative(value) ? "-0" : "0";
        var scientific = value.ToString("E" + (precision - 1), CultureInfo.InvariantCulture);
        var at = scientific.IndexOf('E');
        var exponent = int.Parse(scientific[(at + 1)..], CultureInfo.InvariantCulture);
        if (exponent < -4 || exponent >= precision)
        {
            var mantissa = scientific[..at];
            if (mantissa.Contains('.')) mantissa = mantissa.TrimEnd('0').TrimEnd('.');
            return mantissa + "e" + (exponent < 0 ? "-" : "+") + Math.Abs(exponent).ToString("00", CultureInfo.InvariantCulture);
        }
        var text = Fmt.Fixed(value, precision - 1 - exponent);
        return text.Contains('.') ? text.TrimEnd('0').TrimEnd('.') : text;
    }

    /// <summary>A fraction as a percentage, as in <c>%.3%</c>.</summary>
    public static string Percent(double fraction, int decimals) => Fmt.Fixed(fraction * 100.0, decimals) + "%";
}
