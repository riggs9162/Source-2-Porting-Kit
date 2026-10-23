namespace PortingKit.Addons.AssetBrowser;

internal sealed class VpkProvider : IAssetProvider
{
    public string Name => "VPK archives";
    public bool CanOpen(string path) => File.Exists(path) && Path.GetExtension(path).Equals(".vpk", StringComparison.OrdinalIgnoreCase);

    public Task<AssetPage> ListAsync(string container, string search, int offset, int count, CancellationToken cancellationToken) =>
        Task.Run(() =>
        {
            ArgumentOutOfRangeException.ThrowIfNegative(offset);
            if (count is < 1 or > 200)
                throw new ArgumentOutOfRangeException(nameof(count));
            var records = VpkDirectory.Read(container, cancellationToken)
                .Where(record => record.Path.Contains(search, StringComparison.OrdinalIgnoreCase)).Skip(offset).Take(count + 1).ToArray();
            var items = records.Take(count).Select(record => new AssetEntry(
                new("asset-browser/vpk", Path.GetFullPath(container), record.Path), record.Path,
                Path.GetExtension(record.Path).TrimStart('.'), record.Length + (long)record.PreloadLength)).ToArray();
            return new AssetPage(items, offset, records.Length > count);
        }, cancellationToken);

    public Task<Stream> OpenAsync(AssetReference asset, CancellationToken cancellationToken) => Task.Run<Stream>(() =>
    {
        var record = VpkDirectory.Read(asset.Container, cancellationToken).FirstOrDefault(entry => entry.Path == asset.Path)
            ?? throw new FileNotFoundException("The asset is not in this archive.", asset.Path);
        var preload = new byte[record.PreloadLength];
        using (var directory = File.OpenRead(asset.Container))
        {
            directory.Position = record.PreloadOffset;
            directory.ReadExactly(preload);
        }
        var archive = asset.Container;
        var offset = record.InlineOffset + record.Offset;
        if (record.Archive != 0x7FFF)
        {
            var stem = Path.GetFileNameWithoutExtension(asset.Container);
            if (!stem.EndsWith("_dir", StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Split archive references require the _dir.vpk index file.");
            archive = Path.Combine(Path.GetDirectoryName(asset.Container)!, stem[..^4] + $"_{record.Archive:D3}.vpk");
            offset = record.Offset;
        }
        var file = new FileStream(archive, FileMode.Open, FileAccess.Read, FileShare.Read, 65536, FileOptions.Asynchronous);
        if (offset + record.Length > file.Length)
        {
            file.Dispose();
            throw new InvalidDataException("The archive data range is invalid.");
        }
        file.Position = offset;
        return new VpkAssetStream(preload, file, record.Length);
    }, cancellationToken);
}

/// <summary>Concatenates small preload bytes and a bounded file segment without copying the full asset.</summary>
internal sealed class VpkAssetStream(byte[] preload, FileStream file, long dataLength) : Stream
{
    private long position;
    private readonly long dataStart = file.Position;
    public override bool CanRead => true;
    public override bool CanSeek => true;
    public override bool CanWrite => false;
    public override long Length => preload.Length + dataLength;
    public override long Position { get => position; set => Seek(value, SeekOrigin.Begin); }
    public override int Read(byte[] buffer, int offset, int count) => Read(buffer.AsSpan(offset, count));

    public override int Read(Span<byte> buffer)
    {
        var prefix = CopyPreload(buffer);
        if (prefix > 0 || position == Length)
            return prefix;
        var count = file.Read(buffer[..(int)Math.Min(buffer.Length, Length - position)]);
        position += count;
        return count;
    }

    public override async ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var prefix = CopyPreload(buffer.Span);
        if (prefix > 0 || position == Length)
            return prefix;
        var count = await file.ReadAsync(buffer[..(int)Math.Min(buffer.Length, Length - position)], cancellationToken);
        position += count;
        return count;
    }

    private int CopyPreload(Span<byte> buffer)
    {
        var count = (int)Math.Min(buffer.Length, Math.Max(0, preload.Length - position));
        if (count > 0)
        {
            preload.AsSpan((int)position, count).CopyTo(buffer);
            position += count;
        }
        return count;
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
            file.Dispose();
        base.Dispose(disposing);
    }

    public override void Flush() { }
    public override long Seek(long offset, SeekOrigin origin)
    {
        var next = checked((origin switch { SeekOrigin.Begin => 0, SeekOrigin.Current => position, SeekOrigin.End => Length,
            _ => throw new ArgumentOutOfRangeException(nameof(origin)) }) + offset);
        if (next < 0 || next > Length) throw new IOException("Seek position is outside this VPK asset.");
        file.Position = dataStart + Math.Max(0, next - preload.Length);
        position = next;
        return position;
    }
    public override void SetLength(long value) => throw new NotSupportedException();
    public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
}
