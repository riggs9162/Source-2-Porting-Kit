using System.Security.Cryptography;
using System.Text.Json;

namespace PortingKit.Addons.Utilities;

/// <summary>Writes file records incrementally, with optional streaming SHA-256 checksums.</summary>
public sealed class InventoryTool : ITool
{
    public ToolDefinition Definition { get; } = new("Inventory files", "Utilities",
        "Lists files without loading their contents into memory. Optional checksums read one file at a time.",
        [
            new("directory", "Source folder", ParameterKind.Directory, Required: true),
            new("recursive", "Include subfolders", ParameterKind.Boolean, "true"),
            new("hash", "Calculate SHA-256 checksums", ParameterKind.Boolean, "false"),
            new("maximum", "Maximum files", ParameterKind.Integer, "10000", Minimum: 1, Maximum: 1000000)
        ]);

    public async Task<ToolResult> RunAsync(ToolContext context, ToolArguments arguments, CancellationToken cancellationToken)
    {
        var directory = Path.GetFullPath(arguments.Text("directory"));
        if (!Directory.Exists(directory))
            throw new DirectoryNotFoundException(directory);
        var path = Path.Combine(context.OutputDirectory, "inventory.jsonl");
        var options = new EnumerationOptions
        {
            RecurseSubdirectories = arguments.Boolean("recursive"), IgnoreInaccessible = true,
            AttributesToSkip = FileAttributes.ReparsePoint
        };
        var count = 0;
        long total = 0;
        var maximum = arguments.Integer("maximum");
        await using var writer = new StreamWriter(path);
        foreach (var file in Directory.EnumerateFiles(directory, "*", options))
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (count >= maximum)
            {
                context.Log($"Stopped at the configured limit of {maximum} files.", LogLevel.Warning);
                break;
            }
            var info = new FileInfo(file);
            string? hash = null;
            if (arguments.Boolean("hash"))
            {
                await using var stream = new FileStream(file, FileMode.Open, FileAccess.Read, FileShare.Read,
                    65536, FileOptions.Asynchronous | FileOptions.SequentialScan);
                hash = Convert.ToHexStringLower(await SHA256.HashDataAsync(stream, cancellationToken));
            }
            await writer.WriteLineAsync(JsonSerializer.Serialize(new
            {
                path = Path.GetRelativePath(directory, file), bytes = info.Length,
                modifiedUtc = info.LastWriteTimeUtc, sha256 = hash
            }).AsMemory(), cancellationToken);
            count++;
            total += info.Length;
            if (count % 100 == 0)
                context.Report(null, $"{count:N0} files / {total / 1048576.0:N1} MB");
        }
        return new($"Inventoried {count:N0} files ({total / 1048576.0:N1} MB)", [new(path, "File inventory")]);
    }
}
