using System.Reflection;
using System.Text.RegularExpressions;

namespace PortingKit.Core;

public enum AddonState { Discovered, Disabled, Loaded, Failed }
public sealed record AddonInfo(string Id, string Name, string Version, string Description, string Directory,
    AddonState State, string? Error, IReadOnlyList<string> Dependencies);

/// <summary>Discovers directory manifests, resolves dependency order, and isolates failed registrations.</summary>
public sealed class AddonManager(IHostServices host, ContributionCatalog catalog) : IAsyncDisposable
{
    private readonly List<Slot> slots = [];
    private readonly List<Slot> loaded = [];
    public IReadOnlyList<AddonInfo> Snapshot() => slots.Select(slot => slot.Info).ToArray();

    public ISettings GetSettings(string id) => slots.FirstOrDefault(slot => slot.Manifest.Id == id)?.Context?.Settings
        ?? throw new InvalidOperationException("Settings are available only for loaded add-ons.");

    public async Task LoadAsync(CancellationToken cancellationToken = default)
    {
        var root = Path.Combine(host.RootDirectory, "addons");
        if (!Directory.Exists(root))
            return;
        var enabled = host.Settings.Get<Dictionary<string, bool>>("addons", []);
        foreach (var directory in Directory.EnumerateDirectories(root).Order(StringComparer.Ordinal))
        {
            cancellationToken.ThrowIfCancellationRequested();
            var path = Path.Combine(directory, "addon.json");
            if (!File.Exists(path))
                continue;
            AddonManifest? manifest = null;
            try
            {
                manifest = await JsonFiles.ReadAsync<AddonManifest>(path, cancellationToken)
                    ?? throw new InvalidDataException("The manifest is empty.");
                Validate(manifest);
                var state = enabled.GetValueOrDefault(manifest.Id, true) ? AddonState.Discovered : AddonState.Disabled;
                slots.Add(new(manifest, directory, state));
            }
            catch (Exception exception) when (exception is not OperationCanceledException)
            {
                var failedManifest = manifest is not null && !string.IsNullOrWhiteSpace(manifest.Id) ? manifest : new AddonManifest
                {
                    Id = Path.GetFileName(directory), Name = Path.GetFileName(directory), Version = "?",
                    Assembly = "", EntryPoint = ""
                };
                if (failedManifest.Dependencies is null)
                    failedManifest = failedManifest with { Dependencies = [] };
                slots.Add(new(failedManifest, directory, AddonState.Failed) { Error = exception.Message });
                host.Log.Write(LogLevel.Error, "addons", $"{directory}: {exception.Message}");
            }
        }
        foreach (var group in slots.GroupBy(slot => slot.Manifest.Id).Where(group => group.Count() > 1))
            foreach (var slot in group)
                Fail(slot, "Another directory uses this add-on ID.");
        var byId = slots.GroupBy(slot => slot.Manifest.Id).ToDictionary(group => group.Key, group => group.First());
        foreach (var slot in slots)
            await LoadSlotAsync(slot, byId, new HashSet<string>(), cancellationToken);
    }

