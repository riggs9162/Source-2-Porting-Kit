using System.Buffers.Binary;
using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>Restores authored per-LOD morphs after compiler vertex unification and audits every rendered output.</summary>
/// <remarks>
/// studiomdl merges vertices across LODs by tolerance and keeps only LOD0 flex data for the merged vertex, so a
/// lower LOD ends up deforming with data that was never authored for it, and controller, rule and descriptor counts
/// still match. The repair proves which authored vertex each rendered vertex came from, isolates the LOD vertex pools,
/// and rewrites each mesh's flex records from the authored deltas. The engine reads a delta as a half float converted
/// to 1/4096-inch fixed point, and every comparison here is made in that fixed-point value. Comparing the half or the
/// source float would report differences the game can never show.
/// Morph data is held sparsely, one entry per vertex and target that moves, where v3 held dense vertex-by-target
/// arrays. A vertex with no entries behaves exactly as v3's row of zeros did.
/// </remarks>
public static class CompiledMorphGeometry
{
    private const double SnapRadius = .0501;
    private const int RecordStride = 16;
    private const int MaximumFlexVertices = 10000;
    private const uint FixedPointScaleFlag = 0x200000;
    private const string SourceEncoding = "Legacy half-float with engine 1/4096 fixed conversion";
    private const string PrecisionScope = "All source morphs on VTX-used material vertices at every authored LOD; errors are per unit weight";
    private const string PrecisionWeightScaling = "Signed and overshoot weights scale these deformation errors by the absolute descriptor weight";
    private const string WeightEncoding = "Single canonical direct-weight key (0,1,10,11), including signed and overshoot weights";

    private static readonly int[][] Permutations = [[0, 1, 2], [0, 2, 1], [1, 0, 2], [1, 2, 0], [2, 0, 1], [2, 1, 0]];

    /// <summary>One generated material stream in the compiler's model-space basis.</summary>
    /// <remarks>Entries of a vertex run from <see cref="Start"/>. Those up to <see cref="ActiveEnd"/> survive into
    /// the engine's fixed-point value. The rest round to nothing and only count towards the precision report.</remarks>
    private sealed class MorphSource
    {
        private const double CellSize = .0502;

        private readonly Dictionary<(long, long, long), List<int>> cells = [];

        public required double[] Position { get; init; }
        public required double[] Normal { get; init; }
        public required double[] Uv { get; init; }
        public required int[] Bones { get; init; }
        public required double[] Weights { get; init; }
        public required List<string> Targets { get; init; }
        public required int[] Start { get; init; }
        public required int[] ActiveEnd { get; init; }
        public required int[] EntryTarget { get; init; }
        public required double[] Changes { get; init; }
        public required int[] Effective { get; init; }
        public required int[] Triangles { get; init; }

        public Dictionary<int, HashSet<int>> Adjacency => field ??= BuildAdjacency();

        private Dictionary<int, HashSet<int>> BuildAdjacency()
        {
            var adjacency = new Dictionary<int, HashSet<int>>();
            for (var corner = 0; corner < Triangles.Length; corner++)
            {
                if (!adjacency.TryGetValue(Triangles[corner], out var faces)) adjacency[Triangles[corner]] = faces = [];
                faces.Add(corner / 3);
            }
            return adjacency;
        }

        private static (long, long, long) Cell(double x, double y, double z) =>
            ((long)Math.Floor(x / CellSize), (long)Math.Floor(y / CellSize), (long)Math.Floor(z / CellSize));

        /// <summary>Indexes the stream's vertices for radius queries. A nonfinite position is refused here, as a spatial tree refuses it.</summary>
        public void BuildIndex()
        {
            if (!Position.All(double.IsFinite)) throw new CompiledDataError("data must be finite, check for nan or inf values");
            for (var vertex = 0; vertex < Position.Length / 3; vertex++)
            {
                var key = Cell(Position[vertex * 3], Position[vertex * 3 + 1], Position[vertex * 3 + 2]);
                if (!cells.TryGetValue(key, out var list)) cells[key] = list = [];
                list.Add(vertex);
            }
        }

        /// <summary>Every vertex within the compiler's snap distance, lowest index first.</summary>
        /// <remarks>v3 took a tree's unsorted traversal order and used its first survivor, which picks arbitrarily
        /// among candidates whose fixed-point deltas are all equal. The pick cannot change what the engine reads.
        /// Replaying the zombie repair sorted, reversed and in tree order gave byte-identical output.</remarks>
        public List<int> Within(double x, double y, double z)
        {
            var found = new List<int>();
            var (cx, cy, cz) = Cell(x, y, z);
            var limit = SnapRadius * SnapRadius;
            for (var ix = cx - 1; ix <= cx + 1; ix++)
                for (var iy = cy - 1; iy <= cy + 1; iy++)
                    for (var iz = cz - 1; iz <= cz + 1; iz++)
                    {
                        if (!cells.TryGetValue((ix, iy, iz), out var list)) continue;
                        foreach (var vertex in list)
                        {
                            double dx = Position[vertex * 3] - x, dy = Position[vertex * 3 + 1] - y, dz = Position[vertex * 3 + 2] - z;
                            if (dx * dx + dy * dy + dz * dz <= limit) found.Add(vertex);
                        }
                    }
            found.Sort();
            return found;
        }

        public double Distance(int vertex, double x, double y, double z) => Length(Position[vertex * 3] - x, Position[vertex * 3 + 1] - y, Position[vertex * 3 + 2] - z);

        public double UvError(int vertex, double u, double v) => Math.Max(Math.Abs(Uv[vertex * 2] - u), Math.Abs(Uv[vertex * 2 + 1] - v));

        public double NormalError(int vertex, double x, double y, double z) =>
            Math.Max(Math.Max(Math.Abs(Normal[vertex * 3] - x), Math.Abs(Normal[vertex * 3 + 1] - y)), Math.Abs(Normal[vertex * 3 + 2] - z));

