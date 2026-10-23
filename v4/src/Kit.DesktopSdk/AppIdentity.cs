using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;

namespace PortingKit.Desktop;

/// <summary>Defines the Source Backport identity and its scalable, code-drawn direction mark.</summary>
public static class AppIdentity
{
    private static readonly Font MarkFont = new("Segoe UI", 14, FontStyle.Bold, GraphicsUnit.Pixel);
    public const string Name = "Source Backport";
    public const string Purpose = "Source 2 → Source 1";
    public const string Description = "Source 2 to Source 1 asset porting";

    public static void DrawMark(Graphics graphics, Rectangle bounds, ThemePalette palette)
    {
        var state = graphics.Save();
        graphics.TranslateTransform(bounds.X, bounds.Y);
        graphics.ScaleTransform(bounds.Width / 48F, bounds.Height / 48F);
        graphics.SmoothingMode = SmoothingMode.AntiAlias;
        using var accent = new SolidBrush(palette.Accent);
        using var foreground = new SolidBrush(palette.OnAccent);
        using var pen = new Pen(palette.Accent, 2);
        using var format = new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center };
        graphics.DrawRectangle(pen, 24, 1, 23, 23);
        graphics.FillRectangle(accent, 1, 25, 23, 22);
        graphics.DrawString("2", MarkFont, accent, new RectangleF(24, 1, 23, 23), format);
        graphics.DrawString("1", MarkFont, foreground, new RectangleF(1, 25, 23, 22), format);
        graphics.DrawLines(pen, [new(21, 12), new(11, 12), new(11, 20)]);
        graphics.DrawLines(pen, [new(7, 16), new(11, 20), new(15, 16)]);
        graphics.Restore(state);
    }

    public static Icon CreateIcon()
    {
        using var bitmap = new Bitmap(64, 64);
        using (var graphics = Graphics.FromImage(bitmap))
        {
            graphics.Clear(Color.Transparent);
            DrawMark(graphics, new(4, 4, 56, 56), KitStyle.Palette);
        }
        var handle = bitmap.GetHicon();
        try
        {
            using var borrowed = Icon.FromHandle(handle);
            return (Icon)borrowed.Clone();
        }
        finally
        {
            DestroyIcon(handle);
        }
    }

    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool DestroyIcon(nint icon);
}

public sealed class BrandPanel : Control
{
    public BrandPanel()
    {
        Height = 126;
        Dock = DockStyle.Top;
        AccessibleName = AppIdentity.Name + ". " + AppIdentity.Description;
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true);
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        var p = KitStyle.Palette;
        e.Graphics.Clear(p.Sidebar);
        AppIdentity.DrawMark(e.Graphics, new(20, 27, 43, 43), p);
        TextRenderer.DrawText(e.Graphics, "SOURCE", KitStyle.Font(10, true), new Rectangle(78, 27, Width - 86, 25), p.Muted);
        TextRenderer.DrawText(e.Graphics, "BACKPORT", KitStyle.Font(16, true), new Rectangle(76, 48, Width - 84, 36), p.Text);
        TextRenderer.DrawText(e.Graphics, AppIdentity.Purpose, KitStyle.Font(8.5F, mono: true), new Rectangle(20, 97, Width - 28, 24), p.Accent);
        using var line = new Pen(p.Border);
        e.Graphics.DrawLine(line, 20, Height - 1, Width - 20, Height - 1);
    }
}

/// <summary>Draws native tree rows with category labels, restrained selection, and clear keyboard focus.</summary>
public sealed class NavigationTree : TreeView
{
    public NavigationTree()
    {
        Dock = DockStyle.Fill;
        BorderStyle = BorderStyle.None;
        ShowLines = false;
        ShowPlusMinus = false;
        ShowRootLines = false;
        HideSelection = false;
        FullRowSelect = true;
        DrawMode = TreeViewDrawMode.OwnerDrawAll;
        Indent = 14;
        ItemHeight = 37;
    }

    protected override void OnDrawNode(DrawTreeNodeEventArgs e)
    {
        if (e.Node is null)
            return;
        var p = KitStyle.Palette;
        var isGroup = e.Node.Tag is null;
        var selected = e.Node == SelectedNode && !isGroup;
        var row = new Rectangle(0, e.Bounds.Y, ClientSize.Width, ItemHeight);
        using var fill = new SolidBrush(selected ? p.Selection : p.Sidebar);
        e.Graphics.FillRectangle(fill, row);
        if (selected)
        {
            using var marker = new SolidBrush(p.Accent);
            e.Graphics.FillRectangle(marker, 0, row.Y + 5, 3, Math.Max(0, row.Height - 10));
        }
        var left = isGroup ? 20 : e.Node.Parent is null ? 22 : 30;
        var text = new Rectangle(left, row.Y, Math.Max(0, row.Width - left - 12), row.Height);
        TextRenderer.DrawText(e.Graphics, e.Node.Text, KitStyle.Font(isGroup ? 8 : 9.5F, isGroup || selected),
            text, isGroup ? p.Muted : selected ? p.Accent : p.Text,
            TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis | TextFormatFlags.SingleLine);
        if (selected && Focused && (e.State & TreeNodeStates.Focused) != 0)
            ControlPaint.DrawFocusRectangle(e.Graphics, Rectangle.Inflate(row, -6, -4), p.Accent, p.Selection);
    }
}
