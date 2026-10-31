using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>A per-vertex delta stream that stores only the vertices it moves.</summary>
/// <remarks>A character carries hundreds of targets over tens of thousands of vertices, and most targets touch a small
/// patch. Dense float64 arrays of every target are what took this machine down in Python. Absent vertices read as zero.</remarks>
public sealed class MorphDelta
{
    /// <param name="length">The vertex count of the full stream.</param>
    /// <param name="indices">Moved vertices in ascending order.</param>
    /// <param name="values">Their deltas, in step with <paramref name="indices"/>.</param>
    public MorphDelta(int length, int[] indices, V3[] values)
    {
        Length = length;
        Indices = indices;
        Values = values;
    }

    public int Length { get; }
    public int[] Indices { get; }
    public V3[] Values { get; }

    /// <summary>A sparse view of a dense stream.</summary>
    public static MorphDelta FromDense(ReadOnlySpan<V3> values)
    {
        var count = 0;
        foreach (var value in values)
            if (value != V3.Zero) count++;
        var indices = new int[count];
        var kept = new V3[count];
        var at = 0;
        for (var index = 0; index < values.Length; index++)
        {
            if (values[index] == V3.Zero) continue;
            indices[at] = index;
            kept[at++] = values[index];
        }
        return new(values.Length, indices, kept);
    }

    /// <summary>Applies a selection of index and value pairs to a zero stream, the last entry for a vertex winning.</summary>
    public static MorphDelta FromPairs(int length, IReadOnlyList<int> selection, IReadOnlyList<V3> changes)
    {
        var latest = new Dictionary<int, V3>(selection.Count);
        for (var index = 0; index < selection.Count; index++) latest[selection[index]] = changes[index];
        var indices = latest.Where(pair => pair.Value != V3.Zero).Select(pair => pair.Key).ToArray();
        Array.Sort(indices);
        return new(length, indices, Array.ConvertAll(indices, index => latest[index]));
    }

    /// <summary>The delta of one vertex.</summary>
    public V3 At(int vertex)
    {
        var at = Array.BinarySearch(Indices, vertex);
        return at >= 0 ? Values[at] : V3.Zero;
    }

    /// <summary>A stream with one function applied to every moved vertex.</summary>
    public MorphDelta Map(Func<V3, V3> transform) => new(Length, Indices, Array.ConvertAll(Values, value => transform(value)));

    /// <summary>The stream of a run of vertices, renumbered from zero.</summary>
    public MorphDelta Slice(int offset, int count)
    {
        var first = LowerBound(offset);
        var last = LowerBound(offset + count);
        var indices = new int[last - first];
        var values = new V3[last - first];
        for (var index = first; index < last; index++)
        {
            indices[index - first] = Indices[index] - offset;
            values[index - first] = Values[index];
        }
        return new(count, indices, values);
    }

    private int LowerBound(int vertex)
    {
        var at = Array.BinarySearch(Indices, vertex);
        return at >= 0 ? at : ~at;
    }

    /// <summary>The stream a triangle corner list reads, as <c>values[selection]</c> does.</summary>
    /// <param name="selection">One source vertex per corner.</param>
    /// <param name="scratch">A zeroed array of at least <see cref="Length"/>, left zeroed again.</param>
    public MorphDelta Select(int[] selection, V3[] scratch)
    {
        for (var index = 0; index < Indices.Length; index++) scratch[Indices[index]] = Values[index];
        var count = 0;
        foreach (var vertex in selection)
            if (scratch[vertex] != V3.Zero) count++;
        var indices = new int[count];
        var values = new V3[count];
        var at = 0;
        for (var corner = 0; corner < selection.Length; corner++)
        {
            var value = scratch[selection[corner]];
            if (value == V3.Zero) continue;
            indices[at] = corner;
            values[at++] = value;
        }
        foreach (var vertex in Indices) scratch[vertex] = V3.Zero;
        return new(selection.Length, indices, values);
    }

    /// <summary>Writes the moved vertices into a zeroed dense array.</summary>
    public void Expand(V3[] destination)
    {
        for (var index = 0; index < Indices.Length; index++) destination[Indices[index]] = Values[index];
    }

    /// <summary>Clears what <see cref="Expand"/> wrote.</summary>
    public void Collapse(V3[] destination)
    {
        foreach (var vertex in Indices) destination[vertex] = V3.Zero;
    }
}

/// <summary>One named morph with the channels the source stores for it.</summary>
public sealed class MorphTarget
{
    public const string PositionChannel = "POSITION";
    public const string NormalChannel = "NORMAL";

