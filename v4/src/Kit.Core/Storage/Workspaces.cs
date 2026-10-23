namespace PortingKit.Core;

public sealed class Workspaces(ISettings settings, IEventHub events) : IWorkspaces
{
    private readonly SemaphoreSlim gate = new(1);
    public Workspace? Current { get; private set; }
    public IReadOnlyList<string> Recent => settings.Get<string[]>("recentWorkspaces", []);

    public async Task<Workspace> CreateAsync(string directory, CancellationToken cancellationToken = default)
    {
        var root = Path.GetFullPath(directory);
        Directory.CreateDirectory(root);
        var path = Path.Combine(root, ".s2kit-workspace.json");
        if (File.Exists(path))
            return await OpenAsync(root, cancellationToken);
        var workspace = new Workspace(new DirectoryInfo(root).Name, root, Path.Combine(root, "output"));
        await JsonFiles.WriteAsync(path, workspace, cancellationToken);
        return await OpenAsync(root, cancellationToken);
    }

    public async Task<Workspace> OpenAsync(string directory, CancellationToken cancellationToken = default)
    {
        await gate.WaitAsync(cancellationToken);
        Workspace workspace;
        try
        {
            var root = Path.GetFullPath(directory);
            var saved = await JsonFiles.ReadAsync<Workspace>(Path.Combine(root, ".s2kit-workspace.json"), cancellationToken)
                ?? throw new FileNotFoundException("This folder does not contain a V4 workspace.");
            workspace = saved with { RootDirectory = root, OutputDirectory = Path.Combine(root, "output") };
            var recent = new[] { root }.Concat(Recent).Distinct(StringComparer.OrdinalIgnoreCase).Take(8).ToArray();
            await settings.SetAsync("recentWorkspaces", recent, cancellationToken);
            Current = workspace;
        }
        finally
        {
            gate.Release();
        }
        await events.PublishAsync(new WorkspaceChanged(workspace), cancellationToken);
        return workspace;
    }
}
