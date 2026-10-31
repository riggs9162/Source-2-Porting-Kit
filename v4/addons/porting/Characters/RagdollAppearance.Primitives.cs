using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>One skinned primitive with its exact mesh provenance and named morph deltas.</summary>
public sealed class AppearancePrimitive
{
    public required V3[] Vertices { get; init; }
    public required V3[] Normals { get; init; }
    public required V2[] Uv { get; init; }

    /// <summary>Target skeleton indices, <see cref="Slots"/> per vertex.</summary>
    public required int[] Joints { get; init; }

    /// <summary>Authored weights in step with <see cref="Joints"/>, kept before any Source 1 influence limit applies.</summary>
    public required double[] Weights { get; init; }

    public required int Slots { get; init; }
    public required int[] Indices { get; init; }

    /// <summary>The compiler material token. Before mapping, this is the authored resource token.</summary>
    public required string Material { get; set; }

    /// <summary>The authored resource token, once a compiler token has been assigned.</summary>
    public string? SourceMaterial { get; set; }

    public OrderedDictionary<string, MorphTarget> Targets { get; init; } = new(StringComparer.Ordinal);
    public int GltfMeshIndex { get; init; } = -1;
    public int PrimitiveIndex { get; init; } = -1;
    public int NodeIndex { get; init; } = -1;
    public string MeshName { get; init; } = "";

    /// <summary>Identifies the authored vertex stream, so a stream shared by primitives is measured once.</summary>
    public required string WeightStream { get; init; }

    /// <summary>Whether the stream came from an authored lower-LOD DMX rather than the base glTF.</summary>
    public bool FromDmx { get; init; }

    /// <summary>The authored vertex behind each entry, when vertices were expanded to triangle corners.</summary>
    public int[]? WeightVertexIndices { get; init; }

    public string? SourceDmx { get; init; }

    /// <summary>The same streams carrying another compiler material.</summary>
    public AppearancePrimitive WithMaterial(string material)
    {
        var copy = (AppearancePrimitive)MemberwiseClone();
        copy.Material = material;
        return copy;
    }
}

public static partial class RagdollAppearance
{
    private static readonly Dictionary<string, string> NoRename = new(StringComparer.Ordinal);

    [GeneratedRegex("[^A-Za-z0-9_-]")]
    private static partial Regex UnsafeFilename();

    /// <summary>Quotes identifiers that can safely appear in compiler input.</summary>
    private static string Quoted(string value)
    {
        if (value.AsSpan().IndexOfAny("\"\n\r\0") >= 0) throw new InvalidDataException($"Invalid compiler identifier {PythonText.Repr(value)}");
        return $"\"{value}\"";
    }

    /// <summary>Uses portable source filenames without changing displayed source names.</summary>
    private static string Filename(string value)
    {
        var name = UnsafeFilename().Replace(value, "_").Trim('_');
        return name.Length > 0 ? name : "body";
    }

    /// <summary>Places a material beneath the addon's models folder, escaping identities Source 1 treats as missing geometry.</summary>
    /// <remarks>A material named <c>debug/debugempty</c> makes the compiler drop its triangles, so it moves to a reserved path.</remarks>
    public static string TargetMaterialToken(string value)
    {
        var token = Resources.Token(value);
        return token == "debug/debugempty" ? "__s2kit_reserved/" + token : Resources.AddonMaterial(token);
    }

    /// <summary>Retains the original resource identity alongside its compiler-safe material token.</summary>
    private static void MapPrimitiveMaterials(IEnumerable<AppearancePrimitive> primitives)
    {
        foreach (var primitive in primitives)
        {
            primitive.SourceMaterial ??= primitive.Material;
            primitive.Material = TargetMaterialToken(primitive.SourceMaterial);
        }
    }

    /// <summary>Removes only VRF's explicit model-resource prefix from an exported name.</summary>
    private static string MeshName(string value)
    {
        value = value.Replace('\\', '/');
        foreach (var separator in new[] { ".vmdl_c.", ".vmdl_c:", ".vmdl.", ".vmdl:" })
        {
            var at = value.IndexOf(separator, StringComparison.Ordinal);
            if (at >= 0) return value[(at + separator.Length)..];
        }
        return value;
    }

    private static V3[] Vectors(AccessorData data)
    {
        var result = new V3[data.Count];
        for (var row = 0; row < result.Length; row++) result[row] = data.Vector(row);
        return result;
    }

