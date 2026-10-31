using System.Buffers.Binary;
using System.Globalization;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>A compiled stage refused its input. The plain form stands for a bare value fault in the data.</summary>
public class CompiledDataError(string message, Exception? inner = null) : Exception(message, inner);

/// <summary>A compiled model contains a truncated or invalid serialized record.</summary>
public sealed class ModelAuditError(string message) : CompiledDataError(message);

/// <summary>Generated render sources cannot be reconciled with their compiled material streams.</summary>
public sealed class RenderCoverageError(string message) : CompiledDataError(message);

/// <summary>A compiled model cannot be safely read or rewritten without guessing its layout.</summary>
public sealed class CompiledVertexError(string message) : CompiledDataError(message);

/// <summary>Authored deformation cannot be assigned to a rendered corner without ambiguity.</summary>
public sealed class MorphGeometryError(string message, Exception? inner = null) : CompiledDataError(message, inner);

/// <summary>Reads bounded little-endian records using Valve SDK serialized offsets.</summary>
/// <remarks>Every offset is a 64-bit value because the serialized fields are relative and can be corrupt: a sum
/// that wraps in 32 bits would pass the bounds check and read the wrong record.</remarks>
internal sealed class CompiledBinary(string path)
{
    public string Name { get; } = Path.GetFileName(path);
    public byte[] Data { get; } = File.ReadAllBytes(path);

    public bool HasMagic(string magic) => Data.AsSpan().StartsWith(Encoding.ASCII.GetBytes(magic));

    public void Require(long offset, long size)
    {
        if (offset < 0 || size < 0 || offset + size > Data.Length) throw new ModelAuditError($"{Name}: record outside file at {offset}+{size}");
    }

    private ReadOnlySpan<byte> At(long offset, long size)
    {
        Require(offset, size);
        return Data.AsSpan((int)offset, (int)size);
    }

    public int Int(long offset) => BinaryPrimitives.ReadInt32LittleEndian(At(offset, 4));
    public uint UInt(long offset) => BinaryPrimitives.ReadUInt32LittleEndian(At(offset, 4));
    public short Short(long offset) => BinaryPrimitives.ReadInt16LittleEndian(At(offset, 2));
    public byte Byte(long offset) => At(offset, 1)[0];
    public double Float(long offset) => BinaryPrimitives.ReadSingleLittleEndian(At(offset, 4));

    /// <summary>Reads consecutive 32-bit integers, requiring the whole run before reading any of it.</summary>
    public int[] Ints(long offset, int count)
    {
        Require(offset, count * 4L);
        var values = new int[count];
        for (var index = 0; index < count; index++) values[index] = Int(offset + index * 4L);
        return values;
    }

    /// <summary>Reads consecutive 32-bit floats widened to doubles, which keeps each value exactly.</summary>
    public double[] Floats(long offset, int count)
    {
        Require(offset, count * 4L);
        var values = new double[count];
        for (var index = 0; index < count; index++) values[index] = Float(offset + index * 4L);
        return values;
    }

    public string String(long offset)
    {
        Require(offset, 1);
        var end = Array.IndexOf(Data, (byte)0, (int)offset);
        if (end < 0) throw new ModelAuditError($"{Name}: unterminated string at {offset}");
        return Encoding.UTF8.GetString(Data, (int)offset, end - (int)offset);
    }

    /// <summary>Validates a counted table now and returns its record offsets.</summary>
    /// <remarks>The check runs when this is called, not when the result is first read, so a table that is
    /// only validated and never walked still fails.</remarks>
    public IEnumerable<long> Table(long count, long offset, long stride)
    {
        if (count is < 0 or > 1_000_000) throw new ModelAuditError($"{Name}: invalid record count {count}");
        Require(offset, count * stride);
        return Offsets(count, offset, stride);
    }

    private static IEnumerable<long> Offsets(long count, long offset, long stride)
    {
        for (long index = 0; index < count; index++) yield return offset + index * stride;
    }
}

