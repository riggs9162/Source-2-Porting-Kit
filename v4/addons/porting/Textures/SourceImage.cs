namespace PortingKit.Addons.Porting;

/// <summary>A decoded image file in the mode it was stored, with the conversions the material loaders ask for.</summary>
/// <remarks>
/// Modes keep Pillow's names because the v3 loaders branch on them: <c>1</c>, <c>L</c>, <c>LA</c>, <c>P</c>, <c>RGB</c>, <c>RGBA</c>
/// and <c>I;16</c>, whose pixels are little-endian. Files are recognised by content, not extension: Source 2's builtin default
/// maps are written as PNG data under a <c>.tga</c> name, and a loader keyed on the extension would reject them.
/// </remarks>
public sealed class SourceImage(string mode, int width, int height, byte[] pixels)
{
    public string Mode { get; } = mode;
    public int Width { get; } = width;
    public int Height { get; } = height;
    public byte[] Pixels { get; } = pixels;
    public byte[]? Palette { get; set; }
    public byte[]? PaletteAlpha { get; set; }
    public int? TransparentGrey { get; set; }
    public (int R, int G, int B)? TransparentRgb { get; set; }

    /// <summary>The band names a Pillow image reports for each mode, as recorded in material provenance.</summary>
    public string[] Bands => Mode switch
    {
        "RGB" => ["R", "G", "B"],
        "RGBA" => ["R", "G", "B", "A"],
        "LA" => ["L", "A"],
        "I;16" => ["I"],
        _ => [Mode]
    };

    /// <summary>A size and mode record with no pixels, for reporting.</summary>
    public static SourceImage Header(string mode, int width, int height) => new(mode, width, height, []);

    /// <summary>Decodes a file by its content, or returns null when it is missing.</summary>
    public static SourceImage? Open(string path)
    {
        if (string.IsNullOrEmpty(path) || !File.Exists(path)) return null;
        var bytes = File.ReadAllBytes(path);
        if (Png.Matches(bytes)) return Png.Decode(bytes);
        if (Tga.Matches(bytes)) return Tga.Decode(bytes);
        throw new TextureError($"Cannot identify image file: {path}");
    }

    /// <summary>Reads a file's size and band layout without decoding pixels.</summary>
    public static SourceImage ProbeFile(string path)
    {
        var bytes = File.ReadAllBytes(path);
        if (Png.Matches(bytes)) return Png.Probe(bytes);
        if (Tga.Matches(bytes)) return Tga.Probe(bytes);
        throw new TextureError($"Cannot identify image file: {path}");
    }

    /// <summary>Normalizes 16-bit grey to RGB by dropping the low byte, since those modes break a divide by 255.</summary>
    public SourceImage NormalizeDepth()
    {
        if (Mode != "I;16") return this;
        var output = new byte[Width * Height * 3];
        for (var i = 0; i < Width * Height; i++)
        {
            var value = Pixels[i * 2 + 1];
            output[i * 3] = value;
            output[i * 3 + 1] = value;
            output[i * 3 + 2] = value;
        }
        return new("RGB", Width, Height, output);
    }

    /// <summary>The pixels of an L, RGB or RGBA image as an array-shaped buffer.</summary>
    public ByteImage ToBytes() => Mode switch
    {
        "L" => new(Height, Width, 1, Pixels),
        "RGB" => new(Height, Width, 3, Pixels),
        "RGBA" => new(Height, Width, 4, Pixels),
        _ => throw new TextureError($"Mode {Mode} has no direct pixel layout.")
    };

    /// <summary>Converts to RGBA, honouring transparency stored in the palette or as a colour key.</summary>
    public ByteImage ToRgba()
    {
        var output = new ByteImage(Height, Width, 4);
        var target = output.Data;
        for (var i = 0; i < Width * Height; i++)
        {
            var o = i * 4;
            switch (Mode)
            {
                case "RGBA":
                    Array.Copy(Pixels, o, target, o, 4);
                    break;
                case "RGB":
                    target[o] = Pixels[i * 3];
                    target[o + 1] = Pixels[i * 3 + 1];
                    target[o + 2] = Pixels[i * 3 + 2];
                    target[o + 3] = 255;
                    if (TransparentRgb is var (r, g, b) && target[o] == (r & 0xFF) && target[o + 1] == (g & 0xFF) && target[o + 2] == (b & 0xFF)) target[o + 3] = 0;
                    break;
                case "LA":
                    target[o] = target[o + 1] = target[o + 2] = Pixels[i * 2];
                    target[o + 3] = Pixels[i * 2 + 1];
                    break;
                case "P":
                    var index = Pixels[i];
                    target[o] = Palette![index * 3];
                    target[o + 1] = Palette[index * 3 + 1];
                    target[o + 2] = Palette[index * 3 + 2];
                    target[o + 3] = PaletteAlpha is not null && index < PaletteAlpha.Length ? PaletteAlpha[index] : (byte)255;
                    break;
                default:
                    var grey = Pixels[i];
                    target[o] = target[o + 1] = target[o + 2] = grey;
                    target[o + 3] = TransparentGrey is { } key && grey == GreyKey(key) ? (byte)0 : (byte)255;
                    break;
            }
        }
        return output;
    }

    /// <summary>The 8-bit value a stored grey colour key matches. A bilevel image's key is 0 or 1 while its pixels are 0 or 255.</summary>
    public int GreyKey(int key) => Mode == "1" ? (key != 0 ? 255 : 0) : key & 0xFF;

    /// <summary>Converts a palette image to RGB, ignoring its alpha as Pillow does on this path.</summary>
    public ByteImage PaletteToRgb()
    {
        var output = new ByteImage(Height, Width, 3);
        for (var i = 0; i < Width * Height; i++)
        {
            var index = Pixels[i];
            output.Data[i * 3] = Palette![index * 3];
            output.Data[i * 3 + 1] = Palette[index * 3 + 1];
            output.Data[i * 3 + 2] = Palette[index * 3 + 2];
        }
        return output;
    }

    /// <summary>The first band as one channel: grey for grey modes, red for colour modes, sampled through the palette.</summary>
    /// <remarks>Going straight to luminance would average a palette or a two-band image, while every scalar consumer reads channel zero.</remarks>
    public ByteImage FirstChannel()
    {
        var output = new ByteImage(Height, Width, 1);
        switch (Mode)
        {
            case "P":
                for (var i = 0; i < output.Data.Length; i++) output.Data[i] = Palette![Pixels[i] * 3];
                break;
            case "L":
            case "1":
                Pixels.CopyTo(output.Data, 0);
                break;
            default:
                var step = Mode switch { "RGB" => 3, "RGBA" => 4, "LA" => 2, _ => throw new TextureError($"Mode {Mode} has no first channel.") };
                for (var i = 0; i < output.Data.Length; i++) output.Data[i] = Pixels[i * step];
                break;
        }
        return output;
    }
}
