namespace PortingKit.Addons.Porting;

/// <summary>The 8-bit RGBA scaler the VTF writer used for its mip chain: Mitchell down, Catmull-Rom up, in gamma space.</summary>
/// <remarks>
/// Vertical runs before horizontal, and horizontal keeps separate running totals for even and odd coefficients, because those are
/// the original's order of single-precision additions and any other order rounds a few pixels differently.
/// Alpha-aware in one specific way that shapes every Phong mask's mips. Each pixel is filtered both plain and premultiplied.
/// Where the filtered alpha is above a vanishing threshold the colour comes from the premultiplied result, otherwise from the
/// plain one, so fully transparent regions keep their colour while a pixel touching any opaque neighbour takes only that
/// neighbour's colour. A resize that ignores alpha, or one that only premultiplies, gives visibly different normal map mips.
/// </remarks>
public static class MipFilter
{
    private const int Working = 7;
    private const float Inverse255 = 3.9215689e-03f;
    private const float Small = 1f / (1 << 20) / (1 << 20) / (1 << 20) / (1 << 20) / (1 << 20) / (1 << 20);

    private enum Kernel { Mitchell, CatmullRom }

    private sealed record Axis(int[] First, int[] Count, float[] Weights, int Stride);

    /// <summary>Scales tightly packed RGBA pixels, using at most <paramref name="threads"/> workers.</summary>
    public static byte[] Resize(byte[] rgba, int inWidth, int inHeight, int outWidth, int outHeight, int threads = 4)
    {
        if (inWidth == outWidth && inHeight == outHeight) return (byte[])rgba.Clone();
        var horizontal = Build(inWidth, outWidth);
        var vertical = Build(inHeight, outHeight);
        var options = new ParallelOptions { MaxDegreeOfParallelism = Math.Max(1, threads) };
        var output = new byte[outWidth * outHeight * 4];
        var rowLength = inWidth * Working;
        Parallel.For(0, outHeight, options, () => new Worker(vertical.Stride, rowLength, outWidth), (y, _, worker) =>
        {
            var gathered = worker.Gathered;
            var line = worker.Line;
            var first = vertical.First[y];
            var count = vertical.Count[y];
            var weights = y * vertical.Stride;
            Array.Clear(gathered);
            for (var k = 0; k < count; k++)
            {
                var weight = vertical.Weights[weights + k];
                var source = worker.Row(rgba, first + k, inWidth);
                for (var i = 0; i < rowLength; i++) gathered[i] += weight * source[i];
            }
            for (var x = 0; x < outWidth; x++)
            {
                var start = horizontal.First[x];
                var length = horizontal.Count[x];
                var coefficients = x * horizontal.Stride;
                var target = x * Working;
                for (var c = 0; c < Working; c++)
                {
                    float even = 0f, odd = 0f;
                    for (var k = 0; k < length; k++)
                    {
                        var term = horizontal.Weights[coefficients + k] * gathered[(start + k) * Working + c];
                        if ((k & 1) == 0) even += term;
                        else odd += term;
                    }
                    line[target + c] = even + odd;
                }
            }
            var pixel = y * outWidth * 4;
            for (var x = 0; x < outWidth; x++)
            {
                var at = x * Working;
                var alpha = line[at + 3];
                if (alpha < Small)
                {
                    output[pixel + x * 4] = Encode(line[at]);
                    output[pixel + x * 4 + 1] = Encode(line[at + 1]);
                    output[pixel + x * 4 + 2] = Encode(line[at + 2]);
                }
                else
                {
                    var inverse = 1f / alpha;
                    output[pixel + x * 4] = Encode(line[at + 4] * inverse);
                    output[pixel + x * 4 + 1] = Encode(line[at + 5] * inverse);
                    output[pixel + x * 4 + 2] = Encode(line[at + 6] * inverse);
                }
                output[pixel + x * 4 + 3] = Encode(alpha);
            }
            return worker;
        }, _ => { });
        return output;
    }

    /// <summary>One thread's buffers: the few decoded source rows its current output row needs, which keeps a 4K level out of memory as floats.</summary>
    private sealed class Worker(int rows, int rowLength, int outWidth)
    {
        private readonly float[][] decoded = Enumerable.Range(0, rows).Select(_ => new float[rowLength]).ToArray();
        private readonly int[] held = Enumerable.Repeat(-1, rows).ToArray();
        public float[] Gathered { get; } = new float[rowLength];
        public float[] Line { get; } = new float[outWidth * Working];

        public float[] Row(byte[] rgba, int y, int width)
        {
            var slot = y % held.Length;
            var row = decoded[slot];
            if (held[slot] == y) return row;
            held[slot] = y;
            var input = y * width * 4;
            for (var x = 0; x < width; x++)
            {
                var from = input + x * 4;
                var to = x * Working;
                var alpha = rgba[from + 3] * Inverse255;
                row[to] = rgba[from] * Inverse255;
                row[to + 1] = rgba[from + 1] * Inverse255;
                row[to + 2] = rgba[from + 2] * Inverse255;
                row[to + 3] = alpha;
                row[to + 4] = row[to] * alpha;
                row[to + 5] = row[to + 1] * alpha;
                row[to + 6] = row[to + 2] * alpha;
            }
            return row;
        }
    }

