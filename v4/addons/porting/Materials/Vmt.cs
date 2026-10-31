using System.Text;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>A generated VMT plus what the generator decided along the way.</summary>
public sealed record VmtResult(string Text, string Shader, OrderedDictionary<string, string> Params, List<string> Notes)
{
    /// <summary>Writes the file with Windows line endings, as the kit has always shipped them.</summary>
    public string Write(string path)
    {
        Fmt.WriteText(path, Text.Replace("\n", "\r\n"));
        return path;
    }
}

/// <summary>Generates VMTs.</summary>
/// <remarks>
/// A VMT is just KeyValues, but which keys are legal depends on the engine branch: <c>$phongalbedotint</c>, <c>$envmapfresnel</c> as a
/// float and <c>$blendtintbybasealpha</c> all arrived after the HL2 shaders shipped. Writing them for a
/// branch that predates them does not error: the engine silently ignores the key and the material looks wrong for reasons nothing
/// reports. So the branch table gates every parameter that is not universal, and anything absent is assumed HL2-era, because a
/// missing parameter costs fidelity while an illegal one costs a broken material. Builders return text rather than writing it.
/// </remarks>
public static partial class Vmt
{
    private static readonly HashSet<string> Modern = ["phong_albedo_tint", "phong_exponent_factor", "envmap_fresnel_float", "blend_tint_by_base_alpha"];

    private static readonly Dictionary<string, HashSet<string>> BranchCapabilities = new()
    {
        ["hl2"] = [], ["source2013_sp"] = [], ["source2013_mp"] = Modern, ["gmod"] = Modern, ["tf2"] = Modern, ["l4d2"] = Modern
    };

    /// <summary>Parameters grouped into the comment sections a generated VMT is written in.</summary>
    private static readonly (string Title, string[] Keys)[] Sections =
    [
        ("Textures", ["$basetexture", "%tooltexture", "$detail", "$detailscale", "$detailblendmode", "$detailblendfactor", "$bumpmap", "$phongexponenttexture", "$texture1", "$texture2", "$texture3"]),
        ("Shader", ["$emissionscale", "$blendemissionbycolor", "$parallaxscale", "$alphablend", "$cull"]),
        ("Phong", ["$phong", "$phongboost", "$phongexponentfactor", "$phongtint", "$phongalbedotint", "$phongfresnelranges"]),
        ("Environment map", ["$envmap", "$envmapmask", "$envmaptint", "$envmapcontrast", "$envmapsaturation", "$envmapfresnel", "$normalmapalphaenvmapmask", "$envmaplightscale"]),
        ("Rim light", ["$rimlight", "$rimlightexponent", "$rimlightboost", "$rimmask"]),
        ("Transparency", ["$translucent", "$alphatest", "$alphatestreference", "$nocull"]),
        ("Tint (colourable prop)", ["$color2", "$blendtintbybasealpha", "$blendtintcoloroverbase"]),
        ("Self-illumination", ["$selfillum", "$selfillummask", "$selfillumtint"]),
        ("Emissive blend", ["$EmissiveBlendEnabled", "$EmissiveBlendStrength", "$EmissiveBlendTexture", "$EmissiveBlendBaseTexture", "$EmissiveBlendFlowTexture", "$EmissiveBlendTint", "$EmissiveBlendScrollVector"]),
        ("Surface", ["$surfaceprop", "$halflambert", "$model"])
    ];

    /// <summary>VMT texture keys mapped to the role names the material tools use.</summary>
    private static readonly Dictionary<string, string> TextureKeys = new()
    {
        ["$basetexture"] = "color", ["$bumpmap"] = "normal", ["$phongexponenttexture"] = "phong", ["$envmapmask"] = "envmask", ["$selfillummask"] = "selfillum"
    };

    public static HashSet<string> Capabilities(string branch) => BranchCapabilities.GetValueOrDefault(branch) ?? BranchCapabilities["hl2"];

    public static string Number(double value) => MaterialDefaults.Fixed(value, 3).TrimEnd('0').TrimEnd('.');

    private static string Vec3(IEnumerable<double> values) => "[" + string.Join(" ", values.Select(Number)) + "]";

