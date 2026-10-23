namespace PortingKit.Addons.Hotspot;

public static class RegionDetection
{
    /// <summary>Finds alpha islands or recursively splits flat-color seams on the bounded preview, then maps back to original pixels.</summary>
    public static HotspotRect[] Detect(TexturePreview preview, int minimum, int snap, CancellationToken token)
    {
        if (minimum < 1 || snap < 0) throw new ArgumentException("Minimum region size must be positive and snapping nonnegative.");
        var w = preview.Bitmap.Width; var h = preview.Bitmap.Height;
        var data = TexturePreview.Pixels(preview.Bitmap);
        var regions = new List<HotspotRect>();
        var minX = Math.Max(1, (int)Math.Ceiling((double)minimum * w / preview.Width));
        var minY = Math.Max(1, (int)Math.Ceiling((double)minimum * h / preview.Height));
        if (data.Any(p => (uint)p >> 24 < 8)) Islands();
        else Cut(0, 0, w, h, 10);
        return regions.Select(r =>
        {
            int X(long n) => (int)Math.Round((double)n * preview.Width / w);
            int Y(long n) => (int)Math.Round((double)n * preview.Height / h);
            return new HotspotRect(X(r.X), Y(r.Y), X(r.Right) - X(r.X), Y(r.Bottom) - Y(r.Y)).Snap(snap, preview.Width, preview.Height);
        }).Where(r => r.Width >= minimum && r.Height >= minimum).Distinct().ToArray();

        void Add(HotspotRect rect)
        {
            if (rect.Width < minX || rect.Height < minY) return;
            if (regions.Count >= HotspotDocument.MaxRectangles) throw new InvalidDataException("Detection found too many regions. Increase the minimum size.");
            regions.Add(rect);
        }

        void Islands()
        {
            var queue = new Queue<int>();
            for (var start = 0; start < data.Length; start++)
            {
                if ((start & 4095) == 0) token.ThrowIfCancellationRequested();
                if ((uint)data[start] >> 24 < 8) continue;
                var minLeft = start % w; var maxRight = minLeft; var minTop = start / w; var maxBottom = minTop;
                data[start] = 0; queue.Enqueue(start);
                while (queue.TryDequeue(out var at))
                {
                    if ((at & 4095) == 0) token.ThrowIfCancellationRequested();
                    var x = at % w; var y = at / w;
                    minLeft = Math.Min(minLeft, x); maxRight = Math.Max(maxRight, x);
                    minTop = Math.Min(minTop, y); maxBottom = Math.Max(maxBottom, y);
                    if (x > 0) Visit(at - 1);
                    if (x + 1 < w) Visit(at + 1);
                    if (y > 0) Visit(at - w);
                    if (y + 1 < h) Visit(at + w);
                }
                Add(new(minLeft, minTop, maxRight - minLeft + 1, maxBottom - minTop + 1));
            }
            void Visit(int at)
            {
                if ((uint)data[at] >> 24 < 8) return;
                data[at] = 0;
                queue.Enqueue(at);
            }
        }

        void Cut(int x, int y, int width, int height, int depth)
        {
            token.ThrowIfCancellationRequested();
            if (width < minX || height < minY) return;
            if (depth > 0)
            {
                var split = Seam(false);
                if (split > 0) { Cut(x, y, width, split, depth - 1); Cut(x, y + split, width, height - split, depth - 1); return; }
                split = Seam(true);
                if (split > 0) { Cut(x, y, split, height, depth - 1); Cut(x + split, y, width - split, height, depth - 1); return; }
            }
            Add(new(x, y, width, height));

            int Seam(bool vertical)
            {
                var extent = vertical ? width : height;
                var across = vertical ? height : width;
                var min = vertical ? minX : minY;
                var start = -1;
                for (var i = 0; i < extent; i++)
                {
                    var first = data[(y + (vertical ? 0 : i)) * w + x + (vertical ? i : 0)] & 0xffffff;
                    var uniform = true;
                    for (var j = 1; j < across; j++)
                        if ((data[(y + (vertical ? j : i)) * w + x + (vertical ? i : j)] & 0xffffff) != first) { uniform = false; break; }
                    if (uniform && start < 0) start = i;
                    if (uniform || start < 0) continue;
                    var middle = (start + i) / 2;
                    if (start > 0 && middle >= min && middle <= extent - min) return middle;
                    start = -1;
                }
                return -1;
            }
        }
    }
}
