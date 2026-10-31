using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>Resolves compiled materials in isolated shader-aware texture export contexts.</summary>
/// <remarks>
/// A model export decodes every material's maps into shared folders, where two materials naming the same authored PNG overwrite each
/// other. Exporting exactly one compiled material at a time keeps each one's decoded maps to itself, and the hashed manifest means a
/// later run only trusts a finished export whose entire byte inventory still matches.
/// </remarks>
public static partial class SemanticMaterials
{
    [GeneratedRegex(@"^(.+)_([a-z0-9]{3,4})_([0-9a-f]+)\z")]
    private static partial Regex CompiledName();

    private static readonly string[] MaterialSuffixes = [".vmat", ".vmat_c"];

    private static string Stem(string path)
    {
        var name = path.Replace('\\', '/');
        name = name[(name.LastIndexOf('/') + 1)..];
        var dot = name.LastIndexOf('.');
        return dot > 0 ? name[..dot] : name;
    }

    private static string Posix(string path) => path.Replace('\\', '/');

    /// <summary>Requires one exact relative compiled material resource.</summary>
    private static string Resource(string value)
    {
        var path = Posix(value);
        var suffix = Path.GetExtension(path).ToLowerInvariant();
        if (path.StartsWith('/') || path.Split('/').Contains("..") || path.Contains(':') || !MaterialSuffixes.Contains(suffix))
            throw new InvalidDataException($"Unsafe compiled material resource: {value}");
        return path[..^suffix.Length] + ".vmat_c";
    }

    /// <summary>Keeps authored semantic inputs and the complete original compiled texture table.</summary>
    public static (OrderedDictionary<string, string> Authored, OrderedDictionary<string, string> Compiled) Fields(string path, bool strict = true)
    {
        var text = new UTF8Encoding(false, true).GetString(File.ReadAllBytes(path));
        if (text.StartsWith('﻿')) text = text[1..];
        var document = KeyValues.Parse(text);
        KeyValueBlock layer;
        if (document.Pairs.Count == 1 && document.Pairs[0].Value is KeyValueBlock block) layer = block;
        else if (!strict) layer = document;
        else throw new InvalidDataException($"Expected exactly one decompiled material layer: {path}");
        return (layer.Scalars(), layer.Block("Compiled Textures")?.Scalars() ?? []);
    }

    /// <summary>Links semantic outputs to compiled names only when the exporter retained their identity.</summary>
    private static JsonArray CompiledCandidates(string image, OrderedDictionary<string, string> compiled)
    {
        var stem = Stem(image).ToLowerInvariant();
        var candidates = new JsonArray();
        foreach (var (parameter, resource) in compiled)
        {
            var original = Stem(resource).ToLowerInvariant();
            var match = CompiledName().Match(original);
            if (original == stem || (match.Success && (stem == match.Groups[1].Value || stem.Split('_').Contains(match.Groups[3].Value))))
                candidates.Add(new JsonObject { ["parameter"] = parameter, ["resource"] = resource });
        }
        return candidates;
    }

    /// <summary>Locates only full compiled identities for builtin first-input maps omitted by the exporter.</summary>
    private static JsonObject DefaultSources(string rawVmat, string rawRoot)
    {
        var (fields, compiled) = Fields(rawVmat);
        var sources = new JsonObject();
        foreach (var (role, aliases) in VmatParser.KeyAliases)
        {
            var parameter = aliases.FirstOrDefault(fields.ContainsKey) ?? "";
            var image = fields.GetValueOrDefault(parameter, "");
            if (!image.StartsWith("materials/default/", StringComparison.Ordinal) || role == "roughness") continue;
            var candidates = CompiledCandidates(image, compiled);
            if (candidates.Count != 1 || !candidates[0].Get("resource").Str().StartsWith("materials/default/", StringComparison.Ordinal))
                throw new InvalidDataException($"Builtin default lacks one compiled texture identity: {rawVmat}: {parameter}");
            var resource = candidates[0].Get("resource").Str();
            var matches = Directory.GetFiles(rawRoot, Stem(resource) + ".png", SearchOption.AllDirectories);
            if (matches.Length != 1) throw new InvalidDataException($"Builtin default lacks one exact compiled image: {rawVmat}: {resource}");
            var path = Path.GetFullPath(matches[0]);
            sources[role] = new JsonObject
            {
                ["parameter"] = parameter, ["image"] = image, ["resource"] = resource, ["path"] = path, ["sha256"] = Digest.File(path), ["shader"] = fields.GetValueOrDefault("shader", "")
            };
        }
        return sources;
    }

