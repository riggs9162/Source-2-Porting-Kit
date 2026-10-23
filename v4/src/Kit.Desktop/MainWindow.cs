using System.Diagnostics;
using PortingKit.Core;

namespace PortingKit.Desktop;

internal sealed class MainWindow : Form
{
    private readonly KitHost host;
    private readonly TreeView navigation = new() { Dock = DockStyle.Left, Width = 246, BorderStyle = BorderStyle.None,
        ShowLines = false, ShowPlusMinus = false, ShowRootLines = false, HideSelection = false, FullRowSelect = true,
        ItemHeight = 34, Indent = 14 };
    private readonly Panel content = new() { Dock = DockStyle.Fill };
    private readonly Label status = new() { Dock = DockStyle.Bottom, Height = 30, Padding = new(16, 5, 16, 0) };
    private readonly System.Windows.Forms.Timer timer = new() { Interval = 1000 };
    private readonly Dictionary<string, TreeNode> nodes = [];
    private Control? activePage;
    private bool closing;
    private bool readyToClose;

    public MainWindow(KitHost host)
    {
        this.host = host;
        Text = "Source 2 Porting Kit / V4";
        MinimumSize = new(900, 640);
        Size = new(1320, 880);
        StartPosition = FormStartPosition.CenterScreen;
        BackColor = KitStyle.Background;
        var header = new Label
        {
            Text = "SOURCE 2   /   PORTING KIT V4", Dock = DockStyle.Top, Height = 58,
            Padding = new(18, 16, 0, 0), Font = new("Segoe UI", 14, FontStyle.Bold)
        };
        var menu = BuildMenu();
        Controls.Add(content);
        Controls.Add(navigation);
        Controls.Add(status);
        Controls.Add(header);
        Controls.Add(menu);
        MainMenuStrip = menu;
        AddNavigation("overview", "Overview", () => new OverviewPage(host));
        AddNavigation("jobs", "Jobs", () => new JobsPage(host));
        AddNavigation("addons", "Add-ons", () => new AddonsPage(host));
        AddNavigation("settings", "Settings", () => new SettingsPage(host));
        var categories = new Dictionary<string, TreeNode>();
        TreeNode Category(string name)
        {
            if (!categories.TryGetValue(name, out var node))
            {
                node = navigation.Nodes.Add(name.ToUpperInvariant());
                categories.Add(name, node);
            }
            return node;
        }
        foreach (var page in host.Contributions.GetAll<IDesktopPage>().OrderBy(page => page.Value.Name))
            AddNavigation(page.Id, page.Value.Name, () => page.Value.Create(new(host, SelectPage)), Category(page.Value.Category));
        foreach (var tool in host.Contributions.GetAll<ITool>().OrderBy(tool => tool.Value.Definition.Category).ThenBy(tool => tool.Value.Definition.Name))
            AddNavigation(tool.Id, tool.Value.Definition.Name, () => new ToolPage(host, tool, () => SelectPage("jobs")),
                Category(tool.Value.Definition.Category));
        navigation.AfterSelect += (_, e) =>
        {
            if (e.Node?.Tag is Func<Control> create)
                ShowPage(create);
        };
        navigation.ExpandAll();
        KitStyle.Apply(this);
        navigation.BackColor = KitStyle.Background;
        navigation.LineColor = KitStyle.Border;
        header.ForeColor = KitStyle.Accent;
        status.ForeColor = KitStyle.Muted;
        timer.Tick += (_, _) => RefreshStatus();
        timer.Start();
        FormClosing += OnClosing;
        FormClosed += (_, _) => timer.Dispose();
        Shown += (_, _) => SelectPage("overview");
    }