        /// <summary>Whether two vertices deform identically in the engine, whatever their raw deltas.</summary>
        public bool SameEffective(int a, int b)
        {
            int startA = Start[a], startB = Start[b], count = ActiveEnd[a] - startA;
            return count == ActiveEnd[b] - startB && EntryTarget.AsSpan(startA, count).SequenceEqual(EntryTarget.AsSpan(startB, count))
                && Effective.AsSpan(startA * 6, count * 6).SequenceEqual(Effective.AsSpan(startB * 6, count * 6));
        }
    }

    /// <summary>A resolved set of rendered vertices. <see cref="Selected"/> names the source vertex of each id.</summary>
    private sealed record MorphPlan((int Part, int Choice, int Mesh) Key, int Lod, int[] Ids, MorphSource Source, int[] Selected);

    /// <summary>The fixed-point deltas of one mesh by descriptor name, and the vertices any of them moves.</summary>
    private sealed record CompiledFlexes(Dictionary<string, Dictionary<int, int[]>> Names, HashSet<int> Moved);

    /// <summary>Deformation lost per unit weight through the target's half and fixed-point encoding.</summary>
    private sealed class Precision
    {
        public double PositionComponent { get; private set; }
        public double PositionVector { get; private set; }
        public double NormalComponent { get; private set; }
        public double NormalVector { get; private set; }

        public void Measure(double x, double y, double z, bool position)
        {
            var component = Math.Max(Math.Max(x, y), z);
            var vector = Length(x, y, z);
            if (position)
            {
                PositionComponent = Math.Max(PositionComponent, component);
                PositionVector = Math.Max(PositionVector, vector);
            }
            else
            {
                NormalComponent = Math.Max(NormalComponent, component);
                NormalVector = Math.Max(NormalVector, vector);
            }
        }

        public void Merge(Precision other)
        {
            PositionComponent = Math.Max(PositionComponent, other.PositionComponent);
            PositionVector = Math.Max(PositionVector, other.PositionVector);
            NormalComponent = Math.Max(NormalComponent, other.NormalComponent);
            NormalVector = Math.Max(NormalVector, other.NormalVector);
        }

        public JsonObject ToJson() => new()
        {
            ["maximum_position_component_error_inches"] = PositionComponent, ["maximum_position_vector_error_inches"] = PositionVector,
            ["maximum_normal_component_error"] = NormalComponent, ["maximum_normal_vector_error"] = NormalVector
        };
    }

    /// <summary>A vector length, summing in the order the array reduction that defined these reports sums.</summary>
    private static double Length(double x, double y, double z) => Math.Sqrt(x * x + (y * y + z * z));

    /// <summary>Represents the engine's legacy half-float to 1/4096-inch fixed-point conversion.</summary>
    private static int Effective(Half value)
    {
        var half = (double)value;
        var scaled = Math.Truncate(half * 4096);
        if (!double.IsFinite(half) || scaled < -32768 || scaled > 32767) throw new MorphGeometryError("Authored morph exceeds the legacy Source 1 vertex animation range");
        return (int)scaled;
    }

    /// <summary>Resolves numbered compiler bodygroup choices and their explicit authored replacements.</summary>
    /// <returns>By bodypart, then choice, then LOD: the source path, or null for a blank choice.</returns>
    private static List<List<List<string?>>> SourceChoices(string qc, JsonObject appearance)
    {
        var groups = new List<List<string?>>();
        List<string?>? active = null;
        foreach (var line in PythonText.SplitLines(PythonText.Read(qc).TrimStart('﻿')))
        {
            var values = CompiledText.Tokens(CompiledText.StripComment(line));
            if (values.Count == 0) continue;
            var command = values[0].ToLowerInvariant();
            if (command is "$body" or "$model") groups.Add([values[2]]);
            else if (command == "$bodygroup") groups.Add(active = []);
            else if (active is not null && command is "studio" or "blank") active.Add(command == "studio" ? values[1] : null);
            else if (active is not null && command == "}") active = null;
        }
        var levels = new List<Dictionary<string, string?>> { new(StringComparer.Ordinal) };
        foreach (var lod in appearance.Get("lods").Arr())
        {
            var level = new Dictionary<string, string?>(StringComparer.Ordinal);
            foreach (var item in lod.Key("replacements").Arr())
            {
                var replacement = item.Key("replacement");
                level[item.Key("base").Str()] = replacement is null ? null : replacement.Str();
            }
            levels.Add(level);
        }
        var directory = CompiledText.Parent(PathText.Native(qc));
        return groups.ConvertAll(choices => choices.ConvertAll(baseName => levels.ConvertAll(level =>
        {
            var name = baseName is null ? null : level.GetValueOrDefault(baseName, baseName);
            return string.IsNullOrEmpty(name) ? null : CompiledText.Join(directory, name);
        })));
    }

    /// <summary>Decodes the deltas of one state into six values per vertex that moves, in the compiler's basis.</summary>
    /// <remarks>Subtracting from and adding to zero is not a no-op: the rotation into the compiler's basis has always
    /// summed three products starting from positive zero, so a negative zero never survives it, and a zero sign would
    /// otherwise reach the serialized half float.</remarks>
    private static Dictionary<int, double[]> ReadDelta(DmxElement delta, int vertexCount)
    {
        var values = new Dictionary<int, double[]>();
        foreach (var (channel, start) in new[] { ("positions", 0), ("normals", 3) })
        {
            var selection = delta.Array<int>(channel + "Indices");
            var changes = delta.Array<V3>(channel);
            if (selection.Count != changes.Count || (selection.Count > 0 && (selection.Min() < 0 || selection.Max() >= vertexCount))) throw new MorphGeometryError("Invalid generated DMX morph indices");
            for (var index = 0; index < selection.Count; index++)
            {
                if (!values.TryGetValue(selection[index], out var row)) values[selection[index]] = row = new double[6];
                row[start] = 0.0 - changes[index].Y;
                row[start + 1] = 0.0 + changes[index].X;
                row[start + 2] = 0.0 + changes[index].Z;
            }
        }
        return values;
    }

