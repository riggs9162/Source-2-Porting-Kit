using System.Globalization;
using System.Text;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>One parsed vmat: resolved textures, constants and rendering intent.</summary>
public sealed class VmatEntry
{
    public required string Path { get; init; }
    public required string Name { get; set; }

    /// <summary>The material's folder relative to the parse root, with forward slashes, or an empty string.</summary>
    public required string RelDir { get; init; }

    public string Shader { get; init; } = "";
    public OrderedDictionary<string, string> Resolved { get; } = [];
    public OrderedDictionary<string, double[]> Constants { get; } = [];

    /// <summary>How each role was found: vmat, sibling or literal.</summary>
    public Dictionary<string, string> Sources { get; } = [];

    public List<string> Warnings { get; } = [];
    public bool Translucent { get; set; }
    public bool Alphatest { get; init; }
    public double? RoughnessConstant { get; set; }
    public double? MetallicConstant { get; set; }
    public bool Selfillum { get; set; }
    public double[]? SelfillumTint { get; init; }
    public double? SelfillumBrightness { get; init; }
    public double? SelfillumScale { get; init; }
    public double? SelfillumFlowAmount { get; init; }
    public double? SelfillumFlowSpeed { get; init; }
    public double? SelfillumFlowFrequency { get; init; }
    public double? SelfillumOverallScale { get; init; }
    public double[]? GlassTint { get; set; }
    public double? GlassMaxOpacity { get; init; }
    public double? GlassFresnelExponent { get; init; }
    public bool Tintable { get; set; }
    public double? ModelTintAmount { get; init; }
    public double[]? ColorTint { get; init; }
    public string Surfaceprop { get; init; } = "";
    public bool Decal { get; init; }
    public string Layer { get; set; } = "";

    public bool Has(string role) => Resolved.ContainsKey(role) || Constants.ContainsKey(role);

    /// <summary>Whether a colour map or literal exists, the one hard requirement.</summary>
    public bool Buildable => Has("color");

    private string? Map(string role) => Resolved.GetValueOrDefault(role);

    /// <summary>Bridges to the material builder's input record.</summary>
    /// <remarks>
    /// <paramref name="includeSelfillum"/> gates the glow mask on <see cref="Selfillum"/>: a Source 2 emission texture does not always
    /// mean Source 1 self-illumination is the right call, so a mask found without the flag is dropped rather than silently lighting
    /// the prop up. Alyx's vr_complex feature metadata defines brightness in exposure stops, as
    /// pow(2, brightness) * scale * SrgbGammaToLinear(tint). Other shader families keep their linear brightness semantics.
    /// </remarks>
    public MaterialSource ToSource(bool includeSelfillum = true, bool includeTint = true)
    {
        var glow = includeSelfillum && Selfillum;
        var tint = includeTint && Tintable;
        var glowBrightness = SelfillumBrightness;
        var glowTint = SelfillumTint;
        if (Shader == "vr_complex.vfx")
        {
            glowBrightness = Math.Pow(2.0, SelfillumBrightness ?? 0.0) * (SelfillumScale ?? 1.0);
            glowTint = glowTint?.Select(component => component <= 0.04045 ? component / 12.92 : Math.Pow((component + 0.055) / 1.055, 2.4)).ToArray();
        }
        return new MaterialSource
        {
            Color = Map("color"),
            Normal = Map("normal"),
            Ao = Map("ao"),
            Roughness = Map("roughness"),
            Metallic = Map("metallic"),
            Translucency = Map("translucency"),
            Selfillum = glow ? Map("selfillum") : null,
            Tintmask = tint ? Map("tintmask") : null,
            Heightmap = Map("heightmap"),
            ColorConstant = Constants.GetValueOrDefault("color"),
            NormalConstant = Constants.GetValueOrDefault("normal"),
            AoConstant = Constants.GetValueOrDefault("ao"),
            TranslucencyConstant = Constants.GetValueOrDefault("translucency"),
            SelfillumConstant = glow ? Constants.GetValueOrDefault("selfillum") : null,
            RoughnessConstant = RoughnessConstant,
            MetallicConstant = MetallicConstant,
            SelfillumTint = glowTint,
            SelfillumBrightness = glowBrightness,
            SelfillumFlowPhase = glow ? Map("selfillum_flow_phase") : null,
            SelfillumFlowWaveform = glow ? Map("selfillum_flow_waveform") : null,
            SelfillumFlowAmount = SelfillumFlowAmount,
            SelfillumFlowSpeed = SelfillumFlowSpeed,
            SelfillumFlowFrequency = SelfillumFlowFrequency,
            SelfillumOverallScale = SelfillumOverallScale,
            GlassTint = GlassTint,
            GlassMaxOpacity = GlassMaxOpacity,
            GlassFresnelExponent = GlassFresnelExponent,
            ModelTintAmount = ModelTintAmount,
            ColorTint = ColorTint
        };
    }
}

