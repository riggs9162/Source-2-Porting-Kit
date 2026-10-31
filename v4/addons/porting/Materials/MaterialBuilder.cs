namespace PortingKit.Addons.Porting;

/// <summary>A material could not be built: missing inputs, bad data or an encode failure.</summary>
public sealed class MaterialError(string message) : Exception(message);

/// <summary>A role's pixels: a file to decode, or an already-decoded image.</summary>
public sealed class PixelSource
{
    public string? Path { get; private init; }
    public FloatImage? Image { get; private init; }

    public static implicit operator PixelSource?(string? path) => path is null ? null : new PixelSource { Path = path };
    public static implicit operator PixelSource?(FloatImage? image) => image is null ? null : new PixelSource { Image = image };
}

/// <summary>Where each role's pixels come from, and the vmat scalars that go with them.</summary>
/// <remarks>
/// A role can be a file path, an in-memory image, a uniform constant or absent. In-memory images are how a packed ORM map reaches the
/// builder, split without writing temporary PNGs. Constants exist because Source 2 vmats write <c>"TextureColor" "[1 1 1 0]"</c> when
/// the artist used a solid colour. Pixels always win over constants for the same role.
/// </remarks>
public sealed class MaterialSource
{
    public PixelSource? Color { get; init; }
    public PixelSource? Normal { get; init; }
    public PixelSource? Ao { get; init; }
    public PixelSource? Roughness { get; init; }
    public PixelSource? Metallic { get; init; }
    public PixelSource? Translucency { get; init; }
    public PixelSource? Selfillum { get; init; }
    public PixelSource? Tintmask { get; init; }
    public PixelSource? Heightmap { get; init; }
    public double[]? ColorConstant { get; init; }
    public double[]? NormalConstant { get; init; }
    public double[]? AoConstant { get; init; }
    public double[]? TranslucencyConstant { get; init; }
    public double[]? SelfillumConstant { get; init; }
    public double? RoughnessConstant { get; init; }
    public double? MetallicConstant { get; init; }
    public double[]? SelfillumTint { get; init; }
    public double? SelfillumBrightness { get; init; }
    public PixelSource? SelfillumFlowPhase { get; init; }
    public PixelSource? SelfillumFlowWaveform { get; init; }
    public double? SelfillumFlowAmount { get; init; }
    public double? SelfillumFlowSpeed { get; init; }
    public double? SelfillumFlowFrequency { get; init; }
    public double? SelfillumOverallScale { get; init; }
    public double[]? GlassTint { get; init; }
    public double? GlassMaxOpacity { get; init; }
    public double? GlassFresnelExponent { get; init; }

    /// <summary>Source 2 g_flModelTintAmount, which scales the tint mask before baking.</summary>
    public double? ModelTintAmount { get; init; }

    public double[]? ColorTint { get; init; }

    /// <summary>Whether a role has any signal at all, a path or a constant.</summary>
    public bool Has(string role) => role switch
    {
        "color" => Color is not null || ColorConstant is not null,
        "normal" => Normal is not null || NormalConstant is not null,
        "ao" => Ao is not null || AoConstant is not null,
        "roughness" => Roughness is not null || RoughnessConstant is not null,
        "metallic" => Metallic is not null || MetallicConstant is not null,
        "translucency" => Translucency is not null || TranslucencyConstant is not null,
        "selfillum" => Selfillum is not null || SelfillumConstant is not null,
        "tintmask" => Tintmask is not null,
        "heightmap" => Heightmap is not null,
        "selfillum_flow_phase" => SelfillumFlowPhase is not null,
        "selfillum_flow_waveform" => SelfillumFlowWaveform is not null,
        _ => false
    };
}

/// <summary>Everything that changes what a build produces.</summary>
public sealed class MaterialOptions
{
    /// <summary>fake uses stock shaders with PBR faked through Phong and a cubemap; exo targets the ExoPBR screenspace shader.</summary>
    public string Mode { get; set; } = "fake";

    public bool GenerateVtf { get; set; } = true;
    public bool GenerateVmt { get; set; } = true;
    public bool GenerateMipmaps { get; set; } = true;
    public VtfFormat TextureFormat { get; set; } = VtfFormat.DXT5;

    /// <summary>Caps the working resolution before any maths runs. Zero keeps native size.</summary>
    public int ClampSize { get; set; }

    /// <summary>Per-output caps keyed color, normal, phong, envmask and selfillum; zero or absent is uncapped.</summary>
    public Dictionary<string, int> OutputClamps { get; set; } = [];

    /// <summary>Fills a missing normal with a flat tangent normal instead of failing.</summary>
    public bool SynthesizeMissingMaps { get; set; }

    public string Branch { get; set; } = "gmod";
    public string Shader { get; set; } = "VertexLitGeneric";
    public string Envmap { get; set; } = "env_cubemap";
    public string Surfaceprop { get; set; } = "";
    public double AoStrength { get; set; } = 0.7;
    public double AoBokehStrength { get; set; } = 0.35;

    /// <summary>How much AO to take back where the glow mask marks the surface as emissive; one removes it there entirely.</summary>
    public double EmissiveAoRelief { get; set; } = 1.0;

