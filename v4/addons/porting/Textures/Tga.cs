namespace PortingKit.Addons.Porting;

/// <summary>Reads Targa files, the other format an authored material path can name.</summary>
public static class Tga
{
    /// <summary>Validates the 18-byte header the way a content sniffer must, since Targa has no signature.</summary>
    public static bool Matches(ReadOnlySpan<byte> file)
    {
        if (file.Length < 18) return false;
        var width = file[12] | file[13] << 8;
        var height = file[14] | file[15] << 8;
        return file[1] is 0 or 1 && width > 0 && height > 0 && file[16] is 1 or 8 or 16 or 24 or 32 && file[2] is 1 or 2 or 3 or 9 or 10 or 11;
    }

    /// <summary>Reads only the header, for reporting a map's size and bands without its pixels.</summary>
    public static SourceImage Probe(ReadOnlySpan<byte> file) => SourceImage.Header(ModeFor(file), file[12] | file[13] << 8, file[14] | file[15] << 8);

    /// <summary>Decodes run-length and raw images in either vertical orientation.</summary>
    public static SourceImage Decode(ReadOnlySpan<byte> file)
    {
        if (!Matches(file)) throw new TextureError("Not a TGA file.");
        var type = file[2];
        var depth = file[16];
        var flags = file[17];
        var width = file[12] | file[13] << 8;
        var height = file[14] | file[15] << 8;
        var position = 18 + file[0];
        var mode = ModeFor(file);
        byte[]? palette = null;
        byte[]? paletteAlpha = null;
        if (file[1] == 1)
        {
            var start = file[3] | file[4] << 8;
            var count = file[5] | file[6] << 8;
            var mapDepth = file[7];
            if (mapDepth is not (16 or 24 or 32)) throw new TextureError("Unknown TGA map depth.");
            var size = mapDepth / 8;
            palette = new byte[256 * 3];
            paletteAlpha = mapDepth == 24 ? null : new byte[256];
            for (var i = 0; i < count; i++)
            {
                var slot = start + i;
                var at = position + i * size;
                if (slot >= 256) continue;
                if (mapDepth == 16) Expand15(file[at] | file[at + 1] << 8, palette.AsSpan(slot * 3), paletteAlpha!, slot);
                else
                {
                    palette[slot * 3] = file[at + 2];
                    palette[slot * 3 + 1] = file[at + 1];
                    palette[slot * 3 + 2] = file[at];
                    if (paletteAlpha is not null) paletteAlpha[slot] = file[at + 3];
                }
            }
            position += count * size;
        }
        var bytes = Math.Max(1, depth / 8);
        var pixels = new byte[width * height * bytes];
        if ((type & 8) == 0) file.Slice(position, Math.Min(pixels.Length, file.Length - position)).CopyTo(pixels);
        else ReadRuns(file, position, pixels, bytes);
        var output = Rearrange(pixels, width, height, bytes, depth, type, flags);
        return new SourceImage(mode, width, height, output) { Palette = palette, PaletteAlpha = paletteAlpha };
    }

    private static string ModeFor(ReadOnlySpan<byte> file)
    {
        var type = file[2];
        var depth = file[16];
        return type switch
        {
            3 or 11 => depth == 1 ? "1" : depth == 16 ? "LA" : "L",
            1 or 9 => file[1] != 0 ? "P" : "L",
            2 or 10 => depth == 24 ? "RGB" : "RGBA",
            _ => throw new TextureError("Unknown TGA mode.")
        };
    }

    private static void ReadRuns(ReadOnlySpan<byte> file, int position, byte[] pixels, int bytes)
    {
        var filled = 0;
        while (filled < pixels.Length && position < file.Length)
        {
            var packet = file[position++];
            var count = (packet & 0x7F) + 1;
            if ((packet & 0x80) != 0)
            {
                for (var i = 0; i < count && filled < pixels.Length; i++, filled += bytes)
                    file.Slice(position, bytes).CopyTo(pixels.AsSpan(filled));
                position += bytes;
            }
            else
            {
                var length = Math.Min(count * bytes, pixels.Length - filled);
                file.Slice(position, length).CopyTo(pixels.AsSpan(filled));
                filled += length;
                position += count * bytes;
            }
        }
    }

    private static byte[] Rearrange(byte[] pixels, int width, int height, int bytes, int depth, int type, int flags)
    {
        var channels = depth == 16 && (type & 3) == 2 ? 4 : bytes;
        var output = new byte[width * height * channels];
        var flipX = (flags & 0x10) != 0;
        var topDown = (flags & 0x20) != 0;
        for (var y = 0; y < height; y++)
        {
            var row = topDown ? y : height - 1 - y;
            for (var x = 0; x < width; x++)
            {
                var from = (row * width + (flipX ? width - 1 - x : x)) * bytes;
                var to = (y * width + x) * channels;
                if (depth == 16 && (type & 3) == 2) Expand15(pixels[from] | pixels[from + 1] << 8, output.AsSpan(to), output, to + 3);
                else if (bytes >= 3)
                {
                    output[to] = pixels[from + 2];
                    output[to + 1] = pixels[from + 1];
                    output[to + 2] = pixels[from];
                    if (bytes == 4) output[to + 3] = pixels[from + 3];
                }
                else if (bytes == 2)
                {
                    output[to] = pixels[from];
                    output[to + 1] = pixels[from + 1];
                }
                else output[to] = pixels[from];
            }
        }
        return output;
    }

    private static void Expand15(int value, Span<byte> rgb, byte[] alpha, int alphaAt)
    {
        rgb[0] = (byte)(((value >> 10) & 31) * 255 / 31);
        rgb[1] = (byte)(((value >> 5) & 31) * 255 / 31);
        rgb[2] = (byte)((value & 31) * 255 / 31);
        alpha[alphaAt] = (byte)((value & 0x8000) != 0 ? 255 : 0);
    }
}
