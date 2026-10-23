using PortingKit.Core;

namespace PortingKit.Desktop;

internal sealed class JobsPage : UserControl
{
    private readonly KitHost host;
    private readonly DataGridView grid = KitStyle.Grid();
    private readonly TextBox details = new() { Dock = DockStyle.Fill, Multiline = true, ReadOnly = true,
        ScrollBars = ScrollBars.Both, WordWrap = false, Font = new("Consolas", 10), BorderStyle = BorderStyle.None };
    private readonly System.Windows.Forms.Timer timer = new() { Interval = 500 };
    private IReadOnlyList<JobSnapshot> jobs = [];
    private string previousRows = "";

    public JobsPage(KitHost host)
    {
        this.host = host;
        Padding = new(24);
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 4 };
        layout.RowStyles.Add(new(SizeType.Absolute, 52));
        layout.RowStyles.Add(new(SizeType.Percent, 45));
        layout.RowStyles.Add(new(SizeType.Absolute, 48));
        layout.RowStyles.Add(new(SizeType.Percent, 55));
        layout.Controls.Add(KitStyle.Label("Jobs and outputs", 22, true));
        grid.Columns.Add("time", "Created");
        grid.Columns.Add("tool", "Tool");
        grid.Columns.Add("status", "Status");
        grid.Columns.Add("progress", "Progress");
        grid.Columns[1].FillWeight = 160;
        grid.SelectionChanged += (_, _) => RefreshDetails();
        layout.Controls.Add(grid);
        var actions = new FlowLayoutPanel { Dock = DockStyle.Fill, Padding = new(0, 6, 0, 0) };
        actions.Controls.Add(KitStyle.Button("Cancel job", (_, _) =>
        {
            if (Selected() is { } job)
                host.Jobs.Cancel(job.Id);
            RefreshJobs();
        }));
        actions.Controls.Add(KitStyle.Button("Show outputs", (_, _) =>
        {
            if (Selected() is { } job)
                DesktopActions.Reveal(Path.Combine(host.StateDirectory, "jobs", job.Id.ToString("N")));
        }));
        layout.Controls.Add(actions);
        layout.Controls.Add(details);
        Controls.Add(layout);
        timer.Tick += (_, _) => RefreshJobs();
        timer.Start();
        RefreshJobs();
    }

    private JobSnapshot? Selected() => grid.CurrentRow?.Tag is Guid id ? jobs.FirstOrDefault(job => job.Id == id) : null;

    private void RefreshJobs()
    {
        var id = Selected()?.Id;
        jobs = host.Jobs.Snapshot();
        var signature = string.Join('|', jobs.Select(job => $"{job.Id}:{job.Status}:{job.Progress}"));
        if (signature != previousRows)
        {
            previousRows = signature;
            grid.Rows.Clear();
            foreach (var job in jobs)
            {
                var index = grid.Rows.Add(job.Created.ToLocalTime().ToString("MM-dd HH:mm:ss"), job.Name, job.Status,
                    job.Progress.Fraction is { } fraction ? $"{fraction:P0}" : job.Progress.Message);
                grid.Rows[index].Tag = job.Id;
                if (job.Id == id)
                    grid.CurrentCell = grid.Rows[index].Cells[0];
            }
            if (grid.CurrentCell is null && grid.Rows.Count > 0)
                grid.CurrentCell = grid.Rows[0].Cells[0];
        }
        RefreshDetails();
    }

    private void RefreshDetails()
    {
        var job = Selected();
        if (job is null)
        {
            details.Text = "Run a tool to see its progress, logs, and saved outputs here.";
            return;
        }
        var lines = new List<string>
        {
            $"{job.Name}  /  {job.Status}", job.Progress.Message, $"Job: {job.Id}", ""
        };
        if (job.Error is not null)
            lines.Add("ERROR: " + job.Error);
        if (job.Result is { } result)
        {
            lines.Add(result.Summary);
            foreach (var artifact in (result.Artifacts ?? []).Take(200))
                lines.Add($"{artifact.Label}: {artifact.Path}");
        }
        lines.Add("");
        lines.AddRange(host.Log.Tail(200).Where(entry => entry.Source == job.Id.ToString("N")[..8] || entry.Source == job.ToolId)
            .Select(entry => $"{entry.Time:HH:mm:ss} [{entry.Level}] {entry.Message}"));
        var text = string.Join(Environment.NewLine, lines);
        if (details.Text != text)
        {
            var selection = details.SelectionStart;
            details.Text = text;
            details.SelectionStart = Math.Min(selection, text.Length);
        }
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
            timer.Dispose();
        base.Dispose(disposing);
    }
}
