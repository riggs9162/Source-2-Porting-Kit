using PortingKit.Core;

namespace PortingKit.Desktop;

internal sealed class AddonsPage : UserControl
{
    private readonly KitHost host;
    private readonly DataGridView grid = KitStyle.Grid();
    private readonly IReadOnlyList<AddonInfo> addons;
    private bool updating = true;

    public AddonsPage(KitHost host)
    {
        this.host = host;
        addons = host.Addons.Snapshot();
        Padding = new(24);
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 4 };
        layout.RowStyles.Add(new(SizeType.Absolute, 52));
        layout.RowStyles.Add(new(SizeType.Absolute, 70));
        layout.RowStyles.Add(new(SizeType.Percent, 100));
        layout.RowStyles.Add(new(SizeType.Absolute, 48));
        layout.Controls.Add(KitStyle.Label("Add-ons", 22, true));
        layout.Controls.Add(KitStyle.Label("Each add-on lives in its own folder and is discovered automatically.\nEnable changes take effect the next time V4 starts."));
        grid.ReadOnly = false;
        grid.Columns.Add(new DataGridViewCheckBoxColumn { Name = "enabled", HeaderText = "Enabled", FillWeight = 45 });
        grid.Columns.Add("name", "Add-on");
        grid.Columns.Add("version", "Version");
        grid.Columns.Add("status", "Current status");
        grid.Columns.Add("description", "Description / error");
        grid.Columns[4].FillWeight = 230;
        foreach (DataGridViewColumn column in grid.Columns)
            column.ReadOnly = column.Index != 0;
        var enabled = host.Settings.Get<Dictionary<string, bool>>("addons", []);
        foreach (var addon in addons)
            grid.Rows.Add(enabled.GetValueOrDefault(addon.Id, true), addon.Name, addon.Version, addon.State, addon.Error ?? addon.Description);
        grid.CurrentCellDirtyStateChanged += (_, _) =>
        {
            if (grid.IsCurrentCellDirty)
                grid.CommitEdit(DataGridViewDataErrorContexts.Commit);
        };
        grid.CellValueChanged += SaveEnabled;
        updating = false;
        layout.Controls.Add(grid);
        var actions = new FlowLayoutPanel { Dock = DockStyle.Fill, Padding = new(0, 8, 0, 0) };
        actions.Controls.Add(KitStyle.Button("Open add-on folder", (_, _) =>
        {
            if (grid.CurrentRow is { } row)
                DesktopActions.Reveal(addons[row.Index].Directory);
        }));
        layout.Controls.Add(actions);
        Controls.Add(layout);
    }

    private async void SaveEnabled(object? sender, DataGridViewCellEventArgs e)
    {
        if (updating || e.RowIndex < 0 || e.ColumnIndex != 0)
            return;
        try
        {
            updating = true;
            grid.Enabled = false;
            await host.SetAddonEnabledAsync(addons[e.RowIndex].Id, (bool)(grid.Rows[e.RowIndex].Cells[0].Value ?? true));
        }
        catch (Exception exception)
        {
            KitStyle.ShowError(this, exception);
        }
        finally
        {
            updating = false;
            if (!IsDisposed)
                grid.Enabled = true;
        }
    }
}
