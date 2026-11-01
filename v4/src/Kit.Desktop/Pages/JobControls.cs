using System.ComponentModel;
using System.Drawing.Drawing2D;

namespace PortingKit.Desktop;

/// <summary>Formats job facts the same way across the list and the detail pane.</summary>
internal static class JobText
{
    /// <summary>Single-line row text. Rows draw into a translated buffer, which text ignores unless told otherwise.</summary>
    public const TextFormatFlags RowFlags = TextFormatFlags.Left | TextFormatFlags.SingleLine | TextFormatFlags.NoPrefix |
        TextFormatFlags.NoPadding | TextFormatFlags.PreserveGraphicsTranslateTransform;

    public static Color Tone(JobStatus status) => status switch
    {
        JobStatus.Succeeded => KitStyle.Palette.Success,
        JobStatus.Failed or JobStatus.Interrupted => KitStyle.Palette.Error,
        JobStatus.Running or JobStatus.Cancelling => KitStyle.Palette.Accent,
        _ => KitStyle.Palette.Muted
    };

    public static string When(DateTimeOffset time)
    {
        var local = time.ToLocalTime();
        return local.Date == DateTime.Today ? local.ToString("HH:mm") : local.ToString("MMM d, HH:mm");
    }

    public static string Duration(JobSnapshot job)
    {
        if (job.Started is not { } started)
            return "";
        var span = (job.Finished ?? DateTimeOffset.Now) - started;
        if (span.TotalHours >= 1)
            return $"{(int)span.TotalHours}h {span.Minutes:00}m";
        return span.TotalMinutes >= 1 ? $"{(int)span.TotalMinutes}m {span.Seconds:00}s" : $"{Math.Max(0, (int)span.TotalSeconds)}s";
    }
}

/// <summary>A flat owner-drawn list that updates rows in place, so polling keeps the selection and scroll position.</summary>
internal sealed class RowList<T> : ListBox where T : class
{
    private readonly Func<T, object> key;
    private readonly Func<T, string> describe;
    private bool rebuilding;

    public RowList(Func<T, object> key, Func<T, string> describe)
    {
        this.key = key;
        this.describe = describe;
        DrawMode = DrawMode.OwnerDrawFixed;
        BorderStyle = BorderStyle.None;
        IntegralHeight = false;
        Dock = DockStyle.Fill;
        Margin = Padding.Empty;
        KitStyle.OnSurface(this);
    }

    /// <summary>Draws a row into a buffer; the rectangle stays aligned with any horizontal scroll.</summary>
    [DesignerSerializationVisibility(DesignerSerializationVisibility.Hidden)]
    public Action<Graphics, Rectangle, T, bool>? Painter { get; init; }
    /// <summary>Stays on the newest row while the reader is already at the end.</summary>
    [DesignerSerializationVisibility(DesignerSerializationVisibility.Hidden)]
    public bool FollowEnd { get; init; }
    public event Action<T>? Activated;
    public T? Current => SelectedItem is Row row ? row.Value : null;

    public void SetRows(IReadOnlyList<T> values, bool toEnd = false)
    {
        var same = Items.Count == values.Count;
        for (var index = 0; same && index < values.Count; index++)
            same = Equals(key(((Row)Items[index]).Value), key(values[index]));
        if (same)
        {
            for (var index = 0; index < values.Count; index++)
            {
                var row = (Row)Items[index];
                if (ReferenceEquals(row.Value, values[index]) || row.Value.Equals(values[index]))
                    continue;
                row.Value = values[index];
                Invalidate(GetItemRectangle(index));
            }
            if (toEnd && Items.Count > 0)
                TopIndex = Items.Count - 1;
            return;
        }
        var chosen = Current is { } current ? key(current) : null;
        var top = TopIndex;
        var follow = toEnd || FollowEnd && TopIndex + ClientSize.Height / Math.Max(1, ItemHeight) >= Items.Count;
        rebuilding = true;
        BeginUpdate();
        try
        {
            Items.Clear();
            foreach (var value in values)
            {
                Items.Add(new Row(value, describe));
                if (Equals(key(value), chosen))
                    SelectedIndex = Items.Count - 1;
            }
        }
        finally
        {
            EndUpdate();
            rebuilding = false;
        }
        if (Items.Count > 0)
            TopIndex = follow ? Items.Count - 1 : Math.Min(top, Items.Count - 1);
        OnSelectedIndexChanged(EventArgs.Empty);
    }

    public void Repaint(Func<T, bool> changed)
    {
        var last = Math.Min(Items.Count - 1, TopIndex + ClientSize.Height / Math.Max(1, ItemHeight));
        for (var index = TopIndex; index <= last; index++)
            if (changed(((Row)Items[index]).Value))
                Invalidate(GetItemRectangle(index));
    }

