using System.Globalization;
using PortingKit.Desktop;

namespace PortingKit.Addons.Soundscapes;

internal sealed class SoundscapePage(IAddonContext addon) : IDesktopPage
{
    public string Name => "Soundscape port";
    public string Category => "Sounds";
    public string Description => "Browse a game's soundscapes, listen to them, and port them with their sound events and sounds.";
    public Control Create(DesktopContext context) => new SoundscapeWorkspace(addon, context);
}

/// <summary>One rule of a ported soundscape, flattened for the preview list and the player.</summary>
/// <param name="Soundscape">The soundscape the rule belongs to, which differs from the selected one for nested rules.</param>
/// <param name="Loop">True for a rule that plays continuously.</param>
/// <param name="Waves">The Source 1 paths the rule plays or picks between.</param>
internal sealed record PreviewRule(string Soundscape, bool Loop, IReadOnlyList<string> Waves, double VolumeLow, double VolumeHigh, double TimeLow, double TimeHigh, string Placement)
{
    private const string SoundCharacters = "*#@<>^)}$!?&~";

    /// <summary>Lists the rules of a soundscape in a ported script, following the soundscapes it plays.</summary>
    public static List<PreviewRule> Flatten(KvBlock script, string name)
    {
        var rules = new List<PreviewRule>();
        Collect(script, name, 1, rules, new(StringComparer.OrdinalIgnoreCase));
        return rules;
    }

    public static string Clean(string wave) => wave.TrimStart(SoundCharacters.ToCharArray()).Replace('\\', '/');

    private static void Collect(KvBlock script, string name, double scale, List<PreviewRule> rules, HashSet<string> seen)
    {
        if (!seen.Add(name) || script.Block(name) is not { } body) return;
        foreach (var (key, value) in body.Pairs)
        {
            if (value is not KvBlock rule) continue;
            var (volumeLow, volumeHigh) = Pair(rule.Text("volume"), 1);
            var placement = rule.Text("origin") is { } origin ? $"at {origin}" : rule.Text("position") is { } position ? $"position {position}" : "everywhere";
            switch (key.ToLowerInvariant())
            {
                case "playlooping" when rule.Text("wave") is { } wave:
                    rules.Add(new(name, true, [Clean(wave)], volumeLow * scale, volumeHigh * scale, 0, 0, placement));
                    break;
                case "playrandom" when rule.Block("rndwave") is { } waves:
                    var (timeLow, timeHigh) = Pair(rule.Text("time"), 5);
                    rules.Add(new(name, false, [.. waves.Texts("wave").Select(Clean)], volumeLow * scale, volumeHigh * scale, timeLow, timeHigh, placement));
                    break;
                case "playsoundscape" when rule.Text("name") is { } nested:
                    Collect(script, nested, scale * volumeHigh, rules, seen);
                    break;
            }
        }
        seen.Remove(name);
    }

    private static (double Low, double High) Pair(string? text, double fallback)
    {
        var parts = (text ?? "").Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            .Select(part => double.TryParse(part, NumberStyles.Float, CultureInfo.InvariantCulture, out var value) ? value : fallback).ToArray();
        return parts.Length switch { 0 => (fallback, fallback), 1 => (parts[0], parts[0]), _ => (Math.Min(parts[0], parts[1]), Math.Max(parts[0], parts[1])) };
    }
}

