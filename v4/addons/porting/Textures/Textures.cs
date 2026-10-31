namespace PortingKit.Addons.Porting;

/// <summary>Texture decode, transfer functions and VTF encoding: the pixel plumbing every material tool sits on.</summary>
/// <remarks>
/// RGB and alpha are always resized separately. A combined resize premultiplies colour by alpha, which zeroes RGB wherever
/// alpha is zero; bumpmaps carry the Phong mask in alpha and that mask is zero across flat dielectric regions, so a naive resize
/// decodes as solid black in exactly the areas that should be flat tangent normal.
/// </remarks>
public static class Textures
{
    /// <summary>Extensions the material scanners consider to be source textures.</summary>
    public static readonly string[] ImageExtensions = [".png", ".jpg", ".jpeg", ".tga", ".bmp", ".tif", ".tiff"];

    public const uint FlagNormal = Vtf.FlagNormal;

    /// <summary>A sentinel meaning sRGB data, stripped before the flags reach the header.</summary>
    public const uint FlagSrgb = Vtf.FlagSrgb;

    /// <summary>Says the texture's alpha means something. Without it the material system treats alpha as solid white and nothing reports it.</summary>
    public const uint FlagEightBitAlpha = Vtf.FlagEightBitAlpha;

    public const uint FlagOneBitAlpha = Vtf.FlagOneBitAlpha;

    /// <summary>The numeric block size the row-based helpers used; kept because the normal relief bake is defined per block.</summary>
    public const int ChunkRows = 256;

    private static readonly float[] ByteToFloat = BuildByteTable();

    private static float[] BuildByteTable()
    {
        var table = new float[256];
        for (var i = 0; i < 256; i++) table[i] = i / 255f;
        return table;
    }

    /// <summary>The height and width that fit <paramref name="maxEdge"/>, or null when the image already does.</summary>
    /// <remarks>One definition of the downscale arithmetic, so a cap applied at load time lands on the dimensions the same cap gives later.</remarks>
    public static (int Height, int Width)? ScaledSize(int height, int width, int maxEdge)
    {
        if (maxEdge <= 0) return null;
        var longEdge = Math.Max(height, width);
        if (longEdge <= maxEdge) return null;
        var scale = maxEdge / (double)longEdge;
        return (Math.Max(1, (int)Math.Round(height * scale)), Math.Max(1, (int)Math.Round(width * scale)));
    }

    /// <summary>Shrinks an image so its longest edge fits, with colour and alpha resized apart.</summary>
    private static ByteImage Downscale(SourceImage image, int maxEdge)
    {
        var target = ScaledSize(image.Height, image.Width, maxEdge);
        if (target is not var (height, width)) return image.Mode is "L" or "RGB" or "RGBA" ? image.ToBytes() : image.ToRgba();
        var pixels = image.Mode is "L" or "RGB" or "RGBA" ? image.ToBytes() : image.ToRgba();
        if (pixels.Channels != 4) return Resampler.Resize(pixels, width, height, Resampler.Filter.Lanczos);
        var rgb = Resampler.Resize(RgbOf(pixels), width, height, Resampler.Filter.Lanczos);
        var alpha = Resampler.Resize(pixels.Channel(3), width, height, Resampler.Filter.Lanczos);
        return Merge(rgb, alpha);
    }

    private static ByteImage RgbOf(ByteImage rgba)
    {
        var rgb = new ByteImage(rgba.Height, rgba.Width, 3);
        for (int i = 0, o = 0; i < rgba.Data.Length; i += 4, o += 3)
        {
            rgb.Data[o] = rgba.Data[i];
            rgb.Data[o + 1] = rgba.Data[i + 1];
            rgb.Data[o + 2] = rgba.Data[i + 2];
        }
        return rgb;
    }

    private static ByteImage Merge(ByteImage rgb, ByteImage alpha)
    {
        var output = new ByteImage(rgb.Height, rgb.Width, 4);
        for (int i = 0, o = 0; i < alpha.Data.Length; i++, o += 4)
        {
            output.Data[o] = rgb.Data[i * 3];
            output.Data[o + 1] = rgb.Data[i * 3 + 1];
            output.Data[o + 2] = rgb.Data[i * 3 + 2];
            output.Data[o + 3] = alpha.Data[i];
        }
        return output;
    }

