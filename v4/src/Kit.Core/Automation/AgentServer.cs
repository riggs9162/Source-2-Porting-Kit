using System.IO.Pipes;
using System.Text.Json;

namespace PortingKit.Core.Automation;

/// <summary>Serves same-user local requests against the single owner of a state directory.</summary>
public sealed class AgentServer : IAsyncDisposable
{
    private readonly KitHost host;
    private readonly AgentApi api;
    private readonly CancellationTokenSource stopping = new();
    private readonly SemaphoreSlim clients = new(8);
    private readonly List<Task> requests = [];
    private readonly Task listener;

    public AgentServer(KitHost host)
    {
        this.host = host;
        api = new(host);
        listener = ListenAsync();
    }

    private async Task ListenAsync()
    {
        try
        {
            while (!stopping.IsCancellationRequested)
            {
                await clients.WaitAsync(stopping.Token);
                NamedPipeServerStream? pipe = null;
                try
                {
                    pipe = new(AgentProtocol.PipeName(host.StateDirectory), PipeDirection.InOut, 8,
                        PipeTransmissionMode.Byte, PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
                    await pipe.WaitForConnectionAsync(stopping.Token);
                    requests.RemoveAll(task => task.IsCompleted);
                    requests.Add(HandleAsync(pipe));
                    pipe = null;
                }
                catch
                {
                    pipe?.Dispose();
                    clients.Release();
                    throw;
                }
            }
        }
        catch (OperationCanceledException) when (stopping.IsCancellationRequested) { }
        catch (Exception exception)
        {
            host.Log.Write(LogLevel.Error, "agents", "Agent listener stopped: " + exception.Message);
        }
    }

    private async Task HandleAsync(NamedPipeServerStream pipe)
    {
        await using (pipe)
        using (var deadline = CancellationTokenSource.CreateLinkedTokenSource(stopping.Token))
        {
            deadline.CancelAfter(TimeSpan.FromSeconds(45));
            var stopHost = false;
            AgentResponse response;
            try
            {
                var request = await AgentProtocol.ReadAsync<AgentRequest>(pipe, deadline.Token);
                if (request.Root is null || AgentProtocol.CanonicalPath(request.Root) != AgentProtocol.CanonicalPath(host.RootDirectory))
                    throw new ArgumentException("This state directory belongs to a different V4 root.");
                var result = await api.InvokeAsync(request.Operation, request.Arguments, deadline.Token);
                response = new(AgentProtocol.Element(result));
                stopHost = request.Operation == "host.stop";
            }
            catch (Exception exception)
            {
                response = new(Error: Error(exception));
            }
            try
            {
                try { await AgentProtocol.WriteAsync(pipe, response, deadline.Token); }
                catch (AgentException exception) { await AgentProtocol.WriteAsync(pipe, new AgentResponse(Error: Error(exception)), deadline.Token); }
            }
            catch (Exception exception) when (exception is IOException or OperationCanceledException or ObjectDisposedException) { }
            finally
            {
                if (stopHost) host.RequestHeadlessStop();
                clients.Release();
            }
        }
    }

    public static AgentError Error(Exception exception) => new(exception switch
    {
        AgentException agent => agent.Code,
        ArgumentException or JsonException or FormatException or OverflowException => "invalid_arguments",
        KeyNotFoundException or FileNotFoundException or DirectoryNotFoundException => "not_found",
        OperationCanceledException => "cancelled",
        _ => "operation_failed"
    }, exception.Message);

    public async ValueTask DisposeAsync()
    {
        stopping.Cancel();
        await listener;
        await Task.WhenAll(requests);
        stopping.Dispose();
        clients.Dispose();
    }
}