    public double GlossGamma { get; set; } = 2.0;
    public double MetalDiffuseSuppression { get; set; } = MaterialDefaults.MetalDiffuseSuppression;
    public double EnvmaskGamma { get; set; } = 1.5;
    public double PhongStrength { get; set; } = 0.75;
    public string PhongTintMode { get; set; } = "selective";
    public double ColoredMetalRelief { get; set; } = MaterialDefaults.ColoredMetalRelief;

    /// <summary>The longest edge Hammer and vbsp should believe a brush texture has. Zero uses its real size.</summary>
    /// <remarks>A brush face stores its scale and shifts in texture pixels, and both tools take a material's size from its preview image
    /// rather than the texture the game draws, so a larger texture stretches everything in Hammer and in every new compile. A
    /// compiled map stores the size it was built with, which is why old maps survive a texture upgrade until they are recompiled.
    /// %tooltexture points the preview at a texture of the original size. It starts with % so the game never reads it, and vbsp then
    /// records that size in the BSP while the game draws the full resolution. $basetexturetransform looks like the answer and is not:
    /// it also multiplies the UVs of maps already compiled, shrinking every texture in them.</remarks>
    public int ToolSize { get; set; }

    /// <summary>Ship a normal map at all. Off drops $bumpmap and $phong with it.</summary>
    /// <remarks>That is the combination a prop_static needs: every branch before the CS:GO engine throws away VRAD's baked per-vertex lighting on a model whose material carries $bumpmap, $normalmap or $phong, and lights it from a single point instead.</remarks>
    public bool EmitNormal { get; set; } = true;

    /// <summary>Bakes fixed normal relief into the base colour when normal output is off.</summary>
    public bool BakeNormalDetail { get; set; }

    public double NormalDetailStrength { get; set; } = 0.25;

    /// <summary>Green-channel handling: auto, flip or keep. Source 2 authors normals the other way up from Source 1, so the default flips.</summary>
    public string NormalGreen { get; set; } = "auto";

    public bool Translucent { get; set; }
    public bool Alphatest { get; set; }
    public bool Tintable { get; set; }
    public bool Decal { get; set; }
    public string GlowMode { get; set; } = "emissiveblend";
    public double EmissionScale { get; set; }
    public double ParallaxScale { get; set; }
    public bool AlphaBlend { get; set; }

    public bool Transparent => Translucent || Alphatest;

    /// <summary>Whether a missing normal map should fail this build.</summary>
    /// <remarks>Exo PBR always does, its shader reads one. Fake PBR only does when shipping a $bumpmap; the optional relief bake uses an authored normal when present without requiring one.</remarks>
    public bool WantsNormal => Mode != "fake" || EmitNormal;
}

/// <summary>What a build produced, and what it decided not to produce.</summary>
public sealed class MaterialResult(string name, string materialPath, string outputDir)
{
    public string Name { get; } = name;
    public string MaterialPath { get; } = materialPath;
    public string OutputDir { get; } = outputDir;
    public int Width { get; set; }
    public int Height { get; set; }
    public List<string> Written { get; } = [];
    public VmtResult? Vmt { get; set; }
    public MaterialStats? Stats { get; set; }

    /// <summary>Outputs deliberately skipped, with the reason shown in the log.</summary>
    public List<(string Name, string Reason)> Dropped { get; } = [];

    public List<string> Notes { get; } = [];

    public IEnumerable<string> VtfFiles => Written.Where(path => path.EndsWith(".vtf", StringComparison.OrdinalIgnoreCase));
    public string? VmtFile => Written.FirstOrDefault(path => path.EndsWith(".vmt", StringComparison.OrdinalIgnoreCase));

    public string Summary()
    {
        var parts = new List<string> { $"{VtfFiles.Count()} VTF" };
        if (VmtFile is not null) parts.Add("VMT");
        if (Width > 0) parts.Add($"{Width}x{Height}");
        return string.Join(", ", parts);
    }
}

/// <summary>Decoded inputs at one shared working resolution.</summary>
public sealed record LoadedMaps(FloatImage Color, FloatImage Normal, int Width, int Height, FloatImage? Ao, FloatImage? Roughness, FloatImage? Metallic,
    FloatImage? Translucency, FloatImage? Selfillum, FloatImage? Tintmask, FloatImage? Heightmap);

/// <summary>How to turn a built Fake PBR material back into editable PBR maps.</summary>
public sealed class ExtractOptions
{
    public double GlossGamma { get; set; } = 2.0;
    public double PhongStrength { get; set; } = 0.75;
    public double MetalDiffuseSuppression { get; set; } = MaterialDefaults.MetalDiffuseSuppression;

    /// <summary>Undoes the metal darkening in the basetexture.</summary>
    public bool RecoverAlbedo { get; set; } = true;

    /// <summary>Flips the normal's green channel back. The converter flips Source 2 to Source 1, so an extract must flip back or a round trip leaves every surface inverted.</summary>
    public bool InvertGreen { get; set; } = true;

    /// <summary>Writes an opacity map when the basetexture alpha is not fully opaque.</summary>
    public bool WriteOpacity { get; set; } = true;

    public int ClampSize { get; set; }
    public bool Overwrite { get; set; }
}

