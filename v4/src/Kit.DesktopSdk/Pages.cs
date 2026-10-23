namespace PortingKit.Desktop;

/// <summary>Creates a workspace only when selected; the host disposes it when navigating away.</summary>
public interface IDesktopPage
{
    string Name { get; }
    string Category { get; }
    string Description { get; }
    Control Create(DesktopContext context);
}

public sealed record DesktopContext(IHostServices Host, Action<string> OpenTool);

/// <summary>Shared desktop styles and controls available to add-on-owned pages.</summary>
public static class KitStyle
{
    private static readonly Font BodyFont = new("Segoe UI", 10F);
    public static Color Background => Color.FromArgb(23, 27, 31);
    public static Color Surface => Color.FromArgb(31, 36, 41);
    public static Color Border => Color.FromArgb(61, 69, 76);
    public static Color Text => Color.FromArgb(226, 232, 235);
    public static Color Muted => Color.FromArgb(157, 170, 179);
    public static Color Accent => Color.FromArgb(102, 210, 215);

    public static void Apply(Control control)
    {
        control.BackColor = control is TextBoxBase or ComboBox or NumericUpDown ? Background : Surface;
        control.ForeColor = Text;
        if (control.Parent is null || control is Form)
            control.Font = BodyFont;
        if (control is Button button)
        {
            button.FlatStyle = FlatStyle.Flat;
            button.FlatAppearance.BorderColor = Border;
            button.Padding = new Padding(8, 2, 8, 2);
        }
        if (control is DataGridView grid)
        {
            grid.BackgroundColor = Background;
            grid.BorderStyle = BorderStyle.None;
            grid.EnableHeadersVisualStyles = false;
            grid.ColumnHeadersDefaultCellStyle.BackColor = Surface;
            grid.ColumnHeadersDefaultCellStyle.ForeColor = Text;
            grid.ColumnHeadersDefaultCellStyle.Padding = new Padding(6);
            grid.DefaultCellStyle.BackColor = Background;
            grid.DefaultCellStyle.ForeColor = Text;
            grid.DefaultCellStyle.SelectionBackColor = Color.FromArgb(42, 85, 91);
            grid.DefaultCellStyle.SelectionForeColor = Text;
            grid.GridColor = Border;
            grid.RowHeadersVisible = false;
            grid.ColumnHeadersHeightSizeMode = DataGridViewColumnHeadersHeightSizeMode.DisableResizing;
            grid.ColumnHeadersHeight = 36;
            grid.RowTemplate.Height = 30;
        }
        foreach (Control child in control.Controls)
            Apply(child);
    }

    public static Button Button(string text, EventHandler action)
    {
        var button = new Button { Text = text, AutoSize = true, MinimumSize = new(90, 34), Margin = new(0, 0, 8, 0) };
        button.Click += action;
        return button;
    }

    public static Label Label(string text, float size = 10, bool bold = false) => new()
    {
        Text = text, AutoSize = true, MaximumSize = new(1000, 0),
        Font = new("Segoe UI", size, bold ? FontStyle.Bold : FontStyle.Regular), ForeColor = Text,
        Margin = new(0, 0, 0, 12)
    };

    public static DataGridView Grid() => new()
    {
        Dock = DockStyle.Fill, ReadOnly = true, AllowUserToAddRows = false,
        AllowUserToDeleteRows = false, AutoSizeColumnsMode = DataGridViewAutoSizeColumnsMode.Fill,
        SelectionMode = DataGridViewSelectionMode.FullRowSelect, MultiSelect = false,
        AutoGenerateColumns = false
    };

    public static void ShowError(Control owner, Exception exception) =>
        MessageBox.Show(owner, exception.Message, "Source 2 Porting Kit", MessageBoxButtons.OK, MessageBoxIcon.Error);
}
