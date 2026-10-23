using System.Diagnostics;
using System.IO.Pipes;
using System.Text.Json;

namespace PortingKit.Core.Automation;

/// <summary>Connects to an existing host or starts a detached headless owner; never retries a sent mutation.</summary>
public sealed class AgentClient(string rootDirectory, string? stateDirectory = null, bool autoStart = true)
{
    public string RootDirectory { get; } = Path.GetFullPath(rootDirectory);
    public string StateDirectory { get; } = Path.GetFullPath(stateDirectory ?? Path.Combine(rootDirectory, ".state"));

    public async Task<JsonElement> InvokeAsync(string operation, object? arguments = null, CancellationToken cancellationToken = default)
    {
        await using var pipe = await ConnectAsync(cancellationToken);
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        deadline.CancelAfter(TimeSpan.FromSeconds(50));
        await AgentProtocol.WriteAsync(pipe, new AgentRequest(RootDirectory, operation, AgentProtocol.Element(arguments ?? new { })), deadline.Token);
        var response = await AgentProtocol.ReadAsync<AgentResponse>(pipe, deadline.Token);
        if (response.Error is { } error) throw new AgentException(error.Code, error.Message);
        return response.Result ?? AgentProtocol.Element(null);
    }

    private async Task<NamedPipeClientStream> ConnectAsync(CancellationToken cancellationToken)
    {
        if (await TryConnectAsync(cancellationToken) is { } existing) return existing;
        if (!autoStart) throw new AgentException("backend_unavailable", "No Source Backport backend is running for this state directory.");
        Directory.CreateDirectory(StateDirectory);
        FileStream? startup = null;
        var clock = Stopwatch.StartNew();
        while (startup is null)
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (await TryConnectAsync(cancellationToken) is { } running) return running;
            try { startup = new(Path.Combine(StateDirectory, "agent-start.lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None); }
            catch (IOException)
            {
                if (clock.Elapsed > TimeSpan.FromSeconds(30)) throw new AgentException("backend_unavailable", "Timed out waiting for backend startup.");
                await Task.Delay(100, cancellationToken);
            }
        }
        await using (startup)
        {
            if (await TryConnectAsync(cancellationToken) is { } started) return started;
            try
            {
                using var stateProbe = new FileStream(Path.Combine(StateDirectory, "host.lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
            }
            catch (IOException)
            {
                throw new AgentException("backend_unavailable", "The state directory is in use but its agent endpoint is unavailable. Restart the older desktop/backend build, or use a separate --state directory.");
            }
            var executable = FindCli();
            var start = new ProcessStartInfo(executable) { UseShellExecute = true, WindowStyle = ProcessWindowStyle.Hidden, WorkingDirectory = RootDirectory };
            foreach (var token in new[] { "--root", RootDirectory, "--state", StateDirectory, "serve", "--background" })
                start.ArgumentList.Add(token);
            using var process = Process.Start(start) ?? throw new IOException("Cannot start the headless backend.");
            clock.Restart();
            while (clock.Elapsed < TimeSpan.FromSeconds(30))
            {
                if (await TryConnectAsync(cancellationToken) is { } ready) return ready;
                if (process.HasExited)
                    throw new AgentException("backend_unavailable", $"The backend exited with code {process.ExitCode}. See {Path.Combine(StateDirectory, "agent-startup-error.txt")}.");
                await Task.Delay(100, cancellationToken);
            }
            throw new AgentException("backend_unavailable", "Backend startup exceeded 30 seconds. Inspect the state directory's logs before retrying.");
        }
    }

    private string FindCli()
    {
        var configuration = AppContext.BaseDirectory.Split(Path.DirectorySeparatorChar, StringSplitOptions.RemoveEmptyEntries)
            .LastOrDefault(part => part is "Debug" or "Release") ?? "Release";
        var local = Path.Combine(AppContext.BaseDirectory, "s2kit.exe");
        if (File.Exists(local)) return local;
        foreach (var root in new[] { FindRoot(), RootDirectory }.Distinct(StringComparer.OrdinalIgnoreCase))
        {
            var project = Path.Combine(root, "src", "Kit.Cli", "bin", configuration, "net10.0-windows", "s2kit.exe");
            if (File.Exists(project)) return project;
        }
        throw new FileNotFoundException("Build V4 before starting agent access. The headless CLI executable is missing.");
    }

    private async Task<NamedPipeClientStream?> TryConnectAsync(CancellationToken cancellationToken)
    {
        var pipe = new NamedPipeClientStream(".", AgentProtocol.PipeName(StateDirectory), PipeDirection.InOut,
            PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        deadline.CancelAfter(150);
        try
        {
            await pipe.ConnectAsync(deadline.Token);
            return pipe;
        }
        catch (Exception exception) when (exception is TimeoutException or OperationCanceledException or IOException)
        {
            await pipe.DisposeAsync();
            cancellationToken.ThrowIfCancellationRequested();
            return null;
        }
    }

    public static string FindRoot()
    {
        for (var directory = new DirectoryInfo(AppContext.BaseDirectory); directory is not null; directory = directory.Parent)
            if (Directory.Exists(Path.Combine(directory.FullName, "addons"))) return directory.FullName;
        return Environment.CurrentDirectory;
    }

    public static async Task ReleaseIdleHeadlessAsync(string root, string? state)
    {
        var client = new AgentClient(root, state, false);
        JsonElement status;
        try { status = await client.InvokeAsync("status"); }
        catch (AgentException exception) when (exception.Code == "backend_unavailable") { return; }
        if (status.GetProperty("hostKind").GetString() != "headless") return;
        await client.InvokeAsync("host.stop");
        for (var attempt = 0; attempt < 50; attempt++)
        {
            try
            {
                using var probe = new FileStream(Path.Combine(client.StateDirectory, "host.lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
                return;
            }
            catch (IOException) { await Task.Delay(100); }
        }
        throw new IOException("The idle headless backend has not released its state directory yet.");
    }
}
