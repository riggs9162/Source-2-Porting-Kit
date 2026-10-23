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
    private static readonly Dictionary<(string Family, float Size, FontStyle Style), Font> Fonts = [];
    private static readonly System.Runtime.CompilerServices.ConditionalWeakTable<Control, FontSpec> OriginalFonts = new();
    private static readonly System.Runtime.CompilerServices.ConditionalWeakTable<ComboBox, ComboPainter> Combos = new();
    public static AppearanceOptions Appearance { get; private set; } = new();
    public static ThemePalette Palette { get; private set; } = ThemePalette.Create(new());
    public static event EventHandler? AppearanceChanged;
    public static Color Background => Palette.Background;
    public static Color Surface => Palette.Surface;
    public static Color Border => Palette.Border;
    public static Color Text => Palette.Text;
    public static Color Muted => Palette.Muted;
    public static Color Accent => Palette.Accent;
    public static int RowHeight => Scale(Appearance.Density == "Compact" ? 27 : 34);
    public static int ControlHeight => Scale(Appearance.Density == "Compact" ? 30 : 36);
    public static int Scale(int value) => (int)Math.Round(value * Appearance.TextScale / 100.0);

    public static void Configure(AppearanceOptions appearance)
    {
        Appearance = appearance.Normalize();
        Palette = ThemePalette.Create(Appearance);
        AppearanceChanged?.Invoke(null, EventArgs.Empty);
    }

    public static Font Font(float size = 10, bool bold = false, bool mono = false)
    {
        var key = (mono ? "Consolas" : Appearance.FontFamily, size * Appearance.TextScale / 100F,
            bold ? FontStyle.Bold : FontStyle.Regular);
        if (!Fonts.TryGetValue(key, out var font))
            Fonts.Add(key, font = new(key.Item1, key.Item2, key.Item3));
        return font;
    }

    public static void Apply(Control control)
    {
        control.BackColor = control switch
        {
            System.Windows.Forms.Label or CheckBox => Color.Transparent,
            TextBoxBase or ComboBox or NumericUpDown => Surface,
            _ => Background
        };
        control.ForeColor = control.Enabled ? Text : Muted;
        if (control is StyledLabel label)
        {
            control.Font = Font(label.PointSize, label.Bold, label.Monospace);
            control.ForeColor = label.Tone switch { LabelTone.Muted => Muted, LabelTone.Accent => Accent, _ => Text };
        }
        else
        {
            var specification = OriginalFonts.GetValue(control, item => new(10, item.Font.Bold,
                item.Font.Name.Contains("Consolas", StringComparison.OrdinalIgnoreCase)));
            control.Font = Font(control is Form or UserControl ? 10 : specification.Size, specification.Bold, specification.Mono);
        }
        if (control is Button button)
        {
            button.FlatStyle = FlatStyle.Flat;
            button.FlatAppearance.BorderColor = Border;
            button.FlatAppearance.MouseOverBackColor = Palette.Raised;
            button.FlatAppearance.MouseDownBackColor = Palette.Selection;
            button.UseVisualStyleBackColor = false;
            button.Padding = new Padding(8, 2, 8, 2);
        }
        if (control is ComboBox combo)
        {
            Combos.GetValue(combo, field => new ComboPainter(field));
            combo.DrawMode = DrawMode.OwnerDrawFixed;
            combo.FlatStyle = FlatStyle.Flat;
            combo.ItemHeight = Scale(22);
        }
        if (control is TextBox textBox && textBox.BorderStyle != BorderStyle.None)
            textBox.BorderStyle = BorderStyle.FixedSingle;
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
            grid.DefaultCellStyle.SelectionBackColor = Palette.Selection;
            grid.DefaultCellStyle.SelectionForeColor = Text;
            grid.AlternatingRowsDefaultCellStyle.BackColor = ThemePalette.Blend(Background, Surface, 0.4);
            grid.ColumnHeadersDefaultCellStyle.SelectionBackColor = Surface;
            grid.ColumnHeadersDefaultCellStyle.Font = Font(9, true);
            grid.CellBorderStyle = DataGridViewCellBorderStyle.SingleHorizontal;
            grid.ColumnHeadersBorderStyle = DataGridViewHeaderBorderStyle.None;
            grid.GridColor = Border;
            grid.RowHeadersVisible = false;
            grid.ColumnHeadersHeightSizeMode = DataGridViewColumnHeadersHeightSizeMode.DisableResizing;
            grid.ColumnHeadersHeight = Scale(36);
            grid.RowTemplate.Height = RowHeight;
            foreach (DataGridViewRow row in grid.Rows)
                row.Height = RowHeight;
        }
        if (control is MenuStrip menu)
            menu.Renderer = new ToolStripProfessionalRenderer(new ThemeMenuColors());
        foreach (Control child in control.Controls)
            Apply(child);
        control.Invalidate();
    }

    public static Button Button(string text, EventHandler action)
    {
        var button = new StyledButton { Text = text, AutoSize = true, MinimumSize = new(90, 34), Margin = new(0, 0, 8, 0) };
        button.Click += action;
        return button;
    }

    public static Button PrimaryButton(string text, EventHandler action)
    {
        var button = (StyledButton)Button(text, action);
        button.Primary = true;
        return button;
    }

    public static Label Label(string text, float size = 10, bool bold = false) => new StyledLabel(size, bold)
    {
        Text = text, AutoSize = true, MaximumSize = new(1000, 0),
        Font = Font(size, bold), ForeColor = Text,
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
        MessageBox.Show(owner, exception.Message, AppIdentity.Name, MessageBoxButtons.OK, MessageBoxIcon.Error);

    private sealed record FontSpec(float Size, bool Bold, bool Mono);

    private sealed class ComboPainter
    {
        public ComboPainter(ComboBox field) => field.DrawItem += (_, e) =>
        {
            var text = e.Index >= 0 ? field.GetItemText(field.Items[e.Index]) : field.Text;
            using var fill = new SolidBrush((e.State & DrawItemState.Selected) != 0 ? Palette.Selection : Palette.Surface);
            e.Graphics.FillRectangle(fill, e.Bounds);
            var bounds = Rectangle.Inflate(e.Bounds, -6, 0);
            TextRenderer.DrawText(e.Graphics, text, Font(), bounds, field.Enabled ? Text : Muted,
                TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.EndEllipsis);
            if ((e.State & DrawItemState.Focus) != 0)
                e.DrawFocusRectangle();
        };
    }

    private sealed class ThemeMenuColors : ProfessionalColorTable
    {
        public override Color MenuItemSelected => Palette.Selection;
        public override Color MenuItemBorder => Border;
        public override Color ToolStripDropDownBackground => Surface;
        public override Color ImageMarginGradientBegin => Surface;
        public override Color ImageMarginGradientMiddle => Surface;
        public override Color ImageMarginGradientEnd => Surface;
        public override Color MenuBorder => Border;
    }
}