/// <summary>Builds one Source 1 material from a set of PBR maps: the engine every material tool drives.</summary>
/// <remarks>
/// Nothing is emitted speculatively. A material whose source has no roughness or metallic gets no Phong block and no <c>_phong.vtf</c>;
/// a flat normal gets no bumpmap; a mask that bakes to pure black is dropped along with the VMT parameter that would have referenced
/// it. Inventing a flat default fakes a response the source never asked for, and costs disk to do it.
/// </remarks>
public static class MaterialBuilder
{
    /// <summary>Output keys that accept an individual resolution cap.</summary>
    public static readonly string[] OutputKeys = ["color", "normal", "phong", "envmask", "selfillum"];

    /// <summary>The longest-edge cap to decode at, from the tool cap and the output caps.</summary>
    /// <remarks>
    /// Everything computed is downscaled to its own output cap just before encoding, so running the maths above the largest cap buys
    /// nothing and costs the square of the ratio in memory and time. Returns zero when overrides are off, or when any emitted output is
    /// individually uncapped. Predicting the emit flags from input presence is safe: a path that later fails only makes the cap more generous.
    /// </remarks>
    private static int WorkingCap(MaterialSource source, MaterialOptions options)
    {
        var caps = options.OutputClamps;
        var outputCap = 0;
        if (caps.Count > 0)
        {
            var keys = new List<string> { "color", "normal" };
            if (source.Has("roughness") || source.Has("metallic")) keys.Add("phong");
            if (source.Has("metallic")) keys.Add("envmask");
            if (source.Has("selfillum")) keys.Add("selfillum");
            var values = keys.Select(key => caps.GetValueOrDefault(key)).ToArray();
            outputCap = values.Any(value => value <= 0) ? 0 : values.Max();
        }
        var bounds = new[] { outputCap, options.ClampSize }.Where(cap => cap > 0).ToArray();
        return bounds.Length > 0 ? bounds.Min() : 0;
    }

    private static ByteImage ApplyOutputCap(ByteImage pixels, string key, MaterialOptions options, PortContext context)
    {
        var cap = options.OutputClamps.GetValueOrDefault(key);
        if (cap <= 0 || Math.Max(pixels.Height, pixels.Width) <= cap) return pixels;
        var clamped = Textures.ClampBytes(pixels, cap)!;
        context.Log($"  {key}: {pixels.Width}x{pixels.Height} -> {clamped.Width}x{clamped.Height} (cap {cap})");
        return clamped;
    }

    /// <summary>Decodes every role the source declares, at one shared working resolution.</summary>
    /// <remarks>
    /// Colour is always required; normal output also requires a normal unless synthesis is enabled. A normal-free build can use an optional
    /// authored normal for relief, falling back to flat. Other missing roles stay null rather than inventing an unauthored signal. Brush
    /// builds keep the colour map's dimensions and aspect ratio; other shaders use the largest input. Uniform brush colours can still
    /// follow the resolution of their authored detail maps.
    /// </remarks>
    public static LoadedMaps LoadMaps(MaterialSource source, MaterialOptions options, PortContext context)
    {
        var cap = WorkingCap(source, options);
        if (cap > 0) context.Log($"Decoding at <={cap}px longest edge");

        FloatImage? Scalar(string role, PixelSource? value, double? constant)
        {
            context.Check();
            if (value?.Image is { } image)
            {
                var data = image.Channels > 1 ? image.Channel(0) : image;
                context.Log($"  {role}: in memory {data.Width}x{data.Height}");
                return Textures.ClampFloat(data, cap);
            }
            if (value?.Path is { } path)
            {
                var data = Textures.LoadScalar(path, cap);
                if (data is not null)
                {
                    context.Log($"  {role}: {Path.GetFileName(path)}");
                    return data;
                }
                context.Log($"  {role}: could not read {Path.GetFileName(path)}");
            }
            if (constant is null) return null;
            context.Log($"  {role}: uniform {MaterialDefaults.Fixed(constant.Value, 3)}");
            return Textures.UniformScalar(constant.Value);
        }

        FloatImage? Rgba(string role, PixelSource? value, double[]? constant)
        {
            context.Check();
            if (value?.Image is { } image)
            {
                context.Log($"  {role}: in memory {image.Width}x{image.Height}");
                return Textures.ClampFloat(Textures.AsRgba(image), cap);
            }
            if (value?.Path is { } path)
            {
                var data = Textures.LoadRgba(path, cap);
                if (data is not null)
                {
                    context.Log($"  {role}: {Path.GetFileName(path)}");
                    return data;
                }
                context.Log($"  {role}: could not read {Path.GetFileName(path)}");
            }
            if (constant is null) return null;
            context.Log($"  {role}: uniform " + string.Join(" ", constant.Select(component => MaterialDefaults.Fixed(component, 3))));
            return Textures.UniformRgba(constant);
        }

        var color = Rgba("color", source.Color, source.ColorConstant) ?? throw new MaterialError("No colour map (a material cannot be built without one)");
        if (source.ColorTint is not null) color = Tinted(color, source.ColorTint);
        var normal = Rgba("normal", source.Normal, source.NormalConstant);
        if (normal is null)
        {
            if (options.WantsNormal && !options.SynthesizeMissingMaps) throw new MaterialError("No normal map (enable 'Synthesise missing maps' to build without one)");
            normal = Textures.FlatNormal();
            context.Log("  normal: synthesised flat (no bump)");
        }
        var ao = Scalar("ao", source.Ao, source.AoConstant?[0]);
        var roughness = Scalar("roughness", source.Roughness, source.RoughnessConstant);
        var metallic = Scalar("metallic", source.Metallic, source.MetallicConstant);
        var translucency = Scalar("translucency", source.Translucency, source.TranslucencyConstant?[0]);
        if (source.GlassTint is not null) color = Tinted(color, source.GlassTint);
        if (source.GlassMaxOpacity is { } maximum)
        {
            var opacity = Math.Clamp(maximum, 0.0, 1.0);
            if (translucency is null) translucency = Textures.UniformScalar(opacity);
            else
            {
                translucency = translucency.Copy();
                for (var i = 0; i < translucency.Data.Length; i++) translucency.Data[i] = MathF.Max(translucency.Data[i], (float)opacity);
            }
        }
        var heightmap = Scalar("heightmap", source.Heightmap, null);
        var selfillum = Rgba("selfillum", source.Selfillum, source.SelfillumConstant);
        FloatImage? tintmask = null;
        if (source.Tintmask is not null)
        {
            if (options.Transparent) context.Log("  tintmask: dropped, the alpha channel belongs to transparency");
            else tintmask = Scalar("tintmask", source.Tintmask, null);
        }
        var present = new[] { color, normal, ao, roughness, metallic, translucency, selfillum, tintmask, heightmap }.OfType<FloatImage>().ToArray();
        var height = present.Max(item => item.Height);
        var width = present.Max(item => item.Width);
        if (options.Shader == "LightmappedGeneric" && source.Color is not null) (height, width) = (color.Height, color.Width);
        if (cap > 0 && Math.Max(height, width) > cap)
        {
            var scale = cap / (double)Math.Max(height, width);
            height = Math.Max(1, (int)Math.Round(height * scale));
            width = Math.Max(1, (int)Math.Round(width * scale));
        }
        return new(Textures.Fit(color, height, width)!, Textures.Fit(normal, height, width)!, width, height, Textures.Fit(ao, height, width), Textures.Fit(roughness, height, width),
            Textures.Fit(metallic, height, width), Textures.Fit(translucency, height, width), Textures.Fit(selfillum, height, width), Textures.Fit(tintmask, height, width), Textures.Fit(heightmap, height, width));
    }

