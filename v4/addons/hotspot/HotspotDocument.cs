using System.Text.Json;
using System.Text.Json.Serialization;

namespace PortingKit.Addons.Hotspot;

/// <summary>Pixel coordinates with a top-left origin. Right and bottom edges are exclusive.</summary>
public sealed record HotspotRect(int X, int Y, int Width, int Height,
    bool Rotate = false, bool Reflect = false, bool Alt = false, bool Tiling = false,
    int InsetX = 0, int InsetY = 0)
{
    [JsonIgnore] public long Right => (long)X + Width;
    [JsonIgnore] public long Bottom => (long)Y + Height;

    public HotspotRect Clamp(int width, int height)
    {
        var x = Math.Clamp(X, 0, width - 1);
        var y = Math.Clamp(Y, 0, height - 1);
        return this with { X = x, Y = y, Width = Math.Clamp(Width, 1, width - x), Height = Math.Clamp(Height, 1, height - y) };
    }

    public HotspotRect Snap(int grid, int width, int height)
    {
        if (grid < 2) return Clamp(width, height);
        var x = (int)Math.Round((double)X / grid) * grid;
        var y = (int)Math.Round((double)Y / grid) * grid;
        return (this with { X = x, Y = y, Width = Math.Max(grid, (int)Math.Round((double)Right / grid) * grid - x),
            Height = Math.Max(grid, (int)Math.Round((double)Bottom / grid) * grid - y) }).Clamp(width, height);
    }
}

public sealed record HotspotIssue(string Level, string Message, int? Rectangle = null);
public sealed record FitMatch(int Rectangle, double Score, bool Rotated);

/// <summary>Editable interchange document shared by the desktop, CLI, and MCP. Dimensions describe the original texture.</summary>
public sealed record HotspotDocument(int Width, int Height, HotspotRect[] Rectangles)
{
    public int Version { get; init; } = 1;
    public string SourceFormat { get; init; } = "";
    public string[] Comments { get; init; } = [];
    public string[] ImportWarnings { get; init; } = [];
    public const int MaxRectangles = 4096;
    public static JsonSerializerOptions JsonOptions { get; } = new(JsonSerializerDefaults.Web)
    {
        WriteIndented = true,
        UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow
    };

    public void CheckStructure()
    {
        if (Version != 1) throw new InvalidDataException("Unsupported hotspot document version.");
        if (Width is < 1 or > 65535 || Height is < 1 or > 65535)
            throw new InvalidDataException("Texture width and height must be between 1 and 65535 pixels.");
        if (Rectangles is null || Rectangles.Length > MaxRectangles || Rectangles.Any(r => r is null))
            throw new InvalidDataException($"A document may contain at most {MaxRectangles} rectangles.");
        if (Comments is null || ImportWarnings is null || Comments.Any(c => c is null || c.Contains('\n') || c.Contains('\r')))
            throw new InvalidDataException("Document comments must be single lines.");
    }

    public HotspotIssue[] Validate(int grid = 0)
    {
        CheckStructure();
        var issues = new List<HotspotIssue>();
        var seen = new Dictionary<(int, int, int, int), int>();
        if (Rectangles.Length == 0) issues.Add(new("warning", "The sheet has no rectangles."));
        for (var i = 0; i < Rectangles.Length; i++)
        {
            var r = Rectangles[i];
            if (r.Width <= 0 || r.Height <= 0) issues.Add(new("error", $"Rectangle {i + 1} has no area.", i));
            if (r.X < 0 || r.Y < 0 || r.Right > Width || r.Bottom > Height)
                issues.Add(new("error", $"Rectangle {i + 1} extends outside the texture.", i));
            if (r.InsetX < 0 || r.InsetY < 0 || (long)r.InsetX * 2 >= r.Width || (long)r.InsetY * 2 >= r.Height)
                issues.Add(new("error", $"Rectangle {i + 1} has an invalid inset.", i));
            if (r.Width < 4 || r.Height < 4) issues.Add(new("warning", $"Rectangle {i + 1} is smaller than 4 pixels.", i));
            if (grid > 1 && new long[] { r.X, r.Y, r.Right, r.Bottom }.Any(v => v % grid != 0))
                issues.Add(new("info", $"Rectangle {i + 1} is off the {grid}px grid.", i));
            var key = (r.X, r.Y, r.Width, r.Height);
            if (seen.TryGetValue(key, out var first)) issues.Add(new("warning", $"Rectangle {i + 1} duplicates rectangle {first + 1}.", i));
            else seen.Add(key, i);
        }
        var factor = OversizeFactor();
        if (factor > 1) issues.Add(new("warning", $"These regions appear to use a texture {factor} times larger. Use Sheet > Fix oversized regions to scale them to this texture."));
        return issues.ToArray();
    }

