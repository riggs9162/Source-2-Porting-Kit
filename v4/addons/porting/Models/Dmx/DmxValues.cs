using System.Globalization;

namespace PortingKit.Addons.Porting;

/// <summary>A DMX four-component vector. It shares storage with a quaternion but stays a distinct attribute type.</summary>
public readonly record struct DmxVector4(double X, double Y, double Z, double W);

/// <summary>A DMX Euler angle triple. It shares storage with a vector but stays a distinct attribute type.</summary>
public readonly record struct DmxAngle(double X, double Y, double Z);

/// <summary>A DMX colour stored as four bytes.</summary>
public readonly record struct DmxColor(byte R, byte G, byte B, byte A);

/// <summary>A DMX time, stored on disk as ten-thousandths of a second.</summary>
public readonly record struct DmxTime(double Seconds)
{
    public static DmxTime FromTicks(int ticks) => new(ticks / 10000.0);
    public int Ticks => (int)(Seconds * 10000);
}

/// <summary>Raised when an element is asked for an attribute it does not carry, which is normally a data fault.</summary>
public sealed class DmxAttributeException(string message) : KeyNotFoundException(message);

/// <summary>Raised when an element's identifier collides with another real element.</summary>
public sealed class DmxIdCollisionException(string message) : Exception(message);

/// <summary>Raised when a keyvalues2 document cannot be parsed.</summary>
public sealed class DmxParseException(string message, Exception? inner = null) : Exception(message, inner);

/// <summary>The attribute types DMX defines, one value per type name.</summary>
internal enum DmxKind
{
    Element = 1, Int, Float, Bool, String, Binary, ObjectId, Color, Vector2, Vector3, Vector4, Angle, Quaternion, Matrix, Time, UInt64, UInt8
}

/// <summary>Maps attribute values to their kinds, binary type identifiers and keyvalues2 names.</summary>
internal static class DmxTypes
{
    private const int LegacyArrayOffset = 14;
    private const int ModernArrayOffset = 32;

    private static readonly Dictionary<string, DmxKind> Names = new()
    {
        ["element"] = DmxKind.Element, ["int"] = DmxKind.Int, ["float"] = DmxKind.Float, ["bool"] = DmxKind.Bool,
        ["string"] = DmxKind.String, ["binary"] = DmxKind.Binary, ["time"] = DmxKind.Time, ["color"] = DmxKind.Color,
        ["vector2"] = DmxKind.Vector2, ["vector3"] = DmxKind.Vector3, ["vector4"] = DmxKind.Vector4, ["angle"] = DmxKind.Angle,
        ["quaternion"] = DmxKind.Quaternion, ["matrix"] = DmxKind.Matrix, ["uint64"] = DmxKind.UInt64, ["uint8"] = DmxKind.UInt8,
        ["elementid"] = DmxKind.ObjectId
    };

    /// <summary>Identifies an attribute's stored kind, or fails on a type DMX cannot hold.</summary>
    public static (DmxKind Kind, bool IsArray) Classify(object? value) => value switch
    {
        null or DmxElement => (DmxKind.Element, false),
        int => (DmxKind.Int, false),
        float => (DmxKind.Float, false),
        bool => (DmxKind.Bool, false),
        string => (DmxKind.String, false),
        byte[] => (DmxKind.Binary, false),
        Guid => (DmxKind.ObjectId, false),
        DmxTime => (DmxKind.Time, false),
        DmxColor => (DmxKind.Color, false),
        V2 => (DmxKind.Vector2, false),
        V3 => (DmxKind.Vector3, false),
        DmxVector4 => (DmxKind.Vector4, false),
        DmxAngle => (DmxKind.Angle, false),
        Q4 => (DmxKind.Quaternion, false),
        M4 => (DmxKind.Matrix, false),
        ulong => (DmxKind.UInt64, false),
        byte => (DmxKind.UInt8, false),
        List<DmxElement?> => (DmxKind.Element, true),
        List<int> => (DmxKind.Int, true),
        List<float> => (DmxKind.Float, true),
        List<bool> => (DmxKind.Bool, true),
        List<string?> => (DmxKind.String, true),
        List<byte[]> => (DmxKind.Binary, true),
        List<Guid> => (DmxKind.ObjectId, true),
        List<DmxTime> => (DmxKind.Time, true),
        List<DmxColor> => (DmxKind.Color, true),
        List<V2> => (DmxKind.Vector2, true),
        List<V3> => (DmxKind.Vector3, true),
        List<DmxVector4> => (DmxKind.Vector4, true),
        List<DmxAngle> => (DmxKind.Angle, true),
        List<Q4> => (DmxKind.Quaternion, true),
        List<M4> => (DmxKind.Matrix, true),
        List<ulong> => (DmxKind.UInt64, true),
        List<byte> => (DmxKind.UInt8, true),
        _ => throw new ArgumentException($"Invalid attribute type ({value.GetType().Name}).")
    };

