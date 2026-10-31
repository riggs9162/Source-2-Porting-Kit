using System.Buffers.Binary;
using System.Globalization;
using System.Text;
using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>One numbered QC bodygroup and its source choices. A blank choice is null.</summary>
public sealed record RenderGroup(string Name, List<string?> Choices);

/// <summary>One LOD level's model and material replacements. A removed model maps to null.</summary>
public sealed class RenderLevel
{
    public Dictionary<string, string?> Models { get; } = new(StringComparer.Ordinal);
    public Dictionary<string, string> Materials { get; } = new(StringComparer.Ordinal);
}

/// <summary>The numbered source choices and per-LOD replacements a QC declares.</summary>
public sealed record RenderLayout(List<RenderGroup> Groups, List<RenderLevel> Levels);

/// <summary>Authored and provably zero-area triangles of one material stream.</summary>
public readonly record struct StreamCount(int Triangles, int ZeroAreaTriangles);

/// <summary>Requires every generated material stream and LOD triangle to survive Source compilation.</summary>
/// <remarks>A VTX material replacement belongs to a numeric MDL texture, not to a name. The compiler can give
/// one resource several texture identities, so replacements stay keyed by number and only projected to names
/// for comparison with the QC. Only source triangles with exactly zero area at Source float32 precision may be
/// elided, and a nonempty stream can never disappear. v3 crashed when a slot record named a stream whose model a
/// LOD removes. Here that is reported as a differing source path, so the port still stops, with a finding.</remarks>
public static class CompiledRenderCoverage
{
    private const string DegeneracyPolicy = "Only source triangles with exactly zero area at Source float32 precision may be elided; nonempty streams cannot disappear";

    private static readonly UTF8Encoding StrictUtf8 = new(false, true);

    /// <summary>Tokenizes generated QC without interpreting resource names as escape sequences.</summary>
    private static List<string> Tokens(string text) =>
        CompiledText.Tokens(string.Join('\n', PythonText.SplitLines(text).Select(CompiledText.StripComment)));

    /// <summary>Reads one complete brace-delimited QC block.</summary>
    private static (List<string> Block, int Next) Block(List<string> tokens, int start)
    {
        if (start >= tokens.Count || tokens[start] != "{") throw new RenderCoverageError("Expected a generated QC block");
        var depth = 1;
        for (var index = start + 1; index < tokens.Count; index++)
        {
            depth += (tokens[index] == "{" ? 1 : 0) - (tokens[index] == "}" ? 1 : 0);
            if (depth == 0) return (tokens.GetRange(start + 1, index - start - 1), index + 1);
        }
        throw new RenderCoverageError("Generated QC has an unterminated block");
    }

    private static string ModelKey(string name) => CompiledText.Casefold(name.Replace('\\', '/'));