internal sealed class SoundscapeWorkspace : UserControl
{
    private const string PortToolId = "soundscapes/port";
    private readonly IAddonContext addon;
    private readonly DesktopContext desktop;
    private readonly CancellationTokenSource lifetime = new();
    private readonly SoundPlayer player = new();
    private readonly Random random = new();
    private readonly System.Windows.Forms.Timer debounce = new() { Interval = 600 };
    private readonly List<System.Windows.Forms.Timer> schedules = [];
    private readonly HashSet<string> picked = new(StringComparer.OrdinalIgnoreCase);
    private readonly List<SoundscapeEntry> shown = [];
    private readonly TextBox sourceBox = new() { Anchor = AnchorStyles.Left | AnchorStyles.Right, PlaceholderText = "vpk:S:/SteamLibrary/steamapps/common/Half-Life Alyx/game/hlvr/pak01_dir.vpk:scripts/" };
    private readonly TextBox searchBox = new() { Anchor = AnchorStyles.Left | AnchorStyles.Right, PlaceholderText = "Filter by name or script" };
    private readonly DataGridView list = KitStyle.Grid();
    private readonly DataGridView rulesGrid = KitStyle.Grid();
    private readonly TextBox scriptBox = new() { Multiline = true, ReadOnly = true, Dock = DockStyle.Fill, ScrollBars = ScrollBars.Both, WordWrap = false };
    private readonly TextBox notesBox = new() { Multiline = true, ReadOnly = true, Dock = DockStyle.Fill, ScrollBars = ScrollBars.Vertical };
    private readonly TextBox outputBox = new() { Anchor = AnchorStyles.Left | AnchorStyles.Right };
    private readonly TextBox prefixBox = new() { Anchor = AnchorStyles.Left | AnchorStyles.Right };
    private readonly TextBox namePrefixBox = Field();
    private readonly TextBox eventPrefixBox = Field();
    private readonly TextBox scriptNameBox = Field();
    private readonly TextBox luaNameBox = Field();
    private readonly TextBox gmodBox = Field();
    private readonly TextBox eventsBox = new() { Multiline = true, AcceptsReturn = true, ScrollBars = ScrollBars.Vertical, Width = KitStyle.Scale(360), Height = KitStyle.Scale(80) };
    private readonly CheckBox foldCheck = new() { Text = "Fold surround beds to their front pair", AutoSize = true };
    private readonly CheckBox soundsCheck = new() { Text = "Port the sounds", AutoSize = true };
    private readonly CheckBox manifestCheck = new() { Text = "Merge into Garry's Mod's soundscape manifest", AutoSize = true };
    private readonly Label status = KitStyle.Label("Paste a source to list its soundscapes.", 9);
    private readonly Label playing = KitStyle.Label("", 9);
    private readonly Button portButton;
    private GameSource? source;
    private SoundscapeLibrary? library;
    private EventTable? table;
    private PortResult? preview;
    private List<PreviewRule> rules = [];
    private int loadVersion;
    private int previewVersion;
    private int playVersion;
    private bool restoring;
    private bool filling;
    private SoundscapeEntry? previewed;

    public SoundscapeWorkspace(IAddonContext addon, DesktopContext desktop)
    {
        this.addon = addon;
        this.desktop = desktop;
        Dock = DockStyle.Fill;
        Padding = new(12);
        portButton = KitStyle.PrimaryButton("Port ticked soundscapes", async (_, _) => await PortAsync());
        var root = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 6 };
        root.ColumnStyles.Add(new(SizeType.Percent, 100));
        foreach (var style in new[] { SizeType.AutoSize, SizeType.AutoSize, SizeType.AutoSize, SizeType.Percent, SizeType.AutoSize, SizeType.AutoSize })
            root.RowStyles.Add(style == SizeType.Percent ? new(style, 100) : new(style));
        root.Controls.Add(KitStyle.Label("Soundscape port", 19, true), 0, 0);
        root.Controls.Add(KitStyle.Label("Paste a game's pack, tick the soundscapes you want and port them. You get a Source 1 soundscape script, Lua definitions for every sound event, and the sounds under your own folder."), 0, 1);
        root.Controls.Add(SourceRow(), 0, 2);
        root.Controls.Add(Body(), 0, 3);
        root.Controls.Add(OutputRow(), 0, 4);
        root.Controls.Add(Footer(), 0, 5);
        Controls.Add(root);

