namespace PortingKit.Addons.Porting;

/// <summary>Block compression for VTF colour data: a cluster-fit DXT encoder and a decoder matching the VTF toolchain's.</summary>
public static class Dxt
{
    private const int Pixels = 16;
    private const int MaxThreads = 4;
    private static readonly (byte Low, byte High)[] Single5 = BuildSingle(31, Expand5);
    private static readonly (byte Low, byte High)[] Single6 = BuildSingle(63, Expand6);

    public static int BlockBytes(VtfFormat format) => format is VtfFormat.DXT1 or VtfFormat.DXT1_ONE_BIT_ALPHA ? 8 : 16;

    /// <summary>The stored size of one image, counted in whole four by four blocks.</summary>
    public static int StoredSize(VtfFormat format, int width, int height) => (width + 3) / 4 * ((height + 3) / 4) * BlockBytes(format);

    /// <summary>Compresses tightly packed RGBA pixels. Pixels past the edge of a partial block repeat the image.</summary>
    public static byte[] Compress(byte[] rgba, int width, int height, VtfFormat format, int threads = MaxThreads)
    {
        var blockBytes = BlockBytes(format);
        var across = (width + 3) / 4;
        var down = (height + 3) / 4;
        var output = new byte[across * down * blockBytes];
        var options = new ParallelOptions { MaxDegreeOfParallelism = Math.Clamp(threads, 1, MaxThreads) };
        Parallel.For(0, down, options, () => new Fitter(), (row, _, fitter) =>
        {
            for (var column = 0; column < across; column++)
            {
                fitter.Load(rgba, width, height, column * 4, row * 4);
                var target = output.AsSpan((row * across + column) * blockBytes, blockBytes);
                switch (format)
                {
                    case VtfFormat.DXT1:
                        fitter.Colour(target, false);
                        break;
                    case VtfFormat.DXT1_ONE_BIT_ALPHA:
                        fitter.Colour(target, true);
                        break;
                    case VtfFormat.DXT3:
                        fitter.SharpAlpha(target);
                        fitter.Colour(target[8..], false);
                        break;
                    default:
                        fitter.SmoothAlpha(target);
                        fitter.Colour(target[8..], false);
                        break;
                }
            }
            return fitter;
        }, _ => { });
        return output;
    }

    /// <summary>Decompresses to tightly packed RGBA at exactly <paramref name="width"/> by <paramref name="height"/>.</summary>
    public static byte[] Decompress(ReadOnlySpan<byte> data, int width, int height, VtfFormat format)
    {
        var blockBytes = BlockBytes(format);
        var across = (width + 3) / 4;
        var down = (height + 3) / 4;
        var output = new byte[width * height * 4];
        Span<byte> block = stackalloc byte[64];
        for (var row = 0; row < down; row++)
            for (var column = 0; column < across; column++)
            {
                var source = data.Slice((row * across + column) * blockBytes, blockBytes);
                switch (format)
                {
                    case VtfFormat.DXT1:
                    case VtfFormat.DXT1_ONE_BIT_ALPHA:
                        DecodeColour(source, block, format == VtfFormat.DXT1_ONE_BIT_ALPHA ? 1 : 0);
                        break;
                    case VtfFormat.DXT3:
                        DecodeColour(source[8..], block, 2);
                        DecodeSharpAlpha(source, block);
                        break;
                    default:
                        DecodeColour(source[8..], block, 2);
                        DecodeSmoothAlpha(source, block);
                        break;
                }
                for (var y = 0; y < 4 && row * 4 + y < height; y++)
                    for (var x = 0; x < 4 && column * 4 + x < width; x++)
                        block.Slice((y * 4 + x) * 4, 4).CopyTo(output.AsSpan(((row * 4 + y) * width + column * 4 + x) * 4));
            }
        return output;
    }

