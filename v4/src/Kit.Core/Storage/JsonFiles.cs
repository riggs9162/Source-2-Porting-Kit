using System.Text.Json;
using System.Text.Json.Serialization;

namespace PortingKit.Core;

/// <summary>Writes complete JSON documents beside their destination before atomic replacement.</summary>
public static class JsonFiles
{
    public static JsonSerializerOptions Options { get; } = new(JsonSerializerDefaults.Web)
    {
        WriteIndented = true,
        Converters = { new JsonStringEnumConverter() }
    };

    public static async Task WriteAsync<T>(string path, T value, CancellationToken cancellationToken = default)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        var temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            await using (var file = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None,
                65536, FileOptions.Asynchronous | FileOptions.WriteThrough))
            {
                await JsonSerializer.SerializeAsync(file, value, Options, cancellationToken);
                await file.FlushAsync(cancellationToken);
            }

            for (var attempt = 0; ; attempt++)
            {
                try
                {
                    File.Move(temporary, path, true);
                    break;
                }
                catch (IOException) when (attempt < 4)
                {
                    await Task.Delay(30 * (attempt + 1), cancellationToken);
                }
            }
        }
        finally
        {
            if (File.Exists(temporary))
                File.Delete(temporary);
        }
    }

    public static async Task<T?> ReadAsync<T>(string path, CancellationToken cancellationToken = default)
    {
        if (!File.Exists(path))
            return default;
        await using var file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read,
            65536, FileOptions.Asynchronous);
        return await JsonSerializer.DeserializeAsync<T>(file, Options, cancellationToken);
    }

    public static string ContainedPath(string directory, string relative)
    {
        if (string.IsNullOrWhiteSpace(relative) || Path.IsPathRooted(relative))
            throw new ArgumentException("Expected a relative path.");
        var root = Path.GetFullPath(directory) + Path.DirectorySeparatorChar;
        var path = Path.GetFullPath(Path.Combine(directory, relative));
        if (!path.StartsWith(root, StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException("The path leaves its owning directory.");
        return path;
    }
}
