using System.Text;

namespace PortingKit.Addons.Porting;

/// <summary>Reads Source 2 Viewer DMX meshes without dropping authored LOD skinning.</summary>
public static class DmxMesh
{
    /// <summary>The bone count a reference SMD's nodes block declares.</summary>
    private static int ReferenceBoneCount(string header)
    {
        var nodes = header[(header.IndexOf("nodes\n", StringComparison.Ordinal) + "nodes\n".Length)..];
        nodes = nodes[..nodes.IndexOf("end\n", StringComparison.Ordinal)];
        return PythonText.SplitLines(nodes).Count(line => line.Trim().Length > 0);
    }

    /// <summary>Reuses a verified native skeleton and exports one DMX mesh in Source inches.</summary>
    /// <remarks>An unweighted DMX, a static prop's LOD, binds every vertex to the reference's only bone, and is
    /// refused against a multi-bone skeleton.</remarks>
    /// <returns>The number of triangles written.</returns>
    public static int WriteDmxReference(string dmxPath, string smdPath, string reference, IReadOnlyList<string> boneNames)
    {
        var data = DmxDocument.Load(dmxPath);
        var models = data.OfType("DmeModel");
        var meshes = data.OfType("DmeMesh");
        if (models.Count != 1 || meshes.Count != 1) throw new InvalidDataException("LOD DMX must contain exactly one model and one mesh");
        var joints = models[0].Elements("jointList");
        var mesh = meshes[0];
        var state = mesh.Element("currentState") ?? throw new InvalidDataException("LOD DMX mesh has no current state");
        var count = state.Value<int>("jointCount");
        var positions = state.Array<V3>("position$0");
        var normals = state.Array<V3>("normal$0");
        var uv = state.Array<V2>("texcoord$0");
        var positionIndices = state.Array<int>("position$0Indices");
        var normalIndices = state.Array<int>("normal$0Indices");
        var uvIndices = state.Array<int>("texcoord$0Indices");
        var referenceText = PythonText.Read(reference);
        var header = referenceText[..referenceText.IndexOf("triangles\n", StringComparison.Ordinal)];
        var rigid = count == 0;
        if (rigid && ReferenceBoneCount(header) != 1) throw new InvalidDataException("An unweighted LOD DMX needs a single-bone reference skeleton");
        var slots = rigid ? 1 : count;
        double[] weights;
        int[] indices;
        var mapped = new Dictionary<int, int>();
        if (rigid)
        {
            weights = Enumerable.Repeat(1.0, positions.Count).ToArray();
            indices = new int[positions.Count];
            mapped[0] = 0;
        }
        else
        {
            weights = state.Numbers("blendweights$0");
            indices = state.Array<int>("blendindices$0").ToArray();
            var lookup = new Dictionary<string, int>(StringComparer.Ordinal);
            for (var index = 0; index < boneNames.Count; index++) lookup[boneNames[index]] = index;
            for (var index = 0; index < joints.Count; index++)
                if (joints[index]?.Name is { } name && lookup.TryGetValue(name, out var bone)) mapped[index] = bone;
        }
        var triangles = 0;
        using var output = Fmt.OpenText(smdPath);
        output.Write(header);
        output.Write("triangles\n");
        foreach (var faceSet in mesh.Elements("faceSets"))
        {
            var material = Path.GetFileNameWithoutExtension(faceSet!.Element("material")!.Text("mtlName")) ?? "";
            var face = new List<int>(3);
            foreach (var vertex in faceSet.Array<int>("faces"))
            {
                if (vertex >= 0)
                {
                    face.Add(vertex);
                    continue;
                }
                if (face.Count != 3) throw new InvalidDataException("Source 2 mesh contains a non-triangle polygon");
                output.Write(material);
                output.Write('\n');
                foreach (var corner in face)
                {
                    var position = positionIndices[corner];
                    var links = new List<(int Joint, double Weight)>(slots);
                    for (var slot = 0; slot < slots; slot++)
                    {
                        var weight = weights[position * slots + slot];
                        if (weight > 0) links.Add((mapped[indices[position * slots + slot]], weight));
                    }
                    if (links.Count == 0 || links.Sum(link => link.Weight) <= 0) throw new InvalidDataException("Unweighted DMX vertex");
                    var texcoord = uv[uvIndices[corner]];
                    output.Write(SmdSkeleton.VertexLine(positions[position], normals[normalIndices[corner]], new(texcoord.X, 1.0 - texcoord.Y), links));
                }
                triangles++;
                face.Clear();
            }
            if (face.Count > 0) throw new InvalidDataException("Unterminated DMX face");
        }
        output.Write("end\n");
        return triangles;
    }

    /// <summary>One material and primary-bone group of an SMD's triangles, in first-seen order.</summary>
    private sealed class SmdGroups
    {
        public required string Header { get; init; }
        public List<(string Material, int Bone)> Keys { get; } = [];
        public Dictionary<(string Material, int Bone), List<string[]>> Groups { get; } = [];
    }