    /// <summary>Maps skin links to compiled bones, padded and ordered the way a VVD vertex stores them.</summary>
    private static (int[] Bones, double[] Weights) ReadSkin(DmxElement state, DmxElement model, Dictionary<string, int> boneNames)
    {
        var jointCount = state.Value<int>("jointCount");
        if (jointCount <= 0) throw new CompiledDataError("Generated morph source has no joint weights to reshape");
        var rawWeights = state.Numbers("jointWeights");
        var rawIndices = state.Array<int>("jointIndices");
        var boneMap = model.Elements("jointList").Select(joint => boneNames.GetValueOrDefault(joint?.Name ?? "", -1)).ToArray();
        if (jointCount > 3 || rawIndices.Zip(rawWeights).Any(pair => boneMap[pair.First] < 0 && pair.Second > 0)) throw new MorphGeometryError("Generated morph source has unsupported skin links");
        var rows = rawWeights.Length / jointCount;
        var bones = new int[rows * 3];
        var weights = new double[rows * 3];
        Span<int> rowBones = stackalloc int[3];
        Span<double> rowWeights = stackalloc double[3];
        for (var row = 0; row < rows; row++)
        {
            for (var slot = 0; slot < 3; slot++)
            {
                var linked = slot < jointCount;
                var weight = linked ? rawWeights[row * jointCount + slot] : 0.0;
                rowWeights[slot] = weight;
                rowBones[slot] = linked && weight > 0 ? boneMap[rawIndices[row * jointCount + slot]] : 65535;
            }
            SortSkin(rowBones, rowWeights);
            var total = rowWeights[0] + (rowWeights[1] + rowWeights[2]);
            for (var slot = 0; slot < 3; slot++)
            {
                bones[row * 3 + slot] = rowBones[slot];
                weights[row * 3 + slot] = rowWeights[slot] / total;
            }
        }
        return (bones, weights);
    }

    /// <summary>Orders a vertex's three links by bone, keeping tied links in place.</summary>
    private static void SortSkin(Span<int> bones, Span<double> weights)
    {
        for (var index = 1; index < 3; index++)
        {
            var bone = bones[index];
            var weight = weights[index];
            var cursor = index - 1;
            for (; cursor >= 0 && bones[cursor] > bone; cursor--)
            {
                bones[cursor + 1] = bones[cursor];
                weights[cursor + 1] = weights[cursor];
            }
            bones[cursor + 1] = bone;
            weights[cursor + 1] = weight;
        }
    }

    /// <summary>Builds one material's stream from its face set, with its morph entries grouped by vertex.</summary>
    private static MorphSource BuildStream(DmxElement face, double[] positions, double[] normals, double[] uv, int[] bones, double[] weights, List<string> names, List<Dictionary<int, double[]>> deltas)
    {
        var corners = face.Array<int>("faces").Where(index => index >= 0).ToArray();
        if (corners.Length % 3 != 0) throw new MorphGeometryError("Generated morph source contains non-triangle faces");
        if (names.Count == 0) throw new CompiledDataError("need at least one array to stack");
        var selected = corners.Distinct().Order().ToArray();
        var remap = new Dictionary<int, int>(selected.Length);
        for (var index = 0; index < selected.Length; index++) remap[selected[index]] = index;
        var entries = new List<(int Row, bool Quiet, int Target, double[] Changes, int[] Effective)>();
        for (var target = 0; target < deltas.Count; target++)
        {
            foreach (var (vertex, changes) in deltas[target])
            {
                if (!remap.TryGetValue(vertex, out var row)) continue;
                var effective = Array.ConvertAll(changes, value => Effective((Half)value));
                if (changes.Any(value => value != 0)) entries.Add((row, effective.All(value => value == 0), target, changes, effective));
            }
        }
        var ordered = entries.OrderBy(entry => entry.Row).ThenBy(entry => entry.Quiet).ThenBy(entry => entry.Target).ToList();
        var start = new int[selected.Length + 1];
        var activeEnd = new int[selected.Length];
        var entryTarget = new int[ordered.Count];
        var entryChanges = new double[ordered.Count * 6];
        var entryEffective = new int[ordered.Count * 6];
        for (var index = 0; index < ordered.Count; index++)
        {
            var entry = ordered[index];
            start[entry.Row + 1]++;
            if (!entry.Quiet) activeEnd[entry.Row]++;
            entryTarget[index] = entry.Target;
            entry.Changes.CopyTo(entryChanges, index * 6);
            entry.Effective.CopyTo(entryEffective, index * 6);
        }
        for (var row = 0; row < selected.Length; row++)
        {
            start[row + 1] += start[row];
            activeEnd[row] += start[row];
        }
        var source = new MorphSource
        {
            Position = Pick(positions, selected, 3), Normal = Pick(normals, selected, 3), Uv = Pick(uv, selected, 2), Bones = Pick(bones, selected, 3), Weights = Pick(weights, selected, 3),
            Targets = names, Start = start, ActiveEnd = activeEnd, EntryTarget = entryTarget, Changes = entryChanges, Effective = entryEffective, Triangles = Array.ConvertAll(corners, index => remap[index])
        };
        source.BuildIndex();
        return source;
    }

    private static T[] Pick<T>(T[] values, int[] rows, int width)
    {
        var picked = new T[rows.Length * width];
        for (var index = 0; index < rows.Length; index++) Array.Copy(values, rows[index] * width, picked, index * width, width);
        return picked;
    }