    protected override void OnSelectedIndexChanged(EventArgs e)
    {
        if (!rebuilding)
            base.OnSelectedIndexChanged(e);
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        NativeChrome.ThemeScrollbars(this);
    }

    protected override void OnDoubleClick(EventArgs e)
    {
        base.OnDoubleClick(e);
        if (Current is { } value)
            Activated?.Invoke(value);
    }

    protected override void OnKeyDown(KeyEventArgs e)
    {
        base.OnKeyDown(e);
        if (e.KeyCode == Keys.Enter && Current is { } value)
            Activated?.Invoke(value);
    }

    protected override void OnDrawItem(DrawItemEventArgs e)
    {
        if (Painter is null || e.Index < 0 || e.Index >= Items.Count)
            return;
        var target = new Rectangle(0, e.Bounds.Y, ClientSize.Width, e.Bounds.Height);
        var row = new Rectangle(e.Bounds.X, e.Bounds.Y, Math.Max(e.Bounds.Width, ClientSize.Width - e.Bounds.X), e.Bounds.Height);
        using var buffer = BufferedGraphicsManager.Current.Allocate(e.Graphics, target);
        Painter(buffer.Graphics, row, ((Row)Items[e.Index]).Value, (e.State & DrawItemState.Selected) != 0);
        if ((e.State & DrawItemState.Focus) != 0 && (e.State & DrawItemState.NoFocusRect) == 0)
            ControlPaint.DrawFocusRectangle(buffer.Graphics, Rectangle.Inflate(target, -2, -2), KitStyle.Text, KitStyle.Surface);
        buffer.Render(e.Graphics);
    }

    /// <summary>Holds a replaceable value so screen readers keep a stable item with current text.</summary>
    private sealed class Row(T value, Func<T, string> describe)
    {
        public T Value { get; set; } = value;
        public override string ToString() => describe(Value);
    }
}

/// <summary>A flat progress line. Work with no known fraction shows a still hatch rather than an animation.</summary>
internal sealed class ProgressLine : Control
{
    private double? fraction;

    public ProgressLine()
    {
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true);
        SetStyle(ControlStyles.Selectable, false);
        AccessibleRole = AccessibleRole.ProgressBar;
        AccessibleName = "Job progress";
    }

    [DesignerSerializationVisibility(DesignerSerializationVisibility.Hidden)]
    public double? Fraction
    {
        get => fraction;
        set
        {
            if (fraction == value)
                return;
            fraction = value;
            AccessibleDescription = value is { } known ? $"{known:P0}" : "Working";
            Invalidate();
        }
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        var p = KitStyle.Palette;
        e.Graphics.Clear(p.Raised);
        if (fraction is { } known)
        {
            using var fill = new SolidBrush(p.Accent);
            e.Graphics.FillRectangle(fill, 0, 0, (int)Math.Round(Width * known), Height);
        }
        else
        {
            using var hatch = new HatchBrush(HatchStyle.WideUpwardDiagonal, ThemePalette.Blend(p.Raised, p.Accent, 0.55), p.Raised);
            e.Graphics.FillRectangle(hatch, ClientRectangle);
        }
    }
}

/// <summary>Names a job's status in text and color, so color never carries the meaning alone.</summary>
internal sealed class StatusBadge : Control
{
    private JobStatus status;

    public StatusBadge()
    {
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true);
        SetStyle(ControlStyles.Selectable, false);
        AccessibleRole = AccessibleRole.StaticText;
        Margin = new(12, 4, 0, 0);
    }

    [DesignerSerializationVisibility(DesignerSerializationVisibility.Hidden)]
    public JobStatus Status
    {
        get => status;
        set
        {
            status = value;
            Text = value.ToString().ToUpperInvariant();
            AccessibleName = value.ToString();
            Fit();
            Invalidate();
        }
    }

    public void Fit()
    {
        var text = TextRenderer.MeasureText(Text, KitStyle.Font(8, true), Size.Empty, TextFormatFlags.NoPadding);
        Size = new(text.Width + KitStyle.Scale(20), text.Height + KitStyle.Scale(10));
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        var tone = JobText.Tone(status);
        e.Graphics.Clear(ThemePalette.Blend(KitStyle.Surface, tone, 0.14));
        using var border = new Pen(ThemePalette.Blend(KitStyle.Surface, tone, 0.55));
        e.Graphics.DrawRectangle(border, 0, 0, Width - 1, Height - 1);
        TextRenderer.DrawText(e.Graphics, Text, KitStyle.Font(8, true), ClientRectangle, tone,
            TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.NoPrefix);
    }
}
