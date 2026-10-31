using System.Buffers.Binary;

namespace PortingKit.Addons.Porting;

/// <summary>VTF pixel formats, numbered as stored in the file header.</summary>
public enum VtfFormat
{
    RGBA8888 = 0, ABGR8888 = 1, RGB888 = 2, BGR888 = 3, RGB565 = 4, I8 = 5, IA88 = 6, A8 = 8, ARGB8888 = 11, BGRA8888 = 12,
    DXT1 = 13, DXT3 = 14, DXT5 = 15, BGRX8888 = 16, BGR565 = 17, BGRX5551 = 18, BGRA4444 = 19, DXT1_ONE_BIT_ALPHA = 20,
    BGRA5551 = 21, UV88 = 22, UVWQ8888 = 23
}

/// <summary>A single-frame VTF held in memory: its header facts and every mip, largest first.</summary>
/// <remarks>
/// The writer produces version 7.4 with an 88-byte header, one image resource and no thumbnail. The low-resolution format field
/// keeps the value the earlier writer stored (0x21) beside zero dimensions, which the engine ignores. Reflectivity stays at the
/// default 0.2, which is not computed because no shipped material reads it.
/// </remarks>
public sealed class Vtf
{
    public const uint FlagSrgb = 0x00000040;
    public const uint FlagNormal = 0x00000080;
    public const uint FlagOneBitAlpha = 0x00001000;
    public const uint FlagEightBitAlpha = 0x00002000;
    private const uint FlagEnvmap = 0x00004000;
    private const int HeaderSize = 88;
    private const int NoThumbnail = 0x21;

    public int Width { get; private set; }
    public int Height { get; private set; }
    public VtfFormat Format { get; private set; }
    public uint Flags { get; set; }
    public int Frames { get; private set; } = 1;
    public List<byte[]> Mips { get; private set; } = [];
    public int MipCount => Mips.Count;

    public static bool IsCompressed(VtfFormat format) => format is VtfFormat.DXT1 or VtfFormat.DXT3 or VtfFormat.DXT5 or VtfFormat.DXT1_ONE_BIT_ALPHA;

    /// <summary>Whether a format stores an alpha channel, which is what lets a texture claim one in its flags.</summary>
    public static bool HasAlpha(VtfFormat format) => format is VtfFormat.RGBA8888 or VtfFormat.ABGR8888 or VtfFormat.ARGB8888 or VtfFormat.BGRA8888
        or VtfFormat.DXT3 or VtfFormat.DXT5 or VtfFormat.DXT1_ONE_BIT_ALPHA or VtfFormat.BGRA4444 or VtfFormat.BGRA5551 or VtfFormat.IA88 or VtfFormat.A8 or VtfFormat.UVWQ8888;

    public static int MipSize(int size, int mip) => Math.Max(1, size >> mip);

    private static int MaximumMips(int width, int height)
    {
        var count = 1;
        while (width > 1 || height > 1)
        {
            width = Math.Max(1, width >> 1);
            height = Math.Max(1, height >> 1);
            count++;
        }
        return count;
    }

    private static bool PowerOfTwo(int value) => (value & (value - 1)) == 0;

    /// <summary>Builds a texture the way the earlier writer did, generating mips in RGBA before converting each to the final format.</summary>
    /// <remarks>
    /// Compressed formats need whole blocks, so dimensions round up to a multiple of four and each mip is rescaled to fit, and
    /// only power-of-two textures keep a chain: the rest ship one level. That is the writer's own rule, not a choice made here.
    /// </remarks>
    public static Vtf Create(byte[] rgba, int width, int height, VtfFormat format, uint flags, bool mipmaps)
    {
        var count = mipmaps ? MaximumMips(width, height) : 1;
        var chain = new List<byte[]> { rgba };
        for (var mip = 1; mip < count; mip++)
            chain.Add(MipFilter.Resize(chain[mip - 1], MipSize(width, mip - 1), MipSize(height, mip - 1), MipSize(width, mip), MipSize(height, mip)));
        return new Vtf { Width = width, Height = height, Format = VtfFormat.RGBA8888, Flags = flags, Mips = chain }.Convert(format);
    }