    /// <summary>Decodes generated indexed DMX material streams in the compiler's model-space basis.</summary>
    /// <remarks>The DMX is Y-up with a flipped V where the compiled model is Z-up, so positions, normals and every
    /// delta are rotated a quarter turn about Z and V is mirrored before anything is compared to the VVD.</remarks>
    private static Dictionary<string, MorphSource> SourceMesh(string path, Dictionary<string, int> boneNames)
    {
        var document = DmxDocument.Load(path);
        var meshes = document.OfType("DmeMesh");
        if (meshes.Count != 1) throw new MorphGeometryError($"Expected one generated morph mesh: {path}");
        var mesh = meshes[0];
        var state = mesh.Element("bindState") ?? throw new MorphGeometryError($"Expected one generated morph mesh: {path}");
        var model = document.OfType("DmeModel").First();
        var (bones, weights) = ReadSkin(state, model, boneNames);
        var sourcePositions = state.Array<V3>("positions");
        var sourceNormals = state.Array<V3>("normals");
        var sourceUv = state.Array<V2>("textureCoordinates");
        var vertexCount = sourcePositions.Count;
        var positions = new double[vertexCount * 3];
        var normals = new double[sourceNormals.Count * 3];
        var uv = new double[sourceUv.Count * 2];
        for (var index = 0; index < vertexCount; index++) (positions[index * 3], positions[index * 3 + 1], positions[index * 3 + 2]) = (0.0 - sourcePositions[index].Y, 0.0 + sourcePositions[index].X, 0.0 + sourcePositions[index].Z);
        for (var index = 0; index < sourceNormals.Count; index++) (normals[index * 3], normals[index * 3 + 1], normals[index * 3 + 2]) = (0.0 - sourceNormals[index].Y, 0.0 + sourceNormals[index].X, 0.0 + sourceNormals[index].Z);
        for (var index = 0; index < sourceUv.Count; index++) (uv[index * 2], uv[index * 2 + 1]) = (sourceUv[index].X, 1 - sourceUv[index].Y);
        var names = new List<string>();
        var deltas = new List<Dictionary<int, double[]>>();
        foreach (var delta in mesh.Elements("deltaStates"))
        {
            var values = ReadDelta(delta!, vertexCount);
            var name = delta!.Name ?? "";
            var known = names.IndexOf(name);
            if (known >= 0) deltas[known] = values;
            else
            {
                names.Add(name);
                deltas.Add(values);
            }
        }
        var output = new Dictionary<string, MorphSource>(StringComparer.Ordinal);
        foreach (var face in mesh.Elements("faceSets"))
        {
            var name = Resources.Token(face!.Element("material")!.Text("mtlName") ?? "");
            output[name] = BuildStream(face, positions, normals, uv, bones, weights, names, deltas);
        }
        return output;
    }

    /// <summary>Intersects all incident triangle correspondences when vertex attributes alone are ambiguous.</summary>
    /// <remarks>Two authored corners can share a position, a UV and a normal and still disagree about a morph, along
    /// a mouth seam for instance. Only the triangles around the rendered vertex can tell them apart, so each incident
    /// triangle has to exist in the source, and the corner it assigns must be one all those triangles agree on.</remarks>
    private static int[] CornerCandidates(int vertexId, Dictionary<int, int[]> candidates, int[] triangles, List<int>? incident, MorphSource source)
    {
        var adjacency = source.Adjacency;
        var possible = new HashSet<int>(candidates[vertexId]);
        var corners = new int[3];
        foreach (var triangle in incident ?? [])
        {
            for (var corner = 0; corner < 3; corner++) corners[corner] = triangles[triangle * 3 + corner];
            HashSet<int>? sourceFaces = null;
            foreach (var index in corners)
            {
                var faces = new HashSet<int>();
                foreach (var candidate in candidates[index])
                    if (adjacency.TryGetValue(candidate, out var linked)) faces.UnionWith(linked);
                if (sourceFaces is null) sourceFaces = faces;
                else sourceFaces.IntersectWith(faces);
            }
            var supported = new HashSet<int>();
            var own = Array.IndexOf(corners, vertexId);
            foreach (var faceIndex in sourceFaces ?? [])
            {
                foreach (var order in Permutations)
                {
                    var matches = true;
                    for (var index = 0; index < 3 && matches; index++)
                        matches = Array.BinarySearch(candidates[corners[index]], source.Triangles[faceIndex * 3 + order[index]]) >= 0;
                    if (matches) supported.Add(source.Triangles[faceIndex * 3 + order[own]]);
                }
            }
            if (supported.Count == 0) throw new MorphGeometryError($"Rendered vertex {vertexId} has no complete authored triangle provenance");
            possible.IntersectWith(supported);
        }
        if (incident is null or { Count: 0 } || possible.Count == 0) throw new MorphGeometryError($"Rendered vertex {vertexId} merges incompatible authored triangle corners");
        return possible.Order().ToArray();
    }

    private static bool AllEffectiveEqual(MorphSource source, List<int> candidates) => candidates.TrueForAll(candidate => source.SameEffective(candidate, candidates[0]));

