using PortingKit.Core;

namespace PortingKit.Desktop;

internal sealed class OverviewPage : UserControl
{
    public OverviewPage(KitHost host)
    {
        Padding = new(24);
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 5 };
        layout.RowStyles.Add(new(SizeType.Absolute, 52));
        layout.RowStyles.Add(new(SizeType.Absolute, 76));
        layout.RowStyles.Add(new(SizeType.Absolute, 40));
        layout.RowStyles.Add(new(SizeType.Absolute, 180));
        layout.RowStyles.Add(new(SizeType.Percent, 100));
        layout.Controls.Add(KitStyle.Label("Your porting workspace", 22, true));
        var intro = KitStyle.Label("Browse assets, run tools, and keep track of your outputs.\nChoose an add-on workspace or a tool from the left to get started.");
        layout.Controls.Add(intro);
        layout.Controls.Add(KitStyle.Label($"{host.Addons.Snapshot().Count(addon => addon.State == AddonState.Loaded)} add-ons loaded   /   {host.Contributions.GetAll<ITool>().Count} tools   /   {host.Options.Workers} background worker", 11, true));
        var addons = KitStyle.Grid();
        addons.Columns.Add("name", "Add-on");
        addons.Columns.Add("status", "Status");
        addons.Columns.Add("description", "Description");
        addons.Columns[2].FillWeight = 220;
        foreach (var addon in host.Addons.Snapshot())
            addons.Rows.Add(addon.Name, addon.State.ToString(), addon.Error ?? addon.Description);
        layout.Controls.Add(addons);
        var log = new TextBox { Dock = DockStyle.Fill, Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical,
            BorderStyle = BorderStyle.None, Margin = new(0, 24, 0, 0), Font = new("Consolas", 10),
            Text = string.Join(Environment.NewLine, host.Log.Tail(30).Select(entry => $"{entry.Time:HH:mm:ss}  {entry.Level,-7}  {entry.Message}")) };
        layout.Controls.Add(log);
        Controls.Add(layout);
    }
}