    /// <summary>Fills four palette entries from two 565 endpoints, using the decoder's own integer rounding.</summary>
    /// <remarks>Endpoints expand by bit replication and interpolate in 8 bits, rounding down.</remarks>
    private static void Palette(int c0, int c1, Span<int> red, Span<int> green, Span<int> blue, bool opaque)
    {
        int r0 = Expand5((c0 >> 11) & 31), g0 = Expand6((c0 >> 5) & 63), b0 = Expand5(c0 & 31);
        int r1 = Expand5((c1 >> 11) & 31), g1 = Expand6((c1 >> 5) & 63), b1 = Expand5(c1 & 31);
        red[0] = r0;
        green[0] = g0;
        blue[0] = b0;
        red[1] = r1;
        green[1] = g1;
        blue[1] = b1;
        if (c0 > c1 || opaque)
        {
            red[2] = (2 * r0 + r1) / 3;
            green[2] = (2 * g0 + g1) / 3;
            blue[2] = (2 * b0 + b1) / 3;
            red[3] = (r0 + 2 * r1) / 3;
            green[3] = (g0 + 2 * g1) / 3;
            blue[3] = (b0 + 2 * b1) / 3;
        }
        else
        {
            red[2] = (r0 + r1) / 2;
            green[2] = (g0 + g1) / 2;
            blue[2] = (b0 + b1) / 2;
            red[3] = green[3] = blue[3] = 0;
        }
    }

    private static int Expand5(int value) => value << 3 | value >> 2;
    private static int Expand6(int value) => value << 2 | value >> 4;

    private static void DecodeColour(ReadOnlySpan<byte> source, Span<byte> block, int mode)
    {
        int c0 = source[0] | source[1] << 8, c1 = source[2] | source[3] << 8;
        Span<int> red = stackalloc int[4], green = stackalloc int[4], blue = stackalloc int[4];
        Palette(c0, c1, red, green, blue, mode == 2);
        var indices = (uint)(source[4] | source[5] << 8 | source[6] << 16 | source[7] << 24);
        var cutout = mode != 2 && c0 <= c1;
        for (var i = 0; i < Pixels; i++, indices >>= 2)
        {
            var index = (int)(indices & 3);
            block[i * 4] = (byte)red[index];
            block[i * 4 + 1] = (byte)green[index];
            block[i * 4 + 2] = (byte)blue[index];
            block[i * 4 + 3] = cutout && index == 3 ? (byte)0 : (byte)255;
        }
    }

    private static void DecodeSharpAlpha(ReadOnlySpan<byte> source, Span<byte> block)
    {
        for (var i = 0; i < Pixels; i++)
        {
            var nibble = (source[i / 2] >> (i % 2 * 4)) & 15;
            block[i * 4 + 3] = (byte)(nibble * 17);
        }
    }

    private static void DecodeSmoothAlpha(ReadOnlySpan<byte> source, Span<byte> block)
    {
        Span<int> alpha = stackalloc int[8];
        AlphaRamp(source[0], source[1], alpha);
        var bits = 0UL;
        for (var i = 0; i < 6; i++) bits |= (ulong)source[2 + i] << (8 * i);
        for (var i = 0; i < Pixels; i++, bits >>= 3) block[i * 4 + 3] = (byte)alpha[(int)(bits & 7)];
    }

    private static void AlphaRamp(int a0, int a1, Span<int> alpha)
    {
        alpha[0] = a0;
        alpha[1] = a1;
        if (a0 > a1)
        {
            for (var i = 1; i < 7; i++) alpha[i + 1] = ((7 - i) * a0 + i * a1 + 3) / 7;
            return;
        }
        for (var i = 1; i < 5; i++) alpha[i + 1] = ((5 - i) * a0 + i * a1 + 2) / 5;
        alpha[6] = 0;
        alpha[7] = 255;
    }

    /// <summary>Finds, for each 8-bit value, the 565 endpoint pair that reproduces it, preferring an exact endpoint over an interpolant.</summary>
    private static (byte Low, byte High)[] BuildSingle(int limit, Func<int, int> expand)
    {
        var table = new (byte, byte)[256];
        for (var value = 0; value < 256; value++)
        {
            var best = int.MaxValue;
            for (var low = 0; low <= limit; low++)
                for (var high = 0; high <= limit; high++)
                {
                    var interpolated = (2 * expand(low) + expand(high)) / 3;
                    var error = Math.Abs(interpolated - value) * 256 + Math.Abs(low - high);
                    if (error >= best) continue;
                    best = error;
                    table[value] = ((byte)low, (byte)high);
                }
            for (var exact = 0; exact <= limit; exact++)
                if (expand(exact) == value) table[value] = ((byte)exact, (byte)exact);
        }
        return table;
    }

