using System.Text.Json;
using PortingKit.Core;
using PortingKit.Core.Automation;

namespace PortingKit.Cli;

internal static class CliApplication
{
    public static async Task<int> RunAsync(string[] args)
    {
        string? state = null;
        var background = args.Contains("--background");
        try
        {
            var tokens = args.ToList();
            var root = AgentClient.FindRoot();
            while (tokens.Count > 0 && tokens[0] is "--root" or "--state")
            {
                var key = Shift(tokens);
                var value = Shift(tokens);
                if (key == "--root") root = Path.GetFullPath(value);
                else state = Path.GetFullPath(value);
            }
            state ??= Path.Combine(root, ".state");
            var command = tokens.Count > 0 ? Shift(tokens) : "help";
            if (command is "help" or "--help" or "-h")
            {
                Console.WriteLine(Help);
                return 0;
            }
            if (command == "serve")
            {
                if (tokens.Count > 1 || tokens.Count == 1 && tokens[0] != "--background")
                    throw new ArgumentException("serve accepts only --background.");
                if (background)
                {
                    Console.SetOut(TextWriter.Null);
                    Console.SetError(TextWriter.Null);
                }
                return await ServeAsync(root, state);
            }
            var client = new AgentClient(root, state, !(command == "server" && tokens.FirstOrDefault() == "stop"));
            if (command == "call")
            {
                var operation = Shift(tokens);
                Print(await client.InvokeAsync(operation, await JsonInputAsync(tokens)));
                return 0;
            }
            if (command == "operations")
            {
                if (tokens.Count != 0) throw new ArgumentException("operations accepts no arguments.");
                Print(await client.InvokeAsync("operations"));
                return 0;
            }
            if (command is "run" or "submit" or "validate" or "doctor")
            {
                var tool = command == "doctor" ? "utilities/environment" : Shift(tokens);
                var arguments = tokens.Count > 0 && tokens[0] is "--json" or "--json-file"
                    ? await JsonInputAsync(tokens) : AgentProtocol.Element(ReadPairs(tokens));
                var operation = command == "validate" ? "tools.validate" : "jobs.submit";
                var result = await client.InvokeAsync(operation, new { tool_id = tool, arguments });
                if (command is "run" or "doctor") return await RunJobAsync(client, result);
                Print(result);
                return 0;
            }
            var (name, positional) = Route(command, tokens);
            var schema = AgentCatalog.Builtins.First(item => item.Name == name).InputSchema;
            var values = tokens.Count > 0 && tokens[0] is "--json" or "--json-file"
                ? (await JsonInputAsync(tokens)).EnumerateObject().ToDictionary(field => field.Name, field => (object)field.Value)
                : ReadPairs(tokens).ToDictionary(pair => pair.Key.Replace('-', '_'), pair => ConvertValue(pair.Value,
                    schema.GetProperty("properties").TryGetProperty(pair.Key.Replace('-', '_'), out var field) ? field : default));
            foreach (var pair in positional)
                if (!values.TryAdd(pair.Key, pair.Value)) throw new ArgumentException($"Duplicate argument: {pair.Key}");
            var response = await client.InvokeAsync(name, values);
            Print(response);
            if (name == "jobs.wait") return response.GetProperty("completed").GetBoolean() ? ExitCode(response.GetProperty("job")) : 3;
            return 0;
        }
        catch (Exception exception)
        {
            var error = AgentServer.Error(exception);
            if (background && state is not null)
            {
                Directory.CreateDirectory(state);
                await File.WriteAllTextAsync(Path.Combine(state, "agent-startup-error.txt"), JsonSerializer.Serialize(error, AgentProtocol.Json));
            }
            else Console.Error.WriteLine(JsonSerializer.Serialize(new { error }, AgentProtocol.Json));
            return error.Code == "invalid_arguments" ? 2 : error.Code == "cancelled" ? 130 : 1;
        }
    }

