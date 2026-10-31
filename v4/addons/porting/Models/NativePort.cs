using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>Sources and fidelity measurements produced by a native model port.</summary>
public sealed class NativePortResult(string qc)
{
    public string Qc { get; } = qc;
    public List<string> Files { get; init; } = [];
    public int Bones { get; set; }
    public int Sequences { get; set; }
    public int Attachments { get; set; }
    public int Hitboxes { get; set; }
    public List<string> Warnings { get; set; } = [];
    public bool Compiled { get; set; }
    public string? RagdollQc { get; set; }
    public List<string> CompatibilityBones { get; } = [];
    public JsonObject Physics { get; set; } = [];
    public bool DynamicProp { get; init; }
}

/// <summary>The optional inputs of a native port.</summary>
public sealed record NativePortOptions
{
    public string Cdmaterials { get; init; } = "models/";
    public string Surfaceprop { get; init; } = "metal";
    public string? MetadataPath { get; init; }
    public IReadOnlyList<string>? RagdollBones { get; init; }

    /// <summary>Omits the generated ragdoll bind sequence and refuses a ragdoll companion.</summary>
    /// <remarks>Authored sequences and bone-driven collision remain.</remarks>
    public bool DynamicProp { get; init; }

    public IReadOnlyList<(double Distance, string Source)>? LodSources { get; init; }
    public double RagdollMass { get; init; } = 25.0;
    public double RagdollLimit { get; init; } = 25.0;

    /// <summary>Compiles a QC and reports success, when the caller wants the sources built here.</summary>
    public Func<string, bool>? Compile { get; init; }
}

/// <summary>Preserves a Source 2 model's native skeleton, authored sequences and metadata.</summary>
public static partial class NativePort
{
    private const double CompilerBasisYaw = 90.0;

    [GeneratedRegex("[^a-zA-Z0-9_-]")]
    private static partial Regex UnsafeFilename();

    [GeneratedRegex(@"^\$maxverts\s+\d+", RegexOptions.Multiline)]
    private static partial Regex MaxVerts();

    /// <summary>Gives every exact sequence name a distinct portable filename.</summary>
    private static string Filename(string name, int index)
    {
        var safe = UnsafeFilename().Replace(name, "_").Trim('_');
        return $"anim_{index:D3}_{(safe.Length > 0 ? safe : "clip")}.smd";
    }

    /// <summary>Resolves an optional LOD manifest relative to its own directory.</summary>
    public static List<(double Distance, string Source)> LoadLodSources(string path)
    {
        var entries = JsonNode.Parse(File.ReadAllBytes(path), null, new() { MaxDepth = 512 }).Arr();
        var directory = Path.GetDirectoryName(Path.GetFullPath(path)) ?? "";
        return entries.Select(entry => (entry.Get("distance").Dbl(), Path.GetFullPath(Path.Combine(directory, entry.Get("source").Str())))).ToList();
    }

    private static bool IsInteger(JsonNode? node) => node is JsonValue value && value.GetValueKind() == JsonValueKind.Number && value.TryGetValue<long>(out _);

    /// <summary>A JSON value as Python prints it, for messages that quote a rejected value.</summary>
    private static string Shown(JsonNode? node) => node switch
    {
        null => "None",
        JsonValue value when value.GetValueKind() == JsonValueKind.True => "True",
        JsonValue value when value.GetValueKind() == JsonValueKind.False => "False",
        _ => node.ToJsonString()
    };

