using PortingKit.Core;

namespace PortingKit.Desktop;

internal sealed class AppearancePage : UserControl
{
    private readonly KitHost host;
    private readonly List<ThemePreviewButton> themes = [];
    private readonly TextBox accent = new() { Width = 150, PlaceholderText = "Theme default", AccessibleName = "Accent color hex" };
    private readonly TextBox workspace = new() { Width = 280, MaxLength = 60, AccessibleName = "Personal workspace name" };
    private readonly ComboBox density = Choice(AppearanceOptions.Densities, "Density");
    private readonly ComboBox font = Choice(AppearanceOptions.Fonts, "Interface font");
    private readonly ComboBox scale = Choice(AppearanceOptions.Scales.Select(value => value + "%").ToArray(), "Text size");
    private readonly CheckBox memory = new() { Text = "Show RAM usage in the status bar", AutoSize = true };
    private readonly Label feedback = new StyledLabel(9) { AutoSize = true, Tone = LabelTone.Accent, Margin = new(0, 10, 0, 0) };
    private string selectedTheme;

    public AppearancePage(KitHost host)
    {
        this.host = host;
        var saved = KitStyle.Appearance;
        selectedTheme = saved.Theme;
        Padding = new(28, 24, 28, 24);
        AutoScroll = true;
        var layout = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 1, Padding = new(0, 0, 0, 24) };
        layout.Controls.Add(KitStyle.Label("Make it your workshop", 24, true));
        layout.Controls.Add(KitStyle.Label("Choose a palette, set your accent, and tune the workbench to how you work."));
        layout.Controls.Add(Section("COLOR PALETTE"));
        var previews = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, WrapContents = true, Margin = new(0, 2, 0, 20) };
        foreach (var name in AppearanceOptions.Themes)
        {
            var button = new ThemePreviewButton(name) { Text = name, AccessibleName = name + " color palette",
                Width = 180, Height = 126, Margin = new(0, 0, 12, 8), Selected = name == selectedTheme };
            button.Click += (_, _) =>
            {
                selectedTheme = name;
                foreach (var preview in themes)
                {
                    preview.Selected = preview.Theme == name;
                    preview.Invalidate();
                }
                feedback.Text = "Apply appearance to use the selected palette.";
            };
            themes.Add(button);
            previews.Controls.Add(button);
        }
        layout.Controls.Add(previews);
        layout.Controls.Add(Section("PERSONAL DETAILS"));
        var settings = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, WrapContents = true, Margin = Padding.Empty };
        var colorRow = new FlowLayoutPanel { AutoSize = true, WrapContents = false };
        colorRow.Controls.Add(accent);
        colorRow.Controls.Add(KitStyle.Button("Pick...", PickAccent));
        AddField(settings, "Accent color", colorRow);
        AddField(settings, "Workspace name", workspace);
        AddField(settings, "Interface font", font);
        AddField(settings, "Text size", scale);
        AddField(settings, "Spacing", density);
        layout.Controls.Add(settings);
        layout.Controls.Add(memory);
        var hint = new StyledLabel(9) { Text = "Leave the accent empty to use the palette's own color. Very low-contrast accents are adjusted for readability.",
            AutoSize = true, MaximumSize = new(680, 0), Tone = LabelTone.Muted, Margin = new(0, 12, 0, 18) };
        layout.Controls.Add(hint);
        var actions = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Top, Margin = new(0, 8, 0, 0) };
        actions.Controls.Add(KitStyle.PrimaryButton("Apply appearance", Save));
        actions.Controls.Add(KitStyle.Button("Reset choices", (_, _) => SetValues(new())));
        layout.Controls.Add(actions);
        layout.Controls.Add(feedback);
        Controls.Add(layout);
        SetValues(saved);
        void FitPage()
        {
            var width = Math.Max(300, ClientSize.Width - Padding.Horizontal - SystemInformation.VerticalScrollBarWidth);
            layout.MaximumSize = new(width, 0);
            previews.MaximumSize = settings.MaximumSize = new(width, 0);
            previews.Width = settings.Width = width;
            hint.MaximumSize = new(width, 0);
            foreach (Control child in layout.Controls)
                if (child is Label)
                    child.MaximumSize = new(width, 0);
        }
        ClientSizeChanged += (_, _) => FitPage();
        FitPage();
    }

    private static ComboBox Choice(string[] values, string name)
    {
        var field = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = 280, AccessibleName = name };
        field.Items.AddRange(values);
        return field;
    }

    private static Label Section(string text) => new StyledLabel(8, true)
    {
        Text = text, AutoSize = true, Tone = LabelTone.Muted, Margin = new(0, 20, 0, 12)
    };

    private static void AddField(Control parent, string label, Control field)
    {
        var group = new FlowLayoutPanel { FlowDirection = FlowDirection.TopDown, WrapContents = false,
            AutoSize = true, Width = 300, Margin = new(0, 0, 20, 12) };
        group.Controls.Add(KitStyle.Label(label, 9, true));
        group.Controls.Add(field);
        parent.Controls.Add(group);
    }

    private void SetValues(AppearanceOptions value)
    {
        value = value.Normalize();
        selectedTheme = value.Theme;
        accent.Text = value.AccentColor;
        workspace.Text = value.WorkspaceName;
        density.SelectedItem = value.Density;
        font.SelectedItem = value.FontFamily;
        scale.SelectedItem = value.TextScale + "%";
        memory.Checked = value.ShowMemory;
        foreach (var preview in themes)
        {
            preview.Selected = preview.Theme == selectedTheme;
            preview.Invalidate();
        }
        feedback.Text = "";
    }

    private void PickAccent(object? sender, EventArgs e)
    {
        using var picker = new ColorDialog { FullOpen = true,
            Color = AppearanceOptions.TryColor(accent.Text, out var color) ? color : KitStyle.Accent };
        if (picker.ShowDialog(this) == DialogResult.OK)
            accent.Text = $"#{picker.Color.R:X2}{picker.Color.G:X2}{picker.Color.B:X2}";
    }

    private async void Save(object? sender, EventArgs e)
    {
        try
        {
            var color = accent.Text.Trim();
            if (color.Length > 0 && !AppearanceOptions.TryColor(color, out _))
                throw new ArgumentException("Enter an accent in #RRGGBB format, or leave it empty for the theme default.");
            var appearance = new AppearanceOptions(selectedTheme, color, density.Text, font.Text,
                int.Parse(scale.Text.TrimEnd('%'), System.Globalization.CultureInfo.InvariantCulture), workspace.Text, memory.Checked).Normalize();
            await host.Settings.SetAsync("appearance", appearance);
            if (IsDisposed)
                return;
            KitStyle.Configure(appearance);
            feedback.Text = "Saved. Your appearance is applied and will be restored next time.";
        }
        catch (Exception exception)
        {
            if (!IsDisposed)
                KitStyle.ShowError(this, exception);
        }
    }
}