    /// <summary>Returns this texture in another format, re-encoding every mip from its decoded pixels.</summary>
    public Vtf Convert(VtfFormat format)
    {
        var width = Width;
        var height = Height;
        var chain = Enumerable.Range(0, MipCount).Select(mip => Format == VtfFormat.RGBA8888 ? Mips[mip] : Decode(mip).Data).ToList();
        if (IsCompressed(format))
        {
            var paddedWidth = (width + 3) & ~3;
            var paddedHeight = (height + 3) & ~3;
            var count = PowerOfTwo(paddedWidth) && PowerOfTwo(paddedHeight) ? Math.Min(chain.Count, MaximumMips(paddedWidth, paddedHeight)) : 1;
            if (paddedWidth != width || paddedHeight != height)
                chain = Enumerable.Range(0, count).Select(mip => MipFilter.Resize(chain[mip], MipSize(width, mip), MipSize(height, mip), MipSize(paddedWidth, mip), MipSize(paddedHeight, mip))).ToList();
            else chain = chain.Take(count).ToList();
            width = paddedWidth;
            height = paddedHeight;
        }
        var result = new Vtf { Width = width, Height = height, Format = format, Flags = Flags, Mips = new List<byte[]>(chain.Count) };
        for (var mip = 0; mip < chain.Count; mip++)
            result.Mips.Add(Encode(chain[mip], MipSize(width, mip), MipSize(height, mip), format));
        return result;
    }

    private static byte[] Encode(byte[] rgba, int width, int height, VtfFormat format)
    {
        if (IsCompressed(format)) return Dxt.Compress(rgba, width, height, format);
        var pixels = width * height;
        switch (format)
        {
            case VtfFormat.RGBA8888: return rgba;
            case VtfFormat.BGRA8888:
                var bgra = new byte[pixels * 4];
                for (var i = 0; i < pixels; i++)
                {
                    bgra[i * 4] = rgba[i * 4 + 2];
                    bgra[i * 4 + 1] = rgba[i * 4 + 1];
                    bgra[i * 4 + 2] = rgba[i * 4];
                    bgra[i * 4 + 3] = rgba[i * 4 + 3];
                }
                return bgra;
            case VtfFormat.BGR888:
                var bgr = new byte[pixels * 3];
                for (var i = 0; i < pixels; i++)
                {
                    bgr[i * 3] = rgba[i * 4 + 2];
                    bgr[i * 3 + 1] = rgba[i * 4 + 1];
                    bgr[i * 3 + 2] = rgba[i * 4];
                }
                return bgr;
            default: throw new TextureError($"Writing {format} is not supported.");
        }
    }

    /// <summary>Reads any version 7 texture, keeping only the first frame, face and slice of each mip.</summary>
    public static Vtf Read(string path)
    {
        var file = File.ReadAllBytes(path);
        var name = System.IO.Path.GetFileName(path);
        if (file.Length < 64 || !file.AsSpan(0, 4).SequenceEqual("VTF\0"u8)) throw new TextureError($"{name}: not a VTF file");
        var minor = BinaryPrimitives.ReadInt32LittleEndian(file.AsSpan(8));
        var headerSize = BinaryPrimitives.ReadInt32LittleEndian(file.AsSpan(12));
        var width = BinaryPrimitives.ReadUInt16LittleEndian(file.AsSpan(16));
        var height = BinaryPrimitives.ReadUInt16LittleEndian(file.AsSpan(18));
        if (width == 0 || height == 0) throw new TextureError($"{name}: bad header dimensions {width}x{height}");
        var flags = BinaryPrimitives.ReadUInt32LittleEndian(file.AsSpan(20));
        var frames = Math.Max((int)BinaryPrimitives.ReadUInt16LittleEndian(file.AsSpan(24)), 1);
        var format = (VtfFormat)BinaryPrimitives.ReadInt32LittleEndian(file.AsSpan(52));
        var mipCount = Math.Max((int)file[56], 1);
        var thumbnailFormat = BinaryPrimitives.ReadInt32LittleEndian(file.AsSpan(57));
        int thumbnailWidth = file[61], thumbnailHeight = file[62];
        var depth = minor >= 2 ? Math.Max((int)BinaryPrimitives.ReadUInt16LittleEndian(file.AsSpan(63)), 1) : 1;
        var faces = (flags & FlagEnvmap) == 0 ? 1 : minor is >= 1 and <= 4 ? 7 : 6;
        var data = headerSize;
        if (minor >= 3)
        {
            var resources = BinaryPrimitives.ReadInt32LittleEndian(file.AsSpan(68));
            var found = false;
            for (var i = 0; i < resources; i++)
            {
                var entry = 80 + i * 8;
                if (file[entry] != 0x30) continue;
                data = BinaryPrimitives.ReadInt32LittleEndian(file.AsSpan(entry + 4));
                found = true;
                break;
            }
            if (!found) throw new TextureError($"{name}: no image data resource");
        }
        else if (thumbnailWidth > 0 && thumbnailHeight > 0 && thumbnailFormat >= 0)
            data += StoredSize((VtfFormat)thumbnailFormat, thumbnailWidth, thumbnailHeight);
        var positions = new int[mipCount];
        var sizes = new int[mipCount];
        for (var mip = mipCount - 1; mip >= 0; mip--)
        {
            sizes[mip] = StoredSize(format, MipSize(width, mip), MipSize(height, mip));
            positions[mip] = data;
            data += sizes[mip] * frames * faces * Math.Max(1, depth >> mip);
        }
        var result = new Vtf { Width = width, Height = height, Format = format, Flags = flags, Frames = frames, Mips = new List<byte[]>(mipCount) };
        for (var mip = 0; mip < mipCount; mip++)
            result.Mips.Add(file.AsSpan(positions[mip], sizes[mip]).ToArray());
        return result;
    }