    public int OversizeFactor()
    {
        if (Rectangles.Length == 0 || Rectangles.Any(r => r.X < 0 || r.Y < 0 || r.Width <= 0 || r.Height <= 0)) return 1;
        var right = Rectangles.Max(r => r.Right); var bottom = Rectangles.Max(r => r.Bottom);
        if (right <= Width && bottom <= Height) return 1;
        for (var factor = 2; factor <= 16; factor *= 2)
            if ((right == (long)Width * factor || bottom == (long)Height * factor) && right <= (long)Width * factor && bottom <= (long)Height * factor &&
                Rectangles.All(r => r.X % factor == 0 && r.Y % factor == 0 && r.Width % factor == 0 && r.Height % factor == 0)) return factor;
        return 1;
    }

    public HotspotDocument RepairOversize()
    {
        var factor = OversizeFactor();
        return factor == 1 ? this : this with { Rectangles = Rectangles.Select(r => r with
        {
            X = r.X / factor, Y = r.Y / factor, Width = r.Width / factor, Height = r.Height / factor,
            InsetX = r.InsetX / factor, InsetY = r.InsetY / factor
        }).ToArray() };
    }

    public double Coverage()
    {
        var rects = Rectangles.Where(r => r.Width > 0 && r.Height > 0 && r.X < Width && r.Y < Height && r.Right > 0 && r.Bottom > 0).ToArray();
        var edges = rects.SelectMany(r => new[] { Math.Clamp((long)r.Y, 0, Height), Math.Clamp(r.Bottom, 0, Height) }).Distinct().Order().ToArray();
        long area = 0;
        for (var i = 0; i + 1 < edges.Length; i++)
        {
            long reach = 0, covered = 0;
            foreach (var r in rects.Where(r => r.Y <= edges[i] && r.Bottom >= edges[i + 1]).OrderBy(r => r.X))
            {
                var end = Math.Clamp(r.Right, 0, Width);
                if (end <= reach) continue;
                covered += end - Math.Max(reach, Math.Max(0, r.X));
                reach = end;
            }
            area += covered * (edges[i + 1] - edges[i]);
        }
        return area / ((double)Width * Height);
    }

    /// <summary>Aspect-dominant heuristic from V3, not Hammer's unpublished randomized selection algorithm.</summary>
    public FitMatch[] Match(double width, double height, bool includeAlt = false)
    {
        if (!double.IsFinite(width) || !double.IsFinite(height) || width <= 0 || height <= 0) return [];
        double Score(double w, double h) => 4 * Math.Abs(Math.Log((w / h) / (width / height))) + Math.Abs(Math.Log(w * h / (width * height)));
        return Rectangles.Select((r, i) => (r, i)).Where(p => p.r.Width > 0 && p.r.Height > 0 && (includeAlt || !p.r.Alt))
            .Select(p =>
            {
                var normal = Score(p.r.Width, p.r.Height);
                var rotated = p.r.Rotate ? Score(p.r.Height, p.r.Width) : double.PositiveInfinity;
                return new FitMatch(p.i, Math.Min(normal, rotated), rotated < normal);
            }).OrderBy(m => m.Score).ThenBy(m => m.Rectangle).Take(5).ToArray();
    }

    public string[] Losses(string target, bool flags = true)
    {
        var losses = new List<string>(ImportWarnings);
        if (target == "source2")
        {
            if (Rectangles.Any(r => r.Reflect || r.Alt)) losses.Add("Source 2 cannot store Strata reflect or alt flags.");
            if (Comments.Length != 0) losses.Add("Hammer++ comments are omitted from Source 2 output.");
        }
        else
        {
            if (Rectangles.Any(r => r.Tiling || r.InsetX != 0 || r.InsetY != 0)) losses.Add("Hammer++/Strata cannot store Source 2 tiling or insets.");
            if (!flags && Rectangles.Any(r => r.Rotate || r.Reflect || r.Alt)) losses.Add("Rotation, reflection, and alternate flags are omitted in plain Hammer++ output.");
        }
        return losses.ToArray();
    }

    public static HotspotRect[] Grid(int width, int height, int columns, int rows)
    {
        if (columns < 1 || rows < 1 || columns > width || rows > height || (long)columns * rows > MaxRectangles)
            throw new ArgumentException("The layout must fit the texture and contain at most 4096 cells.");
        var result = new List<HotspotRect>();
        for (var y = 0; y < rows; y++)
        for (var x = 0; x < columns; x++)
        {
            var left = (int)Math.Round((double)x * width / columns);
            var top = (int)Math.Round((double)y * height / rows);
            result.Add(new(left, top, (int)Math.Round((double)(x + 1) * width / columns) - left,
                (int)Math.Round((double)(y + 1) * height / rows) - top));
        }
        return result.ToArray();
    }

    public static HotspotRect[] Trim(int width, int height, int[] counts, bool vertical = false)
    {
        var bands = Grid(width, height, vertical ? counts.Length : 1, vertical ? 1 : counts.Length);
        return bands.SelectMany((band, i) => Grid(band.Width, band.Height, vertical ? 1 : counts[i], vertical ? counts[i] : 1)
            .Select(r => r with { X = r.X + band.X, Y = r.Y + band.Y })).ToArray();
    }
}
