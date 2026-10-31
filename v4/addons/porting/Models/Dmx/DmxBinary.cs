using System.Buffers.Binary;
using System.Collections;
using System.Text;

namespace PortingKit.Addons.Porting;

/// <summary>How one binary encoding version stores its string dictionary.</summary>
/// <param name="HasDictionary">Binary 1 and binary_proto keep every string inline.</param>
/// <param name="ShortLength">The dictionary's own length is 16 bits in binary 2 and 3.</param>
/// <param name="ShortIndex">References into it are 16 bits in binary 2, 3 and 4.</param>
internal readonly record struct DmxStringLayout(bool HasDictionary, bool ShortLength, bool ShortIndex)
{
    public static DmxStringLayout For(string encoding, int version) => encoding != "binary" || version == 1
        ? new(false, false, false)
        : new(true, version is 2 or 3, version is 2 or 3 or 4);
}

/// <summary>Decodes the binary and binary_proto encodings into a document.</summary>
internal sealed class DmxBinaryReader(byte[] data, int position, string encoding, int version, DmxDocument document)
{
    private readonly DmxStringLayout layout = DmxStringLayout.For(encoding, version);
    private readonly List<string?> dictionary = [];

    private ReadOnlySpan<byte> Take(int count)
    {
        var span = data.AsSpan(position, count);
        position += count;
        return span;
    }

    private int Int() => BinaryPrimitives.ReadInt32LittleEndian(Take(4));
    private int Short() => BinaryPrimitives.ReadInt16LittleEndian(Take(2));
    private float Float() => BinaryPrimitives.ReadSingleLittleEndian(Take(4));
    private double Single() => Float();

    /// <summary>Reads an inline string, where an empty one means null.</summary>
    private string? Inline()
    {
        var end = Array.IndexOf(data, (byte)0, position);
        if (end < 0) throw new EndOfStreamException("Unterminated DMX string.");
        var text = end == position ? null : Encoding.UTF8.GetString(data, position, end - position);
        position = end + 1;
        return text;
    }

    private string? DictionaryString()
    {
        if (!layout.HasDictionary) return Inline();
        var index = layout.ShortIndex ? Short() : Int();
        return index >= 0 ? dictionary[index] : null;
    }

    private DmxElement? ElementValue()
    {
        var index = Int();
        return index switch
        {
            -1 => null,
            -2 => document.AddElement("Missing element", id: Guid.Parse(Inline() ?? ""), placeholder: true),
            _ => document.Elements[index]
        };
    }

    private object? Value(DmxKind kind, bool fromArray, bool inlineStrings) => kind switch
    {
        DmxKind.Element => ElementValue(),
        DmxKind.Int => Int(),
        DmxKind.Float => Float(),
        DmxKind.Bool => Take(1)[0] != 0,
        DmxKind.String => version < 4 || fromArray || inlineStrings ? Inline() : DictionaryString(),
        DmxKind.Binary => Take(Int()).ToArray(),
        DmxKind.ObjectId => new Guid(Take(16)),
        DmxKind.Time => DmxTime.FromTicks(Int()),
        DmxKind.Color => new DmxColor(Take(1)[0], Take(1)[0], Take(1)[0], Take(1)[0]),
        DmxKind.Vector2 => new V2(Single(), Single()),
        DmxKind.Vector3 => new V3(Single(), Single(), Single()),
        DmxKind.Vector4 => new DmxVector4(Single(), Single(), Single(), Single()),
        DmxKind.Angle => new DmxAngle(Single(), Single(), Single()),
        DmxKind.Quaternion => new Q4(Single(), Single(), Single(), Single()),
        DmxKind.Matrix => new M4(Enumerable.Range(0, 16).Select(_ => Single()).ToArray()),
        DmxKind.UInt64 => BinaryPrimitives.ReadUInt64LittleEndian(Take(8)),
        DmxKind.UInt8 => Take(1)[0],
        _ => throw new InvalidDataException($"Cannot read attributes of type {kind}")
    };

    private void Attributes(DmxElement element, bool inlineStrings)
    {
        var count = Int();
        for (var index = 0; index < count; index++)
        {
            var name = (inlineStrings ? Inline() : DictionaryString()) ?? "";
            var (kind, isArray) = DmxTypes.FromBinaryId(encoding, version, Take(1)[0]);
            if (!isArray)
            {
                element.Set(name, Value(kind, false, inlineStrings));
                continue;
            }
            var length = Int();
            var items = DmxTypes.NewArray(kind);
            for (var item = 0; item < length; item++) items.Add(Value(kind, true, inlineStrings));
            element.Set(name, items);
        }
    }