    private static async Task<int> ServeAsync(string root, string state)
    {
        await using var host = await KitHost.OpenAsync(root, state, hostKind: "headless");
        using var stopping = CancellationTokenSource.CreateLinkedTokenSource(host.HeadlessStopping);
        ConsoleCancelEventHandler cancel = (_, e) => { e.Cancel = true; stopping.Cancel(); };
        Console.CancelKeyPress += cancel;
        try
        {
            await Task.Delay(Timeout.Infinite, stopping.Token);
        }
        catch (OperationCanceledException) when (stopping.IsCancellationRequested) { }
        finally { Console.CancelKeyPress -= cancel; }
        return 0;
    }

    private static async Task<int> RunJobAsync(AgentClient client, JsonElement submitted)
    {
        var id = submitted.GetProperty("id").GetGuid();
        using var cancelled = new CancellationTokenSource();
        ConsoleCancelEventHandler cancel = (_, e) => { e.Cancel = true; cancelled.Cancel(); };
        Console.CancelKeyPress += cancel;
        try
        {
            try
            {
                while (true)
                {
                    var response = await client.InvokeAsync("jobs.wait", new { job_id = id, wait_seconds = 20 }, cancelled.Token);
                    if (!response.GetProperty("completed").GetBoolean()) continue;
                    var job = response.GetProperty("job");
                    Print(job);
                    return ExitCode(job);
                }
            }
            catch (OperationCanceledException) when (cancelled.IsCancellationRequested)
            {
                await client.InvokeAsync("jobs.cancel", new { job_id = id });
                var response = await client.InvokeAsync("jobs.wait", new { job_id = id, wait_seconds = 30 });
                Print(response.GetProperty("job"));
                return 130;
            }
        }
        finally { Console.CancelKeyPress -= cancel; }
    }

    private static int ExitCode(JsonElement job) => job.GetProperty("status").GetString() switch
    {
        "Succeeded" => 0, "Cancelled" => 130, _ => 1
    };

    private static (string Operation, Dictionary<string, object> Values) Route(string command, List<string> tokens)
    {
        var values = new Dictionary<string, object>();
        string Pair(string operation, string field)
        {
            values.Add(field, Shift(tokens));
            return operation;
        }
        var operation = command switch
        {
            "status" => "status",
            "list" => "tools.list",
            "describe" => Pair("tools.describe", "tool_id"),
            "jobs" when tokens.Count == 0 || tokens[0].StartsWith("--", StringComparison.Ordinal) => "jobs.list",
            "jobs" => Shift(tokens) switch { "list" => "jobs.list", "get" => Pair("jobs.get", "job_id"),
                "wait" => Pair("jobs.wait", "job_id"), "cancel" => Pair("jobs.cancel", "job_id"), _ => throw new ArgumentException("Unknown jobs command.") },
            "logs" => "logs.tail",
            "addons" when tokens.Count == 0 => "addons.list",
            "addons" => Shift(tokens) switch { "list" => "addons.list", "enable" => Enable(true), "disable" => Enable(false), _ => throw new ArgumentException("Unknown add-ons command.") },
            "settings" => Shift(tokens) switch { "get" => Pair("settings.get", "key"), "set" => Pair("settings.set", "key"), _ => throw new ArgumentException("Use settings get or set.") },
            "workspace" when tokens.Count == 0 => "workspace.get",
            "workspace" => Shift(tokens) switch { "get" => "workspace.get", "create" => Pair("workspace.create", "directory"), "open" => Pair("workspace.open", "directory"), _ => throw new ArgumentException("Unknown workspace command.") },
            "assets" => "assets." + Shift(tokens),
            "artifacts" => Pair("artifacts." + Shift(tokens), "job_id"),
            "cache" => "cache." + (tokens.Count > 0 ? Shift(tokens) : "stats"),
            "server" => Shift(tokens) switch { "status" => "status", "stop" => "host.stop", _ => throw new ArgumentException("Use server status or stop.") },
            _ => throw new ArgumentException("Unknown command. Run Backport.cmd help for usage.")
        };
        string Enable(bool enabled)
        {
            values.Add("enabled", enabled);
            return Pair("addons.set_enabled", "addon_id");
        }
        if (!AgentCatalog.Builtins.Any(item => item.Name == operation)) throw new ArgumentException("Unknown operation: " + operation);
        return (operation, values);
    }

