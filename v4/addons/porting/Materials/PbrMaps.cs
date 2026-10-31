using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>Averages the VMT writer uses to pick Phong and envmap parameters.</summary>
public sealed record MaterialStats(double AvgRoughness, double AvgMetallic, double AvgSmoothness, bool HasMetal, bool HasReflective, bool IsRoughDielectric);

/// <summary>Bakes PBR inputs into the channel layout the stock Source 1 shaders sample.</summary>
/// <remarks>
/// Source 2 authors albedo, normal, roughness, metallic and AO as separate maps for a physically based shader. Source 1 has no metallic
/// workflow: it has Phong, a cubemap and a rim term, each reading one channel of one texture. <c>_color</c> holds albedo with AO
/// baked in and metal darkened, alpha carrying opacity or tint mask. <c>_normal</c> holds the tangent normal with the Phong mask in
/// alpha. <c>_phong</c> holds the exponent in R and metallic in G. <c>_envmask</c> holds a coloured reflection tint. The approximation
/// is not energy-conserving and never can be, because Source 1 lights diffuse and specular independently, so the metal split is
/// faked by darkening albedo and pushing the difference into Phong and the cubemap. All arithmetic is single precision in the same
/// order as the array code, so the 8-bit results are identical.
/// </remarks>
public static partial class PbrMaps
{
    /// <summary>Floor used when dividing by linear albedo luminance, so dark pixels do not blow up the compensated mask. It matches the dielectric baseline.</summary>
    public const float TintDivideFloor = 0.04f;

    private const int SumBlock = 128;

    private static readonly HashSet<string> OpenglTokens = ["opengl", "gl", "ogl", "normalgl", "normal_opengl", "normalopengl", "yup", "y+", "plusy"];
    private static readonly HashSet<string> DirectxTokens = ["directx", "dx", "normaldx", "normal_dx", "normaldirectx", "ydown", "y-", "minusy"];

    [GeneratedRegex(@"[\s._\-]+")]
    private static partial Regex TokenSplit();

    /// <summary>A clamped scalar map of exactly the given size.</summary>
    /// <remarks>Missing data becomes a constant, present data is reduced to its first channel and resized when needed. Honouring the size contract here is what lets a 2048px roughness sit next to a 4096px metallic.</remarks>
    public static FloatImage ScalarMap(FloatImage? data, int height, int width, float fallback)
    {
        if (data is null) return FloatImage.Filled(height, width, fallback);
        var values = (data.Channels > 1 ? data.Channel(0) : data).Clipped();
        return values.SameSize(height, width) ? values : Textures.Fit(values, height, width)!;
    }

    /// <summary>How strongly each pixel is marked as glowing, from a self-illumination mask.</summary>
    /// <remarks>The strongest colour channel rather than luminance: a saturated red glow is fully emissive though it carries only 0.21 of luminance, and the question is whether the artist marked the pixel, not how bright it reads.</remarks>
    public static FloatImage? EmissiveWeight(FloatImage? selfillum, int height, int width)
    {
        if (selfillum is null) return null;
        if (selfillum.Channels < 3) return ScalarMap(selfillum, height, width, 0f);
        var weight = new FloatImage(selfillum.Height, selfillum.Width, 1);
        for (var i = 0; i < weight.Data.Length; i++)
        {
            var at = i * selfillum.Channels;
            weight.Data[i] = MathF.Max(Math.Clamp(selfillum.Data[at], 0f, 1f), MathF.Max(Math.Clamp(selfillum.Data[at + 1], 0f, 1f), Math.Clamp(selfillum.Data[at + 2], 0f, 1f)));
        }
        return weight.SameSize(height, width) ? weight : Textures.Fit(weight, height, width);
    }

    /// <summary>Lifts AO back towards unoccluded wherever the material glows.</summary>
    /// <remarks>
    /// AO says how much ambient light reaches a surface, and an emissive panel makes its own, so baking the crevice around a glowing strip
    /// over the strip dims it for a reason that does not apply. <paramref name="amount"/> is how much occlusion to take back on a fully
    /// emissive pixel; partial pixels get a proportional share so the relief fades out with the mask. Returns the input unchanged when
    /// there is nothing to relieve.
    /// </remarks>
    public static FloatImage? RelieveAo(FloatImage? ao, FloatImage? selfillum, int height, int width, double amount = 1.0)
    {
        amount = Math.Clamp(amount, 0.0, 1.0);
        if (ao is null || amount <= 0.0) return ao;
        var weight = EmissiveWeight(selfillum, height, width);
        if (weight is null) return ao;
        var values = ScalarMap(ao, height, width, 1f);
        var factor = (float)amount;
        for (var i = 0; i < values.Data.Length; i++) values.Data[i] += (1f - values.Data[i]) * (factor * weight.Data[i]);
        return values;
    }

