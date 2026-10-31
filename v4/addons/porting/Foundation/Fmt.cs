using System.Globalization;
using System.Text;

namespace PortingKit.Addons.Porting;

/// <summary>Number and text formatting identical to the C printf forms Source tools read.</summary>
public static class Fmt
{
    private static readonly UTF8Encoding Utf8 = new(false);

    public static double Radians(double degrees) => degrees * (Math.PI / 180.0);
    public static double Degrees(double radians) => radians * (180.0 / Math.PI);
    public static double Hypot(double a, double b) => Math.Sqrt(a * a + b * b);

    /// <summary>Fixed notation with six decimals, as in <c>%.6f</c>.</summary>
    public static string F6(double value) => Fixed(value, 6);

    /// <summary>Fixed notation with nine decimals, as used by SMD vertex lines.</summary>
    public static string F9(double value) => Fixed(value, 9);

    public static string Fixed(double value, int decimals) => value.ToString("F" + decimals, CultureInfo.InvariantCulture);
    public static string Int(long value) => value.ToString(CultureInfo.InvariantCulture);

    /// <summary>Six significant digits without trailing zeros, as in <c>%g</c>.</summary>
    public static string G(double value)
    {
        if (double.IsNaN(value)) return "nan";
        if (double.IsInfinity(value)) return value > 0 ? "inf" : "-inf";
        if (value == 0) return double.IsNegative(value) ? "-0" : "0";
        var scientific = value.ToString("E5", CultureInfo.InvariantCulture);
        var at = scientific.IndexOf('E');
        var exponent = int.Parse(scientific[(at + 1)..], CultureInfo.InvariantCulture);
        if (exponent is < -4 or >= 6)
        {
            var mantissa = scientific[..at].TrimEnd('0').TrimEnd('.');
            return mantissa + "e" + (exponent < 0 ? "-" : "+") + Math.Abs(exponent).ToString("00", CultureInfo.InvariantCulture);
        }
        var text = Fixed(value, 5 - exponent);
        return text.Contains('.') ? text.TrimEnd('0').TrimEnd('.') : text;
    }

    /// <summary>A rotation in degrees wrapped into 0 to 360 the way Python's float modulo wraps it.</summary>
    public static double WrapDegrees(double degrees)
    {
        var wrapped = degrees % 360.0;
        if (wrapped < 0) wrapped += 360.0;
        wrapped %= 360.0;
        return wrapped == 0.0 ? 0.0 : wrapped;
    }

    /// <summary>Writes UTF-8 without a byte order mark, keeping line endings exactly as given.</summary>
    public static void WriteText(string path, string content)
    {
        var directory = Path.GetDirectoryName(path);
        if (!string.IsNullOrEmpty(directory)) Directory.CreateDirectory(directory);
        File.WriteAllText(path, content, Utf8);
    }

    public static StreamWriter OpenText(string path, bool append = false)
    {
        var directory = Path.GetDirectoryName(path);
        if (!string.IsNullOrEmpty(directory)) Directory.CreateDirectory(directory);
        return new(path, append, Utf8) { NewLine = "\n" };
    }
}
