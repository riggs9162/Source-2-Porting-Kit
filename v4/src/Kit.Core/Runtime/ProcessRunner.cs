using System.Diagnostics;
using System.Text;

namespace PortingKit.Core;

public sealed class ProcessRunner : IProcessRunner
{
    public async Task<ProcessResult> RunAsync(ProcessRequest request, Action<ProcessLine>? output = null,
        CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        using var timeout = new CancellationTokenSource(request.Timeout ?? Timeout.InfiniteTimeSpan);
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, timeout.Token);
        using var process = new Process();
        process.StartInfo = new ProcessStartInfo(request.Executable)
        {
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
            WorkingDirectory = request.WorkingDirectory ?? Environment.CurrentDirectory
        };
        foreach (var argument in request.Arguments)
            process.StartInfo.ArgumentList.Add(argument);
        var tail = new Queue<string>();
        var gate = new object();
        Exception? outputError = null;
        var watch = Stopwatch.StartNew();
        process.Start();
        using var cancel = linked.Token.Register(() =>
        {
            try
            {
                if (!process.HasExited)
                    process.Kill(entireProcessTree: true);
            }
            catch (InvalidOperationException) { }
            catch (System.ComponentModel.Win32Exception) { }
        });
        void Emit(bool isError, string text)
        {
            lock (gate)
            {
                tail.Enqueue(text);
                while (tail.Count > 100)
                    tail.Dequeue();
                try
                {
                    output?.Invoke(new(isError, text));
                }
                catch (Exception exception)
                {
                    outputError ??= exception;
                }
            }
        }
        var streams = Task.WhenAll(
            ReadAsync(process.StandardOutput, text => Emit(false, text)),
            ReadAsync(process.StandardError, text => Emit(true, text)));
        await process.WaitForExitAsync(CancellationToken.None);
        await streams;
        cancellationToken.ThrowIfCancellationRequested();
        if (timeout.IsCancellationRequested)
            throw new TimeoutException($"{Path.GetFileName(request.Executable)} exceeded its time limit.");
        if (outputError is not null)
            throw new IOException("The process output consumer failed.", outputError);
        return new(process.ExitCode, watch.Elapsed, tail.ToArray());
    }

    private static async Task ReadAsync(StreamReader reader, Action<string> emit)
    {
        var buffer = new char[4096];
        var line = new StringBuilder();
        int count;
        while ((count = await reader.ReadAsync(buffer)) > 0)
        {
            for (var index = 0; index < count; index++)
            {
                var character = buffer[index];
                if (character is '\n' or '\r' || line.Length >= 8192)
                {
                    if (line.Length > 0)
                        emit(line.ToString());
                    line.Clear();
                }
                if (character is not '\n' and not '\r')
                    line.Append(character);
            }
        }
        if (line.Length > 0)
            emit(line.ToString());
    }
}
