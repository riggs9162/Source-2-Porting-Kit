using System.Text.Json;

namespace PortingKit.Core;

public sealed class SettingsStore : ISettings
{
    private readonly string path;
    private readonly SemaphoreSlim writer = new(1);
    private Dictionary<string, JsonElement> values;

    private SettingsStore(string path, Dictionary<string, JsonElement> values)
    {
        this.path = path;
        this.values = values;
    }

    public static async Task<SettingsStore> OpenAsync(string path)
    {
        try
        {
            return new(path, await JsonFiles.ReadAsync<Dictionary<string, JsonElement>>(path) ?? []);
        }
        catch (JsonException exception)
        {
            throw new InvalidDataException($"Settings are invalid: {path}. Preserve or repair this file before starting.", exception);
        }
    }

    public T Get<T>(string key, T fallback)
    {
        var snapshot = Volatile.Read(ref values);
        if (!snapshot.TryGetValue(key, out var value))
            return fallback;
        try
        {
            return value.Deserialize<T>(JsonFiles.Options) ?? fallback;
        }
        catch (JsonException)
        {
            return fallback;
        }
    }

    public async Task SetAsync<T>(string key, T value, CancellationToken cancellationToken = default)
    {
        await writer.WaitAsync(cancellationToken);
        try
        {
            var next = new Dictionary<string, JsonElement>(values)
            {
                [key] = JsonSerializer.SerializeToElement(value, JsonFiles.Options)
            };
            await JsonFiles.WriteAsync(path, next, cancellationToken);
            Volatile.Write(ref values, next);
        }
        finally
        {
            writer.Release();
        }
    }
}
