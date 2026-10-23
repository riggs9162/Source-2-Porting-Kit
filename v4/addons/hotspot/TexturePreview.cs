using System.Buffers.Binary;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;

namespace PortingKit.Addons.Hotspot;

/// <summary>Owns a bounded preview, retaining original dimensions for pixel-accurate editing.</summary>
public sealed record TexturePreview(Bitmap Bitmap, int Width, int Height) : IDisposable
{
    public const int MaxPreview = 2048;
    public void Dispose() => Bitmap.Dispose();

    public static TexturePreview Load(string path, CancellationToken token = default)
    {
        token.ThrowIfCancellationRequested();
        var extension = Path.GetExtension(path).ToLowerInvariant();
        if (extension == ".vtf") return LoadVtf(path, token);
        if (extension == ".tga") return LoadTga(path, token);
        using var stream = File.OpenRead(path);
        using var source = Image.FromStream(stream, false, true);
        CheckSize(source.Width, source.Height);
        var scale = Math.Min(1, (double)MaxPreview / Math.Max(source.Width, source.Height));
        var bitmap = new Bitmap(Math.Max(1, (int)(source.Width * scale)), Math.Max(1, (int)(source.Height * scale)), PixelFormat.Format32bppArgb);
        try
        {
            using var graphics = Graphics.FromImage(bitmap);
            graphics.DrawImage(source, new Rectangle(0, 0, bitmap.Width, bitmap.Height));
            token.ThrowIfCancellationRequested();
            return new(bitmap, source.Width, source.Height);
        }
        catch { bitmap.Dispose(); throw; }
    }

    private static void CheckSize(int width, int height)
    {
        if (width < 1 || height < 1 || width > 65535 || height > 65535 || (long)width * height > 64 * 1024 * 1024)
            throw new InvalidDataException("Preview supports textures up to 64 million pixels, with dimensions at most 65535.");
    }

    private static TexturePreview LoadVtf(string path, CancellationToken token)
    {
        using var stream = File.OpenRead(path);
        using var reader = new BinaryReader(stream);
        var header = reader.ReadBytes(80);
        if (header.Length < 64 || header[0] != 'V' || header[1] != 'T' || header[2] != 'F' || header[3] != 0)
            throw new InvalidDataException("Not a VTF texture.");
        int U16(int offset) => BinaryPrimitives.ReadUInt16LittleEndian(header.AsSpan(offset));
        uint U32(int offset) => BinaryPrimitives.ReadUInt32LittleEndian(header.AsSpan(offset));
        var minor = U32(8);
        if (U32(4) != 7 || minor > 5) throw new InvalidDataException("Native previews support PC VTF 7.0 through 7.5. Export other variants to PNG first.");
        var width = U16(16); var height = U16(18);
        CheckSize(width, height);
        var frames = U16(24); var mipCount = header[56];
        var depth = minor >= 2 ? U16(63) : 1;
        var format = (int)U32(52);
        var faces = (U32(20) & 0x4000) != 0 ? (minor < 5 && U16(26) != 65535 ? 7 : 6) : 1;
        if (frames < 1 || depth < 1 || mipCount < 1 || mipCount > 1 + (int)Math.Log2(Math.Max(width, height)))
            throw new InvalidDataException("Invalid VTF frame, depth, or mip count.");
        var headerSize = U32(12);
        if (headerSize < (minor >= 3 ? 80 : minor >= 2 ? 65 : 64) || headerSize > stream.Length)
            throw new InvalidDataException("Invalid VTF header size.");
        long offset = headerSize;
        if (minor >= 3)
        {
            if (header.Length < 80) throw new InvalidDataException("Truncated VTF resource header.");
            var count = U32(68);
            if (count > 4096 || 80L + count * 8 > headerSize) throw new InvalidDataException("Invalid VTF resource table.");
            stream.Position = 80;
            var found = false;
            for (var i = 0; i < count; i++)
            {
                var tag = reader.ReadUInt32(); var location = reader.ReadUInt32();
                if ((tag & 0xffffff) == 0x30 && (tag >> 24 & 2) == 0) { offset = location; found = true; }
            }
            if (!found) throw new InvalidDataException("VTF has no image resource.");
        }
        else if (header[61] > 0 && header[62] > 0)
            offset += ByteCount(header[61], header[62], (int)U32(57));
        var selectedMip = 0;
        while (selectedMip + 1 < mipCount && Math.Max(width >> selectedMip, height >> selectedMip) > MaxPreview) selectedMip++;
        for (var mip = mipCount - 1; mip > selectedMip; mip--)
            offset = checked(offset + (long)ByteCount(Math.Max(1, width >> mip), Math.Max(1, height >> mip), format) * frames * faces * Math.Max(1, depth >> mip));
        var w = Math.Max(1, width >> selectedMip); var h = Math.Max(1, height >> selectedMip);
        var countBytes = ByteCount(w, h, format);
        if (offset < headerSize || offset > stream.Length - countBytes) throw new InvalidDataException("VTF image data is truncated.");
        stream.Position = offset;
        var pixels = new byte[countBytes];
        stream.ReadExactly(pixels);
        token.ThrowIfCancellationRequested();
        return Decode(w, h, width, height, format, pixels, token);
    }