/// <summary>Reads Source 2 <c>.vmat</c> materials, the plain KeyValues half of what a Source 2 Viewer export produces.</summary>
/// <remarks>
/// Three things make parsing more than a pattern sweep. Texture keys are not stable: albedo is TextureColor in one shader and
/// TextureDiffuse or TextureBaseColor in another, so every role has an alias list. A texture value is sometimes not a texture: a
/// solid colour is written as <c>"TextureColor" "[1.0 1.0 1.0 0.0]"</c>, and that literal is the material's actual albedo, carried
/// as a constant rather than reported missing. And the paths rarely resolve: they are game-relative while the exported PNG sits
/// beside the vmat, so resolution tries the texture root, then the vmat's own folder, then siblings by filename role tokens.
/// </remarks>
public static partial class VmatParser
{
    /// <summary>Source 2 texture keys per role, most specific first.</summary>
    public static readonly OrderedDictionary<string, string[]> KeyAliases = new()
    {
        ["color"] = ["TextureColor", "TextureDiffuse", "TextureAlbedo", "TextureBaseColor", "TextureBasecolor", "TextureBase", "TextureColorA", "GlassDustColor"],
        ["normal"] = ["TextureNormal", "TextureNormalMap", "TextureNormalA"],
        ["ao"] = ["TextureAmbientOcclusion", "TextureAO", "TextureOcclusion", "TextureAmbientOcclusionA"],
        ["roughness"] = ["TextureRoughness", "TextureRough", "TextureRoughnessA"],
        ["metallic"] = ["TextureMetalness", "TextureMetallic", "TextureMetal", "TextureMetalnessA"],
        ["selfillum"] = ["TextureSelfIllumMask", "TextureSelfIllum", "TextureEmissive", "TextureEmission", "TextureGlow"],
        ["selfillum_flow_phase"] = ["TextureSelfIllumFlowPhase"],
        ["selfillum_flow_waveform"] = ["TextureSelfIllumFlowWaveform"],
        ["translucency"] = ["TextureTranslucency", "TextureOpacity", "TextureAlpha", "GlassDustTranslucency"],
        ["tintmask"] = ["TextureTintMask"],
        ["heightmap"] = ["TextureHeight", "TextureDisplacement"]
    };

    /// <summary>Filename tokens used by sibling auto-detect, including the g_t names VRF leaves in compiled texture filenames.</summary>
    /// <remarks>"mask" alone is deliberately generic, because Half-Life: Alyx names tint masks <c>name_mask.png</c>, so sibling detection for that role only runs when the vmat declares F_TINT_MASK.</remarks>
    public static readonly Dictionary<string, string[]> RoleTokens = new()
    {
        ["color"] = ["color", "albedo", "basecolor", "base_color", "basecolour", "base_colour", "diffuse", "base", "col", "bc", "diff", "d", "tcolor", "tdiffuse", "talbedo", "tbasecolor"],
        ["normal"] = ["normal_opengl", "normal", "normalmap", "normal_map", "nrm", "normals", "normalgl", "normal_dx", "nrml", "nor", "n", "tnormal", "tnormalmap"],
        ["ao"] = ["ao", "ambientocclusion", "ambient_occlusion", "occlusion", "occ", "aoc", "mixed_ao", "tao", "tambientocclusion", "tocclusion"],
        ["roughness"] = ["roughness", "rough", "rgh", "r", "rghness", "troughness", "trough"],
        ["metallic"] = ["metallic", "metal", "metalness", "mtl", "m", "tmetalness", "tmetal", "tmetallic"],
        ["selfillum"] = ["emissive", "emit", "emission", "selfillum", "self_illum", "illum", "illumin", "glow", "temission", "temissive", "tselfillum"],
        ["translucency"] = ["trans", "translucency", "translucent", "opacity", "alpha", "opac", "ttranslucency", "topacity"],
        ["tintmask"] = ["tintmask", "tint_mask", "tint", "mask", "ttintmask"],
        ["heightmap"] = ["height", "displacement", "disp", "heightmap", "parallax"]
    };

