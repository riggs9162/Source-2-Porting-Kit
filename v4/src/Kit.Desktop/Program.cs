using PortingKit.Core;
using PortingKit.Core.Automation;

namespace PortingKit.Desktop;

internal static class Program
{
    [STAThread]
    private static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        var root = Argument(args, "--root") ?? FindRoot();
        var state = Argument(args, "--state");
        using var instance = new Mutex(true, "Local\\S2KitV4-" + Convert.ToHexString(
            System.Security.Cryptography.SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(Path.GetFullPath(state ?? Path.Combine(root, ".state")).ToUpperInvariant())))[..20],
            out var first);
        if (!first)
        {
            MessageBox.Show("Source Backport is already running with this settings directory.", AppIdentity.Name);
            return;
        }
        KitHost? host = null;
        try
        {
            host = Task.Run(async () =>
            {
                await AgentClient.ReleaseIdleHeadlessAsync(root, state);
                return await KitHost.OpenAsync(root, state, hostKind: "desktop");
            }).GetAwaiter().GetResult();
            Application.ThreadException += (_, e) =>
            {
                host.Log.Write(LogLevel.Error, "desktop", e.Exception.ToString());
                MessageBox.Show(e.Exception.Message, AppIdentity.Name, MessageBoxButtons.OK, MessageBoxIcon.Error);
            };
            Application.Run(new MainWindow(host));
        }
        catch (Exception exception)
        {
            MessageBox.Show(exception.Message, AppIdentity.Name + " could not start", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
        finally
        {
            if (host is not null)
                Task.Run(async () => await host.DisposeAsync()).GetAwaiter().GetResult();
        }
    }

    private static string? Argument(string[] args, string name)
    {
        var index = Array.IndexOf(args, name);
        return index >= 0 && index + 1 < args.Length ? args[index + 1] : null;
    }

    private static string FindRoot()
    {
        for (var directory = new DirectoryInfo(AppContext.BaseDirectory); directory is not null; directory = directory.Parent)
            if (Directory.Exists(Path.Combine(directory.FullName, "addons")))
                return directory.FullName;
        return Environment.CurrentDirectory;
    }
}