    private static MorphDelta DeltaStream(GltfDocument gltf, int accessor)
    {
        var data = gltf.Accessor(accessor);
        var values = data.Values;
        var kept = 0;
        for (var row = 0; row < data.Count; row++)
            if (values[row * 3] != 0 || values[row * 3 + 1] != 0 || values[row * 3 + 2] != 0) kept++;
        var indices = new int[kept];
        var deltas = new V3[kept];
        var at = 0;
        for (var row = 0; row < data.Count; row++)
        {
            if (values[row * 3] == 0 && values[row * 3 + 1] == 0 && values[row * 3 + 2] == 0) continue;
            indices[at] = row;
            deltas[at++] = data.Vector(row);
        }
        return new(data.Count, indices, deltas);
    }

    /// <summary>Maps source joint slots, leaving a slot at bone zero when no vertex gives its joint any weight.</summary>
    private static int[] MapJoints(int[] joints, double[] weights, Func<int, int?> map, Func<int, string> missing)
    {
        var weighted = new SortedSet<int>();
        for (var index = 0; index < joints.Length; index++)
            if (weights[index] > 0) weighted.Add(joints[index]);
        var mapping = new Dictionary<int, int>();
        foreach (var joint in weighted) mapping[joint] = map(joint) ?? throw new InvalidDataException(missing(joint));
        var result = new int[joints.Length];
        for (var index = 0; index < joints.Length; index++)
            if (mapping.TryGetValue(joints[index], out var target)) result[index] = target;
        return result;
    }