    /// <summary>Validates explicit Source 1 event translations before placing them in QC.</summary>
    public static List<string> SequenceEventQc(JsonArray events, int frameCount)
    {
        var result = new List<string>();
        foreach (var item in events)
        {
            var frame = item.Get("frame");
            var number = item.Get("event");
            var options = item.Has("options") ? item.Get("options") : JsonValue.Create("");
            if (!IsInteger(frame) || frame.Lng() < 0 || frame.Lng() >= frameCount)
                throw new InvalidDataException($"Animation event frame is outside this clip: {Shown(frame)}");
            if (!IsInteger(number) || number.Lng() is < 1 or > 65535)
                throw new InvalidDataException($"Invalid Source 1 animation event number: {Shown(number)}");
            if (options is not JsonValue text || text.GetValueKind() != JsonValueKind.String || text.Str().AsSpan().IndexOfAny("\"\\\r\n\0") >= 0)
                throw new InvalidDataException("Animation event options cannot be represented safely in QC");
            if (Encoding.UTF8.GetByteCount(text.Str()) > 63) throw new InvalidDataException("Source 1 animation event options must fit in 63 UTF-8 bytes");
            result.Add($"{{ event {number.Lng()} {frame.Lng()} \"{text.Str()}\" }}");
        }
        return result;
    }

    /// <summary>Whether the export composed its additive clips over the bind pose, per its fidelity sidecar.</summary>
    public static bool AdditiveComposed(string glbPath)
    {
        var sidecar = Path.ChangeExtension(glbPath, ".fidelity.json");
        return File.Exists(sidecar) && Json.Load(sidecar).Get("animation_export").Get("additive_composed").Bool();
    }

    /// <summary>Chooses the authored collision bone containing the most other physics bones.</summary>
    /// <remarks>Parts named with an empty bone follow the model origin, which is the skeleton root.</remarks>
    private static string KinematicRoot(JsonObject document, GltfSkin skin)
    {
        var names = document.Get("m_boneNames").Arr().Select(name => name.Str() is { Length: > 0 } text ? text : skin.Joints[skin.RootIndex].Name).ToList();
        var parents = document.Get("m_boneParents").Arr();
        if (parents.Count == 0) throw new InvalidDataException("Physics part-to-bone mapping is missing");
        if (parents.Any(index => !IsInteger(index) || index.Lng() < 0 || index.Lng() >= names.Count))
            throw new InvalidDataException("Physics part-to-bone mapping contains an invalid name index");
        var selected = parents.Select(index => names[index.Int()]).ToHashSet(StringComparer.Ordinal);
        var indices = new Dictionary<string, int>(StringComparer.Ordinal);
        for (var index = 0; index < skin.Joints.Count; index++) indices[skin.Joints[index].Name] = index;
        var missing = selected.Where(name => !indices.ContainsKey(name)).Order(StringComparer.Ordinal).ToList();
        if (missing.Count > 0) throw new InvalidDataException($"Physics bones are absent from the render skeleton: [{string.Join(", ", missing.Select(PythonText.Repr))}]");
        var scores = selected.ToDictionary(name => name, _ => 0, StringComparer.Ordinal);
        foreach (var name in selected)
            for (int? index = indices[name]; index is { } at; index = skin.Joints[at].Parent)
                if (scores.ContainsKey(skin.Joints[at].Name)) scores[skin.Joints[at].Name]++;
        return scores.OrderByDescending(pair => pair.Value).ThenBy(pair => indices[pair.Key]).First().Key;
    }