    /// <summary>Formats a parameter dictionary as VMT text, grouped into comment sections.</summary>
    /// <remarks><paramref name="trailer"/> lines are emitted inside the shader block after the parameters, for nested blocks that are not key and value pairs.</remarks>
    public static string Render(string shader, OrderedDictionary<string, string> parameters, IReadOnlyList<string>? header = null, IReadOnlyList<string>? trailer = null)
    {
        var lines = (header ?? []).Select(line => "// " + line).ToList();
        if (lines.Count > 0) lines.Add("");
        lines.Add($"\"{shader}\"");
        lines.Add("{");
        var emitted = new HashSet<string>();
        foreach (var (title, keys) in Sections)
        {
            var present = keys.Where(parameters.ContainsKey).ToArray();
            if (present.Length == 0) continue;
            if (emitted.Count > 0) lines.Add("");
            lines.Add("    // " + title);
            foreach (var key in present)
            {
                lines.Add($"    \"{key}\" \"{parameters[key]}\"");
                emitted.Add(key);
            }
        }
        var extra = parameters.Keys.Where(key => !emitted.Contains(key)).ToArray();
        if (extra.Length > 0)
        {
            lines.Add("");
            lines.Add("    // Other parameters");
            foreach (var key in extra) lines.Add($"    \"{key}\" \"{parameters[key]}\"");
        }
        if (trailer is { Count: > 0 })
        {
            lines.Add("");
            lines.AddRange(trailer.Select(line => "    " + line));
        }
        lines.Add("}");
        return string.Join("\n", lines) + "\n";
    }