    /// <summary>Reads skinned primitives with exact mesh provenance and named morph deltas.</summary>
    /// <remarks>Authored normals and weights are kept untouched here; the three-influence limit applies only when writing.</remarks>
    public static List<AppearancePrimitive> ExtractPrimitives(GltfDocument gltf, GltfSkin skin, IReadOnlyDictionary<string, string>? boneNameMap = null)
    {
        var targetBones = new Dictionary<string, int>(StringComparer.Ordinal);
        for (var index = 0; index < skin.Joints.Count; index++) targetBones[skin.Joints[index].Name] = index;
        var rename = boneNameMap ?? NoRename;
        var primitives = new List<AppearancePrimitive>();
        var nodes = gltf.Nodes;
        var accessors = gltf.Root.Get("accessors").Arr();
        for (var nodeIndex = 0; nodeIndex < nodes.Count; nodeIndex++)
        {
            var node = nodes[nodeIndex];
            if (!node.Has("mesh") || !node.Has("skin")) continue;
            var meshIndex = node.Get("mesh").Int();
            var mesh = gltf.Meshes[meshIndex];
            var skinIndex = node.Get("skin").Int();
            var sourceBones = gltf.Skins[skinIndex].Need("joints").Arr();
            var mapping = new Dictionary<int, int>();
            for (var index = 0; index < sourceBones.Count; index++)
            {
                var name = nodes[sourceBones[index].Int()].Get("name").Str();
                if (targetBones.TryGetValue(rename.GetValueOrDefault(name, name), out var bone)) mapping[index] = bone;
            }
            var entries = mesh.Get("primitives").Arr();
            for (var primitiveIndex = 0; primitiveIndex < entries.Count; primitiveIndex++)
            {
                var primitive = entries[primitiveIndex];
                var attributes = primitive.Get("attributes");
                if (!attributes.Has("JOINTS_0")) continue;
                var vertices = Vectors(gltf.Accessor(attributes.Need("POSITION").Int()));
                if ((primitive.Has("mode") ? primitive.Get("mode").Int() : 4) != 4) throw new InvalidDataException("Source model contains a non-triangle primitive");
                var joints = gltf.Accessor(attributes.Get("JOINTS_0").Int());
                var weightData = gltf.Accessor(attributes.Need("WEIGHTS_0").Int());
                var slots = joints.Width;
                int[] jointValues = joints.Integers();
                double[] weights = weightData.Values;
                if (attributes.Has("JOINTS_1"))
                {
                    var moreJoints = gltf.Accessor(attributes.Get("JOINTS_1").Int());
                    var moreWeights = gltf.Accessor(attributes.Need("WEIGHTS_1").Int());
                    (jointValues, weights) = Stack(jointValues, weights, slots, moreJoints.Integers(), moreWeights.Values, moreJoints.Width);
                    slots += moreJoints.Width;
                }
                if (!vertices.All(vertex => vertex.IsFinite) || !weights.All(weight => double.IsFinite(weight) && weight >= 0))
                    throw new InvalidDataException("Model contains non-finite vertices or invalid weights");
                var mappedJoints = MapJoints(jointValues, weights, joint => mapping.TryGetValue(joint, out var mapped) ? mapped : null, joint => $"Weighted source joint {joint} has no target bone");
                var material = primitive.Get("material") is { } materialIndex ? gltf.Materials[materialIndex.Int()] : null;
                var reference = material.Get("extras").Get("vmat").Get("Name").Bool() ? material.Get("extras").Get("vmat").Get("Name").Str()
                    : material.Has("name") ? material.Get("name").Str() : "default";
                var indices = primitive.Has("indices") ? gltf.Accessor(primitive.Get("indices").Int()).Integers() : Enumerable.Range(0, vertices.Length).ToArray();
                if (indices.Length % 3 != 0 || (indices.Length > 0 && (indices.Min() < 0 || indices.Max() >= vertices.Length))) throw new InvalidDataException("Invalid source triangle indices");
                var targets = new OrderedDictionary<string, MorphTarget>(StringComparer.Ordinal);
                var names = mesh.Get("extras").Get("targetNames").Arr();
                var targetEntries = primitive.Get("targets").Arr();
                for (var targetIndex = 0; targetIndex < targetEntries.Count; targetIndex++)
                {
                    var target = targetEntries[targetIndex];
                    var accessor = target.Has("POSITION") ? target.Get("POSITION") : target.Get("NORMAL");
                    var name = targetIndex < names.Count ? names[targetIndex].Str() : accessors[accessor.Int()].Get("name").Str();
                    if (name.Length == 0 || targets.ContainsKey(name)) throw new InvalidDataException("Morph targets require unique authored names");
                    var morph = new MorphTarget();
                    foreach (var channel in new[] { MorphTarget.PositionChannel, MorphTarget.NormalChannel })
                        if (target.Has(channel)) morph[channel] = DeltaStream(gltf, target.Get(channel).Int());
                    targets[name] = morph;
                }
                var normals = attributes.Has("NORMAL") ? Vectors(gltf.Accessor(attributes.Get("NORMAL").Int())) : Enumerable.Repeat(new V3(0, 0, 1), vertices.Length).ToArray();
                V2[] uv;
                if (attributes.Has("TEXCOORD_0"))
                {
                    var data = gltf.Accessor(attributes.Get("TEXCOORD_0").Int());
                    uv = new V2[data.Count];
                    for (var row = 0; row < uv.Length; row++) uv[row] = data.Texcoord(row);
                }
                else uv = new V2[vertices.Length];
                var stream = new[] { "POSITION", "JOINTS_0", "WEIGHTS_0", "JOINTS_1", "WEIGHTS_1" }.Select(key => Fmt.Int(attributes.Has(key) ? attributes.Get(key).Lng() : -1));
                primitives.Add(new()
                {
                    Vertices = vertices, Normals = normals, Uv = uv, Joints = mappedJoints, Weights = weights, Slots = slots, Indices = indices,
                    Material = Resources.Token(reference), Targets = targets, GltfMeshIndex = meshIndex, PrimitiveIndex = primitiveIndex, NodeIndex = nodeIndex,
                    MeshName = MeshName(mesh.Get("name").Bool() ? mesh.Get("name").Str() : node.Get("name").Str()),
                    WeightStream = $"gltf\0{skinIndex}\0{string.Join('\0', stream)}"
                });
            }
        }
        if (primitives.Count == 0) throw new InvalidDataException("The model has no skinned render primitives");
        return primitives;
    }

    private static (int[] Joints, double[] Weights) Stack(int[] firstJoints, double[] firstWeights, int firstWidth, int[] secondJoints, double[] secondWeights, int secondWidth)
    {
        var rows = firstJoints.Length / firstWidth;
        var width = firstWidth + secondWidth;
        var joints = new int[rows * width];
        var weights = new double[rows * width];
        for (var row = 0; row < rows; row++)
        {
            Array.Copy(firstJoints, row * firstWidth, joints, row * width, firstWidth);
            Array.Copy(secondJoints, row * secondWidth, joints, row * width + firstWidth, secondWidth);
            Array.Copy(firstWeights, row * firstWidth, weights, row * width, firstWidth);
            Array.Copy(secondWeights, row * secondWidth, weights, row * width + firstWidth, secondWidth);
        }
        return (joints, weights);
    }

