using PortingKit.Desktop;

namespace PortingKit.Addons.AssetBrowser;

public sealed class AssetBrowserAddon : IAddon
{
    public void Initialize(IAddonContext context)
    {
        context.Register<IAssetProvider>("files", new FolderProvider());
        context.Register<IAssetProvider>("vpk", new VpkProvider());
        context.Register<IDesktopPage>("browser", new BrowserPage(context));
        context.Register<ITool>("list", new ListAssetsTool());
    }

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}

internal sealed class ListAssetsTool : ITool
{
    public ToolDefinition Definition { get; } = new("List archive or folder", "Assets",
        "Writes one bounded page of matching asset metadata. No textures, meshes, or audio are decoded.",
        [
            new("container", "Folder or VPK", ParameterKind.File, Required: true),
            new("search", "Search"),
            new("offset", "Offset", ParameterKind.Integer, "0", Minimum: 0, Maximum: int.MaxValue),
            new("count", "Page size", ParameterKind.Integer, "100", Minimum: 1, Maximum: 200)
        ]);

    public async Task<ToolResult> RunAsync(ToolContext context, ToolArguments arguments, CancellationToken cancellationToken)
    {
        var container = arguments.Text("container");
        var provider = context.Host.Contributions.GetAll<IAssetProvider>().FirstOrDefault(item => item.Value.CanOpen(container))
            ?? throw new ArgumentException("Select an existing folder or a VPK file.");
        var page = await provider.Value.ListAsync(container, arguments.Text("search"), arguments.Integer("offset"),
            arguments.Integer("count"), cancellationToken);
        var path = Path.Combine(context.OutputDirectory, "assets.json");
        await using (var file = File.Create(path))
            await System.Text.Json.JsonSerializer.SerializeAsync(file, page, cancellationToken: cancellationToken);
        return new($"Listed {page.Items.Count} assets{(page.HasMore ? "; more available" : "")}",
            [new(path, "Asset list")], System.Text.Json.JsonSerializer.SerializeToElement(page));
    }
}
