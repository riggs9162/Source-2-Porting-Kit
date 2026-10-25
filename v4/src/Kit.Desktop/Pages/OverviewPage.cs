using PortingKit.Core;

namespace PortingKit.Desktop;

internal sealed class OverviewPage : UserControl
{
    private readonly Label title;
    private readonly Action updateLayout;

    public OverviewPage(KitHost host, Action<string> navigate, Func<bool, Task> pickWorkspace, Func<string, Task> openWorkspace)
    {
        Padding = new(28, 24, 28, 24);
        AutoScroll = true;
        var layout = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 1, RowCount = 4 };
        layout.RowStyles.Add(new(SizeType.Absolute, 92));
        layout.RowStyles.Add(new(SizeType.Absolute, 338));
        layout.RowStyles.Add(new(SizeType.Absolute, 44));
        layout.RowStyles.Add(new(SizeType.Absolute, 220));
        var heading = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.TopDown, WrapContents = false };
        title = KitStyle.Label(KitStyle.Appearance.WorkspaceName, 24, true);
        heading.Controls.Add(title);
        heading.Controls.Add(KitStyle.Label("Edit material trim sheets and hotspot rectangle maps."));
        layout.Controls.Add(heading);
        var work = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1, Margin = Padding.Empty };
        work.ColumnStyles.Add(new(SizeType.Percent, 63));
        work.ColumnStyles.Add(new(SizeType.Percent, 37));
        var actions = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 2, Margin = new(0, 0, 20, 0) };
        actions.RowStyles.Add(new(SizeType.Absolute, 40));
        actions.RowStyles.Add(new(SizeType.Absolute, 104));
        actions.Controls.Add(SectionLabel("MATERIALS"));
        var pages = host.Contributions.GetAll<IDesktopPage>();
        var tools = host.Contributions.GetAll<ITool>();
        void Action(string id, string text, string detail, string number)
        {
            if (!pages.Any(page => page.Id == id) && !tools.Any(tool => tool.Id == id))
                return;
            var button = new WorkspaceAction { Text = text, Detail = detail, Number = number, Dock = DockStyle.Fill, Margin = new(0, 0, 0, 4) };
            button.Click += (_, _) => navigate(id);
            actions.Controls.Add(button);
        }
        Action("hotspot/editor", "Hotspot editor", "Edit trim sheets and rectangle maps", "01");
        work.Controls.Add(actions);
        var projects = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.TopDown,
            WrapContents = false, Padding = new(18, 0, 0, 0) };
        projects.Controls.Add(SectionLabel("PROJECT WORKSPACE"));
        projects.Controls.Add(KitStyle.Label(host.Workspaces.Current?.Name ?? "A place for your next port", 13, true));
        projects.Controls.Add(KitStyle.Label(host.Workspaces.Current is null
            ? "Keep your sources and outputs together.\nCreate a workspace or reopen a recent project."
            : host.Workspaces.Current.RootDirectory, 9));
        var buttons = new FlowLayoutPanel { AutoSize = true, WrapContents = true, MaximumSize = new(330, 0), Margin = new(0, 8, 0, 18) };
        buttons.Controls.Add(KitStyle.PrimaryButton("New project", async (_, _) => await pickWorkspace(true)));
        buttons.Controls.Add(KitStyle.Button("Open...", async (_, _) => await pickWorkspace(false)));
        projects.Controls.Add(buttons);
        if (host.Workspaces.Recent.Count > 0)
        {
            projects.Controls.Add(SectionLabel("RECENT PROJECTS"));
            foreach (var path in host.Workspaces.Recent.Take(3))
                projects.Controls.Add(KitStyle.Button(Path.GetFileName(path), async (_, _) => await openWorkspace(path)));
        }
        else
            projects.Controls.Add(KitStyle.Label("Recent projects will appear here.", 9));
        work.Controls.Add(projects);
        layout.Controls.Add(work);
        var activity = new FlowLayoutPanel { Dock = DockStyle.Fill, WrapContents = false };
        activity.Controls.Add(SectionLabel("RECENT ACTIVITY"));
        activity.Controls.Add(new StyledLabel(9) { Text = $"{host.Addons.Snapshot().Count(addon => addon.State == AddonState.Loaded)} add-ons ready",
            AutoSize = true, Tone = LabelTone.Muted, Margin = new(14, 8, 0, 0) });
        layout.Controls.Add(activity);
        var recent = host.Jobs.Snapshot().Take(8).ToArray();
        if (recent.Length == 0)
        {
            var empty = new Panel { Dock = DockStyle.Fill, Padding = new(20, 24, 20, 0) };
            empty.Controls.Add(KitStyle.Label("A fresh workbench. Your completed jobs and exported files will appear here.", 10));
            layout.Controls.Add(empty);
        }
        else
        {
            var jobs = KitStyle.Grid();
            jobs.Columns.Add("tool", "Tool");
            jobs.Columns.Add("result", "Result");
            jobs.Columns.Add("time", "When");
            jobs.Columns[1].FillWeight = 170;
            foreach (var job in recent)
                jobs.Rows.Add(job.Name, job.Result?.Summary ?? job.Status.ToString(), job.Created.ToLocalTime().ToString("MMM d, HH:mm"));
            jobs.CellDoubleClick += (_, e) => { if (e.RowIndex >= 0) navigate("jobs"); };
            layout.Controls.Add(jobs);
        }
        Controls.Add(layout);
        updateLayout = () =>
        {
            var width = Math.Max(300, ClientSize.Width - Padding.Horizontal - SystemInformation.VerticalScrollBarWidth);
            var narrow = width < 820;
            work.SuspendLayout();
            work.ColumnStyles.Clear();
            work.RowStyles.Clear();
            if (narrow)
            {
                work.SetCellPosition(projects, new(0, 1));
                work.ColumnCount = 1;
                work.RowCount = 2;
                work.ColumnStyles.Add(new(SizeType.Percent, 100));
                work.RowStyles.Add(new(SizeType.Absolute, KitStyle.Scale(300)));
                work.RowStyles.Add(new(SizeType.Percent, 100));
            }
            else
            {
                work.ColumnCount = 2;
                work.RowCount = 1;
                work.SetCellPosition(projects, new(1, 0));
                work.ColumnStyles.Add(new(SizeType.Percent, 63));
                work.ColumnStyles.Add(new(SizeType.Percent, 37));
                work.RowStyles.Add(new(SizeType.Percent, 100));
            }
            layout.RowStyles[0].Height = KitStyle.Scale(92);
            layout.RowStyles[1].Height = KitStyle.Scale(narrow ? 620 : 338);
            layout.RowStyles[2].Height = KitStyle.Scale(44);
            layout.RowStyles[3].Height = KitStyle.Scale(220);
            var projectWidth = Math.Max(180, (int)(width * (narrow ? 1 : 0.37)) - 40);
            foreach (Control child in projects.Controls)
                child.MaximumSize = new(projectWidth, 0);
            heading.MaximumSize = new(width, 0);
            foreach (Control child in heading.Controls)
                child.MaximumSize = new(width, 0);
            work.ResumeLayout(true);
        };
        ClientSizeChanged += (_, _) => updateLayout();
        KitStyle.AppearanceChanged += AppearanceChanged;
        updateLayout();
    }

    private void AppearanceChanged(object? sender, EventArgs e)
    {
        title.Text = KitStyle.Appearance.WorkspaceName;
        updateLayout();
    }

    private static Label SectionLabel(string text) => new StyledLabel(8, true)
    {
        Text = text, AutoSize = true, Tone = LabelTone.Muted, Margin = new(0, 8, 0, 14)
    };

    protected override void Dispose(bool disposing)
    {
        if (disposing)
            KitStyle.AppearanceChanged -= AppearanceChanged;
        base.Dispose(disposing);
    }
}