    /// <summary>Blurs a scalar map with a circular aperture kernel.</summary>
    public static FloatImage BokehBlur(FloatImage value, int radius = 3)
    {
        radius = Math.Max(1, radius);
        var offsets = new List<(int Y, int X)>();
        for (var y = -radius; y <= radius; y++)
            for (var x = -radius; x <= radius; x++)
                if (x * x + y * y <= radius * radius) offsets.Add((y, x));
        var weight = 1f / offsets.Count;
        var result = new FloatImage(value.Height, value.Width, 1);
        var rows = new int[value.Height + 2 * radius];
        for (var i = 0; i < rows.Length; i++) rows[i] = Math.Clamp(i - radius, 0, value.Height - 1);
        var columns = new int[value.Width + 2 * radius];
        for (var i = 0; i < columns.Length; i++) columns[i] = Math.Clamp(i - radius, 0, value.Width - 1);
        foreach (var (dy, dx) in offsets)
            for (var y = 0; y < value.Height; y++)
            {
                var source = rows[y + dy + radius] * value.Width;
                var target = y * value.Width;
                for (var x = 0; x < value.Width; x++) result.Data[target + x] += weight * value.Data[source + columns[x + dx + radius]];
            }
        return result;
    }

    /// <summary>Single-precision pairwise summation, the order NumPy's mean uses, so averages match to the last bit.</summary>
    private static float PairwiseSum(float[] data, int start, int count)
    {
        if (count < 8)
        {
            var result = -0f;
            for (var i = 0; i < count; i++) result += data[start + i];
            return result;
        }
        if (count <= SumBlock)
        {
            Span<float> r = stackalloc float[8];
            for (var i = 0; i < 8; i++) r[i] = data[start + i];
            int at;
            for (at = 8; at < count - count % 8; at += 8)
                for (var i = 0; i < 8; i++) r[i] += data[start + at + i];
            var result = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
            for (; at < count; at++) result += data[start + at];
            return result;
        }
        var half = count / 2;
        half -= half % 8;
        return PairwiseSum(data, start, half) + PairwiseSum(data, start + half, count - half);
    }

    private static double Mean(float[] data) => (float)(0f + PairwiseSum(data, 0, data.Length)) / (float)data.Length;

    /// <summary>Reduces the specular inputs to the handful of numbers the VMT needs.</summary>
    public static MaterialStats Stats(FloatImage? roughness, FloatImage? metallic, int height, int width)
    {
        var rough = ScalarMap(roughness, height, width, 0.5f);
        var metal = ScalarMap(metallic, height, width, 0f);
        var averageRoughness = Mean(rough.Data);
        var averageMetallic = Mean(metal.Data);
        var hasMetal = false;
        var hasReflective = false;
        for (var i = 0; i < rough.Data.Length; i++)
        {
            hasMetal |= metal.Data[i] > 0.05f;
            hasReflective |= (1f - rough.Data[i]) * (0.04f + 0.96f * metal.Data[i]) > 0.04f;
        }
        return new(averageRoughness, averageMetallic, 1.0 - averageRoughness, hasMetal, hasReflective, averageMetallic < 0.05 && averageRoughness > 0.75);
    }