    /// <summary>Writes original primitive normals, UVs and weights without mesh-library smoothing.</summary>
    /// <remarks>This is what replaces the derived normals of the generic skinned SMD: the authored shading survives
    /// only through this path. In a metre export, positions take the scale and basis swap and normals take the swap.</remarks>
    /// <returns>The number of triangles written.</returns>
    public static int WriteNativeReference(string path, GltfDocument document, GltfSkin skin, CoordinateMode coordinates)
    {
        SmdSkeleton.WriteAnimation(skin, [skin.BindPose()], path, coordinates);
        var (_, primitives) = SkinnedPrimitive.Collect(document);
        var basis = GltfDocument.Source2Basis;
        var triangles = 0;
        using var output = Fmt.OpenText(path, true);
        output.Write("triangles\n");
        foreach (var primitive in primitives)
        {
            if (primitive.Normals is not { } normals) throw new InvalidDataException("Native export requires authored vertex normals");
            if (primitive.Texcoords is not { } texcoords) throw new InvalidDataException("Native export requires texture coordinates");
            for (var index = 0; index < primitive.Indices.Length; index += 3)
            {
                output.Write(primitive.Material);
                output.Write('\n');
                for (var corner = 0; corner < 3; corner++)
                {
                    var vertex = primitive.Indices[index + corner];
                    var position = primitive.Vertices[vertex] * coordinates.Scale;
                    var normal = normals[vertex];
                    if (coordinates.SwapAxes)
                    {
                        position = basis.Direction(position);
                        normal = basis.Direction(normal);
                    }
                    var links = new List<(int Joint, double Weight)>(primitive.Slots);
                    for (var slot = 0; slot < primitive.Slots; slot++)
                    {
                        var weight = primitive.Weight(vertex, slot);
                        if (weight > 0) links.Add((primitive.Joint(vertex, slot), weight));
                    }
                    if (links.Count == 0) throw new InvalidDataException("Native mesh contains an unweighted vertex");
                    var texcoord = texcoords[vertex];
                    output.Write(SmdSkeleton.VertexLine(position, normal, new(texcoord.X, 1.0 - texcoord.Y), links));
                }
                triangles++;
            }
        }
        output.Write("end\n");
        return triangles;
    }

    private static JsonObject? BoundPhysics(string physPath)
    {
        if (!File.Exists(physPath)) return null;
        JsonObject? bound = null;
        foreach (var document in Kv3.ParseDocuments(PythonText.ReadBomless(physPath)))
        {
            if (document.Get("m_joints").Bool()) throw new InvalidDataException("Source 2 physics joints require explicit constraint conversion");
            if (!document.Get("m_boneNames").Bool()) continue;
            if (bound is not null) throw new InvalidDataException("Multiple bone-bound physics aggregates need explicit composition");
            bound = document;
        }
        return bound;
    }

    private static string Quoted(string value) => $"\"{value}\"";