    /// <summary>Selects documented channels only after verifying builtin neutral or monochrome waveform pixels.</summary>
    private static JsonObject WriteDefaults(string directory, JsonObject defaults)
    {
        var proof = new JsonObject();
        foreach (var (role, source) in defaults)
        {
            var path = source.Get("path").Str();
            if (Digest.File(path) != source.Get("sha256").Str()) throw new InvalidDataException($"Compiled builtin default changed while exporting: {path}");
            var pixels = SourceImage.Open(path)!.ToRgba().Data;
            var shader = source.Get("shader").Str();
            var image = source.Get("image").Str();
            var basename = image[(image.LastIndexOf('/') + 1)..];
            int[] low = [255, 255, 255, 255], high = [0, 0, 0, 0];
            bool uniformRgb = true, allWhite = true, blackRgb = true, grey = true, alpha128 = true, alpha255 = true;
            for (var i = 0; i < pixels.Length; i += 4)
            {
                for (var c = 0; c < 4; c++)
                {
                    low[c] = Math.Min(low[c], pixels[i + c]);
                    high[c] = Math.Max(high[c], pixels[i + c]);
                }
                uniformRgb &= pixels[i] == pixels[0] && pixels[i + 1] == pixels[1] && pixels[i + 2] == pixels[2];
                allWhite &= pixels[i] == 255 && pixels[i + 1] == 255 && pixels[i + 2] == 255 && pixels[i + 3] == 255;
                blackRgb &= pixels[i] == 0 && pixels[i + 1] == 0 && pixels[i + 2] == 0;
                grey &= pixels[i] == pixels[i + 1] && pixels[i + 1] == pixels[i + 2];
                alpha128 &= pixels[i + 3] == 128;
                alpha255 &= pixels[i + 3] == 255;
            }
            var pixelCount = pixels.Length / 4;
            string channels;
            ByteImage selected;
            var size = SourceImage.ProbeFile(path);
            if (role == "normal" && basename == "default_normal.tga" && shader is "vr_complex.vfx" or "vr_simple.vfx" && uniformRgb
                && Math.Abs(pixels[0] - 127) <= 1 && Math.Abs(pixels[1] - 127) <= 1 && pixels[2] == 255 && alpha128)
            {
                channels = "RGB";
                selected = new ByteImage(size.Height, size.Width, 3, ExtractChannels(pixels, pixelCount, 3));
            }
            else if (role == "ao" && basename == "default_ao.tga" && shader is "vr_complex.vfx" or "vr_simple.vfx" && allWhite)
            {
                channels = "R";
                selected = new ByteImage(size.Height, size.Width, 1, ExtractChannels(pixels, pixelCount, 1));
            }
            else if (role == "selfillum" && basename == "default_selfillum.tga" && shader == "vr_complex.vfx" && blackRgb && alpha255)
            {
                channels = "R";
                selected = new ByteImage(size.Height, size.Width, 1, ExtractChannels(pixels, pixelCount, 1));
            }
            else if (role == "selfillum_flow_waveform" && basename == "default_wave.tga" && shader == "vr_xen_foliage.vfx" && alpha255 && grey)
            {
                channels = "RGB";
                selected = new ByteImage(size.Height, size.Width, 3, ExtractChannels(pixels, pixelCount, 3));
            }
            else throw new InvalidDataException($"Unsupported builtin default channel semantics: {source.Get("resource").Str()}: {role}");
            if (image.StartsWith('/') || image.Split('/').Contains("..") || image.Contains(':')) throw new InvalidDataException($"Unsafe builtin default image: {image}");
            var target = Path.Combine(directory, image);
            Textures.SavePng(selected, target);
            var record = source!.DeepClone().AsObject();
            record["channel_selection"] = channels;
            record["rgba_min"] = new JsonArray(low.Select(value => JsonValue.Create(value)).ToArray<JsonNode?>());
            record["rgba_max"] = new JsonArray(high.Select(value => JsonValue.Create(value)).ToArray<JsonNode?>());
            record["semantic_image"] = image;
            record["semantic_sha256"] = Digest.File(target);
            record["method"] = "validated_builtin_default_channels";
            proof[role] = record;
        }
        return proof;
    }

    private static byte[] ExtractChannels(byte[] rgba, int pixels, int channels)
    {
        var output = new byte[pixels * channels];
        for (var i = 0; i < pixels; i++)
            for (var c = 0; c < channels; c++) output[i * channels + c] = rgba[i * 4 + c];
        return output;
    }