        debounce.Tick += async (_, _) =>
        {
            debounce.Stop();
            await LoadSourceAsync();
        };
        sourceBox.TextChanged += (_, _) => Restart(debounce);
        searchBox.TextChanged += (_, _) => RefreshList();
        eventsBox.TextChanged += (_, _) => UpdatePortButton();
        list.CurrentCellChanged += async (_, _) => await SelectionChangedAsync();
        list.CellClick += (_, e) =>
        {
            if (e.ColumnIndex == 0 && e.RowIndex >= 0) Toggle([e.RowIndex]);
        };
        list.KeyDown += (_, e) =>
        {
            if (e.KeyCode != Keys.Space) return;
            Toggle(list.SelectedRows.Cast<DataGridViewRow>().Select(row => row.Index));
            e.Handled = true;
        };
        rulesGrid.CellDoubleClick += async (_, _) => await PlayRuleAsync();
        foldCheck.CheckedChanged += async (_, _) => await OptionChangedAsync();
        prefixBox.Leave += async (_, _) => await OptionChangedAsync();
        namePrefixBox.Leave += async (_, _) => await OptionChangedAsync();
        Restore();
    }

    private static TextBox Field() => new() { Width = KitStyle.Scale(360) };

    private static void Restart(System.Windows.Forms.Timer timer)
    {
        timer.Stop();
        timer.Start();
    }

    private Control SourceRow()
    {
        var row = Columns(SizeType.AutoSize, SizeType.Percent, SizeType.AutoSize);
        row.Controls.Add(Caption("Source"), 0, 0);
        row.Controls.Add(sourceBox, 1, 0);
        row.Controls.Add(KitStyle.Button("Browse...", (_, _) =>
        {
            using var dialog = new OpenFileDialog { Filter = "Game pack (*_dir.vpk)|*_dir.vpk|Soundscape script (*.txt)|*.txt", CheckFileExists = true };
            if (dialog.ShowDialog(this) == DialogResult.OK) sourceBox.Text = dialog.FileName.EndsWith(".vpk", StringComparison.OrdinalIgnoreCase) ? $"vpk:{dialog.FileName.Replace('\\', '/')}:scripts/" : dialog.FileName;
        }), 2, 0);
        return row;
    }

    private Control Body()
    {
        var body = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 1, Margin = new(0, 6, 0, 6) };
        body.ColumnStyles.Add(new(SizeType.Percent, 46));
        body.ColumnStyles.Add(new(SizeType.Percent, 54));
        body.RowStyles.Add(new(SizeType.Percent, 100));

        var left = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 2, Margin = new(0, 0, 8, 0) };
        left.ColumnStyles.Add(new(SizeType.Percent, 100));
        left.RowStyles.Add(new(SizeType.AutoSize));
        left.RowStyles.Add(new(SizeType.Percent, 100));
        var filter = Columns(SizeType.Percent, SizeType.AutoSize, SizeType.AutoSize);
        filter.Controls.Add(searchBox, 0, 0);
        filter.Controls.Add(KitStyle.Button("Tick shown", (_, _) => TickShown(true)), 1, 0);
        filter.Controls.Add(KitStyle.Button("Untick all", (_, _) => TickShown(false)), 2, 0);
        left.Controls.Add(filter, 0, 0);
        list.MultiSelect = true;
        list.Columns.Add(new DataGridViewCheckBoxColumn { Name = "port", HeaderText = "", FillWeight = 14 });
        list.Columns.Add(new DataGridViewTextBoxColumn { Name = "name", HeaderText = "Soundscape", FillWeight = 100 });
        list.Columns.Add(new DataGridViewTextBoxColumn { Name = "script", HeaderText = "Script", FillWeight = 60 });
        list.Columns.Add(new DataGridViewTextBoxColumn { Name = "kind", HeaderText = "Kind", FillWeight = 30 });
        list.Columns.Add(new DataGridViewTextBoxColumn { Name = "rules", HeaderText = "Rules", FillWeight = 22 });
        left.Controls.Add(list, 0, 1);
        body.Controls.Add(left, 0, 0);

        var right = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 2 };
        right.ColumnStyles.Add(new(SizeType.Percent, 100));
        right.RowStyles.Add(new(SizeType.AutoSize));
        right.RowStyles.Add(new(SizeType.Percent, 100));
        var listen = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, WrapContents = true, Margin = new(0, 0, 0, 4) };
        listen.Controls.Add(KitStyle.Button("Play soundscape", async (_, _) => await PlaySoundscapeAsync()));
        listen.Controls.Add(KitStyle.Button("Play sound", async (_, _) => await PlayRuleAsync()));
        listen.Controls.Add(KitStyle.Button("Stop", (_, _) => StopPlayback()));
        playing.Margin = new(4, 8, 0, 0);
        listen.Controls.Add(playing);
        right.Controls.Add(listen, 0, 0);
        var tabs = new TabControl { Dock = DockStyle.Fill };
        rulesGrid.Columns.Add("kind", "Plays");
        rulesGrid.Columns.Add("sound", "Sound");
        rulesGrid.Columns.Add("volume", "Volume");
        rulesGrid.Columns.Add("time", "Every");
        rulesGrid.Columns.Add("where", "Where");
        rulesGrid.Columns[0].FillWeight = 26;
        rulesGrid.Columns[1].FillWeight = 120;
        rulesGrid.Columns[2].FillWeight = 30;
        rulesGrid.Columns[3].FillWeight = 30;
        rulesGrid.Columns[4].FillWeight = 44;
        scriptBox.Font = KitStyle.Font(9, mono: true);
        tabs.TabPages.Add(Tab("Sounds", rulesGrid));
        tabs.TabPages.Add(Tab("Ported script", scriptBox));
        tabs.TabPages.Add(Tab("Notes", notesBox));
        tabs.TabPages.Add(Tab("Options", OptionsPanel()));
        right.Controls.Add(tabs, 0, 1);
        body.Controls.Add(right, 1, 0);
        return body;
    }

    private Control OptionsPanel()
    {
        var panel = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoScroll = true, FlowDirection = FlowDirection.TopDown, WrapContents = false, Padding = new(8) };
        void Add(string label, Control control, string help)
        {
            panel.Controls.Add(KitStyle.Label(label, 10, true));
            panel.Controls.Add(control);
            var caption = KitStyle.Label(help, 9);
            caption.MaximumSize = new(KitStyle.Scale(420), 0);
            panel.Controls.Add(caption);
        }
        Add("Script name", scriptNameBox, "Written as scripts/<name>.txt. Name it soundscapes_<map name> and that map loads it by itself.");
        Add("Lua file name", luaNameBox, "Written as lua/autorun/<name>.lua. It registers every sound event the soundscapes use.");
        Add("Soundscape name prefix", namePrefixBox, "Optional. Soundscape names are shared by the whole game, so a ported one replaces a stock one of the same name.");
        Add("Sound event name prefix", eventPrefixBox, "Optional. Put before every sound event name in the Lua definitions.");
        Add("Extra sound events", eventsBox, "Optional. Sound events to port although no ticked soundscape uses them, one per line. * matches any run of characters.");
        panel.Controls.Add(foldCheck);
        panel.Controls.Add(soundsCheck);
        panel.Controls.Add(manifestCheck);
        Add("Garry's Mod game folder", gmodBox, "The garrysmod folder. Only needed to merge the manifest, for scripts that are not named after a map.");
        return panel;
    }

    private Control OutputRow()
    {
        var row = Columns(SizeType.AutoSize, SizeType.Percent, SizeType.AutoSize, SizeType.AutoSize, SizeType.Percent);
        row.ColumnStyles[1].Width = 62;
        row.ColumnStyles[4].Width = 38;
        row.Controls.Add(Caption("Output folder"), 0, 0);
        row.Controls.Add(outputBox, 1, 0);
        row.Controls.Add(KitStyle.Button("Browse...", (_, _) =>
        {
            using var dialog = new FolderBrowserDialog();
            if (dialog.ShowDialog(this) == DialogResult.OK) outputBox.Text = dialog.SelectedPath;
        }), 2, 0);
        row.Controls.Add(Caption("Sound folder"), 3, 0);
        prefixBox.PlaceholderText = "my_name/hlvr";
        row.Controls.Add(prefixBox, 4, 0);
        return row;
    }

    private Control Footer()
    {
        var footer = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, WrapContents = true, Margin = new(0, 6, 0, 0) };
        footer.Controls.Add(portButton);
        footer.Controls.Add(KitStyle.Button("View jobs", (_, _) => desktop.OpenTool("jobs")));
        status.Margin = new(6, 8, 0, 0);
        footer.Controls.Add(status);
        return footer;
    }

    private static TableLayoutPanel Columns(params SizeType[] columns)
    {
        var row = new TableLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, ColumnCount = columns.Length, RowCount = 1, Margin = new(0, 2, 0, 2) };
        foreach (var column in columns) row.ColumnStyles.Add(column == SizeType.Percent ? new(column, 100) : new(column));
        row.RowStyles.Add(new(SizeType.AutoSize));
        return row;
    }

    private static Label Caption(string text)
    {
        var label = KitStyle.Label(text, 10, true);
        label.Margin = new(0, 6, 8, 0);
        return label;
    }

    private static TabPage Tab(string title, Control content)
    {
        var page = new TabPage(title);
        content.Dock = DockStyle.Fill;
        page.Controls.Add(content);
        return page;
    }

    private void Restore()
    {
        restoring = true;
        var draft = addon.Settings.Get<Dictionary<string, string>>("draft", []);
        string Saved(string key, string fallback) => draft.GetValueOrDefault(key, fallback);
        outputBox.Text = Saved("output", "");
        prefixBox.Text = Saved("sound_prefix", "ports");
        namePrefixBox.Text = Saved("name_prefix", "");
        eventPrefixBox.Text = Saved("event_prefix", "");
        scriptNameBox.Text = Saved("script_name", "soundscapes_ports");
        luaNameBox.Text = Saved("lua_name", "ports_sound_events");
        eventsBox.Text = Saved("events", "");
        gmodBox.Text = Saved("gmod", "");
        foldCheck.Checked = Saved("fold_surround", "True") != "False";
        soundsCheck.Checked = Saved("port_sounds", "True") != "False";
        manifestCheck.Checked = Saved("manifest", "none") == "merge";
        restoring = false;
        sourceBox.Text = Saved("source", "");
        UpdatePortButton();
    }

    private Dictionary<string, string> Arguments(string soundscapes) => new(StringComparer.Ordinal)
    {
        ["source"] = sourceBox.Text.Trim(),
        ["soundscapes"] = soundscapes,
        ["events"] = eventsBox.Text.Trim(),
        ["output"] = outputBox.Text.Trim(),
        ["sound_prefix"] = prefixBox.Text.Trim(),
        ["name_prefix"] = namePrefixBox.Text.Trim(),
        ["event_prefix"] = eventPrefixBox.Text.Trim(),
        ["script_name"] = scriptNameBox.Text.Trim(),
        ["lua_name"] = luaNameBox.Text.Trim(),
        ["fold_surround"] = foldCheck.Checked.ToString(),
        ["port_sounds"] = soundsCheck.Checked.ToString(),
        ["manifest"] = manifestCheck.Checked ? "merge" : "none",
        ["gmod"] = gmodBox.Text.Trim(),
        ["ffmpeg"] = "",
        ["vrf"] = ""
    };

    private PortOptions Options() => new(prefixBox.Text.Trim(), namePrefixBox.Text.Trim(), eventPrefixBox.Text.Trim(), foldCheck.Checked);

    private SoundscapeEntry? Selected => list.CurrentRow?.Tag as SoundscapeEntry;

    private async Task LoadSourceAsync()
    {
        var version = ++loadVersion;
        var text = sourceBox.Text.Trim();
        StopPlayback();
        source = null;
        library = null;
        table = null;
        picked.Clear();
        RefreshList();
        if (text.Length == 0)
        {
            status.Text = "Paste a source to list its soundscapes.";
            return;
        }
        status.Text = "Reading soundscapes...";
        try
        {
            var token = lifetime.Token;
            var (opened, read) = await Task.Run(() =>
            {
                var game = GameSource.Open(text);
                return (game, game.ReadLibrary());
            }, token);
            if (version != loadVersion || IsDisposed) return;
            source = opened;
            library = read;
            RefreshList();
            status.Text = $"{read.Entries.Count} soundscapes. Reading sound events...";
            await addon.Settings.SetAsync("draft", Arguments(""), token);
            var events = await Task.Run(() => opened.LoadEventsAsync(
                () => ExternalTools.Locate(null, addon.Host.Settings, "tools.vrf", "Source2Viewer-CLI.exe", "decompile sound events"),
                addon.Host.Processes, SoundscapePortTool.CacheDirectory(addon.Host), _ => { }, token), token);
            if (version != loadVersion || IsDisposed) return;
            table = events;
            status.Text = $"{read.Entries.Count} soundscapes and {events.Count} sound events. Tick the ones to port; select one to preview it.";
            if (read.Problems.Count > 0) status.Text += $" {read.Problems.Count} scripts could not be read.";
            await RefreshPreviewAsync();
        }
        catch (OperationCanceledException)
        {
            return;
        }
        catch (Exception exception)
        {
            if (version == loadVersion && !IsDisposed) status.Text = exception.Message;
        }
    }

    private void RefreshList()
    {
        var filter = searchBox.Text.Trim();
        shown.Clear();
        if (library is not null)
            shown.AddRange(library.Entries.Where(entry => filter.Length == 0 || entry.Name.Contains(filter, StringComparison.OrdinalIgnoreCase) || entry.File.Contains(filter, StringComparison.OrdinalIgnoreCase)));
        filling = true;
        list.Rows.Clear();
        foreach (var entry in shown) list.Rows[list.Rows.Add(picked.Contains(entry.Name), entry.Name, entry.File, entry.Kind, entry.Events + entry.Rules + entry.Nested)].Tag = entry;
        filling = false;
        UpdatePortButton();
        _ = RefreshPreviewAsync();
    }

    private void Toggle(IEnumerable<int> rows)
    {
        foreach (var row in rows.Where(index => index >= 0 && index < list.Rows.Count).Select(index => list.Rows[index]).ToList())
        {
            if (row.Tag is not SoundscapeEntry entry) continue;
            if (!picked.Remove(entry.Name)) picked.Add(entry.Name);
            row.Cells[0].Value = picked.Contains(entry.Name);
        }
        UpdatePortButton();
    }

    private void TickShown(bool tick)
    {
        if (!tick) picked.Clear();
        else picked.UnionWith(shown.Select(entry => entry.Name));
        foreach (DataGridViewRow row in list.Rows) row.Cells[0].Value = row.Tag is SoundscapeEntry entry && picked.Contains(entry.Name);
        UpdatePortButton();
    }

    private void UpdatePortButton()
    {
        portButton.Text = picked.Count == 1 ? "Port 1 soundscape" : $"Port {picked.Count} soundscapes";
        portButton.Enabled = picked.Count > 0 || eventsBox.Text.Trim().Length > 0;
    }

    private async Task OptionChangedAsync()
    {
        if (!restoring) await RefreshPreviewAsync();
    }

    private async Task SelectionChangedAsync()
    {
        if (filling || ReferenceEquals(Selected, previewed) && preview is not null) return;
        await RefreshPreviewAsync();
    }

    private async Task RefreshPreviewAsync()
    {
        var version = ++previewVersion;
        StopPlayback();
        previewed = Selected;
        preview = null;
        rules = [];
        rulesGrid.Rows.Clear();
        if (library is not { } read || source is not { } game || Selected is not { } entry)
        {
            scriptBox.Text = "";
            notesBox.Text = "";
            return;
        }
        if (table is not { } events)
        {
            scriptBox.Text = "Sound events are still being read.";
            return;
        }
        var options = Options();
        try
        {
            var (result, flat) = await Task.Run(() =>
            {
                var ported = new SoundscapePorter(read, events, options, game.HasSound).Port([entry.Name], []);
                return (ported, PreviewRule.Flatten(ported.Script, options.NamePrefix + entry.Name));
            }, lifetime.Token);
            if (version != previewVersion || IsDisposed) return;
            preview = result;
            rules = flat;
            scriptBox.Text = result.ScriptText.Replace("\n", "\r\n");
            notesBox.Text = result.Warnings.Count == 0 ? "Nothing was left out or changed." : string.Join("\r\n", result.Warnings);
            foreach (var rule in flat)
            {
                var sound = Path.GetFileName(rule.Waves[0]) + (rule.Waves.Count == 1 ? "" : $"  (+{rule.Waves.Count - 1} more)");
                var row = rulesGrid.Rows[rulesGrid.Rows.Add(rule.Loop ? "Loop" : "Random", sound, Range(rule.VolumeLow, rule.VolumeHigh), rule.Loop ? "" : Range(rule.TimeLow, rule.TimeHigh) + " s", rule.Placement)];
                row.Cells[1].ToolTipText = string.Join(Environment.NewLine, rule.Waves);
            }
        }
        catch (OperationCanceledException)
        {
            return;
        }
        catch (Exception exception)
        {
            if (version == previewVersion && !IsDisposed) notesBox.Text = exception.Message;
        }
    }

    private static string Range(double low, double high)
    {
        var from = low.ToString("0.##", CultureInfo.InvariantCulture);
        var to = high.ToString("0.##", CultureInfo.InvariantCulture);
        return from == to ? from : $"{from} - {to}";
    }

    /// <summary>Writes the playable files behind a set of waves, off the UI thread.</summary>
    /// <returns>The file for each wave the source holds. Stock Half-Life 2 sounds are not in the source and are left out.</returns>
    private async Task<Dictionary<string, string>> PreviewFilesAsync(IEnumerable<string> waves)
    {
        if (preview is not { } result || source is not { } game) return [];
        var folder = Path.Combine(SoundscapePortTool.CacheDirectory(addon.Host), "preview");
        var wanted = waves.Distinct(StringComparer.OrdinalIgnoreCase).Where(result.Waves.ContainsKey).ToList();
        var token = lifetime.Token;
        return await Task.Run(() =>
        {
            var files = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (var wave in wanted)
            {
                token.ThrowIfCancellationRequested();
                if (AudioPorter.WritePreview(game, result.Waves[wave], folder) is { } file) files[wave] = file;
            }
            return files;
        }, token);
    }

    private async Task PlayRuleAsync()
    {
        if (rulesGrid.CurrentRow is not { Index: >= 0 } row || row.Index >= rules.Count) return;
        var rule = rules[row.Index];
        var wave = rule.Waves[random.Next(rule.Waves.Count)];
        StopPlayback();
        var version = playVersion;
        try
        {
            var files = await PreviewFilesAsync([wave]);
            if (version != playVersion || IsDisposed) return;
            playing.Text = files.TryGetValue(wave, out var file) && player.Play(file, rule.VolumeHigh, false) ? $"Playing {wave}" : "This sound is not in the source, so it cannot be played here.";
        }
        catch (OperationCanceledException)
        {
            return;
        }
        catch (IOException exception)
        {
            playing.Text = exception.Message;
        }
    }

    private async Task PlaySoundscapeAsync()
    {
        if (rules.Count == 0) return;
        StopPlayback();
        var version = playVersion;
        var current = rules;
        playing.Text = "Reading sounds...";
        try
        {
            var files = await PreviewFilesAsync(current.SelectMany(rule => rule.Waves));
            if (version != playVersion || IsDisposed) return;
            var started = 0;
            foreach (var rule in current)
            {
                var available = rule.Waves.Where(files.ContainsKey).ToList();
                if (available.Count == 0) continue;
                started++;
                if (rule.Loop)
                {
                    player.Play(files[available[0]], rule.VolumeHigh, true);
                    continue;
                }
                var schedule = new System.Windows.Forms.Timer { Interval = Delay(rule) };
                schedule.Tick += (_, _) =>
                {
                    schedule.Interval = Delay(rule);
                    player.Play(files[available[random.Next(available.Count)]], rule.VolumeLow + random.NextDouble() * (rule.VolumeHigh - rule.VolumeLow), false);
                };
                schedules.Add(schedule);
                schedule.Start();
            }
            playing.Text = started == 0 ? "None of these sounds are in the source." : $"Playing {started} of {current.Count} rules";
        }
        catch (OperationCanceledException)
        {
            return;
        }
        catch (IOException exception)
        {
            playing.Text = exception.Message;
        }
    }

    private int Delay(PreviewRule rule) => (int)Math.Clamp((rule.TimeLow + random.NextDouble() * (rule.TimeHigh - rule.TimeLow)) * 1000, 100, 600_000);

    private void StopPlayback()
    {
        playVersion++;
        foreach (var schedule in schedules) schedule.Dispose();
        schedules.Clear();
        player.StopAll();
        playing.Text = "";
    }

    private async Task PortAsync()
    {
        try
        {
            if (outputBox.Text.Trim().Length == 0) throw new InvalidDataException("Choose an output folder.");
            var names = library is not null && picked.Count == library.Entries.Count && picked.Count > 0 ? "*" : string.Join("\n", library?.Entries.Select(entry => entry.Name).Where(picked.Contains) ?? []);
            var arguments = Arguments(names);
            await addon.Settings.SetAsync("draft", Arguments(""));
            await addon.Host.Jobs.SubmitAsync(PortToolId, arguments);
            desktop.OpenTool("jobs");
        }
        catch (Exception exception)
        {
            KitStyle.ShowError(this, exception);
        }
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            lifetime.Cancel();
            debounce.Dispose();
            foreach (var schedule in schedules) schedule.Dispose();
            schedules.Clear();
            player.Dispose();
            lifetime.Dispose();
        }
        base.Dispose(disposing);
    }
}