    /// <summary>Writes a native model, optionally restricted to authored animation and collision.</summary>
    /// <remarks>
    /// <para>The generic conversion runs first for its collision and QC header, then its skinned SMD is replaced by a
    /// native reference. Everything authored survives by name: <c>$definebone</c> lines carry the 90 degree compiler
    /// yaw that studiomdl applies to loaded SMD roots but not to explicit roots.</para>
    /// <para>A model-space attachment needs a stationary identity bone, which no authored root can promise, so a
    /// compatibility bone is added to every SMD, weightlist and the skeleton when one is needed.</para>
    /// </remarks>
    public static NativePortResult Port(string glbPath, string outDir, string modelname, NativePortOptions? options = null, PortContext? context = null)
    {
        options ??= new();
        glbPath = Path.GetFullPath(glbPath);
        if (options.DynamicProp && options.RagdollBones is { Count: > 0 })
            throw new InvalidDataException("Dynamic prop mode cannot generate a ragdoll companion; clear ragdoll_bones");
        var document = GltfDocument.Read(glbPath);
        var skin = GltfSkin.Parse(document) ?? throw new InvalidDataException("Native model port requires a skinned model");
        var clips = GltfClip.ParseAll(document, skin);
        SmdSkeleton.ValidateNativeData(document, skin, clips);
        var coordinates = CoordinateMode.For(document);
        var metadata = ModelMetadata.Load(options.MetadataPath ?? glbPath);
        var stem = Path.GetFileNameWithoutExtension(glbPath);
        var eventsPath = Path.ChangeExtension(glbPath, ".events.json");
        var sidecar = File.Exists(eventsPath) ? Json.Load(eventsPath) : [];
        var eventSequences = sidecar.Get("sequences").Obj();
        var unknownEvents = eventSequences.Select(pair => pair.Key).Except(clips.Select(clip => clip.Name)).Order(StringComparer.Ordinal).ToList();
        if (unknownEvents.Count > 0) throw new InvalidDataException($"Event translations refer to absent sequences: [{string.Join(", ", unknownEvents.Select(PythonText.Repr))}]");
        if (metadata.Animations.Count == 0 && clips.Count > 0) throw new InvalidDataException("Authored animation metadata is missing; export again with VRF Batch Export");
        if (!AdditiveComposed(glbPath) && metadata.Animations.Values.Any(animation => animation.Delta))
            throw new InvalidDataException("Additive Source 2 clips need explicit composition before native SMD export");
        var originBone = "";
        if (metadata.Attachments.Any(attachment => attachment.RootTransform))
        {
            if (skin.Joints.Count >= 256) throw new InvalidDataException("Model-space attachments need one compatibility bone within GMod's 256-bone limit");
            originBone = "__s2_model_origin";
            var taken = skin.Joints.Select(joint => joint.Name.ToLowerInvariant()).ToHashSet(StringComparer.Ordinal);
            while (taken.Contains(originBone.ToLowerInvariant())) originBone += "_";
        }
        var physPath = Path.Combine(Path.GetDirectoryName(glbPath) ?? "", stem + "_phys.txt");
        var boundPhysics = BoundPhysics(physPath);
        var nativeRoot = skin.Joints[skin.RootIndex].Name;
        var compilerRoot = boundPhysics is not null ? KinematicRoot(boundPhysics, skin) : nativeRoot;
        var lodManifest = Path.ChangeExtension(glbPath, ".lods.json");
        var lodSources = options.LodSources ?? (File.Exists(lodManifest) ? LoadLodSources(lodManifest) : null);
        var directory = Path.GetDirectoryName(Path.GetFullPath(glbPath)) ?? "";
        var modelSet = new ModelSetScanner(directory, false).FindSets().FirstOrDefault(set => PathsEqual(set.RenderPath, glbPath))
            ?? throw new InvalidDataException($"Could not discover model set for {glbPath}");
        var conversion = new ConversionOptions
        {
            ExportAnimations = false, PreserveFolders = false, Cdmaterials = options.Cdmaterials, StaticSurfaceprop = options.Surfaceprop, AutoSurfaceprop = false,
            CollisionFromRender = false, DecomposeCollision = false, ExportPhysics = boundPhysics is null
        };
        var converted = GltfSmd.ConvertModelSet(modelSet, conversion, directory, outDir, context);
        if (!converted.Ok) throw new InvalidDataException(converted.Reason);
        var reference = Path.Combine(outDir, $"{stem}.smd");
        var triangles = WriteNativeReference(reference, document, skin, coordinates);
        if (originBone.Length > 0) SmdSkeleton.AddModelOriginBone(reference, skin.Joints.Count, originBone);
        context?.Log($"Native reference: {triangles} triangles with authored vertex normals");
        var qc = Path.Combine(outDir, $"{stem}.qc");
        var generated = PythonText.Read(qc);
        var collisionAt = generated.IndexOf("$collisionmodel", StringComparison.Ordinal);
        var collision = collisionAt >= 0 ? generated[collisionAt..] : "";
        var result = new NativePortResult(qc) { Files = [.. converted.Files], Bones = skin.Joints.Count, DynamicProp = options.DynamicProp };
        result.Warnings.AddRange(metadata.Warnings);
        result.Warnings = result.Warnings.Where(warning => !(warning.EndsWith(": Source 2 animation events require manual translation", StringComparison.Ordinal) &&
            eventSequences.Get(warning[..warning.IndexOf(':')]).Bool())).ToList();
        result.Warnings.AddRange(sidecar.Get("warnings").Arr().Select(warning => warning.Str()));
        if (originBone.Length > 0)
        {
            result.CompatibilityBones.Add(originBone);
            result.Warnings = result.Warnings.Where(warning => !warning.Contains("model-space attachment requires a stationary identity root bone", StringComparison.Ordinal)).ToList();
            context?.Log($"Added stationary compatibility bone {originBone} for model-space attachments");
        }
        if (boundPhysics is not null)
        {
            var physicsSmd = Path.Combine(outDir, $"{stem}_physics.smd");
            var physics = NativePhysics.WriteKinematicPhysics(physPath, physicsSmd, reference, skin.JointNames, compilerRoot, nativeRoot);
            collision = string.Join('\n', physics.QcLines) + "\n";
            result.Physics = physics.Report;
            result.Warnings.AddRange(physics.Warnings);
            result.Files.Add(physicsSmd);
            context?.Log($"Native kinematic collision: {result.Physics.Get("part_count").Int()} bodies, {result.Physics.Get("hull_count").Int()} hulls");
        }
        var overlayPath = Path.ChangeExtension(glbPath, ".overlays.json");
        var surfaces = File.Exists(overlayPath) ? Json.Load(overlayPath).Get("surfaces").Arr().Select(surface => surface.Obj()).ToList() : [];
        var overlayFiles = BoneOverlays.WriteBoneOverlays(reference, surfaces, outDir);
        result.Files.AddRange(overlayFiles);
        var weightlists = metadata.WeightlistQcLines();
        if (originBone.Length > 0) weightlists = weightlists.ConvertAll(line => line != "}" ? line : $"    \"{originBone}\" 1\n}}");
        var lines = new List<string>
        {
            $"$modelname \"{modelname}\"", $"$cdmaterials \"{options.Cdmaterials}\"", $"$surfaceprop \"{options.Surfaceprop}\"", "$contents \"solid\"", $"$body \"body\" \"{stem}.smd\""
        };
        lines.AddRange(SmdSkeleton.DefineBoneLines(skin, coordinates, CompilerBasisYaw));
        lines.Add($"$root \"{compilerRoot}\"");
        lines.AddRange(metadata.AttachmentQcLines(originBone));
        lines.AddRange(metadata.HitboxQcLines());
        lines.AddRange(weightlists);
        lines.AddRange(metadata.PoseParameterQcLines());
        for (var index = 0; index < Math.Min(surfaces.Count, overlayFiles.Count); index++)
        {
            var name = Path.GetFileName(overlayFiles[index]);
            var overlayStem = Path.GetFileNameWithoutExtension(overlayFiles[index]);
            if (!surfaces[index].Get("bodygroup").Bool())
            {
                lines.Add($"$body \"{overlayStem}\" \"{name}\"");
                continue;
            }
            var choices = new List<string> { $"    studio \"{name}\"", "    blank" };
            if (surfaces[index].Get("default_off").Bool()) choices.Reverse();
            lines.AddRange([$"$bodygroup \"{overlayStem}\"", "{", .. choices, "}"]);
        }
        if (originBone.Length > 0) lines.Add($"$definebone \"{originBone}\" \"\" 0 0 0 0 90 0 0 0 0 0 0 0");
        var maxVerts = MaxVerts().Match(generated);
        if (maxVerts.Success) lines.Insert(1, maxVerts.Value);
        if (modelSet.Skins.Count > 1)
        {
            lines.AddRange(["$texturegroup \"skinfamilies\"", "{"]);
            for (var index = 0; index < modelSet.Skins.Count; index++)
            {
                var row = modelSet.Skins[index].Materials.Concat(surfaces.Select(surface => BoneOverlays.OverlaySkinMaterial(surface, index))).Select(Quoted);
                lines.Add("    { " + string.Join(' ', row) + " }");
            }
            lines.Add("}");
        }
        var (lodLines, lodFiles, partitioned) = DmxMesh.WriteLodReferences(reference, lodSources ?? [], skin.JointNames, context);
        if (partitioned) lines = lines.Where(line => !line.StartsWith("$body ", StringComparison.Ordinal)).ToList();
        lines.AddRange(lodLines);
        result.Files.AddRange(lodFiles);
        result.Attachments = metadata.Attachments.Count;
        result.Hitboxes = metadata.Hitboxes.Count;
        var bindPath = Path.Combine(outDir, "bind.smd");
        SmdSkeleton.WriteAnimation(skin, [skin.BindPose()], bindPath, coordinates);
        if (originBone.Length > 0) SmdSkeleton.AddModelOriginBone(bindPath, skin.Joints.Count, originBone);
        result.Files.Add(bindPath);
        lines.Add("$animation \"native_bind\" \"bind.smd\" fps 30");
        var audit = new JsonArray();
        for (var index = 0; index < clips.Count; index++)
        {
            context?.Check();
            var clip = clips[index];
            if (!metadata.Animations.TryGetValue(clip.Name, out var authored)) throw new InvalidDataException($"Missing authored metadata for sequence {clip.Name}");
            var (frames, count, sampling) = clip.SampleAuthored(skin, authored);
            var path = Path.Combine(outDir, Filename(clip.Name, index));
            try
            {
                SmdSkeleton.WriteAnimation(skin, frames, path, coordinates);
            }
            catch (InvalidDataException error)
            {
                throw new InvalidDataException($"{clip.Name}: {error.Message}", error);
            }
            if (originBone.Length > 0) SmdSkeleton.AddModelOriginBone(path, skin.Joints.Count, originBone);
            result.Files.Add(path);
            var flags = ModelMetadata.SequenceQcOptions(authored, false);
            var translated = SequenceEventQc(eventSequences.Get(clip.Name).Arr(), count);
            var animationName = $"native_clip_{index:D3}";
            lines.Add($"$animation \"{animationName}\" \"{Path.GetFileName(path)}\" fps {Fmt.G(authored.Fps)}");
            lines.Add($"$sequence \"{clip.Name}\" {{ \"{animationName}\" {string.Join(' ', flags.Concat(translated))} }}");
            audit.Add(new JsonObject
            {
                ["name"] = clip.Name, ["frames"] = count, ["fps"] = authored.Fps, ["loop"] = authored.Loop, ["delta"] = authored.Delta,
                ["sampling"] = sampling, ["translated_events"] = translated.Count
            });
            context?.Log($"Sequence {index + 1}/{clips.Count}: {clip.Name}, {count} frames at {Fmt.G(authored.Fps)} fps");
        }
        if (options.DynamicProp && clips.Count == 0) lines.Add("$sequence \"idle\" \"bind.smd\" fps 1");
        if (!options.DynamicProp && !clips.Any(clip => clip.Name.Equals("ragdoll", StringComparison.OrdinalIgnoreCase)))
            lines.Add("$sequence \"ragdoll\" \"bind.smd\" fps 30 activity \"ACT_DIERAGDOLL\" 1");
        PythonText.Write(qc, string.Join('\n', lines) + "\n" + collision);
        result.Sequences = clips.Count;
        if (options.RagdollBones is { Count: > 0 } ragdollBones)
            NativeRagdoll.Write(document, skin, coordinates, ragdollBones, outDir, stem, modelname, lines, nativeRoot, options, result);
        var report = Path.Combine(outDir, "native_port_report.json");
        Json.Save(report, new JsonObject
        {
            ["source"] = glbPath, ["modelname"] = modelname, ["dynamic_prop"] = options.DynamicProp, ["compiler_basis_yaw_degrees"] = CompilerBasisYaw,
            ["overlays"] = new JsonArray(surfaces.Select(surface => surface.DeepClone()).ToArray()),
            ["bones"] = new JsonArray(skin.JointNames.Select(name => (JsonNode?)name).ToArray()), ["sequences"] = audit,
            ["compatibility_bones"] = new JsonArray(result.CompatibilityBones.Select(name => (JsonNode?)name).ToArray()),
            ["physics"] = result.Physics.DeepClone(), ["attachments"] = result.Attachments, ["hitboxes"] = result.Hitboxes,
            ["warnings"] = new JsonArray(result.Warnings.Select(warning => (JsonNode?)warning).ToArray())
        });
        result.Files.Add(report);
        if (options.Compile is { } compile)
        {
            result.Compiled = compile(qc);
            if (result.Compiled && result.RagdollQc is { } ragdollQc) result.Compiled = compile(ragdollQc);
            if (!result.Compiled) throw new InvalidDataException("Native model compile failed; see the compiler log");
        }
        return result;
    }