    /// <summary>Describes exact decoded inputs without inventing channel equivalence for unsupported shaders.</summary>
    public static JsonObject MaterialProvenance(VmatEntry entry, string resource = "")
    {
        var metadataError = "";
        OrderedDictionary<string, string> authored = [], compiled = [];
        try
        {
            (authored, compiled) = Fields(entry.Path, resource.Length > 0);
        }
        catch (Exception exception) when (exception is InvalidDataException or KeyValuesError or DecoderFallbackException)
        {
            if (resource.Length > 0) throw;
            metadataError = exception.Message;
        }
        var maps = new JsonObject();
        foreach (var (role, path) in entry.Resolved.OrderBy(item => item.Key, StringComparer.Ordinal))
        {
            var parameter = VmatParser.KeyAliases[role].FirstOrDefault(authored.ContainsKey) ?? "";
            var image = authored.GetValueOrDefault(parameter, "");
            var bitmap = SourceImage.ProbeFile(path);
            maps[role] = new JsonObject
            {
                ["authored_parameter"] = parameter, ["authored_image"] = image, ["path"] = Path.GetFullPath(path), ["sha256"] = Digest.File(path),
                ["dimensions"] = new JsonArray(bitmap.Width, bitmap.Height), ["channels"] = new JsonArray(bitmap.Bands.Select(band => JsonValue.Create(band)).ToArray<JsonNode?>()),
                ["resolution"] = entry.Sources[role], ["compiled_texture_candidates"] = CompiledCandidates(image.Length > 0 ? image : Path.GetFileName(path), compiled)
            };
        }
        var textures = new JsonObject();
        foreach (var (key, value) in authored)
            if (key.StartsWith("Texture", StringComparison.Ordinal)) textures[key] = value;
        var compiledTextures = new JsonObject();
        foreach (var (key, value) in compiled) compiledTextures[key] = value;
        var constants = new JsonObject();
        foreach (var (key, value) in entry.Constants) constants[key] = new JsonArray(value.Select(item => JsonValue.Create(item)).ToArray<JsonNode?>());
        return new JsonObject
        {
            ["material_resource"] = resource, ["isolated_vmat"] = Path.GetFullPath(entry.Path), ["isolated_vmat_sha256"] = Digest.File(entry.Path),
            ["authored_textures"] = textures, ["compiled_textures"] = compiledTextures, ["semantic_maps"] = maps, ["constants"] = constants,
            ["metadata_status"] = metadataError.Length > 0 ? "unavailable" : "parsed", ["metadata_error"] = metadataError, ["channel_conversion"] = "authored semantic maps"
        };
    }

    /// <summary>Lists where a normalized material resource can live: beside its model or under materials.</summary>
    private static string[] ArchivePaths(string resource) =>
        resource.Split('/')[0].Equals("materials", StringComparison.OrdinalIgnoreCase) ? [resource] : [resource, "materials/" + resource];

    /// <summary>Exports only the selected VMAT so sibling materials cannot overwrite its decoded maps.</summary>
    private static List<string> Command(string source, string resource, string output)
    {
        var input = source;
        var filtered = Path.GetExtension(source).Equals(".vpk", StringComparison.OrdinalIgnoreCase);
        if (!filtered)
        {
            var matches = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            for (var parent = Path.GetDirectoryName(Path.GetFullPath(source)); !string.IsNullOrEmpty(parent); parent = Path.GetDirectoryName(parent))
                foreach (var path in ArchivePaths(resource))
                {
                    var candidate = Path.Combine(parent, path);
                    if (File.Exists(candidate)) matches.Add(candidate);
                }
            if (matches.Count != 1) throw new InvalidDataException($"Cannot resolve one original compiled material beside {source}: {resource}");
            input = matches.First();
        }
        var arguments = new List<string> { "-i", input, "-o", output, "-d", "--threads", "1", "-e", "vmat_c" };
        if (filtered) arguments.AddRange(["-f", string.Join(',', ArchivePaths(resource))]);
        return arguments;
    }

    private static bool Inside(string path, string directory)
    {
        var root = Path.GetFullPath(directory).TrimEnd('\\', '/') + Path.DirectorySeparatorChar;
        return (Path.GetFullPath(path) + Path.DirectorySeparatorChar).StartsWith(root, StringComparison.OrdinalIgnoreCase);
    }

    private static string RelativeTo(string directory, string path) => Posix(Path.GetRelativePath(directory, path));