    private static int ByteCount(int w, int h, int format) => checked(format switch
    {
        13 or 20 => Math.Max(1, (w + 3) / 4) * Math.Max(1, (h + 3) / 4) * 8,
        14 or 15 => Math.Max(1, (w + 3) / 4) * Math.Max(1, (h + 3) / 4) * 16,
        0 or 1 or 11 or 12 or 16 => w * h * 4,
        2 or 3 => w * h * 3,
        4 or 6 or 17 or 18 or 19 or 21 => w * h * 2,
        5 or 8 => w * h,
        _ => throw new InvalidDataException($"VTF image format {format} is not supported by this preview. Export a PNG first.")
    });

    private static TexturePreview Decode(int w, int h, int originalW, int originalH, int format, byte[] data, CancellationToken token)
    {
        var scale = Math.Min(1, (double)MaxPreview / Math.Max(w, h));
        var pw = Math.Max(1, (int)(w * scale)); var ph = Math.Max(1, (int)(h * scale));
        var pixels = new int[pw * ph];
        var compressed = format is 13 or 14 or 15 or 20;
        var stride = compressed ? 0 : ByteCount(1, 1, format);
        Span<int> colors = stackalloc int[4];
        Span<byte> alphas = stackalloc byte[8];
        for (var y = 0; y < ph; y++)
        {
            token.ThrowIfCancellationRequested();
            for (var x = 0; x < pw; x++)
            {
                var sx = x * w / pw; var sy = y * h / ph;
                if (compressed)
                {
                    var blockSize = format is 13 or 20 ? 8 : 16;
                    var offset = ((sy / 4) * ((w + 3) / 4) + sx / 4) * blockSize;
                    var block = data.AsSpan(offset, blockSize);
                    var color = block[(blockSize - 8)..];
                    var c0 = BinaryPrimitives.ReadUInt16LittleEndian(color);
                    var c1 = BinaryPrimitives.ReadUInt16LittleEndian(color[2..]);
                    colors[0] = Rgb565(c0); colors[1] = Rgb565(c1);
                    colors[2] = Mix(colors[0], colors[1], c0 > c1 || blockSize == 16 ? 2 : 1, 1);
                    colors[3] = c0 > c1 || blockSize == 16 ? Mix(colors[0], colors[1], 1, 2) : 0;
                    var index = (sy % 4) * 4 + sx % 4;
                    var value = colors[(int)(BinaryPrimitives.ReadUInt32LittleEndian(color[4..]) >> (index * 2) & 3)];
                    if (format == 14)
                        value = (value & 0xffffff) | (int)((BinaryPrimitives.ReadUInt64LittleEndian(block) >> (index * 4) & 15) * 17 << 24);
                    if (format == 15)
                    {
                        alphas[0] = block[0]; alphas[1] = block[1];
                        var n = alphas[0] > alphas[1] ? 7 : 5;
                        for (var a = 1; a < n; a++) alphas[a + 1] = (byte)(((n - a) * alphas[0] + a * alphas[1]) / n);
                        if (n == 5) { alphas[6] = 0; alphas[7] = 255; }
                        var bits = BinaryPrimitives.ReadUInt64LittleEndian(block) >> 16;
                        value = (value & 0xffffff) | alphas[(int)(bits >> (index * 3) & 7)] << 24;
                    }
                    pixels[y * pw + x] = value;
                }
                else pixels[y * pw + x] = Uncompressed(data.AsSpan((sy * w + sx) * stride, stride), format);
            }
        }
        return new(ToBitmap(pixels, pw, ph), originalW, originalH);
    }

    private static int Rgba(int r, int g, int b, int a = 255) => a << 24 | r << 16 | g << 8 | b;
    private static int Rgb565(int n) => Rgba(((n >> 11) & 31) * 255 / 31, ((n >> 5) & 63) * 255 / 63, (n & 31) * 255 / 31);
    private static int Mix(int a, int b, int wa, int wb) => Rgba(
        (((a >> 16) & 255) * wa + ((b >> 16) & 255) * wb) / (wa + wb),
        (((a >> 8) & 255) * wa + ((b >> 8) & 255) * wb) / (wa + wb),
        ((a & 255) * wa + (b & 255) * wb) / (wa + wb));