    private static FloatImage Tinted(FloatImage color, double[] tint)
    {
        var output = color.Copy();
        for (var i = 0; i < output.Height * output.Width; i++)
            for (var c = 0; c < 3; c++) output.Data[i * output.Channels + c] *= (float)tint[c];
        return output;
    }

    /// <summary>Builds one material and writes its files. Raises <see cref="MaterialError"/> when it cannot.</summary>
    public static MaterialResult Build(MaterialSource source, string outputDir, string name, string materialPath, MaterialOptions options, PortContext context) =>
        options.Mode == "exo" ? BuildExo(source, outputDir, name, materialPath, options, context) : BuildFake(source, outputDir, name, materialPath, options, context);

    /// <summary>Whether a previous run already produced this material.</summary>
    /// <remarks>The VMT is the canonical marker when it is being generated: it is the file a user hand-edits afterwards, so it is the one worth not clobbering. With VMT generation off, fall back to the basetexture.</remarks>
    public static bool IsAlreadyBuilt(string outputDir, string name, MaterialOptions options)
    {
        if (options.GenerateVmt && File.Exists(Path.Combine(outputDir, name + ".vmt"))) return true;
        return options.GenerateVtf && File.Exists(Path.Combine(outputDir, name + (options.Mode == "exo" ? "_base.vtf" : "_color.vtf")));
    }

    private static void Encode(ByteImage pixels, string target, MaterialResult result, MaterialOptions options, PortContext context, VtfFormat? format = null, uint flags = 0)
    {
        Textures.EncodeVtf(pixels, target, format ?? options.TextureFormat, flags, false, options.GenerateMipmaps);
        result.Written.Add(target);
        context.Log($"  wrote {Path.GetFileName(target)}");
    }

    /// <summary>The size a texture would have had at the tool size, or null when it already does.</summary>
    /// <remarks>Worked out per texture with the clamp that sized it, so a 256 source a 512 limit never touched needs nothing, and only
    /// brush materials ask: a model's UVs are normalized and never depend on texture size.</remarks>
    private static (int Height, int Width)? ToolDimensions(MaterialSource source, MaterialOptions options, int width, int height)
    {
        if (options.ToolSize <= 0 || options.Shader != "LightmappedGeneric") return null;
        (int Width, int Height)? native = source.Color?.Image is { } image ? (image.Width, image.Height)
            : source.Color?.Path is { } path ? (SourceImage.ProbeFile(path).Width, SourceImage.ProbeFile(path).Height) : null;
        if (native is not var (nativeWidth, nativeHeight)) return null;
        var (toolHeight, toolWidth) = Textures.ScaledSize(nativeHeight, nativeWidth, options.ToolSize) ?? (nativeHeight, nativeWidth);
        return toolWidth == width && toolHeight == height ? null : (toolHeight, toolWidth);
    }