    /// <summary>Samples signed tangent normals bilinearly, without gamma or alpha mixing, at the output size.</summary>
    private static float[] NormalDetailRows(FloatImage normal, int height, int width)
    {
        var vectors = new float[height * width * 3];
        if (normal.SameSize(height, width))
        {
            for (var i = 0; i < height * width; i++)
                for (var c = 0; c < 3; c++) vectors[i * 3 + c] = normal.Data[i * normal.Channels + c] * 2f - 1f;
            return vectors;
        }
        int sourceHeight = normal.Height, sourceWidth = normal.Width;
        var x = new float[width];
        var fx = new float[width];
        var x0 = new int[width];
        var x1 = new int[width];
        var xScale = (float)((double)sourceWidth / width);
        for (var i = 0; i < width; i++)
        {
            x[i] = Math.Clamp((i + 0.5f) * xScale - 0.5f, 0f, sourceWidth - 1);
            x0[i] = (int)x[i];
            x1[i] = Math.Min(x0[i] + 1, sourceWidth - 1);
            fx[i] = x[i] - x0[i];
        }
        var yScale = (float)((double)sourceHeight / height);
        for (var row = 0; row < height; row++)
        {
            var y = Math.Clamp((row + 0.5f) * yScale - 0.5f, 0f, sourceHeight - 1);
            var y0 = (int)y;
            var y1 = Math.Min(y0 + 1, sourceHeight - 1);
            var fy = y - y0;
            for (var column = 0; column < width; column++)
                for (var c = 0; c < 3; c++)
                {
                    var top = normal[y0, x0[column], c] * (1f - fx[column]);
                    top += normal[y0, x1[column], c] * fx[column];
                    var bottom = normal[y1, x0[column], c] * (1f - fx[column]);
                    bottom += normal[y1, x1[column], c] * fx[column];
                    top *= 1f - fy;
                    top += bottom * fy;
                    vectors[(row * width + column) * 3 + c] = top * 2f - 1f;
                }
        }
        return vectors;
    }

    /// <summary>Multiplies linear RGB in place by fixed, flat-normal-neutral relief shading.</summary>
    /// <remarks>A tangent-space key at (-1, -1, 2) and equal ambient fill give subtle detail without baking specular highlights. Emissive pixels suppress the effect.</remarks>
    public static void ApplyNormalDetail(FloatImage linear, FloatImage? normal, double strength = 0.25, bool invertGreen = false, FloatImage? selfillum = null)
    {
        strength = Math.Clamp(strength, 0.0, 1.0);
        if (normal is null || strength <= 0.0 || IsFlatNormal(normal)) return;
        int height = linear.Height, width = linear.Width;
        var glow = EmissiveWeight(selfillum, height, width);
        var root = MathF.Sqrt(6f);
        float lightX = -1f / root, lightY = -1f / root, lightZ = 2f / root;
        var vectors = NormalDetailRows(normal, height, width);
        var factor = (float)strength;
        const float Tolerance = 4f / 255f;
        for (var i = 0; i < height * width; i++)
        {
            var x = Math.Clamp(vectors[i * 3], -1f, 1f);
            var y = Math.Clamp(vectors[i * 3 + 1], -1f, 1f);
            var z = Math.Clamp(vectors[i * 3 + 2], -1f, 1f);
            if (invertGreen) y *= -1f;
            var length = MathF.Sqrt(0f + x * x + y * y + z * z);
            var invalid = !float.IsFinite(length) || length <= 1e-6f;
            if (invalid) length = 1f;
            x /= length;
            y /= length;
            z /= length;
            if (invalid || (MathF.Abs(x) < Tolerance && MathF.Abs(y) < Tolerance && MathF.Abs(z - 1f) < Tolerance))
            {
                x = 0f;
                y = 0f;
                z = 1f;
            }
            var diffuse = MathF.Max(0f + x * lightX + y * lightY + z * lightZ, 0f);
            var shade = (0.5f + 0.5f * diffuse) / (0.5f + 0.5f * lightZ) - 1f;
            shade *= factor;
            if (glow is not null) shade *= 1f - glow.Data[i];
            var scale = 1f + shade;
            linear.Data[i * 3] *= scale;
            linear.Data[i * 3 + 1] *= scale;
            linear.Data[i * 3 + 2] *= scale;
        }
    }

    /// <summary>Linear RGB from the colour channels of an image.</summary>
    private static FloatImage LinearColour(FloatImage color)
    {
        var rgb = color.Rgb().Clipped();
        Textures.SrgbToLinear(rgb.Data);
        return rgb;
    }

    private static float[] Chromas(FloatImage linear)
    {
        var values = new float[linear.Height * linear.Width];
        for (var i = 0; i < values.Length; i++) values[i] = Textures.Chroma(linear.Data[i * 3], linear.Data[i * 3 + 1], linear.Data[i * 3 + 2]);
        return values;
    }