/// <summary>Text and value handling the compiled audits share with the way v3 read the same files.</summary>
internal static partial class CompiledText
{
    [GeneratedRegex(@"""[^""\n]*""|[{}]|[^\s\x1c-\x1f{}""]+")]
    private static partial Regex Token();

    /// <summary>Tokenizes generated QC and VMT names while retaining Windows path separators.</summary>
    /// <remarks>A quoted token keeps its backslashes literally, so resource names are never read as escape
    /// sequences. The control characters 1C to 1F are spelled out because Python counts them as whitespace.</remarks>
    public static List<string> Tokens(string line)
    {
        var tokens = new List<string>();
        foreach (Match match in Token().Matches(line))
        {
            var token = match.Value;
            tokens.Add(token.StartsWith('"') ? token[1..^1] : token);
        }
        return tokens;
    }

    /// <summary>Drops a line comment, even one inside a quoted name, because the generated QC never has one there.</summary>
    public static string StripComment(string line)
    {
        var at = line.IndexOf("//", StringComparison.Ordinal);
        return at < 0 ? line : line[..at];
    }

    public static string Casefold(string value) => value.ToLowerInvariant();

    /// <summary>Parses a number the way Python's float constructor does.</summary>
    public static double Float(string text)
    {
        var value = text.Trim();
        var unsigned = value.TrimStart('+', '-').ToLowerInvariant();
        var negative = value.StartsWith('-');
        if (unsigned is "inf" or "infinity") return negative ? double.NegativeInfinity : double.PositiveInfinity;
        if (unsigned == "nan") return double.NaN;
        return double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out var number)
            ? number
            : throw new CompiledDataError($"could not convert string to float: {PythonText.Repr(text)}");
    }

    /// <summary>Parses a whole number the way Python's int constructor does.</summary>
    public static int Int(string text) => int.TryParse(text.Trim(), NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture, out var number)
        ? number
        : throw new CompiledDataError($"invalid literal for int() with base 10: {PythonText.Repr(text)}");

    /// <summary>Reads consecutive floats from a slice of a token list. A short list gives a short slice.</summary>
    public static double[] Floats(List<string> values, int start, int end) =>
        values.Skip(start).Take(Math.Max(end - start, 0)).Select(Float).ToArray();

    /// <summary>A path printed the way a Windows path object prints a parent joined with a relative name.</summary>
    public static string Join(string parent, string relative)
    {
        var parts = relative.Replace('\\', '/').Split('/').Where(part => part.Length > 0 && part != ".");
        return PathText.Native(Path.Combine(parent, string.Join('/', parts)));
    }

    /// <summary>The parent of a path, or the current folder for a bare file name.</summary>
    public static string Parent(string path) => Path.GetDirectoryName(path) is { Length: > 0 } parent ? parent : ".";

    /// <summary>A required key: an absent one is a fault, where <see cref="Json.Get"/> would quietly give null.</summary>
    public static JsonNode? Key(this JsonNode? node, string key) =>
        node is JsonObject map && map.TryGetPropertyValue(key, out var value) ? value : throw new KeyNotFoundException(PythonText.Repr(key));

    public static JsonArray Strings(IEnumerable<string?> values) => new(values.Select(value => (JsonNode?)value).ToArray());
    public static JsonArray Doubles(IEnumerable<double> values) => new(values.Select(value => (JsonNode?)value).ToArray());
    public static JsonArray Integers(IEnumerable<int> values) => new(values.Select(value => (JsonNode?)value).ToArray());

    /// <summary>A tuple of whole numbers as Python prints it, for messages that name a mesh.</summary>
    public static string Tuple(params int[] values) => "(" + string.Join(", ", values) + ")";

    /// <summary>Whether a failure is one the audits report as a finding instead of letting it stop the port.</summary>
    /// <remarks>This stands for v3's list of caught exception types. Cancellation is deliberately absent, so a
    /// cancelled job is never recorded as a failed audit.</remarks>
    public static bool IsAuditFault(Exception exception) => exception is IOException or UnauthorizedAccessException or CompiledDataError
        or InvalidDataException or FormatException or OverflowException or ArgumentException or IndexOutOfRangeException or KeyNotFoundException;

    /// <summary>The words Python would use for the same failure.</summary>
    public static string Describe(Exception exception) => exception switch
    {
        ArgumentOutOfRangeException or IndexOutOfRangeException => "list index out of range",
        FileNotFoundException { FileName: { } name } => $"[Errno 2] No such file or directory: {PythonText.Repr(name.Replace("\\", "\\\\"))}",
        _ => exception.Message
    };
}
