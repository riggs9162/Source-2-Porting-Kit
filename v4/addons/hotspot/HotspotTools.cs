using System.Text.Json;

namespace PortingKit.Addons.Hotspot;

public sealed class HotspotTool(string operation) : ITool
{
    private static ToolParameter Input => new("input", "Rectangle or document file", ParameterKind.File, Required: true, Description: ".rect or editable .json document.");
    private static ToolParameter Width => new("width", "Texture width", ParameterKind.Integer, "1024", Minimum: 1, Maximum: 65535, Description: "Original texture pixels; used for .rect inputs.");
    private static ToolParameter Height => new("height", "Texture height", ParameterKind.Integer, "1024", Minimum: 1, Maximum: 65535, Description: "Original texture pixels; used for .rect inputs.");
    public ToolDefinition Definition { get; } = Define(operation) with { ShowInDesktop = false };

    private static ToolDefinition Define(string operation) => operation switch
    {
        "inspect" => new("Inspect hotspot sheet", "Materials", "Reads either .rect dialect or editable JSON; reports validation, coverage, and approximate fit rankings.",
            [Input, Width, Height, new("face_width", "Preview face width", ParameterKind.Integer, "128", Minimum: 1, Maximum: 65535),
                new("face_height", "Preview face height", ParameterKind.Integer, "128", Minimum: 1, Maximum: 65535), new("include_alt", "Include alternate rectangles", ParameterKind.Boolean, "false")]),
        "generate" => new("Generate hotspot layout", "Materials", "Creates an editable JSON sheet and Hammer++ .rect in job outputs.",
            [Width, Height, new("layout", "Layout", ParameterKind.Choice, "grid", Choices: ["grid", "trim-124", "trim-1248", "columns-24"]),
                new("columns", "Columns", ParameterKind.Integer, "2", Minimum: 1, Maximum: 64), new("rows", "Rows", ParameterKind.Integer, "2", Minimum: 1, Maximum: 64)]),
        "convert" => new("Write hotspot sheet", "Materials", "Writes validated .rect or JSON. Edit the JSON rectangles to create or modify a sheet from an agent. An explicit output path is backed up before replacement.",
            [Input, Width, Height, new("format", "Output format", ParameterKind.Choice, "hammerpp", Choices: ["hammerpp", "source2", "json"]),
                new("flags", "Write Strata flags", ParameterKind.Boolean, "true"), new("repair_scale", "Fix oversized pixel regions", ParameterKind.Boolean, "false", Description: "Only repairs an exact 2x/4x/8x/16x mismatch to the supplied texture size."),
                new("output", "Output file", ParameterKind.Text, Description: "Leave empty for the job's artifact folder.")]),
        "detect" => new("Detect hotspot regions", "Materials", "Detects alpha islands or flat seams on a bounded native texture preview. Coordinates use the original dimensions; review the generated sheet before use.",
            [new("texture", "Texture or material VMT", ParameterKind.File, Required: true, Description: "VMT inputs resolve their base texture automatically."), new("minimum", "Minimum region pixels", ParameterKind.Integer, "16", Minimum: 1, Maximum: 4096),
                new("snap", "Snap grid (0 disables)", ParameterKind.Integer, "0", Minimum: 0, Maximum: 1024)]),
        "link-material" => new("Link hotspot material", "Materials", "Updates only the VMT's rectangle-map reference after validating the Hammer++ sheet against its base texture. Backs up the VMT.",
            [new("material", "Material VMT", ParameterKind.File, Required: true), new("rect", "Hammer++ rectangle map", ParameterKind.File, Required: true)]),
        _ => throw new ArgumentException("Unknown hotspot operation.")
    };