    /// <summary>Reads numbered source choices and explicit per-LOD model and material replacements.</summary>
    public static RenderLayout ReadRenderLayout(string qc)
    {
        var tokens = Tokens(PythonText.Read(qc).TrimStart('﻿'));
        var layout = new RenderLayout([], [new()]);
        var index = 0;
        while (index < tokens.Count)
        {
            var command = CompiledText.Casefold(tokens[index]);
            if (command is "$body" or "$model")
            {
                layout.Groups.Add(new(tokens[index + 1], [tokens[index + 2]]));
                index += 3;
            }
            else if (command == "$bodygroup")
            {
                var name = tokens[index + 1];
                (var block, index) = Block(tokens, index + 2);
                var choices = new List<string?>();
                var cursor = 0;
                while (cursor < block.Count)
                {
                    var kind = CompiledText.Casefold(block[cursor]);
                    if (kind == "studio")
                    {
                        choices.Add(block[cursor + 1]);
                        cursor += 2;
                    }
                    else if (kind == "blank")
                    {
                        choices.Add(null);
                        cursor += 1;
                    }
                    else throw new RenderCoverageError($"Unsupported generated bodygroup token {block[cursor]}");
                }
                layout.Groups.Add(new(name, choices));
            }
            else if (command is "$lod" or "$shadowlod")
            {
                (var block, index) = Block(tokens, index + (command == "$shadowlod" ? 1 : 2));
                var level = new RenderLevel();
                var cursor = 0;
                while (cursor < block.Count)
                {
                    var kind = CompiledText.Casefold(block[cursor]);
                    if (kind == "replacemodel")
                    {
                        string source = ModelKey(block[cursor + 1]), target = block[cursor + 2].Replace('\\', '/');
                        if (level.Models.TryGetValue(source, out var known) && known != target) throw new RenderCoverageError($"Conflicting LOD replacement for {source}");
                        level.Models[source] = target;
                        cursor += 3;
                    }
                    else if (kind == "replacematerial")
                    {
                        string source = Resources.Token(block[cursor + 1]), target = Resources.Token(block[cursor + 2]);
                        if (level.Materials.TryGetValue(source, out var known) && known != target) throw new RenderCoverageError($"Conflicting LOD replacement for {source}");
                        level.Materials[source] = target;
                        cursor += 3;
                    }
                    else if (kind == "removemodel")
                    {
                        var source = ModelKey(block[cursor + 1]);
                        if (level.Models.TryGetValue(source, out var known) && known is not null) throw new RenderCoverageError($"Conflicting LOD removal for {source}");
                        level.Models[source] = null;
                        cursor += 2;
                    }
                    else if (kind is "nofacial" or "use_shadowlod_materials") cursor += 1;
                    else throw new RenderCoverageError($"Unsupported generated LOD token {block[cursor]}");
                }
                layout.Levels.Add(level);
            }
            else if (tokens[index] == "{") (_, index) = Block(tokens, index);
            else index++;
        }
        return layout;
    }

