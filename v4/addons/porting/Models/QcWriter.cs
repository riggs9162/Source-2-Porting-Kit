namespace PortingKit.Addons.Porting;

/// <summary>Resolved information for one animation clip in the QC.</summary>
public sealed record AnimMeta(string ClipName, string SmdFilename, int NumFrames, double Fps, bool Loop);

/// <summary>Everything a generated QC depends on beyond the model's own name.</summary>
public sealed record QcContent
{
    public required string Name { get; init; }
    public required string Modelname { get; init; }
    public required string Cdmaterials { get; init; }
    public required string Surfaceprop { get; init; }
    public required bool HasPhysics { get; init; }
    public required bool Concave { get; init; }
    public required double? Mass { get; init; }
    public int PieceCount { get; init; } = 1;
    public bool Remove2d { get; init; }
    public double? Inertia { get; init; }
    public double? Damping { get; init; }
    public double? RotDamping { get; init; }
    public GltfSkin? Skin { get; init; }
    public CoordinateMode? Coordinates { get; init; }
    public IReadOnlyList<AnimMeta>? Animations { get; init; }
    public IReadOnlyList<SkinDef>? MaterialSkins { get; init; }
    public int? VertCount { get; init; }
}

/// <summary>Writes Source 1 QC files.</summary>
public static class QcWriter
{
    private static string Normalize(string path) => path.Replace('\\', '/');

    /// <summary>Writes a QC, with a skeletal block when the content has a skin.</summary>
    /// <remarks>
    /// <para><c>$maxverts</c> appears only above MAXSTUDIOVERTS (65536): StudioMDL CE honours it, but older
    /// studiomdl builds reject the unknown command, and models under the limit must stay compatible.</para>
    /// <para>A skinned model drops <c>$staticprop</c> and declares every bone with <c>$definebone</c>, because
    /// studiomdl otherwise strips bones with no weighted vertices or animation. With no clips it still needs a
    /// sequence, so a one-frame idle over the reference SMD's bind pose gives the skeleton a home without
    /// inventing motion.</para>
    /// </remarks>
    public static void WriteQc(string path, QcContent content)
    {
        var lines = new List<string> { $"$modelname \"{Normalize(content.Modelname)}\"" };
        if (content.Cdmaterials.Length > 0) lines.Add($"$cdmaterials \"{Normalize(content.Cdmaterials)}\"");
        if (content.VertCount is > 65536) lines.Add($"$maxverts {content.VertCount}");
        if (content.Surfaceprop.Length > 0) lines.Add($"$surfaceprop \"{content.Surfaceprop}\"");
        if (content.MaterialSkins is { Count: > 1 } skins)
        {
            lines.Add("");
            lines.Add("$texturegroup \"skinfamilies\"");
            lines.Add("{");
            foreach (var skin in skins)
                lines.Add($"    {{ {string.Join(' ', skin.Materials.Select(material => $"\"{material}\""))} }} // {skin.Name}");
            lines.Add("}");
        }
        if (content.Skin is not null && content.Coordinates is not null) AddSkeletal(lines, content, content.Skin, content.Coordinates);
        else
        {
            lines.Add("$staticprop");
            lines.Add($"$body \"body\" \"{content.Name}.smd\"");
            lines.Add($"$sequence \"idle\" \"{content.Name}.smd\" fps 1");
        }
        if (content.HasPhysics)
            lines.AddRange(StaticCollision.CollisionModelBlock($"{content.Name}_physics.smd", content.Mass, content.Concave, content.PieceCount,
                StaticCollision.ConvexPieceBudget(content.PieceCount), content.Remove2d, content.Inertia, content.Damping, content.RotDamping));
        PythonText.Write(path, string.Join('\n', lines) + "\n");
    }

    private static void AddSkeletal(List<string> lines, QcContent content, GltfSkin skin, CoordinateMode coordinates)
    {
        lines.Add($"$body \"body\" \"{content.Name}.smd\"");
        lines.Add("");
        lines.AddRange(SmdSkeleton.DefineBoneLines(skin, coordinates));
        lines.Add("");
        lines.Add($"$root \"{skin.Joints[skin.RootIndex].Name}\"");
        lines.Add("");
        if (content.Animations is not { Count: > 0 } animations)
        {
            lines.Add($"$sequence \"idle\" \"{content.Name}.smd\" fps 1");
            lines.Add("");
            return;
        }
        foreach (var animation in animations) lines.Add($"$animation \"a_{animation.ClipName}\" \"{animation.SmdFilename}\" fps {Fmt.G(animation.Fps)}");
        lines.Add("");
        foreach (var animation in animations)
        {
            var options = animation.Loop ? $"fps {Fmt.G(animation.Fps)} loop" : $"fps {Fmt.G(animation.Fps)}";
            lines.Add($"$sequence \"{animation.ClipName}\" {{ \"a_{animation.ClipName}\" {options} }}");
        }
        lines.Add("");
    }
}