    private static MaterialResult BuildFake(MaterialSource source, string outputDir, string name, string materialPath, MaterialOptions options, PortContext context)
    {
        var result = new MaterialResult(name, materialPath, outputDir);
        var emitPhong = source.Has("roughness") || source.Has("metallic");
        var emitEnvmap = source.Has("metallic");
        var phongDrop = emitPhong ? "" : "no roughness or metallic signal";
        if (emitPhong && !options.EmitNormal) (emitPhong, phongDrop) = (false, "shipped without a normal map");
        var capabilities = Vmt.Capabilities(options.Branch);
        var tintMode = capabilities.Contains("phong_albedo_tint") ? options.PhongTintMode : "off";
        if (options.PhongTintMode != "off" && tintMode == "off") context.Log($"Branch '{options.Branch}' has no $phongalbedotint; Phong tint compensation off");
        var compute = options.GenerateVtf;
        context.Log("Loading inputs");
        var maps = LoadMaps(source, options, context);
        result.Width = maps.Width;
        result.Height = maps.Height;
        context.Log($"Working resolution {maps.Width}x{maps.Height}");
        result.Stats = PbrMaps.Stats(maps.Roughness, maps.Metallic, maps.Height, maps.Width);
        var flatNormal = PbrMaps.IsFlatNormal(maps.Normal);
        if (source.GlassTint is not null && flatNormal) (emitPhong, phongDrop) = (false, "flat glass uses the separate reflection mask");
        var emitBumpmap = options.EmitNormal && !(flatNormal && !emitPhong);
        if (!emitBumpmap) result.Dropped.Add(("bumpmap", !options.EmitNormal ? "the material is shipping without one" : "the normal map is flat"));
        ByteImage? basetexture = null, normal = null, phong = null, envmask = null, selfillum = null;
        var tintable = options.Tintable;
        var bakeDetail = false;
        var toolDimensions = ToolDimensions(source, options, maps.Width, maps.Height);
        if (options.BakeNormalDetail)
        {
            string detailNote;
            if (options.EmitNormal) detailNote = "Normal detail bake skipped: Ship a normal map is on";
            else if (!compute) detailNote = "Normal detail bake requires VTF texture generation";
            else if (options.NormalDetailStrength <= 0.0) detailNote = "Normal detail bake skipped: strength is zero";
            else if (!source.Has("normal")) detailNote = "Normal detail bake skipped: no source normal map";
            else if (flatNormal) detailNote = "Normal detail bake skipped: normal map is flat or unavailable";
            else
            {
                bakeDetail = true;
                detailNote = $"Normal detail baked into base texture (strength {MaterialDefaults.Fixed(options.NormalDetailStrength, 2)})";
            }
            if (!bakeDetail) result.Notes.Add(detailNote);
            context.Log(detailNote);
        }
        if (compute)
        {
            var ao = PbrMaps.RelieveAo(maps.Ao, maps.Selfillum, maps.Height, maps.Width, options.EmissiveAoRelief);
            if (!ReferenceEquals(ao, maps.Ao)) context.Log($"  AO relieved on the glow mask ({MaterialDefaults.Fixed(options.EmissiveAoRelief, 2)})");
            context.Check();
            context.Log("Baking basetexture");
            if (tintable && maps.Tintmask is null) context.Log("  no tint mask - the whole surface will take the render colour");
            var relief = tintMode != "off" ? options.ColoredMetalRelief : 0.0;
            var invertGreen = PbrMaps.ShouldInvertGreen(options.NormalGreen, source.Normal?.Path);
            basetexture = PbrMaps.BakeBasetexture(maps.Color, ao, maps.Metallic, options.AoStrength, options.AoBokehStrength, options.MetalDiffuseSuppression, relief, maps.Translucency,
                tintable ? maps.Tintmask : null, source.ModelTintAmount, bakeDetail ? maps.Normal : null, options.NormalDetailStrength, invertGreen, maps.Selfillum);
            context.Check();
            if (emitBumpmap)
            {
                context.Log("Packing bumpmap" + (emitPhong ? $" (Phong mask, tint {tintMode})" : " (no Phong mask)"));
                context.Log("  green channel: " + (invertGreen ? "flipped for Source 1" : "kept as authored"));
                normal = PbrMaps.BakeBumpmap(maps.Normal, ao, maps.Metallic, maps.Roughness, invertGreen, emitPhong, options.PhongStrength, tintMode != "off" ? maps.Color : null,
                    tintMode, options.MetalDiffuseSuppression, relief);
            }
            context.Check();
            if (emitPhong)
            {
                context.Log("Baking Phong exponent");
                phong = PbrMaps.BakePhongExponent(maps.Roughness, maps.Metallic, maps.Height, maps.Width, options.GlossGamma, options.PhongStrength);
                if (PbrMaps.IsEffectivelyBlack(phong))
                {
                    phong = null;
                    emitPhong = false;
                    result.Dropped.Add(("phong", "bakes to black"));
                }
            }
            else result.Dropped.Add(("phong", phongDrop));
            context.Check();
            if (emitEnvmap)
            {
                context.Log("Baking envmap mask");
                envmask = PbrMaps.BakeEnvmapMask(maps.Color, ao, maps.Metallic, maps.Roughness, options.EnvmaskGamma);
                if (PbrMaps.IsEffectivelyBlack(envmask))
                {
                    envmask = null;
                    emitEnvmap = false;
                    result.Dropped.Add(("envmask", "bakes to black"));
                }
            }
            else result.Dropped.Add(("envmask", "no metallic signal"));
            if (options.Shader == "LightmappedGeneric" && normal is not null && envmask is not null && !options.Transparent && result.Stats?.IsRoughDielectric != true)
            {
                context.Log("Packing envmap mask into the bumpmap alpha");
                normal = PbrMaps.PackEnvmapMask(normal, envmask);
            }
            if (maps.Selfillum is not null)
            {
                selfillum = PbrMaps.BakeSelfillum(maps.Selfillum);
                if (PbrMaps.IsEffectivelyBlack(selfillum))
                {
                    selfillum = null;
                    result.Dropped.Add(("selfillum", "the glow mask is black"));
                }
            }
        }
        else context.Log("VTF generation off - writing the VMT only");
        context.Check();
        if (options.GenerateVtf && basetexture is not null)
        {
            Directory.CreateDirectory(outputDir);
            Encode(ApplyOutputCap(basetexture, "color", options, context), Path.Combine(outputDir, name + "_color.vtf"), result, options, context, flags: Textures.FlagSrgb);
            if (toolDimensions is var (toolHeight, toolWidth))
                Encode(Textures.ToBytes(Textures.Fit(Textures.ToFloat(basetexture), toolHeight, toolWidth)!), Path.Combine(outputDir, name + "_tool.vtf"), result, options, context, flags: Textures.FlagSrgb);
            if (normal is not null) Encode(ApplyOutputCap(normal, "normal", options, context), Path.Combine(outputDir, name + "_normal.vtf"), result, options, context, flags: Textures.FlagNormal);
            if (phong is not null) Encode(ApplyOutputCap(phong, "phong", options, context), Path.Combine(outputDir, name + "_phong.vtf"), result, options, context);
            if (selfillum is not null)
            {
                Encode(ApplyOutputCap(selfillum, "selfillum", options, context), Path.Combine(outputDir, name + "_selfillum.vtf"), result, options, context, flags: Textures.FlagSrgb);
                if (HasSelfillumFlow(source, options)) BuildSelfillumFlow(source, outputDir, name, result, options, context);
            }
        }
        if (options.GenerateVmt)
        {
            context.Check();
            var extra = FakeExtraParams(source, options, name, materialPath, compute ? selfillum is not null : source.Has("selfillum"));
            if (tintable && extra.GetValueOrDefault("$selfillum") == "1")
                result.Notes.Add("$selfillum breaks the $blendtintbybasealpha combo - switch the glow mode to Emissive Blend to keep the prop colourable");
            result.Vmt = Vmt.FakePbr(name, materialPath, options.Shader, options.Branch, options.Envmap, result.Stats, emitBumpmap && (normal is not null || !compute), emitPhong, emitEnvmap,
                envmask is not null || !compute, options.Surfaceprop, tintable, tintMode, extra, toolDimensions is not null);
            result.Notes.AddRange(result.Vmt.Notes);
            if (source.GlassMaxOpacity is not null)
                result.Notes.Add("Source 2 glass Fresnel opacity is approximated by its maximum opacity; Source 1 VertexLitGeneric has no matching opacity curve");
            if (HasSelfillumFlow(source, options))
                result.Notes.Add("Source 2 self-illumination uses its authored phase, waveform, flow speed and tint through the native EmissiveBlend pass");
            result.Written.Add(result.Vmt.Write(Path.Combine(outputDir, name + ".vmt")));
            context.Log($"  wrote {name}.vmt");
        }
        if (options.GenerateVtf && basetexture is not null && envmask is not null)
        {
            if (!options.GenerateVmt || result.Vmt!.Params.ContainsKey("$envmapmask")) Encode(ApplyOutputCap(envmask, "envmask", options, context), Path.Combine(outputDir, name + "_envmask.vtf"), result, options, context);
            else result.Dropped.Add(("envmask", "nothing in the material reads a separate mask"));
        }
        foreach (var (what, why) in result.Dropped) context.Log($"  skipped {what}: {why}");
        return result;
    }