    private static int Uncompressed(ReadOnlySpan<byte> p, int format)
    {
        var n = p.Length >= 2 ? BinaryPrimitives.ReadUInt16LittleEndian(p) : 0;
        return format switch
        {
            0 => Rgba(p[0], p[1], p[2], p[3]), 1 => Rgba(p[1], p[2], p[3], p[0]),
            2 => Rgba(p[0], p[1], p[2]), 3 => Rgba(p[2], p[1], p[0]), 4 => Rgb565(n),
            5 => Rgba(p[0], p[0], p[0]), 6 => Rgba(p[0], p[0], p[0], p[1]), 8 => Rgba(255, 255, 255, p[0]),
            11 => Rgba(p[3], p[2], p[1], p[0]), 12 => Rgba(p[2], p[1], p[0], p[3]),
            16 => Rgba(p[2], p[1], p[0]),
            17 => Rgba((n & 31) * 255 / 31, ((n >> 5) & 63) * 255 / 63, ((n >> 11) & 31) * 255 / 31),
            18 or 21 => Rgba(((n >> 10) & 31) * 255 / 31, ((n >> 5) & 31) * 255 / 31, (n & 31) * 255 / 31, format == 18 || (n & 0x8000) != 0 ? 255 : 0),
            19 => Rgba(((n >> 8) & 15) * 17, ((n >> 4) & 15) * 17, (n & 15) * 17, ((n >> 12) & 15) * 17),
            _ => throw new InvalidDataException("Unsupported texture format.")
        };
    }

    private static TexturePreview LoadTga(string path, CancellationToken token)
    {
        using var stream = File.OpenRead(path);
        using var reader = new BinaryReader(stream);
        var header = reader.ReadBytes(18);
        if (header.Length != 18 || header[1] != 0 || header[2] is not (2 or 3 or 10 or 11))
            throw new InvalidDataException("Preview supports true-color or grayscale TGA, raw or RLE.");
        var w = BinaryPrimitives.ReadUInt16LittleEndian(header.AsSpan(12));
        var h = BinaryPrimitives.ReadUInt16LittleEndian(header.AsSpan(14));
        CheckSize(w, h);
        var bytes = header[16] / 8;
        if (bytes is not (1 or 3 or 4) || (header[2] is 3 or 11) != (bytes == 1)) throw new InvalidDataException("Unsupported TGA pixel depth.");
        stream.Position += header[0];
        var scale = Math.Min(1, (double)MaxPreview / Math.Max(w, h));
        var pw = Math.Max(1, (int)(w * scale)); var ph = Math.Max(1, (int)(h * scale));
        var pixels = new int[pw * ph];
        int ReadPixel()
        {
            var b = reader.ReadByte();
            if (bytes == 1) return Rgba(b, b, b);
            var g = reader.ReadByte(); var r = reader.ReadByte();
            return Rgba(r, g, b, bytes == 4 ? reader.ReadByte() : 255);
        }
        var total = w * h; var at = 0;
        while (at < total)
        {
            token.ThrowIfCancellationRequested();
            var packet = header[2] >= 10 ? reader.ReadByte() : 0;
            var count = (packet & 127) + 1;
            if (at + count > total) throw new InvalidDataException("TGA packet overruns image.");
            var repeated = (packet & 128) != 0;
            var value = repeated ? ReadPixel() : 0;
            for (var i = 0; i < count; i++, at++)
            {
                if (!repeated) value = ReadPixel();
                var x = at % w; var y = at / w;
                if ((header[17] & 0x20) == 0) y = h - 1 - y;
                if ((header[17] & 0x10) != 0) x = w - 1 - x;
                pixels[y * ph / h * pw + x * pw / w] = value;
            }
        }
        return new(ToBitmap(pixels, pw, ph), w, h);
    }

    internal static Bitmap ToBitmap(int[] pixels, int width, int height)
    {
        var result = new Bitmap(width, height, PixelFormat.Format32bppArgb);
        var bits = result.LockBits(new Rectangle(0, 0, width, height), ImageLockMode.WriteOnly, PixelFormat.Format32bppArgb);
        try { for (var y = 0; y < height; y++) Marshal.Copy(pixels, y * width, bits.Scan0 + y * bits.Stride, width); }
        finally { result.UnlockBits(bits); }
        return result;
    }

    internal static int[] Pixels(Bitmap bitmap)
    {
        var result = new int[bitmap.Width * bitmap.Height];
        var bits = bitmap.LockBits(new Rectangle(0, 0, bitmap.Width, bitmap.Height), ImageLockMode.ReadOnly, PixelFormat.Format32bppArgb);
        try { for (var y = 0; y < bitmap.Height; y++) Marshal.Copy(bits.Scan0 + y * bits.Stride, result, y * bitmap.Width, bitmap.Width); }
        finally { bitmap.UnlockBits(bits); }
        return result;
    }
}
