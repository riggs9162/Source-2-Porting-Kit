using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using static PortingKit.Core.Automation.AgentSchema;

namespace PortingKit.Core.Automation;

/// <summary>Discovers every backend tool without creating an add-on desktop page.</summary>
public sealed class AgentCatalog(KitHost host)
{
    public static IReadOnlyList<AgentOperation> Builtins { get; } =
    [
        new("status", "Inspect the shared backend, process, active jobs, paths, and restart requirements.", Object()),
        new("tools.list", "List all loaded add-on tools and their parameters.", Object()),
        new("tools.describe", "Describe a tool, including its agent operation and typed input schema.", Object(new Field("tool_id", "string", Required: true))),
        new("tools.validate", "Validate and normalize tool inputs without running or saving them.", Object(new Field("tool_id", "string", Required: true), new("arguments", "object", Required: true))),
        new("jobs.submit", "Queue a tool and return its job ID immediately. Jobs survive client disconnects.", Object(new Field("tool_id", "string", Required: true), new("arguments", "object", Required: true)), false, true),
        new("jobs.list", "Read a bounded page of jobs, newest first.", Object(new Field("offset", "integer", Minimum: 0), new("count", "integer", Minimum: 1, Maximum: 100))),
        new("jobs.get", "Read a job's progress, result, error, arguments, and artifact references.", Object(new Field("job_id", "string", Required: true))),
        new("jobs.cancel", "Request cancellation of a queued or running job and its child processes.", Object(new Field("job_id", "string", Required: true)), false, true),
        new("jobs.wait", "Wait up to 30 seconds for a terminal job state. Repeat if still active; timeout does not cancel the job.", Object(new Field("job_id", "string", Required: true), new("wait_seconds", "integer", Minimum: 0, Maximum: 30))),
        new("artifacts.list", "List a bounded page of a job's output directory. Paths are relative to that job's output.", Object(new Field("job_id", "string", Required: true), new("path", "string"), new("offset", "integer", Minimum: 0), new("count", "integer", Minimum: 1, Maximum: 200))),
        new("artifacts.read", "Read up to 64 KiB of a job output file as base64, optionally decoded as UTF-8. Links and paths outside the job output are rejected.", Object(new Field("job_id", "string", Required: true), new("path", "string", Required: true), new("offset", "integer", Minimum: 0), new("max_bytes", "integer", Minimum: 1, Maximum: 65536), new("utf8", "boolean"))),
        new("logs.tail", "Read the live log tail, optionally filtered to one job. The tail is bounded and resets on backend restart.", Object(new Field("job_id", "string"), new("count", "integer", Minimum: 1, Maximum: 200))),
        new("addons.list", "Inspect discovered add-ons, errors, dependencies, and configured enablement.", Object()),
        new("addons.set_enabled", "Save an add-on's enabled state. Takes effect on the next backend restart.", Object(new Field("addon_id", "string", Required: true), new("enabled", "boolean", Required: true)), false),
        new("settings.get", "Read a host setting or an add-on-owned setting. Use tools.vrf, tools.ffmpeg, tools.studiomdl, runtime, or appearance for host settings.", Object(new Field("key", "string", Required: true), new("addon_id", "string"))),
        new("settings.set", "Persist a host or add-on-owned setting. Runtime and add-on settings may require a restart; appearance updates the open desktop.", Object(new Field("key", "string", Required: true), new("value", "any", Required: true), new("addon_id", "string")), false),
        new("workspace.get", "Read the current and recent project workspaces.", Object()),
        new("workspace.create", "Create a project workspace marker in a directory and open it.", Object(new Field("directory", "string", Required: true)), false),
        new("workspace.open", "Open a directory containing an existing workspace marker.", Object(new Field("directory", "string", Required: true)), false),
        new("assets.providers", "List asset providers registered by loaded add-ons.", Object()),
        new("assets.list", "List a bounded page of folder or VPK metadata without decoding content.", Object(new Field("container", "string", Required: true), new("provider", "string"), new("search", "string"), new("offset", "integer", Minimum: 0), new("count", "integer", Minimum: 1, Maximum: 200))),
        new("assets.read", "Read a bounded chunk of an asset stream from a folder or VPK. Returns base64 and optional UTF-8 text.", Object(new Field("provider", "string", Required: true), new("container", "string", Required: true), new("path", "string", Required: true), new("offset", "integer", Minimum: 0), new("max_bytes", "integer", Minimum: 1, Maximum: 65536), new("utf8", "boolean"))),
        new("cache.stats", "Read disk preview cache usage and active leases.", Object()),
        new("cache.clear", "Remove unused cached previews while preserving active leases.", Object(), false, true),
        new("host.stop", "Stop an idle headless backend. Refuses desktop hosts and active jobs.", Object(), false)
    ];

    public IReadOnlyList<AgentOperation> Operations() => Builtins.Concat(host.Contributions.GetAll<ITool>()
        .Select(item => ForTool(item.Id, item.Value.Definition))).ToArray();

    public static AgentOperation ForTool(string id, ToolDefinition definition)
    {
        var properties = new Dictionary<string, object>();
        foreach (var parameter in definition.Parameters)
        {
            var value = new Dictionary<string, object>
            {
                ["type"] = parameter.Kind switch { ParameterKind.Boolean => "boolean", ParameterKind.Integer => "integer", _ => "string" },
                ["description"] = parameter.Label + (parameter.Description.Length > 0 ? ": " + parameter.Description : "")
            };
            if (parameter.Kind == ParameterKind.Integer)
            {
                value["minimum"] = parameter.Minimum;
                value["maximum"] = parameter.Maximum;
            }
            if (parameter.Kind == ParameterKind.Choice) value["enum"] = parameter.Choices ?? [];
            if (parameter.Kind is not (ParameterKind.Integer or ParameterKind.Boolean)) value["maxLength"] = 32768;
            properties.Add(parameter.Key, value);
        }
        var slug = new string(id.Select(character => char.IsAsciiLetterOrDigit(character) ? character : '_').ToArray());
        var hash = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(id)))[..8].ToLowerInvariant();
        return new("run_" + slug[..Math.Min(90, slug.Length)] + "_" + hash,
            $"{definition.Name}. {definition.Description} Queues {id} and immediately returns a job ID; poll jobs_get or jobs_wait for results.",
            AgentProtocol.Element(new { type = "object", properties,
                required = definition.Parameters.Where(parameter => parameter.Required).Select(parameter => parameter.Key).ToArray(), additionalProperties = false }), false, true);
    }
}