    /// <summary>Transparency and glow parameters, which sit outside the standard blocks.</summary>
    /// <remarks>An alpha-blended draw and the normal-alpha envmap mask cannot coexist, so transparency turns the latter off.</remarks>
    private static OrderedDictionary<string, string> FakeExtraParams(MaterialSource source, MaterialOptions options, string name, string materialPath, bool hasSelfillum)
    {
        var extra = new OrderedDictionary<string, string>();
        if (options.Decal) extra["$decal"] = "1";
        if (options.Translucent) extra["$translucent"] = "1";
        else if (options.Alphatest)
        {
            extra["$alphatest"] = "1";
            extra["$alphatestreference"] = "0.5";
        }
        if (options.Transparent)
        {
            extra["$nocull"] = "0";
            extra["$normalmapalphaenvmapmask"] = "0";
        }
        if (hasSelfillum)
        {
            var brightness = (source.SelfillumBrightness ?? 1.0) * (source.SelfillumOverallScale ?? 1.0);
            var glow = options.Shader == "LightmappedGeneric"
                ? Vmt.BrushSelfillumParams($"{materialPath}/{name}_selfillum", $"{materialPath}/{name}_color", options.GlowMode, source.SelfillumTint, brightness)
                : Vmt.SelfillumParams($"{materialPath}/{name}_selfillum", $"{materialPath}/{name}_color", options.GlowMode, source.SelfillumTint, brightness);
            foreach (var (key, value) in glow) extra[key] = value;
            if (HasSelfillumFlow(source, options))
            {
                extra["$EmissiveBlendBaseTexture"] = $"{materialPath}/{name}_selfillum";
                extra["$EmissiveBlendTexture"] = $"{materialPath}/{name}_waveform";
                extra["$EmissiveBlendFlowTexture"] = $"{materialPath}/{name}_flow";
                extra["$EmissiveBlendScrollVector"] = $"[{Fmt.G(source.SelfillumFlowSpeed ?? 1.0)} 0]";
            }
        }
        return extra;
    }

