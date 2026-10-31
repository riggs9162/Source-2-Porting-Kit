using PortingKit.Core;

namespace PortingKit.Desktop;

internal sealed class SettingsPage : UserControl
{
    private readonly KitHost host;
    private readonly NumericUpDown workers = new() { Minimum = 1, Maximum = 4, Width = 180 };
    private readonly NumericUpDown cache = new() { Minimum = 16, Maximum = 65536, Width = 180, Increment = 128 };
    private readonly TextBox vrf = new() { Width = 560 };
    private readonly TextBox studiomdl = new() { Width = 560 };

    public SettingsPage(KitHost host)
    {
        this.host = host;
        Padding = new(24);
        AutoScroll = true;
        var layout = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, FlowDirection = FlowDirection.TopDown, WrapContents = false };
        layout.Controls.Add(KitStyle.Label("Settings", 22, true));
        var options = host.Settings.Get("runtime", host.Options);
        workers.Value = options.Workers;
        cache.Value = options.DiskCacheMegabytes;
        layout.Controls.Add(KitStyle.Label("Background workers", 10, true));
        layout.Controls.Add(workers);
        layout.Controls.Add(KitStyle.Label("Disk cache budget (MB)", 10, true));
        layout.Controls.Add(cache);
        layout.Controls.Add(KitStyle.Label("Worker and cache budget changes apply after restarting V4."));
        vrf.Text = host.Settings.Get("tools.vrf", "");
        studiomdl.Text = host.Settings.Get("tools.studiomdl", "");
        layout.Controls.Add(KitStyle.Label("Source2Viewer-CLI", 10, true));
        layout.Controls.Add(ProgramRow(vrf));
        layout.Controls.Add(KitStyle.Label("studiomdl", 10, true));
        layout.Controls.Add(ProgramRow(studiomdl));
        layout.Controls.Add(KitStyle.Label("Porting tools use these programs unless a tool names its own."));
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

    private Control ProgramRow(TextBox field)
    {
        var row = new FlowLayoutPanel { AutoSize = true, WrapContents = false };
        row.Controls.Add(field);
        row.Controls.Add(KitStyle.Button("Browse...", (_, _) =>
        {
            using var dialog = new OpenFileDialog { CheckFileExists = true, Filter = "Programs (*.exe)|*.exe" };
            if (dialog.ShowDialog(this) == DialogResult.OK)
                field.Text = dialog.FileName;
        }));
        return row;
    }

    private async void Save(object? sender, EventArgs e)
    {
        try
        {
            await host.Settings.SetAsync("runtime", new HostOptions((int)workers.Value, (int)cache.Value).Validate());
            await host.Settings.SetAsync("tools.vrf", vrf.Text.Trim());
            await host.Settings.SetAsync("tools.studiomdl", studiomdl.Text.Trim());
            MessageBox.Show(this, "Settings saved.", AppIdentity.Name);
        }
        catch (Exception exception)
        {
            KitStyle.ShowError(this, exception);
        }
    }
}