    /// <summary>Rejects prefix-filter siblings, missing images and mismatched original material bytes.</summary>
    private static (VmatEntry Entry, List<string> Files) CacheFiles(string directory, string resource, string rawHash, bool archiveInput)
    {
        var vmats = Directory.GetFiles(directory, "*.vmat", SearchOption.AllDirectories);
        if (vmats.Length != 1 || Stem(vmats[0]) != Stem(resource)) throw new InvalidDataException($"Isolated material export selected unexpected VMATs: {resource}");
        if (archiveInput && !ArchivePaths(resource).Select(path => path[..^".vmat_c".Length] + ".vmat").Contains(RelativeTo(directory, vmats[0])))
            throw new InvalidDataException($"Isolated archive export selected another resource path: {resource}");
        if (Digest.File(vmats[0]) != rawHash) throw new InvalidDataException($"Isolated VMAT bytes differ from the selected extracted material: {resource}");
        var entry = VmatParser.Parse(vmats[0], directory, directory);
        foreach (var path in entry.Resolved.Values)
            if (!Inside(path, directory)) throw new InvalidDataException($"Isolated material resolved a texture outside its own context: {path}");
        var (authored, _) = Fields(vmats[0]);
        foreach (var (role, aliases) in VmatParser.KeyAliases)
        {
            var parameter = aliases.FirstOrDefault(authored.ContainsKey);
            var value = parameter is null ? "" : authored[parameter];
            if (value.Length > 0 && !value.StartsWith('[') && !value.StartsWith('{') && (!entry.Resolved.ContainsKey(role) || entry.Sources.GetValueOrDefault(role) != "vmat"))
                throw new InvalidDataException($"Isolated material lacks authored {role} map: {resource}: {value}");
        }
        var manifest = Path.Combine(directory, "manifest.json");
        var files = Directory.GetFiles(directory, "*", SearchOption.AllDirectories)
            .Where(path => !path.Equals(manifest, StringComparison.OrdinalIgnoreCase)).OrderBy(path => path, StringComparer.Ordinal).ToList();
        return (entry, files);
    }

    /// <summary>Trusts only completed isolated exports whose entire recorded byte inventory still matches.</summary>
    private static (VmatEntry Entry, List<string> Files)? VerifiedCache(string directory, JsonObject identity, string resource)
    {
        var manifestPath = Path.Combine(directory, "manifest.json");
        if (!File.Exists(manifestPath)) return null;
        try
        {
            var manifest = Json.Load(manifestPath);
            if (!JsonNode.DeepEquals(manifest.Get("identity"), identity) || manifest.Get("complete") is not JsonValue complete || complete.GetValueKind() != JsonValueKind.True) return null;
            var (entry, files) = CacheFiles(directory, resource, identity["raw_vmat_sha256"].Str(), identity["archive_input"].Bool());
            var recorded = manifest.Get("files").Obj();
            if (recorded.Count != files.Count) return null;
            foreach (var path in files)
                if (recorded.Get(RelativeTo(directory, path)).Str() != Digest.File(path)) return null;
            files.Add(manifestPath);
            return (entry, files);
        }
        catch (Exception exception) when (exception is IOException or InvalidDataException or KeyNotFoundException or JsonException or KeyValuesError or InvalidOperationException)
        {
            return null;
        }
    }

    /// <summary>Removes only a verified owned temporary or cache directory beneath the project cache root.</summary>
    private static void Discard(string directory, string root)
    {
        var resolved = Path.GetFullPath(directory);
        if (resolved.Equals(Path.GetFullPath(root), StringComparison.OrdinalIgnoreCase) || !Inside(resolved, root))
            throw new InvalidDataException($"Refusing to remove unowned semantic material directory: {directory}");
        if (Directory.Exists(directory)) Directory.Delete(directory, true);
    }

    /// <summary>Serializes with sorted keys and ASCII escapes, so a cache key survives any change of writer.</summary>
    private static void Canonical(JsonNode? node, StringBuilder output)
    {
        switch (node)
        {
            case null:
                output.Append("null");
                break;
            case JsonObject map:
                output.Append('{');
                var first = true;
                foreach (var (key, value) in map.OrderBy(item => item.Key, StringComparer.Ordinal))
                {
                    if (!first) output.Append(", ");
                    first = false;
                    AppendString(key, output);
                    output.Append(": ");
                    Canonical(value, output);
                }
                output.Append('}');
                break;
            case JsonArray list:
                output.Append('[');
                for (var i = 0; i < list.Count; i++)
                {
                    if (i > 0) output.Append(", ");
                    Canonical(list[i], output);
                }
                output.Append(']');
                break;
            default:
                var kind = node.GetValueKind();
                if (kind == JsonValueKind.String) AppendString(node.Str(), output);
                else output.Append(kind switch { JsonValueKind.True => "true", JsonValueKind.False => "false", _ => node.ToJsonString() });
                break;
        }
    }