    /// <summary>Builds a stock-shader VMT that fakes PBR with Phong and a cubemap.</summary>
    /// <remarks>
    /// <para>The <c>emit</c> flags gate whole parameter blocks. Turn one off when the source had no matching signal: no roughness or
    /// metallic means no Phong, a flat normal means no bumpmap. Skip the corresponding VTF too, so the VMT never references a file
    /// that was not written.</para>
    /// <para>Bump-mapped VertexLitGeneric uses the normal alpha for its reflection mask; its stock Phong path never loads the separate
    /// envmap mask, so that texture can stay an intermediate artifact but is not referenced.</para>
    /// <para><paramref name="emitBumpmap"/> takes Phong down with it: Phong shades against the bumpmap's normal and is ignored without
    /// one. That is also the shape a prop_static wants, since every branch before the CS:GO engine discards VRAD's baked per-vertex
    /// lighting on a model carrying $bumpmap, $normalmap or $phong on any skin. $envmap survives, needing no tangent normal.</para>
    /// <para>Brush reflections take only $envmap and their mask. The mask already carries the reflection's full strength, and the engine
    /// reads $envmaptint in gamma space, so 0.16 is really 0.018 and multiplying it in leaves about a thousandth of the cubemap.
    /// $envmaplightscale dims reflections by the lightmap, which blacks them out in a dark room, and Garry's Mod's brush shader has
    /// no $envmapfresnel. $envmapcontrast and $envmapsaturation only restated their defaults.</para>
    /// <para><paramref name="mappingTexture"/> makes a black texture the $basetexture and adds the colour back as a detail layer at the same
    /// UVs, so every tool sees the black texture's size. <see cref="MaterialOptions.ToolSize"/> says why.</para>
    /// <para>Brushes get no Phong block. Garry's Mod's brush shader has no $phongexponenttexture, $phongboost or $phongfresnelranges, and
    /// its own $phong only answers a cascaded-shadow key light that stock Garry's Mod does not have.</para>
    /// <para><paramref name="tintable"/> enables the colourable-prop block; the mask must already be baked into basetexture alpha, and
    /// the entity's render colour then tints only the masked regions with no proxy. It is skipped when the branch lacks the
    /// parameter, the shader is not VertexLitGeneric, or $selfillum, $translucent or $alphatest would break the shader combo.</para>
    /// </remarks>
    public static VmtResult FakePbr(string materialName, string materialPath, string shader = "VertexLitGeneric", string branch = "gmod", string envmap = "env_cubemap",
        MaterialStats? stats = null, bool emitBumpmap = true, bool emitPhong = true, bool emitEnvmap = true, bool hasEnvmapMask = true, string surfaceprop = "",
        bool tintable = false, string tintModeUsed = "off", OrderedDictionary<string, string>? extra = null, bool toolTexture = false, string? mappingTexture = null)
    {
        var capabilities = Capabilities(branch);
        var vertexLit = shader == "VertexLitGeneric";
        var lightmapped = shader == "LightmappedGeneric";
        var notes = new List<string>();
        var averageRoughness = stats?.AvgRoughness ?? 0.5;
        var averageMetallic = stats?.AvgMetallic ?? 0.0;
        var roughDielectric = stats?.IsRoughDielectric ?? false;
        var smoothness = Math.Clamp(1.0 - averageRoughness, 0.0, 1.0);
        var colour = $"{materialPath}/{materialName}_color";
        var parameters = new OrderedDictionary<string, string> { ["$basetexture"] = mappingTexture is null ? colour : $"{materialPath}/{mappingTexture}" };
        if (toolTexture) parameters["%tooltexture"] = $"{materialPath}/{materialName}_tool";
        if (mappingTexture is not null)
        {
            parameters["$detail"] = colour;
            parameters["$detailscale"] = "1";
            parameters["$detailblendmode"] = "1";
            parameters["$detailblendfactor"] = "1";
        }
        if (emitBumpmap) parameters["$bumpmap"] = $"{materialPath}/{materialName}_normal";
        if (emitBumpmap && emitPhong && vertexLit)
        {
            parameters["$phong"] = "1";
            parameters["$phongexponenttexture"] = $"{materialPath}/{materialName}_phong";
            parameters["$phongboost"] = Number(Math.Clamp(0.5 + 3.0 * Math.Pow(smoothness, 2), 0.5, 3.5));
            parameters["$phongfresnelranges"] = FresnelRanges(averageMetallic);
            if (capabilities.Contains("phong_albedo_tint")) parameters["$phongalbedotint"] = "1";
            else parameters["$phongtint"] = "[1 1 1]";
            if (capabilities.Contains("phong_exponent_factor")) parameters["$phongexponentfactor"] = "90";
        }
        if (emitEnvmap && !roughDielectric)
        {
            parameters["$envmap"] = envmap;
            if (!lightmapped)
            {
                var tint = Math.Clamp(0.04 + 0.3 * Math.Pow(smoothness, 2), 0.04, 0.34);
                parameters["$envmaptint"] = Vec3([tint, tint, tint]);
                parameters["$envmapcontrast"] = "0";
                parameters["$envmapsaturation"] = "1";
            }
            if (emitBumpmap) parameters["$normalmapalphaenvmapmask"] = "1";
            if (hasEnvmapMask && !emitBumpmap) parameters["$envmapmask"] = $"{materialPath}/{materialName}_envmask";
            else if (hasEnvmapMask) notes.Add(lightmapped ? "envmap mask packed into the bumpmap alpha; the shader discards $envmapmask beside a $bumpmap" : "envmap mask uses bumpmap alpha");
            if (!lightmapped && capabilities.Contains("envmap_fresnel_float")) parameters["$envmapfresnel"] = "1.0";
        }
        else if (emitEnvmap && roughDielectric) notes.Add("envmap skipped: rough dielectric, nothing would reflect");
        if (vertexLit)
        {
            parameters["$halflambert"] = "0";
            parameters["$model"] = "1";
        }
        if (surfaceprop.Trim().Length > 0) parameters["$surfaceprop"] = surfaceprop.Trim();
        if (extra is not null)
            foreach (var (key, value) in extra) parameters[key] = value;
        if (tintable)
        {
            var conflicts = new[] { "$selfillum", "$translucent", "$alphatest" }.Where(key => parameters.GetValueOrDefault(key, "0").Trim() == "1").ToArray();
            if (!capabilities.Contains("blend_tint_by_base_alpha")) notes.Add($"tint skipped: '{branch}' has no $blendtintbybasealpha");
            else if (!vertexLit) notes.Add($"tint skipped: $blendtintbybasealpha is VertexLitGeneric-only, shader is '{shader}'");
            else if (conflicts.Length > 0) notes.Add("tint skipped: conflicts with " + string.Join(", ", conflicts));
            else
            {
                parameters["$color2"] = "[1 1 1]";
                parameters["$blendtintbybasealpha"] = "1";
                parameters["$blendtintcoloroverbase"] = "0";
                notes.Add("tint on: mask in basetexture alpha, white = tinted");
            }
        }
        var header = new List<string>
        {
            "Generated by Source 2 Porting Kit - Fake PBR",
            $"Material: {materialPath}/{materialName}",
            $"Branch: {branch}   Shader: {shader}",
            $"Approximation from metallic={MaterialDefaults.Fixed(averageMetallic, 3)} roughness={MaterialDefaults.Fixed(averageRoughness, 3)}; not energy-conserving"
        };
        if (vertexLit) header.Add($"Phong tint mode: {tintModeUsed}");
        if (vertexLit && !emitBumpmap) header.Add("No $bumpmap, and no $phong with it - a prop_static using this keeps its baked lighting");
        header.AddRange(notes);
        return new(Render(shader, parameters, header), shader, parameters, notes);
    }