    /// <summary>Decodes an image as RGBA in the zero to one range, or null when it is missing.</summary>
    /// <remarks><paramref name="maxEdge"/> caps the longest side while the data is still 8-bit, which is what keeps a batch of 4K materials inside memory.</remarks>
    public static FloatImage? LoadRgba(string? path, int maxEdge = 0)
    {
        var image = string.IsNullOrEmpty(path) ? null : SourceImage.Open(path)?.NormalizeDepth();
        if (image is null) return null;
        ByteImage pixels;
        if (ScaledSize(image.Height, image.Width, maxEdge) is null)
            pixels = image.Mode == "RGB" ? OpaqueRgba(image) : image.ToRgba();
        else
        {
            var scaled = Downscale(image, maxEdge);
            pixels = scaled.Channels == 3 ? scaled.ToRgba() : scaled.Channels == 1 ? GreyToRgba(scaled) : scaled;
        }
        return ToFloat(pixels);
    }

    private static ByteImage OpaqueRgba(SourceImage image) => image.ToBytes().ToRgba();

    private static ByteImage GreyToRgba(ByteImage grey)
    {
        var output = new ByteImage(grey.Height, grey.Width, 4);
        for (var i = 0; i < grey.Data.Length; i++)
        {
            output.Data[i * 4] = output.Data[i * 4 + 1] = output.Data[i * 4 + 2] = grey.Data[i];
            output.Data[i * 4 + 3] = 255;
        }
        return output;
    }

    /// <summary>Decodes a single-channel map (AO, roughness, metallic, mask), or null when it is missing.</summary>
    /// <remarks>
    /// Every consumer reads channel zero anyway, so expanding these to RGBA costs four times the memory for identical values.
    /// Palette images are sampled through the palette; converting straight to luminance would average it instead.
    /// </remarks>
    public static FloatImage? LoadScalar(string? path, int maxEdge = 0)
    {
        var image = string.IsNullOrEmpty(path) ? null : SourceImage.Open(path)?.NormalizeDepth();
        if (image is null) return null;
        var channel = image.FirstChannel();
        if (ScaledSize(channel.Height, channel.Width, maxEdge) is var (height, width))
            channel = Resampler.Resize(channel, width, height, Resampler.Filter.Lanczos);
        return ToFloat(channel);
    }

    /// <summary>Converts 8-bit pixels to floats by the same division the array code performed.</summary>
    public static FloatImage ToFloat(ByteImage pixels)
    {
        var output = new FloatImage(pixels.Height, pixels.Width, pixels.Channels);
        for (var i = 0; i < output.Data.Length; i++) output.Data[i] = ByteToFloat[pixels.Data[i]];
        return output;
    }

    /// <summary>A 4x4 RGBA image filled with a literal.</summary>
    /// <remarks>
    /// Source 2 vmats declare <c>"TextureColor" "[1.0 1.0 1.0 0.0]"</c> when the source used a solid colour. Materialising the
    /// literal lets the rest of the pipeline treat it as an ordinary tiny texture that gets resized up like any other.
    /// </remarks>
    public static FloatImage UniformRgba(IReadOnlyList<double> rgba)
    {
        var values = new float[4];
        for (var i = 0; i < 4; i++) values[i] = i < rgba.Count ? (float)Math.Clamp(rgba[i], 0.0, 1.0) : 1f;
        var image = new FloatImage(4, 4, 4);
        for (var i = 0; i < image.Data.Length; i++) image.Data[i] = values[i % 4];
        return image;
    }

    /// <summary>A 4x4 scalar map filled with a value, the <c>g_fl*</c> equivalent.</summary>
    public static FloatImage UniformScalar(double value) => FloatImage.Filled(4, 4, (float)Math.Clamp(value, 0.0, 1.0));

    /// <summary>A 4x4 tangent-space "no bump" normal: X 0, Y 0, Z 1 stored as 0.5, 0.5, 1.</summary>
    public static FloatImage FlatNormal() => UniformRgba([0.5, 0.5, 1.0, 1.0]);

    /// <summary>Resizes image data to exactly the given size through 8 bits, or passes it through when it already matches.</summary>
    /// <remarks>RGBA is split so alpha cannot premultiply the colour.</remarks>
    public static FloatImage? Fit(FloatImage? data, int height, int width)
    {
        if (data is null || data.SameSize(height, width)) return data;
        if (data.Channels is not (1 or 3 or 4)) throw new TextureError($"Unsupported channel count: {data.Channels}");
        var source = new ByteImage(data.Height, data.Width, data.Channels);
        for (var i = 0; i < source.Data.Length; i++) source.Data[i] = (byte)(Math.Clamp(data.Data[i], 0f, 1f) * 255f);
        if (data.Channels != 4) return ToFloat(Resampler.Resize(source, width, height, Resampler.Filter.Lanczos));
        var rgb = Resampler.Resize(RgbOf(source), width, height, Resampler.Filter.Lanczos);
        var alpha = Resampler.Resize(source.Channel(3), width, height, Resampler.Filter.Lanczos);
        return ToFloat(Merge(rgb, alpha));
    }