    /// <summary>Holds one worker's scratch space, so a block fit never allocates.</summary>
    private sealed class Fitter
    {
        private readonly byte[] pixels = new byte[Pixels * 4];
        private readonly float[] x = new float[Pixels], y = new float[Pixels], z = new float[Pixels];
        private readonly float[] sortedX = new float[Pixels], sortedY = new float[Pixels], sortedZ = new float[Pixels];
        private readonly float[] dots = new float[Pixels];
        private readonly int[] order = new int[Pixels];
        private readonly int[] paletteRed = new int[4], paletteGreen = new int[4], paletteBlue = new int[4];
        private int count;

        public void Load(byte[] rgba, int width, int height, int left, int top)
        {
            for (var row = 0; row < 4; row++)
                for (var column = 0; column < 4; column++)
                {
                    var py = top + row;
                    var px = left + column;
                    if (py >= height) py = top + (height - top == 0 ? 0 : row % (height - top));
                    if (px >= width) px = left + (width - left == 0 ? 0 : column % (width - left));
                    var from = (py * width + px) * 4;
                    var to = (row * 4 + column) * 4;
                    pixels[to] = rgba[from];
                    pixels[to + 1] = rgba[from + 1];
                    pixels[to + 2] = rgba[from + 2];
                    pixels[to + 3] = rgba[from + 3];
                }
        }

        public void SharpAlpha(Span<byte> target)
        {
            for (var i = 0; i < Pixels; i += 2)
            {
                var low = (pixels[i * 4 + 3] + 8) / 17;
                var high = (pixels[(i + 1) * 4 + 3] + 8) / 17;
                target[i / 2] = (byte)(low | high << 4);
            }
        }

        /// <summary>Fits the interpolated alpha block, comparing the eight-step ramp against the six-step ramp with literal 0 and 255.</summary>
        public void SmoothAlpha(Span<byte> target)
        {
            int low = 255, high = 0, lowInner = 255, highInner = 0;
            for (var i = 0; i < Pixels; i++)
            {
                int alpha = pixels[i * 4 + 3];
                if (alpha < low) low = alpha;
                if (alpha > high) high = alpha;
                if (alpha != 0 && alpha < lowInner) lowInner = alpha;
                if (alpha != 255 && alpha > highInner) highInner = alpha;
            }
            if (low == high)
            {
                target[0] = (byte)high;
                target[1] = (byte)low;
                target[2..8].Clear();
                return;
            }
            Span<int> ramp = stackalloc int[8];
            var bestError = long.MaxValue;
            int bestA0 = high, bestA1 = low;
            for (var a0 = Math.Max(low, high - 2); a0 <= Math.Min(255, high + 2); a0++)
                for (var a1 = Math.Max(0, low - 2); a1 <= Math.Min(low + 2, a0 - 1); a1++)
                    AlphaTrial(a0, a1, ramp, ref bestError, ref bestA0, ref bestA1);
            if (lowInner <= highInner && (low == 0 || high == 255))
                for (var a1 = Math.Max(0, highInner - 2); a1 <= Math.Min(255, highInner + 2); a1++)
                    for (var a0 = Math.Max(0, lowInner - 2); a0 <= Math.Min(a1, lowInner + 2); a0++)
                        AlphaTrial(a0, a1, ramp, ref bestError, ref bestA0, ref bestA1);
            AlphaRamp(bestA0, bestA1, ramp);
            var bits = 0UL;
            for (var i = 0; i < Pixels; i++)
            {
                int alpha = pixels[i * 4 + 3];
                var best = int.MaxValue;
                var index = 0;
                for (var candidate = 0; candidate < 8; candidate++)
                {
                    var distance = Math.Abs(ramp[candidate] - alpha);
                    if (distance >= best) continue;
                    best = distance;
                    index = candidate;
                }
                bits |= (ulong)index << (i * 3);
            }
            target[0] = (byte)bestA0;
            target[1] = (byte)bestA1;
            for (var i = 0; i < 6; i++) target[2 + i] = (byte)(bits >> (8 * i));
        }