    /// <summary>Bakes the <c>_color</c> basetexture: RGB with AO and metal darkening, plus alpha.</summary>
    /// <remarks>
    /// <para>RGB is AO-multiplied and darkened where the surface is metallic, because Source 1 has no metallic energy split and a metal
    /// keeping its full albedo reads as bright plastic.</para>
    /// <para>Alpha carries, in priority order, translucency so $translucent and $alphatest see the authored opacity, then the tint mask
    /// for opaque colourable props via $blendtintbybasealpha, then the colour texture's own alpha or opaque. Transparency always wins
    /// the channel, because Source 1 cannot honour both a blend and a masked tint. <paramref name="tintAmount"/> scales the mask.</para>
    /// <para><paramref name="coloredMetalRelief"/> reduces suppression on chromatic pixels so gold and copper keep more albedo, while
    /// grayscale metals always take the full factor. <paramref name="normalDetail"/> optionally bakes fixed relief into linear RGB, with
    /// alpha untouched and the self-illumination mask suppressing it.</para>
    /// </remarks>
    public static ByteImage BakeBasetexture(FloatImage color, FloatImage? ao, FloatImage? metallic, double aoStrength = 0.7, double aoBokehStrength = 0.35,
        double metalDiffuseSuppression = MaterialDefaults.MetalDiffuseSuppression, double coloredMetalRelief = MaterialDefaults.ColoredMetalRelief,
        FloatImage? translucency = null, FloatImage? tintmask = null, double? tintAmount = null, FloatImage? normalDetail = null,
        double normalDetailStrength = 0.25, bool invertNormalGreen = false, FloatImage? selfillum = null)
    {
        var linear = LinearColour(color);
        int height = linear.Height, width = linear.Width, pixels = height * width;
        var aoValues = ScalarMap(ao, height, width, 1f);
        var metalValues = ScalarMap(metallic, height, width, 0f);
        var bokehStrength = Math.Clamp(aoBokehStrength, 0.0, 1.0);
        if (bokehStrength > 0.0)
        {
            var soft = BokehBlur(aoValues);
            var keep = (float)(1.0 - bokehStrength);
            var blend = (float)bokehStrength;
            for (var i = 0; i < pixels; i++) aoValues.Data[i] = aoValues.Data[i] * keep + soft.Data[i] * blend;
        }
        var exponent = (float)aoStrength;
        for (var i = 0; i < pixels; i++)
        {
            var occlusion = MathF.Pow(aoValues.Data[i], exponent);
            linear.Data[i * 3] *= occlusion;
            linear.Data[i * 3 + 1] *= occlusion;
            linear.Data[i * 3 + 2] *= occlusion;
        }
        var relief = (float)Math.Clamp(coloredMetalRelief, 0.0, 1.0);
        var suppression = (float)metalDiffuseSuppression;
        var chroma = relief > 0f ? Chromas(linear) : null;
        for (var i = 0; i < pixels; i++)
        {
            var amount = chroma is null ? suppression : suppression * (1f - relief * chroma[i]);
            var keep = 1f - amount * metalValues.Data[i];
            linear.Data[i * 3] *= keep;
            linear.Data[i * 3 + 1] *= keep;
            linear.Data[i * 3 + 2] *= keep;
        }
        ApplyNormalDetail(linear, normalDetail, normalDetailStrength, invertNormalGreen, selfillum);
        Textures.LinearToSrgb(linear.Data);
        var output = new ByteImage(height, width, 4);
        FloatImage? alpha = null;
        if (translucency is not null) alpha = ScalarMap(translucency, height, width, 1f);
        else if (tintmask is not null)
        {
            alpha = ScalarMap(tintmask, height, width, 0f);
            var amount = tintAmount is null ? 1.0 : Math.Clamp(tintAmount.Value, 0.0, 1.0);
            if (amount < 1.0)
                for (var i = 0; i < pixels; i++) alpha.Data[i] *= (float)amount;
        }
        for (var i = 0; i < pixels; i++)
        {
            output.Data[i * 4] = Quantize(linear.Data[i * 3]);
            output.Data[i * 4 + 1] = Quantize(linear.Data[i * 3 + 1]);
            output.Data[i * 4 + 2] = Quantize(linear.Data[i * 3 + 2]);
            var value = alpha is not null ? alpha.Data[i] : color.Channels >= 4 ? color.Data[i * color.Channels + 3] : 1f;
            output.Data[i * 4 + 3] = Quantize(value);
        }
        return output;
    }

    private static byte Quantize(float value) => (byte)(Math.Clamp(value, 0f, 1f) * 255f);