    /// <summary>Promotes a one or three-channel image to RGBA.</summary>
    public static FloatImage AsRgba(FloatImage data)
    {
        var values = data.Clipped();
        if (values.Channels >= 4) return values;
        var output = new FloatImage(values.Height, values.Width, 4);
        for (var i = 0; i < values.Height * values.Width; i++)
        {
            var o = i * 4;
            if (values.Channels == 1) output.Data[o] = output.Data[o + 1] = output.Data[o + 2] = values.Data[i];
            else
            {
                output.Data[o] = values.Data[i * 3];
                output.Data[o + 1] = values.Data[i * 3 + 1];
                output.Data[o + 2] = values.Data[i * 3 + 2];
            }
            output.Data[o + 3] = 1f;
        }
        return output;
    }

    /// <summary>Downscales a float buffer to a cap, or passes it through untouched.</summary>
    public static FloatImage? ClampFloat(FloatImage? data, int maxEdge)
    {
        if (data is null || maxEdge <= 0) return data;
        return ScaledSize(data.Height, data.Width, maxEdge) is var (height, width) ? Fit(data, height, width) : data;
    }

    /// <summary>Downscales an 8-bit output buffer so its longest edge fits.</summary>
    /// <remarks>Bilinear rather than Lanczos: this runs on already-baked masks where ringing would push values outside the 8-bit range.</remarks>
    public static ByteImage? ClampBytes(ByteImage? data, int maxEdge)
    {
        if (data is null || maxEdge <= 0) return data;
        if (ScaledSize(data.Height, data.Width, maxEdge) is not var (height, width)) return data;
        if (data.Channels != 4) return Resampler.Resize(data, width, height, Resampler.Filter.Bilinear);
        var rgb = Resampler.Resize(RgbOf(data), width, height, Resampler.Filter.Bilinear);
        var alpha = Resampler.Resize(data.Channel(3), width, height, Resampler.Filter.Bilinear);
        return Merge(rgb, alpha);
    }

    /// <summary>sRGB to linear, in place.</summary>
    public static void SrgbToLinear(float[] values)
    {
        for (var i = 0; i < values.Length; i++)
        {
            var value = values[i];
            values[i] = value > 0.04045f ? MathF.Pow((value + 0.055f) / 1.055f, 2.4f) : value / 12.92f;
        }
    }

    /// <summary>Linear to sRGB, in place.</summary>
    public static void LinearToSrgb(float[] values)
    {
        const float Exponent = (float)(1.0 / 2.4);
        for (var i = 0; i < values.Length; i++)
        {
            var value = values[i];
            values[i] = value > 0.0031308f ? MathF.Pow(value, Exponent) * 1.055f - 0.055f : value * 12.92f;
        }
    }

    /// <summary>Clips to the zero to one range and truncates to 8 bits.</summary>
    public static ByteImage ToBytes(FloatImage data)
    {
        var output = new ByteImage(data.Height, data.Width, data.Channels);
        for (var i = 0; i < output.Data.Length; i++) output.Data[i] = (byte)(Math.Clamp(data.Data[i], 0f, 1f) * 255f);
        return output;
    }

    /// <summary>Rec.709 luminance of a linear RGB pixel, clipped.</summary>
    public static float Luminance(float r, float g, float b) => Math.Clamp(0.2126f * r + 0.7152f * g + 0.0722f * b, 0f, 1f);

    /// <summary>Per-pixel colourfulness: brightest channel minus darkest.</summary>
    public static float Chroma(float r, float g, float b) => Math.Clamp(MathF.Max(r, MathF.Max(g, b)) - MathF.Min(r, MathF.Min(g, b)), 0f, 1f);

    /// <summary>Marks alpha worth reading.</summary>
    /// <remarks>
    /// Content decides it rather than the format: a DXT5 whose alpha is solid white carries no information, and claiming otherwise
    /// invites the engine to read a channel that says nothing. A format that cannot hold alpha, like DXT1, never claims it.
    /// </remarks>
    public static uint AlphaFlag(ByteImage pixels, VtfFormat format)
    {
        if (!Vtf.HasAlpha(format) || pixels.Channels < 4) return 0;
        for (var i = 3; i < pixels.Data.Length; i += 4)
            if (pixels.Data[i] < 255) return FlagEightBitAlpha;
        return 0;
    }

    /// <summary>Decodes a VTF's largest mip as RGBA plus its header facts.</summary>
    public static (ByteImage Pixels, Vtf Texture) DecodeVtf(string path)
    {
        var texture = Vtf.Read(path);
        return (texture.Decode(0), texture);
    }

