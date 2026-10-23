using PortingKit.Desktop;

namespace PortingKit.Addons.AssetBrowser;

internal sealed class BrowserPage(IAddonContext addon) : IDesktopPage
{
    public string Name => "Asset browser";
    public string Category => "Workspaces";
    public string Description => "Browse local folders and VPK metadata without decoding their assets.";
    public Control Create(DesktopContext context) => new BrowserControl(addon, context);
}

internal sealed class BrowserControl : UserControl
{
    private readonly IAddonContext addon;
    private readonly DesktopContext context;
    private readonly TextBox location = new() { Dock = DockStyle.Fill, PlaceholderText = "Folder or _dir.vpk path" };
    private readonly TextBox search = new() { Width = 240, PlaceholderText = "Filter paths" };
    private readonly DataGridView grid = KitStyle.Grid();
    private readonly Label status = new() { AutoSize = true, Padding = new(4, 8, 4, 0) };
    private readonly Button next;
    private readonly Button previous;
    private CancellationTokenSource? loading;
    private AssetPage? page;
    private int offset;

    public BrowserControl(IAddonContext addon, DesktopContext context)
    {
        this.addon = addon;
        this.context = context;
        Dock = DockStyle.Fill;
        Padding = new(20);
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 5 };
        layout.RowStyles.Add(new(SizeType.Absolute, KitStyle.Scale(56)));
        layout.RowStyles.Add(new(SizeType.Absolute, KitStyle.Scale(44)));
        layout.RowStyles.Add(new(SizeType.Absolute, KitStyle.Scale(44)));
        layout.RowStyles.Add(new(SizeType.Percent, 100));
        layout.RowStyles.Add(new(SizeType.Absolute, KitStyle.Scale(42)));
        layout.Controls.Add(KitStyle.Label("Asset browser", 20, true));
        var pathRow = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 4 };
        pathRow.ColumnStyles.Add(new(SizeType.Percent, 100));
        for (var index = 0; index < 3; index++)
            pathRow.ColumnStyles.Add(new(SizeType.Absolute, 100));
        pathRow.Controls.Add(location);
        pathRow.Controls.Add(KitStyle.Button("Folder...", PickFolder));
        pathRow.Controls.Add(KitStyle.Button("VPK...", PickArchive));
        pathRow.Controls.Add(KitStyle.Button("Open", async (_, _) => await LoadAsync(true)));
        layout.Controls.Add(pathRow);
        var filterRow = new FlowLayoutPanel { Dock = DockStyle.Fill, WrapContents = false };
        filterRow.Controls.Add(search);
        filterRow.Controls.Add(KitStyle.Button("Search", async (_, _) => await LoadAsync(true)));
        filterRow.Controls.Add(KitStyle.Button("Up", async (_, _) =>
        {
            var parent = Directory.GetParent(location.Text)?.FullName;
            if (parent is not null)
            {
                location.Text = parent;
                await LoadAsync(true);
            }
        }));
        filterRow.Controls.Add(KitStyle.Button("Use in export", UseInExport));
        layout.Controls.Add(filterRow);
        grid.Columns.Add("name", "Asset path");
        grid.Columns.Add("kind", "Type");
        grid.Columns.Add("size", "Size");
        grid.Columns[0].FillWeight = 75;
        grid.Columns[1].FillWeight = 12;
        grid.Columns[2].FillWeight = 13;
        grid.CellDoubleClick += OpenDirectory;
        layout.Controls.Add(grid);
        var paging = new FlowLayoutPanel { Dock = DockStyle.Fill };
        previous = KitStyle.Button("Previous", async (_, _) => { offset = Math.Max(0, offset - 100); await LoadAsync(false); });
        next = KitStyle.Button("Next", async (_, _) => { offset += 100; await LoadAsync(false); });
        paging.Controls.AddRange([previous, next, status]);
        layout.Controls.Add(paging);
        Controls.Add(layout);
        location.Text = addon.Settings.Get("lastContainer", context.Host.RootDirectory);
        location.KeyDown += async (_, e) =>
        {
            if (e.KeyCode != Keys.Enter)
                return;
            e.SuppressKeyPress = true;
            await LoadAsync(true);
        };
        Load += async (_, _) => await LoadAsync(true);
        KitStyle.Apply(this);
    }

    private async Task LoadAsync(bool reset)
    {
        loading?.Cancel();
        var request = new CancellationTokenSource();
        loading = request;
        if (reset)
            offset = 0;
        try
        {
            status.Text = "Reading metadata...";
            var path = location.Text.Trim().Trim('"');
            var provider = context.Host.Contributions.GetAll<IAssetProvider>().FirstOrDefault(item => item.Value.CanOpen(path))
                ?? throw new ArgumentException("Select an existing folder or a VPK archive.");
            var loadedPage = await provider.Value.ListAsync(path, search.Text, offset, 100, request.Token);
            if (request.IsCancellationRequested || IsDisposed)
                return;
            page = loadedPage;
            grid.Rows.Clear();
            foreach (var item in page.Items)
                grid.Rows.Add(item.Name, item.Kind, item.Kind == "directory" ? "" : $"{item.Size / 1024.0:N1} KB");
            previous.Enabled = offset > 0;
            next.Enabled = page.HasMore;
            status.Text = $"{offset + (page.Items.Count > 0 ? 1 : 0):N0}-{offset + page.Items.Count:N0}{(page.HasMore ? " / more available" : " / end")}";
            await addon.Settings.SetAsync("lastContainer", path, request.Token);
        }
        catch (OperationCanceledException) when (request.IsCancellationRequested) { }
        catch (Exception exception)
        {
            if (!IsDisposed && !request.IsCancellationRequested)
                status.Text = exception.Message;
        }
        finally
        {
            if (ReferenceEquals(loading, request))
                loading = null;
            request.Dispose();
        }
    }

    private async void PickFolder(object? sender, EventArgs e)
    {
        using var dialog = new FolderBrowserDialog();
        if (dialog.ShowDialog(this) == DialogResult.OK)
        {
            location.Text = dialog.SelectedPath;
            await LoadAsync(true);
        }
    }

    private async void PickArchive(object? sender, EventArgs e)
    {
        using var dialog = new OpenFileDialog { Filter = "VPK archives|*.vpk", CheckFileExists = true };
        if (dialog.ShowDialog(this) == DialogResult.OK)
        {
            location.Text = dialog.FileName;
            await LoadAsync(true);
        }
    }

    private async void OpenDirectory(object? sender, DataGridViewCellEventArgs e)
    {
        if (e.RowIndex < 0 || page is null || e.RowIndex >= page.Items.Count)
            return;
        var asset = page.Items[e.RowIndex];
        if (asset.Kind != "directory")
            return;
        location.Text = Path.Combine(asset.Reference.Container, asset.Reference.Path);
        search.Clear();
        await LoadAsync(true);
    }

    private async void UseInExport(object? sender, EventArgs e)
    {
        try
        {
            var selected = grid.CurrentRow?.Index;
            var asset = selected is { } index && page is not null && index < page.Items.Count ? page.Items[index] : null;
            var input = asset?.Reference.Provider == "asset-browser/files"
                ? Path.Combine(asset.Reference.Container, asset.Reference.Path) : location.Text;
            var filter = asset?.Reference.Provider == "asset-browser/vpk" ? asset.Reference.Path : "";
            await context.Host.Settings.SetAsync("draft.vrf/export", new Dictionary<string, string> { ["input"] = input, ["filter"] = filter });
            context.OpenTool("vrf/export");
        }
        catch (Exception exception)
        {
            KitStyle.ShowError(this, exception);
        }
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
            loading?.Cancel();
        base.Dispose(disposing);
    }
}