    public MorphDelta? Position { get; set; }
    public MorphDelta? Normal { get; set; }

    public MorphDelta? this[string channel]
    {
        get => channel == PositionChannel ? Position : Normal;
        set
        {
            if (channel == PositionChannel) Position = value;
            else Normal = value;
        }
    }
}

/// <summary>A mesh's decoded atlas, the MDAT block that lays it out and the file it came from.</summary>
public sealed record MeshMorphs(OrderedDictionary<string, MorphTarget> Targets, JsonObject Mdat, string Path);

/// <summary>One vertex buffer's place in the morph grid.</summary>
public sealed class MorphBufferEntry
{
    public int Offset { get; init; }
    public int Extent { get; set; }
    public List<string> Materials { get; } = [];
}

/// <summary>Decodes retained Source 2 morph atlases and validates their native vertex-buffer layout.</summary>
public static class MorphAtlas
{
    private static readonly (string Bundle, string Channel)[] Bundles =
    [
        ("MORPH_BUNDLE_TYPE_POSITION_SPEED", MorphTarget.PositionChannel),
        ("MORPH_BUNDLE_TYPE_NORMAL_WRINKLE", MorphTarget.NormalChannel)
    ];

    private static int RoundedProduct(double value, int size) => (int)MathF.Round((float)value * size, MidpointRounding.ToEven);

    /// <summary>Reconstructs object-space deltas with VRF's float32 UNORM8 and clipped-rectangle semantics.</summary>
    /// <remarks>All arithmetic stays in single precision, because VRF's decode does: the UNORM division, range and offset
    /// each round in float32, and the grid search compares these exact values against the exported glTF deltas. The
    /// fourth component of each bundle, speed or wrinkle, is decoded in the source but no consumer reads it.</remarks>
    public static OrderedDictionary<string, MorphTarget> DecodeMorphAtlas(JsonNode? data, SourceImage image)
    {
        if (data.Need("m_nLookupType").Str() != "LOOKUP_TYPE_VERTEX_ID") throw new InvalidDataException("Morph atlas requires vertex-ID lookup");
        if (data.Need("m_nEncodingType").Str() != "ENCODING_TYPE_OBJECT_SPACE") throw new InvalidDataException("Morph atlas requires object-space encoding");
        int width = data.Need("m_nWidth").Int(), height = data.Need("m_nHeight").Int();
        if (width <= 0 || height <= 0 || image.Mode != "RGBA") throw new InvalidDataException("Invalid morph grid or lossless RGBA8 atlas");
        int textureHeight = image.Height, textureWidth = image.Width;
        var pixels = image.Pixels;
        var pixelCount = (long)textureWidth * textureHeight;
        var bundleTypes = data.Need("m_bundleTypes").Arr().Select(item => item.Str()).ToList();
        var output = new OrderedDictionary<string, MorphTarget>(StringComparer.Ordinal);
        var slots = new int[width * height];
        foreach (var morph in data.Get("m_morphDatas").Arr())
        {
            var name = morph.Get("m_name").Str();
            if (name.Length == 0) continue;
            if (output.ContainsKey(name)) throw new InvalidDataException($"Duplicate morph atlas target {PythonText.Repr(name)}");
            var channels = new Dictionary<string, (List<int> Vertices, List<V3> Values)>(StringComparer.Ordinal);
            foreach (var (bundle, channel) in Bundles)
                if (bundleTypes.Contains(bundle)) channels[channel] = ([], []);
            foreach (var rect in morph.Get("m_morphRectDatas").Arr())
            {
                var rectangleWidth = RoundedProduct(rect.Need("m_flUWidthSrc").Dbl(), textureWidth);
                var rectangleHeight = RoundedProduct(rect.Need("m_flVHeightSrc").Dbl(), textureHeight);
                var bundles = rect.Get("m_bundleDatas").Arr();
                if (bundles.Count > bundleTypes.Count || rectangleWidth < 0 || rectangleHeight < 0) throw new InvalidDataException("Invalid morph atlas rectangle or bundle count");
                int top = rect.Need("m_nYTopDst").Int(), left = rect.Need("m_nXLeftDst").Int();
                for (var slot = 0; slot < bundles.Count; slot++)
                {
                    var channel = Bundles.FirstOrDefault(item => item.Bundle == bundleTypes[slot]).Channel;
                    if (channel is null) continue;
                    var bundle = bundles[slot];
                    var sourceU = RoundedProduct(bundle.Need("m_flULeftSrc").Dbl(), textureWidth);
                    var sourceV = RoundedProduct(bundle.Need("m_flVTopSrc").Dbl(), textureHeight);
                    var ranges = bundle.Need("m_ranges").Doubles();
                    var offsets = bundle.Need("m_offsets").Doubles();
                    if (ranges.Length != 4 || offsets.Length != 4 || !ranges.Concat(offsets).All(value => float.IsFinite((float)value)))
                        throw new InvalidDataException("Invalid morph atlas quantization parameters");
                    float rx = (float)ranges[0], ry = (float)ranges[1], rz = (float)ranges[2];
                    float ox = (float)offsets[0], oy = (float)offsets[1], oz = (float)offsets[2];
                    var (vertices, values) = channels[channel];
                    for (var row = 0; row < rectangleHeight; row++)
                    {
                        var destinationRow = row + top;
                        if (destinationRow < 0 || destinationRow >= height) continue;
                        for (var column = 0; column < rectangleWidth; column++)
                        {
                            var destinationColumn = column + left;
                            if (destinationColumn < 0 || destinationColumn >= width) continue;
                            var source = (long)(row + sourceV) * textureWidth + column + sourceU;
                            if (source < 0 || source >= pixelCount) continue;
                            var at = (int)(source * 4);
                            var value = new V3(pixels[at] / 255f * rx + ox, pixels[at + 1] / 255f * ry + oy, pixels[at + 2] / 255f * rz + oz);
                            var vertex = destinationRow * width + destinationColumn;
                            var slotted = slots[vertex] - 1;
                            if (slotted >= 0 && slotted < vertices.Count && vertices[slotted] == vertex)
                            {
                                values[slotted] = value;
                                continue;
                            }
                            slots[vertex] = vertices.Count + 1;
                            vertices.Add(vertex);
                            values.Add(value);
                        }
                    }
                }
            }
            var target = new MorphTarget();
            foreach (var (channel, (vertices, values)) in channels)
                target[channel] = MorphDelta.FromPairs(width * height, vertices, values);
            output[name] = target;
        }
        return output;
    }