    /// <summary>Fits DXT alpha storage without changing decoded pixels, using the fOptimizer alpha rules.</summary>
    /// <remarks>
    /// Fully zero alpha may store a shader mask, so it is preserved. All mips participate in the decision. Opaque data can drop to
    /// DXT1 and binary alpha to one-bit DXT1, but only if every decoded pixel of every mip survives the re-encode: binary
    /// transparency that would crush RGB in a packed shader mask therefore keeps its original storage. Uncompressed data textures
    /// are never converted.
    /// </remarks>
    public static Vtf OptimiseDxt(Vtf texture)
    {
        if (texture.Format is not (VtfFormat.DXT5 or VtfFormat.DXT3 or VtfFormat.DXT1_ONE_BIT_ALPHA)) return texture;
        var binary = false;
        var decoded = new byte[texture.MipCount][];
        for (var mip = 0; mip < texture.MipCount; mip++)
        {
            if (!texture.CanDecode(mip)) return texture;
            decoded[mip] = texture.Decode(mip).Data;
            bool anyZero = false, anyPartial = false, allZero = true;
            for (var i = 3; i < decoded[mip].Length; i += 4)
            {
                var alpha = decoded[mip][i];
                anyZero |= alpha == 0;
                anyPartial |= alpha is > 0 and < 255;
                allZero &= alpha == 0;
            }
            if (allZero || anyPartial) return texture;
            binary |= anyZero;
        }
        var target = binary ? VtfFormat.DXT1_ONE_BIT_ALPHA : VtfFormat.DXT1;
        if (target == texture.Format) return texture;
        var candidate = texture.ConvertExact(target, decoded);
        if (candidate is null) return texture;
        candidate.Flags = (candidate.Flags & ~(FlagOneBitAlpha | FlagEightBitAlpha)) | (binary ? FlagOneBitAlpha : 0);
        return candidate;
    }

    /// <summary>Writes three or four-channel 8-bit pixels as a VTF 7.4.</summary>
    public static string EncodeVtf(ByteImage pixels, string outputPath, VtfFormat format = VtfFormat.DXT5, uint flags = 0, bool invertGreen = false, bool mipmaps = true)
    {
        if (pixels.Channels is not (3 or 4)) throw new TextureError($"Expected HxWx3 or HxWx4, got {pixels.Height}x{pixels.Width}x{pixels.Channels}");
        var rgba = pixels.ToRgba();
        var data = rgba.Data;
        if (invertGreen)
        {
            data = (byte[])data.Clone();
            for (var i = 1; i < data.Length; i += 4) data[i] = (byte)(255 - data[i]);
        }
        var name = Path.GetFileName(outputPath);
        try
        {
            var header = (flags & ~FlagSrgb) | AlphaFlag(rgba, format);
            OptimiseDxt(Vtf.Create(data, rgba.Width, rgba.Height, format, header, mipmaps)).Save(outputPath);
        }
        catch (Exception exception) when (exception is not TextureError)
        {
            throw new TextureError($"Encoding {name}: {exception.Message}", exception);
        }
        if (!File.Exists(outputPath)) throw new TextureError($"Encoder produced no file: {outputPath}");
        return outputPath;
    }

    /// <summary>Re-encodes an existing VTF at a new size, keeping its format and flags.</summary>
    public static void ResizeVtf(string path, int width, int height)
    {
        var texture = Vtf.Read(path);
        var resized = Resampler.ResizePremultiplied(texture.Decode(0), width, height, Resampler.Filter.Lanczos);
        OptimiseDxt(Vtf.Create(resized.Data, width, height, texture.Format, texture.Flags, true)).Save(path);
    }

    /// <summary>Writes 8-bit greyscale, RGB or RGBA pixels as a PNG.</summary>
    public static string SavePng(ByteImage pixels, string outputPath)
    {
        var directory = Path.GetDirectoryName(outputPath);
        if (!string.IsNullOrEmpty(directory)) Directory.CreateDirectory(directory);
        File.WriteAllBytes(outputPath, Png.Encode(pixels));
        return outputPath;
    }

    /// <summary>Writes a scalar map as an 8-bit greyscale PNG, rounding rather than truncating.</summary>
    public static string SaveScalarPng(FloatImage values, string outputPath)
    {
        var bytes = new ByteImage(values.Height, values.Width, 1);
        for (var i = 0; i < bytes.Data.Length; i++) bytes.Data[i] = (byte)(Math.Clamp(values.Data[i * values.Channels], 0f, 1f) * 255f + 0.5f);
        return SavePng(bytes, outputPath);
    }
}
