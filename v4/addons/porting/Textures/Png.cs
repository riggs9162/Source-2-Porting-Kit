using System.IO.Compression;
using System.Text;

namespace PortingKit.Addons.Porting;

/// <summary>Decodes and encodes PNG files without gamma or colour management, so stored samples reach the bakes unchanged.</summary>
public static class Png
{
    private static readonly byte[] Signature = [0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A];
    private static readonly uint[] CrcTable = BuildCrcTable();
    private static readonly int[] PassX = [0, 4, 0, 2, 0, 1, 0];
    private static readonly int[] PassY = [0, 0, 4, 0, 2, 0, 1];
    private static readonly int[] StepX = [8, 8, 4, 4, 2, 2, 1];
    private static readonly int[] StepY = [8, 8, 8, 4, 4, 2, 2];

    public static bool Matches(ReadOnlySpan<byte> file) => file.Length >= 8 && file[..8].SequenceEqual(Signature);

    /// <summary>Reads only the header, for reporting a map's size and bands without its pixels.</summary>
    public static SourceImage Probe(ReadOnlySpan<byte> file)
    {
        var header = ReadHeader(file);
        return SourceImage.Header(ModeFor(header), header.Width, header.Height);
    }

    /// <summary>Decodes every colour type and bit depth the way the kit's earlier Pillow pipeline saw them.</summary>
    /// <remarks>
    /// Pillow maps PNG storage onto its modes: 1-bit grey stays bilevel, 2 and 4-bit grey expand by 85 and 17,
    /// 16-bit grey stays 16-bit, and 16-bit colour keeps only the high byte, with grey-alpha promoted to RGBA.
    /// </remarks>
    public static SourceImage Decode(ReadOnlySpan<byte> file)
    {
        var header = ReadHeader(file);
        var compressed = new MemoryStream();
        byte[]? palette = null;
        byte[]? transparency = null;
        for (var position = 8; position + 12 <= file.Length;)
        {
            var length = ReadUInt32(file, position);
            if (length > file.Length - position - 12) throw new TextureError("PNG chunk runs past the end of the file.");
            var tag = file.Slice(position + 4, 4);
            var chunk = file.Slice(position + 8, (int)length);
            if (Crc(file.Slice(position + 4, (int)length + 4)) != ReadUInt32(file, position + 8 + (int)length))
                throw new TextureError($"PNG chunk {Encoding.ASCII.GetString(tag)} failed its checksum.");
            if (tag.SequenceEqual("IDAT"u8)) compressed.Write(chunk);
            else if (tag.SequenceEqual("PLTE"u8)) palette = chunk.ToArray();
            else if (tag.SequenceEqual("tRNS"u8)) transparency = chunk.ToArray();
            else if (tag.SequenceEqual("IEND"u8)) break;
            position += 12 + (int)length;
        }
        compressed.Position = 0;
        var raw = Inflate(compressed, ScanlineBytes(header));
        var samples = Unfilter(header, raw);
        var image = Assemble(header, samples, palette, transparency);
        return image;
    }

    /// <summary>Writes greyscale, RGB or RGBA pixels with adaptive filtering.</summary>
    public static byte[] Encode(ByteImage image)
    {
        var colourType = image.Channels switch
        {
            1 => (byte)0,
            3 => (byte)2,
            4 => (byte)6,
            _ => throw new TextureError($"Cannot save shape {image.Height}x{image.Width}x{image.Channels} as PNG")
        };
        var stride = image.Width * image.Channels;
        var filtered = new byte[(stride + 1) * image.Height];
        var previous = new byte[stride];
        var current = new byte[stride];
        var best = new byte[stride];
        var candidate = new byte[stride];
        for (var y = 0; y < image.Height; y++)
        {
            Array.Copy(image.Data, y * stride, current, 0, stride);
            var bestType = 0;
            var bestScore = long.MaxValue;
            for (var type = 0; type < 5; type++)
            {
                FilterRow(type, current, previous, candidate, image.Channels);
                long score = 0;
                foreach (var value in candidate) score += value < 128 ? value : 256 - value;
                if (score >= bestScore) continue;
                bestScore = score;
                bestType = type;
                candidate.CopyTo(best, 0);
            }
            var at = y * (stride + 1);
            filtered[at] = (byte)bestType;
            best.CopyTo(filtered, at + 1);
            (previous, current) = (current, previous);
        }
        using var output = new MemoryStream();
        output.Write(Signature);
        Span<byte> ihdr = stackalloc byte[13];
        WriteUInt32(ihdr, (uint)image.Width);
        WriteUInt32(ihdr[4..], (uint)image.Height);
        ihdr[8] = 8;
        ihdr[9] = colourType;
        WriteChunk(output, "IHDR"u8, ihdr);
        using (var deflated = new MemoryStream())
        {
            using (var zlib = new ZLibStream(deflated, CompressionLevel.Optimal, true)) zlib.Write(filtered);
            WriteChunk(output, "IDAT"u8, deflated.GetBuffer().AsSpan(0, (int)deflated.Length));
        }
        WriteChunk(output, "IEND"u8, []);
        return output.ToArray();
    }

