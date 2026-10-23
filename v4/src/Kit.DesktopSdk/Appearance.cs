using System.Globalization;

namespace PortingKit.Desktop;

public sealed record AppearanceOptions(
    string Theme = "Graphite",
    string AccentColor = "",
    string Density = "Comfortable",
    string FontFamily = "Segoe UI",
    int TextScale = 100,
    string WorkspaceName = "My workshop",
    bool ShowMemory = true)
{
    public static readonly string[] Themes = ["Graphite", "Blueprint", "Paper"];
    public static readonly string[] Densities = ["Comfortable", "Compact"];
    public static readonly string[] Fonts = ["Segoe UI", "Tahoma", "Bahnschrift"];
    public static readonly int[] Scales = [100, 110, 125];

    public AppearanceOptions Normalize() => this with
    {
        Theme = Themes.Contains(Theme) ? Theme : "Graphite",
        AccentColor = TryColor(AccentColor, out _) ? AccentColor.ToUpperInvariant() : "",
        Density = Densities.Contains(Density) ? Density : "Comfortable",
        FontFamily = Fonts.Contains(FontFamily) ? FontFamily : "Segoe UI",
        TextScale = Scales.Contains(TextScale) ? TextScale : 100,
        WorkspaceName = string.IsNullOrWhiteSpace(WorkspaceName) ? "My workshop" : WorkspaceName.Trim()[..Math.Min(60, WorkspaceName.Trim().Length)]
    };

    public static bool TryColor(string? text, out Color color)
    {
        color = Color.Empty;
        if (text is not { Length: 7 } || text[0] != '#' ||
            !int.TryParse(text.AsSpan(1), NumberStyles.HexNumber, CultureInfo.InvariantCulture, out var rgb))
            return false;
        color = Color.FromArgb((rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255);
        return true;
    }
}

public sealed record ThemePalette(
    Color Background,
    Color Surface,
    Color Raised,
    Color Sidebar,
    Color Border,
    Color Text,
    Color Muted,
    Color Accent,
    Color Selection,
    Color OnAccent,
    Color Success,
    Color Error,
    bool IsDark)
{
    public static ThemePalette Create(AppearanceOptions appearance)
    {
        appearance = appearance.Normalize();
        var palette = appearance.Theme switch
        {
            "Blueprint" => new ThemePalette(C("141E2A"), C("1B2938"), C("243549"), C("101924"),
                C("35485F"), C("E8EFF6"), C("9BAFC4"), C("76CFE1"), Color.Empty, Color.Empty, C("8FCBA9"), C("EF9292"), true),
            "Paper" => new ThemePalette(C("F3F0EA"), C("FCFAF6"), C("E8E3D9"), C("E7E2D8"),
                C("C9C3B8"), C("252C31"), C("5D666A"), C("A44C23"), Color.Empty, Color.Empty, C("2D7055"), C("A93137"), false),
            _ => new ThemePalette(C("202224"), C("282B2E"), C("32363A"), C("191C1E"),
                C("42474C"), C("F0EDE7"), C("ACAFAF"), C("E7A16A"), Color.Empty, Color.Empty, C("A0C7A4"), C("E9948B"), true)
        };
        var requested = AppearanceOptions.TryColor(appearance.AccentColor, out var custom) ? custom : palette.Accent;
        var accent = EnsureContrast(requested, palette.Surface, 3);
        var onAccent = Contrast(accent, Color.Black) >= Contrast(accent, Color.White) ? Color.Black : Color.White;
        return palette with { Accent = accent, OnAccent = onAccent, Selection = Blend(palette.Surface, accent, palette.IsDark ? 0.19 : 0.15) };
    }

    public static Color Blend(Color first, Color second, double amount) => Color.FromArgb(
        (int)Math.Round(first.R * (1 - amount) + second.R * amount),
        (int)Math.Round(first.G * (1 - amount) + second.G * amount),
        (int)Math.Round(first.B * (1 - amount) + second.B * amount));

    public static double Contrast(Color first, Color second)
    {
        var a = Luminance(first);
        var b = Luminance(second);
        return (Math.Max(a, b) + 0.05) / (Math.Min(a, b) + 0.05);
    }

    private static double Luminance(Color color)
    {
        static double Channel(byte channel)
        {
            var value = channel / 255.0;
            return value <= 0.04045 ? value / 12.92 : Math.Pow((value + 0.055) / 1.055, 2.4);
        }
        return Channel(color.R) * 0.2126 + Channel(color.G) * 0.7152 + Channel(color.B) * 0.0722;
    }

    private static Color EnsureContrast(Color accent, Color background, double minimum)
    {
        var target = Luminance(background) < 0.5 ? Color.White : Color.Black;
        for (var index = 0; index < 20 && Contrast(accent, background) < minimum; index++)
            accent = Blend(accent, target, 0.12);
        return accent;
    }

    private static Color C(string hex) => ColorTranslator.FromHtml("#" + hex);
}
