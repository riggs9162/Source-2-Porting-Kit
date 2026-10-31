namespace PortingKit.Addons.Porting;

/// <summary>Raised when a texture cannot be decoded or encoded.</summary>
public sealed class TextureError(string message, Exception? inner = null) : Exception(message, inner);

/// <summary>Interleaved 8-bit pixels, top row first, laid out like a height by width by channels array.</summary>
public sealed class ByteImage(int height, int width, int channels, byte[]? data = null)
{
    public int Height { get; } = height;
    public int Width { get; } = width;
    public int Channels { get; } = channels;
    public byte[] Data { get; } = data ?? new byte[checked(height * width * channels)];

    public byte this[int y, int x, int channel]
    {
        get => Data[(y * Width + x) * Channels + channel];
        set => Data[(y * Width + x) * Channels + channel] = value;
    }

    public ByteImage Copy() => new(Height, Width, Channels, (byte[])Data.Clone());

    /// <summary>One channel as a single-channel image.</summary>
    public ByteImage Channel(int channel)
    {
        var result = new ByteImage(Height, Width, 1);
        for (int i = 0, at = channel; i < result.Data.Length; i++, at += Channels) result.Data[i] = Data[at];
        return result;
    }

    /// <summary>Fills a scanline-ordered RGBA buffer, adding opaque alpha to three channels.</summary>
    public ByteImage ToRgba()
    {
        if (Channels == 4) return this;
        if (Channels != 3) throw new TextureError($"Expected HxWx3 or HxWx4, got {Height}x{Width}x{Channels}");
        var result = new ByteImage(Height, Width, 4);
        for (int i = 0, o = 0; i < Data.Length; i += 3, o += 4)
        {
            result.Data[o] = Data[i];
            result.Data[o + 1] = Data[i + 1];
            result.Data[o + 2] = Data[i + 2];
            result.Data[o + 3] = 255;
        }
        return result;
    }
}

/// <summary>Interleaved single-precision pixels in the zero to one range, laid out like a NumPy float32 array.</summary>
/// <remarks>A one-channel image stands for both of NumPy's two-dimensional and single-band shapes, which every caller treated alike.</remarks>
public sealed class FloatImage(int height, int width, int channels, float[]? data = null)
{
    public int Height { get; } = height;
    public int Width { get; } = width;
    public int Channels { get; } = channels;
    public float[] Data { get; } = data ?? new float[checked(height * width * channels)];

    public float this[int y, int x, int channel]
    {
        get => Data[(y * Width + x) * Channels + channel];
        set => Data[(y * Width + x) * Channels + channel] = value;
    }

    public bool SameSize(int height, int width) => Height == height && Width == width;
    public FloatImage Copy() => new(Height, Width, Channels, (float[])Data.Clone());

    /// <summary>One channel as a single-channel image.</summary>
    public FloatImage Channel(int channel)
    {
        var result = new FloatImage(Height, Width, 1);
        for (int i = 0, at = channel; i < result.Data.Length; i++, at += Channels) result.Data[i] = Data[at];
        return result;
    }

    /// <summary>The first three channels as a new image.</summary>
    public FloatImage Rgb()
    {
        var result = new FloatImage(Height, Width, 3);
        for (int i = 0, o = 0; i < Data.Length; i += Channels, o += 3)
        {
            result.Data[o] = Data[i];
            result.Data[o + 1] = Data[i + 1];
            result.Data[o + 2] = Data[i + 2];
        }
        return result;
    }

    /// <summary>A new image holding every value limited to zero and one.</summary>
    public FloatImage Clipped()
    {
        var result = new FloatImage(Height, Width, Channels);
        for (var i = 0; i < Data.Length; i++) result.Data[i] = Math.Clamp(Data[i], 0f, 1f);
        return result;
    }

    public static FloatImage Filled(int height, int width, float value)
    {
        var result = new FloatImage(height, width, 1);
        Array.Fill(result.Data, value);
        return result;
    }
}