    /// <summary>Joins glTF mesh identities to CTRL entries, refusing ambiguous name joins.</summary>
    private static Dictionary<int, List<AppearancePrimitive>> AuthoredMeshLookup(JsonObject fidelity, IReadOnlyList<AppearancePrimitive> primitives)
    {
        var exported = new Dictionary<string, List<AppearancePrimitive>>(StringComparer.Ordinal);
        foreach (var primitive in primitives)
        {
            if (!exported.TryGetValue(primitive.MeshName, out var list)) exported[primitive.MeshName] = list = [];
            list.Add(primitive);
        }
        var names = new HashSet<string>(StringComparer.Ordinal);
        var lookup = new Dictionary<int, List<AppearancePrimitive>>();
        foreach (var mesh in fidelity.Need("meshes").Arr())
        {
            if ((mesh.Need("lod_mask").Lng() & 1) == 0) continue;
            var name = mesh.Need("name").Str();
            if (!names.Add(name)) throw new InvalidDataException($"Ambiguous authored LOD0 mesh identity {name}");
            if (!exported.TryGetValue(name, out var list)) throw new InvalidDataException($"Authored LOD0 mesh {PythonText.Repr(name)} was not exported");
            lookup[mesh.Need("index").Int()] = list;
        }
        if (!names.SetEquals(exported.Keys))
            throw new InvalidDataException($"glTF contains meshes absent from authored LOD0: {PathText.Reprs(exported.Keys.Except(names))}");
        return lookup;
    }

    /// <summary>Reads authored lower-LOD DMX streams into the source glTF's coordinate space.</summary>
    /// <param name="morphReport">Filled with where an atlas came from when one applies.</param>
    private static List<AppearancePrimitive> ReadDmxPrimitives(string path, GltfSkin targetSkin, CoordinateMode sourceCoord, IReadOnlyDictionary<string, string> rename, MeshMorphs? atlasMorphs, JsonObject? morphReport)
    {
        var document = DmxDocument.Load(path);
        var models = document.OfType("DmeModel");
        var meshes = document.OfType("DmeMesh");
        if (models.Count != 1 || meshes.Count == 0) throw new InvalidDataException($"Authored LOD source requires one model with render meshes: {path}");
        var sourceBones = models[0].Elements("jointList");
        var targetLookup = new Dictionary<string, int>(StringComparer.Ordinal);
        for (var index = 0; index < targetSkin.Joints.Count; index++) targetLookup[targetSkin.Joints[index].Name] = index;
        var mapped = new Dictionary<int, int>();
        for (var index = 0; index < sourceBones.Count; index++)
        {
            var name = sourceBones[index]?.Name ?? "";
            if (targetLookup.TryGetValue(rename.GetValueOrDefault(name, name), out var target)) mapped[index] = target;
        }
        var slices = Enumerable.Repeat((Offset: 0, Count: 0), meshes.Count).ToList();
        if (atlasMorphs is not null)
        {
            var (targets, mdat, atlasPath) = atlasMorphs;
            var first = targets.GetAt(0).Value;
            var count = (first.Position ?? first.Normal)?.Length ?? 0;
            slices = MorphAtlas.MorphBufferSlices(meshes, mdat, count);
            if (morphReport is not null)
            {
                morphReport["atlas"] = atlasPath;
                morphReport["native_source"] = path;
                morphReport["target_names"] = JsonKeys.Strings(targets.Keys);
                morphReport["buffer_slices"] = new JsonArray(slices.Select(slice => (JsonNode?)JsonKeys.Integers([slice.Offset, slice.Count])).ToArray());
                morphReport["source"] = "Retained MRPH and lossless RGBA8 atlas";
            }
        }
        var output = new List<AppearancePrimitive>();
        for (var index = 0; index < meshes.Count; index++)
        {
            OrderedDictionary<string, MorphTarget>? atlasTargets = null;
            if (atlasMorphs is not null)
            {
                atlasTargets = new(StringComparer.Ordinal);
                foreach (var (name, channels) in atlasMorphs.Targets)
                    atlasTargets[name] = new() { Position = channels.Position?.Slice(slices[index].Offset, slices[index].Count), Normal = channels.Normal?.Slice(slices[index].Offset, slices[index].Count) };
            }
            output.AddRange(ReadDmxMesh(meshes[index], mapped, sourceBones, sourceCoord, path, atlasTargets));
        }
        return output;
    }

    private static V3 Unswap(V3 value) => new(value.Y + 0.0, value.Z + 0.0, value.X + 0.0);

