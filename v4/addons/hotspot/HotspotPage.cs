using PortingKit.Desktop;

namespace PortingKit.Addons.Hotspot;

internal sealed class HotspotPage(IAddonContext addon, HotspotSession session) : IDesktopPage
{
    public string Name => "Hotspot editor";
    public string Category => "Materials";
    public string Description => "Draw trim-sheet regions and port Source 2 rectangle maps to Hammer++ or Strata.";
    public Control Create(DesktopContext context) => new HotspotEditor(addon, session);
}

internal sealed class HotspotEditor : UserControl
{
    private readonly IAddonContext addon;
    private readonly HotspotSession session;
    private readonly HotspotCanvas canvas;
    private readonly CancellationTokenSource lifetime = new();
    private readonly DataGridView list = KitStyle.Grid();
    private readonly Label status = KitStyle.Label("", 9);
    private readonly Label cursor = KitStyle.Label("", 9);
    private readonly Label target = KitStyle.Label("", 9);
    private readonly TextBox issues = new() { Multiline = true, ReadOnly = true, Dock = DockStyle.Fill, ScrollBars = ScrollBars.Vertical };
    private readonly TextBox fitResult = new() { Multiline = true, ReadOnly = true, Height = KitStyle.Scale(112), Width = KitStyle.Scale(280) };
    private readonly Dictionary<string, NumericUpDown> fields = [];
    private readonly Dictionary<string, CheckBox> flags = [];
    private readonly NumericUpDown sheetWidth = Number(1, 65535, 1024);
    private readonly NumericUpDown sheetHeight = Number(1, 65535, 1024);
    private readonly NumericUpDown grid = Number(1, 1024, 16);
    private readonly NumericUpDown columns = Number(1, 64, 2);
    private readonly NumericUpDown rows = Number(1, 64, 2);
    private readonly NumericUpDown minimum = Number(1, 4096, 16);
    private readonly NumericUpDown faceWidth = Number(1, 65535, 128);
    private readonly NumericUpDown faceHeight = Number(1, 65535, 128);
    private readonly CheckBox snap = new() { Text = "Snap", AutoSize = true };
    private readonly CheckBox writeFlags = new() { Text = "Write Strata flags", AutoSize = true };
    private readonly CheckBox link = new() { Text = "Link VMT when saving", AutoSize = true };
    private readonly ComboBox format = Choice(["Hammer++ / Strata", "Source 2 KV3"]);
    private bool refreshing;
    private bool busy;
    private int reportVersion;
    private HotspotDocument? displayedDocument;

