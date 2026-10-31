using System.Text;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>A datamodel document: a flat list of elements with a format name and version.</summary>
/// <remarks>Reads and writes binary 1 to 5 and 9, binary_proto 2 and keyvalues2 1 to 4. Source 2 Viewer's
/// model export arrives as binary 9 and the flex sources the kit writes for studiomdl are binary 5.</remarks>
public sealed partial class DmxDocument(string format, int formatVersion)
{
    private static readonly Dictionary<string, int[]> Support = new()
    {
        ["binary"] = [1, 2, 3, 4, 5, 9], ["keyvalues2"] = [1, 2, 3, 4], ["binary_proto"] = [2]
    };

    private readonly HashSet<Guid> identifiers = [];

    public string Format { get; set; } = format;
    public int FormatVersion { get; set; } = formatVersion;
    public List<DmxElement> Elements { get; } = [];
    public DmxElement? Root { get; set; }

    /// <summary>Attributes stored ahead of the element list by binary 9 and keyvalues2 prefix elements.</summary>
    public DmxElement PrefixAttributes { get; } = new("", "DmElement", Guid.NewGuid(), false);

    [GeneratedRegex(@"<!-- dmx encoding (\S+) ([0-9]+) format (\S+) ([0-9]+) -->")]
    private static partial Regex Header();

    [GeneratedRegex(@"<!-- DMXVersion binary_v([0-9]+) -->")]
    private static partial Regex ProtoHeader();

    private static void CheckSupport(string encoding, int version)
    {
        if (!Support.TryGetValue(encoding, out var versions)) throw new NotSupportedException($"DMX encoding \"{encoding}\" is not supported");
        if (!versions.Contains(version)) throw new NotSupportedException($"Version {version} of {encoding} DMX is not supported");
    }

    /// <summary>Creates an element, making the first one the document's root.</summary>
    public DmxElement AddElement(string? name, string type = "DmElement", Guid? id = null, bool placeholder = false)
    {
        var element = new DmxElement(name, type, id ?? Guid.NewGuid(), placeholder);
        if (!placeholder && !identifiers.Add(element.Id))
            throw new DmxIdCollisionException($"{element} invalid for this document: ID collision. ID is {element.Id}.");
        Elements.Add(element);
        if (Elements.Count == 1) Root = element;
        return element;
    }

    /// <summary>Every element of one type, in document order.</summary>
    public List<DmxElement> OfType(string type) => Elements.Where(element => element.Type == type).ToList();

    /// <summary>Loads any supported encoding, which the file's header names.</summary>
    public static DmxDocument Load(string path) => Read(File.ReadAllBytes(path), path);

    /// <summary>Parses keyvalues2 text that includes its header.</summary>
    public static DmxDocument Parse(string text) => Read(Encoding.UTF8.GetBytes(text), "<string>");

    private static DmxDocument Read(byte[] data, string source)
    {
        var terminator = Array.IndexOf(data, (byte)'>');
        var head = terminator < 0 ? "" : Encoding.ASCII.GetString(data, 0, terminator + 1);
        string encoding, format;
        int version, formatVersion;
        if (Header().Match(head) is { Success: true } match)
        {
            encoding = match.Groups[1].Value;
            version = int.Parse(match.Groups[2].ValueSpan);
            format = match.Groups[3].Value;
            formatVersion = int.Parse(match.Groups[4].ValueSpan);
        }
        else if (ProtoHeader().Match(head) is { Success: true } proto)
        {
            encoding = "binary_proto";
            version = int.Parse(proto.Groups[1].ValueSpan);
            format = "undefined_format";
            formatVersion = 0;
        }
        else throw new IOException("Could not read DMX header");
        CheckSupport(encoding, version);
        var document = new DmxDocument(format, formatVersion);
        if (encoding == "keyvalues2") DmxKeyValues2.Read(document, Encoding.UTF8.GetString(data, terminator + 1, data.Length - terminator - 1), source);
        else new DmxBinaryReader(data, terminator + 3, encoding, version, document).Read();
        return document;
    }

    /// <summary>The elements reachable from the root, each with how many attributes refer to it.</summary>
    internal Dictionary<DmxElement, int> CountUsers()
    {
        var users = new Dictionary<DmxElement, int>();
        void Visit(DmxElement element)
        {
            users.TryAdd(element, 0);
            foreach (var name in element.Keys)
            {
                if (element[name] is DmxElement child) Follow(child);
                else if (element[name] is List<DmxElement?> items)
                    foreach (var item in items)
                        if (item is not null) Follow(item);
            }
        }
        void Follow(DmxElement child)
        {
            if (!users.ContainsKey(child)) Visit(child);
            users[child]++;
        }
        if (Root is not null) Visit(Root);
        return users;
    }

    /// <summary>Serializes the document to the bytes <see cref="Save"/> would write.</summary>
    public byte[] Echo(string encoding, int version)
    {
        CheckSupport(encoding, version);
        return encoding == "keyvalues2"
            ? Encoding.UTF8.GetBytes(DmxKeyValues2.Write(this, version))
            : new DmxBinaryWriter(this, encoding, version).Write();
    }

    /// <summary>Writes the document in one encoding and version.</summary>
    public void Save(string path, string encoding, int version)
    {
        var bytes = Echo(encoding, version);
        var directory = Path.GetDirectoryName(path);
        if (!string.IsNullOrEmpty(directory)) Directory.CreateDirectory(directory);
        File.WriteAllBytes(path, bytes);
    }
}