        private void AlphaTrial(int a0, int a1, Span<int> ramp, ref long bestError, ref int bestA0, ref int bestA1)
        {
            AlphaRamp(a0, a1, ramp);
            long error = 0;
            for (var i = 0; i < Pixels; i++)
            {
                int alpha = pixels[i * 4 + 3];
                var best = int.MaxValue;
                for (var candidate = 0; candidate < 8; candidate++)
                {
                    var distance = ramp[candidate] - alpha;
                    distance *= distance;
                    if (distance < best) best = distance;
                }
                error += best;
            }
            if (error >= bestError) return;
            bestError = error;
            bestA0 = a0;
            bestA1 = a1;
        }

        /// <summary>Fits the colour block. With <paramref name="punchThrough"/>, blocks holding transparent pixels use the three-colour mode.</summary>
        public void Colour(Span<byte> target, bool punchThrough)
        {
            count = 0;
            var transparent = false;
            for (var i = 0; i < Pixels; i++)
            {
                if (punchThrough && pixels[i * 4 + 3] < 128)
                {
                    transparent = true;
                    continue;
                }
                order[count++] = i;
            }
            if (count == 0)
            {
                target[0] = target[1] = target[2] = target[3] = 0;
                target[4] = target[5] = target[6] = target[7] = 0xFF;
                return;
            }
            var same = true;
            for (var i = 0; i < count; i++)
            {
                var at = order[i] * 4;
                x[i] = pixels[at] / 255f;
                y[i] = pixels[at + 1] / 255f;
                z[i] = pixels[at + 2] / 255f;
                same &= pixels[at] == pixels[order[0] * 4] && pixels[at + 1] == pixels[order[0] * 4 + 1] && pixels[at + 2] == pixels[order[0] * 4 + 2];
            }
            int c0, c1;
            if (same) SingleColour(order[0] * 4, out c0, out c1);
            else ClusterFit(transparent, out c0, out c1);
            Refine(ref c0, ref c1, transparent);
            Emit(target, c0, c1, transparent);
        }

        private void SingleColour(int at, out int c0, out int c1)
        {
            var (redLow, redHigh) = Single5[pixels[at]];
            var (greenLow, greenHigh) = Single6[pixels[at + 1]];
            var (blueLow, blueHigh) = Single5[pixels[at + 2]];
            c0 = redLow << 11 | greenLow << 5 | blueLow;
            c1 = redHigh << 11 | greenHigh << 5 | blueHigh;
        }

        private void PrincipalAxis(out float ax, out float ay, out float az)
        {
            float mx = 0, my = 0, mz = 0;
            for (var i = 0; i < count; i++)
            {
                mx += x[i];
                my += y[i];
                mz += z[i];
            }
            var inverse = 1f / count;
            mx *= inverse;
            my *= inverse;
            mz *= inverse;
            float xx = 0, xy = 0, xz = 0, yy = 0, yz = 0, zz = 0;
            for (var i = 0; i < count; i++)
            {
                float a = x[i] - mx, b = y[i] - my, c = z[i] - mz;
                xx += a * a;
                xy += a * b;
                xz += a * c;
                yy += b * b;
                yz += b * c;
                zz += c * c;
            }
            float vx = 1, vy = 1, vz = 1;
            for (var iteration = 0; iteration < 8; iteration++)
            {
                var nx = vx * xx + vy * xy + vz * xz;
                var ny = vx * xy + vy * yy + vz * yz;
                var nz = vx * xz + vy * yz + vz * zz;
                var length = MathF.Max(MathF.Abs(nx), MathF.Max(MathF.Abs(ny), MathF.Abs(nz)));
                if (length <= 0f) break;
                vx = nx / length;
                vy = ny / length;
                vz = nz / length;
            }
            ax = vx;
            ay = vy;
            az = vz;
        }

        private void Sort(float ax, float ay, float az)
        {
            for (var i = 0; i < count; i++)
            {
                var dot = x[i] * ax + y[i] * ay + z[i] * az;
                var j = i;
                while (j > 0 && dots[j - 1] > dot)
                {
                    dots[j] = dots[j - 1];
                    sortedX[j] = sortedX[j - 1];
                    sortedY[j] = sortedY[j - 1];
                    sortedZ[j] = sortedZ[j - 1];
                    j--;
                }
                dots[j] = dot;
                sortedX[j] = x[i];
                sortedY[j] = y[i];
                sortedZ[j] = z[i];
            }
        }

