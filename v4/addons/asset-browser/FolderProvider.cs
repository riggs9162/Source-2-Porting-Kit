namespace PortingKit.Addons.AssetBrowser;

internal sealed class FolderProvider : IAssetProvider
{
    public string Name => "Local files";
    public bool CanOpen(string path) => Directory.Exists(path);

    public Task<AssetPage> ListAsync(string container, string search, int offset, int count, CancellationToken cancellationToken) =>
        Task.Run(() => List(container, search, offset, count, cancellationToken), cancellationToken);

    private static AssetPage List(string container, string search, int offset, int count, CancellationToken cancellationToken)
    {
        ArgumentOutOfRangeException.ThrowIfNegative(offset);
        if (count is < 1 or > 200)
            throw new ArgumentOutOfRangeException(nameof(count));
        var options = new EnumerationOptions { IgnoreInaccessible = true, AttributesToSkip = FileAttributes.ReparsePoint };
        var items = new List<AssetEntry>(count + 1);
        var skipped = 0;
        foreach (var path in Directory.EnumerateDirectories(container, "*", options).Concat(Directory.EnumerateFiles(container, "*", options)))
        {
            cancellationToken.ThrowIfCancellationRequested();
            var name = Path.GetFileName(path);
            if (!name.Contains(search, StringComparison.OrdinalIgnoreCase) || skipped++ < offset)
                continue;
            var isDirectory = Directory.Exists(path);
            items.Add(new(new("asset-browser/files", Path.GetFullPath(container), name), name,
                isDirectory ? "directory" : Path.GetExtension(path).TrimStart('.'), isDirectory ? 0 : new FileInfo(path).Length));
            if (items.Count > count)
                break;
        }
        return new(items.Take(count).ToArray(), offset, items.Count > count);
    }

    public Task<Stream> OpenAsync(AssetReference asset, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var root = Path.GetFullPath(asset.Container).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        var path = Path.GetFullPath(Path.Combine(root, asset.Path));
        if (!path.StartsWith(root, StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException("The asset path leaves its container.");
        return Task.FromResult<Stream>(new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read,
            65536, FileOptions.Asynchronous | FileOptions.SequentialScan));
    }
}
