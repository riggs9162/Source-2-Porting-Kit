using System.Text;
using System.Text.Json;

namespace PortingKit.Addons.Porting;

/// <summary>Which of a model's separate outputs a stage builds.</summary>
public enum ModelVariant { Regular, Static, Player }

/// <summary>The settings recorded with a resumable project.</summary>
public sealed record WorkflowOptions
{
    public string Sources { get; init; } = "";
    public string Project { get; init; } = "";
    public string Namespace { get; init; } = "ports";
    public string ModelType { get; init; } = "auto";
    public string MaterialSource { get; init; } = "";
    public string VrfExecutable { get; init; } = "";
    public string Compiler { get; init; } = "";
    public string Game { get; init; } = "";
    public bool IncludeSourceAnimations { get; init; } = true;
    public bool EmitNormal { get; init; } = true;

    /// <summary>Caps regular model textures; zero keeps native size.</summary>
    public int Resolution { get; init; }

    /// <summary>Adds a separate <c>_static</c> model for models with no physics or one rigid body.</summary>
    public bool StaticVariant { get; init; }

    /// <summary>Caps the static model's textures independently; zero keeps native size.</summary>
    public int StaticResolution { get; init; }

    /// <summary>Adds a separate <c>_pm</c> playermodel for humanoid characters.</summary>
    public bool CreatePlayermodels { get; init; }

    /// <summary>Where the published player animation sources are cached.</summary>
    public string AnimationCache { get; init; } = "";

    /// <summary>Sequence names to loop whatever their source says, one per line; <c>*</c> loops all of them.</summary>
    public string LoopAnimations { get; init; } = "";

    public bool Install { get; init; }
    public string GmodAddons { get; init; } = "";
}

/// <summary>A selected model with an extraction and output identity independent of queue order.</summary>
public sealed record PortAsset(string Key, string Source, string Resource, string Relative, string MaterialRoot = "")
{
    public bool CompiledInput => Path.GetExtension(Source).ToLowerInvariant() is ".vpk" or ".vmdl_c";

    public static PortAsset Create(string source, string resource, string relative, string materialRoot = "")
    {
        var full = Path.GetFullPath(source);
        var key = Digest.Text(full.ToLowerInvariant() + ":" + resource.ToLowerInvariant())[..16];
        var path = Resources.Relative(relative);
        var extension = Path.GetExtension(path);
        if (extension.Length > 0) path = path[..^extension.Length];
        if (path.StartsWith("models/", StringComparison.Ordinal)) path = path["models/".Length..];
        return new(key, full, resource, path, materialRoot);
    }
}

/// <summary>A measured preflight result, with its scope and severity preserved in reports.</summary>
public sealed record Finding(string Severity, string Asset, string Message);

/// <summary>One checkpointed stage and the verified files it produced.</summary>
public sealed class StageRecord
{
    public string Id { get; set; } = "";
    public string Kind { get; set; } = "";
    public string Source { get; set; } = "";
    public List<string> Dependencies { get; set; } = [];
    public string Status { get; set; } = "pending";
    public Dictionary<string, string> Outputs { get; set; } = [];
    public List<string[]> Messages { get; set; } = [];
    public string Error { get; set; } = "";
    public bool Rerun { get; set; }
    public string ConverterSignature { get; set; } = "";
    public string DependencySignature { get; set; } = "";
}

/// <summary>The durable state of one project, replaced atomically after every stage.</summary>
public sealed class WorkflowState
{
    public const int CurrentVersion = 3;

    public static readonly JsonSerializerOptions Serializer = new(Json.Options)
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower
    };

    public int Version { get; set; } = CurrentVersion;
    public WorkflowOptions Options { get; set; } = new();
    public string Signature { get; set; } = "";
    public List<PortAsset> Assets { get; set; } = [];
    public Dictionary<string, StageRecord> Tasks { get; set; } = [];
    public List<Finding> Findings { get; set; } = [];
    public string Status { get; set; } = "planned";
    public DateTimeOffset UpdatedAt { get; set; }

    public void Save(string path)
    {
        var temporary = path + ".tmp";
        using (var stream = new FileStream(temporary, FileMode.Create, FileAccess.Write, FileShare.None))
        {
            JsonSerializer.Serialize(stream, this, Serializer);
            stream.Flush(true);
        }
        File.Move(temporary, path, true);
    }

    public static WorkflowState Load(string path) =>
        JsonSerializer.Deserialize<WorkflowState>(File.ReadAllBytes(path), Serializer) ?? throw new InvalidDataException("The workflow checkpoint is empty.");

    /// <summary>Writes factual outcomes, remaining failures and what was not validated.</summary>
    public string WriteReport(string project)
    {
        var report = Path.Combine(project, "port-report.md");
        Save(Path.Combine(project, "port-report.json"));
        var text = new StringBuilder("# Port report\n\n").Append($"Status: {Status}\n\n## Preflight\n\n");
        foreach (var finding in Findings) text.Append($"- {finding.Severity.ToUpperInvariant()}: {finding.Asset} - {finding.Message}\n");
        text.Append("\n## Queue\n\n");
        foreach (var (key, record) in Tasks)
        {
            text.Append($"- {key}: {record.Status}").Append(record.Error.Length > 0 ? $" - {record.Error}" : "").Append('\n');
            foreach (var message in record.Messages.Where(item => item[0] is "Warning" or "Error")) text.Append($"  - {message[0].ToUpperInvariant()}: {message[1]}\n");
            foreach (var output in record.Outputs.Keys) text.Append($"  - Output: {output}\n");
        }
        text.Append("\n## Conversion audits\n\n");
        foreach (var record in Tasks.Values)
            foreach (var output in record.Outputs.Keys.Where(name => Path.GetFileName(name) is "ragdoll-report.json" or "prop-report.json" or "playermodel-report.json" or "compiled-report.json" or "material-report.json"))
                text.Append($"- [{Path.GetFileName(output)}](<{output.Replace('\\', '/')}>)\n");
        text.Append("\n## Validation\n\n")
            .Append("- Compiled outputs are packaged only after file-consistency checks and available physics audits.\n")
            .Append("- Source primitives, joint envelopes, shader conversion and generated physics retain their individual adaptation reports.\n")
            .Append("- In-game appearance, movement and physical behavior require an explicit engine validation record.\n")
            .Append("- Editable sources and reports remain outside the addon; installation is optional.\n");
        Fmt.WriteText(report, text.ToString());
        return report;
    }
}
