using System.ComponentModel;
using System.Drawing.Drawing2D;
using PortingKit.Desktop;

namespace PortingKit.Addons.Hotspot;

internal sealed class HotspotCanvas : Control
{
    private readonly HotspotSession session;
    private TexturePreview? preview;
    private float zoom = 0.5F;
    private bool fitted = true;
    private PointF origin = new(20, 20);
    private Point down;
    private PointF downTexture;
    private PointF panOrigin;
    private HotspotRect[]? before;
    private HotspotRect[]? working;
    private string drag = "";
    private int handle = -1;
    private HotspotRect? box;
    private static readonly Color[] Colors = [Color.Cyan, Color.Orange, Color.LimeGreen, Color.HotPink, Color.Yellow, Color.DodgerBlue, Color.Violet, Color.SpringGreen];
    public HashSet<int> Selection { get; } = [];
    public event EventHandler? SelectionChanged;
    public event EventHandler? SaveRequested;
    public event Action<string>? PointerMoved;
    [DesignerSerializationVisibility(DesignerSerializationVisibility.Hidden)] public bool DrawMode { get; set; }

    public HotspotCanvas(HotspotSession session)
    {
        this.session = session;
        Dock = DockStyle.Fill;
        TabStop = true;
        AccessibleName = "Hotspot texture canvas";
        AccessibleDescription = "Drag to draw, select and move rectangles. Shift selects multiple. Alt draws over an existing rectangle. Wheel zooms; right drag pans.";
        SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.UserPaint | ControlStyles.Selectable, true);
        session.Changed += SessionChanged;
        KitStyle.AppearanceChanged += AppearanceChanged;
    }

    private void SessionChanged(object? sender, EventArgs args)
    {
        Selection.RemoveWhere(i => i >= session.Draft.Document.Rectangles.Length);
        Invalidate();
    }
    private void AppearanceChanged(object? sender, EventArgs args) => Invalidate();

    public void SetPreview(TexturePreview? value)
    {
        preview?.Dispose();
        preview = value;
        Invalidate();
    }

    public void Fit()
    {
        fitted = true;
        var doc = session.Draft.Document;
        zoom = Math.Clamp(Math.Min(Math.Max(1, Width - 40) / (float)doc.Width, Math.Max(1, Height - 40) / (float)doc.Height), 0.005F, 32);
        origin = new((Width - doc.Width * zoom) / 2, (Height - doc.Height * zoom) / 2);
        Invalidate();
    }

    protected override void OnSizeChanged(EventArgs e)
    {
        base.OnSizeChanged(e);
        if (fitted && session is not null) Fit();
    }

    private PointF ToTexture(Point point) => new((point.X - origin.X) / zoom, (point.Y - origin.Y) / zoom);
    private RectangleF ToScreen(HotspotRect r) => new(origin.X + r.X * zoom, origin.Y + r.Y * zoom, r.Width * zoom, r.Height * zoom);
    private int Snap(float value) => session.Draft.Snap ? (int)Math.Round(value / session.Draft.Grid) * session.Draft.Grid : (int)Math.Round(value);

    protected override void OnPaint(PaintEventArgs e)
    {
        var graphics = e.Graphics;
        graphics.Clear(KitStyle.Palette.Sidebar);
        var doc = session.Draft.Document;
        var area = new RectangleF(origin.X, origin.Y, doc.Width * zoom, doc.Height * zoom);
        var clip = graphics.Save();
        graphics.SetClip(area, CombineMode.Intersect);
        using (var fill = new SolidBrush(KitStyle.Surface)) graphics.FillRectangle(fill, area);
        if (preview is not null)
        {
            graphics.InterpolationMode = zoom >= 1 ? InterpolationMode.NearestNeighbor : InterpolationMode.HighQualityBilinear;
            graphics.PixelOffsetMode = PixelOffsetMode.Half;
            graphics.DrawImage(preview.Bitmap, area);
        }
        DrawGrid(graphics, doc);
        var rects = working ?? doc.Rectangles;
        for (var i = 0; i < rects.Length; i++)
        {
            var rectangle = ToScreen(rects[i]);
            if (!rectangle.IntersectsWith(ClientRectangle) || rectangle.Width < 0 || rectangle.Height < 0) continue;
            var color = Colors[i % Colors.Length];
            var selected = Selection.Contains(i);
            if (selected)
            {
                using var tint = new SolidBrush(Color.FromArgb(40, color));
                graphics.FillRectangle(tint, rectangle);
            }
            using var shadow = new Pen(Color.FromArgb(180, Color.Black), selected ? 4 : 3);
            graphics.DrawRectangle(shadow, rectangle.X, rectangle.Y, rectangle.Width, rectangle.Height);
            using var line = new Pen(color, selected ? 2 : 1);
            graphics.DrawRectangle(line, rectangle.X, rectangle.Y, rectangle.Width, rectangle.Height);
            if (rectangle.Width > 24 && rectangle.Height > 18)
            {
                var label = Rectangle.Round(new RectangleF(rectangle.X + 3, rectangle.Y + 2, 60, 20));
                TextRenderer.DrawText(graphics, (i + 1).ToString(), KitStyle.Font(9, true), label, color, Color.FromArgb(25, 25, 25), TextFormatFlags.NoPadding);
            }
            if (selected && Selection.Count == 1)
                foreach (var point in Handles(rectangle))
                {
                    using var fill = new SolidBrush(color);
                    graphics.FillRectangle(fill, point.X - 4, point.Y - 4, 8, 8);
                }
        }
        if (box is not null)
        {
            var rect = ToScreen(box);
            using var pen = new Pen(Color.White, 2) { DashStyle = DashStyle.Dash };
            graphics.DrawRectangle(pen, rect.X, rect.Y, rect.Width, rect.Height);
        }
        graphics.Restore(clip);
        using var border = new Pen(Focused ? KitStyle.Accent : KitStyle.Border);
        graphics.DrawRectangle(border, 0, 0, Math.Max(0, Width - 1), Math.Max(0, Height - 1));
        if (preview is null && doc.Rectangles.Length == 0)
            TextRenderer.DrawText(graphics, "Open a texture or material, or draw on this blank sheet.", KitStyle.Font(11), new Rectangle(20, 20, Math.Max(0, Width - 40), 60), KitStyle.Text, TextFormatFlags.WordBreak);
    }

    private void DrawGrid(Graphics graphics, HotspotDocument doc)
    {
        var settings = session.Draft;
        if (!settings.ShowGrid) return;
        var step = settings.Grid;
        while (step * zoom < 12) step *= 2;
        var color = Color.FromName(settings.GridColor);
        using var minor = new Pen(Color.FromArgb(settings.GridOpacity * 255 / 100, color));
        using var major = new Pen(Color.FromArgb(Math.Min(255, settings.GridOpacity * 4), color));
        var left = Math.Max(0, (int)Math.Floor(-origin.X / zoom / step) * step);
        var top = Math.Max(0, (int)Math.Floor(-origin.Y / zoom / step) * step);
        var right = Math.Min(doc.Width, (int)Math.Ceiling((Width - origin.X) / zoom));
        var bottom = Math.Min(doc.Height, (int)Math.Ceiling((Height - origin.Y) / zoom));
        for (var x = left; x <= right; x += step)
        {
            var sx = origin.X + x * zoom;
            if (settings.GridStyle == "Lines") graphics.DrawLine(x / step % settings.GridMajor == 0 ? major : minor, sx, origin.Y, sx, origin.Y + doc.Height * zoom);
            else
                for (var y = top; y <= bottom; y += step)
                {
                    var sy = origin.Y + y * zoom;
                    var pen = x / step % settings.GridMajor == 0 && y / step % settings.GridMajor == 0 ? major : minor;
                    var radius = settings.GridStyle == "Crosses" ? 3 : 1;
                    graphics.DrawLine(pen, sx - radius, sy, sx + radius, sy);
                    graphics.DrawLine(pen, sx, sy - radius, sx, sy + radius);
                }
        }
        if (settings.GridStyle == "Lines")
            for (var y = top; y <= bottom; y += step)
                graphics.DrawLine(y / step % settings.GridMajor == 0 ? major : minor, origin.X, origin.Y + y * zoom, origin.X + doc.Width * zoom, origin.Y + y * zoom);
    }

    private static PointF[] Handles(RectangleF r) => [new(r.Left, r.Top), new(r.Left + r.Width / 2, r.Top), new(r.Right, r.Top),
        new(r.Right, r.Top + r.Height / 2), new(r.Right, r.Bottom), new(r.Left + r.Width / 2, r.Bottom), new(r.Left, r.Bottom), new(r.Left, r.Top + r.Height / 2)];

    protected override void OnMouseDown(MouseEventArgs e)
    {
        base.OnMouseDown(e);
        Focus();
        if (drag.Length != 0) return;
        down = e.Location; downTexture = ToTexture(e.Location); panOrigin = origin;
        if (e.Button is MouseButtons.Right or MouseButtons.Middle) { drag = "pan"; Capture = true; return; }
        if (e.Button != MouseButtons.Left) return;
        var doc = session.Draft.Document;
        if (downTexture.X < 0 || downTexture.Y < 0 || downTexture.X > doc.Width || downTexture.Y > doc.Height) return;
        before = doc.Rectangles;
        var forceDraw = DrawMode || (ModifierKeys & Keys.Alt) != 0;
        handle = -1;
        if (!forceDraw && Selection.Count == 1)
        {
            var handles = Handles(ToScreen(before[Selection.First()]));
            handle = Array.FindIndex(handles, p => Math.Abs(p.X - e.X) <= 7 && Math.Abs(p.Y - e.Y) <= 7);
        }
        var hit = forceDraw ? -1 : Enumerable.Range(0, before.Length)
            .Where(i => downTexture.X >= before[i].X && downTexture.X <= before[i].Right && downTexture.Y >= before[i].Y && downTexture.Y <= before[i].Bottom)
            .OrderBy(i => (long)before[i].Width * before[i].Height).ThenByDescending(i => i).DefaultIfEmpty(-1).First();
        if (handle >= 0) drag = "resize";
        else if (hit >= 0)
        {
            if ((ModifierKeys & Keys.Shift) != 0)
            {
                if (!Selection.Add(hit)) Selection.Remove(hit);
                SelectionChanged?.Invoke(this, EventArgs.Empty); Invalidate(); return;
            }
            if (!Selection.Contains(hit)) { Selection.Clear(); Selection.Add(hit); }
            drag = "move";
        }
        else
        {
            if ((ModifierKeys & Keys.Shift) == 0) Selection.Clear();
            drag = (ModifierKeys & Keys.Shift) != 0 ? "select" : "draw";
        }
        Capture = true;
        SelectionChanged?.Invoke(this, EventArgs.Empty);
        Invalidate();
    }

    protected override void OnMouseMove(MouseEventArgs e)
    {
        base.OnMouseMove(e);
        var point = ToTexture(e.Location);
        PointerMoved?.Invoke($"{(int)point.X}, {(int)point.Y}px   {zoom:P0}");
        if (drag == "") return;
        if (drag == "pan") { fitted = false; origin = new(panOrigin.X + e.X - down.X, panOrigin.Y + e.Y - down.Y); Invalidate(); return; }
        var doc = session.Draft.Document;
        if (before is null) return;
        if (drag is "draw" or "select")
        {
            var x = Math.Clamp(Snap(downTexture.X), 0, doc.Width); var y = Math.Clamp(Snap(downTexture.Y), 0, doc.Height);
            var right = Math.Clamp(Snap(point.X), 0, doc.Width); var bottom = Math.Clamp(Snap(point.Y), 0, doc.Height);
            box = new(Math.Min(x, right), Math.Min(y, bottom), Math.Abs(right - x), Math.Abs(bottom - y));
        }
        if (drag == "move")
        {
            var selected = Selection.Select(i => before[i]).ToArray();
            if (selected.Length == 0 || selected.Any(r => r.X < 0 || r.Y < 0 || r.Right > doc.Width || r.Bottom > doc.Height)) return;
            var dx = Math.Clamp(Snap(point.X - downTexture.X), -selected.Min(r => r.X), doc.Width - (int)selected.Max(r => r.Right));
            var dy = Math.Clamp(Snap(point.Y - downTexture.Y), -selected.Min(r => r.Y), doc.Height - (int)selected.Max(r => r.Bottom));
            working = before.Select((r, i) => Selection.Contains(i) ? r with { X = r.X + dx, Y = r.Y + dy } : r).ToArray();
        }
        if (drag == "resize")
        {
            var index = Selection.First(); var r = before[index];
            if (r.X < 0 || r.Y < 0 || r.Right > doc.Width || r.Bottom > doc.Height || r.Width <= 0 || r.Height <= 0) return;
            var x = r.X; var y = r.Y; var right = (int)r.Right; var bottom = (int)r.Bottom;
            if (handle is 0 or 6 or 7) x = Math.Clamp(Snap(point.X), 0, right - 1);
            if (handle is 0 or 1 or 2) y = Math.Clamp(Snap(point.Y), 0, bottom - 1);
            if (handle is 2 or 3 or 4) right = Math.Clamp(Snap(point.X), x + 1, doc.Width);
            if (handle is 4 or 5 or 6) bottom = Math.Clamp(Snap(point.Y), y + 1, doc.Height);
            working = before.ToArray();
            working[index] = r with { X = x, Y = y, Width = right - x, Height = bottom - y };
        }
        Invalidate();
    }

    protected override void OnMouseUp(MouseEventArgs e)
    {
        base.OnMouseUp(e);
        if (drag == "draw" && box is { Width: > 0, Height: > 0 } && session.Draft.Document.Rectangles.Length < HotspotDocument.MaxRectangles)
        {
            working = [.. session.Draft.Document.Rectangles, box];
            Selection.Clear(); Selection.Add(working.Length - 1);
        }
        if (drag == "select" && box is not null)
            for (var i = 0; i < session.Draft.Document.Rectangles.Length; i++)
            {
                var r = session.Draft.Document.Rectangles[i];
                if (r.X >= box.X && r.Y >= box.Y && r.Right <= box.Right && r.Bottom <= box.Bottom) Selection.Add(i);
            }
        var result = working;
        ResetDrag();
        if (result is not null) session.Edit(session.Draft.Document with { Rectangles = result });
        SelectionChanged?.Invoke(this, EventArgs.Empty);
        Invalidate();
    }

    private void ResetDrag() { drag = ""; before = null; working = null; box = null; Capture = false; }
    protected override void OnMouseCaptureChanged(EventArgs e) { if (!Capture && drag != "") { ResetDrag(); Invalidate(); } base.OnMouseCaptureChanged(e); }
    protected override void OnMouseDoubleClick(MouseEventArgs e) { if (e.Button == MouseButtons.Middle) Fit(); base.OnMouseDoubleClick(e); }
    protected override void OnMouseWheel(MouseEventArgs e)
    {
        if (drag.Length != 0) return;
        fitted = false;
        var at = ToTexture(e.Location);
        zoom = Math.Clamp(zoom * (float)Math.Pow(1.15, e.Delta / 120.0), 0.005F, 32);
        origin = new(e.X - at.X * zoom, e.Y - at.Y * zoom);
        Invalidate();
        base.OnMouseWheel(e);
    }

    public void SelectIndices(IEnumerable<int> indices)
    {
        Selection.Clear();
        foreach (var i in indices) if (i >= 0 && i < session.Draft.Document.Rectangles.Length) Selection.Add(i);
        SelectionChanged?.Invoke(this, EventArgs.Empty); Invalidate();
    }

    public void DeleteSelected()
    {
        var rects = session.Draft.Document.Rectangles.Where((_, i) => !Selection.Contains(i)).ToArray();
        Selection.Clear(); session.Edit(session.Draft.Document with { Rectangles = rects });
        SelectionChanged?.Invoke(this, EventArgs.Empty);
    }

    public void DuplicateSelected()
    {
        var doc = session.Draft.Document;
        if (doc.Rectangles.Length + Selection.Count > HotspotDocument.MaxRectangles) return;
        var copies = Selection.Order().Select(i => (doc.Rectangles[i] with { X = doc.Rectangles[i].X + session.Draft.Grid, Y = doc.Rectangles[i].Y + session.Draft.Grid }).Clamp(doc.Width, doc.Height)).ToArray();
        SelectIndices([]);
        session.Edit(doc with { Rectangles = [.. doc.Rectangles, .. copies] });
        SelectIndices(Enumerable.Range(doc.Rectangles.Length, copies.Length));
    }

    protected override bool IsInputKey(Keys keyData) => (keyData & Keys.KeyCode) is Keys.Left or Keys.Right or Keys.Up or Keys.Down || base.IsInputKey(keyData);
    protected override void OnKeyDown(KeyEventArgs e)
    {
        var doc = session.Draft.Document;
        if (e.KeyCode == Keys.Escape) { ResetDrag(); SelectIndices([]); }
        else if (e.Control && e.KeyCode == Keys.A) SelectIndices(Enumerable.Range(0, doc.Rectangles.Length));
        else if (e.Control && e.KeyCode == Keys.S) SaveRequested?.Invoke(this, EventArgs.Empty);
        else if (e.Control && e.KeyCode == Keys.D) DuplicateSelected();
        else if (e.Control && e.KeyCode is Keys.Z or Keys.Y) session.Undo(e.KeyCode == Keys.Y || e.Shift);
        else if (e.KeyCode is Keys.Delete or Keys.Back) DeleteSelected();
        else if (e.KeyCode == Keys.F) Fit();
        else if (e.KeyCode == Keys.G) session.Update(d => d with { ShowGrid = !d.ShowGrid });
        else if (e.KeyCode is Keys.Left or Keys.Right or Keys.Up or Keys.Down)
        {
            var step = e.Shift ? session.Draft.Grid : 1;
            var dx = e.KeyCode == Keys.Left ? -step : e.KeyCode == Keys.Right ? step : 0;
            var dy = e.KeyCode == Keys.Up ? -step : e.KeyCode == Keys.Down ? step : 0;
            var selected = Selection.Select(i => doc.Rectangles[i]).ToArray();
            if (selected.Length > 0 && selected.All(r => r.X >= 0 && r.Y >= 0 && r.Right <= doc.Width && r.Bottom <= doc.Height))
            {
                dx = Math.Clamp(dx, -selected.Min(r => r.X), doc.Width - (int)selected.Max(r => r.Right));
                dy = Math.Clamp(dy, -selected.Min(r => r.Y), doc.Height - (int)selected.Max(r => r.Bottom));
                session.Edit(doc with { Rectangles = doc.Rectangles.Select((r, i) => Selection.Contains(i) ? r with { X = r.X + dx, Y = r.Y + dy } : r).ToArray() });
            }
        }
        else { base.OnKeyDown(e); return; }
        e.Handled = true; e.SuppressKeyPress = true;
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            session.Changed -= SessionChanged;
            KitStyle.AppearanceChanged -= AppearanceChanged;
            preview?.Dispose(); preview = null;
        }
        base.Dispose(disposing);
    }
}