    private readonly record struct HeaderInfo(int Width, int Height, int Depth, int ColourType, bool Interlaced)
    {
        public int Samples => ColourType switch { 0 => 1, 2 => 3, 3 => 1, 4 => 2, 6 => 4, _ => 0 };
        public int BitsPerPixel => Depth * Samples;
        public int FilterUnit => Math.Max(1, BitsPerPixel / 8);
    }

    private static HeaderInfo ReadHeader(ReadOnlySpan<byte> file)
    {
        if (!Matches(file) || file.Length < 33 || !file.Slice(12, 4).SequenceEqual("IHDR"u8)) throw new TextureError("Not a PNG file.");
        var header = new HeaderInfo((int)ReadUInt32(file, 16), (int)ReadUInt32(file, 20), file[24], file[25], file[28] == 1);
        var valid = header.ColourType switch
        {
            0 => header.Depth is 1 or 2 or 4 or 8 or 16,
            2 or 4 or 6 => header.Depth is 8 or 16,
            3 => header.Depth is 1 or 2 or 4 or 8,
            _ => false
        };
        if (!valid || header.Width <= 0 || header.Height <= 0) throw new TextureError($"Unsupported PNG layout: colour type {header.ColourType}, depth {header.Depth}.");
        return header;
    }

    private static string ModeFor(HeaderInfo header) => (header.ColourType, header.Depth) switch
    {
        (0, 1) => "1",
        (0, 16) => "I;16",
        (0, _) => "L",
        (2, _) => "RGB",
        (3, _) => "P",
        (4, 8) => "LA",
        _ => "RGBA"
    };

    private static long ScanlineBytes(HeaderInfo header)
    {
        if (!header.Interlaced) return (RowBytes(header, header.Width) + 1L) * header.Height;
        long total = 0;
        for (var pass = 0; pass < 7; pass++)
        {
            var (width, height) = PassSize(header, pass);
            if (width > 0 && height > 0) total += (RowBytes(header, width) + 1L) * height;
        }
        return total;
    }

    private static int RowBytes(HeaderInfo header, int width) => (int)((width * (long)header.BitsPerPixel + 7) / 8);

    private static (int Width, int Height) PassSize(HeaderInfo header, int pass) =>
        ((header.Width - PassX[pass] + StepX[pass] - 1) / StepX[pass], (header.Height - PassY[pass] + StepY[pass] - 1) / StepY[pass]);

    private static byte[] Inflate(Stream compressed, long expected)
    {
        var raw = new byte[expected];
        using var zlib = new ZLibStream(compressed, CompressionMode.Decompress);
        var filled = 0;
        while (filled < raw.Length)
        {
            var read = zlib.Read(raw, filled, raw.Length - filled);
            if (read == 0) throw new TextureError("PNG image data is truncated.");
            filled += read;
        }
        return raw;
    }