    /// <summary>Roles stored as RGBA literals. Roughness and metallic literals use their scalar fields, taking the first component.</summary>
    private static readonly string[] ConstantRoles = ["color", "normal", "ao", "selfillum", "translucency"];

    /// <summary>Shaders that render transparent even without an explicit blend flag.</summary>
    private static readonly string[] TranslucentShaders = ["vr_glass.vfx", "vr_glass_markable.vfx", "vr_unlit.vfx", "vr_monitor.vfx"];

    [GeneratedRegex("\"(?<key>Texture[A-Za-z0-9_]+|Glass[A-Za-z0-9_]+)\"\\s+\"(?<value>[^\"]+)\"")]
    private static partial Regex KeyPattern();

    [GeneratedRegex("\"shader\"\\s+\"(?<value>[^\"]+)\"", RegexOptions.IgnoreCase)]
    private static partial Regex ShaderPattern();

    [GeneratedRegex("\"(?<key>F_[A-Z_]+)\"\\s+\"(?<value>[01])\"")]
    private static partial Regex FlagPattern();

    [GeneratedRegex("\"(?<key>g_fl[A-Za-z0-9_]+)\"\\s+\"(?<value>-?[0-9.]+)\"")]
    private static partial Regex ScalarPattern();

    [GeneratedRegex("\"(?<key>g_v[A-Za-z0-9_]+)\"\\s+\"(?<value>\\[[^\"]+\\])\"")]
    private static partial Regex VectorPattern();

    [GeneratedRegex("\"PhysicsSurfaceProperties\"\\s+\"([^\"]+)\"")]
    private static partial Regex SurfacePattern();

    [GeneratedRegex(@"[\s._-]+")]
    private static partial Regex TokenSplit();

    private static string[] Tokens(string stem) => TokenSplit().Split(stem).Where(token => token.Length > 0).Select(token => token.ToLowerInvariant()).ToArray();

    /// <summary>Parses a bracketed literal into RGBA. Three components gain alpha 1; anything past four is dropped.</summary>
    public static double[]? ParseRgba(string raw)
    {
        var inner = raw.Trim().TrimStart('[').TrimEnd(']');
        var parts = inner.Replace(",", " ").Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        if (parts.Length < 3) return null;
        var values = new List<double>();
        foreach (var part in parts.Take(4))
        {
            if (!double.TryParse(part, NumberStyles.Float, CultureInfo.InvariantCulture, out var value)) return null;
            values.Add(value);
        }
        while (values.Count < 4) values.Add(1.0);
        return values.ToArray();
    }

    /// <summary>The token prefixes a sibling texture of this vmat may start with.</summary>
    /// <remarks>
    /// The vmat's own stem is the identity, kept whole: names like combine_metal_base legitimately use "metal" and "base" as
    /// descriptors, and stripping them leaves "combine", which would match every other combine material in the folder. A vmat may
    /// also be named after a role, so a second identity with the trailing role tokens removed is offered as a fallback.
    /// </remarks>
    private static List<string[]> IdentityTokens(string vmatPath)
    {
        var every = RoleTokens.Values.SelectMany(tokens => tokens).ToHashSet();
        var tokens = Tokens(System.IO.Path.GetFileNameWithoutExtension(vmatPath));
        var identities = new List<string[]> { tokens };
        var trimmed = tokens.Length;
        while (trimmed > 0 && every.Contains(tokens[trimmed - 1])) trimmed--;
        if (trimmed > 0 && trimmed != tokens.Length) identities.Add(tokens[..trimmed]);
        return identities;
    }