    private static object ConvertValue(string value, JsonElement schema)
    {
        var type = schema.ValueKind == JsonValueKind.Object && schema.TryGetProperty("type", out var typeValue) ? typeValue.GetString() : null;
        if (type == "integer") return long.Parse(value, System.Globalization.CultureInfo.InvariantCulture);
        if (type == "boolean") return bool.Parse(value);
        if (schema.ValueKind == JsonValueKind.Object && type is null) return JsonDocument.Parse(value).RootElement.Clone();
        return value;
    }

    private static Dictionary<string, string> ReadPairs(List<string> tokens)
    {
        var values = new Dictionary<string, string>();
        while (tokens.Count > 0)
        {
            var key = Shift(tokens);
            if (!key.StartsWith("--", StringComparison.Ordinal) || key.Length == 2)
                throw new ArgumentException("Arguments use --key value pairs.");
            if (!values.TryAdd(key[2..], Shift(tokens))) throw new ArgumentException("Duplicate option: " + key);
        }
        return values;
    }

    private static async Task<JsonElement> JsonInputAsync(List<string> tokens)
    {
        var flag = Shift(tokens);
        if (flag is not ("--json" or "--json-file")) throw new ArgumentException("Supply --json OBJECT or --json-file FILE (use - for stdin).");
        var value = Shift(tokens);
        if (tokens.Count != 0) throw new ArgumentException("JSON input cannot be combined with other flags.");
        if (flag == "--json-file")
        {
            using var reader = value == "-" ? Console.In : File.OpenText(value);
            var buffer = new char[1024 * 1024 + 1];
            var count = await reader.ReadBlockAsync(buffer);
            if (count == buffer.Length) throw new ArgumentException("JSON input exceeds 1 MiB.");
            value = new string(buffer, 0, count);
        }
        if (System.Text.Encoding.UTF8.GetByteCount(value) > 1024 * 1024) throw new ArgumentException("JSON input exceeds 1 MiB.");
        using var document = JsonDocument.Parse(value);
        if (document.RootElement.ValueKind != JsonValueKind.Object) throw new ArgumentException("JSON input must be an object.");
        return document.RootElement.Clone();
    }

    private static string Shift(List<string> tokens)
    {
        if (tokens.Count == 0) throw new ArgumentException("A required command or value is missing.");
        var value = tokens[0];
        tokens.RemoveAt(0);
        return value;
    }

    private static void Print(JsonElement value) => Console.WriteLine(JsonSerializer.Serialize(value, JsonFiles.Options));

    private const string Help = """
        Source Backport agent CLI
        Backport.cmd [--root V4_FOLDER] [--state STATE_FOLDER] COMMAND
        status | list | describe TOOL | operations
        validate TOOL [--key value ... | --json OBJECT | --json-file FILE]
        run TOOL [arguments]                 Wait for a result; Ctrl+C cancels the job
        submit TOOL [arguments]              Return a job ID; keep running in the backend
        doctor                              Run the environment check
        jobs [list|get ID|wait ID|cancel ID] [--count N|--offset N|--wait-seconds N]
        logs [--job-id ID] [--count N]
        artifacts list ID [--path RELATIVE] | artifacts read ID --path RELATIVE [--utf8 true]
        assets providers | assets list --container PATH | assets read --provider ID --container PATH --path PATH
        addons [list|enable ID|disable ID]
        settings get KEY [--addon-id ID] | settings set KEY --value JSON [--addon-id ID]
        workspace [get|create DIR|open DIR] | cache [stats|clear]
        server status | server stop | serve
        call OPERATION --json OBJECT        Full access to the shared API
        JSON files may use - to read stdin. Global --root/--state precede COMMAND.
        Output is JSON; errors are JSON on stderr. No desktop window is required.
        """;
}