    /// <summary>Reverses the scanline filters and lays samples out at full width, packed sub-byte depths included.</summary>
    private static byte[] Unfilter(HeaderInfo header, byte[] raw)
    {
        var stride = RowBytes(header, header.Width);
        var output = new byte[stride * header.Height];
        if (!header.Interlaced)
        {
            UnfilterPass(raw, 0, output, stride, header.Height, header.FilterUnit);
            return output;
        }
        var position = 0;
        for (var pass = 0; pass < 7; pass++)
        {
            var (width, height) = PassSize(header, pass);
            if (width <= 0 || height <= 0) continue;
            var passStride = RowBytes(header, width);
            var pixels = new byte[passStride * height];
            position = UnfilterPass(raw, position, pixels, passStride, height, header.FilterUnit);
            for (var y = 0; y < height; y++)
                for (var x = 0; x < width; x++)
                    CopySample(pixels, y * passStride, x, output, (PassY[pass] + y * StepY[pass]) * stride, PassX[pass] + x * StepX[pass], header.BitsPerPixel);
        }
        return output;
    }

    private static void CopySample(byte[] source, int sourceRow, int sourceX, byte[] target, int targetRow, int targetX, int bits)
    {
        if (bits >= 8)
        {
            var bytes = bits / 8;
            Array.Copy(source, sourceRow + sourceX * bytes, target, targetRow + targetX * bytes, bytes);
            return;
        }
        var value = (source[sourceRow + sourceX * bits / 8] >> (8 - bits - sourceX * bits % 8)) & ((1 << bits) - 1);
        target[targetRow + targetX * bits / 8] |= (byte)(value << (8 - bits - targetX * bits % 8));
    }

    private static int UnfilterPass(byte[] raw, int position, byte[] output, int stride, int height, int unit)
    {
        var previous = new byte[stride];
        var current = new byte[stride];
        for (var y = 0; y < height; y++)
        {
            int type = raw[position++];
            Array.Copy(raw, position, current, 0, stride);
            position += stride;
            switch (type)
            {
                case 0: break;
                case 1:
                    for (var i = unit; i < stride; i++) current[i] += current[i - unit];
                    break;
                case 2:
                    for (var i = 0; i < stride; i++) current[i] += previous[i];
                    break;
                case 3:
                    for (var i = 0; i < stride; i++) current[i] += (byte)(((i >= unit ? current[i - unit] : 0) + previous[i]) >> 1);
                    break;
                case 4:
                    for (var i = 0; i < stride; i++) current[i] += Paeth(i >= unit ? current[i - unit] : (byte)0, previous[i], i >= unit ? previous[i - unit] : (byte)0);
                    break;
                default: throw new TextureError($"Unknown PNG filter {type}.");
            }
            Array.Copy(current, 0, output, y * stride, stride);
            (previous, current) = (current, previous);
        }
        return position;
    }

    private static byte Paeth(byte a, byte b, byte c)
    {
        var p = a + b - c;
        var pa = Math.Abs(p - a);
        var pb = Math.Abs(p - b);
        var pc = Math.Abs(p - c);
        return pa <= pb && pa <= pc ? a : pb <= pc ? b : c;
    }

    private static SourceImage Assemble(HeaderInfo header, byte[] samples, byte[]? palette, byte[]? transparency)
    {
        var width = header.Width;
        var height = header.Height;
        var stride = RowBytes(header, width);
        var mode = ModeFor(header);
        var image = mode switch
        {
            "RGB" => new SourceImage(mode, width, height, HighBytes(samples, header.Depth, 3, width * height)),
            "RGBA" when header.ColourType == 4 => new SourceImage(mode, width, height, GreyAlphaToRgba(samples, width * height)),
            "RGBA" => new SourceImage(mode, width, height, HighBytes(samples, header.Depth, 4, width * height)),
            "LA" => new SourceImage(mode, width, height, samples),
            "I;16" => new SourceImage(mode, width, height, Swap16(samples)),
            _ => new SourceImage(mode, width, height, Unpack(samples, stride, width, height, header.Depth, header.ColourType == 3))
        };
        if (mode == "P")
        {
            image.Palette = palette ?? throw new TextureError("PNG palette image has no palette.");
            image.PaletteAlpha = transparency;
        }
        else if (transparency is { Length: >= 2 } && mode is "1" or "L" or "I;16")
            image.TransparentGrey = (transparency![0] << 8) | transparency[1];
        else if (transparency is { Length: >= 6 } && mode == "RGB")
            image.TransparentRgb = ((transparency![0] << 8) | transparency[1], (transparency[2] << 8) | transparency[3], (transparency[4] << 8) | transparency[5]);
        return image;
    }

