using PortingKit.Core;

namespace PortingKit.Desktop;

/// <summary>Pairs a filterable job list with the progress, finished files, and log of the selected job.</summary>
internal sealed class JobsPage : UserControl
{
    private static readonly (string Name, Func<JobStatus, bool> Match)[] Filters =
    [
        ("All", _ => true),
        ("Active", JobQueue.IsActive),
        ("Succeeded", status => status == JobStatus.Succeeded),
        ("Failed", status => status is JobStatus.Failed or JobStatus.Interrupted)
    ];

    private readonly KitHost host;
    private readonly IReadOnlyDictionary<string, ToolDefinition> tools;
    private readonly RowList<JobSnapshot> list;
    private readonly RowList<Artifact> outputs;
    private readonly RowList<LogLine> log;
    private readonly List<StyledButton> filterButtons = [];
    private readonly TableLayoutPanel detail = KitStyle.OnSurface(new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 10,
        Margin = Padding.Empty, Padding = new(20, 18, 20, 14) });
    private readonly Label heading = new StyledLabel(15, true) { AutoSize = true, Margin = new(0, 0, 0, 2), UseMnemonic = false };
    private readonly StatusBadge badge = new();
    private readonly Label subject = new StyledLabel(9) { Monospace = true, Tone = LabelTone.Muted, AutoEllipsis = true, Dock = DockStyle.Fill,
        Height = 20, Margin = new(0, 0, 0, 10), UseMnemonic = false };
    private readonly ProgressLine progress = new() { Dock = DockStyle.Fill, Margin = new(3, 0, 0, 8) };
    private readonly TextBox message = new() { Dock = DockStyle.Fill, Multiline = true, ReadOnly = true, BorderStyle = BorderStyle.None,
        TabStop = false, Margin = new(0, 0, 0, 12) };
    private readonly Label created = Value();
    private readonly Label finished = Value();
    private readonly Label elapsed = Value();
    private readonly Label jobId = Value();
    private readonly Button open;
    private readonly Button copyLog;
    private readonly Button cancel;
    private readonly Label outputsCaption = Caption("OUTPUTS");
    private readonly Label outputsHint = new StyledLabel(8) { Text = "Double-click to show in Explorer", AutoSize = true, Tone = LabelTone.Muted, Margin = new(10, 0, 0, 0) };
    private readonly FlowLayoutPanel outputsHeader = KitStyle.OnSurface(new FlowLayoutPanel { AutoSize = true, WrapContents = false, Margin = new(0, 0, 0, 6) });
    private readonly Label empty = new StyledLabel(10) { Dock = DockStyle.Fill, Tone = LabelTone.Muted, TextAlign = ContentAlignment.MiddleCenter, Padding = new(32), Visible = false };
    private readonly System.Windows.Forms.Timer timer = new() { Interval = 500 };
    private int filter;
    private Guid? shownJob;

    public JobsPage(KitHost host)
    {
        this.host = host;
        tools = host.Contributions.GetAll<ITool>().ToDictionary(tool => tool.Id, tool => tool.Value.Definition);
        Padding = new(24);
        list = new(job => job.Id, Describe) { Painter = PaintJob };
        outputs = new(artifact => artifact, artifact => $"{artifact.Label}: {artifact.Path}") { Painter = PaintArtifact };
        log = new(line => line, line => line.ToString()) { Painter = PaintLog, FollowEnd = true, HorizontalScrollbar = true, IntegralHeight = true };
        list.AccessibleName = "Jobs";
        outputs.AccessibleName = "Outputs";
        log.AccessibleName = "Job log";
        list.SelectedIndexChanged += (_, _) => RefreshDetails();
        outputs.Activated += artifact => ShowPath(artifact.Path);
        open = KitStyle.PrimaryButton("Open outputs", (_, _) => { if (Selected() is { } job) ShowPath(OutputFolder(job)); });
        copyLog = KitStyle.Button("Copy log", (_, _) => CopyLog());
        cancel = KitStyle.Button("Cancel job", (_, _) =>
        {
            if (Selected() is { } job)
                host.Jobs.Cancel(job.Id);
            RefreshJobs();
        });
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 2, Margin = Padding.Empty };
        layout.ColumnStyles.Add(new(SizeType.Percent, 100));
        layout.RowStyles.Add(new(SizeType.AutoSize));
        layout.RowStyles.Add(new(SizeType.Percent, 100));
        layout.Controls.Add(BuildHeader());
        layout.Controls.Add(BuildBody());
        Controls.Add(layout);
        KitStyle.AppearanceChanged += AppearanceChanged;
        ClientSizeChanged += (_, _) => RefreshDetails();
        timer.Tick += (_, _) => RefreshJobs();
        timer.Start();
        RefreshJobs();
    }

    private static Label Caption(string text) => new StyledLabel(8, true) { Text = text, AutoSize = true, Tone = LabelTone.Muted, Margin = new(0, 0, 0, 4) };

    private static Label Value() => new StyledLabel(9.5F) { AutoSize = true, Monospace = true, Margin = new(0, 0, 18, 14) };

    private Control BuildHeader()
    {
        var header = new TableLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, ColumnCount = 2, RowCount = 1, Margin = new(0, 0, 0, 12) };
        header.ColumnStyles.Add(new(SizeType.Percent, 100));
        header.ColumnStyles.Add(new(SizeType.AutoSize));
        header.Controls.Add(KitStyle.Label("Jobs and outputs", 22, true));
        var strip = new FlowLayoutPanel { AutoSize = true, WrapContents = false, Anchor = AnchorStyles.Right, Margin = Padding.Empty };
        for (var index = 0; index < Filters.Length; index++)
        {
            var chosen = index;
            var button = (StyledButton)KitStyle.Button(Filters[index].Name, (_, _) =>
            {
                filter = chosen;
                RefreshJobs();
            });
            button.Selected = index == 0;
            button.Margin = new(0, 0, 4, 0);
            filterButtons.Add(button);
            strip.Controls.Add(button);
        }
        header.Controls.Add(strip);
        return header;
    }

    private Control BuildBody()
    {
        var body = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1, Margin = Padding.Empty };
        body.ColumnStyles.Add(new(SizeType.Percent, 37));
        body.ColumnStyles.Add(new(SizeType.Percent, 63));
        body.RowStyles.Add(new(SizeType.Percent, 100));
        list.Margin = new(0, 0, 2, 0);
        body.Controls.Add(list, 0, 0);
        var pane = KitStyle.OnSurface(new Panel { Dock = DockStyle.Fill, Margin = Padding.Empty });
        BuildDetail();
        pane.Controls.Add(detail);
        pane.Controls.Add(empty);
        body.Controls.Add(pane, 1, 0);
        return body;
    }

    private void BuildDetail()
    {
        detail.ColumnStyles.Add(new(SizeType.Percent, 100));
        foreach (var style in new[] { SizeType.AutoSize, SizeType.Absolute, SizeType.Absolute, SizeType.Absolute, SizeType.AutoSize,
            SizeType.AutoSize, SizeType.AutoSize, SizeType.Absolute, SizeType.AutoSize, SizeType.Percent })
            detail.RowStyles.Add(new(style, style == SizeType.Percent ? 100 : 0));
        var title = KitStyle.OnSurface(new TableLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, ColumnCount = 2, RowCount = 1, Margin = Padding.Empty });
        title.ColumnStyles.Add(new(SizeType.Percent, 100));
        title.ColumnStyles.Add(new(SizeType.AutoSize));
        title.Controls.Add(heading);
        title.Controls.Add(badge);
        detail.Controls.Add(title, 0, 0);
        detail.Controls.Add(subject, 0, 1);
        detail.Controls.Add(progress, 0, 2);
        detail.Controls.Add(message, 0, 3);
        var facts = KitStyle.OnSurface(new TableLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, ColumnCount = 4, RowCount = 2, Margin = Padding.Empty });
        var entries = new[] { ("CREATED", created), ("FINISHED", finished), ("DURATION", elapsed), ("JOB", jobId) };
        for (var index = 0; index < entries.Length; index++)
        {
            facts.ColumnStyles.Add(index < 2 ? new(SizeType.Percent, 50) : new(SizeType.AutoSize));
            var caption = Caption(entries[index].Item1);
            caption.Margin = new(0, 0, 18, 4);
            facts.Controls.Add(caption, index, 0);
            facts.Controls.Add(entries[index].Item2, index, 1);
        }
        detail.Controls.Add(facts, 0, 4);
        var actions = KitStyle.OnSurface(new FlowLayoutPanel { AutoSize = true, WrapContents = false, Margin = new(3, 0, 0, 16) });
        actions.Controls.Add(open);
        actions.Controls.Add(copyLog);
        actions.Controls.Add(cancel);
        detail.Controls.Add(actions, 0, 5);
        outputsHeader.Controls.Add(outputsCaption);
        outputsHeader.Controls.Add(outputsHint);
        detail.Controls.Add(outputsHeader, 0, 6);
        outputs.Margin = new(3, 0, 0, 14);
        detail.Controls.Add(outputs, 0, 7);
        detail.Controls.Add(Caption("LOG"), 0, 8);
        detail.Controls.Add(log, 0, 9);
    }

    private JobSnapshot? Selected() => list.Current;

    private void RefreshJobs()
    {
        var jobs = host.Jobs.Snapshot();
        for (var index = 0; index < Filters.Length; index++)
        {
            var match = Filters[index].Match;
            filterButtons[index].Text = $"{Filters[index].Name}  {jobs.Count(job => match(job.Status))}";
            filterButtons[index].Selected = index == filter;
        }
        list.SetRows(jobs.Where(job => Filters[filter].Match(job.Status)).ToArray());
        if (list.SelectedIndex < 0 && list.Items.Count > 0)
            list.SelectedIndex = 0;
        list.Repaint(job => JobQueue.IsActive(job.Status));
        RefreshDetails();
    }

    private void RefreshDetails()
    {
        var job = Selected();
        detail.Visible = job is not null;
        empty.Visible = job is null;
        if (job is null)
        {
            shownJob = null;
            empty.Text = host.Jobs.Snapshot().Count == 0
                ? "Run a tool to see its progress, logs, and saved outputs here."
                : $"No {Filters[filter].Name.ToLowerInvariant()} jobs right now.";
            return;
        }
        var changed = job.Id != shownJob;
        shownJob = job.Id;
        var active = JobQueue.IsActive(job.Status);
        heading.Text = job.Name;
        badge.Status = job.Status;
        subject.Text = SubjectPath(job) ?? job.ToolId;
        progress.Fraction = job.Progress.Fraction;
        detail.RowStyles[2].Height = active ? KitStyle.Scale(6) + progress.Margin.Vertical : 0;
        progress.Visible = active;
        ShowMessage(job, active);
        created.Text = JobText.When(job.Created);
        finished.Text = job.Finished is { } time ? JobText.When(time) : "-";
        elapsed.Text = JobText.Duration(job) is { Length: > 0 } duration ? duration : "-";
        jobId.Text = job.Id.ToString("N")[..8];
        cancel.Enabled = active && job.Status != JobStatus.Cancelling;
        open.Enabled = job.Status != JobStatus.Queued;
        var artifacts = (job.Result?.Artifacts ?? []).Take(200).ToArray();
        outputs.SetRows(artifacts);
        outputsCaption.Text = $"OUTPUTS  {artifacts.Length}";
        outputsHeader.Visible = artifacts.Length > 0;
        outputs.Visible = artifacts.Length > 0;
        detail.RowStyles[7].Height = artifacts.Length == 0 ? 0 : Math.Min(artifacts.Length, 4) * outputs.ItemHeight + outputs.Margin.Vertical;
        var lines = LogLines(job);
        log.HorizontalExtent = LogWidth(lines);
        log.SetRows(lines, changed);
        copyLog.Enabled = lines.Any(line => line.Time is not null);
    }

    private void ShowMessage(JobSnapshot job, bool active)
    {
        var text = job.Error ?? job.Result?.Summary ?? job.Progress.Message;
        if (active && job.Progress.Fraction is { } fraction)
            text = $"{fraction:P0}  {text}";
        text = text.ReplaceLineEndings(Environment.NewLine);
        if (message.Text != text)
            message.Text = text;
        var color = job.Error is not null ? KitStyle.Palette.Error : KitStyle.Text;
        if (message.ForeColor != color)
            message.ForeColor = color;
        var width = Math.Max(100, detail.ClientSize.Width - detail.Padding.Horizontal);
        var measured = TextRenderer.MeasureText(text, message.Font, new(width, 0), TextFormatFlags.WordBreak | TextFormatFlags.TextBoxControl).Height;
        var line = message.Font.Height;
        var height = Math.Clamp(measured, line, line * 5);
        var bars = measured > height ? ScrollBars.Vertical : ScrollBars.None;
        if (message.ScrollBars != bars)
        {
            message.ScrollBars = bars;
            NativeChrome.ThemeScrollbars(message);
        }
        detail.RowStyles[3].Height = height + message.Margin.Vertical;
    }

    private string? SubjectPath(JobSnapshot job)
    {
        if (!tools.TryGetValue(job.ToolId, out var tool))
            return null;
        var parameter = tool.Parameters.FirstOrDefault(item => item.Required && item.Kind is ParameterKind.File or ParameterKind.Directory);
        return parameter is not null && job.Arguments.TryGetValue(parameter.Key, out var value) && value.Length > 0 ? value : null;
    }

    private string Subject(JobSnapshot job) => SubjectPath(job) is { } path
        ? Path.GetFileName(path.TrimEnd('/', '\\')) : "";

    private string Describe(JobSnapshot job) => $"{job.Name} {Subject(job)}, {job.Status}, {job.Progress.Message}";

    private string OutputFolder(JobSnapshot job)
    {
        var artifacts = job.Result?.Artifacts ?? [];
        if (artifacts.FirstOrDefault(item => item.Kind == "directory") is { } folder)
            return folder.Path;
        return artifacts.Count > 0 && Path.GetDirectoryName(artifacts[0].Path) is { } parent
            ? parent : Path.Combine(host.StateDirectory, "jobs", job.Id.ToString("N"));
    }

    private void ShowPath(string path)
    {
        if (!DesktopActions.Show(path))
            MessageBox.Show(this, $"This output is no longer there:\n{path}", AppIdentity.Name);
    }

    private LogLine[] LogLines(JobSnapshot job)
    {
        var tag = job.Id.ToString("N")[..8];
        var from = job.Started ?? job.Created;
        var until = (job.Finished ?? DateTimeOffset.Now).AddSeconds(1);
        var lines = new List<LogLine>();
        foreach (var entry in host.Log.Tail(500).Where(entry => entry.Source == tag || entry.Source == job.ToolId && entry.Time >= from && entry.Time <= until))
        {
            var parts = entry.Message.Replace("\t", "    ").ReplaceLineEndings("\n").TrimEnd('\n').Split('\n');
            for (var index = 0; index < parts.Length; index++)
                lines.Add(new(index == 0 ? entry.Time : null, entry.Level, parts[index], index > 0));
        }
        if (lines.Count == 0)
            lines.Add(new(null, LogLevel.Debug, JobQueue.IsActive(job.Status)
                ? "Nothing logged yet." : "Older logs are in the logs folder.", false));
        return lines.ToArray();
    }

    private static int LogWidth(LogLine[] lines)
    {
        var font = KitStyle.Font(9, mono: true);
        var character = TextRenderer.MeasureText("0000000000", font, Size.Empty, TextFormatFlags.NoPadding).Width / 10;
        return Math.Min(32000, LogTextOffset() + lines.Max(line => line.Text.Length) * character + KitStyle.Scale(12));
    }

    private void CopyLog()
    {
        var text = string.Join(Environment.NewLine, log.Items.Cast<object>().Select(item => item.ToString()));
        try
        {
            Clipboard.SetText(text);
        }
        catch (System.Runtime.InteropServices.ExternalException exception)
        {
            KitStyle.ShowError(this, exception);
        }
    }

    private static int LogTextOffset() => KitStyle.Scale(3) +
        TextRenderer.MeasureText("00:00:00  WARN  ", KitStyle.Font(9, mono: true), Size.Empty, TextFormatFlags.NoPadding).Width;

    private void PaintJob(Graphics graphics, Rectangle bounds, JobSnapshot job, bool selected)
    {
        var p = KitStyle.Palette;
        var tone = JobText.Tone(job.Status);
        using (var fill = new SolidBrush(selected ? p.Selection : p.Surface))
            graphics.FillRectangle(fill, bounds);
        using (var stripe = new SolidBrush(tone))
            graphics.FillRectangle(stripe, bounds.X, bounds.Y, KitStyle.Scale(3), bounds.Height - 1);
        using (var line = new Pen(p.Border))
            graphics.DrawLine(line, bounds.X, bounds.Bottom - 1, bounds.Right, bounds.Bottom - 1);
        var flags = JobText.RowFlags | TextFormatFlags.Top | TextFormatFlags.EndEllipsis;
        var left = bounds.X + KitStyle.Scale(16);
        var right = bounds.Right - KitStyle.Scale(12);
        var name = KitStyle.Font(10, true);
        var small = KitStyle.Font(9);
        var clock = KitStyle.Font(8.5F, mono: true);
        var top = bounds.Y + KitStyle.Scale(10);
        var when = JobText.When(job.Created);
        var whenWidth = TextRenderer.MeasureText(graphics, when, clock, Size.Empty, JobText.RowFlags).Width;
        TextRenderer.DrawText(graphics, when, clock, new Rectangle(right - whenWidth, top + 1, whenWidth, clock.Height), p.Muted, flags);
        var room = Math.Max(0, right - whenWidth - KitStyle.Scale(10) - left);
        var nameWidth = Math.Min(room, TextRenderer.MeasureText(graphics, job.Name, name, Size.Empty, JobText.RowFlags).Width);
        TextRenderer.DrawText(graphics, job.Name, name, new Rectangle(left, top, nameWidth, name.Height), p.Text, flags);
        var gap = KitStyle.Scale(8);
        if (room - nameWidth > gap)
            TextRenderer.DrawText(graphics, Subject(job), small, new Rectangle(left + nameWidth + gap, top + 1, room - nameWidth - gap, small.Height), p.Muted, flags);
        var second = top + name.Height + KitStyle.Scale(3);
        var status = job.Status.ToString();
        var statusFont = KitStyle.Font(9, true);
        var statusWidth = TextRenderer.MeasureText(graphics, status, statusFont, Size.Empty, JobText.RowFlags).Width;
        TextRenderer.DrawText(graphics, status, statusFont, new Rectangle(left, second, statusWidth, statusFont.Height), tone, flags);
        var note = JobQueue.IsActive(job.Status) ? job.Progress.Message : JobText.Duration(job);
        TextRenderer.DrawText(graphics, note, small, new Rectangle(left + statusWidth + gap, second, Math.Max(0, right - left - statusWidth - gap), small.Height), p.Muted, flags);
        if (JobQueue.IsActive(job.Status) && job.Progress.Fraction is { } fraction)
        {
            using var track = new SolidBrush(p.Raised);
            using var done = new SolidBrush(p.Accent);
            var height = KitStyle.Scale(2);
            graphics.FillRectangle(track, bounds.X, bounds.Bottom - height - 1, bounds.Width, height);
            graphics.FillRectangle(done, bounds.X, bounds.Bottom - height - 1, (int)(bounds.Width * fraction), height);
        }
    }

    private void PaintArtifact(Graphics graphics, Rectangle bounds, Artifact artifact, bool selected)
    {
        var p = KitStyle.Palette;
        using (var fill = new SolidBrush(selected ? p.Selection : p.Raised))
            graphics.FillRectangle(fill, bounds);
        using (var line = new Pen(p.Surface))
            graphics.DrawLine(line, bounds.X, bounds.Bottom - 1, bounds.Right, bounds.Bottom - 1);
        var flags = JobText.RowFlags | TextFormatFlags.EndEllipsis;
        var tag = KitStyle.Font(8, true, true);
        var left = bounds.X + KitStyle.Scale(10);
        var text = left + KitStyle.Scale(46);
        var width = Math.Max(0, bounds.Right - text - KitStyle.Scale(10));
        var label = KitStyle.Font(9.5F, true);
        var path = KitStyle.Font(8.5F, mono: true);
        var top = bounds.Y + Math.Max(0, (bounds.Height - label.Height - path.Height - KitStyle.Scale(2)) / 2);
        TextRenderer.DrawText(graphics, artifact.Kind == "directory" ? "DIR" : "FILE", tag, new Rectangle(left, top + 2, KitStyle.Scale(40), tag.Height), p.Accent, flags);
        TextRenderer.DrawText(graphics, artifact.Label, label, new Rectangle(text, top, width, label.Height), p.Text, flags);
        TextRenderer.DrawText(graphics, artifact.Path, path, new Rectangle(text, top + label.Height + KitStyle.Scale(2), width, path.Height), p.Muted,
            flags | TextFormatFlags.PathEllipsis);
    }

    private static void PaintLog(Graphics graphics, Rectangle bounds, LogLine line, bool selected)
    {
        var p = KitStyle.Palette;
        using (var fill = new SolidBrush(selected ? p.Selection : p.Surface))
            graphics.FillRectangle(fill, bounds);
        var font = KitStyle.Font(9, mono: true);
        var flags = JobText.RowFlags | TextFormatFlags.VerticalCenter;
        var tone = line.Level switch
        {
            LogLevel.Error => p.Error,
            LogLevel.Warning => p.Accent,
            LogLevel.Success => p.Success,
            LogLevel.Debug => p.Muted,
            _ => p.Text
        };
        var x = bounds.X + KitStyle.Scale(3);
        if (line.Time is { } time)
        {
            TextRenderer.DrawText(graphics, $"{time.ToLocalTime():HH:mm:ss}", font, new Rectangle(x, bounds.Y, bounds.Width, bounds.Height), p.Muted, flags);
            var column = TextRenderer.MeasureText("00:00:00  ", font, Size.Empty, JobText.RowFlags).Width;
            TextRenderer.DrawText(graphics, line.Tag, font, new Rectangle(x + column, bounds.Y, bounds.Width, bounds.Height), tone, flags);
        }
        var offset = line.Time is null && !line.Continued ? x : bounds.X + LogTextOffset();
        TextRenderer.DrawText(graphics, line.Text, font, new Rectangle(offset, bounds.Y, bounds.Right - offset, bounds.Height), line.Time is null && !line.Continued ? p.Muted : tone, flags);
    }

    private void AppearanceChanged(object? sender, EventArgs e) => ApplyMetrics();

    private void ApplyMetrics()
    {
        list.ItemHeight = KitStyle.Scale(KitStyle.Appearance.Density == "Compact" ? 54 : 62);
        outputs.ItemHeight = KitStyle.Scale(44);
        log.ItemHeight = KitStyle.Scale(19);
        detail.RowStyles[1].Height = KitStyle.Scale(20) + subject.Margin.Vertical;
        badge.Fit();
        NativeChrome.ThemeScrollbars(list);
        NativeChrome.ThemeScrollbars(outputs);
        NativeChrome.ThemeScrollbars(log);
        NativeChrome.ThemeScrollbars(message);
        message.ForeColor = Color.Empty;
        RefreshDetails();
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        BeginInvoke(ApplyMetrics);
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            timer.Dispose();
            KitStyle.AppearanceChanged -= AppearanceChanged;
        }
        base.Dispose(disposing);
    }

    /// <summary>One displayed log row; multi-line messages become continued rows without a timestamp.</summary>
    private sealed record LogLine(DateTimeOffset? Time, LogLevel Level, string Text, bool Continued)
    {
        public string Tag => Level switch
        {
            LogLevel.Error => "FAIL",
            LogLevel.Warning => "WARN",
            LogLevel.Success => "DONE",
            LogLevel.Debug => "DBUG",
            _ => "INFO"
        };

        public override string ToString() => Time is { } time ? $"{time.ToLocalTime():HH:mm:ss} [{Level}] {Text}" : Text;
    }
}
