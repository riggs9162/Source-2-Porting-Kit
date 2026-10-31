namespace PortingKit.Addons.Porting;

/// <summary>Block compression for VTF colour data, matching the output of the toolchain v3 used bit for bit.</summary>
public static class Dxt
{
    private const int Pixels = 16;
    private const int MaxThreads = 4;

    public static int BlockBytes(VtfFormat format) => format is VtfFormat.DXT1 or VtfFormat.DXT1_ONE_BIT_ALPHA ? 8 : 16;

    /// <summary>The stored size of one image, counted in whole four by four blocks.</summary>
    public static int StoredSize(VtfFormat format, int width, int height) => (width + 3) / 4 * ((height + 3) / 4) * BlockBytes(format);

    /// <summary>Compresses tightly packed RGBA pixels. An image that is not a whole number of blocks is first extended by repeating its edge pixels.</summary>
    public static byte[] Compress(byte[] rgba, int width, int height, VtfFormat format, int threads = MaxThreads)
    {
        var (pixels, paddedWidth, paddedHeight) = Pad(rgba, width, height);
        var blockBytes = BlockBytes(format);
        int across = paddedWidth / 4, down = paddedHeight / 4;
        var output = new byte[across * down * blockBytes];
        var options = new ParallelOptions { MaxDegreeOfParallelism = Math.Clamp(threads, 1, MaxThreads) };
        Parallel.For(0, down, options, () => new BlockEncoder(), (row, _, encoder) =>
        {
            for (var column = 0; column < across; column++)
                Encode(encoder, pixels, paddedWidth * 4, row, column, output.AsSpan((row * across + column) * blockBytes, blockBytes), format);
            return encoder;
        }, _ => { });
        return output;
    }

    /// <summary>Compresses only if every block decodes back to exactly these pixels, otherwise returns null as soon as one does not.</summary>
    /// <remarks>Most rich textures fail within their first blocks, so stopping there avoids encoding the rest of a chain that will be thrown away.</remarks>
    public static byte[]? CompressExact(byte[] rgba, int width, int height, VtfFormat format, int threads = MaxThreads)
    {
        var (pixels, paddedWidth, paddedHeight) = Pad(rgba, width, height);
        var blockBytes = BlockBytes(format);
        int across = paddedWidth / 4, down = paddedHeight / 4;
        var output = new byte[across * down * blockBytes];
        var options = new ParallelOptions { MaxDegreeOfParallelism = Math.Clamp(threads, 1, MaxThreads) };
        var loop = Parallel.For(0, down, options, () => new BlockEncoder(), (row, state, encoder) =>
        {
            Span<byte> decoded = stackalloc byte[64];
            for (var column = 0; column < across && !state.IsStopped; column++)
            {
                var target = output.AsSpan((row * across + column) * blockBytes, blockBytes);
                Encode(encoder, pixels, paddedWidth * 4, row, column, target, format);
                DecodeBlock(target, decoded, format);
                if (Matches(decoded, rgba, width, height, row, column)) continue;
                state.Stop();
                break;
            }
            return encoder;
        }, _ => { });
        return loop.IsCompleted ? output : null;
    }

    private static bool Matches(ReadOnlySpan<byte> decoded, byte[] rgba, int width, int height, int row, int column)
    {
        for (var y = 0; y < 4 && row * 4 + y < height; y++)
            for (var x = 0; x < 4 && column * 4 + x < width; x++)
                if (!decoded.Slice((y * 4 + x) * 4, 4).SequenceEqual(rgba.AsSpan(((row * 4 + y) * width + column * 4 + x) * 4, 4))) return false;
        return true;
    }

    private static void Encode(BlockEncoder encoder, byte[] pixels, int stride, int row, int column, Span<byte> target, VtfFormat format)
    {
        encoder.Load(pixels, stride, column * 4, row * 4);
        switch (format)
        {
            case VtfFormat.DXT1: encoder.Colour(target, false); break;
            case VtfFormat.DXT1_ONE_BIT_ALPHA: encoder.Colour(target, true); break;
            case VtfFormat.DXT3: encoder.Dxt3(target); break;
            default: encoder.Dxt5(target); break;
        }
    }

    private static (byte[] Pixels, int Width, int Height) Pad(byte[] rgba, int width, int height)
    {
        int paddedWidth = (width + 3) & ~3, paddedHeight = (height + 3) & ~3;
        if (paddedWidth == width && paddedHeight == height) return (rgba, width, height);
        var pixels = new byte[paddedWidth * paddedHeight * 4];
        for (var y = 0; y < paddedHeight; y++)
            for (var x = 0; x < paddedWidth; x++)
                Array.Copy(rgba, (Math.Min(y, height - 1) * width + Math.Min(x, width - 1)) * 4, pixels, (y * paddedWidth + x) * 4, 4);
        return (pixels, paddedWidth, paddedHeight);
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
                DecodeBlock(data.Slice((row * across + column) * blockBytes, blockBytes), block, format);
                for (var y = 0; y < 4 && row * 4 + y < height; y++)
                    for (var x = 0; x < 4 && column * 4 + x < width; x++)
                        block.Slice((y * 4 + x) * 4, 4).CopyTo(output.AsSpan(((row * 4 + y) * width + column * 4 + x) * 4));
            }
        return output;
    }

    private static void DecodeBlock(ReadOnlySpan<byte> source, Span<byte> block, VtfFormat format)
    {
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
}