    /// <summary>Creates the empty list that holds a kind's array attribute.</summary>
    public static System.Collections.IList NewArray(DmxKind kind) => kind switch
    {
        DmxKind.Element => new List<DmxElement?>(),
        DmxKind.Int => new List<int>(),
        DmxKind.Float => new List<float>(),
        DmxKind.Bool => new List<bool>(),
        DmxKind.String => new List<string?>(),
        DmxKind.Binary => new List<byte[]>(),
        DmxKind.ObjectId => new List<Guid>(),
        DmxKind.Time => new List<DmxTime>(),
        DmxKind.Color => new List<DmxColor>(),
        DmxKind.Vector2 => new List<V2>(),
        DmxKind.Vector3 => new List<V3>(),
        DmxKind.Vector4 => new List<DmxVector4>(),
        DmxKind.Angle => new List<DmxAngle>(),
        DmxKind.Quaternion => new List<Q4>(),
        DmxKind.Matrix => new List<M4>(),
        DmxKind.UInt64 => new List<ulong>(),
        DmxKind.UInt8 => new List<byte>(),
        _ => throw new ArgumentOutOfRangeException(nameof(kind))
    };

    /// <summary>Decodes a binary attribute type byte for one encoding version.</summary>
    /// <remarks>Identifier 7 is an object identifier before binary 3 and a time from then on. Binary 9
    /// widens the table with uint64 and uint8, moving arrays from an offset of 14 to an offset of 32.</remarks>
    public static (DmxKind Kind, bool IsArray) FromBinaryId(string encoding, int version, int id)
    {
        var modern = encoding == "binary" && version == 9;
        var offset = modern ? ModernArrayOffset : LegacyArrayOffset;
        var isArray = id > offset;
        var index = isArray ? id - offset : id;
        var legacyTable = encoding == "binary_proto" || version is 1 or 2;
        DmxKind? kind = index switch
        {
            >= 1 and <= 6 => (DmxKind)index,
            7 => legacyTable ? DmxKind.ObjectId : DmxKind.Time,
            >= 8 and <= 14 => (DmxKind)index,
            15 when modern => DmxKind.UInt64,
            16 when modern => DmxKind.UInt8,
            _ => null
        };
        return kind is { } value ? (value, isArray) : throw new InvalidDataException($"Type ID {id} invalid in {encoding} {version}");
    }

    /// <summary>Encodes an attribute kind as a binary type byte for one encoding version.</summary>
    public static int ToBinaryId(string encoding, int version, DmxKind kind, bool isArray)
    {
        var modern = encoding == "binary" && version == 9;
        var legacyTable = encoding == "binary_proto" || version is 1 or 2;
        int? index = kind switch
        {
            DmxKind.Time when !legacyTable => 7,
            DmxKind.ObjectId when legacyTable => 7,
            DmxKind.UInt64 when modern => 15,
            DmxKind.UInt8 when modern => 16,
            DmxKind.Time or DmxKind.ObjectId or DmxKind.UInt64 or DmxKind.UInt8 => null,
            _ => (int)kind
        };
        if (index is not { } value) throw new InvalidDataException($"Type {NameOf(kind)} not supported in {encoding} {version}");
        return isArray ? value + (modern ? ModernArrayOffset : LegacyArrayOffset) : value;
    }

    public static string NameOf(DmxKind kind) => Names.First(pair => pair.Value == kind).Key;

    public static bool TryKind(string name, out DmxKind kind) => Names.TryGetValue(name, out kind);

    /// <summary>A float's shortest round-trip text, always with a decimal point as Python prints it.</summary>
    public static string FloatText(double value)
    {
        var text = value.ToString("R", CultureInfo.InvariantCulture);
        return double.IsFinite(value) && !text.Contains('.') && !text.Contains('E') ? text + ".0" : text;
    }
}