    /// <summary>Finds a texture beside the vmat whose filename claims a role.</summary>
    /// <remarks>
    /// A candidate qualifies when its tokens start with the vmat's identity and the remaining tokens name the role, so glow_sign.vmat
    /// accepts glow_sign_selfillum.png. Matching on shared tokens instead would pull in textures from any material using the same
    /// descriptor. A tie between equally good candidates returns nothing: picking one arbitrarily would silently bake the wrong
    /// texture, which is worse than reporting the role as missing.
    /// </remarks>
    private static string? SiblingFor(string vmatPath, string role)
    {
        if (!RoleTokens.TryGetValue(role, out var names)) return null;
        var roleTokens = names.ToHashSet();
        var identities = IdentityTokens(vmatPath);
        string[] candidates;
        try
        {
            candidates = Directory.GetFiles(System.IO.Path.GetDirectoryName(System.IO.Path.GetFullPath(vmatPath))!);
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
        {
            return null;
        }
        var scored = new List<(double Score, string Path)>();
        foreach (var candidate in candidates)
        {
            if (!Textures.ImageExtensions.Contains(System.IO.Path.GetExtension(candidate).ToLowerInvariant())) continue;
            var tokens = Tokens(System.IO.Path.GetFileNameWithoutExtension(candidate));
            double? best = null;
            for (var index = 0; index < identities.Count; index++)
            {
                var identity = identities[index];
                if (tokens.Length < identity.Length || !tokens.AsSpan(0, identity.Length).SequenceEqual(identity)) continue;
                var extra = tokens[identity.Length..];
                if (!extra.Any(roleTokens.Contains)) continue;
                var score = 100.0 + identity.Length * 5.0 - index * 10.0 - extra.Length;
                best = best is null ? score : Math.Max(best.Value, score);
            }
            if (best is not null) scored.Add((best.Value, candidate));
        }
        if (scored.Count == 0) return null;
        var top = scored.Max(item => item.Score);
        var winners = scored.Where(item => Math.Abs(item.Score - top) < 1e-6).ToArray();
        return winners.Length > 1 ? null : winners[0].Path;
    }

    private static bool Absolute(string path) => System.IO.Path.IsPathFullyQualified(path) || path.StartsWith('/');

    /// <summary>Resolves an authored path, or an unambiguous decoded VRF hash-name copy of it.</summary>
    private static string? Resolve(string? raw, string vmatPath, string? textureRoot, List<string> warnings)
    {
        if (string.IsNullOrEmpty(raw)) return null;
        var value = raw.Trim();
        if (value.StartsWith('[') || value.StartsWith('{')) return null;
        var requested = value.Replace('\\', '/');
        var candidate = requested;
        var rooted = Absolute(requested);
        if (!rooted && textureRoot is not null) candidate = System.IO.Path.Combine(textureRoot, requested);
        if (File.Exists(candidate)) return candidate;
        var name = requested[(requested.LastIndexOf('/') + 1)..];
        var vmatFolder = System.IO.Path.GetDirectoryName(vmatPath) ?? "";
        var local = System.IO.Path.Combine(vmatFolder, name);
        if (File.Exists(local)) return local;
        var directories = new HashSet<string>(StringComparer.OrdinalIgnoreCase) { System.IO.Path.GetDirectoryName(candidate) ?? "", vmatFolder };
        if (textureRoot is not null && !rooted)
        {
            var parts = requested.Split('/', StringSplitOptions.RemoveEmptyEntries).Where(part => !part.Equals("materials", StringComparison.OrdinalIgnoreCase));
            directories.Add(System.IO.Path.GetDirectoryName(System.IO.Path.Combine(textureRoot, string.Join('/', parts))) ?? "");
        }
        var stem = System.IO.Path.GetFileNameWithoutExtension(name);
        var suffix = System.IO.Path.GetExtension(name).TrimStart('.');
        var pattern = new Regex("^" + Regex.Escape(stem + "_" + suffix) + "_[0-9a-f]{8}$", RegexOptions.IgnoreCase);
        var matches = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var directory in directories)
        {
            try
            {
                foreach (var image in Directory.GetFiles(directory.Length == 0 ? "." : directory))
                    if (Textures.ImageExtensions.Contains(System.IO.Path.GetExtension(image).ToLowerInvariant()) && pattern.IsMatch(System.IO.Path.GetFileNameWithoutExtension(image)))
                        matches.Add(System.IO.Path.GetFullPath(image));
            }
            catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
            {
            }
        }
        if (matches.Count == 1) return matches.First();
        if (matches.Count > 0)
        {
            warnings.Add($"ambiguous: {raw} has {matches.Count} decoded hash-name copies");
            return null;
        }
        warnings.Add($"missing: {raw}");
        return null;
    }

    /// <summary>Reads text as UTF-8, silently dropping invalid bytes.</summary>
    private static string ReadText(string path) => Encoding.UTF8.GetString(File.ReadAllBytes(path)).Replace("\uFFFD", "").Replace("\r\n", "\n").Replace('\r', '\n');

