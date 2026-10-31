using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>Generates self-contained GMod drivers for authored model defaults and deformation.</summary>
public static class RagdollRuntime
{
    private const int ChunkLength = 48000;

    /// <summary>Writes a shared autorun driver with model-scoped state and no external dependency.</summary>
    /// <remarks>The configuration lands in numbered chunks under a folder named for its own content hash. A changed model
    /// therefore gets new include paths, which is what lets a live GMA replacement pick up new data, while an unchanged
    /// model keeps the old ones. The hook identity comes from the model path alone and stays put across changes.</remarks>
    public static string WriteModelDriver(string output, string modelname, JsonObject data)
    {
        var configuration = data.DeepClone().AsObject();
        configuration["model"] = ("models/" + modelname).ToLowerInvariant();
        RagdollAppearance.Update(configuration, RagdollCorrectives.PrepareCorrectiveDrivers(configuration));
        if (!configuration.ContainsKey("source_frames")) configuration["source_frames"] = new JsonObject();
        if (!configuration.ContainsKey("skin_names")) configuration["skin_names"] = new JsonArray();
        configuration.Remove("source_constraints");
        var encoded = PythonJson.Compact(configuration);
        if (encoded.Contains("]==]", StringComparison.Ordinal)) throw new InvalidDataException("Model driver data contains a reserved Lua delimiter");
        var path = Path.Combine(output, "model_driver.lua");
        var identity = Digest.Text(encoded)[..16];
        var folder = Path.Combine(output, "driver_data", identity);
        Directory.CreateDirectory(folder);
        var entries = new JsonArray();
        var includes = new List<string>();
        for (var index = 0; index * ChunkLength < encoded.Length; index++)
        {
            var name = $"config_{index:D4}.lua";
            var part = Path.Combine(folder, name);
            var start = index * ChunkLength;
            PythonText.Write(part, "return [==[" + encoded[start..Math.Min(start + ChunkLength, encoded.Length)] + "]==]\n");
            entries.Add(new JsonObject { ["source"] = part, ["relative"] = $"lua/s2kit/{identity}/{name}" });
            includes.Add($"s2kit/{identity}/{name}");
        }
        var loader = new StringBuilder("if ( SERVER ) then\n");
        foreach (var name in includes) loader.Append("    AddCSLuaFile(").Append(PythonJson.Compact(JsonValue.Create(name))).Append(")\n");
        loader.Append("end\n\nlocal config = util.JSONToTable(table.concat({\n");
        foreach (var name in includes) loader.Append("    include(").Append(PythonJson.Compact(JsonValue.Create(name))).Append("),\n");
        loader.Append("}))");
        Fmt.WriteText(path, RagdollDriverLua.Text.Replace(RagdollDriverLua.ConfigLoader, loader.ToString(), StringComparison.Ordinal));
        PythonText.Write(Path.ChangeExtension(path, ".payload.json"), PythonJson.Indented(entries) + "\n");
        return path;
    }

    /// <summary>Lists generated configuration chunks alongside the shared driver and its manifest.</summary>
    public static List<string> ModelDriverFiles(string path)
    {
        var manifest = Path.ChangeExtension(path, ".payload.json");
        var entries = JsonNode.Parse(File.ReadAllBytes(manifest), null, new JsonDocumentOptions { MaxDepth = 512 }).Arr();
        return [path, manifest, .. entries.Select(entry => entry.Need("source").Str())];
    }
}