    /// <summary>Groups intact triangle records by material and primary bone.</summary>
    private static SmdGroups ReadGroups(string path)
    {
        var text = PythonText.Read(path);
        var at = text.IndexOf("triangles\n", StringComparison.Ordinal);
        var result = new SmdGroups { Header = text[..at] };
        var records = PythonText.SplitLines(text[(at + "triangles\n".Length)..]);
        for (var index = 0; index < records.Count - 1; index += 4)
        {
            if (index + 4 > records.Count) throw new InvalidDataException($"Incomplete SMD triangle: {path}");
            var triangle = records.GetRange(index, 4).ToArray();
            var primary = new Dictionary<int, int>();
            foreach (var vertex in triangle.Skip(1))
            {
                var bone = int.Parse(vertex.Split((char[]?)null, 2, StringSplitOptions.RemoveEmptyEntries)[0]);
                primary[bone] = primary.GetValueOrDefault(bone) + 1;
            }
            var chosen = primary.OrderBy(pair => -pair.Value).ThenBy(pair => pair.Key).First().Key;
            var key = (triangle[0], chosen);
            if (!result.Groups.TryGetValue(key, out var list))
            {
                result.Groups[key] = list = [];
                result.Keys.Add(key);
            }
            list.Add(triangle);
        }
        return result;
    }

    /// <summary>Keeps matching bodyparts below the compiler's split limit so all LOD replacements apply.</summary>
    /// <returns>QC lines and files, both empty when the model fits in one part.</returns>
    public static (List<string> Qc, List<string> Outputs) PartitionLodMeshes(string reference, IReadOnlyList<(double Distance, string Source)> lods, int maxVertices = 30000)
    {
        var baseline = ReadGroups(reference);
        var partitions = new List<List<(string, int)>>();
        var active = new List<(string, int)>();
        var activeVertices = new HashSet<string>(StringComparer.Ordinal);
        foreach (var key in baseline.Keys)
        {
            var vertices = new HashSet<string>(baseline.Groups[key].SelectMany(triangle => triangle.Skip(1)), StringComparer.Ordinal);
            if (vertices.Count > maxVertices) throw new InvalidDataException($"Material/bone group {key} exceeds the safe LOD bodypart limit");
            if (active.Count > 0 && activeVertices.Union(vertices).Count() > maxVertices)
            {
                partitions.Add(active);
                active = [];
                activeVertices = new(StringComparer.Ordinal);
            }
            active.Add(key);
            activeVertices.UnionWith(vertices);
        }
        if (active.Count > 0) partitions.Add(active);
        if (partitions.Count <= 1) return ([], []);
        var sources = new List<(double Distance, string Source, SmdGroups Groups)> { (0.0, reference, baseline) };
        foreach (var (distance, source) in lods)
        {
            var groups = ReadGroups(source);
            var unknown = groups.Keys.Where(key => !baseline.Groups.ContainsKey(key)).ToList();
            if (unknown.Count > 0) throw new InvalidDataException($"LOD introduced material/bone groups absent from the reference: {string.Join(", ", unknown)}");
            sources.Add((distance, source, groups));
        }
        var outputs = new List<string>();
        var qc = new List<string>();
        var referenceStem = Path.GetFileNameWithoutExtension(reference);
        for (var level = 0; level < sources.Count; level++)
        {
            var (distance, source, groups) = sources[level];
            if (level > 0) qc.AddRange([$"$lod {Fmt.G(distance)}", "{"]);
            for (var index = 0; index < partitions.Count; index++)
            {
                var triangles = partitions[index].SelectMany(key => groups.Groups.GetValueOrDefault(key) ?? []).ToList();
                var baseName = $"{referenceStem}_part{index}.smd";
                if (triangles.Count == 0)
                {
                    if (level > 0) qc.Add($"    removemodel \"{baseName}\"");
                    continue;
                }
                var destination = Path.Combine(Path.GetDirectoryName(source) ?? "", $"{Path.GetFileNameWithoutExtension(source)}_part{index}.smd");
                var text = new StringBuilder(groups.Header).Append("triangles\n");
                text.Append(string.Join('\n', triangles.SelectMany(triangle => triangle))).Append("\nend\n");
                PythonText.Write(destination, text.ToString());
                outputs.Add(destination);
                qc.Add(level > 0 ? $"    replacemodel \"{baseName}\" \"{Path.GetFileName(destination)}\"" : $"$body \"body{index}\" \"{baseName}\"");
            }
            if (level > 0) qc.Add("}");
        }
        return (qc, outputs);
    }

    /// <summary>Writes authored LOD meshes beside a reference SMD and returns their QC.</summary>
    /// <returns>QC lines, every file written, and whether large meshes were partitioned. Partitioned lines declare
    /// their own bodyparts and replace every <c>$body</c> line of the model.</returns>
    public static (List<string> Lines, List<string> Files, bool Partitioned) WriteLodReferences(string reference, IReadOnlyList<(double Distance, string Source)> lodSources,
        IReadOnlyList<string> boneNames, PortContext? context = null)
    {
        var lodSmds = new List<(double Distance, string Source)>();
        var files = new List<string>();
        var stem = Path.GetFileNameWithoutExtension(reference);
        for (var index = 0; index < lodSources.Count; index++)
        {
            var (distance, source) = lodSources[index];
            var destination = Path.Combine(Path.GetDirectoryName(reference) ?? "", $"{stem}_lod{index + 1}.smd");
            var triangles = WriteDmxReference(source, destination, reference, boneNames);
            lodSmds.Add((distance, destination));
            files.Add(destination);
            context?.Log($"LOD {index + 1}: {triangles} triangles from {Path.GetFileName(source)}");
        }
        if (lodSmds.Count == 0) return ([], files, false);
        var (partitionLines, partitionFiles) = PartitionLodMeshes(reference, lodSmds);
        if (partitionLines.Count > 0) return (partitionLines, [.. files, .. partitionFiles], true);
        var lines = new List<string>();
        foreach (var (distance, destination) in lodSmds)
            lines.AddRange([$"$lod {Fmt.G(distance)}", "{", $"    replacemodel \"{Path.GetFileName(reference)}\" \"{Path.GetFileName(destination)}\"", "}"]);
        return (lines, files, false);
    }
}
