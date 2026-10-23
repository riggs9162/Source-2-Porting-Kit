using System.Buffers.Binary;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace PortingKit.Core.Automation;

public sealed record AgentOperation(string Name, string Description, JsonElement InputSchema,
    bool ReadOnly = true, bool Destructive = false);
public sealed record AgentRequest(string Root, string Operation, JsonElement Arguments);
public sealed record AgentError(string Code, string Message);
public sealed record AgentResponse(JsonElement? Result = null, AgentError? Error = null);
public sealed class AgentException(string code, string message) : Exception(message)
{
    public string Code { get; } = code;
}

/// <summary>Frames bounded local messages independently of console output or line endings.</summary>
public static class AgentProtocol
{
    public const int MaximumMessageBytes = 4 * 1024 * 1024;
    public static JsonSerializerOptions Json { get; } = new(JsonFiles.Options) { WriteIndented = false };
    public static JsonElement Element(object? value) => JsonSerializer.SerializeToElement(value, Json);
    public static string CanonicalPath(string path) => Path.TrimEndingDirectorySeparator(Path.GetFullPath(path)).ToUpperInvariant();
    public static string PipeName(string state) => "SourceBackport-" + Convert.ToHexString(
        SHA256.HashData(Encoding.UTF8.GetBytes(CanonicalPath(state))))[..32];

    public static async Task WriteAsync<T>(Stream stream, T value, CancellationToken cancellationToken)
    {
        var bytes = JsonSerializer.SerializeToUtf8Bytes(value, Json);
        if (bytes.Length > MaximumMessageBytes)
            throw new AgentException("response_too_large", "The response exceeds 4 MB. Request a smaller page or read an artifact in chunks.");
        var prefix = new byte[4];
        BinaryPrimitives.WriteInt32LittleEndian(prefix, bytes.Length);
        await stream.WriteAsync(prefix, cancellationToken);
        await stream.WriteAsync(bytes, cancellationToken);
        await stream.FlushAsync(cancellationToken);
    }

    public static async Task<T> ReadAsync<T>(Stream stream, CancellationToken cancellationToken)
    {
        var prefix = new byte[4];
        await stream.ReadExactlyAsync(prefix, cancellationToken);
        var length = BinaryPrimitives.ReadInt32LittleEndian(prefix);
        if (length is < 1 or > MaximumMessageBytes)
            throw new AgentException("invalid_request", "Message size must be between 1 byte and 4 MB.");
        var bytes = new byte[length];
        await stream.ReadExactlyAsync(bytes, cancellationToken);
        return JsonSerializer.Deserialize<T>(bytes, Json) ?? throw new ArgumentException("The request cannot be null.");
    }
}
