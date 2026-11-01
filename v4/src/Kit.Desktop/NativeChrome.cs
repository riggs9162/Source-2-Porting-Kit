using System.Runtime.InteropServices;

namespace PortingKit.Desktop;

internal static class NativeChrome
{
    public static void Apply(Form form)
    {
        if (!form.IsHandleCreated)
            return;
        var dark = KitStyle.Palette.IsDark ? 1 : 0;
        DwmSetWindowAttribute(form.Handle, 20, ref dark, sizeof(int));
        var caption = ColorTranslator.ToWin32(KitStyle.Palette.Sidebar);
        DwmSetWindowAttribute(form.Handle, 35, ref caption, sizeof(int));
        var text = ColorTranslator.ToWin32(KitStyle.Text);
        DwmSetWindowAttribute(form.Handle, 36, ref text, sizeof(int));
    }

    /// <summary>Matches a control's native scrollbars to the active theme.</summary>
    public static void ThemeScrollbars(Control control)
    {
        if (control.IsHandleCreated)
            SetWindowTheme(control.Handle, KitStyle.Palette.IsDark ? "DarkMode_Explorer" : "Explorer", null);
    }

    [DllImport("uxtheme.dll", CharSet = CharSet.Unicode)]
    private static extern int SetWindowTheme(nint window, string application, string? identifiers);

    [DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(nint window, int attribute, ref int value, int size);
}
