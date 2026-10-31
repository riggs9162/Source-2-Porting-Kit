using System.Numerics;

namespace PortingKit.Addons.Porting;

/// <summary>Shared material defaults, and number formatting matching the Python format specifiers the VMT text was defined with.</summary>
public static class MaterialDefaults
{
    /// <summary>Residual diffuse light compensates for stock Source 1 cubemap limitations.</summary>
    public const double MetalDiffuseSuppression = 0.92;

    /// <summary>Colored metals retain their hue through specular tint rather than diffuse relief.</summary>
    public const double ColoredMetalRelief = 0.0;

    /// <summary>Fixed-point text rounded half-to-even on the exact binary value, as <c>f"{value:.3f}"</c> does.</summary>
    /// <remarks>Standard .NET fixed formatting breaks exact ties away from zero, so 0.5625 would read 0.563 instead of 0.562 and change VMT text.</remarks>
    public static string Fixed(double value, int decimals)
    {
        if (double.IsNaN(value)) return "nan";
        if (double.IsInfinity(value)) return value > 0 ? "inf" : "-inf";
        var bits = BitConverter.DoubleToInt64Bits(value);
        var negative = bits < 0;
        var exponent = (int)((bits >> 52) & 0x7FF);
        var mantissa = bits & 0xFFFFFFFFFFFFFL;
        if (exponent == 0) exponent++;
        else mantissa |= 1L << 52;
        exponent -= 1075;
        var numerator = new BigInteger(mantissa) * BigInteger.Pow(10, decimals);
        BigInteger scaled;
        if (exponent >= 0) scaled = numerator << exponent;
        else
        {
            var denominator = BigInteger.One << -exponent;
            scaled = BigInteger.DivRem(numerator, denominator, out var remainder);
            var twice = remainder * 2;
            if (twice > denominator || (twice == denominator && !scaled.IsEven)) scaled += 1;
        }
        var digits = scaled.ToString().PadLeft(decimals + 1, '0');
        var text = decimals == 0 ? digits : digits[..^decimals] + "." + digits[^decimals..];
        return negative ? "-" + text : text;
    }
}
