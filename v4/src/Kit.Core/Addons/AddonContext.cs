namespace PortingKit.Core;

internal sealed class AddonContext(
    AddonManifest manifest,
    string directory,
    string dataDirectory,
    ISettings settings,
    IHostServices host,
    ContributionCatalog catalog) : IAddonContext, IDisposable
{
    private readonly List<IDisposable> subscriptions = [];
    public AddonManifest Manifest { get; } = manifest;
    public string Directory { get; } = directory;
    public string DataDirectory { get; } = dataDirectory;
    public ISettings Settings { get; } = settings;
    public IHostServices Host { get; } = host;

    public void Register<T>(string id, T contribution) where T : class => catalog.Add(Manifest.Id, id, contribution);

    public void Subscribe<T>(Func<T, CancellationToken, ValueTask> handler) => subscriptions.Add(Host.Events.Subscribe(handler));

    public void Dispose()
    {
        foreach (var subscription in subscriptions)
            subscription.Dispose();
        subscriptions.Clear();
        catalog.Remove(Manifest.Id);
    }
}
