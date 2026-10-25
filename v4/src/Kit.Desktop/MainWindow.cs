using System.Diagnostics;
using PortingKit.Core;
using PortingKit.Core.Automation;

namespace PortingKit.Desktop;

internal sealed class MainWindow : Form
{
    private readonly KitHost host;
    private readonly NavigationTree navigation = new();
    private readonly Panel sidebar = new() { Dock = DockStyle.Fill };
    private readonly Panel content = new() { Dock = DockStyle.Fill };
    private readonly Panel toolbar = new() { Dock = DockStyle.Top, Height = 76, Padding = new(24, 17, 20, 0) };
    private readonly Label breadcrumb = new() { Dock = DockStyle.Fill, AutoEllipsis = true, TextAlign = ContentAlignment.MiddleLeft };
    private readonly Label status = new() { Dock = DockStyle.Bottom, Height = 32, Padding = new(24, 7, 16, 0) };
    private readonly Label edition = new() { Text = "V4  /  LOCAL WORKSPACE", Dock = DockStyle.Bottom, Height = 44, Padding = new(20, 12, 0, 0) };
    private readonly TableLayoutPanel shell = new() { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1 };
    private readonly System.Windows.Forms.Timer timer = new() { Interval = 1000 };
    private readonly Dictionary<string, TreeNode> nodes = [];
    private Control? activePage;
    private bool closing;
    private bool readyToClose;
    private readonly IDisposable agentSettings;

