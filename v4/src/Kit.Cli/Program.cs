using System.Text.Json;
using PortingKit;
using PortingKit.Core;

var tokens = args.ToList();
string? Option(string name)
{
    var index = tokens.IndexOf(name);
    if (index < 0)
        return null;
    if (index + 1 >= tokens.Count)
        throw new ArgumentException($"{name} requires a value.");
    var value = tokens[index + 1];
    tokens.RemoveRange(index, 2);
    return value;
}
try
{
    var root = Option("--root") ?? FindRoot();
    var state = Option("--state");
    var command = tokens.FirstOrDefault() ?? "help";
    if (command == "help")
    {
        Console.WriteLine("s2kit [--root V4_FOLDER] [--state STATE_FOLDER] list | addons | jobs | describe TOOL | run TOOL [--key value] | doctor");
        return 0;
    }
    await using var host = await KitHost.OpenAsync(root, state);
    void Print(object value) => Console.WriteLine(JsonSerializer.Serialize(value, JsonFiles.Options));
    switch (command)
    {
        case "list":
            Print(host.Contributions.GetAll<ITool>().Select(tool => new { tool.Id, tool.Value.Definition }));
            return 0;
        case "addons":
            Print(host.Addons.Snapshot());
            return host.Addons.Snapshot().Any(addon => addon.State == AddonState.Failed) ? 1 : 0;
        case "jobs":
            Print(host.Jobs.Snapshot());
            return 0;
        case "describe" when tokens.Count == 2:
            Print(host.Contributions.Get<ITool>(tokens[1]).Definition);
            return 0;
        case "run" when tokens.Count >= 2:
        case "doctor":
            var toolId = command == "doctor" ? "utilities/environment" : tokens[1];
            var values = new Dictionary<string, string>();
            for (var index = 2; command == "run" && index < tokens.Count; index += 2)
            {
                if (!tokens[index].StartsWith("--", StringComparison.Ordinal) || index + 1 >= tokens.Count)
                    throw new ArgumentException("Tool arguments use --key value pairs.");
                values.Add(tokens[index][2..], tokens[index + 1]);
            }
            var job = await host.Jobs.SubmitAsync(toolId, values);
            ConsoleCancelEventHandler cancel = (_, e) => { e.Cancel = true; host.Jobs.Cancel(job.Id); };
            Console.CancelKeyPress += cancel;
            try
            {
                while (true)
                {
                    var current = host.Jobs.Snapshot().First(item => item.Id == job.Id);
                    if (!JobQueue.IsActive(current.Status))
                    {
                        Print(current);
                        return current.Status == JobStatus.Succeeded ? 0 : current.Status == JobStatus.Cancelled ? 130 : 1;
                    }
                    await Task.Delay(75);
                }
            }
            finally
            {
                Console.CancelKeyPress -= cancel;
            }
        default:
            throw new ArgumentException("Unknown command. Run s2kit help for usage.");
    }
}
catch (Exception exception)
{
    Console.Error.WriteLine(exception.Message);
    return 1;
}

static string FindRoot()
{
    for (var directory = new DirectoryInfo(AppContext.BaseDirectory); directory is not null; directory = directory.Parent)
        if (Directory.Exists(Path.Combine(directory.FullName, "addons")))
            return directory.FullName;
    return Environment.CurrentDirectory;
}