    /// <summary>Picks <c>$phongfresnelranges</c> from the material's average character.</summary>
    /// <remarks>The three numbers are the Phong response at facing, mid and grazing angles. Metals retain a stronger facing response than dielectrics, and the grazing response is bounded at one instead of amplifying rough surfaces.</remarks>
    private static string FresnelRanges(double averageMetallic)
    {
        var metal = Math.Clamp(averageMetallic, 0.0, 1.0);
        return Vec3([0.04 + 0.46 * metal, 0.5 + 0.25 * metal, 1.0]);
    }

    /// <summary>Uses LightmappedGeneric's unlit additive detail pass for separate brush emission.</summary>
    /// <remarks>This shader exposes neither VertexLitGeneric's separate self-illumination mask nor the emissive-flow pass. Detail blend mode 5 preserves independent RGB emission without consuming the base texture's opacity alpha.</remarks>
    public static OrderedDictionary<string, string> BrushSelfillumParams(string glowPath, string baseColorPath, string mode = "selfillum", IReadOnlyList<double>? tint = null, double brightness = 1.0) => new()
    {
        ["$detail"] = glowPath,
        ["$detailblendmode"] = "5",
        ["$detailblendfactor"] = "1",
        ["$detailscale"] = "1",
        ["$detailtint"] = Vec3((tint ?? [1.0, 1.0, 1.0]).Select(channel => Math.Max(0.0, channel * brightness)))
    };

    /// <summary>Builds the glow parameters for one of the three techniques.</summary>
    /// <remarks>
    /// <c>selfillum</c> is classic $selfillum with $selfillummask, folding brightness into $selfillumtint because older branches lack
    /// $selfillummaskscale; it is incompatible with $translucent on some branches and breaks the $blendtintbybasealpha combo.
    /// <c>emissiveblend</c> uses the $EmissiveBlend family with the glow mask as emission source and coexists with $translucent and
    /// $phong. <c>emissiveblend_basecolor</c> makes the base colour the emission source and the mask decide where it glows.
    /// $EmissiveBlendStrength clamps to one in the shader, so anything brighter rides along in the tint, and $EmissiveBlendTexture is
    /// required even when the glow is static, with stock white the recommended placeholder.
    /// </remarks>
    public static OrderedDictionary<string, string> SelfillumParams(string glowPath, string baseColorPath, string mode = "selfillum", IReadOnlyList<double>? tint = null, double brightness = 1.0)
    {
        tint ??= [1.0, 1.0, 1.0];
        mode = string.IsNullOrEmpty(mode) ? "selfillum" : mode.ToLowerInvariant();
        var parameters = new OrderedDictionary<string, string>();
        if (mode is "emissiveblend" or "emissiveblend_basecolor")
        {
            parameters["$EmissiveBlendEnabled"] = "1";
            var value = Math.Max(0.0, brightness);
            parameters["$EmissiveBlendStrength"] = MaterialDefaults.Fixed(Math.Min(1.0, value), 3);
            var scale = Math.Max(1.0, value);
            if (mode == "emissiveblend_basecolor")
            {
                parameters["$EmissiveBlendTexture"] = glowPath;
                parameters["$EmissiveBlendBaseTexture"] = baseColorPath;
            }
            else
            {
                parameters["$EmissiveBlendTexture"] = "vgui/white";
                parameters["$EmissiveBlendBaseTexture"] = glowPath;
            }
            parameters["$EmissiveBlendFlowTexture"] = "vgui/white";
            parameters["$EmissiveBlendTint"] = Vec3(tint.Select(component => Math.Max(0.0, component * scale)));
            parameters["$EmissiveBlendScrollVector"] = "[0 0]";
            return parameters;
        }
        parameters["$selfillum"] = "1";
        parameters["$selfillummask"] = glowPath;
        var scaled = tint.Select(component => Math.Max(0.0, component * brightness)).ToArray();
        if (!scaled.SequenceEqual([1.0, 1.0, 1.0])) parameters["$selfillumtint"] = Vec3(scaled);
        return parameters;
    }

