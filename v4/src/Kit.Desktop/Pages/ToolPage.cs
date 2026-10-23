using PortingKit.Core;

namespace PortingKit.Desktop;

internal sealed class ToolPage : UserControl
{
    private readonly KitHost host;
    private readonly Contribution<ITool> tool;
    private readonly Dictionary<string, Control> fields = [];
    private readonly Action showJobs;

    public ToolPage(KitHost host, Contribution<ITool> tool, Action showJobs)
    {
        this.host = host;
        this.tool = tool;
        this.showJobs = showJobs;
        Padding = new(24);
        var scroll = new Panel { Dock = DockStyle.Fill, AutoScroll = true };
        var layout = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 1, Padding = new(0, 0, 14, 24) };
        layout.Controls.Add(KitStyle.Label(tool.Value.Definition.Name, 22, true));
        layout.Controls.Add(KitStyle.Label(tool.Value.Definition.Description));
        var draft = host.Settings.Get<Dictionary<string, string>>("draft." + tool.Id, []);
        foreach (var parameter in tool.Value.Definition.Parameters)
        {
            var value = draft.GetValueOrDefault(parameter.Key, parameter.Default);
            layout.Controls.Add(KitStyle.Label(parameter.Label + (parameter.Required ? " *" : ""), 10, true));
            var row = new TableLayoutPanel { Width = 720, Height = 42, ColumnCount = 2, Margin = new(0, 0, 0, 12) };
            row.ColumnStyles.Add(new(SizeType.Percent, 100));
            row.ColumnStyles.Add(new(SizeType.Absolute, parameter.Kind is ParameterKind.File or ParameterKind.Directory ? 98 : 0));
            Control field = parameter.Kind switch
            {
                ParameterKind.Boolean => new CheckBox { Checked = bool.TryParse(value, out var check) && check, Text = "Enabled", AutoSize = true },
                ParameterKind.Integer => new NumericUpDown { Minimum = parameter.Minimum, Maximum = parameter.Maximum,
                    Value = int.TryParse(value, out var number) ? Math.Clamp(number, parameter.Minimum, parameter.Maximum) : parameter.Minimum },
                ParameterKind.Choice => new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList },
                _ => new TextBox { Text = value }
            };
            if (field is ComboBox combo)
            {
                combo.Items.AddRange(parameter.Choices ?? []);
                combo.SelectedItem = value;
                if (combo.SelectedIndex < 0 && combo.Items.Count > 0)
                    combo.SelectedIndex = 0;
            }
            field.Dock = DockStyle.Fill;
            fields[parameter.Key] = field;
            row.Controls.Add(field);
            if (parameter.Kind is ParameterKind.File or ParameterKind.Directory)
                row.Controls.Add(KitStyle.Button("Browse...", (_, _) => Browse(parameter, field)));
            layout.Controls.Add(row);
            if (parameter.Description.Length > 0)
                layout.Controls.Add(KitStyle.Label(parameter.Description));
        }
        var buttons = new FlowLayoutPanel { AutoSize = true, Margin = new(0, 12, 0, 0), Width = 720 };
        buttons.Controls.Add(KitStyle.Button("Run tool", Run));
        buttons.Controls.Add(KitStyle.Button("View jobs", (_, _) => showJobs()));
        layout.Controls.Add(buttons);
        layout.Controls.Add(KitStyle.Label("Outputs are saved in a separate folder for each job and remain available in Jobs."));
        scroll.Controls.Add(layout);
        Controls.Add(scroll);
    }

    private void Browse(ToolParameter parameter, Control field)
    {
        if (parameter.Kind == ParameterKind.Directory)
        {
            using var dialog = new FolderBrowserDialog();
            if (dialog.ShowDialog(this) == DialogResult.OK)
                field.Text = dialog.SelectedPath;
        }
        else
        {
            using var dialog = new OpenFileDialog { CheckFileExists = true };
            if (dialog.ShowDialog(this) == DialogResult.OK)
                field.Text = dialog.FileName;
        }
    }

    private async void Run(object? sender, EventArgs e)
    {
        try
        {
            var values = fields.ToDictionary(pair => pair.Key, pair => pair.Value switch
            {
                CheckBox check => check.Checked.ToString(),
                NumericUpDown number => number.Value.ToString(System.Globalization.CultureInfo.InvariantCulture),
                _ => pair.Value.Text
            });
            values = ToolValidation.Normalize(tool.Value.Definition, values).ToDictionary();
            await host.Settings.SetAsync("draft." + tool.Id, values);
            await host.Jobs.SubmitAsync(tool.Id, values);
            showJobs();
        }
        catch (Exception exception)
        {
            KitStyle.ShowError(this, exception);
        }
    }
}