    public void Read()
    {
        if (encoding == "binary" && version >= 9)
        {
            var prefixes = Int();
            for (var index = 0; index < prefixes; index++) Attributes(document.PrefixAttributes, true);
        }
        if (layout.HasDictionary)
        {
            var count = layout.ShortLength ? Short() : Int();
            for (var index = 0; index < count; index++) dictionary.Add(Inline());
        }
        var elements = Int();
        for (var index = 0; index < elements; index++)
        {
            var type = DictionaryString() ?? "";
            var name = version >= 4 ? DictionaryString() : Inline();
            document.AddElement(name, type, new Guid(Take(16)));
        }
        foreach (var element in document.Elements.Where(element => !element.IsPlaceholder).ToList()) Attributes(element, false);
    }
}

/// <summary>Encodes a document in the binary and binary_proto encodings.</summary>
/// <remarks>Elements are numbered depth first from the root, the order element bodies are stored in, and only
/// elements reachable from the root are written. Placeholders are references, so they never get an index.</remarks>
internal sealed class DmxBinaryWriter(DmxDocument document, string encoding, int version)
{
    private const string HeaderFormat = "<!-- dmx encoding {0} {1} format {2} {3} -->";
    private const string ProtoFormat = "<!-- DMXVersion binary_v{0} -->";

    private readonly DmxStringLayout layout = DmxStringLayout.For(encoding, version);
    private readonly MemoryStream output = new();
    private readonly Dictionary<string, int> dictionary = new(StringComparer.Ordinal);
    private readonly Dictionary<DmxElement, int> indices = [];
    private readonly List<DmxElement> chain = [];

    private void Int(int value)
    {
        Span<byte> bytes = stackalloc byte[4];
        BinaryPrimitives.WriteInt32LittleEndian(bytes, value);
        output.Write(bytes);
    }

    private void Short(int value)
    {
        Span<byte> bytes = stackalloc byte[2];
        BinaryPrimitives.WriteInt16LittleEndian(bytes, (short)value);
        output.Write(bytes);
    }

    private void Single(double value)
    {
        Span<byte> bytes = stackalloc byte[4];
        BinaryPrimitives.WriteSingleLittleEndian(bytes, (float)value);
        output.Write(bytes);
    }

    private void Byte(int value) => output.WriteByte((byte)value);

    private void Inline(string? text)
    {
        if (!string.IsNullOrEmpty(text)) output.Write(Encoding.UTF8.GetBytes(text));
        Byte(0);
    }

    private void Reference(string? text)
    {
        var index = string.IsNullOrEmpty(text) ? -1 : dictionary[text];
        if (layout.ShortIndex) Short(index);
        else Int(index);
    }

    private void Text(string? text, bool suppressDictionary)
    {
        if (suppressDictionary || !layout.HasDictionary) Inline(text);
        else Reference(text);
    }

    private void Dictionary()
    {
        var strings = new HashSet<string>(StringComparer.Ordinal);
        var checkedElements = new HashSet<DmxElement>();
        void Process(DmxElement element)
        {
            checkedElements.Add(element);
            if (!string.IsNullOrEmpty(element.Name)) strings.Add(element.Name);
            strings.Add(element.Type);
            foreach (var name in element.Keys)
            {
                strings.Add(name);
                var value = element[name];
                if (value is string text) strings.Add(text);
                else if (value is DmxElement child && !checkedElements.Contains(child)) Process(child);
                else if (value is List<DmxElement?> items)
                    foreach (var item in items)
                        if (item is not null && !checkedElements.Contains(item)) Process(item);
            }
        }
        if (document.Root is not null) Process(document.Root);
        var ordered = strings.Order(StringComparer.Ordinal).ToList();
        if (layout.ShortLength) Short(ordered.Count);
        else Int(ordered.Count);
        foreach (var text in ordered)
        {
            dictionary[text] = dictionary.Count;
            Inline(text);
        }
    }

    private void Prefixes()
    {
        var prefix = document.PrefixAttributes;
        Int(prefix.Count > 0 ? 1 : 0);
        if (prefix.Count == 0) return;
        Int(prefix.Count);
        foreach (var name in prefix.Keys)
        {
            Inline(name);
            Attribute(prefix[name], true);
        }
    }

