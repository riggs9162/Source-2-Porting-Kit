using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>Audits compiled Source 1 model data against editable ragdoll sources and payloads.</summary>
/// <remarks>Offsets follow Valve Source SDK 2013 studio.h and optimize.h for v48 and v49 serialized records.
/// Nothing here trusts studiomdl's success: a model has to prove it kept its binds, choices, skins, metadata,
/// flexes and LODs, because a defect in any of them compiles cleanly and only shows up in the game.</remarks>
public static partial class CompiledModelAudit
{
    private const string SourceLayout = "Valve Source SDK 2013 studio.h/optimize.h v48-v49 serialized records";

    private static readonly HashSet<string> TextureParameters = new(StringComparer.Ordinal)
    {
        "$basetexture", "$basetexture2", "$bumpmap", "$bumpmap2", "$normalmap", "$detail", "$envmap",
        "$envmapmask", "$phongexponenttexture", "$lightwarptexture", "$selfillummask",
        "$blendmodulatetexture", "$dudvmap", "$flowmap", "$flow_noise_texture",
        "$wrinkle", "$stretch", "$compress", "$corneatexture", "$iris", "$ambientoccltexture"
    };

    [GeneratedRegex(@"""?(\$[A-Za-z_0-9]+)""?[\s\x1c-\x1f]+(?:""([^""\r\n]*)""|([^\s\x1c-\x1f{}]+))")]
    private static partial Regex VmtParameter();

    private sealed record QcBone(string Parent, double[] Position, double[] Rotation);
    private sealed record QcGroup(string Name, List<bool> Choices);
    private sealed record QcAttachment(string Name, string Bone, double[] Position, double[] Rotation);
    private sealed record QcHitbox(string Set, int Group, string Bone, double[] Minimum, double[] Maximum);

    /// <summary>The emitted QC declarations that have directly checkable binary equivalents.</summary>
    private sealed class QcExpectations
    {
        public Dictionary<string, QcBone> Bones { get; } = new(StringComparer.Ordinal);
        public List<QcGroup> Bodygroups { get; } = [];
        public List<QcAttachment> Attachments { get; } = [];
        public List<QcHitbox> Hitboxes { get; } = [];
        public List<string> Jiggles { get; } = [];
        public int Lods { get; set; }
    }

    /// <summary>Converts Source QAngle pitch, yaw, roll degrees to an orthonormal matrix.</summary>
    private static double[,] Rotation(IReadOnlyList<double> angles)
    {
        double pitch = Fmt.Radians(angles[0]), yaw = Fmt.Radians(angles[1]), roll = Fmt.Radians(angles[2]);
        double sp = Math.Sin(pitch), sy = Math.Sin(yaw), sr = Math.Sin(roll);
        double cp = Math.Cos(pitch), cy = Math.Cos(yaw), cr = Math.Cos(roll);
        return new[,]
        {
            { cp * cy, sr * sp * cy - cr * sy, cr * sp * cy + sr * sy },
            { cp * sy, sr * sp * sy + cr * cy, cr * sp * sy - sr * cy },
            { -sp, sr * cp, cr * cp }
        };
    }

    /// <summary>Converts serialized XYZW unit quaternions into matrices without sign ambiguity.</summary>
    private static double[,] Quaternion(double[] value)
    {
        var length = Math.Sqrt(value.Sum(component => component * component));
        if (length < 1e-12 || !value.All(double.IsFinite)) throw new ModelAuditError("Invalid compiled bone quaternion");
        double x = value[0] / length, y = value[1] / length, z = value[2] / length, w = value[3] / length;
        return new[,]
        {
            { 1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w) },
            { 2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w) },
            { 2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y) }
        };
    }

    /// <summary>Reads the emitted QC declarations that have directly checkable binary equivalents.</summary>
    private static QcExpectations ReadExpectations(string path)
    {
        var result = new QcExpectations();
        QcGroup? group = null;
        var hitboxSet = "default";
        foreach (var line in PythonText.SplitLines(PythonText.Read(path)))
        {
            var values = CompiledText.Tokens(CompiledText.StripComment(line));
            if (values.Count == 0) continue;
            var kind = CompiledText.Casefold(values[0]);
            if (kind == "$definebone") result.Bones[values[1]] = new(values[2], CompiledText.Floats(values, 3, 6), CompiledText.Floats(values, 6, 9));
            else if (kind is "$body" or "$model") result.Bodygroups.Add(new(values[1], [false]));
            else if (kind == "$bodygroup")
            {
                group = new(values[1], []);
                result.Bodygroups.Add(group);
            }
            else if (group is not null && kind is "studio" or "blank") group.Choices.Add(kind == "blank");
            else if (group is not null && kind == "}") group = null;
            else if (kind == "$attachment")
            {
                var rotate = values.IndexOf("rotate");
                double[] rotation = rotate >= 0 ? CompiledText.Floats(values, rotate + 1, rotate + 4) : [0, 0, 0];
                result.Attachments.Add(new(values[1], values[2], CompiledText.Floats(values, 3, 6), rotation));
            }
            else if (kind == "$hboxset") hitboxSet = values[1];
            else if (kind == "$hbox") result.Hitboxes.Add(new(hitboxSet, CompiledText.Int(values[1]), values[2], CompiledText.Floats(values, 3, 6), CompiledText.Floats(values, 6, 9)));
            else if (kind == "$jigglebone") result.Jiggles.Add(values[1]);
            else if (kind is "$lod" or "$shadowlod") result.Lods++;
        }
        return result;
    }

    /// <summary>Reads output flex names from studiohdr offsets 260 and 264 and their relative four-byte descriptors.</summary>
    public static List<string> ReadCompiledFlexDescriptions(string mdl)
    {
        var source = new CompiledBinary(mdl);
        source.Require(0, 268);
        if (!source.HasMagic("IDST") || source.Int(4) is not (48 or 49)) throw new ModelAuditError("Flex descriptor reader requires a v48/v49 IDST model");
        var header = source.Ints(260, 2);
        return source.Table(header[0], header[1], 4).Select(offset => source.String(offset + source.Int(offset))).ToList();
    }

    private static string Fixed64(CompiledBinary source, long offset)
    {
        var name = source.Data.AsSpan((int)offset, 64);
        var end = name.IndexOf((byte)0);
        return System.Text.Encoding.UTF8.GetString(end < 0 ? name : name[..end]);
    }

    /// <summary>Reads v48 and v49 studiohdr, bone, mesh, skin, flex and metadata records from an MDL.</summary>
    public static CompiledMetadata ReadCompiledModel(string path)
    {
        var source = new CompiledBinary(path);
        source.Require(0, 308);
        if (!source.HasMagic("IDST")) throw new ModelAuditError($"{source.Name}: expected IDST model header");
        source.Require(4, 8);
        var version = source.Int(4);
        if (version is not (48 or 49)) throw new ModelAuditError($"Unsupported compiled MDL version {version}");
        var declaredLength = source.Int(76);
        if (declaredLength != source.Data.Length) throw new ModelAuditError($"MDL length {declaredLength} differs from file size {source.Data.Length}");
        var result = new CompiledMetadata { Version = version, Checksum = source.UInt(8) };
        var pair = source.Ints(156, 2);
        foreach (var offset in source.Table(pair[0], pair[1], 216))
        {
            var head = source.Ints(offset, 2);
            var state = source.Ints(offset + 160, 4);
            var bone = new CompiledBone(source.String(offset + head[0]), head[1], source.Floats(offset + 32, 3), source.Floats(offset + 44, 4), state[0], state[1], state[2], state[3]);
            if (!bone.Position.Concat(bone.Quaternion).All(double.IsFinite)) throw new ModelAuditError($"Bone {bone.Name} has non-finite bind values");
            if (bone.ProceduralType == 5)
            {
                if (bone.ProceduralOffset == 0) throw new ModelAuditError($"Jiggle bone {bone.Name} has no procedural record");
                bone.JiggleFlags = source.Int(offset + bone.ProceduralOffset);
                bone.JiggleParameters = source.Floats(offset + bone.ProceduralOffset + 4, 34);
            }
            result.Bones.Add(bone);
        }
        foreach (var bone in result.Bones)
        {
            if (bone.Parent < -1 || bone.Parent >= result.Bones.Count) throw new ModelAuditError($"Bone {bone.Name} has invalid parent {bone.Parent}");
            bone.ParentName = bone.Parent >= 0 ? result.Bones[bone.Parent].Name : "";
        }
        pair = source.Ints(204, 2);
        foreach (var offset in source.Table(pair[0], pair[1], 64)) result.Materials.Add(source.String(offset + source.Int(offset)).Replace('\\', '/'));
        pair = source.Ints(212, 2);
        foreach (var offset in source.Table(pair[0], pair[1], 4)) result.MaterialPaths.Add(source.String(source.Int(offset)).Replace('\\', '/'));
        var skinHeader = source.Ints(220, 3);
        var slots = skinHeader[0];
        foreach (var offset in source.Table(skinHeader[1], skinHeader[2], Math.Max(1, slots * 2L)))
        {
            source.Require(offset, slots * 2L);
            var row = new List<int>(slots);
            for (var slot = 0; slot < slots; slot++) row.Add(source.Short(offset + slot * 2L));
            if (row.Any(value => value < 0 || value >= result.Materials.Count)) throw new ModelAuditError("Compiled skin family references an absent texture");
            result.SkinIndices.Add(row);
            result.Skins.Add(row.ConvertAll(value => result.Materials[value]));
        }
        pair = source.Ints(232, 2);
        foreach (var partOffset in source.Table(pair[0], pair[1], 16))
        {
            var part = source.Ints(partOffset, 4);
            var group = new CompiledBodygroup(source.String(partOffset + part[0]), part[2], []);
            foreach (var modelOffset in source.Table(part[1], partOffset + part[3], 148))
            {
                source.Require(modelOffset, 148);
                var modelName = Fixed64(source, modelOffset);
                var model = source.Ints(modelOffset + 72, 3);
                var choice = new CompiledChoice(modelName, model[0], model[2], []);
                foreach (var meshOffset in source.Table(model[0], modelOffset + model[1], 116))
                {
                    var record = source.Ints(meshOffset, 6);
                    if (record[0] < 0 || record[0] >= slots) throw new ModelAuditError($"Mesh references invalid skin slot {record[0]}");
                    var mesh = new CompiledRenderMesh(record[0], record[2], []);
                    foreach (var flexOffset in source.Table(record[4], meshOffset + record[5], 60))
                    {
                        var descriptor = source.Int(flexOffset);
                        var deltas = source.Ints(flexOffset + 20, 3);
                        int animationType = source.Byte(flexOffset + 32);
                        var stride = animationType switch { 0 => 16, 1 => 18, _ => 0 };
                        if (stride == 0) throw new ModelAuditError($"Unsupported compiled vertex animation type {animationType}");
                        source.Table(deltas[0], flexOffset + deltas[1], stride);
                        mesh.Flexes.Add(new(descriptor, deltas[0], deltas[2], animationType));
                    }
                    choice.Meshes.Add(mesh);
                    result.Meshes.Add(mesh);
                }
                group.Choices.Add(choice);
            }
            result.Bodygroups.Add(group);
        }
        pair = source.Ints(240, 2);
        foreach (var offset in source.Table(pair[0], pair[1], 92))
        {
            var head = source.Ints(offset, 3);
            var cells = source.Floats(offset + 12, 12);
            double[][] matrix = [cells[..4], cells[4..8], cells[8..12]];
            result.Attachments.Add(new(source.String(offset + head[0]), head[1], head[2], matrix));
        }
        pair = source.Ints(172, 2);
        foreach (var setOffset in source.Table(pair[0], pair[1], 12))
        {
            var head = source.Ints(setOffset, 3);
            var setName = source.String(setOffset + head[0]);
            foreach (var offset in source.Table(head[1], setOffset + head[2], 68))
            {
                var box = source.Ints(offset, 2);
                result.Hitboxes.Add(new(setName, box[0], box[1], source.Floats(offset + 8, 3), source.Floats(offset + 20, 3)));
            }
        }
        pair = source.Ints(260, 2);
        foreach (var offset in source.Table(pair[0], pair[1], 4)) result.FlexDescriptors.Add(source.String(offset + source.Int(offset)));
        pair = source.Ints(268, 2);
        foreach (var offset in source.Table(pair[0], pair[1], 20))
        {
            source.Require(offset, 20);
            int kind = source.Int(offset), name = source.Int(offset + 4);
            double low = source.Float(offset + 12), high = source.Float(offset + 16);
            result.FlexControllers.Add(new(source.String(offset + name), source.String(offset + kind), low, high));
        }
        pair = source.Ints(276, 2);
        foreach (var offset in source.Table(pair[0], pair[1], 12))
        {
            var rule = source.Ints(offset, 3);
            source.Table(rule[1], offset + rule[2], 8);
            result.FlexRules.Add(new(rule[0], rule[1]));
        }
        var descriptors = result.FlexDescriptors.Count;
        foreach (var flex in result.Meshes.SelectMany(mesh => mesh.Flexes))
        {
            if (flex.Descriptor < 0 || flex.Descriptor >= descriptors) throw new ModelAuditError("Mesh flex references an absent descriptor");
            flex.Name = result.FlexDescriptors[flex.Descriptor];
        }
        foreach (var rule in result.FlexRules)
        {
            if (rule.Descriptor < 0 || rule.Descriptor >= descriptors) throw new ModelAuditError("Flex rule references an absent descriptor");
            rule.Name = result.FlexDescriptors[rule.Descriptor];
        }
        return result;
    }

    /// <summary>Reads packed OptimizedModel file, body, submodel and LOD headers from optimize.h.</summary>
    private static JsonObject ReadVtx(string path)
    {
        var source = new CompiledBinary(path);
        var version = source.Int(0);
        source.Require(16, 20);
        var checksum = source.UInt(16);
        var header = source.Ints(20, 4);
        var lodCount = header[0];
        var bodygroups = new JsonArray();
        var result = new JsonObject { ["version"] = version, ["checksum"] = checksum, ["lod_count"] = lodCount, ["bodygroups"] = bodygroups };
        if (version != 7 || lodCount is < 1 or > 8) throw new ModelAuditError($"Invalid VTX version/LOD count {version}/{lodCount}");
        foreach (var partOffset in source.Table(header[2], header[3], 8))
        {
            var models = source.Ints(partOffset, 2);
            var part = new JsonArray();
            foreach (var modelOffset in source.Table(models[0], partOffset + models[1], 8))
            {
                var lodTable = source.Ints(modelOffset, 2);
                var lods = new JsonArray();
                foreach (var offset in source.Table(lodTable[0], modelOffset + lodTable[1], 12))
                {
                    source.Require(offset, 12);
                    int meshes = source.Int(offset), meshIndex = source.Int(offset + 4);
                    var distance = source.Float(offset + 8);
                    source.Table(meshes, offset + meshIndex, 9);
                    lods.Add(new JsonObject { ["meshes"] = meshes, ["distance"] = distance });
                }
                part.Add(lods);
            }
            bodygroups.Add(part);
        }
        return result;
    }

    /// <summary>Checks complete emitted bind definitions by name, parent, position and rotation.</summary>
    private static JsonObject AuditBones(CompiledMetadata compiled, QcExpectations expected, JsonArray errors)
    {
        var bones = compiled.Bones;
        var lookup = new Dictionary<string, CompiledBone>(StringComparer.Ordinal);
        foreach (var bone in bones) lookup[CompiledText.Casefold(bone.Name)] = bone;
        if (bones.Count is 0 or > 256 || lookup.Count != bones.Count) errors.Add($"Compiled bone count/names violate the 256-bone limit: {bones.Count}");
        double maximumPosition = 0.0, maximumRotation = 0.0;
        var mismatches = new JsonArray();
        foreach (var (name, source) in expected.Bones)
        {
            if (!lookup.TryGetValue(CompiledText.Casefold(name), out var bone))
            {
                errors.Add($"Missing defined bone {name}");
                continue;
            }
            if (CompiledText.Casefold(bone.ParentName) != CompiledText.Casefold(source.Parent)) errors.Add($"Bone {name} parent {bone.ParentName} differs from QC {source.Parent}");
            var position = double.NegativeInfinity;
            for (var axis = 0; axis < 3; axis++) position = Math.Max(position, Math.Abs(bone.Position[axis] - source.Position[axis]));
            double[,] actual = Quaternion(bone.Quaternion), wanted = Rotation(source.Rotation);
            var rotation = double.NegativeInfinity;
            for (var row = 0; row < 3; row++)
                for (var column = 0; column < 3; column++)
                    rotation = Math.Max(rotation, Math.Abs(actual[row, column] - wanted[row, column]));
            if (position > maximumPosition) maximumPosition = position;
            if (rotation > maximumRotation) maximumRotation = rotation;
            if (position > 0.0002 || rotation > 0.00002) mismatches.Add(new JsonObject { ["name"] = name, ["position_error_inches"] = position, ["rotation_matrix_error"] = rotation });
        }
        if (mismatches.Count > 0) errors.Add($"{mismatches.Count} compiled bone bind transforms differ from $definebone");
        return new()
        {
            ["count"] = bones.Count, ["expected"] = expected.Bones.Count, ["maximum_position_error_inches"] = maximumPosition,
            ["maximum_rotation_matrix_error"] = maximumRotation, ["mismatches"] = mismatches
        };
    }

    /// <summary>Compares binary bodypart choice counts, blank alternatives and authored group reports.</summary>
    private static JsonObject AuditBodygroups(CompiledMetadata compiled, QcExpectations expected, JsonObject appearance, JsonArray errors)
    {
        var lookup = new Dictionary<string, CompiledBodygroup>(StringComparer.Ordinal);
        foreach (var part in compiled.Bodygroups) lookup[CompiledText.Casefold(part.Name)] = part;
        foreach (var group in expected.Bodygroups)
        {
            if (!lookup.TryGetValue(CompiledText.Casefold(group.Name), out var actual) || actual.Choices.Count != group.Choices.Count)
            {
                errors.Add($"Bodygroup {group.Name} choice count differs from QC");
                continue;
            }
            for (var index = 0; index < group.Choices.Count; index++)
                if (actual.Choices[index].MeshCount == 0 != group.Choices[index]) errors.Add($"Bodygroup {group.Name} choice {index} blank state differs from QC");
        }
        foreach (var group in appearance.Get("bodygroups").Arr())
        {
            var name = group.Key("name").Str();
            if (!lookup.TryGetValue(CompiledText.Casefold(name), out var actual) || actual.Choices.Count != group.Key("choices").Arr().Count)
                errors.Add($"Authored bodygroup {name} choices were lost in compilation");
        }
        var groups = new JsonArray();
        foreach (var part in compiled.Bodygroups)
        {
            var blanks = Enumerable.Range(0, part.Choices.Count).Where(index => part.Choices[index].MeshCount == 0);
            groups.Add(new JsonObject { ["name"] = part.Name, ["choices"] = part.Choices.Count, ["blank_choices"] = CompiledText.Integers(blanks) });
        }
        return new() { ["count"] = lookup.Count, ["groups"] = groups };
    }

    /// <summary>Resolves the concrete final material payload directory without searching unrelated games.</summary>
    private static string MaterialRoot(JsonObject? map, string? payload, string mdl)
    {
        if (map is not null)
        {
            var value = map.Get("material_root").Str();
            if (value.Length > 0) return PathText.Native(value);
            payload = map.Has("addon_root") ? map.Get("addon_root").Str() : map.Has("root") ? map.Get("root").Str() : map.Get("path").Str();
        }
        if (!string.IsNullOrEmpty(payload))
        {
            var bottom = Path.TrimEndingDirectorySeparator(PathText.Native(payload));
            return CompiledText.Casefold(Path.GetFileName(bottom)) == "materials" ? bottom : CompiledText.Join(bottom, "materials");
        }
        for (var parent = Path.GetDirectoryName(mdl); !string.IsNullOrEmpty(parent); parent = Path.GetDirectoryName(parent))
            if (CompiledText.Casefold(Path.GetFileName(parent)) == "models") return CompiledText.Join(CompiledText.Parent(parent), "materials");
        return CompiledText.Join(CompiledText.Parent(mdl), "materials");
    }

    /// <summary>Resolves a relative Source resource token while rejecting paths outside its payload.</summary>
    private static string ResourcePath(string root, string name, string suffix)
    {
        var token = name.Replace('\\', '/').Trim('/');
        if (CompiledText.Casefold(token).EndsWith(suffix, StringComparison.Ordinal)) token = token[..^suffix.Length];
        var path = CompiledText.Join(root, token + suffix);
        var boundary = Path.TrimEndingDirectorySeparator(Path.GetFullPath(root)) + Path.DirectorySeparatorChar;
        if (!Path.GetFullPath(path).StartsWith(boundary, StringComparison.OrdinalIgnoreCase)) throw new ModelAuditError($"Unsafe compiled resource path {name}");
        return path;
    }

    private static bool HasVtfHeader(string path)
    {
        using var stream = File.OpenRead(path);
        Span<byte> magic = stackalloc byte[4];
        return stream.ReadAtLeast(magic, 4, false) == 4 && magic.SequenceEqual("VTF\0"u8);
    }

    /// <summary>Validates compiled skin bindings and each shipped VMT and referenced VTF resource.</summary>
    private static JsonObject AuditMaterials(CompiledMetadata compiled, JsonObject appearance, JsonObject? payloadMap, string? payload, string mdl, JsonArray errors)
    {
        var root = MaterialRoot(payloadMap, payload, mdl);
        var references = new List<string>();
        var checkedTextures = new HashSet<string>(StringComparer.Ordinal);
        var directories = compiled.MaterialPaths.Count > 0 ? compiled.MaterialPaths : [""];
        foreach (var token in compiled.Materials)
        {
            var folded = CompiledText.Casefold(token);
            if (folded.Contains("___error", StringComparison.Ordinal) || folded is "error" or "debug/debugempty") errors.Add($"Compiled model contains error material {token}");
            var candidates = directories.ConvertAll(directory => ResourcePath(root, directory.TrimEnd('/') + "/" + token, ".vmt"));
            var path = candidates.Find(File.Exists);
            if (path is null)
            {
                errors.Add($"Missing compiled material {token}: " + string.Join(", ", candidates));
                continue;
            }
            var text = PythonText.Read(path).TrimStart('﻿');
            references.Add(path);
            foreach (Match match in VmtParameter().Matches(text))
            {
                var parameter = CompiledText.Casefold(match.Groups[1].Value);
                var texture = match.Groups[2].Success ? match.Groups[2].Value : match.Groups[3].Value;
                if (!TextureParameters.Contains(parameter) || texture.Length == 0) continue;
                if (texture.StartsWith("_rt_", StringComparison.Ordinal) || (parameter == "$envmap" && CompiledText.Casefold(texture) == "env_cubemap")) continue;
                var resource = ResourcePath(root, texture, ".vtf");
                checkedTextures.Add(resource);
                if (!File.Exists(resource)) errors.Add($"Missing texture {texture} referenced by {Path.GetFileName(path)}");
                else if (!HasVtfHeader(resource)) errors.Add($"Invalid VTF header {resource}");
            }
        }
        var authored = appearance.Get("skins").Arr();
        if (authored.Count > 1 && compiled.Skins.Count != authored.Count) errors.Add($"Compiled skin family count {compiled.Skins.Count} differs from source {authored.Count}");
        if (authored.Count > 0 && compiled.Skins.Count > 0)
        {
            var aliases = new Dictionary<string, string>(StringComparer.Ordinal);
            foreach (var (source, target) in appearance.Get("material_map").Obj()) aliases[CompiledText.Casefold(source)] = CompiledText.Casefold(target.Str("None"));
            string Alias(JsonNode? token)
            {
                var folded = CompiledText.Casefold(token.Str("None"));
                return aliases.GetValueOrDefault(folded, folded);
            }
            var original = authored[0].Get("materials").Arr().Select(Alias).ToList();
            var internalSlots = new Dictionary<string, JsonNode>(StringComparer.Ordinal);
            foreach (var item in appearance.Get("material_slot_skins").Arr()) internalSlots[CompiledText.Casefold(item.Key("compiler_material").Str("None"))] = item!;
            var firstFamily = compiled.Skins[0].Select(CompiledText.Casefold).ToHashSet(StringComparer.Ordinal);
            foreach (var (name, item) in internalSlots)
            {
                if (!firstFamily.Contains(name)) errors.Add($"Missing internal material carrier skin slot {name}");
                if (item.Key("skin_materials").Arr().Count != compiled.Skins.Count) errors.Add($"Internal material carrier skin family count differs: {name}");
            }
            for (var family = 0; family < Math.Min(authored.Count, compiled.Skins.Count); family++)
            {
                var replacements = new Dictionary<string, string>(StringComparer.Ordinal);
                foreach (var (from, to) in original.Zip(authored[family].Get("materials").Arr().Select(Alias))) replacements[from] = to;
                for (var slot = 0; slot < compiled.Skins[0].Count; slot++)
                {
                    var baseName = CompiledText.Casefold(compiled.Skins[0][slot]);
                    var target = CompiledText.Casefold(compiled.Skins[family][slot]);
                    var inner = internalSlots.GetValueOrDefault(baseName)?.Key("skin_materials").Arr();
                    var wanted = inner is not null && family < inner.Count ? CompiledText.Casefold(inner[family].Str("None")) : replacements.GetValueOrDefault(baseName, baseName);
                    if (target != wanted) errors.Add($"Skin {family} slot {slot}: {target} differs from source {wanted}");
                }
            }
        }
        var families = new JsonArray(compiled.Skins.Select(family => (JsonNode?)CompiledText.Strings(family)).ToArray());
        var indices = new JsonArray(compiled.SkinIndices.Select(family => (JsonNode?)CompiledText.Integers(family)).ToArray());
        return new()
        {
            ["materials"] = CompiledText.Strings(compiled.Materials), ["search_paths"] = CompiledText.Strings(compiled.MaterialPaths), ["material_root"] = root,
            ["family_count"] = compiled.Skins.Count, ["skin_bindings"] = families, ["skin_texture_indices"] = indices, ["vmts"] = CompiledText.Strings(references),
            ["referenced_vtfs"] = CompiledText.Strings(checkedTextures.Order(StringComparer.Ordinal))
        };
    }

    /// <summary>Tests two values as a tolerance check does: within range and finite, or exactly equal.</summary>
    private static bool Close(double a, double b, double tolerance) => (Math.Abs(a - b) <= tolerance && double.IsFinite(b)) || a == b;

    private static bool AllClose(double[] a, double[] b, double tolerance) => a.Length == b.Length && a.Zip(b).All(pair => Close(pair.First, pair.Second, tolerance));

    /// <summary>Verifies attachment transforms, hitbox bounds and actual STUDIO_PROC_JIGGLE records.</summary>
    private static JsonObject AuditMetadata(CompiledMetadata compiled, QcExpectations expected, JsonObject report, JsonArray errors)
    {
        var bones = compiled.Bones;
        foreach (var (key, count) in new[] { ("attachments", compiled.Attachments.Count), ("hitboxes", compiled.Hitboxes.Count) })
        {
            var source = report.Get(key);
            JsonNode? sourceCount = source switch
            {
                JsonObject map => map.Get("count"),
                JsonArray list => list.Count(item => item.Get("status").Str("None") != "unsupported"),
                _ => null
            };
            if (sourceCount is not null && count != sourceCount.Dbl()) errors.Add($"Compiled {key} count {count} differs from source {sourceCount.Str()}");
        }
        List<int> invalidBones = [], nonfinite = [], inverted = [];
        for (var index = 0; index < compiled.Hitboxes.Count; index++)
        {
            var actual = compiled.Hitboxes[index];
            var label = $"{index} ({actual.Set})";
            if (actual.Bone < 0 || actual.Bone >= bones.Count)
            {
                invalidBones.Add(index);
                errors.Add($"Compiled hitbox {label} references invalid bone {actual.Bone}");
            }
            if (!actual.Minimum.All(double.IsFinite) || !actual.Maximum.All(double.IsFinite))
            {
                nonfinite.Add(index);
                errors.Add($"Compiled hitbox {label} has non-finite bounds");
            }
            else if (actual.Minimum.Zip(actual.Maximum).Any(pair => pair.First > pair.Second))
            {
                inverted.Add(index);
                errors.Add($"Compiled hitbox {label} has inverted extents");
            }
        }
        var attachments = new Dictionary<string, CompiledAttachment>(StringComparer.Ordinal);
        foreach (var item in compiled.Attachments) attachments[CompiledText.Casefold(item.Name)] = item;
        foreach (var item in expected.Attachments)
        {
            if (!attachments.TryGetValue(CompiledText.Casefold(item.Name), out var actual))
            {
                errors.Add($"Missing attachment {item.Name}");
                continue;
            }
            if (actual.Bone < 0 || actual.Bone >= bones.Count || CompiledText.Casefold(bones[actual.Bone].Name) != CompiledText.Casefold(item.Bone))
                errors.Add($"Attachment {item.Name} bone differs from QC");
            var rotation = Rotation(item.Rotation);
            var same = true;
            for (var row = 0; row < 3; row++)
            {
                same &= Close(actual.Matrix[row][3], item.Position[row], 0.0002);
                for (var column = 0; column < 3; column++) same &= Close(actual.Matrix[row][column], rotation[row, column], 0.00002);
            }
            if (!same) errors.Add($"Attachment {item.Name} transform differs from QC");
        }
        foreach (var item in expected.Hitboxes)
        {
            var candidates = compiled.Hitboxes.Where(actual => CompiledText.Casefold(actual.Set) == CompiledText.Casefold(item.Set) && actual.Group == item.Group
                && actual.Bone >= 0 && actual.Bone < bones.Count && CompiledText.Casefold(bones[actual.Bone].Name) == CompiledText.Casefold(item.Bone));
            if (!candidates.Any(actual => AllClose(actual.Minimum, item.Minimum, 0.0002) && AllClose(actual.Maximum, item.Maximum, 0.0002)))
                errors.Add($"Hitbox {item.Set}/{item.Bone}/{item.Group} differs from QC");
        }
        var procedural = (report.Has("appearance") ? report.Get("appearance") : report).Get("procedural");
        var jiggles = new Dictionary<string, CompiledBone>(StringComparer.Ordinal);
        foreach (var bone in bones.Where(bone => bone.ProceduralType == 5 && bone.ProceduralOffset != 0)) jiggles[CompiledText.Casefold(bone.Name)] = bone;
        var wanted = new HashSet<string>(expected.Jiggles, StringComparer.Ordinal);
        foreach (var item in procedural.Get("jiggle_bones").Arr()) wanted.Add(item.Key("target").Str());
        foreach (var name in wanted)
            if (!jiggles.ContainsKey(CompiledText.Casefold(name))) errors.Add($"Jiggle bone {name} lacks a compiled type-5 procedural record");
        var sourceHitboxes = report.Get("hitboxes") ?? new JsonObject();
        var isMap = sourceHitboxes is JsonObject;
        var generated = isMap && sourceHitboxes.Get("status").Str("None") == "generated";
        var jiggleList = new JsonArray();
        foreach (var bone in jiggles.Values) jiggleList.Add(new JsonObject { ["name"] = bone.Name, ["flags"] = bone.JiggleFlags });
        return new()
        {
            ["attachments"] = compiled.Attachments.Count, ["expected_attachments"] = expected.Attachments.Count,
            ["hitboxes"] = compiled.Hitboxes.Count, ["expected_hitboxes"] = expected.Hitboxes.Count,
            ["source_hitbox_count"] = isMap ? (sourceHitboxes.Has("source_count") ? sourceHitboxes.Get("source_count") : sourceHitboxes.Get("count"))?.DeepClone() : null,
            ["generated_hitboxes"] = generated ? compiled.Hitboxes.Count : 0,
            ["hitbox_validation"] = new JsonObject
            {
                ["checked"] = compiled.Hitboxes.Count, ["invalid_bone_indices"] = CompiledText.Integers(invalidBones),
                ["nonfinite_bounds"] = CompiledText.Integers(nonfinite), ["inverted_extents"] = CompiledText.Integers(inverted)
            },
            ["jiggle_bones"] = jiggleList
        };
    }

    /// <summary>Requires retained controllers, expression rules and nonempty compiled vertex deltas.</summary>
    private static JsonObject AuditFlexes(CompiledMetadata compiled, JsonObject appearance, JsonArray errors)
    {
        var expected = appearance.Get("flexes");
        var controllers = compiled.FlexControllers.Select(item => item.Name).ToHashSet(StringComparer.Ordinal);
        foreach (var item in expected.Get("controllers").Arr())
        {
            var name = item.Has("m_szName") ? item.Get("m_szName").Str("None") : item.Get("name").Str();
            if (!controllers.Contains(name)) errors.Add($"Missing flex controller {name}");
        }
        var descriptors = compiled.FlexDescriptors.ToHashSet(StringComparer.Ordinal);
        var rules = compiled.FlexRules.Where(item => item.Operations != 0).Select(item => item.Name).ToHashSet(StringComparer.Ordinal);
        var deltas = new Dictionary<string, long>(StringComparer.Ordinal);
        foreach (var item in compiled.Meshes.SelectMany(mesh => mesh.Flexes)) deltas[item.Name] = deltas.GetValueOrDefault(item.Name) + item.VertexDeltas;
        var targets = expected.Get("targets").Arr().Select(name => name.Str("None")).ToList();
        foreach (var name in targets)
            if (!descriptors.Contains(name) || !rules.Contains(name)) errors.Add($"Missing compiled flex descriptor/rule {name}");
        var total = deltas.Values.Sum();
        if (targets.Count > 0 && total == 0) errors.Add("Authored flex rig has no compiled vertex deltas");
        var byTarget = new JsonObject();
        foreach (var (name, count) in deltas) byTarget[name] = count;
        return new()
        {
            ["controller_count"] = controllers.Count, ["descriptors"] = descriptors.Count, ["rules"] = compiled.FlexRules.Count, ["delta_records"] = total,
            ["delta_records_by_target"] = byTarget, ["targets_without_vertex_deltas"] = CompiledText.Strings(targets.Where(name => deltas.GetValueOrDefault(name) == 0))
        };
    }

    /// <summary>Returns an acceptance report for compiled bones, appearance, materials and binary geometry tables.</summary>
    /// <param name="mdl">The compiled model.</param>
    /// <param name="qc">The QC that produced it.</param>
    /// <param name="report">The port report holding its authored expectations.</param>
    /// <param name="materialPayload">The addon root or material folder holding the shipped VMT and VTF files.</param>
    public static JsonObject AuditCompiledModel(string mdl, string qc, JsonObject? report = null, string? materialPayload = null) =>
        Audit(mdl, qc, report, null, materialPayload);

    /// <summary>Audits against a payload described by <c>material_root</c>, <c>addon_root</c>, <c>root</c> or <c>path</c>.</summary>
    public static JsonObject AuditCompiledModel(string mdl, string qc, JsonObject? report, JsonObject materialPayload) =>
        Audit(mdl, qc, report, materialPayload, null);

    private static JsonObject Audit(string mdl, string qc, JsonObject? report, JsonObject? payloadMap, string? payload)
    {
        report ??= [];
        var errors = new JsonArray();
        var result = new JsonObject { ["ok"] = false, ["mdl"] = PathText.Native(mdl), ["qc"] = PathText.Native(qc), ["errors"] = errors, ["source_layout"] = SourceLayout };
        try
        {
            var compiled = ReadCompiledModel(mdl);
            var expected = ReadExpectations(qc);
            var appearance = report.Has("appearance") ? report.Get("appearance").Obj() : report;
            result["version"] = compiled.Version;
            result["checksum"] = compiled.Checksum;
            result["bones"] = AuditBones(compiled, expected, errors);
            if (report.Get("compiled_bones") is { } reported && compiled.Bones.Count != reported.Dbl()) errors.Add($"Compiled bone count differs from reported {reported.Str()}");
            result["bodygroups"] = AuditBodygroups(compiled, expected, appearance, errors);
            result["materials"] = AuditMaterials(compiled, appearance, payloadMap, payload, mdl, errors);
            result["metadata"] = AuditMetadata(compiled, expected, report, errors);
            result["flexes"] = AuditFlexes(compiled, appearance, errors);
            var vtx = ReadVtx(Path.ChangeExtension(mdl, ".dx90.vtx"));
            result["lods"] = vtx;
            var lodCount = vtx.Get("lod_count").Int();
            var parts = vtx.Get("bodygroups").Arr();
            if (vtx.Get("checksum").Lng() != compiled.Checksum) errors.Add("MDL/VTX checksums differ");
            if (lodCount != expected.Lods + 1) errors.Add($"Compiled VTX LOD count {lodCount} differs from QC {expected.Lods + 1}");
            if (appearance.Has("lods") && lodCount != appearance.Get("lods").Arr().Count + 1) errors.Add($"Compiled VTX LOD count differs from authored appearance {appearance.Get("lods").Arr().Count + 1}");
            if (parts.Count != compiled.Bodygroups.Count) errors.Add("MDL/VTX bodygroup counts differ");
            for (var index = 0; index < Math.Min(parts.Count, compiled.Bodygroups.Count); index++)
            {
                var part = parts[index].Arr();
                if (part.Count != compiled.Bodygroups[index].Choices.Count) errors.Add($"MDL/VTX bodygroup {index} choice counts differ");
                foreach (var choice in part)
                    if (choice.Arr().Count != lodCount) errors.Add($"VTX bodygroup {index} has an incomplete submodel LOD table");
            }
            var vvd = new CompiledBinary(Path.ChangeExtension(mdl, ".vvd"));
            vvd.Require(0, 64);
            if (!vvd.HasMagic("IDSV")) errors.Add("Invalid VVD header");
            var vvdChecksum = vvd.UInt(8);
            var vvdLods = vvd.Int(12);
            if (vvdChecksum != compiled.Checksum || vvdLods != lodCount) errors.Add("MDL/VVD checksums or VVD/VTX LOD counts differ");
            var counts = vvd.Ints(16, 8);
            result["vertices_per_lod"] = CompiledText.Integers(vvdLods >= 0 ? counts.Take(vvdLods) : counts.SkipLast(-vvdLods));
            var coverage = CompiledRenderCoverage.AuditRenderCoverage(mdl, qc, appearance, compiled);
            result["render_coverage"] = coverage;
            foreach (var error in coverage.Get("errors").Arr()) errors.Add(error?.DeepClone());
        }
        catch (Exception exception) when (CompiledText.IsAuditFault(exception))
        {
            errors.Add($"Compiled model audit failed: {CompiledText.Describe(exception)}");
        }
        result["ok"] = errors.Count == 0;
        return result;
    }
}