    /// <summary>The bumpmap alpha that drives $phong.</summary>
    /// <remarks>
    /// <para>The base formula is Fresnel-ish: smooth metals reflect strongly, dielectrics keep a 0.04 floor. <paramref name="strength"/>
    /// scales the result; the unscaled mask reads far too hot in engine, and zero disables Phong.</para>
    /// <para><paramref name="tintMode"/> compensates for $phongalbedotint, where the engine multiplies Phong by the basetexture at runtime.
    /// <c>off</c> does nothing. <c>selective</c> gives only coloured metallic pixels divide-by-luminance compensation and suppresses
    /// dielectrics towards zero so their specular does not pick up an albedo cast, the cubemap providing their reflection instead.
    /// <c>blanket</c> divides everywhere. When compensating, the suppression values must match the basetexture bake, otherwise the
    /// division is against a basetexture the engine never samples.</para>
    /// </remarks>
    public static FloatImage PhongMask(FloatImage? roughness, FloatImage? metallic, FloatImage? ao, int height, int width, double strength = 0.5, FloatImage? color = null,
        string tintMode = "off", double metalDiffuseSuppression = MaterialDefaults.MetalDiffuseSuppression, double coloredMetalRelief = MaterialDefaults.ColoredMetalRelief)
    {
        var aoValues = ScalarMap(ao, height, width, 1f);
        var rough = ScalarMap(roughness, height, width, 0.5f);
        var metal = ScalarMap(metallic, height, width, 0f);
        var mask = new FloatImage(height, width, 1);
        var pixels = height * width;
        for (var i = 0; i < pixels; i++) mask.Data[i] = (1f - rough.Data[i]) * (0.04f + 0.96f * metal.Data[i]) + 0.04f * (1f - metal.Data[i]);
        var mode = string.IsNullOrEmpty(tintMode) ? "off" : tintMode.ToLowerInvariant();
        if (mode != "off" && color is not null)
        {
            var linear = LinearColour(color);
            var relief = (float)Math.Clamp(coloredMetalRelief, 0.0, 1.0);
            var chroma = relief > 0f || mode == "selective" ? Chromas(linear) : null;
            var suppression = (float)metalDiffuseSuppression;
            for (var i = 0; i < pixels; i++)
            {
                var amount = relief > 0f ? suppression * (1f - relief * chroma![i]) : suppression;
                var keep = 1f - amount * metal.Data[i];
                var luma = MathF.Max(Textures.Luminance(linear.Data[i * 3] * keep, linear.Data[i * 3 + 1] * keep, linear.Data[i * 3 + 2] * keep), TintDivideFloor);
                var compensated = mask.Data[i] / luma;
                if (mode == "selective")
                {
                    var weight = Math.Clamp(metal.Data[i] * chroma![i], 0f, 1f);
                    var dielectric = (1f - rough.Data[i]) * (0.04f + 0.96f * metal.Data[i]) * metal.Data[i];
                    mask.Data[i] = dielectric * (1f - weight) + compensated * weight;
                }
                else mask.Data[i] = compensated;
            }
        }
        var scale = (float)Math.Max(0.0, strength);
        for (var i = 0; i < pixels; i++) mask.Data[i] = Math.Clamp(mask.Data[i] * aoValues.Data[i] * scale, 0f, 1f);
        return mask;
    }

    /// <summary>Reads a normal map's stated convention off its filename, returning opengl, directx or null.</summary>
    /// <remarks>Only an explicit token counts. A bare <c>_normal</c> is not a claim, and guessing from pixels is not reliable, because a normal map's green channel looks equally plausible either way unless you already know which way the surface bulges.</remarks>
    public static string? NormalConvention(string? source)
    {
        if (source is null) return null;
        var tokens = TokenSplit().Split(Path.GetFileNameWithoutExtension(source).ToLowerInvariant()).Where(token => token.Length > 0).ToArray();
        if (tokens.Any(DirectxTokens.Contains)) return "directx";
        return tokens.Any(OpenglTokens.Contains) ? "opengl" : null;
    }

    /// <summary>Whether to flip the green channel, for a mode and this source file.</summary>
    /// <remarks>
    /// <c>flip</c> always, <c>keep</c> never, <c>auto</c> flips unless the filename says the map is already in Source 1's convention.
    /// Per the Valve Developer Community, Source takes the DirectX format and Source 2 the OpenGL format. Source 2's VR Standard shader
    /// takes OpenGL +Y, and its own texture compiler calls the other setting "Legacy Source 1 inverted normals". So a Source 2 export is
    /// green-up and Source 1 wants green-down: porting inverts green, always, or every bump reads inside out. The exception is a map
    /// labelled <c>_normal_dx</c>, already in Source 1's convention. Several third-party blogs state this backwards; do not trust them.
    /// </remarks>
    public static bool ShouldInvertGreen(string mode, string? source = null)
    {
        mode = string.IsNullOrEmpty(mode) ? "auto" : mode.ToLowerInvariant();
        if (mode == "flip") return true;
        if (mode == "keep") return false;
        return NormalConvention(source) != "directx";
    }