    private static byte Encode(float value)
    {
        var scaled = 0.5f + 255f * value;
        return scaled <= 0f ? (byte)0 : scaled >= 255f ? (byte)255 : (byte)(int)scaled;
    }

    private static float Evaluate(Kernel kernel, float x)
    {
        if (x < 0f) x = -x;
        if (kernel == Kernel.Mitchell)
        {
            if (x >= 2f) return 0f;
            return x >= 1f ? (32f + x * (-60f + x * (36f - 7f * x))) / 18f : (16f + x * x * (-36f + 21f * x)) / 18f;
        }
        if (x >= 2f) return 0f;
        return x >= 1f ? (4f + x * (-8f + x * (5f - x))) / 2f : (2f + x * x * (-5f + 3f * x)) / 2f;
    }

    /// <summary>Precomputes one axis' contributors, normalising before folding off-edge weight onto the nearest pixel.</summary>
    private static Axis Build(int inSize, int outSize)
    {
        var scale = (float)((double)outSize / inSize);
        if (inSize == outSize) return Identity(outSize);
        var inverse = (float)((double)inSize / outSize);
        var kernel = scale >= 1f ? Kernel.CatmullRom : Kernel.Mitchell;
        var entries = new List<(int Pixel, float Weight)>[outSize];
        for (var i = 0; i < outSize; i++) entries[i] = [];
        if (scale >= 1f)
        {
            var radius = 2f * scale;
            for (var n = 0; n < outSize; n++)
            {
                var center = n + 0.5f;
                var inCenter = center * inverse;
                var first = (int)MathF.Floor((center - radius) * inverse + 0.5f);
                var last = (int)MathF.Floor((center + radius) * inverse - 0.5f);
                if (last < first) last = first;
                for (var i = first; i <= last; i++)
                {
                    var coefficient = Evaluate(kernel, inCenter - (i + 0.5f));
                    if (coefficient is < Small and > -Small) coefficient = 0f;
                    entries[n].Add((i, coefficient));
                }
            }
        }
        else
        {
            var radius = 2f * inverse;
            var width = (int)MathF.Ceiling(2f * 2f / scale);
            var margin = width / 2;
            for (var pixel = -margin; pixel < inSize + margin; pixel++)
            {
                var inCenter = pixel + 0.5f;
                var outCenter = inCenter * scale;
                var first = (int)MathF.Floor((inCenter - radius) * scale + 0.5f);
                var last = (int)MathF.Floor((inCenter + radius) * scale - 0.5f);
                if (first < 0) first = 0;
                if (last >= outSize) last = outSize - 1;
                for (var n = first; n <= last; n++)
                {
                    var coefficient = Evaluate(kernel, n + 0.5f - outCenter) * scale;
                    if (coefficient is < Small and > -Small) coefficient = 0f;
                    entries[n].Add((pixel, coefficient));
                }
            }
        }
        var folded = new List<(int Pixel, float Weight)>[outSize];
        var stride = 0;
        for (var n = 0; n < outSize; n++)
        {
            var total = 0f;
            foreach (var entry in entries[n]) total += entry.Weight;
            var rescale = total is < 1f - Small or > 1f + Small ? 1f / total : 1f;
            var merged = new SortedDictionary<int, float>();
            foreach (var (pixel, weight) in entries[n])
            {
                var place = Math.Clamp(pixel, 0, inSize - 1);
                merged[place] = merged.GetValueOrDefault(place) + weight * rescale;
            }
            folded[n] = merged.Select(item => (item.Key, item.Value)).ToList();
            stride = Math.Max(stride, folded[n].Count);
        }
        var firsts = new int[outSize];
        var counts = new int[outSize];
        var weights = new float[outSize * stride];
        for (var n = 0; n < outSize; n++)
        {
            firsts[n] = folded[n][0].Pixel;
            counts[n] = folded[n][^1].Pixel - firsts[n] + 1;
            if (counts[n] > stride)
            {
                Array.Resize(ref weights, outSize * counts[n]);
                stride = counts[n];
            }
        }
        weights = new float[outSize * stride];
        for (var n = 0; n < outSize; n++)
            foreach (var (pixel, weight) in folded[n])
                weights[n * stride + pixel - firsts[n]] = weight;
        return new(firsts, counts, weights, stride);
    }

    private static Axis Identity(int size)
    {
        var firsts = new int[size];
        var counts = new int[size];
        var weights = new float[size];
        for (var i = 0; i < size; i++)
        {
            firsts[i] = i;
            counts[i] = 1;
            weights[i] = 1f;
        }
        return new(firsts, counts, weights, 1);
    }
}