    /// <summary>Requires equivalent target deformations among all surviving bounded attribute candidates.</summary>
    /// <remarks>LOD0 vertices were written from the source, so they must match it almost exactly. A lower LOD went
    /// through the compiler's unification, which allows a 0.05 inch snap, 0.01 of UV and ten degrees of normal.</remarks>
    private static (int[] Chosen, JsonObject Report) ResolveVertices(MorphSource source, CompiledVertices vertices, int meshBase, int[] ids, int[] triangles, int lod, PortContext? context)
    {
        var chosen = new int[ids.Length];
        var snapMaximum = 0.0;
        int uvFallbacks = 0, multiple = 0, normalDisambiguations = 0, skinDisambiguations = 0, topologyDisambiguations = 0;
        var candidateSets = new Dictionary<int, int[]>(ids.Length);
        var ambiguous = new List<(int Ordinal, int VertexId)>();
        var minimumDot = Math.Cos(Fmt.Radians(10.01));
        Span<int> vertexBones = stackalloc int[3];
        Span<double> vertexWeights = stackalloc double[3];
        for (var ordinal = 0; ordinal < ids.Length; ordinal++)
        {
            if (ordinal % 1024 == 0) context?.Check();
            var vertexId = ids[ordinal];
            var row = meshBase + vertexId;
            double wx = vertices.Float(row, 0), wy = vertices.Float(row, 4), wz = vertices.Float(row, 8);
            double px = vertices.Float(row, 16), py = vertices.Float(row, 20), pz = vertices.Float(row, 24);
            double nx = vertices.Float(row, 28), ny = vertices.Float(row, 32), nz = vertices.Float(row, 36);
            double u = vertices.Float(row, 40), v = vertices.Float(row, 44);
            var candidates = source.Within(px, py, pz);
            if (candidates.Count == 0) throw new MorphGeometryError($"Rendered vertex {vertexId} has no source within the compiler LOD snap bound");
            if (lod == 0) candidates = candidates.FindAll(candidate => source.Distance(candidate, px, py, pz) <= 1e-4 && source.UvError(candidate, u, v) <= 1e-6 && source.NormalError(candidate, nx, ny, nz) <= 1e-5);
            else candidates = candidates.FindAll(candidate => source.UvError(candidate, u, v) <= .0101
                && source.Normal[candidate * 3] * nx + source.Normal[candidate * 3 + 1] * ny + source.Normal[candidate * 3 + 2] * nz >= minimumDot);
            if (candidates.Count == 0) throw new MorphGeometryError($"Rendered vertex {vertexId} has no source within attribute unification bounds");
            var original = candidates.ToArray();
            var needsTopology = !AllEffectiveEqual(source, candidates);
            var matchingUv = candidates.FindAll(candidate => source.UvError(candidate, u, v) <= 1e-6);
            if (matchingUv.Count > 0) candidates = matchingUv;
            else uvFallbacks++;
            if (!AllEffectiveEqual(source, candidates))
            {
                vertexWeights[0] = wx;
                vertexWeights[1] = wy;
                vertexWeights[2] = wz;
                for (var slot = 0; slot < 3; slot++) vertexBones[slot] = vertexWeights[slot] > 0 ? vertices.Byte(row, 12 + slot) : 65535;
                SortSkin(vertexBones, vertexWeights);
                var total = vertexWeights[0] + (vertexWeights[1] + vertexWeights[2]);
                var matchingSkin = new List<int>();
                foreach (var candidate in candidates)
                {
                    var matches = true;
                    for (var slot = 0; slot < 3; slot++)
                        matches &= source.Bones[candidate * 3 + slot] == vertexBones[slot] && Math.Abs(source.Weights[candidate * 3 + slot] - vertexWeights[slot] / total) <= 1e-5;
                    if (matches) matchingSkin.Add(candidate);
                }
                if (matchingSkin.Count > 0)
                {
                    candidates = matchingSkin;
                    skinDisambiguations++;
                }
            }
            if (!AllEffectiveEqual(source, candidates))
            {
                var matchingNormal = candidates.FindAll(candidate => source.NormalError(candidate, nx, ny, nz) <= 1e-5);
                if (matchingNormal.Count > 0)
                {
                    candidates = matchingNormal;
                    normalDisambiguations++;
                }
            }
            if (needsTopology) ambiguous.Add((ordinal, vertexId));
            candidateSets[vertexId] = original;
            chosen[ordinal] = candidates[0];
            if (candidates.Count > 1) multiple++;
            foreach (var candidate in candidates) snapMaximum = Math.Max(snapMaximum, source.Distance(candidate, px, py, pz));
        }
        if (ambiguous.Count > 0)
        {
            var incident = new Dictionary<int, List<int>>();
            for (var corner = 0; corner < triangles.Length; corner++)
            {
                if (!incident.TryGetValue(triangles[corner], out var list)) incident[triangles[corner]] = list = [];
                if (list.Count == 0 || list[^1] != corner / 3) list.Add(corner / 3);
            }
            foreach (var (ordinal, vertexId) in ambiguous)
            {
                var candidates = CornerCandidates(vertexId, candidateSets, triangles, incident.GetValueOrDefault(vertexId), source);
                if (!AllEffectiveEqual(source, [.. candidates])) throw new MorphGeometryError($"Authored triangle corners disagree about rendered vertex {vertexId}");
                chosen[ordinal] = candidates[0];
                topologyDisambiguations++;
            }
        }
        return (chosen, new()
        {
            ["vertices"] = ids.Length, ["position_snap_maximum"] = snapMaximum, ["uv_fallbacks"] = uvFallbacks, ["multiple_equivalent_candidates"] = multiple,
            ["normal_disambiguations"] = normalDisambiguations, ["skin_disambiguations"] = skinDisambiguations, ["topology_disambiguations"] = topologyDisambiguations
        });
    }

    private static Precision DeltaPrecision(MorphSource source, int[] selected)
    {
        var precision = new Precision();
        foreach (var row in selected)
        {
            for (var entry = source.Start[row]; entry < source.Start[row + 1]; entry++)
            {
                var at = entry * 6;
                double Error(int component) => Math.Abs(source.Changes[at + component] - source.Effective[at + component] / 4096.0);
                precision.Measure(Error(0), Error(1), Error(2), true);
                precision.Measure(Error(3), Error(4), Error(5), false);
            }
        }
        return precision;
    }

    /// <summary>Resolves every actually rendered LOD vertex against its exact bodygroup and material source.</summary>
    private static (List<MorphPlan> Plans, JsonObject Report) Provenance(CompiledVertices compiled, string qc, JsonObject appearance, PortContext? context)
    {
        var metadata = CompiledModelAudit.ReadCompiledModel(compiled.MdlPath);
        var replacements = CompiledRenderCoverage.ReadLodMaterialReplacementIds(compiled.VtxBytes, metadata.Materials);
        var boneNames = new Dictionary<string, int>(StringComparer.Ordinal);
        for (var index = 0; index < metadata.Bones.Count; index++) boneNames[metadata.Bones[index].Name] = index;
        var choices = SourceChoices(qc, appearance);
        var plans = new List<MorphPlan>();
        var precision = new Precision();
        var meshLods = new JsonArray();
        var cache = new Dictionary<string, Dictionary<string, MorphSource>>(StringComparer.OrdinalIgnoreCase);
        (int, int)? cachedModel = null;
        foreach (var mesh in compiled.Meshes)
        {
            context?.Check();
            if (cachedModel != (mesh.Part, mesh.Choice))
            {
                cache = new(StringComparer.OrdinalIgnoreCase);
                cachedModel = (mesh.Part, mesh.Choice);
            }
            var paths = choices[mesh.Part][mesh.Choice];
            if (!paths.Exists(IsDmx)) continue;
            var material = metadata.Skins[0][mesh.MaterialSlot];
            foreach (var (lod, ids) in mesh.LodUsed)
            {
                if (ids.Length == 0) continue;
                var path = paths[lod];
                if (!IsDmx(path)) throw new MorphGeometryError("Mixed SMD/DMX morph LOD sources require explicit corner provenance");
                if (!cache.TryGetValue(path!, out var streams)) cache[path!] = streams = SourceMesh(path!, boneNames);
                if (!streams.TryGetValue(material, out var source)) throw new MorphGeometryError($"Authored LOD source has no material {material}");
                int[] selected;
                JsonObject detail;
                try
                {
                    (selected, detail) = ResolveVertices(source, compiled, mesh.VertexBase, ids, mesh.LodTriangles[lod], lod, context);
                }
                catch (MorphGeometryError error)
                {
                    throw new MorphGeometryError($"{Path.GetFileName(path)}, {material}, mesh {CompiledText.Tuple(mesh.Part, mesh.Choice, mesh.Mesh)}, LOD {lod}: {error.Message}", error);
                }
                var bindings = new JsonArray();
                var runtime = new List<string>();
                for (var skin = 0; skin < metadata.Skins.Count; skin++)
                {
                    var binding = CompiledRenderCoverage.ResolveLodMaterial(metadata, replacements, lod, skin, mesh.MaterialSlot);
                    runtime.Add(binding.Get("runtime_material").Str());
                    bindings.Add(binding);
                }
                detail["part"] = mesh.Part;
                detail["choice"] = mesh.Choice;
                detail["mesh"] = mesh.Mesh;
                detail["lod"] = lod;
                detail["material"] = material;
                detail["compiler_material"] = material;
                detail["runtime_materials"] = CompiledText.Strings(runtime);
                detail["material_bindings"] = bindings;
                var deltaPrecision = DeltaPrecision(source, selected);
                detail["source_to_encoded_precision"] = deltaPrecision.ToJson();
                precision.Merge(deltaPrecision);
                meshLods.Add(detail);
                plans.Add(new(mesh.Key, lod, ids, source, selected));
            }
        }
        return (plans, new()
        {
            ["mesh_lods"] = meshLods, ["source_encoding"] = SourceEncoding, ["source_to_encoded_precision"] = precision.ToJson(), ["precision_scope"] = PrecisionScope,
            ["precision_weight_scaling"] = PrecisionWeightScaling, ["raw_source_morphs_retained_in_extraction"] = true
        });
    }