    public async Task<ToolResult> RunAsync(ToolContext context, ToolArguments args, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        if (operation == "link-material")
        {
            var material = Path.GetFullPath(args.Text("material"));
            var rect = Path.GetFullPath(args.Text("rect"));
            var texture = MaterialFiles.TextureFor(material) ?? throw new InvalidDataException("The VMT's base texture could not be resolved.");
            using var preview = TexturePreview.Load(texture, token);
            var sheet = HotspotFormats.Read(rect, preview.Width, preview.Height);
            if (sheet.SourceFormat != "hammerpp" || Path.GetExtension(rect).Equals(".json", StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("VMT rectangle maps require the Hammer++ .rect format.");
            HotspotFormats.Write(sheet, "hammerpp");
            await HotspotFormats.SaveAsync(material, MaterialFiles.LinkText(material, rect), true, token);
            return new("Linked the material to its hotspot sheet.", [new(material, "Linked VMT"), new(material + ".bak", "Previous VMT")]);
        }
        HotspotDocument doc;
        if (operation == "generate")
        {
            var width = args.Integer("width"); var height = args.Integer("height");
            var regions = args.Text("layout") switch
            {
                "trim-124" => HotspotDocument.Trim(width, height, [1, 2, 4]),
                "trim-1248" => HotspotDocument.Trim(width, height, [1, 2, 4, 8]),
                "columns-24" => HotspotDocument.Trim(width, height, [2, 4], true),
                _ => HotspotDocument.Grid(width, height, args.Integer("columns"), args.Integer("rows"))
            };
            doc = new(width, height, regions);
        }
        else if (operation == "detect")
        {
            context.Report(null, "Decoding a bounded texture preview");
            using var preview = TexturePreview.Load(args.Text("texture"), token);
            doc = new(preview.Width, preview.Height, RegionDetection.Detect(preview, args.Integer("minimum"), args.Integer("snap"), token));
            context.Log($"Detection uses {preview.Bitmap.Width}x{preview.Bitmap.Height} preview pixels; output coordinates use {doc.Width}x{doc.Height}.");
        }
        else doc = HotspotFormats.Read(args.Text("input"), args.Integer("width"), args.Integer("height"));
        if (operation == "convert" && args.Boolean("repair_scale"))
        {
            var factor = doc.OversizeFactor();
            doc = doc.RepairOversize();
            context.Log(factor > 1 ? $"Divided region coordinates by {factor} to match the texture." : "No exact power-of-two size mismatch found.");
        }
        var artifacts = new List<Artifact>();
        var documentPath = Path.Combine(context.OutputDirectory, "sheet.json");
        await HotspotFormats.SaveAsync(documentPath, JsonSerializer.Serialize(doc, HotspotDocument.JsonOptions), false, token);
        artifacts.Add(new(documentPath, "Editable hotspot document"));
        var format = operation == "convert" ? args.Text("format") : "hammerpp";
        var flags = operation != "convert" || args.Boolean("flags");
        var losses = format == "json" ? doc.ImportWarnings : doc.Losses(format, flags);
        foreach (var loss in losses) context.Log(loss, LogLevel.Warning);
        if (operation != "inspect")
        {
            var output = operation == "convert" ? args.Text("output") : "";
            var path = string.IsNullOrWhiteSpace(output) ? Path.Combine(context.OutputDirectory, format == "json" ? "converted.json" : "sheet.rect") : Path.GetFullPath(output);
            if (operation == "convert" && Path.GetFullPath(args.Text("input")).Equals(path, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Choose a separate output path; keep the input document available for editing.");
            if (Path.GetExtension(path).Equals(format == "json" ? ".json" : ".rect", StringComparison.OrdinalIgnoreCase) == false)
                throw new InvalidDataException("Output extension must match the selected format (.json or .rect).");
            await HotspotFormats.SaveAsync(path, HotspotFormats.Write(doc, format, flags), true, token);
            artifacts.Add(new(path, format == "json" ? "Converted document" : "Rectangle map"));
        }
        var report = new
        {
            doc.Width, doc.Height, rectangles = doc.Rectangles.Length, coverage = doc.Coverage(),
            issues = doc.Validate(), losses, suggestedScaleDivisor = doc.OversizeFactor(),
            fit = operation == "inspect" ? doc.Match(args.Integer("face_width"), args.Integer("face_height"), args.Boolean("include_alt")) : [],
            fitMethod = "Approximate aspect/area ranking, not Hammer's randomized algorithm. Rectangle indices are zero-based."
        };
        var reportPath = Path.Combine(context.OutputDirectory, "report.json");
        await HotspotFormats.SaveAsync(reportPath, JsonSerializer.Serialize(report, HotspotDocument.JsonOptions), false, token);
        artifacts.Add(new(reportPath, "Validation report"));
        return new($"{doc.Rectangles.Length} hotspot rectangles, {doc.Coverage():P0} coverage.", artifacts, JsonSerializer.SerializeToElement(report, HotspotDocument.JsonOptions));
    }
}