    /// <summary>Resolves the mesh's exact MDAT block from retained typed or lossless raw documents.</summary>
    /// <remarks>Raw documents are parsed once and cached in the sidecar object, because every mesh of a model reads them.</remarks>
    private static JsonObject MeshMdat(JsonObject fidelity, JsonNode? mesh)
    {
        if (fidelity.Get("source_mdat") is not JsonArray documents)
        {
            var relative = fidelity.Get("raw_blocks").Get("MDAT").Str();
            if (relative.Length == 0) throw new InvalidDataException("Morph mapping requires retained MDAT draw calls");
            var path = Path.Combine(fidelity.Need("_sidecar_directory").Str(), relative);
            documents = new JsonArray(Kv3.ParseDocuments(PythonText.ReadBomless(path)).Select(document => (JsonNode?)document).ToArray());
            fidelity["source_mdat"] = documents;
        }
        var blocks = fidelity.Need("block_table").Arr().Where(block => block.Need("type").Str() == "MDAT").Select(block => block.Need("index").Lng()).ToList();
        var dataBlock = mesh.Need("data_block").Lng();
        if (blocks.Count != documents.Count || !blocks.Contains(dataBlock)) throw new InvalidDataException("Morph MDAT document count or block identity differs from the resource");
        return documents[blocks.IndexOf(dataBlock)].Obj();
    }

    /// <summary>Resolves the retained atlas resource beneath the source model's export directory.</summary>
    public static string MorphAtlasPath(JsonObject fidelity, JsonNode? data)
    {
        var resource = data.Need("m_pTextureAtlas").Str().Replace('\\', '/');
        var parent = PythonText.PosixParent(fidelity.Need("resource").Str());
        var parts = resource.Split('/', StringSplitOptions.RemoveEmptyEntries).Where(part => part != ".").ToList();
        if (resource.StartsWith('/') || parts.Contains("..")) throw new InvalidDataException("Unsafe morph atlas resource path");
        var parentParts = parent == "." ? [] : parent.Split('/', StringSplitOptions.RemoveEmptyEntries).ToList();
        var inside = parts.Count > parentParts.Count && parts.Take(parentParts.Count).SequenceEqual(parentParts);
        if (!inside) throw new InvalidDataException($"{PythonText.Repr(string.Join('/', parts))} is not in the subpath of {PythonText.Repr(parent)}");
        var relative = parts.Skip(parentParts.Count).ToList();
        relative[^1] = PathText.Stem(relative[^1]) + ".png";
        return Path.GetFullPath(Path.Combine([fidelity.Need("_sidecar_directory").Str(), .. relative]));
    }