    /// <summary>Whether two paths name one file, compared the Windows way: full paths, ignoring case.</summary>
    internal static bool PathsEqual(string first, string second) =>
        string.Equals(Path.GetFullPath(first), Path.GetFullPath(second), StringComparison.OrdinalIgnoreCase);
}

/// <summary>Writes the generated ragdoll companion of a native port.</summary>
internal static class NativeRagdoll
{
    /// <summary>Fits separate rigid convex bodies to the strongest weights of the selected bones.</summary>
    /// <remarks>Bodies come out in the iteration order of a Python set of bone indices. That order reaches the QC,
    /// as the order of the joint constraints, so it is kept.</remarks>
    private static List<(int Bone, ConvexHull Hull)> GeneratedHulls(GltfDocument document, GltfSkin skin, CoordinateMode coordinates, IReadOnlyList<string> selected)
    {
        var names = skin.JointNames.ToList();
        var missing = selected.Except(names).Order(StringComparer.Ordinal).ToList();
        if (missing.Count > 0) throw new InvalidDataException($"Unknown ragdoll bones: [{string.Join(", ", missing.Select(PythonText.Repr))}]");
        var chosen = PythonIntSet.Order(selected.Select(name => names.IndexOf(name)));
        if (!chosen.Contains(skin.RootIndex) || chosen.Count < 2 || chosen.Count > 32)
            throw new InvalidDataException("Generated ragdolls need the skeleton root and 2 to 32 selected bones");
        var mapping = new int[names.Count];
        for (var index = 0; index < mapping.Length; index++)
        {
            var at = index;
            while (!chosen.Contains(at))
                at = skin.Joints[at].Parent ?? throw new InvalidDataException("Every ragdoll bone must descend from the selected root");
            mapping[index] = at;
        }
        var (_, primitives) = SkinnedPrimitive.Collect(document);
        var points = chosen.ToDictionary(index => index, _ => new List<V3>());
        var basis = GltfDocument.Source2Basis;
        foreach (var primitive in primitives)
            foreach (var vertex in primitive.Indices.Distinct().Order())
            {
                var totals = new double[names.Count];
                for (var slot = 0; slot < primitive.Slots; slot++) totals[mapping[primitive.Joint(vertex, slot)]] += primitive.Weight(vertex, slot);
                var dominant = Array.IndexOf(totals, totals.Max());
                if (!points.TryGetValue(dominant, out var cloud)) continue;
                var position = primitive.Vertices[vertex] * coordinates.Scale;
                cloud.Add(coordinates.SwapAxes ? basis.Direction(position) : position);
            }
        var hulls = new List<(int Bone, ConvexHull Hull)>();
        foreach (var index in chosen)
        {
            var cloud = points[index];
            if (cloud.Count < 4) throw new InvalidDataException($"Too little geometry for ragdoll bone {names[index]}");
            var unique = cloud.Select(point => new V3(Math.Round(point.X, 5), Math.Round(point.Y, 5), Math.Round(point.Z, 5))).Distinct().Order(Comparer<V3>.Create(Compare)).ToList();
            var hull = ConvexHull.Build(unique);
            var extents = hull.Extents;
            if (Math.Min(extents.X, Math.Min(extents.Y, extents.Z)) < 0.5 || hull.Volume < 0.1)
                throw new InvalidDataException($"Ragdoll bone {names[index]} has a degenerate hull; select its parent");
            hulls.Add((index, hull));
        }
        return hulls;
    }