    public HotspotEditor(IAddonContext addon, HotspotSession session)
    {
        this.addon = addon; this.session = session;
        canvas = new(session);
        Dock = DockStyle.Fill; Padding = new(12);
        var root = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 6 };
        root.RowStyles.Add(new(SizeType.AutoSize));
        root.RowStyles.Add(new(SizeType.AutoSize));
        root.RowStyles.Add(new(SizeType.AutoSize));
        root.RowStyles.Add(new(SizeType.Percent, 100));
        root.RowStyles.Add(new(SizeType.Absolute, KitStyle.Scale(60)));
        root.RowStyles.Add(new(SizeType.AutoSize));
        root.Controls.Add(KitStyle.Label("Hotspot editor", 19, true));
        var files = Row();
        files.Controls.AddRange([
            KitStyle.Button("Open texture...", async (_, _) => await PickTextureAsync(false)),
            KitStyle.Button("Open material...", async (_, _) => await PickTextureAsync(true)),
            KitStyle.Button("Open .rect / JSON...", async (_, _) => await PickRectAsync()),
            KitStyle.Button("New sheet", (_, _) => NewSheet()),
            KitStyle.PrimaryButton("Save", async (_, _) => await SaveAsync(false)),
            KitStyle.Button("Save as...", async (_, _) => await SaveAsync(true))
        ]);
        root.Controls.Add(files);
        var edits = Row();
        edits.Controls.AddRange([
            KitStyle.Button("Undo", (_, _) => session.Undo()), KitStyle.Button("Redo", (_, _) => session.Undo(true)),
            KitStyle.Button("Duplicate", (_, _) => canvas.DuplicateSelected()), KitStyle.Button("Delete", (_, _) => canvas.DeleteSelected()),
            KitStyle.Button("Fit view", (_, _) => canvas.Fit())
        ]);
        var draw = new CheckBox { Text = "Draw over regions", AutoSize = true, Padding = new(0, 6, 0, 0) };
        draw.CheckedChanged += (_, _) => canvas.DrawMode = draw.Checked;
        edits.Controls.Add(draw); edits.Controls.Add(KitStyle.Label("Grid", 9)); edits.Controls.Add(grid); edits.Controls.Add(snap);
        root.Controls.Add(edits);
        var workspace = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1 };
        workspace.ColumnStyles.Add(new(SizeType.Percent, 100)); workspace.ColumnStyles.Add(new(SizeType.Absolute, KitStyle.Scale(330)));
        workspace.Controls.Add(canvas);
        var tabs = new TabControl { Dock = DockStyle.Fill };
        tabs.TabPages.Add(SelectionTab()); tabs.TabPages.Add(SheetTab()); tabs.TabPages.Add(ViewTab());
        workspace.Controls.Add(tabs);
        root.Controls.Add(workspace);
        root.Controls.Add(issues);
        var footer = Row(); footer.Controls.Add(status); footer.Controls.Add(cursor);
        root.Controls.Add(footer);
        Controls.Add(root);
        session.Changed += Changed;
        canvas.SelectionChanged += (_, _) => RefreshSelection();
        canvas.PointerMoved += value => cursor.Text = value;
        canvas.SaveRequested += async (_, _) => await SaveAsync(false);
        grid.ValueChanged += (_, _) => { if (!refreshing) session.Update(d => d with { Grid = (int)grid.Value }); };
        snap.CheckedChanged += (_, _) => { if (!refreshing) session.Update(d => d with { Snap = snap.Checked }); };
        format.SelectedIndexChanged += (_, _) => { if (!refreshing) session.Update(d => d with { Format = format.SelectedIndex == 1 ? "source2" : "hammerpp" }); };
        writeFlags.CheckedChanged += (_, _) => { if (!refreshing) session.Update(d => d with { WriteFlags = writeFlags.Checked }); };
        link.CheckedChanged += (_, _) => { if (!refreshing) session.Update(d => d with { LinkMaterial = link.Checked }); };
        faceWidth.ValueChanged += (_, _) => UpdateFit(); faceHeight.ValueChanged += (_, _) => UpdateFit();
        RefreshDocument();
        Load += async (_, _) =>
        {
            canvas.Fit();
            if (session.RecoveryError is not null) issues.Text = session.RecoveryError;
            if (File.Exists(session.Draft.Texture)) await RunAsync(async token =>
            {
                var preview = await Task.Run(() => TexturePreview.Load(session.Draft.Texture, token), token);
                if (IsDisposed) { preview.Dispose(); return; }
                canvas.SetPreview(preview);
            });
        };
    }

    private static NumericUpDown Number(int min, int max, int value) => new() { Minimum = min, Maximum = max, Value = value, Width = KitStyle.Scale(82) };
    private static ComboBox Choice(string[] values)
    {
        var box = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = KitStyle.Scale(270) };
        box.Items.AddRange(values); box.SelectedIndex = 0; return box;
    }
    private static FlowLayoutPanel Row() => new() { Dock = DockStyle.Fill, AutoSize = true, WrapContents = true, Margin = new(0, 2, 0, 4) };
    private static FlowLayoutPanel Column() => new() { Dock = DockStyle.Fill, AutoScroll = true, FlowDirection = FlowDirection.TopDown, WrapContents = false, Padding = new(8) };
    private static Label Caption(string text) { var label = KitStyle.Label(text, 9); label.MaximumSize = new(KitStyle.Scale(280), 0); label.AutoSize = true; return label; }
    private static void Field(FlowLayoutPanel panel, string label, Control control)
    {
        var row = new FlowLayoutPanel { AutoSize = true, WrapContents = false, Margin = new(0, 3, 0, 3) };
        var caption = KitStyle.Label(label, 9); caption.AutoSize = false; caption.Width = KitStyle.Scale(115); caption.Height = KitStyle.Scale(26);
        row.Controls.Add(caption); row.Controls.Add(control); panel.Controls.Add(row);
    }

    private TabPage SelectionTab()
    {
        var page = new TabPage("Rectangles");
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, RowCount = 2, ColumnCount = 1 };
        layout.RowStyles.Add(new(SizeType.Percent, 100)); layout.RowStyles.Add(new(SizeType.Absolute, KitStyle.Scale(268)));
        list.Dock = DockStyle.Fill; list.MultiSelect = true; list.SelectionMode = DataGridViewSelectionMode.FullRowSelect;
        list.Columns.Add("number", "#"); list.Columns.Add("position", "Position"); list.Columns.Add("size", "Size");
        list.Columns[0].FillWeight = 26; list.Columns[1].FillWeight = 80; list.Columns[2].FillWeight = 80;
        list.VirtualMode = true;
        list.CellValueNeeded += (_, e) =>
        {
            var rects = session.Draft.Document.Rectangles;
            if (e.RowIndex >= rects.Length) return;
            var r = rects[e.RowIndex];
            e.Value = e.ColumnIndex switch { 0 => (object)(e.RowIndex + 1), 1 => $"{r.X}, {r.Y}", _ => $"{r.Width} x {r.Height}" };
        };
        list.SelectionChanged += (_, _) => { if (!refreshing) canvas.SelectIndices(list.SelectedRows.Cast<DataGridViewRow>().Select(r => r.Index)); };
        layout.Controls.Add(list);
        var selected = Column();
        selected.Controls.Add(Caption("Select a region to edit exact pixel coordinates."));
        var pairs = new FlowLayoutPanel { AutoSize = true, Width = KitStyle.Scale(290), WrapContents = true };
        foreach (var (key, label, min) in new[] { ("x", "X", 0), ("y", "Y", 0), ("width", "W", 1), ("height", "H", 1), ("insetX", "Inset X", 0), ("insetY", "Inset Y", 0) })
        {
            var box = Number(min, 65535, min); box.Width = KitStyle.Scale(75); box.AccessibleName = label; box.Margin = new(0);
            fields.Add(key, box);
            var row = new FlowLayoutPanel { AutoSize = false, WrapContents = false, Width = KitStyle.Scale(135), Height = KitStyle.Scale(32), Margin = new(0, 0, 4, 2) };
            var caption = KitStyle.Label(label, 9); caption.AutoSize = false; caption.Size = new(KitStyle.Scale(50), KitStyle.Scale(25)); caption.Margin = new(0);
            row.Controls.Add(caption); row.Controls.Add(box); pairs.Controls.Add(row);
        }
        selected.Controls.Add(pairs);
        selected.Controls.Add(KitStyle.Button("Apply coordinates", (_, _) => ApplyCoordinates()));
        var options = new FlowLayoutPanel { AutoSize = true, Width = KitStyle.Scale(285) };
        foreach (var label in new[] { "Rotate", "Reflect", "Alt", "Tiling" })
        {
            var check = new CheckBox { Text = label, AutoSize = true, ThreeState = true };
            flags.Add(label, check); options.Controls.Add(check);
            check.CheckStateChanged += (_, _) => ApplyFlag(label, check);
        }
        selected.Controls.Add(options);
        selected.Controls.Add(Caption("Reflect / Alt: Strata. Tiling / insets: Source 2. Hammer++ ignores flags."));
        layout.Controls.Add(selected); page.Controls.Add(layout); return page;
    }

    private TabPage SheetTab()
    {
        var page = new TabPage("Sheet"); var panel = Column();
        panel.Controls.Add(KitStyle.Label("Texture dimensions", 11, true));
        Field(panel, "Width", sheetWidth); Field(panel, "Height", sheetHeight);
        panel.Controls.Add(KitStyle.Button("Apply size", (_, _) => ResizeSheet()));
        panel.Controls.Add(Caption("Changing size keeps pixel coordinates. Use Scale regions to resize existing geometry."));
        panel.Controls.Add(KitStyle.Button("Scale regions to size", (_, _) => ResizeSheet(true)));
        panel.Controls.Add(KitStyle.Button("Fix oversized regions", (_, _) => session.Edit(session.Draft.Document.RepairOversize())));
        panel.Controls.Add(KitStyle.Label("Output", 11, true));
        panel.Controls.Add(format); panel.Controls.Add(writeFlags); panel.Controls.Add(link);
        target.MaximumSize = new(KitStyle.Scale(280), 0); target.AutoSize = true; panel.Controls.Add(target);
        panel.Controls.Add(KitStyle.Label("Layouts", 11, true));
        Field(panel, "Columns", columns); Field(panel, "Rows", rows);
        panel.Controls.Add(KitStyle.Button("Replace with grid", (_, _) => ApplyLayout("grid")));
        panel.Controls.Add(KitStyle.Button("Trim 1 / 2 / 4", (_, _) => ApplyLayout("124")));
        panel.Controls.Add(KitStyle.Button("Trim 1 / 2 / 4 / 8", (_, _) => ApplyLayout("1248")));
        panel.Controls.Add(KitStyle.Button("Columns 2 / 4", (_, _) => ApplyLayout("24")));
        panel.Controls.Add(KitStyle.Label("Region detection", 11, true)); Field(panel, "Minimum pixels", minimum);
        panel.Controls.Add(KitStyle.Button("Detect regions", async (_, _) => await DetectAsync()));
        panel.Controls.Add(Caption("Alpha islands or flat seams. Detection uses the preview; review its result. Undo restores the previous regions."));
        panel.Controls.Add(KitStyle.Label("Fit preview (approximate)", 11, true));
        Field(panel, "Face width", faceWidth); Field(panel, "Face height", faceHeight);
        panel.Controls.Add(fitResult);
        panel.Controls.Add(Caption("Aspect and area ranking, not Hammer's exact randomized choice. Alt regions are excluded."));
        page.Controls.Add(panel); return page;
    }

    private TabPage ViewTab()
    {
        var page = new TabPage("View / help"); var panel = Column();
        var show = new CheckBox { Text = "Show grid", Checked = session.Draft.ShowGrid, AutoSize = true };
        show.CheckedChanged += (_, _) => { if (!refreshing) session.Update(d => d with { ShowGrid = show.Checked }); };
        session.Changed += SyncGrid;
        Disposed += (_, _) => session.Changed -= SyncGrid;
        void SyncGrid(object? sender, EventArgs args) { refreshing = true; show.Checked = session.Draft.ShowGrid; refreshing = false; }
        panel.Controls.Add(show);
        var style = Choice(["Lines", "Dots", "Crosses"]); style.SelectedItem = session.Draft.GridStyle;
        var color = Choice(["White", "Black", "Cyan", "Lime", "Yellow", "Orange", "Magenta", "Red"]); color.SelectedItem = session.Draft.GridColor;
        var opacity = Number(1, 100, session.Draft.GridOpacity); var major = Number(1, 32, session.Draft.GridMajor);
        panel.Controls.Add(Caption("Grid style")); panel.Controls.Add(style); panel.Controls.Add(Caption("Grid ink")); panel.Controls.Add(color);
        Field(panel, "Strength %", opacity); Field(panel, "Major interval", major);
        style.SelectedIndexChanged += (_, _) => session.Update(d => d with { GridStyle = style.Text });
        color.SelectedIndexChanged += (_, _) => session.Update(d => d with { GridColor = color.Text });
        opacity.ValueChanged += (_, _) => session.Update(d => d with { GridOpacity = (int)opacity.Value });
        major.ValueChanged += (_, _) => session.Update(d => d with { GridMajor = (int)major.Value });
        panel.Controls.Add(KitStyle.Label("Canvas controls", 11, true));
        panel.Controls.Add(Caption("Drag empty space to draw. Drag a region to move it. Drag its handles to resize. Alt-drag draws over existing regions. Shift-click toggles selection; Shift-drag selects a group."));
        panel.Controls.Add(Caption("Wheel: zoom at cursor\nRight / middle drag: pan\nF: fit view    G: grid\nArrows: move 1px\nShift + arrows: move one grid cell\nDelete: remove selection\nCtrl+A: select all    Esc: clear / cancel\nCtrl+D: duplicate\nCtrl+Z / Ctrl+Y: undo / redo\nCtrl+S: save"));
        panel.Controls.Add(Caption("Edits recover automatically in this add-on's local draft. Save writes the actual .rect. Overlapping rectangles are allowed. Existing files are backed up as .bak."));
        page.Controls.Add(panel); return page;
    }

    private void Changed(object? sender, EventArgs args) => RefreshDocument();
    private void RefreshDocument()
    {
        refreshing = true;
        var draft = session.Draft; var doc = draft.Document;
        sheetWidth.Value = doc.Width; sheetHeight.Value = doc.Height;
        grid.Value = draft.Grid; snap.Checked = draft.Snap;
        format.SelectedIndex = draft.Format == "source2" ? 1 : 0; writeFlags.Checked = draft.WriteFlags; link.Checked = draft.LinkMaterial;
        link.Enabled = draft.Format == "hammerpp" && File.Exists(draft.Material);
        if (!ReferenceEquals(displayedDocument, doc))
        {
            list.RowCount = doc.Rectangles.Length;
            list.Invalidate();
            displayedDocument = doc;
        }
        target.Text = "Save: " + (draft.RectPath.Length == 0 ? "choose a .rect path" : draft.RectPath) + "\nMaterial: " + (draft.Material.Length == 0 ? "none" : draft.Material);
        _ = UpdateReportAsync(draft, ++reportVersion);
        refreshing = false;
        RefreshSelection(); UpdateFit();
    }

    private async Task UpdateReportAsync(HotspotDraft draft, int version)
    {
        try
        {
            var report = await Task.Run(() => (Issues: string.Join(Environment.NewLine, draft.Document.Validate(draft.Grid).OrderByDescending(i => i.Message.Contains("times larger", StringComparison.Ordinal)).Select(i => i.Message)
                .Concat(draft.Document.Losses(draft.Format, draft.WriteFlags)).Take(40)), Coverage: draft.Document.Coverage()), lifetime.Token);
            if (IsDisposed || version != reportVersion) return;
            issues.Text = report.Issues.Length == 0 ? "No problems found. Overlapping rectangles are allowed." : report.Issues;
            status.Text = $"{draft.Document.Width} x {draft.Document.Height}px   {draft.Document.Rectangles.Length} regions   {report.Coverage:P0} coverage   {(draft.Dirty ? "Unsaved .rect changes; recovery draft kept" : "Ready")}";
        }
        catch (OperationCanceledException) { }
        catch (Exception ex) { if (!IsDisposed) issues.Text = ex.Message; }
    }

    private void RefreshSelection()
    {
        refreshing = true;
        list.ClearSelection();
        foreach (var i in canvas.Selection) if (i < list.Rows.Count) list.Rows[i].Selected = true;
        var rects = canvas.Selection.Order().Where(i => i < session.Draft.Document.Rectangles.Length).Select(i => session.Draft.Document.Rectangles[i]).ToArray();
        foreach (var box in fields.Values) box.Enabled = rects.Length == 1;
        if (rects.Length == 1)
        {
            var r = rects[0];
            foreach (var (key, value) in new[] { ("x", r.X), ("y", r.Y), ("width", r.Width), ("height", r.Height), ("insetX", r.InsetX), ("insetY", r.InsetY) })
                fields[key].Value = Math.Clamp(value, fields[key].Minimum, fields[key].Maximum);
        }
        foreach (var (label, check) in flags)
        {
            check.Enabled = rects.Length > 0;
            bool Value(HotspotRect r) => label switch { "Rotate" => r.Rotate, "Reflect" => r.Reflect, "Alt" => r.Alt, _ => r.Tiling };
            check.CheckState = rects.Length == 0 || rects.All(r => !Value(r)) ? CheckState.Unchecked : rects.All(Value) ? CheckState.Checked : CheckState.Indeterminate;
        }
        refreshing = false;
    }

    private void ApplyCoordinates()
    {
        if (canvas.Selection.Count != 1) return;
        var doc = session.Draft.Document; var index = canvas.Selection.First(); var rects = doc.Rectangles.ToArray();
        int Value(string key) => (int)fields[key].Value;
        rects[index] = rects[index] with { X = Value("x"), Y = Value("y"), Width = Value("width"), Height = Value("height"), InsetX = Value("insetX"), InsetY = Value("insetY") };
        session.Edit(doc with { Rectangles = rects });
    }

    private void ApplyFlag(string label, CheckBox check)
    {
        if (refreshing || check.CheckState == CheckState.Indeterminate) return;
        var value = check.Checked;
        session.Edit(session.Draft.Document with { Rectangles = session.Draft.Document.Rectangles.Select((r, i) => !canvas.Selection.Contains(i) ? r : label switch
        {
            "Rotate" => r with { Rotate = value }, "Reflect" => r with { Reflect = value }, "Alt" => r with { Alt = value }, _ => r with { Tiling = value }
        }).ToArray() });
    }

    private void UpdateFit() => fitResult.Text = string.Join(Environment.NewLine, session.Draft.Document.Match((double)faceWidth.Value, (double)faceHeight.Value)
        .Select((m, i) => $"{(i == 0 ? "Best" : "    ")} #{m.Rectangle + 1}   score {m.Score:F2}{(m.Rotated ? " (rotated)" : "")}"));

    private bool Discard() => !session.Draft.Dirty || MessageBox.Show(this, "Replace this sheet? Its unsaved changes and recovery draft will be replaced.", "Open another sheet", MessageBoxButtons.OKCancel, MessageBoxIcon.Question) == DialogResult.OK;
    private void NewSheet()
    {
        if (!Discard()) return;
        canvas.SelectIndices([]); canvas.SetPreview(null);
        session.Replace(session.Draft with { Document = new((int)sheetWidth.Value, (int)sheetHeight.Value, []), Texture = "", Material = "", RectPath = "", Dirty = false });
        canvas.Fit();
    }

    private async Task PickTextureAsync(bool material)
    {
        using var dialog = new OpenFileDialog { Filter = material ? "Source 1 material|*.vmt" : "Textures|*.vtf;*.png;*.tga;*.jpg;*.jpeg;*.bmp;*.gif;*.tif;*.tiff", InitialDirectory = addon.Settings.Get("lastDirectory", "") };
        if (dialog.ShowDialog(this) != DialogResult.OK || !Discard()) return;
        await RunAsync(async token =>
        {
            var loaded = await Task.Run(() =>
            {
                var vmt = material ? dialog.FileName : MaterialFiles.MaterialFor(dialog.FileName) ?? "";
                var texture = material ? MaterialFiles.TextureFor(vmt) ?? throw new InvalidDataException("Could not find this material's base texture.") : dialog.FileName;
                var preview = TexturePreview.Load(texture, token);
                try
                {
                    var rect = vmt.Length > 0 ? MaterialFiles.RectFor(vmt) : Path.ChangeExtension(texture, ".rect");
                    var doc = File.Exists(rect) ? HotspotFormats.Read(rect, preview.Width, preview.Height) : new HotspotDocument(preview.Width, preview.Height, []);
                    return (preview, texture, vmt, rect, doc);
                }
                catch { preview.Dispose(); throw; }
            }, token);
            if (IsDisposed) { loaded.preview.Dispose(); return; }
            canvas.SelectIndices([]); canvas.SetPreview(loaded.preview);
            session.Replace(session.Draft with { Document = loaded.doc, Texture = loaded.texture, Material = loaded.vmt, RectPath = loaded.rect,
                Format = loaded.doc.SourceFormat == "source2" ? "source2" : "hammerpp", Dirty = false, LinkMaterial = loaded.vmt.Length > 0 });
            canvas.Fit();
            await addon.Settings.SetAsync("lastDirectory", Path.GetDirectoryName(dialog.FileName)!);
        });
    }

    private async Task PickRectAsync()
    {
        using var dialog = new OpenFileDialog { Filter = "Hotspot sheets|*.rect;*.json", InitialDirectory = addon.Settings.Get("lastDirectory", "") };
        if (dialog.ShowDialog(this) != DialogResult.OK || !Discard()) return;
        var width = (int)sheetWidth.Value; var height = (int)sheetHeight.Value;
        await RunAsync(async token =>
        {
            var doc = await Task.Run(() => HotspotFormats.Read(dialog.FileName, width, height), token);
            if (IsDisposed) return;
            canvas.SelectIndices([]);
            var dimensionsChanged = doc.Width != session.Draft.Document.Width || doc.Height != session.Draft.Document.Height;
            if (dimensionsChanged) canvas.SetPreview(null);
            session.Replace(session.Draft with { Document = doc, RectPath = Path.ChangeExtension(dialog.FileName, ".rect"),
                Texture = dimensionsChanged ? "" : session.Draft.Texture, Format = doc.SourceFormat == "source2" ? "source2" : "hammerpp", Dirty = Path.GetExtension(dialog.FileName).Equals(".json", StringComparison.OrdinalIgnoreCase) });
            canvas.Fit();
            await addon.Settings.SetAsync("lastDirectory", Path.GetDirectoryName(dialog.FileName)!);
        });
    }

    private void ResizeSheet(bool scale = false)
    {
        var doc = session.Draft.Document; var width = (int)sheetWidth.Value; var height = (int)sheetHeight.Value;
        int X(long n) => (int)Math.Round((double)n * width / doc.Width);
        int Y(long n) => (int)Math.Round((double)n * height / doc.Height);
        var rects = scale ? doc.Rectangles.Select(r => r with { X = X(r.X), Y = Y(r.Y), Width = X(r.Right) - X(r.X), Height = Y(r.Bottom) - Y(r.Y), InsetX = X(r.InsetX), InsetY = Y(r.InsetY) }).ToArray() : doc.Rectangles;
        session.Edit(doc with { Width = width, Height = height, Rectangles = rects }); canvas.Fit();
    }

    private void ApplyLayout(string kind)
    {
        try
        {
            var doc = session.Draft.Document;
            var rects = kind switch
            {
                "124" => HotspotDocument.Trim(doc.Width, doc.Height, [1, 2, 4]),
                "1248" => HotspotDocument.Trim(doc.Width, doc.Height, [1, 2, 4, 8]),
                "24" => HotspotDocument.Trim(doc.Width, doc.Height, [2, 4], true),
                _ => HotspotDocument.Grid(doc.Width, doc.Height, (int)columns.Value, (int)rows.Value)
            };
            canvas.SelectIndices([]); session.Edit(doc with { Rectangles = rects });
        }
        catch (Exception ex) { KitStyle.ShowError(this, ex); }
    }

    private async Task DetectAsync()
    {
        var draft = session.Draft;
        if (!File.Exists(draft.Texture)) { issues.Text = "Open a texture before detecting regions."; return; }
        var min = (int)minimum.Value;
        await RunAsync(async token =>
        {
            var rects = await Task.Run(() =>
            {
                using var preview = TexturePreview.Load(draft.Texture, token);
                if (preview.Width != draft.Document.Width || preview.Height != draft.Document.Height)
                    throw new InvalidDataException("Restore the texture's original dimensions before detecting regions.");
                return RegionDetection.Detect(preview, min, draft.Snap ? draft.Grid : 0, token);
            }, token);
            if (IsDisposed) return;
            if (rects.Length == 0) { issues.Text = "No regions detected. Try a smaller minimum size."; return; }
            canvas.SelectIndices([]); session.Edit(draft.Document with { Rectangles = rects });
        });
    }

    private async Task SaveAsync(bool choosePath)
    {
        if (busy) return;
        var draft = session.Draft;
        var path = draft.RectPath;
        if (choosePath || string.IsNullOrWhiteSpace(path))
        {
            using var dialog = new SaveFileDialog { Filter = "Hotspot rectangle map|*.rect", DefaultExt = "rect", FileName = Path.GetFileName(path), InitialDirectory = Path.GetDirectoryName(path) ?? addon.Settings.Get("lastDirectory", "") };
            if (dialog.ShowDialog(this) != DialogResult.OK) return;
            path = dialog.FileName;
        }
        var losses = draft.Document.Losses(draft.Format, draft.WriteFlags);
        if (losses.Length > 0 && MessageBox.Show(this, string.Join(Environment.NewLine, losses) + "\n\nSave this format? The recovery draft keeps all flags.", "Format differences", MessageBoxButtons.OKCancel, MessageBoxIcon.Warning) != DialogResult.OK) return;
        var output = path;
        await RunAsync(async token =>
        {
            await Task.Run(async () =>
            {
                var text = HotspotFormats.Write(draft.Document, draft.Format, draft.WriteFlags);
                if (draft.Format == "source2" && File.Exists(draft.Material) && Path.GetFullPath(MaterialFiles.RectFor(draft.Material)).Equals(Path.GetFullPath(output), StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("Save the Source 2 export to a different path. This .rect is linked to a Source 1 material that requires Hammer++ format.");
                var materialText = draft.LinkMaterial && draft.Format == "hammerpp" ? MaterialFiles.LinkText(draft.Material, output) : null;
                await HotspotFormats.SaveAsync(output, text, true, token);
                if (materialText is not null) await HotspotFormats.SaveAsync(draft.Material, materialText, true, token);
            }, token);
            if (IsDisposed) return;
            session.Update(d => d with { RectPath = output, Dirty = false });
            addon.Host.Log.Write(LogLevel.Info, "hotspot", $"Saved {draft.Document.Rectangles.Length} rectangles to {output}");
        });
    }

    private async Task RunAsync(Func<CancellationToken, Task> work)
    {
        if (busy) return;
        Control focused = this;
        while (focused.Controls.Cast<Control>().FirstOrDefault(c => c.ContainsFocus) is { } child) focused = child;
        busy = true; Enabled = false; UseWaitCursor = true;
        try { await work(lifetime.Token); }
        catch (OperationCanceledException) { }
        catch (Exception ex)
        {
            addon.Host.Log.Write(LogLevel.Error, "hotspot", ex.Message);
            if (!IsDisposed) KitStyle.ShowError(this, ex);
        }
        finally
        {
            if (!IsDisposed)
            {
                busy = false; Enabled = true; UseWaitCursor = false;
                if (!focused.IsDisposed && focused.CanFocus) focused.Focus();
            }
        }
    }

    protected override bool ProcessCmdKey(ref Message msg, Keys keyData)
    {
        if (keyData == (Keys.Control | Keys.S)) { _ = SaveAsync(false); return true; }
        return base.ProcessCmdKey(ref msg, keyData);
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) { session.Changed -= Changed; lifetime.Cancel(); lifetime.Dispose(); }
        base.Dispose(disposing);
    }
}