        private void ClusterFit(bool threeColour, out int c0, out int c1)
        {
            PrincipalAxis(out var ax, out var ay, out var az);
            var bestError = float.MaxValue;
            float startX = 0, startY = 0, startZ = 0, endX = 0, endY = 0, endZ = 0;
            for (var iteration = 0; iteration < 3; iteration++)
            {
                Sort(ax, ay, az);
                var improved = threeColour
                    ? Partition3(ref bestError, ref startX, ref startY, ref startZ, ref endX, ref endY, ref endZ)
                    : Partition4(ref bestError, ref startX, ref startY, ref startZ, ref endX, ref endY, ref endZ);
                if (!improved) break;
                float dx = endX - startX, dy = endY - startY, dz = endZ - startZ;
                var length = MathF.Max(MathF.Abs(dx), MathF.Max(MathF.Abs(dy), MathF.Abs(dz)));
                if (length <= 0f) break;
                ax = dx / length;
                ay = dy / length;
                az = dz / length;
            }
            c0 = Pack(startX, startY, startZ);
            c1 = Pack(endX, endY, endZ);
        }

        private static int Pack(float r, float g, float b) =>
            Math.Clamp((int)(r * 31f + 0.5f), 0, 31) << 11 | Math.Clamp((int)(g * 63f + 0.5f), 0, 63) << 5 | Math.Clamp((int)(b * 31f + 0.5f), 0, 31);

        private static float Snap(float value, float grid) => MathF.Floor(Math.Clamp(value, 0f, 1f) * grid + 0.5f) / grid;

        private bool Partition4(ref float bestError, ref float sx, ref float sy, ref float sz, ref float ex, ref float ey, ref float ez)
        {
            const float Third = 1f / 3f, TwoThirds = 2f / 3f, Ninth = 1f / 9f, FourNinths = 4f / 9f, TwoNinths = 2f / 9f;
            float totalX = 0, totalY = 0, totalZ = 0;
            for (var i = 0; i < count; i++)
            {
                totalX += sortedX[i];
                totalY += sortedY[i];
                totalZ += sortedZ[i];
            }
            var improved = false;
            float part0X = 0, part0Y = 0, part0Z = 0;
            for (var c0 = 0; c0 <= count; c0++)
            {
                float part1X = 0, part1Y = 0, part1Z = 0;
                for (var c1 = 0; c0 + c1 <= count; c1++)
                {
                    float part2X = 0, part2Y = 0, part2Z = 0;
                    for (var c2 = 0; c0 + c1 + c2 <= count; c2++)
                    {
                        var part3X = totalX - part0X - part1X - part2X;
                        var part3Y = totalY - part0Y - part1Y - part2Y;
                        var part3Z = totalZ - part0Z - part1Z - part2Z;
                        var c3 = count - c0 - c1 - c2;
                        var alpha2 = c0 + c1 * FourNinths + c2 * Ninth;
                        var beta2 = c3 + c2 * FourNinths + c1 * Ninth;
                        var alphaBeta = (c1 + c2) * TwoNinths;
                        var alphaX = part0X + part1X * TwoThirds + part2X * Third;
                        var alphaY = part0Y + part1Y * TwoThirds + part2Y * Third;
                        var alphaZ = part0Z + part1Z * TwoThirds + part2Z * Third;
                        var betaX = part3X + part2X * TwoThirds + part1X * Third;
                        var betaY = part3Y + part2Y * TwoThirds + part1Y * Third;
                        var betaZ = part3Z + part2Z * TwoThirds + part1Z * Third;
                        var denominator = alpha2 * beta2 - alphaBeta * alphaBeta;
                        if (denominator > 1e-6f)
                        {
                            var inverse = 1f / denominator;
                            var ax = Snap((alphaX * beta2 - betaX * alphaBeta) * inverse, 31f);
                            var ay = Snap((alphaY * beta2 - betaY * alphaBeta) * inverse, 63f);
                            var az = Snap((alphaZ * beta2 - betaZ * alphaBeta) * inverse, 31f);
                            var bx = Snap((betaX * alpha2 - alphaX * alphaBeta) * inverse, 31f);
                            var by = Snap((betaY * alpha2 - alphaY * alphaBeta) * inverse, 63f);
                            var bz = Snap((betaZ * alpha2 - alphaZ * alphaBeta) * inverse, 31f);
                            var error = ax * ax * alpha2 + bx * bx * beta2 + 2f * (ax * bx * alphaBeta - ax * alphaX - bx * betaX)
                                + ay * ay * alpha2 + by * by * beta2 + 2f * (ay * by * alphaBeta - ay * alphaY - by * betaY)
                                + az * az * alpha2 + bz * bz * beta2 + 2f * (az * bz * alphaBeta - az * alphaZ - bz * betaZ);
                            if (error < bestError - 1e-7f)
                            {
                                bestError = error;
                                sx = ax;
                                sy = ay;
                                sz = az;
                                ex = bx;
                                ey = by;
                                ez = bz;
                                improved = true;
                            }
                        }
                        var at = c0 + c1 + c2;
                        if (at == count) break;
                        part2X += sortedX[at];
                        part2Y += sortedY[at];
                        part2Z += sortedZ[at];
                    }
                    var after = c0 + c1;
                    if (after == count) break;
                    part1X += sortedX[after];
                    part1Y += sortedY[after];
                    part1Z += sortedZ[after];
                }
                if (c0 == count) break;
                part0X += sortedX[c0];
                part0Y += sortedY[c0];
                part0Z += sortedZ[c0];
            }
            return improved;
        }