    /// <summary>Parses one vmat file.</summary>
    public static VmatEntry Parse(string vmatPath, string? root = null, string? textureRoot = null) => ParseText(vmatPath, ReadText(vmatPath), root, textureRoot, true);

    /// <summary>Resolves each explicitly authored blend layer without borrowing sibling maps.</summary>
    public static List<VmatEntry> ParseLayers(string vmatPath, string? root = null, string? textureRoot = null)
    {
        var text = ReadText(vmatPath);
        var documents = VmatLayers.LayerDocuments(text);
        if (documents.Count == 0) return [ParseText(vmatPath, text, root, textureRoot, true)];
        var painted = VmatLayers.PaintedBlend(text) is not null;
        var entries = new List<VmatEntry>();
        for (var index = 0; index < documents.Count; index++)
        {
            var (layer, document) = documents[index];
            var entry = ParseText(vmatPath, document, root, textureRoot, false);
            entry.Layer = layer;
            if (index > 0) entry.Name += "_layer_" + layer.ToLowerInvariant();
            entry.Warnings.Add("adapted: exported authored layer " + layer + (painted ? " as a plain material beside the painted blend" : " independently; dynamic blending and triplanar mapping are not reproduced"));
            entries.Add(entry);
        }
        return entries;
    }

    /// <summary>Reads a painted blend's mask and tiling, or null when the material is not one.</summary>
    public static VmatBlend? ParseBlend(string vmatPath, string? textureRoot = null)
    {
        if (VmatLayers.PaintedBlend(ReadText(vmatPath)) is not { } blend) return null;
        var raw = blend.Mask?.Trim();
        if (raw is null) return blend;
        if (raw.StartsWith('[') || raw.StartsWith('{')) return blend with { Mask = null, MaskConstant = ParseRgba(raw)?[0] };
        return blend with { Mask = Resolve(raw, vmatPath, textureRoot, blend.Notes) };
    }

    private static string RelativeDirectory(string path, string? root)
    {
        if (root is null) return "";
        var folder = System.IO.Path.GetFullPath(System.IO.Path.GetDirectoryName(path)!);
        var parent = System.IO.Path.GetFullPath(root).TrimEnd('\\', '/');
        if (folder.Equals(parent, StringComparison.OrdinalIgnoreCase)) return "";
        return folder.StartsWith(parent + System.IO.Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)
            ? folder[(parent.Length + 1)..].Replace('\\', '/')
            : "";
    }

    /// <summary>Parses an original material or one normalized independent layer.</summary>
    private static VmatEntry ParseText(string path, string text, string? root, string? textureRoot, bool allowSiblings)
    {
        var rawKeys = new Dictionary<string, string>();
        foreach (Match match in KeyPattern().Matches(text)) rawKeys.TryAdd(match.Groups["key"].Value, match.Groups["value"].Value);
        var flags = new Dictionary<string, string>();
        foreach (Match match in FlagPattern().Matches(text)) flags[match.Groups["key"].Value] = match.Groups["value"].Value;
        var scalars = new Dictionary<string, string>();
        foreach (Match match in ScalarPattern().Matches(text)) scalars[match.Groups["key"].Value] = match.Groups["value"].Value;
        var vectors = new Dictionary<string, string>();
        foreach (Match match in VectorPattern().Matches(text)) vectors[match.Groups["key"].Value] = match.Groups["value"].Value;

        double? Scalar(string key) => scalars.TryGetValue(key, out var value) && double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out var number) ? number : null;
        double[]? Vec3(string key) => vectors.TryGetValue(key, out var value) && ParseRgba(value) is { } rgba ? rgba[..3] : null;

