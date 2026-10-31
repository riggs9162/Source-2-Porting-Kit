namespace PortingKit.Addons.Porting;

/// <summary>Translates Source 2 brush surface names into stock Source 1 properties.</summary>
public static class BrushMaterials
{
    private static readonly Dictionary<string, string> Aliases = new()
    {
        ["concrete_flooded"] = "concrete", ["tile_floor"] = "tile", ["metal_panel"] = "metalpanel", ["metal_combine"] = "metal",
        ["metal_thin"] = "metal", ["metal_grate"] = "metalgrate", ["metal_box"] = "metal_box", ["wood_solid"] = "wood", ["wood_plank"] = "wood",
        ["mud_puddles"] = "mud", ["linoleum_wet"] = "tile", ["foliage"] = "grass", ["asphalt"] = "concrete", ["cloth"] = "carpet", ["drywall"] = "plaster"
    };

    /// <summary>Maps a namespaced Alyx property to its closest standard Source 1 surface.</summary>
    public static string Source1Surfaceprop(string value)
    {
        var name = value.Trim().ToLowerInvariant();
        name = name[(name.LastIndexOf('.') + 1)..];
        return Aliases.GetValueOrDefault(name, name);
    }
}
