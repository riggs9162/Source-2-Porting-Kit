using System.Drawing.Drawing2D;
using System.ComponentModel;

namespace PortingKit.Desktop;

public enum LabelTone { Normal, Muted, Accent }

public sealed class StyledLabel(float pointSize = 10, bool bold = false) : Label
{
    public float PointSize { get; } = pointSize;
    public bool Bold { get; } = bold;
    [DefaultValue(false)]
    public bool Monospace { get; init; }
    [DefaultValue(LabelTone.Normal)]
    public LabelTone Tone { get; init; }
}

/// <summary>Paints themed buttons while retaining native button accessibility and keyboard handling.</summary>
public class StyledButton : Button
{
    private bool hovered;
    private bool pressed;
    [DefaultValue(false)]
    public bool Primary { get; set; }

    public StyledButton()
    {
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true);
        FlatStyle = FlatStyle.Flat;
        Cursor = Cursors.Hand;
    }

    protected override void OnMouseEnter(EventArgs e) { hovered = true; Invalidate(); base.OnMouseEnter(e); }
    protected override void OnMouseLeave(EventArgs e) { hovered = false; pressed = false; Invalidate(); base.OnMouseLeave(e); }
    protected override void OnMouseDown(MouseEventArgs e) { pressed = true; Invalidate(); base.OnMouseDown(e); }
    protected override void OnMouseUp(MouseEventArgs e) { pressed = false; Invalidate(); base.OnMouseUp(e); }
    protected override void OnGotFocus(EventArgs e) { Invalidate(); base.OnGotFocus(e); }
    protected override void OnLostFocus(EventArgs e) { Invalidate(); base.OnLostFocus(e); }

    protected override void OnPaint(PaintEventArgs e)
    {
        var palette = KitStyle.Palette;
        var background = Primary ? palette.Accent : pressed ? palette.Selection : hovered ? palette.Raised : palette.Surface;
        if (Primary && hovered)
            background = ThemePalette.Blend(background, palette.Text, 0.12);
        if (!Enabled)
            background = palette.Surface;
        e.Graphics.Clear(background);
        using var border = new Pen(Primary && Enabled ? palette.Accent : palette.Border);
        e.Graphics.DrawRectangle(border, 0, 0, Width - 1, Height - 1);
        var foreground = !Enabled ? palette.Muted : Primary ? palette.OnAccent : palette.Text;
        TextRenderer.DrawText(e.Graphics, Text, KitStyle.Font(9.5F, Primary), ClientRectangle, foreground,
            TextFormatFlags.HorizontalCenter | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis);
        if (Focused && ShowFocusCues)
            ControlPaint.DrawFocusRectangle(e.Graphics, Rectangle.Inflate(ClientRectangle, -4, -4), foreground, background);
    }

    public override Size GetPreferredSize(Size proposedSize)
    {
        var text = TextRenderer.MeasureText(Text, KitStyle.Font(9.5F, Primary));
        return new(Math.Max(90, text.Width + 26), KitStyle.ControlHeight);
    }
}

/// <summary>Shows a task with its description in a flat, keyboard-accessible workspace list.</summary>
public sealed class WorkspaceAction : Button
{
    private bool hovered;
    [DesignerSerializationVisibility(DesignerSerializationVisibility.Hidden)]
    public required string Detail { get; init; }
    [DefaultValue("")]
    public string Number { get; init; } = "";

    public WorkspaceAction()
    {
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true);
        FlatStyle = FlatStyle.Flat;
        Height = 80;
        Dock = DockStyle.Top;
        Cursor = Cursors.Hand;
        TextAlign = ContentAlignment.MiddleLeft;
    }

    protected override void OnMouseEnter(EventArgs e) { hovered = true; Invalidate(); base.OnMouseEnter(e); }
    protected override void OnMouseLeave(EventArgs e) { hovered = false; Invalidate(); base.OnMouseLeave(e); }
    protected override void OnGotFocus(EventArgs e) { Invalidate(); base.OnGotFocus(e); }
    protected override void OnLostFocus(EventArgs e) { Invalidate(); base.OnLostFocus(e); }

    protected override void OnPaint(PaintEventArgs e)
    {
        var p = KitStyle.Palette;
        e.Graphics.Clear(hovered || Focused ? p.Raised : p.Surface);
        using var line = new Pen(p.Border);
        e.Graphics.DrawLine(line, 0, Height - 1, Width, Height - 1);
        TextRenderer.DrawText(e.Graphics, Number, KitStyle.Font(9, mono: true), new Rectangle(16, 18, 35, 24), p.Accent);
        TextRenderer.DrawText(e.Graphics, Text, KitStyle.Font(11, true), new Rectangle(62, 15, Math.Max(0, Width - 100), 26), p.Text,
            TextFormatFlags.EndEllipsis | TextFormatFlags.SingleLine);
        TextRenderer.DrawText(e.Graphics, Detail, KitStyle.Font(9), new Rectangle(62, 43, Math.Max(0, Width - 90), 28), p.Muted,
            TextFormatFlags.EndEllipsis | TextFormatFlags.SingleLine);
        TextRenderer.DrawText(e.Graphics, "›", KitStyle.Font(18), new Rectangle(Width - 30, 22, 24, 32), p.Accent);
        if (Focused && ShowFocusCues)
            ControlPaint.DrawFocusRectangle(e.Graphics, Rectangle.Inflate(ClientRectangle, -4, -4), p.Text, p.Surface);
    }
}

public sealed class ThemePreviewButton(string theme) : Button
{
    public string Theme { get; } = theme;
    [DefaultValue(false)]
    public bool Selected { get; set; }

    public ThemePreviewButton() : this("Graphite") { }

    protected override void OnPaint(PaintEventArgs e)
    {
        var p = ThemePalette.Create(new(Theme: Theme));
        e.Graphics.Clear(KitStyle.Palette.Surface);
        var preview = new Rectangle(8, 8, Width - 16, Height - 42);
        using var background = new SolidBrush(p.Background);
        using var sidebar = new SolidBrush(p.Sidebar);
        using var accent = new SolidBrush(p.Accent);
        using var lines = new Pen(p.Border, 3);
        e.Graphics.FillRectangle(background, preview);
        e.Graphics.FillRectangle(sidebar, preview.X, preview.Y, preview.Width / 4, preview.Height);
        e.Graphics.FillRectangle(accent, preview.X + 8, preview.Y + 12, preview.Width / 4 - 16, 4);
        var left = preview.X + preview.Width / 4 + 12;
        for (var index = 0; index < 3; index++)
            e.Graphics.DrawLine(lines, left, preview.Y + 18 + index * 14, preview.Right - 12, preview.Y + 18 + index * 14);
        using var border = new Pen(Selected ? KitStyle.Accent : KitStyle.Border, Selected ? 2 : 1);
        e.Graphics.DrawRectangle(border, 1, 1, Width - 3, Height - 3);
        TextRenderer.DrawText(e.Graphics, Theme, KitStyle.Font(10, Selected), new Rectangle(10, Height - 31, Width - 20, 26), KitStyle.Text,
            TextFormatFlags.Left | TextFormatFlags.VerticalCenter);
        if (Focused && ShowFocusCues)
            ControlPaint.DrawFocusRectangle(e.Graphics, Rectangle.Inflate(ClientRectangle, -5, -5));
    }
}
