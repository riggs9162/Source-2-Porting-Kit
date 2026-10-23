using System.Security.Cryptography;
using System.Text;

namespace PortingKit.Core;

/// <summary>Maintains a disk budget with least-recently-used eviction and pinned active files.</summary>
public sealed class DiskCache(string directory, long limit) : IDiskCache
{
    private readonly SemaphoreSlim gate = new(1);
    private readonly Dictionary<string, int> pins = new(StringComparer.OrdinalIgnoreCase);

    public async Task<ICacheLease> AcquireAsync(string key, Func<Stream, CancellationToken, Task> create,
        CancellationToken cancellationToken = default)
    {
        await gate.WaitAsync(cancellationToken);
        try
        {
            Directory.CreateDirectory(directory);
            var hash = Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(key)));
            var path = Path.Combine(directory, hash + ".cache");
            if (!File.Exists(path))
            {
                var temporary = path + ".tmp";
                try
                {
                    await using (var file = new FileStream(temporary, FileMode.Create, FileAccess.Write,
                        FileShare.None, 65536, FileOptions.Asynchronous))
                        await create(file, cancellationToken);
                    var size = new FileInfo(temporary).Length;
                    if (size > limit)
                        throw new IOException("This preview exceeds the configured disk cache budget.");
                    Evict(size);
                    File.Move(temporary, path);
                }
                finally
                {
                    if (File.Exists(temporary))
                        File.Delete(temporary);
                }
            }
            File.SetLastWriteTimeUtc(path, DateTime.UtcNow);
            lock (pins)
                pins[path] = pins.GetValueOrDefault(path) + 1;
            return new Lease(path, () =>
            {
                lock (pins)
                {
                    if (--pins[path] == 0)
                        pins.Remove(path);
                }
            });
        }
        finally
        {
            gate.Release();
        }
    }

    private void Evict(long incoming)
    {
        var files = new DirectoryInfo(directory).GetFiles("*.cache").OrderBy(file => file.LastWriteTimeUtc).ToArray();
        var size = files.Sum(file => file.Length);
        foreach (var file in files)
        {
            if (size + incoming <= limit)
                return;
            lock (pins)
            {
                if (pins.ContainsKey(file.FullName))
                    continue;
            }
            File.Delete(file.FullName);
            size -= file.Length;
        }
        if (size + incoming > limit)
            throw new IOException("Active previews fill the disk cache. Close a preview or increase the cache budget.");
    }

    public async Task<CacheStatistics> StatisticsAsync(CancellationToken cancellationToken = default)
    {
        await gate.WaitAsync(cancellationToken);
        try
        {
            var files = Directory.Exists(directory) ? new DirectoryInfo(directory).GetFiles("*.cache") : [];
            lock (pins)
                return new(files.Sum(file => file.Length), limit, files.Length, pins.Values.Sum());
        }
        finally
        {
            gate.Release();
        }
    }

    public async Task ClearAsync(CancellationToken cancellationToken = default)
    {
        await gate.WaitAsync(cancellationToken);
        try
        {
            if (!Directory.Exists(directory))
                return;
            foreach (var path in Directory.EnumerateFiles(directory, "*.cache"))
            {
                cancellationToken.ThrowIfCancellationRequested();
                lock (pins)
                {
                    if (pins.ContainsKey(path))
                        continue;
                }
                File.Delete(path);
            }
        }
        finally
        {
            gate.Release();
        }
    }

    private sealed class Lease(string path, Action release) : ICacheLease
    {
        private Action? action = release;
        public string Path { get; } = path;
        public void Dispose() => Interlocked.Exchange(ref action, null)?.Invoke();
    }
}
