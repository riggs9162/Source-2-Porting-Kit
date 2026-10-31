namespace PortingKit.Addons.Porting;

/// <summary>Joins the two layers of a painted Source 2 blend into one WorldVertexTransition material.</summary>
/// <remarks>
/// <para>Alyx paints a value on the mesh, and Source 1 paints alpha on a displacement. Both feed it through
/// smoothstep(mask - softness, mask + softness, painted value), Source 2 from its mask texture and Source 1 from the green and red of
/// $blendmodulatetexture, so the blend line lands on the same pixels. Painted value zero is the first layer in both engines.</para>
/// <para>The two layers are built as plain materials first, and this material shares their textures. A plain brush cannot be painted: vbsp
/// patches this material to LightmappedGeneric there and shows the first layer, which is what the plain first material already does.</para>
/// <para>Source 1 gives both colours, both normals and the mask one set of UVs, and $basetexturetransform is not available on a blend, as
/// <see cref="Vmt.Blend"/> says. A second layer that tiles faster is therefore repeated inside its own texture. It also means a Hammer and
/// compile size cannot be layered over a black texture as it is on a plain brush, because a detail layer lands on top of both colours, so
/// $basetexture has to be the small tool texture itself. That caps the first layer's colour on the blend at the tool size, while its
/// normal and the whole second layer keep their resolution.</para>
/// </remarks>
public static class BlendMaterial
{
    /// <summary>The softness of a mesh painted without any, the smallest Source 2 allows.</summary>
    /// <remarks>Source 2 paints softness per vertex and uses max(0.5 * painted, 0.1). A displacement has no second painted value, so one constant has to stand in for it.</remarks>
    public const double Softness = 0.1;

    /// <summary>The longest edge a mask or a repeated second layer grows to by itself.</summary>
    private const int LargestEdge = 2048;

    private const string FlatNormal = "flat_normal";

    /// <summary>Whether these layers can blend. The shader's mask needs an opaque surface with no self-illumination.</summary>
    public static bool Supports(IReadOnlyList<VmatEntry> layers) =>
        layers.Count == 2 && layers.All(layer => layer.Buildable && !layer.Translucent && !layer.Alphatest && !layer.Decal && !layer.Selfillum);

    /// <summary>The longest edge a repeated second layer may reach: its own size, or more when it started small.</summary>
    /// <remarks>Keeping every pixel of a 2048 layer repeated four times would take 8192, so a large layer gives up density instead of growing.</remarks>
    public static int RepeatEdge(MaterialResult second, int clamp)
    {
        var edge = Math.Max(LargestEdge, Math.Max(second.Width, second.Height));
        return clamp > 0 ? Math.Min(edge, clamp) : edge;
    }

    /// <summary>Writes the mask and the material that blends two built layers.</summary>
    /// <param name="repeated">The second layer repeated for this blend, or null when <paramref name="second"/> already tiles with the first.</param>
    public static MaterialResult Build(VmatBlend blend, MaterialResult first, MaterialResult second, MaterialResult? repeated, int clamp, string surfaceprop, PortContext context)
    {
        var name = first.Name + "_blend";
        var path = first.MaterialPath;
        var result = new MaterialResult(name, path, first.OutputDir);
        var layer = repeated ?? second;
        var notes = new List<string>(blend.Notes);
        var mask = WriteMask(blend, layer, clamp, result, notes, context);
        var (firstParams, secondParams) = (first.Vmt!.Params, second.Vmt!.Params);
        var (firstBump, secondBump) = (firstParams.ContainsKey("$bumpmap"), secondParams.ContainsKey("$bumpmap"));
        string? bumpmap = null, bumpmap2 = null;
        if (firstBump || secondBump)
        {
            bumpmap = firstBump ? $"{path}/{first.Name}_normal" : WriteFlatNormal(result);
            bumpmap2 = secondBump ? $"{path}/{layer.Name}_normal" : WriteFlatNormal(result);
        }
        if (firstParams.GetValueOrDefault("%tooltexture") is not { } tool) tool = $"{path}/{first.Name}_color";
        else notes.Add("The first layer's colour is capped at the Hammer size on this blend, because a blend cannot layer a full-size texture over a small one");
        var envmap = firstParams.GetValueOrDefault("$envmap") ?? secondParams.GetValueOrDefault("$envmap");
        result.Vmt = Vmt.Blend(name, path, tool, $"{path}/{layer.Name}_color", mask, bumpmap, bumpmap2, envmap, firstBump && firstParams.ContainsKey("$envmap"), secondBump && secondParams.ContainsKey("$envmap"), surfaceprop, notes);
        result.Notes.AddRange(result.Vmt.Notes);
        result.Written.Add(result.Vmt.Write(Path.Combine(first.OutputDir, name + ".vmt")));
        context.Log($"  wrote {name}.vmt");
        return result;
    }

    /// <summary>Writes $blendmodulatetexture no larger than the layer it follows, or returns null when the blend has no mask.</summary>
    /// <remarks>DXT1 is safe here, unlike on a smooth normal map: the blocks only shift a noisy edge by a few texels, where a reflection would turn them into hard squares. Compressing costs a sixth of the space, so the mask can keep twice the resolution.</remarks>
    private static string? WriteMask(VmatBlend blend, MaterialResult layer, int clamp, MaterialResult result, List<string> notes, PortContext context)
    {
        var mask = blend.MaskConstant is { } constant ? Textures.UniformScalar(constant) : Textures.LoadScalar(blend.Mask);
        if (mask is null)
        {
            notes.Add("No blend mask was found, so the paint fades evenly across its whole range");
            return null;
        }
        var edge = Math.Min(LargestEdge, Math.Max(layer.Width, layer.Height));
        var (across, down) = blend.MaskRepeat;
        var (height, width) = Textures.RepeatedSize(mask.Height, mask.Width, across, down, clamp > 0 ? Math.Min(edge, clamp) : edge);
        var target = Path.Combine(result.OutputDir, result.Name + "_mask.vtf");
        Directory.CreateDirectory(result.OutputDir);
        Textures.EncodeVtf(PbrMaps.BakeBlendMask(Textures.Repeat(mask, across, down, height, width)!, Softness), target, VtfFormat.DXT1);
        result.Written.Add(target);
        context.Log($"  wrote {Path.GetFileName(target)}");
        return $"{result.MaterialPath}/{result.Name}_mask";
    }

    /// <summary>Writes the shared flat normal a layer without one needs, because $bumpmap2 is ignored without $bumpmap and the reverse leaves a layer wearing the other's normal.</summary>
    private static string WriteFlatNormal(MaterialResult result)
    {
        var target = Path.Combine(result.OutputDir, FlatNormal + ".vtf");
        if (!File.Exists(target))
        {
            Directory.CreateDirectory(result.OutputDir);
            Textures.EncodeVtf(Textures.ToBytes(Textures.FlatNormal()), target, VtfFormat.BGRA8888, Textures.FlagNormal);
            result.Written.Add(target);
        }
        return $"{result.MaterialPath}/{FlatNormal}";
    }
}