    public MainWindow(KitHost host)
    {
        this.host = host;
        agentSettings = host.Events.Subscribe<AgentSettingsChanged>((change, _) =>
        {
            if (change.AddonId is null && change.Key == "appearance" && IsHandleCreated && !IsDisposed)
                BeginInvoke(() => { if (!IsDisposed) KitStyle.Configure(host.Settings.Get("appearance", new AppearanceOptions())); });
            return ValueTask.CompletedTask;
        });
        KitStyle.Configure(host.Settings.Get("appearance", new AppearanceOptions()));
        Text = AppIdentity.Name + "  |  " + AppIdentity.Purpose;
        AutoScaleMode = AutoScaleMode.Dpi;
        MinimumSize = new(1000, 700);
        Size = new(1380, 920);
        StartPosition = FormStartPosition.CenterScreen;
        Icon = AppIdentity.CreateIcon();
        shell.ColumnStyles.Add(new(SizeType.Absolute, 252));
        shell.ColumnStyles.Add(new(SizeType.Percent, 100));
        sidebar.Controls.Add(navigation);
        sidebar.Controls.Add(edition);
        sidebar.Controls.Add(new BrandPanel());
        var workspace = new Panel { Dock = DockStyle.Fill };
        workspace.Controls.Add(content);
        workspace.Controls.Add(status);
        workspace.Controls.Add(toolbar);
        shell.Controls.Add(sidebar, 0, 0);
        shell.Controls.Add(workspace, 1, 0);
        Controls.Add(shell);
        var commandLayout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1 };
        commandLayout.ColumnStyles.Add(new(SizeType.Percent, 100));
        commandLayout.ColumnStyles.Add(new(SizeType.AutoSize));
        var commands = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, WrapContents = false };
        commands.Controls.Add(KitStyle.Button("New workspace", async (_, _) => await PickWorkspaceAsync(true)));
        commands.Controls.Add(KitStyle.Button("Open workspace", async (_, _) => await PickWorkspaceAsync(false)));
        commands.Controls.Add(KitStyle.Button("Appearance", (_, _) => SelectPage("appearance")));
        commandLayout.Controls.Add(breadcrumb);
        commandLayout.Controls.Add(commands);
        toolbar.Controls.Add(commandLayout);
        AddNavigation("overview", "Workshop", CreateOverview);
        AddNavigation("jobs", "Jobs & outputs", () => new JobsPage(host));
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
        foreach (var name in new[] { "Materials", "Models", "Sounds" })
            Category(name);
        foreach (var page in host.Contributions.GetAll<IDesktopPage>().OrderBy(page => page.Value.Name))
            AddNavigation(page.Id, page.Value.Name, () => page.Value.Create(new(host, SelectPage)), Category(page.Value.Category));
        foreach (var tool in host.Contributions.GetAll<ITool>().Where(tool => tool.Value.Definition.ShowInDesktop)
            .OrderBy(tool => tool.Value.Definition.Category).ThenBy(tool => tool.Value.Definition.Name))
            AddNavigation(tool.Id, tool.Value.Definition.Name, () => new ToolPage(host, tool, () => SelectPage("jobs")),
                Category(tool.Value.Definition.Category));
        var application = Category("Application");
        AddNavigation("addons", "Add-ons", () => new AddonsPage(host), application);
        AddNavigation("appearance", "Appearance", () => new AppearancePage(host), application);
        AddNavigation("settings", "Settings", () => new SettingsPage(host), application);
        navigation.AfterSelect += (_, e) =>
        {
            if (e.Node?.Tag is Func<Control> create)
            {
                breadcrumb.Text = (e.Node.Parent?.Text ?? "WORKSPACE") + "  /  " + e.Node.Text;
                ShowPage(create);
            }
        };
        navigation.BeforeCollapse += (_, e) => e.Cancel = true;
        navigation.ExpandAll();
        KitStyle.AppearanceChanged += AppearanceChanged;
        ApplyAppearance();
        timer.Tick += (_, _) => RefreshStatus();
        timer.Start();
        FormClosing += OnClosing;
        FormClosed += (_, _) =>
        {
            timer.Dispose();
            agentSettings.Dispose();
            KitStyle.AppearanceChanged -= AppearanceChanged;
        };
        Shown += (_, _) =>
        {
            NativeChrome.Apply(this);
            SelectPage("overview");
        };
    }

    private Control CreateOverview() => new OverviewPage(host, SelectPage, PickWorkspaceAsync, OpenWorkspaceAsync);

    private void AppearanceChanged(object? sender, EventArgs e)
    {
        ApplyAppearance();
        var previous = Icon;
        Icon = AppIdentity.CreateIcon();
        previous?.Dispose();
    }

    private void ApplyAppearance()
    {
        SuspendLayout();
        try
        {
            KitStyle.Apply(this);
            sidebar.BackColor = navigation.BackColor = edition.BackColor = KitStyle.Palette.Sidebar;
            navigation.ItemHeight = KitStyle.Scale(KitStyle.Appearance.Density == "Compact" ? 31 : 38);
            navigation.ForeColor = KitStyle.Text;
            edition.ForeColor = KitStyle.Muted;
            edition.Font = KitStyle.Font(8, mono: true);
            toolbar.BackColor = KitStyle.Surface;
            SetToolbarSurface(toolbar);
            breadcrumb.Font = KitStyle.Font(9, true);
            breadcrumb.ForeColor = KitStyle.Muted;
            status.BackColor = KitStyle.Surface;
            status.ForeColor = KitStyle.Muted;
            status.Font = KitStyle.Font(8.5F);
            shell.ColumnStyles[0].Width = KitStyle.Scale(252);
            NativeChrome.Apply(this);
            RefreshStatus();
        }
        finally
        {
            ResumeLayout(true);
        }
    }

    private static void SetToolbarSurface(Control parent)
    {
        parent.BackColor = KitStyle.Surface;
        foreach (Control child in parent.Controls)
            SetToolbarSurface(child);
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
            MessageBox.Show(this, "This tool is not available. Check its add-on in Add-ons.", AppIdentity.Name);
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
            if (activePage is OverviewPage)
                SelectPage("overview");
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
            SelectPage("overview");
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
        var memory = KitStyle.Appearance.ShowMemory ? $"    /    {process.WorkingSet64 / 1048576.0:N0} MB RAM" : "";
        status.Text = $"{host.Workspaces.Current?.Name ?? "No project open"}    /    {active} active jobs{memory}    /    {AppIdentity.Purpose}";
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