        var shaderMatch = ShaderPattern().Match(text);
        var shader = shaderMatch.Success ? shaderMatch.Groups["value"].Value.ToLowerInvariant() : "";
        var translucent = flags.Any(item => item.Value == "1" && item.Key is "F_TRANSLUCENT" or "F_ADDITIVE_BLEND" or "F_ADDITIVE_BLEND_OVER_ALPHA" or "F_ALPHA_BLEND" or "F_BLEND_MODE")
            || TranslucentShaders.Any(shader.Contains);
        var surface = SurfacePattern().Match(text);
        var entry = new VmatEntry
        {
            Path = path,
            Name = System.IO.Path.GetFileNameWithoutExtension(path),
            RelDir = RelativeDirectory(path, root),
            Shader = shader,
            Translucent = translucent,
            Alphatest = flags.GetValueOrDefault("F_ALPHA_TEST") == "1",
            RoughnessConstant = Scalar("g_flRoughness"),
            MetallicConstant = Scalar("g_flMetalness"),
            SelfillumTint = Vec3("g_vSelfIllumTint"),
            SelfillumBrightness = Scalar("g_flSelfIllumBrightness"),
            SelfillumScale = Scalar("g_flSelfIllumScale"),
            SelfillumFlowAmount = Scalar("g_flSelfIllumFlowAmount"),
            SelfillumFlowSpeed = Scalar("g_flSelfIllumFlowSpeed"),
            SelfillumFlowFrequency = Scalar("g_flSelfIllumFlowFrequency"),
            SelfillumOverallScale = Scalar("g_flSelfIllumOverallScale"),
            GlassMaxOpacity = Scalar("g_flGlassFresnelMaxOpacity"),
            GlassFresnelExponent = Scalar("g_flGlassFresnelExponent"),
            ModelTintAmount = Scalar("g_flModelTintAmount"),
            ColorTint = Vec3("g_vColorTint"),
            Surfaceprop = surface.Success ? surface.Groups[1].Value : "",
            Decal = shader.Contains("decal") || shader.Contains("overlay")
        };
        if (shader is "vr_glass.vfx" or "vr_glass_markable.vfx")
            entry.GlassTint = ParseRgba(rawKeys.GetValueOrDefault("GlassTintColor", ""))?[..3];
        if (rawKeys.ContainsKey("TextureColorA"))
        {
            entry.RoughnessConstant ??= Scalar("g_flRoughnessA");
            entry.MetallicConstant ??= Scalar("g_flMetalnessA");
        }
        if (rawKeys.ContainsKey("TextureColorB"))
            entry.Warnings.Add("adapted: retained the authored base material layer; secondary blend/triplanar layer and its dynamic transition are unsupported by the target shader");
        foreach (var (role, aliases) in KeyAliases)
        {
            var raw = aliases.Where(rawKeys.ContainsKey).Select(key => rawKeys[key]).FirstOrDefault();
            var literal = !string.IsNullOrEmpty(raw) && (raw.Trim().StartsWith('[') || raw.Trim().StartsWith('{'));
            if (literal)
            {
                if (ParseRgba(raw!) is { } constant)
                {
                    if (ConstantRoles.Contains(role))
                    {
                        entry.Constants[role] = constant;
                        entry.Sources[role] = "literal";
                    }
                    else if (role == "roughness")
                    {
                        entry.RoughnessConstant = constant[0];
                        entry.Sources[role] = "literal";
                    }
                    else if (role == "metallic")
                    {
                        entry.MetallicConstant = constant[0];
                        entry.Sources[role] = "literal";
                    }
                }
                continue;
            }
            var warningCount = entry.Warnings.Count;
            var resolved = Resolve(raw, path, textureRoot, entry.Warnings);
            var ambiguous = entry.Warnings.Skip(warningCount).Any(warning => warning.StartsWith("ambiguous: ", StringComparison.Ordinal));
            if (resolved is not null) entry.Sources[role] = "vmat";
            else
            {
                var useSiblings = allowSiblings && !ambiguous
                    && (role != "tintmask" || flags.GetValueOrDefault("F_TINT_MASK") == "1")
                    && (role != "translucency" || raw is not null || entry.Translucent || entry.Alphatest);
                var sibling = useSiblings ? SiblingFor(path, role) : null;
                if (sibling is not null)
                {
                    resolved = sibling;
                    entry.Sources[role] = "sibling";
                    if (!string.IsNullOrEmpty(raw)) entry.Warnings.Add($"using sibling {System.IO.Path.GetFileName(sibling)} for {role}");
                }
            }
            if (resolved is not null) entry.Resolved[role] = resolved;
        }
        DeriveIntent(entry, flags);
        return entry;
    }

    /// <summary>Fills in the transparency, glow and tint decisions the flags imply.</summary>
    /// <remarks>
    /// An opacity texture with no blend flag is itself the authored signal: without flipping the flag the alpha gets baked into the
    /// basetexture and silently ignored at runtime. g_flModelTintAmount 0 opts a material out even when F_TINT_MASK is set; Source 2
    /// defaults the amount to 1 when the key is absent. Alyx's "the entity tint is the colour" skins author TextureColor as a literal,
    /// and that literal is what Source 2 compiles, so it must beat sibling auto-detect: the sibling colour map belongs to the
    /// non-tintable skin, and using it renders the ported skin as a muddy duplicate of skin 0.
    /// </remarks>
    private static void DeriveIntent(VmatEntry entry, Dictionary<string, string> flags)
    {
        if (entry.Translucent || entry.Alphatest)
        {
            if (!entry.Has("translucency") && entry.GlassMaxOpacity is null)
                entry.Warnings.Add("transparent but no opacity map; the colour alpha will be used");
        }
        else if (entry.Resolved.ContainsKey("translucency")) entry.Translucent = true;
        entry.Selfillum = flags.GetValueOrDefault("F_SELF_ILLUM") == "1" || entry.Has("selfillum");
        if (entry.Selfillum && !entry.Has("selfillum")) entry.Warnings.Add("F_SELF_ILLUM set but no glow mask found");
        entry.Tintable = flags.GetValueOrDefault("F_TINT_MASK") == "1" || entry.Resolved.ContainsKey("tintmask");
        if (entry.ModelTintAmount is <= 0.0) entry.Tintable = false;
        if (entry.Tintable && !entry.Resolved.ContainsKey("tintmask")) entry.Warnings.Add("F_TINT_MASK set but no mask found; the whole surface will tint");
        if (entry.Tintable && entry.Sources.GetValueOrDefault("color") == "sibling")
            entry.Warnings.Add("tintable skin using a sibling colour map - check it is not the untinted variant");
    }

    /// <summary>Every <c>.vmat</c> under a folder, sorted.</summary>
    public static List<string> FindVmats(string root, bool recursive = true)
    {
        if (!Directory.Exists(root)) return [];
        var files = Directory.GetFiles(root, "*.vmat", recursive ? SearchOption.AllDirectories : SearchOption.TopDirectoryOnly);
        Array.Sort(files, StringComparer.Ordinal);
        return files.ToList();
    }

    /// <summary>Parses a batch of vmats, skipping the unreadable ones.</summary>
    public static List<VmatEntry> ParseAll(IEnumerable<string> paths, string? root = null, string? textureRoot = null, string blendLayers = "base")
    {
        var entries = new List<VmatEntry>();
        foreach (var path in paths)
        {
            try
            {
                if (blendLayers == "all") entries.AddRange(ParseLayers(path, root, textureRoot));
                else entries.Add(Parse(path, root, textureRoot));
            }
            catch (IOException)
            {
            }
        }
        return entries;
    }

    /// <summary>Applies a transparency override to a parsed entry.</summary>
    /// <remarks>
    /// <c>auto</c> keeps whatever the flags said. The forced modes only apply to materials that already showed some transparency
    /// intent: adding it to a material authored opaque would reinterpret its basetexture alpha, which in a tintable material is the
    /// tint mask, not opacity. <c>opaque</c> strips it regardless, the escape hatch for a vmat that asked for blending it does not need.
    /// </remarks>
    public static (bool Translucent, bool Alphatest) ResolveTransparency(VmatEntry entry, string mode)
    {
        mode = string.IsNullOrEmpty(mode) ? "auto" : mode.ToLowerInvariant();
        if (mode == "auto") return (entry.Translucent, entry.Alphatest);
        if (mode == "opaque") return (false, false);
        var hadIntent = entry.Translucent || entry.Alphatest || entry.Resolved.ContainsKey("translucency");
        if (!hadIntent) return (entry.Translucent, entry.Alphatest);
        return mode switch { "translucent" => (true, false), "alphatest" => (false, true), _ => (entry.Translucent, entry.Alphatest) };
    }

    /// <summary>Splits a user prefix into a material-path override and a filename prefix.</summary>
    /// <remarks>A prefix containing a slash is path-shaped: everything up to the last slash overrides the material path and the remainder prefixes the filename, because Source 1 only finds a material at its materials-relative path.</remarks>
    public static (string Path, string Prefix) SplitPrefix(string prefix)
    {
        if (!prefix.Contains('/') && !prefix.Contains('\\')) return ("", prefix);
        var normal = prefix.Replace('\\', '/');
        var last = normal.LastIndexOf('/');
        return (normal[..last], normal[(last + 1)..]);
    }
}