    private static int Compare(V3 a, V3 b)
    {
        var x = a.X.CompareTo(b.X);
        if (x != 0) return x;
        var y = a.Y.CompareTo(b.Y);
        return y != 0 ? y : a.Z.CompareTo(b.Z);
    }

    /// <summary>Writes one rigid weight per collision vertex, preserving the render skeleton.</summary>
    private static void WriteHulls(string path, GltfSkin skin, CoordinateMode coordinates, List<(int Bone, ConvexHull Hull)> hulls)
    {
        var vertices = new List<V3>();
        var faces = new List<int>();
        var joints = new List<int>();
        var normals = new List<V3>();
        foreach (var (index, hull) in hulls)
        {
            var offset = vertices.Count;
            vertices.AddRange(hull.Mesh.Vertices);
            faces.AddRange(hull.Mesh.Faces.Select(face => face + offset));
            normals.AddRange(hull.Mesh.VertexNormals());
            for (var vertex = 0; vertex < hull.Mesh.Vertices.Length; vertex++) joints.AddRange([index, 0, 0, 0]);
        }
        var weights = new float[vertices.Count * 4];
        for (var vertex = 0; vertex < vertices.Count; vertex++) weights[vertex * 4] = 1.0f;
        var mesh = new ModelMesh(vertices.ToArray(), faces.ToArray());
        var (ok, error) = SmdExporter.WriteSkinned(mesh, skin, joints.ToArray(), weights, path, "physics", coordinates, overrideNormals: normals.ToArray());
        if (!ok) throw new InvalidDataException(error);
    }