    /// <summary>Packs the <c>_normal</c> bumpmap: RGB tangent normal, alpha Phong mask.</summary>
    /// <remarks>With <paramref name="includePhong"/> off the alpha is left opaque, a well-formed VTF making no specular claim.</remarks>
    public static ByteImage BakeBumpmap(FloatImage normal, FloatImage? ao, FloatImage? metallic, FloatImage? roughness, bool invertGreen = false, bool includePhong = true,
        double phongStrength = 0.5, FloatImage? color = null, string tintMode = "off", double metalDiffuseSuppression = MaterialDefaults.MetalDiffuseSuppression,
        double coloredMetalRelief = MaterialDefaults.ColoredMetalRelief)
    {
        int height = normal.Height, width = normal.Width;
        var alpha = includePhong ? PhongMask(roughness, metallic, ao, height, width, phongStrength, color, tintMode, metalDiffuseSuppression, coloredMetalRelief) : null;
        var output = new ByteImage(height, width, 4);
        for (var i = 0; i < height * width; i++)
        {
            var at = i * normal.Channels;
            var green = Math.Clamp(normal.Data[at + 1], 0f, 1f);
            if (invertGreen) green = 1f - green;
            output.Data[i * 4] = Quantize(normal.Data[at]);
            output.Data[i * 4 + 1] = Quantize(green);
            output.Data[i * 4 + 2] = Quantize(normal.Data[at + 2]);
            output.Data[i * 4 + 3] = alpha is null ? (byte)255 : Quantize(alpha.Data[i]);
        }
        return output;
    }

    /// <summary>The <c>_phong</c> exponent texture: R exponent, G metallic, B zero, A one.</summary>
    /// <remarks>
    /// R is (1 - roughness) ^ gamma scaled by the Phong strength, so a lower strength yields a softer response rather than only a dimmer
    /// mask. There is no AO here on purpose: the exponent controls highlight tightness, and occluding a highlight by broadening it is
    /// not what AO means. AO acts on the Phong mask instead, which controls strength.
    /// </remarks>
    public static ByteImage BakePhongExponent(FloatImage? roughness, FloatImage? metallic, int height, int width, double glossGamma = 2.0, double phongStrength = 0.5)
    {
        var rough = ScalarMap(roughness, height, width, 0.5f);
        var metal = ScalarMap(metallic, height, width, 0f);
        var output = new ByteImage(height, width, 4);
        var scale = (float)Math.Max(0.0, phongStrength);
        for (var i = 0; i < height * width; i++)
        {
            var exponent = Math.Clamp(Power(Math.Clamp(1f - rough.Data[i], 0f, 1f), glossGamma) * scale, 0f, 1f);
            output.Data[i * 4] = Quantize(exponent);
            output.Data[i * 4 + 1] = Quantize(metal.Data[i]);
            output.Data[i * 4 + 3] = 255;
        }
        return output;
    }

    private static float Power(float value, double exponent) => exponent == 2.0 ? value * value : MathF.Pow(value, (float)exponent);

    /// <summary>The <c>_envmask</c> texture: a coloured $envmapmask for metal tint fakery.</summary>
    /// <remarks>Reflection colour is mix(0.04, albedo, metallic), the dielectric baseline for non-metals and the albedo itself for metals, which is what makes gold reflect gold, scaled by smoothness and AO.</remarks>
    public static ByteImage BakeEnvmapMask(FloatImage color, FloatImage? ao, FloatImage? metallic, FloatImage? roughness, double envmaskGamma = 1.5)
    {
        var mask = LinearColour(color);
        int height = mask.Height, width = mask.Width;
        var aoValues = ScalarMap(ao, height, width, 1f);
        var rough = ScalarMap(roughness, height, width, 0.5f);
        var metal = ScalarMap(metallic, height, width, 0f);
        var exponent = (float)envmaskGamma;
        var output = new ByteImage(height, width, 4);
        for (var i = 0; i < height * width; i++)
        {
            var gloss = Power(Math.Clamp(1f - rough.Data[i], 0f, 1f), exponent);
            for (var c = 0; c < 3; c++)
            {
                var value = mask.Data[i * 3 + c];
                value -= 0.04f;
                value *= metal.Data[i];
                value += 0.04f;
                value *= gloss;
                value *= aoValues.Data[i];
                output.Data[i * 4 + c] = Quantize(Math.Clamp(value * 3f, 0f, 1f));
            }
            output.Data[i * 4 + 3] = 255;
        }
        return output;
    }