    /// <summary>Reads a mesh's exact retained PNG atlas without reexporting its source resource.</summary>
    /// <returns>The decoded morphs, or null when the mesh has none.</returns>
    public static MeshMorphs? LoadMeshMorphs(JsonObject fidelity, JsonNode? mesh)
    {
        if (mesh.Get("morph_index") is not { } morphIndex) return null;
        var data = fidelity.Need("morphs").Arr()[morphIndex.Int()];
        if (!data.Get("m_morphDatas").Arr().Any(morph => morph.Get("m_name").Bool())) return null;
        var path = MorphAtlasPath(fidelity, data);
        var image = SourceImage.Open(path) ?? throw new FileNotFoundException($"No such file: {path}", path);
        if (image.Mode != "RGBA") throw new InvalidDataException($"Morph atlas must retain lossless RGBA channels: {path}");
        return new(DecodeMorphAtlas(data, image), MeshMdat(fidelity, mesh), path);
    }

    /// <summary>Derives first-use buffer offsets from the original ordered mesh draw calls.</summary>
    public static List<MorphBufferEntry> MorphBufferLayout(JsonObject mdat)
    {
        var buffers = new Dictionary<string, MorphBufferEntry>(StringComparer.Ordinal);
        var ordered = new List<MorphBufferEntry>();
        var offset = 0;
        foreach (var sceneObject in mdat.Get("m_sceneObjects").Arr())
            foreach (var draw in sceneObject.Get("m_drawCalls").Arr())
            {
                var handles = draw.Need("m_vertexBuffers").Arr().Select(buffer => Fmt.Int(buffer.Need("m_hBuffer").Lng())).ToList();
                if (handles.Count is < 1 or > 2) throw new InvalidDataException("Unsupported morph vertex-buffer layout");
                var key = string.Join(',', handles);
                if (!buffers.TryGetValue(key, out var entry))
                {
                    buffers[key] = entry = new() { Offset = offset };
                    ordered.Add(entry);
                }
                var count = draw.Need("m_nVertexCount").Int();
                entry.Extent = Math.Max(entry.Extent, draw.Need("m_nBaseVertex").Int() + count);
                var material = draw.Has("m_material") ? draw.Get("m_material") : draw.Get("m_pMaterial");
                if (material is not JsonValue text || text.GetValueKind() != System.Text.Json.JsonValueKind.String)
                    throw new InvalidDataException("Morph draw call has no unambiguous source material");
                entry.Materials.Add(Resources.Token(text.Str()));
                offset += count;
            }
        return ordered;
    }

    /// <summary>Matches native DMX buffer creation order, material membership and draw-call extents to morph offsets.</summary>
    /// <remarks>A single DMX mesh covering several buffers is one contiguous run from the start of the grid.</remarks>
    public static List<(int Offset, int Count)> MorphBufferSlices(IReadOnlyList<DmxElement> meshes, JsonObject mdat, int vertexCount)
    {
        var entries = MorphBufferLayout(mdat);
        if (meshes.Count == 1 && entries.Count > 1)
        {
            var merged = new MorphBufferEntry { Offset = 0, Extent = entries.Sum(entry => entry.Extent) };
            merged.Materials.AddRange(entries.SelectMany(entry => entry.Materials));
            entries = [merged];
        }
        if (meshes.Count != entries.Count) throw new InvalidDataException("Native DMX mesh count differs from morph draw-call buffers");
        var slices = new List<(int, int)>();
        for (var index = 0; index < meshes.Count; index++)
        {
            var entry = entries[index];
            var state = meshes[index].Element("currentState")!;
            var key = state.Contains("position$0") ? "position$0" : "positions";
            var count = state.Array<V3>(key).Count;
            var materials = meshes[index].Elements("faceSets").Select(face => Resources.Token(face!.Element("material")!.Text("mtlName") ?? "")).ToList();
            if (count != entry.Extent || !materials.Order(StringComparer.Ordinal).SequenceEqual(entry.Materials.Order(StringComparer.Ordinal)))
                throw new InvalidDataException("Native DMX vertex extent or material membership differs from morph draw calls");
            if (entry.Offset < 0 || entry.Offset + count > vertexCount) throw new InvalidDataException("Native DMX vertices exceed their authored morph grid");
            slices.Add((entry.Offset, count));
        }
        return slices;
    }
}