        private bool Partition3(ref float bestError, ref float sx, ref float sy, ref float sz, ref float ex, ref float ey, ref float ez)
        {
            float totalX = 0, totalY = 0, totalZ = 0;
            for (var i = 0; i < count; i++)
            {
                totalX += sortedX[i];
                totalY += sortedY[i];
                totalZ += sortedZ[i];
            }
            var improved = false;
            float part0X = 0, part0Y = 0, part0Z = 0;
            for (var c0 = 0; c0 <= count; c0++)
            {
                float part1X = 0, part1Y = 0, part1Z = 0;
                for (var c1 = 0; c0 + c1 <= count; c1++)
                {
                    var part2X = totalX - part0X - part1X;
                    var part2Y = totalY - part0Y - part1Y;
                    var part2Z = totalZ - part0Z - part1Z;
                    var c2 = count - c0 - c1;
                    var alpha2 = c0 + c1 * 0.25f;
                    var beta2 = c2 + c1 * 0.25f;
                    var alphaBeta = c1 * 0.25f;
                    var alphaX = part0X + part1X * 0.5f;
                    var alphaY = part0Y + part1Y * 0.5f;
                    var alphaZ = part0Z + part1Z * 0.5f;
                    var betaX = part2X + part1X * 0.5f;
                    var betaY = part2Y + part1Y * 0.5f;
                    var betaZ = part2Z + part1Z * 0.5f;
                    var denominator = alpha2 * beta2 - alphaBeta * alphaBeta;
                    if (denominator > 1e-6f)
                    {
                        var inverse = 1f / denominator;
                        var ax = Snap((alphaX * beta2 - betaX * alphaBeta) * inverse, 31f);
                        var ay = Snap((alphaY * beta2 - betaY * alphaBeta) * inverse, 63f);
                        var az = Snap((alphaZ * beta2 - betaZ * alphaBeta) * inverse, 31f);
                        var bx = Snap((betaX * alpha2 - alphaX * alphaBeta) * inverse, 31f);
                        var by = Snap((betaY * alpha2 - alphaY * alphaBeta) * inverse, 63f);
                        var bz = Snap((betaZ * alpha2 - alphaZ * alphaBeta) * inverse, 31f);
                        var error = ax * ax * alpha2 + bx * bx * beta2 + 2f * (ax * bx * alphaBeta - ax * alphaX - bx * betaX)
                            + ay * ay * alpha2 + by * by * beta2 + 2f * (ay * by * alphaBeta - ay * alphaY - by * betaY)
                            + az * az * alpha2 + bz * bz * beta2 + 2f * (az * bz * alphaBeta - az * alphaZ - bz * betaZ);
                        if (error < bestError - 1e-7f)
                        {
                            bestError = error;
                            sx = ax;
                            sy = ay;
                            sz = az;
                            ex = bx;
                            ey = by;
                            ez = bz;
                            improved = true;
                        }
                    }
                    var after = c0 + c1;
                    if (after == count) break;
                    part1X += sortedX[after];
                    part1Y += sortedY[after];
                    part1Z += sortedZ[after];
                }
                if (c0 == count) break;
                part0X += sortedX[c0];
                part0Y += sortedY[c0];
                part0Z += sortedZ[c0];
            }
            return improved;
        }