    /// <summary>Decodes one vertex-buffer mesh within a reconstructed Source 2 DMX model.</summary>
    /// <remarks>Each face set becomes one primitive with one vertex per triangle corner. The authored vertex behind every
    /// corner is kept, so a stream shared by face sets is measured once by the skin-weight audit.</remarks>
    /// <param name="mapped">Source joint index to target skeleton index.</param>
    /// <param name="atlasTargets">Morphs decoded from the retained atlas, filling channels the DMX itself lacks.</param>
    public static List<AppearancePrimitive> ReadDmxMesh(DmxElement mesh, IReadOnlyDictionary<int, int> mapped, IReadOnlyList<DmxElement?> sourceBones, CoordinateMode sourceCoord, string path,
        OrderedDictionary<string, MorphTarget>? atlasTargets = null)
    {
        var state = mesh.Element("currentState") ?? throw new InvalidDataException("DMX mesh has no current state");
        var positionKey = state.Contains("position$0") ? "position$0" : "positions";
        var normalKey = state.Contains("normal$0") ? "normal$0" : "normals";
        var uvKey = state.Contains("texcoord$0") ? "texcoord$0" : "textureCoordinates";
        var weightsKey = state.Contains("blendweights$0") ? "blendweights$0" : "jointWeights";
        var jointsKey = state.Contains("blendindices$0") ? "blendindices$0" : "jointIndices";
        var positions = state.Array<V3>(positionKey);
        var normals = state.Array<V3>(normalKey);
        var uv = state.Array<V2>(uvKey);
        var count = state.Value<int>("jointCount");
        if (count <= 0) throw new InvalidDataException($"cannot reshape array of size {state.Numbers(weightsKey).Length} into shape (-1,{count})");
        var weights = state.Numbers(weightsKey);
        var jointNumbers = state.Numbers(jointsKey);
        if (weights.Length % count != 0 || jointNumbers.Length % count != 0) throw new InvalidDataException("DMX skin streams do not divide into whole vertices");
        var joints = MapJoints(Array.ConvertAll(jointNumbers, value => (int)value), weights, joint => mapped.TryGetValue(joint, out var bone) ? bone : null,
            joint => $"LOD weighted joint {sourceBones[joint]?.Name} has no target bone");
        var sourceTargets = new OrderedDictionary<string, MorphTarget>(StringComparer.Ordinal);
        if (mesh.Contains("deltaStates"))
            foreach (var delta in mesh.Elements("deltaStates"))
            {
                var channels = new MorphTarget();
                foreach (var (channel, key, length) in new[] { (MorphTarget.PositionChannel, positionKey, positions.Count), (MorphTarget.NormalChannel, normalKey, normals.Count) })
                {
                    if (!delta!.Contains(key) || !delta.Contains(key + "Indices")) continue;
                    var selection = delta.Array<int>(key + "Indices");
                    var changes = delta.Array<V3>(key);
                    if (selection.Count != changes.Count || selection.Exists(index => index < 0 || index >= length)) throw new InvalidDataException("Authored LOD has invalid sparse morph indices");
                    channels[channel] = MorphDelta.FromPairs(length, selection, changes);
                }
                sourceTargets[delta!.Name ?? ""] = channels;
            }
        foreach (var (name, channels) in atlasTargets ?? [])
        {
            foreach (var channel in new[] { MorphTarget.PositionChannel, MorphTarget.NormalChannel })
            {
                if (channels[channel] is not { } values) continue;
                if (values.Length != (channel == MorphTarget.PositionChannel ? positions.Count : normals.Count)) throw new InvalidDataException("Morph atlas vertex count differs from native DMX channel");
                if (!sourceTargets.TryGetValue(name, out var existing)) sourceTargets[name] = existing = new();
                existing[channel] ??= values;
            }
        }
        var positionIndices = state.Array<int>(positionKey + "Indices");
        var normalIndices = state.Array<int>(normalKey + "Indices");
        var uvIndices = state.Array<int>(uvKey + "Indices");
        var scratchPositions = new V3[positions.Count];
        var scratchNormals = new V3[normals.Count];
        var output = new List<AppearancePrimitive>();
        foreach (var faceSet in mesh.Elements("faceSets"))
        {
            var faces = new List<int>();
            var face = new List<int>(3);
            foreach (var index in faceSet!.Array<int>("faces"))
            {
                if (index >= 0)
                {
                    face.Add(index);
                    continue;
                }
                if (face.Count != 3) throw new InvalidDataException("Authored LOD contains a non-triangle face");
                faces.AddRange(face);
                face.Clear();
            }
            if (face.Count > 0) throw new InvalidDataException("Unterminated authored LOD face");
            if (faces.Count == 0) continue;
            var pindex = faces.Select(index => positionIndices[index]).ToArray();
            var nindex = faces.Select(index => normalIndices[index]).ToArray();
            var uindex = faces.Select(index => uvIndices[index]).ToArray();
            var vertices = Array.ConvertAll(pindex, index => positions[index]);
            var normalValues = Array.ConvertAll(nindex, index => normals[index]);
            var targets = new OrderedDictionary<string, MorphTarget>(StringComparer.Ordinal);
            foreach (var (name, channels) in sourceTargets)
                targets[name] = new() { Position = channels.Position?.Select(pindex, scratchPositions), Normal = channels.Normal?.Select(nindex, scratchNormals) };
            if (sourceCoord.SwapAxes)
            {
                vertices = Array.ConvertAll(vertices, Unswap);
                normalValues = Array.ConvertAll(normalValues, Unswap);
                foreach (var (_, target) in targets)
                {
                    target.Position = target.Position?.Map(Unswap);
                    target.Normal = target.Normal?.Map(Unswap);
                }
            }
            vertices = Array.ConvertAll(vertices, vertex => vertex / sourceCoord.Scale);
            foreach (var (_, target) in targets) target.Position = target.Position?.Map(value => value / sourceCoord.Scale);
            var cornerJoints = new int[pindex.Length * count];
            var cornerWeights = new double[pindex.Length * count];
            for (var corner = 0; corner < pindex.Length; corner++)
            {
                Array.Copy(joints, pindex[corner] * count, cornerJoints, corner * count, count);
                Array.Copy(weights, pindex[corner] * count, cornerWeights, corner * count, count);
            }
            output.Add(new()
            {
                Vertices = vertices, Normals = normalValues, Uv = Array.ConvertAll(uindex, index => uv[index]), Joints = cornerJoints, Weights = cornerWeights, Slots = count,
                Indices = Enumerable.Range(0, faces.Count).ToArray(), Material = Resources.Token(faceSet.Element("material")!.Text("mtlName") ?? ""), Targets = targets,
                SourceDmx = path, WeightVertexIndices = pindex, FromDmx = true, WeightStream = $"dmx\0{path}\0{mesh.Id}\0{state.Id}"
            });
        }
        return output;
    }

