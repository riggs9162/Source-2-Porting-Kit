using System.Text;

namespace PortingKit.Addons.Soundscapes;

/// <summary>Reads the file table of a Valve pack and single files out of it.</summary>
/// <remarks>Both pack versions are read: Half-Life: Alyx ships version 2 and Garry's Mod version 1. Only the directory
/// file is read up front. A file's bytes are read from its numbered archive when asked for, so listing a game's
/// soundscapes never touches the large archives.</remarks>
public sealed class VpkArchive
{
    private const uint Signature = 0x55AA1234;
    private const ushort InDirectory = 0x7FFF;

    private sealed record Entry(byte[] Preload, ushort Archive, uint Offset, uint Length);

    private readonly Dictionary<string, Entry> entries = new(StringComparer.OrdinalIgnoreCase);
    private readonly string directoryPath;
    private readonly long dataStart;

    public string Path => directoryPath;

    private VpkArchive(string path)
    {
        directoryPath = path;
        using var stream = File.OpenRead(path);
        using var reader = new BinaryReader(stream, Encoding.UTF8);
        if (reader.ReadUInt32() != Signature) throw new InvalidDataException($"Not a Valve pack directory: {path}");
        var version = reader.ReadUInt32();
        var treeSize = reader.ReadUInt32();
        if (version == 2) reader.ReadBytes(16);
        else if (version != 1) throw new InvalidDataException($"Pack version {version} is not supported: {path}");
        var treeStart = stream.Position;
        dataStart = treeStart + treeSize;
        while (ReadText(reader) is { Length: > 0 } extension)
        {
            while (ReadText(reader) is { Length: > 0 } folder)
            {
                while (ReadText(reader) is { Length: > 0 } name)
                {
                    reader.ReadUInt32();
                    var preloadLength = reader.ReadUInt16();
                    var archive = reader.ReadUInt16();
                    var offset = reader.ReadUInt32();
                    var length = reader.ReadUInt32();
                    reader.ReadUInt16();
                    var preload = preloadLength > 0 ? reader.ReadBytes(preloadLength) : [];
                    var file = extension == " " ? name : $"{name}.{extension}";
                    entries[folder == " " ? file : $"{folder}/{file}"] = new(preload, archive, offset, length);
                }
            }
        }
    }

    /// <summary>Opens a pack by its <c>_dir.vpk</c> file.</summary>
    public static VpkArchive Open(string path)
    {
        if (!File.Exists(path)) throw new FileNotFoundException($"The pack does not exist: {path}");
        return new(System.IO.Path.GetFullPath(path));
    }

    private static string ReadText(BinaryReader reader)
    {
        var bytes = new List<byte>(32);
        for (var value = reader.ReadByte(); value != 0; value = reader.ReadByte()) bytes.Add(value);
        return Encoding.UTF8.GetString([.. bytes]);
    }

    public bool Contains(string path) => entries.ContainsKey(Normalize(path));

    /// <summary>Lists the files whose path starts with a folder prefix, in name order.</summary>
    public IReadOnlyList<string> List(string prefix, string extension)
    {
        prefix = Normalize(prefix);
        return [.. entries.Keys.Where(key => key.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) && key.EndsWith(extension, StringComparison.OrdinalIgnoreCase))
            .Order(StringComparer.OrdinalIgnoreCase)];
    }

    /// <summary>Reads one whole file.</summary>
    public byte[] Read(string path)
    {
        if (!entries.TryGetValue(Normalize(path), out var entry)) throw new FileNotFoundException($"Not in the pack: {path}");
        var result = new byte[entry.Preload.Length + entry.Length];
        entry.Preload.CopyTo(result, 0);
        if (entry.Length == 0) return result;
        var inDirectory = entry.Archive == InDirectory;
        var source = inDirectory ? directoryPath : ArchivePath(entry.Archive);
        using var stream = File.OpenRead(source);
        stream.Position = (inDirectory ? dataStart : 0) + entry.Offset;
        stream.ReadExactly(result, entry.Preload.Length, (int)entry.Length);
        return result;
    }

    private string ArchivePath(ushort archive)
    {
        const string suffix = "_dir.vpk";
        if (!directoryPath.EndsWith(suffix, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException($"The pack stores files in numbered archives, so its name must end in {suffix}: {directoryPath}");
        return $"{directoryPath[..^suffix.Length]}_{archive:000}.vpk";
    }

    private static string Normalize(string path) => path.Replace('\\', '/').TrimStart('/');
}