    /// <summary>Whether the native emissive pass can sample the authored phase waveform.</summary>
    private static bool HasSelfillumFlow(MaterialSource source, MaterialOptions options) =>
        options.Shader != "LightmappedGeneric" && options.GlowMode.StartsWith("emissiveblend", StringComparison.Ordinal) && source.Has("selfillum_flow_phase") && source.Has("selfillum_flow_waveform");

    /// <summary>Encodes scalar phase as waveform UV and retains a linear emission waveform.</summary>
    /// <remarks>
    /// Source 1's emissive pass samples its emission texture at flow.xy + time * scroll, so a one-dimensional waveform uses the phase in R
    /// and a constant G. Flow is uncompressed linear data, because DXT mangles flow vectors. The waveform is sRGB-encoded because the native
    /// emissive shader always enables sRGB on that sampler.
    /// </remarks>
    private static void BuildSelfillumFlow(MaterialSource source, string outputDir, string name, MaterialResult result, MaterialOptions options, PortContext context)
    {
        var phase = source.SelfillumFlowPhase?.Image ?? Textures.LoadScalar(source.SelfillumFlowPhase?.Path, options.ClampSize);
        var waveformImage = source.SelfillumFlowWaveform?.Image ?? Textures.LoadRgba(source.SelfillumFlowWaveform?.Path);
        if (phase is null || waveformImage is null) throw new MaterialError("Could not read the self-illumination phase or waveform");
        if (phase.Channels > 1) phase = phase.Channel(0);
        var frequency = source.SelfillumFlowFrequency ?? 1.0;
        var flow = new FloatImage(phase.Height, phase.Width, 4);
        for (var i = 0; i < phase.Data.Length; i++)
        {
            flow.Data[i * 4] = frequency == 1.0 ? phase.Data[i] : phase.Data[i] * (float)frequency % 1f;
            flow.Data[i * 4 + 1] = 0.5f;
            flow.Data[i * 4 + 3] = 1f;
        }
        var amount = Math.Clamp(source.SelfillumFlowAmount ?? 1.0, 0.0, 1.0);
        var waveform = Textures.AsRgba(waveformImage).Copy();
        var linear = new float[waveform.Height * waveform.Width * 3];
        for (var i = 0; i < waveform.Height * waveform.Width; i++)
            for (var c = 0; c < 3; c++) linear[i * 3 + c] = (float)(1.0 - amount + amount * waveform.Data[i * 4 + c]);
        Textures.LinearToSrgb(linear);
        for (var i = 0; i < waveform.Height * waveform.Width; i++)
        {
            for (var c = 0; c < 3; c++) waveform.Data[i * 4 + c] = linear[i * 3 + c];
            waveform.Data[i * 4 + 3] = 1f;
        }
        if (waveform.Height < 4)
        {
            var repeated = new FloatImage(4, waveform.Width, 4);
            for (var row = 0; row < 4; row++) Array.Copy(waveform.Data, 0, repeated.Data, row * waveform.Width * 4, waveform.Width * 4);
            waveform = repeated;
        }
        Encode(ApplyOutputCap(Textures.ToBytes(flow), "selfillum", options, context), Path.Combine(outputDir, name + "_flow.vtf"), result, options, context, VtfFormat.RGBA8888);
        Encode(ApplyOutputCap(Textures.ToBytes(waveform), "selfillum", options, context), Path.Combine(outputDir, name + "_waveform.vtf"), result, options, context, VtfFormat.RGBA8888);
    }