    /// <summary>Fully deserializes selected mesh data and checks all referenced vertex streams.</summary>
    /// <remarks>Passed to <see cref="ModelFidelity.PreserveLodsAsync"/>: recovering from an animation-only decompile failure
    /// claims the geometry is sound, so every mesh it keeps must survive this read.</remarks>
    public static void ValidateNativeLodMesh(string path)
    {
        var document = DmxDocument.Load(path);
        var models = document.OfType("DmeModel");
        var meshes = document.OfType("DmeMesh");
        if (models.Count != 1 || meshes.Count == 0) throw new InvalidDataException($"Native LOD source does not contain one complete render model: {path}");
        var joints = models[0].Elements("jointList");
        var mapped = Enumerable.Range(0, joints.Count).ToDictionary(index => index);
        var primitives = meshes.SelectMany(mesh => ReadDmxMesh(mesh, mapped, joints, new(), path)).ToList();
        if (primitives.Count == 0) throw new InvalidDataException($"Native LOD source has no triangle geometry: {path}");
        foreach (var primitive in primitives)
        {
            var finite = primitive.Vertices.All(value => value.IsFinite) && primitive.Normals.All(value => value.IsFinite)
                && primitive.Uv.All(value => double.IsFinite(value.X) && double.IsFinite(value.Y)) && primitive.Weights.All(double.IsFinite);
            var rows = primitive.Weights.Chunk(primitive.Slots);
            if (!finite || primitive.Weights.Any(weight => weight < 0) || rows.Any(row => row.Sum() <= 0)) throw new InvalidDataException($"Native LOD source contains invalid vertex streams: {path}");
            foreach (var target in primitive.Targets.Values)
                if (!(target.Position?.Values.All(value => value.IsFinite) ?? true) || !(target.Normal?.Values.All(value => value.IsFinite) ?? true))
                    throw new InvalidDataException($"Native LOD source contains invalid morph streams: {path}");
        }
    }
}
