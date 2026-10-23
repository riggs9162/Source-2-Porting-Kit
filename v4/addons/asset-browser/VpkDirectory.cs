using System.Text;

namespace PortingKit.Addons.AssetBrowser;

internal sealed record VpkRecord(string Path, ushort Archive, uint Offset, uint Length, ushort PreloadLength,
    long PreloadOffset, long InlineOffset);

/// <summary>Reads VPK v1/v2 directory records without retaining the archive index in memory.</summary>
internal static class VpkDirectory
{
    public static IEnumerable<VpkRecord> Read(string path, CancellationToken cancellationToken)
    {
        using var file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read, 65536, FileOptions.SequentialScan);
        using var reader = new BinaryReader(file, new UTF8Encoding(false, true));
        if (reader.ReadUInt32() != 0x55AA1234)
            throw new InvalidDataException("This file is not a VPK archive.");
        var version = reader.ReadUInt32();
        var treeSize = reader.ReadUInt32();
        var headerSize = version switch { 1 => 12, 2 => 28, _ => throw new InvalidDataException("Unsupported VPK version.") };
        var end = headerSize + (long)treeSize;
        if (end > file.Length || treeSize > 256 * 1024 * 1024)
            throw new InvalidDataException("The VPK directory size is invalid.");
        file.Position = headerSize;
        string extension;
        while ((extension = ReadString(reader, end)).Length > 0)
        {
            string folder;
            while ((folder = ReadString(reader, end)).Length > 0)
            {
                string name;
                while ((name = ReadString(reader, end)).Length > 0)
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    if (file.Position + 18 > end)
                        throw new InvalidDataException("Truncated VPK directory entry.");
                    reader.ReadUInt32();
                    var preload = reader.ReadUInt16();
                    var archive = reader.ReadUInt16();
                    var offset = reader.ReadUInt32();
                    var length = reader.ReadUInt32();
                    if (reader.ReadUInt16() != ushort.MaxValue || file.Position + preload > end)
                        throw new InvalidDataException("Invalid VPK directory entry.");
                    var preloadOffset = file.Position;
                    file.Position += preload;
                    var resource = (folder == " " ? "" : folder + "/") + name + (extension == " " ? "" : "." + extension);
                    yield return new(resource, archive, offset, length, preload, preloadOffset, end);
                }
            }
        }
    }

    private static string ReadString(BinaryReader reader, long end)
    {
        var bytes = new List<byte>();
        while (reader.BaseStream.Position < end)
        {
            var value = reader.ReadByte();
            if (value == 0)
                return Encoding.UTF8.GetString(bytes.ToArray());
            if (bytes.Count >= 4096)
                throw new InvalidDataException("VPK path component is too long.");
            bytes.Add(value);
        }
        throw new InvalidDataException("Unterminated VPK directory string.");
    }
}