    /// <summary>Builds for the ExoPBR shader.</summary>
    /// <remarks>An emissive map with no emission scale would encode a texture the shader never samples, so it defaults on; likewise parallax when the source carried a height map.</remarks>
    private static MaterialResult BuildExo(MaterialSource source, string outputDir, string name, string materialPath, MaterialOptions options, PortContext context)
    {
        var result = new MaterialResult(name, materialPath, outputDir);
        context.Log("Loading inputs");
        var maps = LoadMaps(source, options, context);
        result.Width = maps.Width;
        result.Height = maps.Height;
        context.Log($"Working resolution {maps.Width}x{maps.Height}");
        result.Stats = PbrMaps.Stats(maps.Roughness, maps.Metallic, maps.Height, maps.Width);
        var hasEmissive = maps.Selfillum is not null;
        if (options.GenerateVtf)
        {
            context.Check();
            Directory.CreateDirectory(outputDir);
            context.Log("Packing base, ARM and normal");
            Encode(ApplyOutputCap(PbrMaps.BakeExoBase(maps.Color, maps.Translucency, maps.Height, maps.Width), "color", options, context), Path.Combine(outputDir, name + "_base.vtf"), result, options, context);
            context.Check();
            Encode(ApplyOutputCap(PbrMaps.BakeArm(maps.Ao, maps.Roughness, maps.Metallic, maps.Heightmap, maps.Height, maps.Width), "color", options, context), Path.Combine(outputDir, name + "_arm.vtf"), result, options, context);
            context.Check();
            Encode(ApplyOutputCap(PbrMaps.BakeExoNormal(maps.Normal, maps.Height, maps.Width), "normal", options, context), Path.Combine(outputDir, name + "_normal.vtf"), result, options, context);
            if (hasEmissive)
            {
                context.Check();
                Encode(ApplyOutputCap(PbrMaps.BakeSelfillum(maps.Selfillum!), "selfillum", options, context), Path.Combine(outputDir, name + "_emissive.vtf"), result, options, context);
            }
        }
        if (options.GenerateVmt)
        {
            var emission = options.EmissionScale;
            if (hasEmissive && emission == 0.0)
            {
                emission = 1.0;
                context.Log("  $emissionscale 1.0 (emissive map present)");
            }
            var parallax = options.ParallaxScale;
            if (maps.Heightmap is not null && parallax == 0.0)
            {
                parallax = 0.03;
                context.Log("  $parallaxscale 0.03 (height map present)");
            }
            result.Vmt = Vmt.ExoPbr(name, materialPath, emission, parallax, options.AlphaBlend || options.Transparent, hasEmissive);
            result.Written.Add(result.Vmt.Write(Path.Combine(outputDir, name + ".vmt")));
            context.Log($"  wrote {name}.vmt");
        }
        return result;
    }

    /// <summary>Decodes a built material's VTFs back into PBR PNGs.</summary>
    /// <remarks>
    /// Exactly inverts the Phong exponent bake, which recovers roughness and metallic cleanly. Colour is the lossy direction: AO was
    /// multiplied into the same pixels with no second equation to separate it, so what comes out is the AO-baked albedo with the metal
    /// darkening removed. The bumpmap alpha is skipped for the same reason.
    /// </remarks>
    public static List<string> ExtractMaterial(IReadOnlyDictionary<string, string> sources, string outputDir, string name, ExtractOptions options, PortContext context)
    {
        Directory.CreateDirectory(outputDir);
        var targets = new List<string> { Path.Combine(outputDir, name + "_color.png"), Path.Combine(outputDir, name + "_normal.png") };
        if (sources.ContainsKey("phong"))
        {
            targets.Add(Path.Combine(outputDir, name + "_roughness.png"));
            targets.Add(Path.Combine(outputDir, name + "_metallic.png"));
        }
        if (!options.Overwrite && targets.All(File.Exists))
        {
            context.Log($"{name}: outputs exist");
            return [];
        }
        ByteImage? Decode(string role) => sources.TryGetValue(role, out var path) ? Textures.ClampBytes(Textures.DecodeVtf(path).Pixels, options.ClampSize) : null;
        var color = Decode("color");
        var normal = Decode("normal");
        var phong = Decode("phong");
        var written = new List<string>();
        FloatImage? metallic = null;
        if (phong is not null)
        {
            var red = Textures.ToFloat(phong.Channel(0));
            var roughness = PbrMaps.UnbakeRoughness(red, options.PhongStrength, options.GlossGamma);
            metallic = Textures.ToFloat(phong.Channel(1)).Clipped();
            written.Add(Textures.SaveScalarPng(roughness, Path.Combine(outputDir, name + "_roughness.png")));
            written.Add(Textures.SaveScalarPng(metallic, Path.Combine(outputDir, name + "_metallic.png")));
        }
        if (normal is not null)
        {
            var rgb = new ByteImage(normal.Height, normal.Width, 3);
            for (var i = 0; i < normal.Height * normal.Width; i++)
            {
                rgb.Data[i * 3] = normal.Data[i * 4];
                rgb.Data[i * 3 + 1] = options.InvertGreen ? (byte)(255 - normal.Data[i * 4 + 1]) : normal.Data[i * 4 + 1];
                rgb.Data[i * 3 + 2] = normal.Data[i * 4 + 2];
            }
            written.Add(Textures.SavePng(rgb, Path.Combine(outputDir, name + "_normal.png")));
        }
        if (color is not null)
        {
            var rgb = new ByteImage(color.Height, color.Width, 3);
            for (var i = 0; i < color.Height * color.Width; i++)
                for (var c = 0; c < 3; c++) rgb.Data[i * 3 + c] = color.Data[i * 4 + c];
            if (options.RecoverAlbedo && metallic is not null)
            {
                var albedo = PbrMaps.UnbakeAlbedo(Textures.ToFloat(rgb), metallic, options.MetalDiffuseSuppression);
                for (var i = 0; i < rgb.Data.Length; i++) rgb.Data[i] = (byte)(albedo.Data[i] * 255f + 0.5f);
            }
            written.Add(Textures.SavePng(rgb, Path.Combine(outputDir, name + "_color.png")));
            var alpha = color.Channel(3);
            if (options.WriteOpacity && alpha.Data.Any(value => value < 255))
                written.Add(Textures.SaveScalarPng(Textures.ToFloat(alpha), Path.Combine(outputDir, name + "_opacity.png")));
        }
        context.Log($"{name}: {written.Count} map(s)");
        return written;
    }
}