    /// <summary>Width and height straight from the header, without decoding pixels.</summary>
    public static (int Width, int Height) ReadSize(string path)
    {
        var name = System.IO.Path.GetFileName(path);
        Span<byte> header = stackalloc byte[20];
        using var stream = File.OpenRead(path);
        if (stream.ReadAtLeast(header, 20, false) < 20 || !header[..4].SequenceEqual("VTF\0"u8)) throw new TextureError($"{name}: not a VTF file");
        int width = BinaryPrimitives.ReadUInt16LittleEndian(header[16..]), height = BinaryPrimitives.ReadUInt16LittleEndian(header[18..]);
        if (width <= 0 || height <= 0) throw new TextureError($"{name}: bad header dimensions {width}x{height}");
        return (width, height);
    }

    private static int StoredSize(VtfFormat format, int width, int height)
    {
        if (IsCompressed(format)) return Dxt.StoredSize(format, width, height);
        return width * height * format switch
        {
            VtfFormat.RGBA8888 or VtfFormat.ABGR8888 or VtfFormat.ARGB8888 or VtfFormat.BGRA8888 or VtfFormat.BGRX8888 or VtfFormat.UVWQ8888 => 4,
            VtfFormat.RGB888 or VtfFormat.BGR888 => 3,
            VtfFormat.RGB565 or VtfFormat.BGR565 or VtfFormat.BGRA4444 or VtfFormat.BGRX5551 or VtfFormat.BGRA5551 or VtfFormat.IA88 or VtfFormat.UV88 => 2,
            VtfFormat.I8 or VtfFormat.A8 => 1,
            _ => throw new TextureError($"Unsupported VTF format {(int)format}.")
        };
    }

    /// <summary>Whether a mip can be decoded at all.</summary>
    /// <remarks>
    /// The earlier toolchain returned nothing for a compressed mip with one side at or above four pixels and the other side not a
    /// multiple of four, which every non-square chain reaches near its end. The alpha-storage check treated that as "stop here",
    /// so those textures always keep their original format, and that behaviour is preserved.
    /// </remarks>
    public bool CanDecode(int mip)
    {
        if (!IsCompressed(Format)) return true;
        int width = MipSize(Width, mip), height = MipSize(Height, mip);
        return (width % 4 == 0 && height % 4 == 0) || (width < 4 && height < 4);
    }

    /// <summary>Decodes one mip as tightly packed RGBA.</summary>
    public ByteImage Decode(int mip = 0)
    {
        int width = MipSize(Width, mip), height = MipSize(Height, mip);
        var data = Mips[mip];
        var pixels = width * height;
        if (IsCompressed(Format)) return new(height, width, 4, Dxt.Decompress(data, width, height, Format));
        var output = new byte[pixels * 4];
        for (var i = 0; i < pixels; i++)
        {
            var o = i * 4;
            switch (Format)
            {
                case VtfFormat.RGBA8888: case VtfFormat.UVWQ8888: Set(output, o, data[o], data[o + 1], data[o + 2], data[o + 3]); break;
                case VtfFormat.ABGR8888: Set(output, o, data[o + 3], data[o + 2], data[o + 1], data[o]); break;
                case VtfFormat.ARGB8888: Set(output, o, data[o + 1], data[o + 2], data[o + 3], data[o]); break;
                case VtfFormat.BGRA8888: Set(output, o, data[o + 2], data[o + 1], data[o], data[o + 3]); break;
                case VtfFormat.BGRX8888: Set(output, o, data[o + 2], data[o + 1], data[o], 255); break;
                case VtfFormat.RGB888: Set(output, o, data[i * 3], data[i * 3 + 1], data[i * 3 + 2], 255); break;
                case VtfFormat.BGR888: Set(output, o, data[i * 3 + 2], data[i * 3 + 1], data[i * 3], 255); break;
                case VtfFormat.I8: Set(output, o, data[i], data[i], data[i], 255); break;
                case VtfFormat.A8: Set(output, o, 0, 0, 0, data[i]); break;
                case VtfFormat.IA88: Set(output, o, data[i * 2], data[i * 2], data[i * 2], data[i * 2 + 1]); break;
                case VtfFormat.UV88: Set(output, o, data[i * 2], data[i * 2 + 1], 0, 255); break;
                default: DecodePacked(data, i, output, o); break;
            }
        }
        return new(height, width, 4, output);
    }