    private void Index(DmxElement element)
    {
        if (element.IsPlaceholder || indices.ContainsKey(element)) return;
        Text(element.Type, !layout.HasDictionary);
        Text(element.Name, version < 4);
        output.Write(element.Id.ToByteArray());
        indices[element] = chain.Count;
        chain.Add(element);
        foreach (var name in element.Keys)
        {
            if (element[name] is DmxElement child) Index(child);
            else if (element[name] is List<DmxElement?> items)
                foreach (var item in items)
                    if (item is not null) Index(item);
        }
    }

    private void ElementReference(DmxElement? element)
    {
        if (element is null) Int(-1);
        else if (element.IsPlaceholder && version >= 5)
        {
            Int(-2);
            Inline(element.Id.ToString());
        }
        else Int(element.IsPlaceholder ? -1 : indices[element]);
    }

    private void Single(object? value, DmxKind kind, bool inlineStrings, bool fromArray)
    {
        switch (kind)
        {
            case DmxKind.Element: ElementReference((DmxElement?)value); break;
            case DmxKind.Int: Int((int)value!); break;
            case DmxKind.Float: Single((float)value!); break;
            case DmxKind.Bool: Byte((bool)value! ? 1 : 0); break;
            case DmxKind.String: Text((string?)value, inlineStrings || fromArray || version < 4); break;
            case DmxKind.Binary:
                var bytes = (byte[])value!;
                Int(bytes.Length);
                output.Write(bytes);
                break;
            case DmxKind.ObjectId: output.Write(((Guid)value!).ToByteArray()); break;
            case DmxKind.Time: Int(((DmxTime)value!).Ticks); break;
            case DmxKind.Color:
                var color = (DmxColor)value!;
                Byte(color.R);
                Byte(color.G);
                Byte(color.B);
                Byte(color.A);
                break;
            case DmxKind.Vector2:
                var two = (V2)value!;
                Single(two.X);
                Single(two.Y);
                break;
            case DmxKind.Vector3:
                var three = (V3)value!;
                Single(three.X);
                Single(three.Y);
                Single(three.Z);
                break;
            case DmxKind.Vector4:
                var four = (DmxVector4)value!;
                Single(four.X);
                Single(four.Y);
                Single(four.Z);
                Single(four.W);
                break;
            case DmxKind.Angle:
                var angle = (DmxAngle)value!;
                Single(angle.X);
                Single(angle.Y);
                Single(angle.Z);
                break;
            case DmxKind.Quaternion:
                var quaternion = (Q4)value!;
                Single(quaternion.X);
                Single(quaternion.Y);
                Single(quaternion.Z);
                Single(quaternion.W);
                break;
            case DmxKind.Matrix:
                var matrix = (M4)value!;
                for (var row = 0; row < 4; row++)
                    for (var column = 0; column < 4; column++)
                        Single(matrix[row, column]);
                break;
            case DmxKind.UInt64:
                Span<byte> wide = stackalloc byte[8];
                BinaryPrimitives.WriteUInt64LittleEndian(wide, (ulong)value!);
                output.Write(wide);
                break;
            case DmxKind.UInt8: Byte((byte)value!); break;
            default: throw new InvalidDataException($"Cannot write attributes of type {kind}");
        }
    }

    private void Attribute(object? value, bool inlineStrings)
    {
        var (kind, isArray) = DmxTypes.Classify(value);
        Byte(DmxTypes.ToBinaryId(encoding, version, kind, isArray));
        if (!isArray)
        {
            Single(value, kind, inlineStrings, false);
            return;
        }
        var items = (IList)value!;
        Int(items.Count);
        foreach (var item in items) Single(item, kind, inlineStrings, true);
    }

    public byte[] Write()
    {
        var header = encoding == "binary_proto"
            ? string.Format(ProtoFormat, version)
            : string.Format(HeaderFormat, encoding, version, document.Format, document.FormatVersion);
        Inline(header + "\n");
        if (encoding == "binary" && version >= 9) Prefixes();
        if (layout.HasDictionary) Dictionary();
        var root = document.Root ?? throw new InvalidDataException("A DMX document needs a root element.");
        Int(document.CountUsers().Keys.Count(element => !element.IsPlaceholder));
        Index(root);
        foreach (var element in chain)
        {
            Int(element.Count);
            foreach (var name in element.Keys)
            {
                Text(name, !layout.HasDictionary);
                Attribute(element[name], false);
            }
        }
        return output.ToArray();
    }
}