    private static byte[] Swap16(byte[] samples)
    {
        var output = new byte[samples.Length];
        for (var i = 0; i + 1 < samples.Length; i += 2)
        {
            output[i] = samples[i + 1];
            output[i + 1] = samples[i];
        }
        return output;
    }

    private static byte[] HighBytes(byte[] samples, int depth, int channels, int pixels)
    {
        if (depth == 8) return samples;
        var output = new byte[pixels * channels];
        for (var i = 0; i < output.Length; i++) output[i] = samples[i * 2];
        return output;
    }

    private static byte[] GreyAlphaToRgba(byte[] samples, int pixels)
    {
        var output = new byte[pixels * 4];
        for (var i = 0; i < pixels; i++)
        {
            var grey = samples[i * 4];
            output[i * 4] = grey;
            output[i * 4 + 1] = grey;
            output[i * 4 + 2] = grey;
            output[i * 4 + 3] = samples[i * 4 + 2];
        }
        return output;
    }

    private static byte[] Unpack(byte[] samples, int stride, int width, int height, int depth, bool indexed)
    {
        if (depth == 8) return samples;
        var output = new byte[width * height];
        var scale = indexed ? 1 : depth switch { 1 => 255, 2 => 85, _ => 17 };
        var mask = (1 << depth) - 1;
        for (var y = 0; y < height; y++)
            for (var x = 0; x < width; x++)
            {
                var bit = x * depth;
                output[y * width + x] = (byte)(((samples[y * stride + bit / 8] >> (8 - depth - bit % 8)) & mask) * scale);
            }
        return output;
    }

    private static void FilterRow(int type, byte[] row, byte[] previous, byte[] output, int unit)
    {
        for (var i = 0; i < row.Length; i++)
        {
            var left = i >= unit ? row[i - unit] : (byte)0;
            var corner = i >= unit ? previous[i - unit] : (byte)0;
            output[i] = type switch
            {
                0 => row[i],
                1 => (byte)(row[i] - left),
                2 => (byte)(row[i] - previous[i]),
                3 => (byte)(row[i] - ((left + previous[i]) >> 1)),
                _ => (byte)(row[i] - Paeth(left, previous[i], corner))
            };
        }
    }

    private static void WriteChunk(Stream output, ReadOnlySpan<byte> tag, ReadOnlySpan<byte> data)
    {
        Span<byte> number = stackalloc byte[4];
        WriteUInt32(number, (uint)data.Length);
        output.Write(number);
        output.Write(tag);
        output.Write(data);
        var crc = Update(Update(0xFFFFFFFF, tag), data) ^ 0xFFFFFFFF;
        WriteUInt32(number, crc);
        output.Write(number);
    }

    private static uint ReadUInt32(ReadOnlySpan<byte> data, int at) => (uint)(data[at] << 24 | data[at + 1] << 16 | data[at + 2] << 8 | data[at + 3]);

    private static void WriteUInt32(Span<byte> data, uint value)
    {
        data[0] = (byte)(value >> 24);
        data[1] = (byte)(value >> 16);
        data[2] = (byte)(value >> 8);
        data[3] = (byte)value;
    }

    private static uint Crc(ReadOnlySpan<byte> data) => Update(0xFFFFFFFF, data) ^ 0xFFFFFFFF;

    private static uint Update(uint crc, ReadOnlySpan<byte> data)
    {
        foreach (var value in data) crc = CrcTable[(crc ^ value) & 0xFF] ^ (crc >> 8);
        return crc;
    }

    private static uint[] BuildCrcTable()
    {
        var table = new uint[256];
        for (uint n = 0; n < 256; n++)
        {
            var value = n;
            for (var bit = 0; bit < 8; bit++) value = (value & 1) != 0 ? 0xEDB88320 ^ (value >> 1) : value >> 1;
            table[n] = value;
        }
        return table;
    }
}