    private MenuStrip BuildMenu()
    {
        var menu = new MenuStrip { Dock = DockStyle.Top };
        var workspace = new ToolStripMenuItem("Workspace");
        workspace.DropDownItems.Add("New workspace...", null, async (_, _) => await PickWorkspaceAsync(true));
        workspace.DropDownItems.Add("Open workspace...", null, async (_, _) => await PickWorkspaceAsync(false));
        foreach (var path in host.Workspaces.Recent)
            workspace.DropDownItems.Add(path, null, async (_, _) => await OpenWorkspaceAsync(path));
        menu.Items.Add(workspace);
        menu.Items.Add("Show logs", null, (_, _) => DesktopActions.Reveal(Path.Combine(host.StateDirectory, "logs")));
        menu.Items.Add("Show add-ons", null, (_, _) => DesktopActions.Reveal(Path.Combine(host.RootDirectory, "addons")));
        return menu;
    }

    private void AddNavigation(string id, string name, Func<Control> create, TreeNode? parent = null)
    {
        var node = new TreeNode(name) { Tag = create };
        (parent?.Nodes ?? navigation.Nodes).Add(node);
        nodes[id] = node;
    }

    private void SelectPage(string id)
    {
        if (nodes.TryGetValue(id, out var node))
        {
            if (navigation.SelectedNode == node && node.Tag is Func<Control> create)
                ShowPage(create);
            else
                navigation.SelectedNode = node;
        }
        else
            MessageBox.Show(this, "This tool is not available. Check its add-on in Add-ons.", "Source 2 Porting Kit");
    }

    private void ShowPage(Func<Control> create)
    {
        content.SuspendLayout();
        try
        {
            content.Controls.Clear();
            activePage?.Dispose();
            activePage = null;
            activePage = create();
            activePage.Dock = DockStyle.Fill;
            content.Controls.Add(activePage);
            KitStyle.Apply(activePage);
        }
        catch (Exception exception)
        {
            host.Log.Write(LogLevel.Error, "desktop", exception.ToString());
            content.Controls.Add(new Label { Text = exception.Message, Dock = DockStyle.Fill, Padding = new(24), ForeColor = KitStyle.Text });
        }
        finally
        {
            content.ResumeLayout();
        }
    }

    private async Task PickWorkspaceAsync(bool create)
    {
        using var dialog = new FolderBrowserDialog { Description = create ? "Choose a project folder for the new workspace" : "Choose an existing V4 workspace" };
        if (dialog.ShowDialog(this) != DialogResult.OK)
            return;
        try
        {
            if (create)
                await host.Workspaces.CreateAsync(dialog.SelectedPath);
            else
                await host.Workspaces.OpenAsync(dialog.SelectedPath);
            RefreshStatus();
        }
        catch (Exception exception)
        {
            KitStyle.ShowError(this, exception);
        }
    }

    private async Task OpenWorkspaceAsync(string path)
    {
        try
        {
            await host.Workspaces.OpenAsync(path);
            RefreshStatus();
        }
        catch (Exception exception)
        {
            KitStyle.ShowError(this, exception);
        }
    }

    private void RefreshStatus()
    {
        using var process = Process.GetCurrentProcess();
        var active = host.Jobs.Snapshot().Count(job => JobQueue.IsActive(job.Status));
        status.Text = $"{host.Workspaces.Current?.Name ?? "No workspace selected"}    |    {active} active jobs    |    {process.WorkingSet64 / 1048576.0:N0} MB RAM    |    .NET {Environment.Version.Major}";
    }

    private async void OnClosing(object? sender, FormClosingEventArgs e)
    {
        if (readyToClose)
            return;
        e.Cancel = true;
        if (closing)
            return;
        closing = true;
        Enabled = false;
        timer.Stop();
        activePage?.Dispose();
        activePage = null;
        try
        {
            await host.Queue.DisposeAsync();
            readyToClose = true;
            Close();
        }
        catch (Exception exception)
        {
            closing = false;
            Enabled = true;
            timer.Start();
            KitStyle.ShowError(this, exception);
        }
    }
}

internal static class DesktopActions
{
    public static void Reveal(string path)
    {
        var directory = Directory.Exists(path) ? path : Path.GetDirectoryName(path);
        if (directory is null || !Directory.Exists(directory))
            return;
        var start = new ProcessStartInfo("explorer.exe") { UseShellExecute = false };
        start.ArgumentList.Add(directory);
        Process.Start(start)?.Dispose();
    }
}