    private async Task LoadSlotAsync(Slot slot, Dictionary<string, Slot> byId, HashSet<string> visiting,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (slot.State != AddonState.Discovered)
            return;
        if (!visiting.Add(slot.Manifest.Id))
        {
            Fail(slot, "Circular add-on dependency.");
            return;
        }
        foreach (var dependency in slot.Manifest.Dependencies)
        {
            if (!byId.TryGetValue(dependency, out var other))
            {
                Fail(slot, $"Missing dependency: {dependency}");
                return;
            }
            await LoadSlotAsync(other, byId, visiting, cancellationToken);
            if (other.State != AddonState.Loaded)
            {
                Fail(slot, $"Dependency is unavailable: {dependency} ({other.State})");
                return;
            }
        }
        visiting.Remove(slot.Manifest.Id);
        if (slot.State != AddonState.Discovered)
            return;
        try
        {
            var buildError = Path.Combine(slot.Directory, "obj", "v4-build-error.txt");
            if (File.Exists(buildError))
                throw new InvalidOperationException(await File.ReadAllTextAsync(buildError, cancellationToken));
            var configuration = Assembly.GetExecutingAssembly().GetCustomAttribute<AssemblyConfigurationAttribute>()?.Configuration ?? "Release";
            var relative = slot.Manifest.Assembly.Replace("{configuration}", configuration, StringComparison.Ordinal);
            var assemblyPath = JsonFiles.ContainedPath(slot.Directory, relative);
            if (!File.Exists(assemblyPath))
                throw new FileNotFoundException("Build this add-on using Build.ps1 before starting.", assemblyPath);
            slot.Loader = new(assemblyPath);
            var assembly = slot.Loader.LoadFromAssemblyPath(assemblyPath);
            var type = assembly.GetType(slot.Manifest.EntryPoint, throwOnError: true)!;
            slot.Instance = Activator.CreateInstance(type) as IAddon
                ?? throw new InvalidDataException("The entry point must implement IAddon.");
            var dataDirectory = Path.Combine(host.StateDirectory, "addons", slot.Manifest.Id);
            Directory.CreateDirectory(dataDirectory);
            var settings = await SettingsStore.OpenAsync(Path.Combine(dataDirectory, "settings.json"));
            slot.Context = new(slot.Manifest, slot.Directory, dataDirectory, settings, host, catalog);
            slot.Instance.Initialize(slot.Context);
            slot.State = AddonState.Loaded;
            loaded.Add(slot);
            host.Log.Write(LogLevel.Success, "addons", $"Loaded {slot.Manifest.Name} {slot.Manifest.Version}");
        }
        catch (Exception exception) when (exception is not OperationCanceledException)
        {
            await ReleaseAsync(slot);
            Fail(slot, exception.GetBaseException().Message);
        }
    }

    private static void Validate(AddonManifest manifest)
    {
        if (string.IsNullOrWhiteSpace(manifest.Id) || !Regex.IsMatch(manifest.Id, "^[a-z][a-z0-9-]{0,63}$"))
            throw new InvalidDataException("Use a lowercase, hyphenated add-on ID.");
        if (string.IsNullOrWhiteSpace(manifest.Name) || !Version.TryParse(manifest.Version, out _))
            throw new InvalidDataException("An add-on needs a name and a numeric version.");
        if (manifest.ApiVersion != 1)
            throw new InvalidDataException($"Unsupported add-on API: {manifest.ApiVersion}");
        if (string.IsNullOrWhiteSpace(manifest.Assembly) || string.IsNullOrWhiteSpace(manifest.EntryPoint))
            throw new InvalidDataException("An assembly and entry point are required.");
        if (manifest.Dependencies is null || manifest.Dependencies.Any(id => !Regex.IsMatch(id, "^[a-z][a-z0-9-]{0,63}$")))
            throw new InvalidDataException("Invalid dependency IDs.");
    }

    private void Fail(Slot slot, string error)
    {
        slot.State = AddonState.Failed;
        slot.Error = error;
        host.Log.Write(LogLevel.Error, "addons", $"{slot.Manifest.Id}: {error}");
    }

    private async ValueTask ReleaseAsync(Slot slot)
    {
        try
        {
            if (slot.Instance is not null)
                await slot.Instance.DisposeAsync();
        }
        catch (Exception exception)
        {
            host.Log.Write(LogLevel.Error, "addons", $"{slot.Manifest.Id} cleanup: {exception.Message}");
        }
        slot.Context?.Dispose();
        slot.Context = null;
        slot.Instance = null;
        slot.Loader?.Unload();
        slot.Loader = null;
    }

    public async ValueTask DisposeAsync()
    {
        foreach (var slot in loaded.AsEnumerable().Reverse())
            await ReleaseAsync(slot);
        loaded.Clear();
    }

    private sealed class Slot(AddonManifest manifest, string directory, AddonState state)
    {
        public AddonManifest Manifest { get; } = manifest;
        public string Directory { get; } = directory;
        public AddonState State { get; set; } = state;
        public string? Error { get; set; }
        public IAddon? Instance { get; set; }
        public AddonContext? Context { get; set; }
        public AddonLoadContext? Loader { get; set; }
        public AddonInfo Info => new(Manifest.Id, Manifest.Name, Manifest.Version, Manifest.Description,
            Directory, State, Error, Manifest.Dependencies);
    }
}
