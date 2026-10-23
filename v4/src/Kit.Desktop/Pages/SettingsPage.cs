using PortingKit.Core;

namespace PortingKit.Desktop;

internal sealed class SettingsPage : UserControl
{
    private readonly KitHost host;
    private readonly Dictionary<string, TextBox> paths = [];
    private readonly NumericUpDown workers = new() { Minimum = 1, Maximum = 4, Width = 180 };
    private readonly NumericUpDown cache = new() { Minimum = 16, Maximum = 65536, Width = 180, Increment = 128 };

    public SettingsPage(KitHost host)
    {
        this.host = host;
        Padding = new(24);
        AutoScroll = true;
        var layout = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, FlowDirection = FlowDirection.TopDown, WrapContents = false };
        layout.Controls.Add(KitStyle.Label("Tool settings", 22, true));
        layout.Controls.Add(KitStyle.Label("External tools"));
        foreach (var (key, name) in new[] { ("vrf", "Source2Viewer-CLI"), ("studiomdl", "StudioMDL"), ("ffmpeg", "FFmpeg") })
        {
            layout.Controls.Add(KitStyle.Label(name, 10, true));
            var row = new FlowLayoutPanel { Width = 740, Height = 44 };
            var field = new TextBox { Width = 610, Text = host.Settings.Get("tools." + key, "") };
            paths.Add(key, field);
            row.Controls.Add(field);
            row.Controls.Add(KitStyle.Button("Browse...", (_, _) =>
            {
                using var dialog = new OpenFileDialog { Filter = "Executables|*.exe|All files|*.*", CheckFileExists = true };
                if (dialog.ShowDialog(this) == DialogResult.OK)
                    field.Text = dialog.FileName;
            }));
            layout.Controls.Add(row);
        }
        var options = host.Settings.Get("runtime", host.Options);
        workers.Value = options.Workers;
        cache.Value = options.DiskCacheMegabytes;
        layout.Controls.Add(KitStyle.Label("Background workers", 10, true));
        layout.Controls.Add(workers);
        layout.Controls.Add(KitStyle.Label("Disk cache budget (MB)", 10, true));
        layout.Controls.Add(cache);
        layout.Controls.Add(KitStyle.Label("Worker and cache budget changes apply after restarting V4."));
        var actions = new FlowLayoutPanel { Width = 740, Height = 48, Padding = new(0, 8, 0, 0) };
        actions.Controls.Add(KitStyle.Button("Save settings", Save));
        actions.Controls.Add(KitStyle.Button("Clear preview cache", async (_, _) =>
        {
            try
            {
                await host.Cache.ClearAsync();
                MessageBox.Show(this, "Unused cached previews were removed. Active previews were kept.", AppIdentity.Name);
            }
            catch (Exception exception)
            {
                KitStyle.ShowError(this, exception);
            }
        }));
        layout.Controls.Add(actions);
        Controls.Add(layout);
    }

    private async void Save(object? sender, EventArgs e)
    {
        try
        {
            foreach (var (key, field) in paths)
                await host.Settings.SetAsync("tools." + key, field.Text.Trim().Trim('"'));
            await host.Settings.SetAsync("runtime", new HostOptions((int)workers.Value, (int)cache.Value).Validate());
            MessageBox.Show(this, "Settings saved.", AppIdentity.Name);
        }
        catch (Exception exception)
        {
            KitStyle.ShowError(this, exception);
        }
    }
}