    private void DecodePacked(byte[] data, int i, byte[] output, int o)
    {
        var value = data[i * 2] | data[i * 2 + 1] << 8;
        switch (Format)
        {
            case VtfFormat.BGR565: Set(output, o, Scale(value >> 11, 31), Scale(value >> 5, 63), Scale(value, 31), 255); break;
            case VtfFormat.RGB565: Set(output, o, Scale(value, 31), Scale(value >> 5, 63), Scale(value >> 11, 31), 255); break;
            case VtfFormat.BGRA4444: Set(output, o, Scale(value >> 8, 15), Scale(value >> 4, 15), Scale(value, 15), Scale(value >> 12, 15)); break;
            case VtfFormat.BGRX5551: Set(output, o, Scale(value >> 10, 31), Scale(value >> 5, 31), Scale(value, 31), 255); break;
            case VtfFormat.BGRA5551: Set(output, o, Scale(value >> 10, 31), Scale(value >> 5, 31), Scale(value, 31), (byte)((value >> 15) * 255)); break;
            default: throw new TextureError($"Unsupported VTF format {(int)Format}.");
        }
    }

    private static byte Scale(int value, int limit) => (byte)((value & limit) * 255 / limit);

    private static void Set(byte[] output, int at, byte r, byte g, byte b, byte a)
    {
        output[at] = r;
        output[at + 1] = g;
        output[at + 2] = b;
        output[at + 3] = a;
    }

    /// <summary>Serializes the texture, smallest mip first.</summary>
    public byte[] Bake()
    {
        var size = HeaderSize + Mips.Sum(mip => mip.Length);
        var file = new byte[size];
        var span = file.AsSpan();
        "VTF\0"u8.CopyTo(span);
        BinaryPrimitives.WriteInt32LittleEndian(span[4..], 7);
        BinaryPrimitives.WriteInt32LittleEndian(span[8..], 4);
        BinaryPrimitives.WriteInt32LittleEndian(span[12..], HeaderSize);
        BinaryPrimitives.WriteUInt16LittleEndian(span[16..], (ushort)Width);
        BinaryPrimitives.WriteUInt16LittleEndian(span[18..], (ushort)Height);
        BinaryPrimitives.WriteUInt32LittleEndian(span[20..], Flags);
        BinaryPrimitives.WriteUInt16LittleEndian(span[24..], 1);
        BinaryPrimitives.WriteSingleLittleEndian(span[32..], 0.2f);
        BinaryPrimitives.WriteSingleLittleEndian(span[36..], 0.2f);
        BinaryPrimitives.WriteSingleLittleEndian(span[40..], 0.2f);
        BinaryPrimitives.WriteSingleLittleEndian(span[48..], 1f);
        BinaryPrimitives.WriteInt32LittleEndian(span[52..], (int)Format);
        file[56] = (byte)MipCount;
        BinaryPrimitives.WriteInt32LittleEndian(span[57..], NoThumbnail);
        BinaryPrimitives.WriteUInt16LittleEndian(span[63..], 1);
        BinaryPrimitives.WriteInt32LittleEndian(span[68..], 1);
        file[80] = 0x30;
        BinaryPrimitives.WriteInt32LittleEndian(span[84..], HeaderSize);
        var position = HeaderSize;
        for (var mip = MipCount - 1; mip >= 0; mip--)
        {
            Mips[mip].CopyTo(file, position);
            position += Mips[mip].Length;
        }
        return file;
    }

    public void Save(string path)
    {
        var directory = System.IO.Path.GetDirectoryName(path);
        if (!string.IsNullOrEmpty(directory)) Directory.CreateDirectory(directory);
        File.WriteAllBytes(path, Bake());
    }
}