        /// <summary>Scores endpoints with the decoder's real palette, so 565 rounding cannot hide behind the fit's continuous maths.</summary>
        private int Score(int c0, int c1, bool threeColour)
        {
            var low = threeColour ? Math.Min(c0, c1) : Math.Max(c0, c1);
            var high = threeColour ? Math.Max(c0, c1) : Math.Min(c0, c1);
            Palette(low, high, paletteRed, paletteGreen, paletteBlue, !threeColour);
            var entries = threeColour ? 3 : 4;
            var total = 0;
            for (var i = 0; i < count; i++)
            {
                var at = order[i] * 4;
                int r = pixels[at], g = pixels[at + 1], b = pixels[at + 2];
                var best = int.MaxValue;
                for (var entry = 0; entry < entries; entry++)
                {
                    int dr = paletteRed[entry] - r, dg = paletteGreen[entry] - g, db = paletteBlue[entry] - b;
                    var error = dr * dr + dg * dg + db * db;
                    if (error < best) best = error;
                }
                total += best;
            }
            return total;
        }

        private void Refine(ref int c0, ref int c1, bool threeColour)
        {
            Span<int> shifts = [11, 5, 0];
            Span<int> limits = [31, 63, 31];
            var best = Score(c0, c1, threeColour);
            for (var round = 0; round < 4 && best > 0; round++)
            {
                var changed = false;
                for (var endpoint = 0; endpoint < 2; endpoint++)
                    for (var channel = 0; channel < 3; channel++)
                        for (var step = -1; step <= 1; step += 2)
                        {
                            var current = endpoint == 0 ? c0 : c1;
                            var value = ((current >> shifts[channel]) & limits[channel]) + step;
                            if (value < 0 || value > limits[channel]) continue;
                            var candidate = (current & ~(limits[channel] << shifts[channel])) | value << shifts[channel];
                            var error = endpoint == 0 ? Score(candidate, c1, threeColour) : Score(c0, candidate, threeColour);
                            if (error >= best) continue;
                            best = error;
                            if (endpoint == 0) c0 = candidate;
                            else c1 = candidate;
                            changed = true;
                        }
                if (!changed) break;
            }
        }

        private void Emit(Span<byte> target, int c0, int c1, bool threeColour)
        {
            var first = threeColour ? Math.Min(c0, c1) : Math.Max(c0, c1);
            var second = threeColour ? Math.Max(c0, c1) : Math.Min(c0, c1);
            Palette(first, second, paletteRed, paletteGreen, paletteBlue, !threeColour);
            var entries = threeColour ? 3 : 4;
            var indices = 0u;
            for (var i = 0; i < Pixels; i++)
            {
                var at = i * 4;
                var index = 3;
                if (!threeColour || pixels[at + 3] >= 128)
                {
                    var best = int.MaxValue;
                    for (var entry = 0; entry < entries; entry++)
                    {
                        int dr = paletteRed[entry] - pixels[at], dg = paletteGreen[entry] - pixels[at + 1], db = paletteBlue[entry] - pixels[at + 2];
                        var error = dr * dr + dg * dg + db * db;
                        if (error >= best) continue;
                        best = error;
                        index = entry;
                    }
                }
                indices |= (uint)index << (i * 2);
            }
            target[0] = (byte)first;
            target[1] = (byte)(first >> 8);
            target[2] = (byte)second;
            target[3] = (byte)(second >> 8);
            target[4] = (byte)indices;
            target[5] = (byte)(indices >> 8);
            target[6] = (byte)(indices >> 16);
            target[7] = (byte)(indices >> 24);
        }
    }
}