    /// <summary>Builds a VMT for the ExoPBR <c>screenspace_general_8tex</c> shader.</summary>
    /// <remarks>
    /// This is a real PBR shader, so the VMT is short: $basetexture holds albedo and opacity, $texture1 the ARM map (R AO, G roughness,
    /// B metallic, A height), $texture2 the normal in DirectX convention, and $texture3 the emission gated by $emissionscale. The shader
    /// is driven by a material proxy, a nested block rather than a key and value pair, hence the trailer.
    /// </remarks>
    public static VmtResult ExoPbr(string materialName, string materialPath, double emissionScale = 0.0, double parallaxScale = 0.0, bool alphaBlend = false, bool hasEmissive = false)
    {
        const string Shader = "screenspace_general_8tex";
        var parameters = new OrderedDictionary<string, string>
        {
            ["$basetexture"] = $"{materialPath}/{materialName}_base",
            ["$texture1"] = $"{materialPath}/{materialName}_arm",
            ["$texture2"] = $"{materialPath}/{materialName}_normal"
        };
        if (hasEmissive) parameters["$texture3"] = $"{materialPath}/{materialName}_emissive";
        if (emissionScale != 0.0)
        {
            parameters["$emissionscale"] = Number(emissionScale);
            parameters["$blendemissionbycolor"] = "1";
        }
        if (parallaxScale != 0.0) parameters["$parallaxscale"] = Number(parallaxScale);
        if (alphaBlend) parameters["$alphablend"] = "1";
        parameters["$model"] = "1";
        parameters["$cull"] = "1";
        string[] header = ["Generated by Source 2 Porting Kit - Exo PBR", $"Material: {materialPath}/{materialName}"];
        string[] trailer = ["\"Proxies\"", "{", "    ExoPBR {}", "}"];
        return new(Render(Shader, parameters, header, trailer), Shader, parameters, []);
    }

    [GeneratedRegex("^\"?\\s*(\\$[A-Za-z_][A-Za-z_0-9]*)\\s*\"?\\s+\"?([^\"\\s{}]+)\"?")]
    private static partial Regex TexturePattern();

    /// <summary>Pulls the texture references out of an existing VMT, keyed by role.</summary>
    /// <remarks>Comments are stripped per line and the first value for a key wins, which is how the engine itself reads a VMT. The pattern tolerates unquoted keys and the trailing whitespace real VMTs sometimes carry inside the quotes.</remarks>
    public static Dictionary<string, string> ReadTextureRefs(string path)
    {
        var references = new Dictionary<string, string>();
        string text;
        try
        {
            text = Encoding.UTF8.GetString(File.ReadAllBytes(path));
        }
        catch (IOException)
        {
            return references;
        }
        foreach (var raw in KeyValues.Lines(text))
        {
            var cut = raw.IndexOf("//", StringComparison.Ordinal);
            var line = (cut >= 0 ? raw[..cut] : raw).Trim();
            if (line.Length == 0 || line is "{" or "}") continue;
            var match = TexturePattern().Match(line);
            if (!match.Success || !TextureKeys.TryGetValue(match.Groups[1].Value.ToLowerInvariant(), out var role) || references.ContainsKey(role)) continue;
            references[role] = match.Groups[2].Value;
        }
        return references;
    }

    /// <summary>Turns a VMT texture reference into a real <c>.vtf</c> path.</summary>
    /// <remarks>References are relative to the game's materials folder, which may or may not be near the folder being scanned, so this tries the VMT's own directory, the scan root, and any materials ancestor.</remarks>
    public static string? ResolveTextureRef(string reference, string vmtDirectory, string? searchRoot = null)
    {
        var cleaned = reference.Replace('\\', '/').Trim();
        if (cleaned.Length == 0) return null;
        var stem = cleaned.EndsWith(".vtf", StringComparison.OrdinalIgnoreCase) ? cleaned[..^4] : cleaned;
        var candidates = new List<string>
        {
            Path.Combine(vmtDirectory, stem[(stem.LastIndexOf('/') + 1)..] + ".vtf"),
            Path.Combine(vmtDirectory, stem + ".vtf")
        };
        if (searchRoot is not null) candidates.Add(Path.Combine(searchRoot, stem + ".vtf"));
        if (MaterialsRoot(vmtDirectory) is { } materials) candidates.Add(Path.Combine(materials, stem + ".vtf"));
        return candidates.FirstOrDefault(File.Exists);
    }

    private static string? MaterialsRoot(string start)
    {
        for (var parent = Path.GetFullPath(start); !string.IsNullOrEmpty(parent); parent = Path.GetDirectoryName(parent)!)
        {
            if (Path.GetFileName(parent).Equals("materials", StringComparison.OrdinalIgnoreCase)) return parent;
            var candidate = Path.Combine(parent, "materials");
            if (Directory.Exists(candidate)) return candidate;
        }
        return null;
    }
}