    /// <summary>The <c>_selfillum</c> mask: an sRGB colour map where non-black pixels glow.</summary>
    public static ByteImage BakeSelfillum(FloatImage selfillum)
    {
        int height = selfillum.Height, width = selfillum.Width;
        var output = new ByteImage(height, width, 4);
        for (var i = 0; i < height * width; i++)
        {
            if (selfillum.Channels >= 3)
            {
                var at = i * selfillum.Channels;
                output.Data[i * 4] = Quantize(selfillum.Data[at]);
                output.Data[i * 4 + 1] = Quantize(selfillum.Data[at + 1]);
                output.Data[i * 4 + 2] = Quantize(selfillum.Data[at + 2]);
                output.Data[i * 4 + 3] = selfillum.Channels >= 4 ? Quantize(selfillum.Data[at + 3]) : (byte)255;
            }
            else
            {
                var grey = Quantize(selfillum.Data[i * selfillum.Channels]);
                output.Data[i * 4] = output.Data[i * 4 + 1] = output.Data[i * 4 + 2] = grey;
                output.Data[i * 4 + 3] = 255;
            }
        }
        return output;
    }

    /// <summary>The packed ARM texture the ExoPBR shader samples: R AO, G roughness, B metallic, A height.</summary>
    /// <remarks>Unlike the Fake PBR bakes this is a straight repack: the shader does the PBR maths itself, so nothing is approximated here.</remarks>
    public static ByteImage BakeArm(FloatImage? ao, FloatImage? roughness, FloatImage? metallic, FloatImage? heightmap, int height, int width)
    {
        FloatImage red = ScalarMap(ao, height, width, 1f), green = ScalarMap(roughness, height, width, 1f), blue = ScalarMap(metallic, height, width, 0f), alpha = ScalarMap(heightmap, height, width, 1f);
        var output = new ByteImage(height, width, 4);
        for (var i = 0; i < height * width; i++)
        {
            output.Data[i * 4] = Quantize(red.Data[i]);
            output.Data[i * 4 + 1] = Quantize(green.Data[i]);
            output.Data[i * 4 + 2] = Quantize(blue.Data[i]);
            output.Data[i * 4 + 3] = Quantize(alpha.Data[i]);
        }
        return output;
    }

    /// <summary>The ExoPBR normal: RGB inverted, blue zeroed, alpha preserved.</summary>
    /// <remarks>The screenspace shader reconstructs Z itself and reads the inverted XY, so the blue channel is dead weight and cleared rather than carried.</remarks>
    public static ByteImage BakeExoNormal(FloatImage normal, int height, int width)
    {
        var output = new ByteImage(height, width, 4);
        for (var i = 0; i < height * width; i++)
        {
            var at = i * normal.Channels;
            output.Data[i * 4] = Quantize(1f - Math.Clamp(normal.Data[at], 0f, 1f));
            output.Data[i * 4 + 1] = Quantize(1f - Math.Clamp(normal.Data[at + 1], 0f, 1f));
            output.Data[i * 4 + 3] = normal.Channels >= 4 ? Quantize(normal.Data[at + 3]) : (byte)255;
        }
        return output;
    }

    /// <summary>The ExoPBR base texture: RGB albedo untouched, alpha opacity.</summary>
    public static ByteImage BakeExoBase(FloatImage color, FloatImage? transparency, int height, int width)
    {
        var output = new ByteImage(height, width, 4);
        var opacity = transparency is null ? null : ScalarMap(transparency, height, width, 1f);
        for (var i = 0; i < height * width; i++)
        {
            var at = i * color.Channels;
            if (color.Channels >= 3)
            {
                output.Data[i * 4] = Quantize(color.Data[at]);
                output.Data[i * 4 + 1] = Quantize(color.Data[at + 1]);
                output.Data[i * 4 + 2] = Quantize(color.Data[at + 2]);
            }
            else output.Data[i * 4] = output.Data[i * 4 + 1] = output.Data[i * 4 + 2] = Quantize(color.Data[at]);
            output.Data[i * 4 + 3] = opacity is not null ? Quantize(opacity.Data[i]) : color.Channels >= 4 ? Quantize(color.Data[at + 3]) : (byte)255;
        }
        return output;
    }