    /// <summary>Conservative generated articulation with fixed limits and disabled self collision.</summary>
    private static List<string> Block(GltfSkin skin, List<(int Bone, ConvexHull Hull)> hulls, string filename, double mass, double limit)
    {
        var lines = new List<string>
        {
            $"$collisionjoints \"{filename}\"", "{", $"    $mass {Fmt.G(mass)}", "    $inertia 2", "    $damping 0.05", "    $rotdamping 1.5",
            $"    $rootbone \"{skin.Joints[skin.RootIndex].Name}\"", "    $noselfcollisions"
        };
        foreach (var (index, _) in hulls)
        {
            if (index == skin.RootIndex) continue;
            foreach (var axis in "xyz") lines.Add($"    $jointconstrain \"{skin.Joints[index].Name}\" {axis} limit {Fmt.G(-limit)} {Fmt.G(limit)} 1");
        }
        lines.Add("}");
        return lines;
    }

    public static void Write(GltfDocument document, GltfSkin skin, CoordinateMode coordinates, IReadOnlyList<string> bones, string outDir, string stem, string modelname,
        List<string> lines, string nativeRoot, NativePortOptions options, NativePortResult result)
    {
        var smd = Path.Combine(outDir, "ragdoll_physics.smd");
        var hulls = GeneratedHulls(document, skin, coordinates, bones);
        WriteHulls(smd, skin, coordinates, hulls);
        var ragdollLines = lines.ConvertAll(line => line.StartsWith("$root ", StringComparison.Ordinal) ? $"$root \"{nativeRoot}\"" : line);
        var trimmed = modelname.EndsWith(".mdl", StringComparison.Ordinal) ? modelname[..^".mdl".Length] : modelname;
        ragdollLines[0] = $"$modelname \"{trimmed}_ragdoll.mdl\"";
        ragdollLines.AddRange(Block(skin, hulls, Path.GetFileName(smd), options.RagdollMass, options.RagdollLimit));
        var ragdollQc = Path.Combine(outDir, $"{stem}_ragdoll.qc");
        result.RagdollQc = ragdollQc;
        PythonText.Write(ragdollQc, string.Join('\n', ragdollLines) + "\n");
        result.Files.AddRange([smd, ragdollQc]);
        result.Warnings.Add("The ragdoll companion uses generated convex hulls and joint limits; it is not authored Source 2 physics.");
    }
}