    private static void AppendString(string value, StringBuilder output)
    {
        output.Append('"');
        foreach (var c in value)
        {
            switch (c)
            {
                case '"': output.Append("\\\""); break;
                case '\\': output.Append("\\\\"); break;
                case '\n': output.Append("\\n"); break;
                case '\r': output.Append("\\r"); break;
                case '\t': output.Append("\\t"); break;
                case '\b': output.Append("\\b"); break;
                case '\f': output.Append("\\f"); break;
                default:
                    if (c is < ' ' or > '~') output.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
                    else output.Append(c);
                    break;
            }
        }
        output.Append('"');
    }

    /// <summary>Returns a hash-verified exact VMAT export without touching the immutable raw model extraction.</summary>
    public static async Task<(VmatEntry Entry, JsonObject Provenance, List<string> Files)> Resolve(string resource, string rawVmat, string rawRoot, string source, string project, string vrfExecutable,
        string sourceSignature, string implementationSignature, PortContext context)
    {
        context.Check();
        var selected = Resource(resource);
        var rawHash = Digest.File(rawVmat);
        var defaults = DefaultSources(rawVmat, rawRoot);
        var withoutPaths = new JsonObject();
        foreach (var (role, item) in defaults)
        {
            var copy = item!.DeepClone().AsObject();
            copy.Remove("path");
            withoutPaths[role] = copy;
        }
        var archive = Path.GetExtension(source).Equals(".vpk", StringComparison.OrdinalIgnoreCase);
        var identity = new JsonObject
        {
            ["version"] = 1, ["material_resource"] = selected, ["source"] = Path.GetFullPath(source), ["archive_input"] = archive, ["source_signature"] = sourceSignature,
            ["raw_vmat_sha256"] = rawHash, ["exporter"] = Path.GetFullPath(vrfExecutable), ["exporter_sha256"] = Digest.File(vrfExecutable),
            ["implementation_signature"] = implementationSignature, ["builtin_defaults"] = withoutPaths
        };
        var cacheRoot = Path.Combine(Path.GetFullPath(project), "semantic-materials");
        Directory.CreateDirectory(cacheRoot);
        var canonical = new StringBuilder();
        Canonical(identity, canonical);
        var destination = Path.Combine(cacheRoot, Digest.Text(canonical.ToString()));
        var cached = VerifiedCache(destination, identity, selected);
        if (cached is null)
        {
            var temporary = Path.Combine(cacheRoot, ".export-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(temporary);
            try
            {
                var result = await context.Processes.RunAsync(new ProcessRequest(vrfExecutable, Command(source, selected, temporary), temporary), line => context.Log(line.Text), context.Token);
                context.Check();
                if (result.ExitCode != 0) throw new InvalidDataException($"Isolated material export failed ({result.ExitCode}): {selected}");
                var proof = WriteDefaults(temporary, defaults);
                var (_, files) = CacheFiles(temporary, selected, rawHash, archive);
                var inventory = new JsonObject();
                foreach (var path in files) inventory[RelativeTo(temporary, path)] = Digest.File(path);
                var manifest = new JsonObject { ["identity"] = identity.DeepClone(), ["complete"] = true, ["builtin_defaults"] = proof, ["files"] = inventory };
                context.Check();
                Fmt.WriteText(Path.Combine(temporary, "manifest.json"), manifest.ToJsonString(Json.Options) + "\n");
                if (Directory.Exists(destination)) Discard(destination, cacheRoot);
                Directory.Move(temporary, destination);
            }
            finally
            {
                Discard(temporary, cacheRoot);
            }
            cached = VerifiedCache(destination, identity, selected);
        }
        if (cached is not var (entry, cachedFiles)) throw new InvalidDataException($"Isolated material cache did not verify after export: {selected}");
        context.Check();
        var provenance = MaterialProvenance(entry, selected);
        var manifestPath = Path.Combine(destination, "manifest.json");
        var finished = Json.Load(manifestPath);
        provenance["mode"] = "isolated_compiled_vmat";
        provenance["raw_vmat"] = Path.GetFullPath(rawVmat);
        provenance["raw_vmat_sha256"] = rawHash;
        provenance["vmat_bytes_preserved"] = true;
        provenance["channel_conversion"] = "Source2Viewer shader-aware material unpacking";
        provenance["builtin_defaults"] = finished.Get("builtin_defaults")?.DeepClone();
        provenance["manifest"] = manifestPath;
        provenance["manifest_sha256"] = Digest.File(manifestPath);
        return (entry, provenance, cachedFiles);
    }
}