    /// <summary>Recovers roughness from a <c>_phong</c> red channel.</summary>
    /// <remarks>The exact inverse of the exponent bake, given the same two settings. Values the bake clipped come back at the clip boundary, so a fully rough material round-trips to one rather than to noise.</remarks>
    public static FloatImage UnbakeRoughness(FloatImage phongRed, double phongStrength = 0.5, double glossGamma = 2.0)
    {
        var output = new FloatImage(phongRed.Height, phongRed.Width, 1);
        var divisor = (float)Math.Max(1e-4, phongStrength);
        var exponent = (float)(1.0 / Math.Max(0.01, glossGamma));
        for (var i = 0; i < output.Data.Length; i++)
            output.Data[i] = Math.Clamp(1f - MathF.Pow(Math.Clamp(phongRed.Data[i] / divisor, 0f, 1f), exponent), 0f, 1f);
        return output;
    }

    /// <summary>Undoes the metal darkening in a baked basetexture.</summary>
    /// <remarks>Only the metal term is recoverable. AO was multiplied into the same pixels with no second equation to separate it, so what comes back is the AO-baked albedo with the darkening removed: good enough to re-author from, not an exact original.</remarks>
    public static FloatImage UnbakeAlbedo(FloatImage colorRgb, FloatImage? metallic, double metalDiffuseSuppression = MaterialDefaults.MetalDiffuseSuppression)
    {
        var rgb = colorRgb.Clipped();
        if (metallic is null || metalDiffuseSuppression <= 0.0) return rgb;
        var metal = ScalarMap(metallic, rgb.Height, rgb.Width, 0f);
        var suppression = (float)metalDiffuseSuppression;
        for (var i = 0; i < metal.Data.Length; i++)
        {
            var divisor = MathF.Max(1f - suppression * metal.Data[i], 1e-3f);
            for (var c = 0; c < 3; c++) rgb.Data[i * 3 + c] = Math.Clamp(rgb.Data[i * 3 + c] / divisor, 0f, 1f);
        }
        return rgb;
    }

    /// <summary>Splits a packed ORM texture into AO, roughness and metallic scalar maps.</summary>
    public static (FloatImage Ao, FloatImage Roughness, FloatImage Metallic) SplitOrm(FloatImage orm)
    {
        if (orm.Channels < 3) throw new ArgumentException("An ORM map needs at least three channels");
        var clipped = orm.Clipped();
        return (clipped.Channel(0), clipped.Channel(1), clipped.Channel(2));
    }

    /// <summary>Whether a normal map is uniformly (0.5, 0.5, 1.0), carrying no bump.</summary>
    /// <remarks>A material whose source authored no bump detail should not reference a $bumpmap: the VTF costs disk and the shader combo costs fill rate for a texture that says "flat" at every pixel.</remarks>
    public static bool IsFlatNormal(FloatImage normal, double tolerance = 2.0 / 255.0)
    {
        var worst = 0f;
        for (var i = 0; i < normal.Height * normal.Width; i++)
        {
            var at = i * normal.Channels;
            worst = MathF.Max(worst, MathF.Abs(Math.Clamp(normal.Data[at], 0f, 1f) - 0.5f));
            worst = MathF.Max(worst, MathF.Abs(Math.Clamp(normal.Data[at + 1], 0f, 1f) - 0.5f));
            worst = MathF.Max(worst, MathF.Abs(Math.Clamp(normal.Data[at + 2], 0f, 1f) - 1f));
        }
        return worst < (float)tolerance;
    }

    /// <summary>Whether a baked 8-bit RGB buffer is black enough to not be worth writing.</summary>
    /// <remarks>Threshold 4 tolerates DXT encoder noise. A vmat with g_flMetalness 0 bakes a pure-black envmask, and writing it costs disk for a runtime contribution of nothing.</remarks>
    public static bool IsEffectivelyBlack(ByteImage pixels, int threshold = 4)
    {
        var channels = pixels.Channels;
        for (var i = 0; i < pixels.Data.Length; i += channels)
            for (var c = 0; c < Math.Min(3, channels); c++)
                if (pixels.Data[i + c] >= threshold) return false;
        return true;
    }
}
