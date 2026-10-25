using PortingKit.Core;

namespace PortingKit.Desktop;

internal sealed class SettingsPage : UserControl
{
    private readonly KitHost host;
    private readonly NumericUpDown workers = new() { Minimum = 1, Maximum = 4, Width = 180 };
    private readonly NumericUpDown cache = new() { Minimum = 16, Maximum = 65536, Width = 180, Increment = 128 };

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
            await host.Settings.SetAsync("runtime", new HostOptions((int)workers.Value, (int)cache.Value).Validate());
            MessageBox.Show(this, "Settings saved.", AppIdentity.Name);
        }
        catch (Exception exception)
        {
            KitStyle.ShowError(this, exception);
        }
    }
}
