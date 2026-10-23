using System.Text.Json;
using PortingKit.Desktop;

namespace PortingKit.Addons.Hotspot;

public sealed class HotspotAddon : IAddon
{
    private HotspotSession? session;
    public void Initialize(IAddonContext context)
    {
        session = new(context);
        context.Register<IDesktopPage>("editor", new HotspotPage(context, session));
        foreach (var operation in new[] { "inspect", "generate", "convert", "detect", "link-material" })
            context.Register<ITool>(operation, new HotspotTool(operation));
    }
    public async ValueTask DisposeAsync()
    {
        if (session is not null) await session.FlushAsync();
    }
}

internal sealed record HotspotDraft
{
    public HotspotDocument Document { get; init; } = new(1024, 1024, []);
    public string Texture { get; init; } = "";
    public string Material { get; init; } = "";
    public string RectPath { get; init; } = "";
    public string Format { get; init; } = "hammerpp";
    public bool Dirty { get; init; }
    public int Grid { get; init; } = 16;
    public bool Snap { get; init; } = true;
    public bool ShowGrid { get; init; } = true;
    public string GridStyle { get; init; } = "Lines";
    public string GridColor { get; init; } = "White";
    public int GridOpacity { get; init; } = 25;
    public int GridMajor { get; init; } = 4;
    public bool WriteFlags { get; init; } = true;
    public bool LinkMaterial { get; init; }
}

/// <summary>Owns edit history and an automatically saved recovery draft, without retaining decoded images.</summary>
internal sealed class HotspotSession
{
    private readonly IAddonContext addon;
    private readonly string draftPath;
    private readonly List<HotspotDocument> undo = [];
    private readonly List<HotspotDocument> redo = [];
    private Task pending = Task.CompletedTask;
    public HotspotDraft Draft { get; private set; } = new();
    public event EventHandler? Changed;
    public string? RecoveryError { get; }

    public HotspotSession(IAddonContext addon)
    {
        this.addon = addon;
        draftPath = Path.Combine(addon.DataDirectory, "hotspot-draft.json");
        if (!File.Exists(draftPath)) return;
        try
        {
            if (new FileInfo(draftPath).Length > 4 * 1024 * 1024) throw new InvalidDataException("Recovery draft is too large.");
            Draft = JsonSerializer.Deserialize<HotspotDraft>(File.ReadAllText(draftPath), HotspotDocument.JsonOptions) ?? new();
            Draft.Document.CheckStructure();
            Draft = Draft with { Grid = Math.Clamp(Draft.Grid, 1, 1024), GridOpacity = Math.Clamp(Draft.GridOpacity, 1, 100), GridMajor = Math.Clamp(Draft.GridMajor, 1, 32) };
        }
        catch (Exception ex) when (ex is IOException or JsonException or ArgumentException)
        {
            RecoveryError = "Could not restore the previous draft: " + ex.Message;
            Draft = new();
        }
    }

    public void Replace(HotspotDraft draft)
    {
        draft.Document.CheckStructure();
        Draft = draft;
        undo.Clear(); redo.Clear();
        Notify();
    }

    public void Edit(HotspotDocument doc)
    {
        doc.CheckStructure();
        if (doc.Width == Draft.Document.Width && doc.Height == Draft.Document.Height && doc.Rectangles.SequenceEqual(Draft.Document.Rectangles)) return;
        undo.Add(Draft.Document);
        if (undo.Count > 64) undo.RemoveAt(0);
        redo.Clear();
        Draft = Draft with { Document = doc, Dirty = true };
        Notify();
    }

    public void Undo(bool forward = false)
    {
        var from = forward ? redo : undo; var to = forward ? undo : redo;
        if (from.Count == 0) return;
        to.Add(Draft.Document);
        Draft = Draft with { Document = from[^1], Dirty = true };
        from.RemoveAt(from.Count - 1);
        Notify();
    }

    public void Update(Func<HotspotDraft, HotspotDraft> update)
    {
        Draft = update(Draft);
        Notify();
    }

    private void Notify()
    {
        var snapshot = Draft;
        pending = PersistAsync(pending, snapshot);
        Changed?.Invoke(this, EventArgs.Empty);
    }

    private async Task PersistAsync(Task previous, HotspotDraft snapshot)
    {
        await previous.ConfigureAwait(false);
        try
        {
            await Task.Run(async () => await HotspotFormats.SaveAsync(draftPath,
                JsonSerializer.Serialize(snapshot, HotspotDocument.JsonOptions), false, CancellationToken.None)).ConfigureAwait(false);
        }
        catch (Exception ex) { addon.Host.Log.Write(LogLevel.Error, addon.Manifest.Id, "Hotspot draft could not be saved: " + ex.Message); }
    }

    public Task FlushAsync() => pending;
}