    private static bool IsDmx(string? path) => path is not null && PathText.Suffix(path).Equals(".dmx", StringComparison.OrdinalIgnoreCase);

    /// <summary>Reads complete named legacy vertex deltas without controller or descriptor inference.</summary>
    /// <remarks>Records of one descriptor add up. Inside a single record a repeated vertex does not: the last
    /// occurrence replaces the sum, which is how the engine's own loop over the same record behaves under v3's
    /// reading of it, so the sum is taken against the value from before the record.</remarks>
    private static CompiledFlexes ReadFlexes(CompiledVertices compiled, CompiledMesh mesh, List<string> names)
    {
        var data = compiled.MdlBytes;
        var count = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(mesh.MdlOffset + 16));
        var relative = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(mesh.MdlOffset + 20));
        var output = new Dictionary<string, Dictionary<int, int[]>>(StringComparer.Ordinal);
        for (var index = 0; index < count; index++)
        {
            var offset = mesh.MdlOffset + relative + index * 60;
            var header = data.AsSpan(offset, 60);
            var descriptor = BinaryPrimitives.ReadInt32LittleEndian(header);
            int length = BinaryPrimitives.ReadInt32LittleEndian(header[20..]), start = BinaryPrimitives.ReadInt32LittleEndian(header[24..]), pair = BinaryPrimitives.ReadInt32LittleEndian(header[28..]);
            var direct = BinaryPrimitives.ReadSingleLittleEndian(header[4..]) == 0f && BinaryPrimitives.ReadSingleLittleEndian(header[8..]) == 1f
                && BinaryPrimitives.ReadSingleLittleEndian(header[12..]) == 10f && BinaryPrimitives.ReadSingleLittleEndian(header[16..]) == 11f;
            if (header[32] != 0 || pair != 0 || descriptor < 0 || descriptor >= names.Count || !direct) throw new MorphGeometryError("Unsupported compiled stereo or nonlinear vertex animation record");
            var records = data.AsSpan(offset + start, length * RecordStride);
            for (var record = 0; record < length; record++)
                if (records[record * RecordStride + 2] != 255 || records[record * RecordStride + 3] != 255) throw new MorphGeometryError("Compiled morph uses unsupported delayed or stereo vertex weights");
            if (!output.TryGetValue(names[descriptor], out var values)) output[names[descriptor]] = values = [];
            for (var record = 0; record < length; record++)
                if (BinaryPrimitives.ReadUInt16LittleEndian(records[(record * RecordStride)..]) >= mesh.VertexCount) throw new MorphGeometryError("Compiled morph vertex index exceeds its material mesh");
            var summed = new int[length * 6];
            for (var record = 0; record < length; record++)
            {
                var existing = values.GetValueOrDefault(BinaryPrimitives.ReadUInt16LittleEndian(records[(record * RecordStride)..]));
                for (var component = 0; component < 6; component++)
                    summed[record * 6 + component] = (existing?[component] ?? 0) + Effective(BinaryPrimitives.ReadHalfLittleEndian(records[(record * RecordStride + 4 + component * 2)..]));
            }
            for (var record = 0; record < length; record++)
                values[BinaryPrimitives.ReadUInt16LittleEndian(records[(record * RecordStride)..])] = summed.AsSpan(record * 6, 6).ToArray();
        }
        var moved = new HashSet<int>();
        foreach (var (vertex, row) in output.Values.SelectMany(values => values))
            if (row.Any(component => component != 0)) moved.Add(vertex);
        return new(output, moved);
    }

    /// <summary>Compares every rendered descriptor component to its uniquely resolved authored value.</summary>
    private static JsonObject AuditPlans(CompiledVertices compiled, List<MorphPlan> plans, Dictionary<(int Part, int Choice, int Mesh, int Lod, int Id), int>? mapping, PortContext? context)
    {
        var errors = new JsonArray();
        long comparisons = 0;
        var maximum = 0;
        var meshes = compiled.Meshes.ToDictionary(mesh => mesh.Key);
        var names = CompiledModelAudit.ReadCompiledModel(compiled.MdlPath).FlexDescriptors;
        (int, int, int)? cachedKey = null;
        CompiledFlexes? cached = null;
        foreach (var plan in plans)
        {
            context?.Check();
            var key = plan.Key;
            var mesh = meshes[key];
            if (cachedKey != key)
            {
                cached = ReadFlexes(compiled, mesh, names);
                cachedKey = key;
            }
            var flexes = cached!;
            var ids = mapping is null ? plan.Ids : Array.ConvertAll(plan.Ids, index => mapping[(key.Part, key.Choice, key.Mesh, plan.Lod, index)]);
            var ordinals = new Dictionary<int, int>(ids.Length);
            for (var ordinal = 0; ordinal < ids.Length; ordinal++) ordinals[ids[ordinal]] = ordinal;
            var source = plan.Source;
            var targetIndices = new Dictionary<string, int>(StringComparer.Ordinal);
            for (var target = 0; target < source.Targets.Count; target++) targetIndices[source.Targets[target]] = target;
            var byTarget = new List<(int Ordinal, int Entry)>[source.Targets.Count];
            for (var ordinal = 0; ordinal < plan.Selected.Length; ordinal++)
            {
                var row = plan.Selected[ordinal];
                for (var entry = source.Start[row]; entry < source.ActiveEnd[row]; entry++) (byTarget[source.EntryTarget[entry]] ??= []).Add((ordinal, entry));
            }
            foreach (var name in targetIndices.Keys.Union(flexes.Names.Keys).Order(StringComparer.Ordinal))
            {
                var actual = flexes.Names.GetValueOrDefault(name);
                var expected = targetIndices.TryGetValue(name, out var target) ? byTarget[target] : null;
                var error = 0;
                var seen = new HashSet<int>();
                foreach (var (ordinal, entry) in expected ?? [])
                {
                    seen.Add(ordinal);
                    var row = actual?.GetValueOrDefault(ids[ordinal]);
                    for (var component = 0; component < 6; component++) error = Math.Max(error, Math.Abs((row?[component] ?? 0) - source.Effective[entry * 6 + component]));
                }
                foreach (var (vertex, row) in actual ?? [])
                {
                    if (!ordinals.TryGetValue(vertex, out var ordinal) || seen.Contains(ordinal)) continue;
                    foreach (var component in row) error = Math.Max(error, Math.Abs(component));
                }
                maximum = Math.Max(maximum, error);
                comparisons += ids.Length * 6L;
                if (error != 0) errors.Add(new JsonObject { ["mesh"] = CompiledText.Integers([key.Part, key.Choice, key.Mesh]), ["lod"] = plan.Lod, ["target"] = name, ["maximum_delta_error"] = error / 4096.0 });
            }
            foreach (var group in mesh.StripGroups)
            {
                if (group.Lod != plan.Lod || (group.Flags & 4) != 0) continue;
                if (group.OriginalIds.Any(flexes.Moved.Contains))
                    errors.Add(new JsonObject { ["mesh"] = CompiledText.Integers([key.Part, key.Choice, key.Mesh]), ["lod"] = plan.Lod, ["reason"] = "Rendered morph strip group lacks the VTX delta-flex flag" });
            }
        }
        return new()
        {
            ["ok"] = errors.Count == 0, ["feature_status"] = "adapted", ["errors"] = errors, ["component_comparisons"] = comparisons,
            ["maximum_effective_delta_error"] = maximum / 4096.0, ["weight_encoding"] = WeightEncoding
        };
    }

    private static JsonObject NotApplicable() => new() { ["ok"] = true, ["applicable"] = false, ["errors"] = new JsonArray() };

    private static JsonObject Merge(JsonObject target, JsonObject additions)
    {
        foreach (var (key, value) in additions) target[key] = value?.DeepClone();
        return target;
    }

    /// <summary>Rejects compiler morph loss even when controller, rule and descriptor counts match.</summary>
    public static JsonObject AuditCompiledMorphGeometry(string mdlPath, string qcPath, JsonObject appearance, PortContext? context = null)
    {
        if (!appearance.Get("flex_geometry").Bool()) return NotApplicable();
        var compiled = CompiledLodVertices.ReadCompiledVertices(mdlPath);
        var (plans, provenance) = Provenance(compiled, qcPath, appearance, context);
        return Merge(new() { ["applicable"] = true, ["provenance"] = provenance }, AuditPlans(compiled, plans, null, context));
    }

    /// <summary>Gathers each mesh's rendered morph entries by descriptor, in first-seen target order.</summary>
    private static Dictionary<(int Part, int Choice, int Mesh), Dictionary<string, List<(int Id, MorphSource Source, int Entry)>>> BuildChannels(List<MorphPlan> plans,
        Dictionary<(int Part, int Choice, int Mesh, int Lod, int Id), int> mapping, HashSet<string> descriptors)
    {
        var channels = new Dictionary<(int Part, int Choice, int Mesh), Dictionary<string, List<(int Id, MorphSource Source, int Entry)>>>();
        foreach (var plan in plans)
        {
            if (!channels.TryGetValue(plan.Key, out var meshChannels)) channels[plan.Key] = meshChannels = new(StringComparer.Ordinal);
            var streams = new List<(int, MorphSource, int)>[plan.Source.Targets.Count];
            for (var target = 0; target < streams.Length; target++)
            {
                var name = plan.Source.Targets[target];
                if (!descriptors.Contains(name)) throw new MorphGeometryError($"Authored morph descriptor is missing: {name}");
                if (!meshChannels.TryGetValue(name, out var stream)) meshChannels[name] = stream = [];
                streams[target] = stream;
            }
            for (var ordinal = 0; ordinal < plan.Ids.Length; ordinal++)
            {
                var id = mapping[(plan.Key.Part, plan.Key.Choice, plan.Key.Mesh, plan.Lod, plan.Ids[ordinal])];
                var row = plan.Selected[ordinal];
                for (var entry = plan.Source.Start[row]; entry < plan.Source.ActiveEnd[row]; entry++) streams[plan.Source.EntryTarget[entry]].Add((id, plan.Source, entry));
            }
        }
        return channels;
    }

    /// <summary>Serializes one descriptor's vertex animation records, ordered by vertex.</summary>
    /// <remarks>All six components of a moved vertex are written from the source's raw delta, so a component that
    /// rounds to nothing in the fixed-point value still keeps its authored half float.</remarks>
    private static byte[] SerializeRecords(List<(int Id, MorphSource Source, int Entry)> stream)
    {
        var ordered = stream.OrderBy(item => item.Id).ToList();
        if (stream.Count > MaximumFlexVertices) throw new MorphGeometryError("Repaired morph exceeds the supported Source renderer flex-vertex limit");
        for (var index = 1; index < ordered.Count; index++)
            if (ordered[index].Id == ordered[index - 1].Id) throw new MorphGeometryError("Repaired morph vertex IDs overlap or exceed the serialized index limit");
        if (ordered[^1].Id > 65535) throw new MorphGeometryError("Repaired morph vertex IDs overlap or exceed the serialized index limit");
        var payload = new byte[ordered.Count * RecordStride];
        for (var index = 0; index < ordered.Count; index++)
        {
            var (id, source, entry) = ordered[index];
            var record = payload.AsSpan(index * RecordStride, RecordStride);
            BinaryPrimitives.WriteUInt16LittleEndian(record, (ushort)id);
            record[2] = record[3] = 255;
            for (var component = 0; component < 6; component++) BinaryPrimitives.WriteHalfLittleEndian(record[(4 + component * 2)..], (Half)source.Changes[entry * 6 + component]);
        }
        return payload;
    }

    /// <summary>Appends a mesh's new flex table and vertex records, and points the mesh at them.</summary>
    private static void AppendRecords(MemoryStream data, CompiledMesh mesh, List<(int Descriptor, byte[] Payload, int Count)> records)
    {
        var start = (int)data.Length;
        data.SetLength(start + 60L * records.Count);
        data.Position = data.Length;
        for (var index = 0; index < records.Count; index++)
        {
            var (descriptor, payload, count) = records[index];
            var offset = start + index * 60;
            var header = data.GetBuffer().AsSpan(offset, 60);
            BinaryPrimitives.WriteInt32LittleEndian(header, descriptor);
            BinaryPrimitives.WriteSingleLittleEndian(header[4..], 0f);
            BinaryPrimitives.WriteSingleLittleEndian(header[8..], 1f);
            BinaryPrimitives.WriteSingleLittleEndian(header[12..], 10f);
            BinaryPrimitives.WriteSingleLittleEndian(header[16..], 11f);
            BinaryPrimitives.WriteInt32LittleEndian(header[20..], count);
            BinaryPrimitives.WriteInt32LittleEndian(header[24..], (int)data.Length - offset);
            data.Write(payload);
        }
        var head = data.GetBuffer().AsSpan(mesh.MdlOffset + 16, 8);
        BinaryPrimitives.WriteInt32LittleEndian(head, records.Count);
        BinaryPrimitives.WriteInt32LittleEndian(head[4..], start - mesh.MdlOffset);
    }

    /// <summary>Splits only proven LOD pools and installs exact authored deltas before atomically publishing files.</summary>
    /// <param name="mdlPath">The compiled model, holding what studiomdl wrote.</param>
    /// <param name="qcPath">The QC that produced it.</param>
    /// <param name="appearance">The authored appearance report.</param>
    /// <param name="backupDir">Where the compiler's original files are kept.</param>
    /// <param name="context">Cancellation and logging.</param>
    public static JsonObject RepairCompiledMorphGeometry(string mdlPath, string qcPath, JsonObject appearance, string? backupDir = null, PortContext? context = null)
    {
        if (!appearance.Get("flex_geometry").Bool()) return NotApplicable();
        var original = CompiledLodVertices.ReadCompiledVertices(mdlPath);
        if ((BinaryPrimitives.ReadUInt32LittleEndian(original.MdlBytes.AsSpan(152)) & FixedPointScaleFlag) != 0) throw new MorphGeometryError("Custom fixed-point morph encoding needs an explicit supported conversion");
        var (plans, provenance) = Provenance(original, qcPath, appearance, context);
        var existing = AuditPlans(original, plans, null, context);
        if (existing.Get("ok").Bool())
            return Merge(new() { ["applicable"] = true, ["status"] = "already_faithful", ["provenance"] = provenance, ["backups"] = new JsonArray() }, existing);
        var split = CompiledLodVertices.DuplicateLodVertices(mdlPath, plans.Select(plan => plan.Key).ToHashSet(), write: false);
        var names = CompiledModelAudit.ReadCompiledModel(mdlPath).FlexDescriptors;
        var descriptors = new Dictionary<string, int>(StringComparer.Ordinal);
        for (var index = 0; index < names.Count; index++) descriptors[names[index]] = index;
        var channels = BuildChannels(plans, split.Mapping, descriptors.Keys.ToHashSet(StringComparer.Ordinal));
        using var data = new MemoryStream(split.Compiled.MdlBytes.Length * 2);
        data.Write(split.Compiled.MdlBytes);
        foreach (var mesh in split.Compiled.Meshes)
        {
            context?.Check();
            if (!channels.TryGetValue(mesh.Key, out var meshChannels)) continue;
            var records = new List<(int Descriptor, byte[] Payload, int Count)>();
            foreach (var (name, stream) in meshChannels)
            {
                context?.Check();
                if (stream.Count > 0) records.Add((descriptors[name], SerializeRecords(stream), stream.Count));
            }
            AppendRecords(data, mesh, records);
        }
        BinaryPrimitives.WriteInt32LittleEndian(data.GetBuffer().AsSpan(76), (int)data.Length);
        var rewritten = split.Compiled with { MdlBytes = data.ToArray() };
        var audit = AuditPlans(rewritten, plans, split.Mapping, context);
        if (!audit.Get("ok").Bool()) throw new MorphGeometryError($"Rebuilt morph records failed exact output verification: {new JsonArray(audit.Get("errors").Arr().Take(3).Select(error => error?.DeepClone()).ToArray()).ToJsonString()}");
        context?.Check();
        var backups = CompiledLodVertices.WriteCompiledVertices(original, rewritten, backupDir);
        context?.Log($"Restored authored morph geometry across {plans.Count} material LOD meshes");
        return Merge(new() { ["applicable"] = true, ["provenance"] = provenance, ["vertex_split"] = split.Report, ["backups"] = CompiledText.Strings(backups) }, audit);
    }
}
