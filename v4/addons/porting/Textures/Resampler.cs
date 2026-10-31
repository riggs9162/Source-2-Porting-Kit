namespace PortingKit.Addons.Porting;

/// <summary>Fixed-point image scaling that reproduces Pillow's Lanczos and bilinear output exactly.</summary>
/// <remarks>
/// Every band is treated alone, so alpha can never premultiply colour. That is load-bearing: a combined RGBA resize zeroes
/// colour wherever alpha is zero, and bumpmaps carry the Phong mask in alpha, which would turn flat regions black.
/// </remarks>
public static class Resampler
{
    private const int PrecisionBits = 32 - 8 - 2;

    public enum Filter { Bilinear, Lanczos }

    /// <summary>Scales each band separately, horizontally first, as the Pillow core does.</summary>
    public static ByteImage Resize(ByteImage source, int width, int height, Filter filter)
    {
        if (source.Width == width && source.Height == height) return source.Copy();
        var image = source;
        if (width != source.Width)
        {
            var (bounds, weights, size) = Coefficients(source.Width, width, filter);
            var output = new ByteImage(source.Height, width, source.Channels);
            Horizontal(image, output, bounds, weights, size);
            image = output;
        }
        if (height != source.Height)
        {
            var (bounds, weights, size) = Coefficients(source.Height, height, filter);
            var output = new ByteImage(height, width, source.Channels);
            Vertical(image, output, bounds, weights, size);
            image = output;
        }
        return image;
    }

    /// <summary>Scales RGBA as one image with colour premultiplied by alpha, as a combined Pillow resize does.</summary>
    public static ByteImage ResizePremultiplied(ByteImage source, int width, int height, Filter filter)
    {
        if (source.Width == width && source.Height == height) return source.Copy();
        var premultiplied = source.Copy();
        var data = premultiplied.Data;
        for (var i = 0; i < data.Length; i += 4)
        {
            int alpha = data[i + 3];
            data[i] = MulDiv255(data[i], alpha);
            data[i + 1] = MulDiv255(data[i + 1], alpha);
            data[i + 2] = MulDiv255(data[i + 2], alpha);
        }
        var resized = Resize(premultiplied, width, height, filter);
        var pixels = resized.Data;
        for (var i = 0; i < pixels.Length; i += 4)
        {
            int alpha = pixels[i + 3];
            if (alpha is 0 or 255) continue;
            pixels[i] = (byte)Math.Min(255, pixels[i] * 255 / alpha);
            pixels[i + 1] = (byte)Math.Min(255, pixels[i + 1] * 255 / alpha);
            pixels[i + 2] = (byte)Math.Min(255, pixels[i + 2] * 255 / alpha);
        }
        return resized;
    }

    private static byte MulDiv255(int value, int alpha)
    {
        var scaled = value * alpha + 128;
        return (byte)(((scaled >> 8) + scaled) >> 8);
    }

    private static double Weight(double x, Filter filter)
    {
        if (filter == Filter.Bilinear)
        {
            if (x < 0.0) x = -x;
            return x < 1.0 ? 1.0 - x : 0.0;
        }
        return x is >= -3.0 and < 3.0 ? Sinc(x) * Sinc(x / 3) : 0.0;
    }

    private static double Sinc(double x)
    {
        if (x == 0.0) return 1.0;
        x *= Math.PI;
        return Math.Sin(x) / x;
    }

    private static (int[] Bounds, int[] Weights, int Size) Coefficients(int inSize, int outSize, Filter filter)
    {
        var filterScale = (double)inSize / outSize;
        var scale = filterScale;
        if (filterScale < 1.0) filterScale = 1.0;
        var support = (filter == Filter.Bilinear ? 1.0 : 3.0) * filterScale;
        var size = (int)Math.Ceiling(support) * 2 + 1;
        var kernel = new double[outSize * size];
        var bounds = new int[outSize * 2];
        for (var xx = 0; xx < outSize; xx++)
        {
            var center = (xx + 0.5) * scale;
            var total = 0.0;
            var ss = 1.0 / filterScale;
            var xmin = (int)(center - support + 0.5);
            if (xmin < 0) xmin = 0;
            var xmax = (int)(center + support + 0.5);
            if (xmax > inSize) xmax = inSize;
            xmax -= xmin;
            var row = xx * size;
            for (var x = 0; x < xmax; x++)
            {
                var w = Weight((x + xmin - center + 0.5) * ss, filter);
                kernel[row + x] = w;
                total += w;
            }
            if (total != 0.0)
                for (var x = 0; x < xmax; x++) kernel[row + x] /= total;
            bounds[xx * 2] = xmin;
            bounds[xx * 2 + 1] = xmax;
        }
        var weights = new int[kernel.Length];
        for (var i = 0; i < kernel.Length; i++)
            weights[i] = kernel[i] < 0 ? (int)(-0.5 + kernel[i] * (1 << PrecisionBits)) : (int)(0.5 + kernel[i] * (1 << PrecisionBits));
        return (bounds, weights, size);
    }

    private static byte Clip8(int value)
    {
        var shifted = value >> PrecisionBits;
        return shifted < 0 ? (byte)0 : shifted > 255 ? (byte)255 : (byte)shifted;
    }

    private static void Horizontal(ByteImage source, ByteImage output, int[] bounds, int[] weights, int size)
    {
        var channels = source.Channels;
        for (var y = 0; y < source.Height; y++)
        {
            var row = y * source.Width * channels;
            var target = y * output.Width * channels;
            for (var xx = 0; xx < output.Width; xx++)
            {
                var xmin = bounds[xx * 2];
                var xmax = bounds[xx * 2 + 1];
                var kernel = xx * size;
                for (var c = 0; c < channels; c++)
                {
                    var sum = 1 << (PrecisionBits - 1);
                    for (var x = 0; x < xmax; x++) sum += source.Data[row + (x + xmin) * channels + c] * weights[kernel + x];
                    output.Data[target + xx * channels + c] = Clip8(sum);
                }
            }
        }
    }

    private static void Vertical(ByteImage source, ByteImage output, int[] bounds, int[] weights, int size)
    {
        var stride = source.Width * source.Channels;
        for (var yy = 0; yy < output.Height; yy++)
        {
            var ymin = bounds[yy * 2];
            var ymax = bounds[yy * 2 + 1];
            var kernel = yy * size;
            var target = yy * stride;
            for (var i = 0; i < stride; i++)
            {
                var sum = 1 << (PrecisionBits - 1);
                for (var y = 0; y < ymax; y++) sum += source.Data[(y + ymin) * stride + i] * weights[kernel + y];
                output.Data[target + i] = Clip8(sum);
            }
        }
    }
}