    /// <summary>Keeps each VTX replacement attached to its numeric global MDL texture identity.</summary>
    public static List<Dictionary<int, string>> ReadLodMaterialReplacementIds(byte[] data, IReadOnlyList<string> materials)
    {
        if (data.Length < 36 || BinaryPrimitives.ReadInt32LittleEndian(data) != 7) throw new RenderCoverageError("Expected a version-seven VTX material table");
        var lodCount = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(20));
        long start = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(24));
        if (lodCount is < 1 or > 8 || start < 36 || start + lodCount * 8L > data.Length) throw new RenderCoverageError("VTX material replacement list exceeds its file");
        var output = new List<Dictionary<int, string>>(lodCount);
        for (var lod = 0; lod < lodCount; lod++)
        {
            var header = start + lod * 8L;
            var count = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan((int)header));
            var first = header + BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan((int)header + 4));
            if (count < 0 || first < 0 || first + count * 6L > data.Length) throw new RenderCoverageError("VTX material replacements exceed their file");
            var replacements = new Dictionary<int, string>();
            for (var index = 0; index < count; index++)
            {
                var offset = (int)first + index * 6;
                int material = BinaryPrimitives.ReadInt16LittleEndian(data.AsSpan(offset));
                var stringOffset = offset + (long)BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(offset + 2));
                if (material < 0 || material >= materials.Count || stringOffset < 0 || stringOffset >= data.Length) throw new RenderCoverageError("VTX material replacement references an absent texture/name");
                var end = Array.IndexOf(data, (byte)0, (int)stringOffset);
                if (end < 0) throw new RenderCoverageError("VTX material replacement name is unterminated");
                var target = Resources.Token(StrictUtf8.GetString(data, (int)stringOffset, end - (int)stringOffset));
                if (!replacements.TryAdd(material, target)) throw new RenderCoverageError($"Repeated VTX material replacement for texture ID {material}");
            }
            output.Add(replacements);
        }
        return output;
    }

    /// <inheritdoc cref="ReadLodMaterialReplacementIds(byte[], IReadOnlyList{string})"/>
    public static List<Dictionary<int, string>> ReadLodMaterialReplacementIds(string vtx, IReadOnlyList<string> materials) =>
        ReadLodMaterialReplacementIds(File.ReadAllBytes(vtx), materials);

    /// <summary>Projects unambiguous replacement resource names for QC comparison and older callers.</summary>
    private static List<Dictionary<string, string>> ReplacementNames(List<Dictionary<int, string>> replacements, IReadOnlyList<string> materials)
    {
        var output = new List<Dictionary<string, string>>(replacements.Count);
        foreach (var level in replacements)
        {
            var names = new Dictionary<string, string>(StringComparer.Ordinal);
            foreach (var (textureId, target) in level)
            {
                var source = Resources.Token(materials[textureId]);
                if (names.TryGetValue(source, out var known) && known != target) throw new RenderCoverageError($"Ambiguous duplicate VTX material name {source} has different replacement targets");
                names[source] = target;
            }
            output.Add(names);
        }
        return output;
    }

    /// <summary>Reads unambiguous resource-name replacements. Runtime resolution must retain the numeric IDs.</summary>
    public static List<Dictionary<string, string>> ReadLodMaterialReplacements(byte[] vtx, IReadOnlyList<string> materials) =>
        ReplacementNames(ReadLodMaterialReplacementIds(vtx, materials), materials);

    /// <summary>Resolves one rendered skin slot through its actual texture ID and per-LOD VTX record.</summary>
    public static JsonObject ResolveLodMaterial(CompiledMetadata metadata, IReadOnlyList<Dictionary<int, string>> replacements, int lod, int skin, int slot)
    {
        var families = metadata.SkinIndices;
        if (skin < 0 || skin >= families.Count || slot < 0 || slot >= families[skin].Count || lod < 0 || lod >= replacements.Count)
            throw new RenderCoverageError("Rendered material references an absent skin slot or LOD");
        var textureId = families[skin][slot];
        if (textureId < 0 || textureId >= metadata.Materials.Count) throw new RenderCoverageError("Rendered material references an absent texture ID");
        var source = Resources.Token(metadata.Materials[textureId]);
        if (Resources.Token(metadata.Skins[skin][slot]) != source) throw new RenderCoverageError("Decoded skin names disagree with their numeric texture indices");
        return new() { ["texture_id"] = textureId, ["source_material"] = source, ["runtime_material"] = replacements[lod].GetValueOrDefault(textureId, source) };
    }

    /// <summary>Counts authored triangles and provable zero-area triangles at Source float precision.</summary>
    /// <remarks>Each corner is rounded to single precision first and the cross product runs in double precision,
    /// so a triangle counts as zero-area only when it collapses exactly in the values the compiler sees.</remarks>
    private static Dictionary<string, StreamCount> StreamCounts(Dictionary<string, List<double>> positions)
    {
        var output = new Dictionary<string, StreamCount>(StringComparer.Ordinal);
        foreach (var (material, values) in positions)
        {
            var count = values.Count / 9;
            var zero = 0;
            var triangle = new double[9];
            for (var index = 0; index < count; index++)
            {
                for (var cell = 0; cell < 9; cell++)
                {
                    triangle[cell] = (float)values[index * 9 + cell];
                    if (!double.IsFinite(triangle[cell])) throw new RenderCoverageError("Generated source contains nonfinite triangle positions");
                }
                double a0 = triangle[3] - triangle[0], a1 = triangle[4] - triangle[1], a2 = triangle[5] - triangle[2];
                double b0 = triangle[6] - triangle[0], b1 = triangle[7] - triangle[1], b2 = triangle[8] - triangle[2];
                if (a1 * b2 - a2 * b1 == 0 && a2 * b0 - a0 * b2 == 0 && a0 * b1 - a1 * b0 == 0) zero++;
            }
            output[material] = new(count, zero);
        }
        return output;
    }

    private static List<double> StreamOf(Dictionary<string, List<double>> positions, string material)
    {
        if (!positions.TryGetValue(material, out var values)) positions[material] = values = [];
        return values;
    }

    private static void ReadSmdStreams(string path, Dictionary<string, List<double>> positions)
    {
        var lines = PythonText.SplitLines(PythonText.Read(path).TrimStart('﻿')).Select(line => line.Trim()).Where(line => line.Length > 0).ToList();
        var section = lines.IndexOf("triangles");
        if (section < 0) throw new RenderCoverageError($"Generated SMD has no triangle section: {path}");
        var index = section + 1;
        while (index < lines.Count && lines[index] != "end")
        {
            var material = Resources.Token(lines[index]);
            if (index + 3 >= lines.Count) throw new RenderCoverageError($"Generated SMD has an incomplete triangle: {path}");
            var stream = StreamOf(positions, material);
            for (var corner = 1; corner <= 3; corner++)
            {
                var fields = lines[index + corner].Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
                if (fields.Length < 9) throw new RenderCoverageError($"Generated SMD has an incomplete vertex: {path}");
                for (var axis = 1; axis <= 3; axis++) stream.Add(CompiledText.Float(fields[axis]));
            }
            index += 4;
        }
        if (index >= lines.Count) throw new RenderCoverageError($"Generated SMD has no triangle terminator: {path}");
    }

    private static void ReadDmxStreams(string path, Dictionary<string, List<double>> positions)
    {
        var document = DmxDocument.Load(path);
        var meshes = document.OfType("DmeMesh");
        if (meshes.Count != 1) throw new RenderCoverageError($"Expected one generated DMX mesh: {path}");
        var state = meshes[0].Element("bindState") ?? throw new RenderCoverageError($"Expected one generated DMX mesh: {path}");
        var vertices = state.Array<V3>("positions");
        var indices = state.Contains("positionsIndices") ? state.Array<int>("positionsIndices") : Enumerable.Range(0, vertices.Count).ToList();
        if (indices.Count > 0 && (indices.Min() < 0 || indices.Max() >= vertices.Count)) throw new RenderCoverageError($"DMX position index exceeds its array: {path}");
        foreach (var faceSet in meshes[0].Elements("faceSets"))
        {
            var material = Resources.Token(faceSet!.Element("material")!.Text("mtlName") ?? "");
            var polygon = new List<int>(3);
            foreach (var index in faceSet.Array<int>("faces"))
            {
                if (index == -1)
                {
                    if (polygon.Count != 3) throw new RenderCoverageError($"Generated DMX contains a non-triangle face: {path}");
                    var stream = StreamOf(positions, material);
                    foreach (var corner in polygon)
                    {
                        var position = vertices[indices[corner]];
                        stream.AddRange([position.X, position.Y, position.Z]);
                    }
                    polygon.Clear();
                }
                else if (index < 0 || index >= indices.Count) throw new RenderCoverageError($"DMX face index exceeds its array: {path}");
                else polygon.Add(index);
            }
            if (polygon.Count > 0) throw new RenderCoverageError($"Generated DMX face has no terminator: {path}");
        }
    }

    /// <summary>Reads actual generated SMD or indexed DMX compiler input material and triangle streams.</summary>
    public static Dictionary<string, StreamCount> ReadSourceMaterialStreams(string path)
    {
        var positions = new Dictionary<string, List<double>>(StringComparer.Ordinal);
        var suffix = CompiledText.Casefold(PathText.Suffix(path));
        if (suffix == ".smd") ReadSmdStreams(path, positions);
        else if (suffix == ".dmx") ReadDmxStreams(path, positions);
        else throw new RenderCoverageError($"Unsupported generated render source: {path}");
        return StreamCounts(positions);
    }

    /// <summary>Maps emitted material aliases back to their original VMAT resource identity.</summary>
    private static Dictionary<string, string> AuthoredAliases(JsonObject appearance)
    {
        var aliases = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var (source, target) in appearance.Get("material_map").Obj())
        {
            string from = Resources.Token(source), to = Resources.Token(target.Str());
            if (from != to) aliases[to] = from;
        }
        foreach (var (target, source) in appearance.Get("material_aliases").Obj()) aliases[Resources.Token(target)] = Resources.Token(source.Str());
        return aliases;
    }

    /// <summary>Resolves an emitted material alias back to its original VMAT resource identity.</summary>
    private static string AuthoredMaterial(string material, Dictionary<string, string> aliases)
    {
        var current = Resources.Token(material);
        var visited = new HashSet<string>(StringComparer.Ordinal);
        while (aliases.TryGetValue(current, out var next) && next != current)
        {
            if (!visited.Add(current)) throw new RenderCoverageError($"Cyclic material alias {material}");
            current = next;
        }
        return current;
    }

    private static bool SamePath(string a, string b) => string.Equals(Path.GetFullPath(a), Path.GetFullPath(b), StringComparison.OrdinalIgnoreCase);

    private static void Audit(string mdl, string qc, JsonObject appearance, CompiledMetadata? known, JsonObject result, JsonArray errors, JsonArray streams)
    {
        var metadata = known ?? CompiledModelAudit.ReadCompiledModel(mdl);
        var compiled = CompiledLodVertices.ReadCompiledVertices(mdl);
        var layout = ReadRenderLayout(qc);
        var replacements = ReadLodMaterialReplacementIds(compiled.VtxBytes, metadata.Materials);
        var replacementIds = new JsonArray();
        foreach (var level in replacements)
        {
            var row = new JsonObject();
            foreach (var (textureId, target) in level) row[textureId.ToString(CultureInfo.InvariantCulture)] = target;
            replacementIds.Add(row);
        }
        result["lod_material_replacement_ids"] = replacementIds;
        var replacementNames = ReplacementNames(replacements, metadata.Materials);
        var nameRows = new JsonArray();
        foreach (var level in replacementNames)
        {
            var row = new JsonObject();
            foreach (var (source, target) in level) row[source] = target;
            nameRows.Add(row);
        }
        result["lod_material_replacements"] = nameRows;
        if (layout.Groups.Count != metadata.Bodygroups.Count || layout.Levels.Count != compiled.LodCount) throw new RenderCoverageError("Generated source bodygroups/LODs differ from the compiled layout");
        for (var lod = 0; lod < Math.Min(layout.Levels.Count, replacementNames.Count); lod++)
        {
            var expected = layout.Levels[lod].Materials;
            var actual = replacementNames[lod];
            if (expected.Count != actual.Count || expected.Any(pair => actual.GetValueOrDefault(pair.Key) != pair.Value))
                errors.Add($"LOD {lod} material replacements differ from QC: expected {Show(expected)}, compiled {Show(actual)}");
        }
        var records = new Dictionary<(int Part, int Choice, int Lod, string Material), List<JsonNode>>();
        foreach (var record in appearance.Get("lod_material_slots").Arr())
        {
            var key = (record.Key("part").Int(), record.Key("choice").Int(), record.Key("lod").Int(), Resources.Token(record.Key("compiler_material").Str()));
            if (!records.TryGetValue(key, out var rows)) records[key] = rows = [];
            rows.Add(record!);
        }
        var usedRecords = new HashSet<(int, int, int, string)>();
        var cache = new Dictionary<string, Dictionary<string, StreamCount>>(StringComparer.OrdinalIgnoreCase);
        var hashes = result.Get("source_hashes").Obj();
        var directory = CompiledText.Parent(PathText.Native(qc));
        var aliases = new Lazy<Dictionary<string, string>>(() => AuthoredAliases(appearance));
        var skinCount = metadata.Skins.Count;
        for (var part = 0; part < layout.Groups.Count; part++)
        {
            var group = layout.Groups[part];
            if (group.Choices.Count != metadata.Bodygroups[part].Choices.Count) throw new RenderCoverageError($"Compiled bodygroup {part} has a different choice count");
            for (var choice = 0; choice < group.Choices.Count; choice++)
            {
                var baseName = group.Choices[choice];
                var meshes = compiled.Meshes.Where(mesh => mesh.Part == part && mesh.Choice == choice).ToList();
                for (var lod = 0; lod < layout.Levels.Count; lod++)
                {
                    var level = layout.Levels[lod];
                    var filename = string.IsNullOrEmpty(baseName) ? null : level.Models.GetValueOrDefault(ModelKey(baseName), baseName);
                    var source = string.IsNullOrEmpty(filename) ? null : CompiledText.Join(directory, filename);
                    if (source is not null && PathText.Suffix(source).Length == 0) source += ".smd";
                    if (source is not null && !cache.ContainsKey(source))
                    {
                        cache[source] = ReadSourceMaterialStreams(source);
                        hashes[source] = Digest.File(source);
                    }
                    Dictionary<string, StreamCount> expectedStreams = source is not null ? cache[source] : [];
                    var actualStreams = new Dictionary<string, int>(StringComparer.Ordinal);
                    var materialMeshes = new Dictionary<string, List<CompiledMesh>>(StringComparer.Ordinal);
                    foreach (var mesh in meshes)
                    {
                        var count = mesh.LodTriangles[lod].Length / 3;
                        var material = Resources.Token(metadata.Skins[0][mesh.MaterialSlot]);
                        actualStreams[material] = actualStreams.GetValueOrDefault(material) + count;
                        if (count == 0) continue;
                        if (!materialMeshes.TryGetValue(material, out var list)) materialMeshes[material] = list = [];
                        list.Add(mesh);
                    }
                    foreach (var material in expectedStreams.Keys.Union(actualStreams.Keys).Order(StringComparer.Ordinal))
                    {
                        var expected = expectedStreams.GetValueOrDefault(material);
                        var actual = actualStreams.GetValueOrDefault(material);
                        if (expected.Triangles == 0 && actual == 0) continue;
                        var removed = expected.Triangles - actual;
                        JsonArray runtimeMaterials = [], authoredMaterials = [], materialBindings = [];
                        streams.Add(new JsonObject
                        {
                            ["part"] = part, ["choice"] = choice, ["lod"] = lod, ["source"] = source, ["compiler_material"] = material,
                            ["source_triangles"] = expected.Triangles, ["source_zero_area_triangles"] = expected.ZeroAreaTriangles, ["compiled_triangles"] = actual,
                            ["runtime_materials"] = runtimeMaterials, ["authored_materials"] = authoredMaterials, ["material_bindings"] = materialBindings,
                            ["removed_zero_area_triangles"] = Math.Min(Math.Max(removed, 0), expected.ZeroAreaTriangles),
                            ["unaccounted_lost_triangles"] = Math.Max(removed - expected.ZeroAreaTriangles, 0)
                        });
                        var label = $"{part}/{choice}/{lod}/{material}";
                        if (removed < 0 || removed > expected.ZeroAreaTriangles || (expected.Triangles > expected.ZeroAreaTriangles && actual == 0))
                            errors.Add($"Bodypart {part} choice {choice} LOD {lod} material {material}: source {expected.Triangles} triangles ({expected.ZeroAreaTriangles} zero-area), compiled {actual}");
                        var rows = records.GetValueOrDefault((part, choice, lod, material)) ?? [];
                        if (records.Count > 0 && expectedStreams.ContainsKey(material) && rows.Count == 0)
                            errors.Add($"Missing authored material-slot provenance for bodypart {part} choice {choice} LOD {lod} material {material}");
                        if (rows.Count > 0)
                        {
                            usedRecords.Add((part, choice, lod, material));
                            if (rows.Sum(record => record.Key("triangle_count").Lng()) != expected.Triangles) errors.Add($"Authored material-slot triangle count differs from generated source: {label}");
                            foreach (var record in rows)
                            {
                                var declared = PathText.Native(record.Key("source").Str());
                                if (source is null || !SamePath(Path.IsPathFullyQualified(declared) ? declared : CompiledText.Join(directory, declared), source))
                                    errors.Add($"Authored material-slot source path differs: {label}");
                            }
                        }
                        for (var skin = 0; skin < skinCount; skin++)
                        {
                            var bindings = new JsonArray();
                            var resolved = new List<string>();
                            foreach (var mesh in materialMeshes.GetValueOrDefault(material) ?? [])
                            {
                                var binding = ResolveLodMaterial(metadata, replacements, lod, skin, mesh.MaterialSlot);
                                binding["mesh"] = mesh.Mesh;
                                binding["skin_slot"] = mesh.MaterialSlot;
                                var sourceMaterial = binding.Get("source_material").Str();
                                var runtimeMaterial = binding.Get("runtime_material").Str();
                                var wanted = level.Materials.GetValueOrDefault(sourceMaterial, sourceMaterial);
                                if (runtimeMaterial != wanted)
                                    errors.Add($"Numeric LOD material differs from QC: {label}/skin{skin}, texture ID {binding.Get("texture_id").Int()}: expected {wanted}, compiled {runtimeMaterial}");
                                bindings.Add(binding);
                                if (!resolved.Contains(runtimeMaterial)) resolved.Add(runtimeMaterial);
                            }
                            materialBindings.Add(bindings);
                            if (resolved.Count > 1) errors.Add($"One generated material stream resolves to multiple runtime materials: {label}/skin{skin}");
                            var runtime = resolved.Count > 0 ? resolved[0] : null;
                            var authored = runtime is not null ? AuthoredMaterial(runtime, aliases.Value) : null;
                            runtimeMaterials.Add(runtime);
                            authoredMaterials.Add(authored);
                            foreach (var record in rows)
                            {
                                var fallback = record.Key("runtime_material").Str();
                                var wanted = record.Has("runtime_materials") ? record.Get("runtime_materials").Arr().Select(item => item.Str()).ToList() : Enumerable.Repeat(fallback, skinCount).ToList();
                                var originals = record.Get("authored_materials").Arr().Select(item => item.Str()).ToList();
                                if (wanted.Count != skinCount || (runtime is not null && Resources.Token(wanted[skin]) != runtime))
                                    errors.Add($"Runtime LOD material differs from authored slot: {label}/skin{skin}");
                                if (originals.Count > 0 && (originals.Count != skinCount || authored != Resources.Token(originals[skin])))
                                    errors.Add($"Original VMAT binding differs after skin/LOD resolution: {label}/skin{skin}");
                            }
                        }
                    }
                }
            }
        }
        foreach (var (key, rows) in records)
        {
            if (usedRecords.Contains(key) || !rows.Any(record => record.Key("triangle_count").Bool())) continue;
            errors.Add($"Nonempty authored material-slot stream is absent from generated sources: ({key.Part}, {key.Choice}, {key.Lod}, {PythonText.Repr(key.Material)})");
        }
    }

    private static string Show(Dictionary<string, string> map) =>
        "{" + string.Join(", ", map.Select(pair => $"{PythonText.Repr(pair.Key)}: {PythonText.Repr(pair.Value)}")) + "}";

    /// <summary>Audits every source stream, numbered choice, LOD and resolved skin material binding.</summary>
    /// <param name="mdl">The compiled model.</param>
    /// <param name="qc">The QC that produced it. Its sources are read beside it.</param>
    /// <param name="appearance">The authored appearance report.</param>
    /// <param name="metadata">The model's records when the caller already holds them.</param>
    public static JsonObject AuditRenderCoverage(string mdl, string qc, JsonObject appearance, CompiledMetadata? metadata = null)
    {
        JsonArray errors = [], streams = [];
        var result = new JsonObject
        {
            ["ok"] = false, ["errors"] = errors, ["streams"] = streams, ["source_hashes"] = new JsonObject(), ["lod_material_replacements"] = new JsonArray(),
            ["lod_material_replacement_ids"] = new JsonArray(), ["degeneracy_policy"] = DegeneracyPolicy
        };
        try
        {
            Audit(mdl, qc, appearance, metadata, result, errors, streams);
        }
        catch (Exception exception) when (CompiledText.IsAuditFault(exception) || exception is InvalidCastException)
        {
            errors.Add($"Compiled render coverage audit failed: {CompiledText.Describe(exception)}");
        }
        result["ok"] = errors.Count == 0;
        result["source_triangles"] = streams.Sum(row => row.Get("source_triangles").Lng());
        result["compiled_triangles"] = streams.Sum(row => row.Get("compiled_triangles").Lng());
        return result;
    }
}
