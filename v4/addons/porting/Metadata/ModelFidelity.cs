using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>Loads the authored model graph kept beside an export.</summary>
public static class ModelFidelity
{
    public const string Schema = "s2kit.model-fidelity.v1";

    /// <summary>Loads a sidecar, accepting a GLB or the explicit sidecar path.</summary>
    /// <remarks>The result gains <c>_sidecar_directory</c> so relative block and mesh paths can be resolved.</remarks>
    public static JsonObject Load(string path)
    {
        if (Path.GetExtension(path).ToLowerInvariant() is ".glb" or ".gltf") path = Path.ChangeExtension(path, ".fidelity.json");
        var result = Json.Load(path);
        result["_sidecar_directory"] = Path.GetDirectoryName(Path.GetFullPath(path));
        if (result.Get("schema").Str() != Schema) throw new InvalidDataException("Unsupported or missing model fidelity sidecar schema.");
        return result;
    }
}
