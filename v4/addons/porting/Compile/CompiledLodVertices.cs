using System.Buffers.Binary;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>One rendered VTX primitive range and its strip-group vertex indices.</summary>
public sealed record CompiledStrip(int Offset, int Flags, int FirstIndex, int IndexCount, int FirstVertex, int VertexCount, ArraySegment<ushort> Indices);

/// <summary>One packed VTX strip group and its original mesh-local vertex references.</summary>
public sealed record CompiledStripGroup(int Lod, int Offset, int Flags, int[] VertexOffsets, int[] OriginalIds, ushort[] Indices, int IndexOffset, List<CompiledStrip> Strips);

/// <summary>A material mesh with logical root-zero VVD offsets and VTX LOD references.</summary>
public sealed class CompiledMesh(int part, int choice, int mesh, int mdlOffset, int modelOffset, int modelVertexBase, int meshVertexBase, int vertexCount, int materialSlot, uint[] lodCounts)
{
    public int Part { get; } = part;
    public int Choice { get; } = choice;
    public int Mesh { get; } = mesh;
    public int MdlOffset { get; } = mdlOffset;
    public int ModelOffset { get; } = modelOffset;
    public int ModelVertexBase { get; } = modelVertexBase;
    public int MeshVertexBase { get; } = meshVertexBase;
    public int VertexCount { get; } = vertexCount;
    public int MaterialSlot { get; } = materialSlot;
    public uint[] LodCounts { get; } = lodCounts;
    public Dictionary<int, int[]> LodUsed { get; } = [];
    public Dictionary<int, int[]> LodVertices { get; } = [];

    /// <summary>Mesh-local vertex ids per LOD, three per triangle.</summary>
    public Dictionary<int, int[]> LodTriangles { get; } = [];

    /// <summary>VTX vertex record offsets per LOD, three per triangle.</summary>
    public Dictionary<int, int[]> LodTriangleVertexOffsets { get; } = [];

    public List<CompiledStripGroup> StripGroups { get; } = [];

    /// <summary>The bodypart, model choice and material mesh identity.</summary>
    public (int Part, int Choice, int Mesh) Key => (Part, Choice, Mesh);

    /// <summary>This mesh's first logical root-zero VVD vertex.</summary>
    public int VertexBase => ModelVertexBase + MeshVertexBase;
}

/// <summary>An MDL bodygroup choice and its contiguous root-zero vertex range.</summary>
public sealed record CompiledBodyModel(int Part, int Choice, int MdlOffset, int VertexBase, int VertexCount)
{
    public List<CompiledMesh> Meshes { get; } = [];
}

/// <summary>Validated MDL, VVD and VTX bytes with root-zero structured vertex and tangent rows.</summary>
/// <remarks><see cref="Vertices"/> holds 48-byte rows of weights, bones, bone count, position, normal and UV.
/// <see cref="Tangents"/> holds 16-byte rows. Both are in logical order, after the VVD fixups.</remarks>
public sealed record CompiledVertices(string MdlPath, string VvdPath, string VtxPath, byte[] MdlBytes, byte[] VvdBytes, byte[] VtxBytes, byte[] Vertices, byte[] Tangents,
    List<CompiledBodyModel> Models, List<CompiledMesh> Meshes, int LodCount, uint Checksum, int StripGroupSize, int StripSize, int FixupCount)
{
    public const int VertexStride = 48;
    public const int TangentStride = 16;

    public int VertexCount => Vertices.Length / VertexStride;

    /// <summary>One float of a vertex row.</summary>
    public float Float(int row, int offset) => BinaryPrimitives.ReadSingleLittleEndian(Vertices.AsSpan(row * VertexStride + offset));

    /// <summary>One byte of a vertex row.</summary>
    public byte Byte(int row, int offset) => Vertices[row * VertexStride + offset];
}

/// <summary>Original provenance, isolated mesh-local IDs, output layout and measured costs.</summary>
public sealed record LodVertexSplitResult(CompiledVertices Original, CompiledVertices Compiled,
    Dictionary<(int Part, int Choice, int Mesh, int Lod, int Id), int> Mapping,
    Dictionary<(int Part, int Choice, int Mesh, int Id), List<(int Part, int Choice, int Mesh, int Lod, int Id)>> Reverse, JsonObject Report)
{
    /// <summary>The original logical root-zero vertex rows, for source correspondence.</summary>
    public byte[] Vertices => Original.Vertices;

    /// <summary>The rewritten mesh offsets and LOD references.</summary>
    public List<CompiledMesh> Meshes => Compiled.Meshes;

    /// <summary>The rewritten bodygroup model offsets.</summary>
    public List<CompiledBodyModel> Models => Compiled.Models;
}

/// <summary>Isolates compiled LOD vertex pools while preserving every vertex attribute byte.</summary>
/// <remarks>studiomdl shares vertices between LODs by a tolerance, so a lower LOD's vertex can be a LOD0 vertex that
/// was never authored for it, and the fixup table makes the sharing part of the file format. To give every LOD its
/// own morph data the pools have to be separated, which is what this class does. StudioMDL CE writes 33-byte strip
/// groups and 35-byte strips with extra topology fields; older tools wrote 25 and 27.</remarks>
public static class CompiledLodVertices
{
    private const int MaximumOffsetVertices = 0x7FFFFFFF / 64;

    /// <summary>Validates a complete nonnegative range before unpacking or slicing it.</summary>
    private static void Require(byte[] data, long offset, long size, string label)
    {
        if (offset < 0 || size < 0 || offset + size > data.Length) throw new CompiledVertexError($"{label}: range {offset}+{size} exceeds {data.Length} bytes");
    }

    private static int I32(byte[] data, long offset, string label)
    {
        Require(data, offset, 4, label);
        return BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan((int)offset));
    }

    private static uint U32(byte[] data, long offset, string label)
    {
        Require(data, offset, 4, label);
        return BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan((int)offset));
    }

    private static ushort U16(byte[] data, long offset, string label)
    {
        Require(data, offset, 2, label);
        return BinaryPrimitives.ReadUInt16LittleEndian(data.AsSpan((int)offset));
    }

    private static int[] I32s(byte[] data, long offset, int count, string label)
    {
        Require(data, offset, count * 4L, label);
        var values = new int[count];
        for (var index = 0; index < count; index++) values[index] = BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan((int)offset + index * 4));
        return values;
    }

    private static uint[] U32s(byte[] data, long offset, int count, string label)
    {
        Require(data, offset, count * 4L, label);
        var values = new uint[count];
        for (var index = 0; index < count; index++) values[index] = BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan((int)offset + index * 4));
        return values;
    }

    /// <summary>Validates a counted packed table and returns the offset of its first record.</summary>
    private static int Table(byte[] data, long count, long offset, int size, string label)
    {
        if (count < 0 || count > data.Length / Math.Max(size, 1)) throw new CompiledVertexError($"{label}: invalid count {count}");
        Require(data, offset, count * size, label);
        return (int)offset;
    }

    /// <summary>Copies a run of little-endian floats out of a packed row and checks that each is finite.</summary>
    private static bool Finite(ReadOnlySpan<byte> rows, int stride, int offset, int components)
    {
        for (var row = 0; row < rows.Length / stride; row++)
            for (var component = 0; component < components; component++)
                if (!float.IsFinite(BinaryPrimitives.ReadSingleLittleEndian(rows[(row * stride + offset + component * 4)..]))) return false;
        return true;
    }

    /// <summary>Reconstructs logical root-zero arrays using the file's ordered VVD fixups.</summary>
    private static (byte[] Vertices, byte[] Tangents, int LodCount, int FixupCount) VvdArrays(byte[] data, uint checksum)
    {
        Require(data, 0, 64, "VVD header");
        var version = U32(data, 4, "VVD header");
        var actualChecksum = U32(data, 8, "VVD header");
        var lodCount = U32(data, 12, "VVD header");
        if (!data.AsSpan(0, 4).SequenceEqual("IDSV"u8) || version != 4 || actualChecksum != checksum) throw new CompiledVertexError("Expected a checksum-matched version-four IDSV vertex file");
        if (lodCount is < 1 or > 8) throw new CompiledVertexError($"VVD has invalid LOD count {lodCount}");
        var counts = U32s(data, 16, 8, "VVD LOD counts");
        if (counts[0] == 0 || Enumerable.Range(1, 7).Any(index => counts[index] > counts[index - 1])) throw new CompiledVertexError("VVD LOD vertex counts are empty or increasing");
        var tables = U32s(data, 48, 4, "VVD tables");
        long fixupCount = tables[0], fixupOffset = tables[1], vertexOffset = tables[2], tangentOffset = tables[3];
        long root = counts[0];
        Require(data, vertexOffset, root * 48, "VVD vertex rows");
        Require(data, tangentOffset, root * 16, "VVD tangent rows");
        if (vertexOffset < 64 || tangentOffset < vertexOffset + root * 48) throw new CompiledVertexError("VVD vertex and tangent arrays overlap");
        var vertices = data.AsSpan((int)vertexOffset, (int)root * 48).ToArray();
        var tangents = data.AsSpan((int)tangentOffset, (int)root * 16).ToArray();
        if (fixupCount != 0)
        {
            var seen = new bool[root];
            var perLod = new long[8];
            var logical = new byte[vertices.Length];
            var logicalTangents = new byte[tangents.Length];
            long cursor = 0;
            var first = Table(data, fixupCount, fixupOffset, 12, "VVD fixups");
            var duplicated = false;
            for (long index = 0; index < fixupCount; index++)
            {
                var fixup = U32s(data, first + index * 12, 3, "VVD fixup");
                long lod = fixup[0], start = fixup[1], count = fixup[2];
                if (lod >= lodCount || start + count > root) throw new CompiledVertexError("VVD fixup references an absent LOD or vertex range");
                if (cursor + count <= root)
                {
                    data.AsSpan((int)(vertexOffset + start * 48), (int)count * 48).CopyTo(logical.AsSpan((int)cursor * 48));
                    data.AsSpan((int)(tangentOffset + start * 16), (int)count * 16).CopyTo(logicalTangents.AsSpan((int)cursor * 16));
                }
                for (var row = start; row < start + count; row++)
                {
                    duplicated |= seen[row];
                    seen[row] = true;
                }
                cursor += count;
                for (var level = 0; level <= lod; level++) perLod[level] += count;
            }
            if (cursor != root || duplicated) throw new CompiledVertexError("VVD root-zero fixups do not partition the original vertex rows");
            if (Enumerable.Range(0, (int)lodCount).Any(index => perLod[index] != counts[index])) throw new CompiledVertexError("VVD fixup counts disagree with the header");
            vertices = logical;
            tangents = logicalTangents;
        }
        if (!Finite(vertices, 48, 16, 3) || !Finite(vertices, 48, 28, 3)) throw new CompiledVertexError("VVD contains nonfinite positions or normals");
        if (!Finite(vertices, 48, 0, 3) || !Finite(vertices, 48, 40, 2) || !Finite(tangents, 16, 0, 4)) throw new CompiledVertexError("VVD contains nonfinite weights, UVs or tangents");
        for (var row = 0; row < root; row++)
        {
            var bad = vertices[row * 48 + 15] > 3;
            for (var weight = 0; weight < 3; weight++) bad |= BinaryPrimitives.ReadSingleLittleEndian(vertices.AsSpan(row * 48 + weight * 4)) < 0;
            if (bad) throw new CompiledVertexError("VVD contains invalid Source-one skin weights");
        }
        return (vertices, tangents, (int)lodCount, (int)fixupCount);
    }

    /// <summary>Reads packed v48 and v49 bodygroups, model ranges and 116-byte material meshes.</summary>
    private static (List<CompiledBodyModel> Models, List<CompiledMesh> Meshes, List<int> Choices) MdlModels(byte[] data, int vertexCount)
    {
        Require(data, 0, 240, "MDL header");
        if (!data.AsSpan(0, 4).SequenceEqual("IDST"u8) || U32(data, 4, "MDL version") is not (48 or 49)) throw new CompiledVertexError("Compiled vertex rewriting requires a v48/v49 IDST model");
        if (U32(data, 76, "MDL length") != data.Length) throw new CompiledVertexError("MDL declared length differs from its byte count");
        var header = U32s(data, 232, 2, "MDL bodyparts");
        List<CompiledBodyModel> models = [];
        List<CompiledMesh> meshes = [];
        List<int> choices = [];
        long cursor = 0;
        var partTable = Table(data, header[0], header[1], 16, "MDL bodyparts");
        for (var part = 0; part < header[0]; part++)
        {
            var partOffset = partTable + part * 16L;
            var modelCount = U32(data, partOffset + 4, "MDL choice count");
            var modelIndex = I32(data, partOffset + 12, "MDL model index");
            choices.Add((int)modelCount);
            var modelTable = Table(data, modelCount, partOffset + modelIndex, 148, "MDL models");
            for (var choice = 0; choice < modelCount; choice++)
            {
                var offset = modelTable + choice * 148;
                var fields = I32s(data, offset + 72L, 5, "MDL model");
                int meshCount = fields[0], meshIndex = fields[1], count = fields[2], vertexIndex = fields[3], tangentIndex = fields[4];
                if (count < 0 || vertexIndex < 0 || vertexIndex % 48 != 0 || tangentIndex < 0 || tangentIndex % 16 != 0) throw new CompiledVertexError("MDL model has invalid vertex or tangent offsets");
                var vertexBase = vertexIndex / 48;
                if (count != 0 && (vertexBase != cursor || tangentIndex / 16 != vertexBase)) throw new CompiledVertexError("MDL nonempty model vertex ranges are not contiguous root-zero rows");
                var model = new CompiledBodyModel(part, choice, offset, vertexBase, count);
                long meshCursor = 0;
                var meshTable = Table(data, meshCount, (long)offset + meshIndex, 116, "MDL meshes");
                for (var index = 0; index < meshCount; index++)
                {
                    var meshOffset = meshTable + index * 116;
                    var record = I32s(data, meshOffset, 4, "MDL mesh");
                    int material = record[0], back = record[1], meshVertices = record[2], meshBase = record[3];
                    if (back != offset - meshOffset || meshVertices < 0 || meshBase != meshCursor) throw new CompiledVertexError("MDL mesh backreference or contiguous vertex range is invalid");
                    var lodCounts = U32s(data, meshOffset + 52L, 8, "MDL mesh LOD counts");
                    if (lodCounts[0] != meshVertices || lodCounts.Any(value => value > meshVertices) || Enumerable.Range(1, 7).Any(lod => lodCounts[lod] > lodCounts[lod - 1]))
                        throw new CompiledVertexError("MDL mesh LOD counts exceed the mesh vertex range");
                    var mesh = new CompiledMesh(part, choice, index, meshOffset, offset, vertexBase, meshBase, meshVertices, material, lodCounts);
                    model.Meshes.Add(mesh);
                    meshes.Add(mesh);
                    meshCursor += meshVertices;
                }
                if (meshCursor != count || (long)vertexBase + count > vertexCount) throw new CompiledVertexError("MDL model and mesh vertex totals disagree with the VVD");
                cursor += count;
                models.Add(model);
            }
        }
        if (cursor != vertexCount) throw new CompiledVertexError("MDL mesh ranges do not account for every VVD vertex");
        return (models, meshes, choices);
    }

    private static int[] SortedDistinct(IEnumerable<int> values) => values.Distinct().Order().ToArray();

    /// <summary>Turns a strip's selected indices into triangles, in the winding the strip's order gives them.</summary>
    /// <remarks>Tristrips flip every other triangle and carry degenerates to bridge runs, and a triangle that
    /// repeats a vertex is one of those bridges, not geometry.</remarks>
    private static void AppendPrimitives(int flags, ReadOnlySpan<ushort> selected, int[] originalIds, int[] vertexOffsets, List<int> triangles, List<int> corners)
    {
        void Add(int a, int b, int c)
        {
            triangles.AddRange([originalIds[a], originalIds[b], originalIds[c]]);
            corners.AddRange([vertexOffsets[a], vertexOffsets[b], vertexOffsets[c]]);
        }
        if (flags == 1)
        {
            if (selected.Length % 3 != 0) throw new CompiledVertexError("VTX triangle-list strip has an incomplete triangle");
            for (var index = 0; index < selected.Length; index += 3) Add(selected[index], selected[index + 1], selected[index + 2]);
        }
        else if (flags == 2)
        {
            for (var index = 0; index < selected.Length - 2; index++)
            {
                int a = selected[index], b = selected[index + 1], c = selected[index + 2];
                if (index % 2 != 0) (a, b) = (b, a);
                if (a != b && b != c && a != c) Add(a, b, c);
            }
        }
        else throw new CompiledVertexError($"Unsupported VTX primitive flags {flags}");
    }

    /// <summary>Reads one mesh's strip groups at one LOD.</summary>
    private static void ReadMeshLod(byte[] data, CompiledMesh mesh, int lod, int meshOffset, int groupSize, int stripSize)
    {
        var header = I32s(data, meshOffset, 2, "VTX mesh");
        int groupCount = header[0];
        var groupTable = Table(data, groupCount, (long)meshOffset + header[1], groupSize, "VTX strip groups");
        List<int> allIds = [], triangles = [], corners = [];
        for (var group = 0; group < groupCount; group++)
        {
            var groupOffset = groupTable + group * groupSize;
            var fields = I32s(data, groupOffset, 6, "VTX strip group");
            int vertices = fields[0], vertexIndex = fields[1], indices = fields[2], indexIndex = fields[3], strips = fields[4], stripIndex = fields[5];
            int flags = data[groupOffset + 24];
            var vertexTable = Table(data, vertices, (long)groupOffset + vertexIndex, 9, "VTX vertices");
            var vertexOffsets = new int[vertices];
            var originalIds = new int[vertices];
            for (var vertex = 0; vertex < vertices; vertex++)
            {
                vertexOffsets[vertex] = vertexTable + vertex * 9;
                originalIds[vertex] = U16(data, vertexOffsets[vertex] + 4L, "VTX mesh vertex");
                if (originalIds[vertex] >= mesh.LodCounts[lod]) throw new CompiledVertexError("VTX vertex references an absent MDL mesh vertex");
            }
            Require(data, (long)groupOffset + indexIndex, indices * 2L, "VTX indices");
            var indexArray = MemoryMarshal.Cast<byte, ushort>(data.AsSpan(groupOffset + indexIndex, indices * 2)).ToArray();
            if (indices > 0 && indexArray.Max() >= vertices) throw new CompiledVertexError("VTX index references an absent strip-group vertex");
            var stripRecords = new List<CompiledStrip>(strips);
            var stripTable = Table(data, strips, (long)groupOffset + stripIndex, stripSize, "VTX strips");
            for (var strip = 0; strip < strips; strip++)
            {
                var offset = stripTable + strip * stripSize;
                var counts = I32s(data, offset, 4, "VTX strip");
                int stripIndices = counts[0], firstIndex = counts[1], stripVertices = counts[2], firstVertex = counts[3];
                if (counts.Min() < 0 || (long)firstIndex + stripIndices > indices || (long)firstVertex + stripVertices > vertices) throw new CompiledVertexError("VTX strip range exceeds its group");
                var changes = I32s(data, offset + 19L, 2, "VTX bone state changes");
                Table(data, changes[0], (long)offset + changes[1], 8, "VTX bone state changes");
                int stripFlags = data[offset + 18];
                var selected = new ArraySegment<ushort>(indexArray, firstIndex, stripIndices);
                AppendPrimitives(stripFlags, selected, originalIds, vertexOffsets, triangles, corners);
                stripRecords.Add(new(offset, stripFlags, firstIndex, stripIndices, firstVertex, stripVertices, selected));
            }
            if (groupSize == 33)
            {
                var topology = I32s(data, groupOffset + 25L, 2, "VTX topology");
                Table(data, topology[0], (long)groupOffset + topology[1], 2, "VTX topology indices");
            }
            mesh.StripGroups.Add(new(lod, groupOffset, flags, vertexOffsets, originalIds, indexArray, groupOffset + indexIndex, stripRecords));
            allIds.AddRange(originalIds);
        }
        mesh.LodVertices[lod] = SortedDistinct(allIds);
        mesh.LodUsed[lod] = SortedDistinct(triangles);
        mesh.LodTriangles[lod] = triangles.ToArray();
        mesh.LodTriangleVertexOffsets[lod] = corners.ToArray();
    }

    /// <summary>Reads packed VTX references using an explicitly selected strip-group layout.</summary>
    private static void ReadVtx(byte[] data, List<CompiledBodyModel> models, List<int> choices, uint checksum, int lodCount, int groupSize, int stripSize)
    {
        Require(data, 0, 36, "VTX header");
        var version = U32(data, 0, "VTX version");
        var header = U32s(data, 16, 5, "VTX header");
        if (version != 7 || header[0] != checksum || header[1] != lodCount || header[3] != choices.Count) throw new CompiledVertexError("VTX version, checksum, LOD or bodypart count differs from MDL/VVD");
        var byModel = models.ToDictionary(model => (model.Part, model.Choice));
        var partTable = Table(data, header[3], header[4], 8, "VTX bodyparts");
        for (var part = 0; part < choices.Count; part++)
        {
            var partOffset = partTable + part * 8;
            var partHeader = I32s(data, partOffset, 2, "VTX bodypart");
            if (partHeader[0] != choices[part]) throw new CompiledVertexError("VTX model choices differ from the MDL bodygroups");
            var modelTable = Table(data, partHeader[0], (long)partOffset + partHeader[1], 8, "VTX models");
            for (var choice = 0; choice < partHeader[0]; choice++)
            {
                var modelOffset = modelTable + choice * 8;
                var model = byModel[(part, choice)];
                var modelHeader = I32s(data, modelOffset, 2, "VTX model");
                if (modelHeader[0] != lodCount) throw new CompiledVertexError("VTX model has a different LOD count");
                var lodTable = Table(data, lodCount, (long)modelOffset + modelHeader[1], 12, "VTX LODs");
                for (var lod = 0; lod < lodCount; lod++)
                {
                    var lodOffset = lodTable + lod * 12;
                    var lodHeader = I32s(data, lodOffset, 2, "VTX LOD");
                    if (lodHeader[0] != model.Meshes.Count) throw new CompiledVertexError("VTX LOD mesh count differs from the MDL");
                    var meshTable = Table(data, lodHeader[0], (long)lodOffset + lodHeader[1], 9, "VTX meshes");
                    for (var index = 0; index < lodHeader[0]; index++) ReadMeshLod(data, model.Meshes[index], lod, meshTable + index * 9, groupSize, stripSize);
                }
            }
        }
    }

    /// <summary>Validates a coherent compiler-owned byte triplet without opening output files.</summary>
    private static CompiledVertices ReadBytes(string mdlPath, byte[] mdl, byte[] vvd, byte[] vtx, int stripGroupSize = 33, int stripSize = 35)
    {
        var checksum = U32(mdl, 8, "MDL checksum");
        var (vertices, tangents, lodCount, fixupCount) = VvdArrays(vvd, checksum);
        var (models, meshes, choices) = MdlModels(mdl, vertices.Length / 48);
        var counts = U32s(vvd, 16, 8, "VVD LOD counts");
        if (Enumerable.Range(0, 8).Any(lod => meshes.Sum(mesh => (long)mesh.LodCounts[lod]) != counts[lod])) throw new CompiledVertexError("MDL mesh LOD totals disagree with the VVD header");
        ReadVtx(vtx, models, choices, checksum, lodCount, stripGroupSize, stripSize);
        return new(mdlPath, Path.ChangeExtension(mdlPath, ".vvd"), Path.ChangeExtension(mdlPath, ".dx90.vtx"), mdl, vvd, vtx, vertices, tangents, models, meshes, lodCount, checksum, stripGroupSize, stripSize, fixupCount);
    }

    /// <summary>Reads v48 or v49 MDL, version-four VVD and CE's 33 and 35-byte extended DX90 VTX layout.</summary>
    public static CompiledVertices ReadCompiledVertices(string mdlPath, int stripGroupSize = 33, int stripSize = 35)
    {
        if ((stripGroupSize, stripSize) is not ((33, 35) or (25, 27))) throw new CompiledVertexError("Unsupported packed VTX strip-group/strip record sizes");
        var path = PathText.Native(mdlPath);
        return ReadBytes(path, File.ReadAllBytes(path), File.ReadAllBytes(Path.ChangeExtension(path, ".vvd")), File.ReadAllBytes(Path.ChangeExtension(path, ".dx90.vtx")), stripGroupSize, stripSize);
    }

    /// <summary>Preserves exact originals and replaces a fully validated triplet with rollback on failure.</summary>
    /// <remarks>Backups live in a folder named from the original bytes, so a second run keeps the first run's
    /// originals, and a backup that disagrees with what it should hold stops the write.</remarks>
    private static List<string> ReplaceFiles(CompiledVertices original, CompiledVertices rewritten, string? backupDir)
    {
        var pairs = new[] { (original.MdlPath, original.MdlBytes, rewritten.MdlBytes), (original.VvdPath, original.VvdBytes, rewritten.VvdBytes), (original.VtxPath, original.VtxBytes, rewritten.VtxBytes) };
        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        foreach (var (_, old, _) in pairs) hash.AppendData(old);
        var identity = Convert.ToHexStringLower(hash.GetHashAndReset())[..20];
        var directory = Path.Combine(backupDir ?? Path.Combine(CompiledText.Parent(original.MdlPath), ".s2kit-lod-originals"), identity);
        Directory.CreateDirectory(directory);
        var temporaries = new List<(string Path, string Temporary, byte[] Old)>();
        var replaced = new List<(string Path, byte[] Old)>();
        try
        {
            foreach (var (path, old, updated) in pairs)
            {
                if (!File.ReadAllBytes(path).AsSpan().SequenceEqual(old)) throw new CompiledVertexError($"Compiled component changed before replacement: {path}");
                var backup = Path.Combine(directory, Path.GetFileName(path));
                if (File.Exists(backup) && !File.ReadAllBytes(backup).AsSpan().SequenceEqual(old)) throw new CompiledVertexError($"Original byte backup conflicts with {backup}");
                if (!File.Exists(backup)) File.WriteAllBytes(backup, old);
                var temporary = path + ".s2kit-lod-tmp";
                File.WriteAllBytes(temporary, updated);
                temporaries.Add((path, temporary, old));
            }
            foreach (var (path, temporary, old) in temporaries)
            {
                File.Move(temporary, path, true);
                replaced.Add((path, old));
            }
        }
        catch (Exception)
        {
            for (var index = replaced.Count - 1; index >= 0; index--) File.WriteAllBytes(replaced[index].Path, replaced[index].Old);
            throw;
        }
        finally
        {
            foreach (var (_, temporary, _) in temporaries)
                if (File.Exists(temporary)) File.Delete(temporary);
        }
        return pairs.Select(pair => Path.Combine(directory, Path.GetFileName(pair.Item1))).ToList();
    }

    /// <summary>Validates a prepared triplet and replaces it once, retaining exact originals and rollback bytes.</summary>
    /// <returns>The backup path of each of the three files.</returns>
    public static List<string> WriteCompiledVertices(CompiledVertices original, CompiledVertices rewritten, string? backupDir = null)
    {
        if ((original.MdlPath, original.VvdPath, original.VtxPath) != (rewritten.MdlPath, rewritten.VvdPath, rewritten.VtxPath)) throw new CompiledVertexError("Original and rewritten compiled paths differ");
        var verified = ReadBytes(rewritten.MdlPath, rewritten.MdlBytes, rewritten.VvdBytes, rewritten.VtxBytes, rewritten.StripGroupSize, rewritten.StripSize);
        if (verified.Checksum != original.Checksum || verified.LodCount != original.LodCount) throw new CompiledVertexError("Prepared vertex output changed checksum or LOD identity");
        return ReplaceFiles(original, verified, backupDir);
    }

    private static void Pack(byte[] target, int offset, params int[] values)
    {
        for (var index = 0; index < values.Length; index++) BinaryPrimitives.WriteInt32LittleEndian(target.AsSpan(offset + index * 4), values[index]);
    }

    private static bool SameRows(byte[] a, int aBase, int[] aIds, byte[] b, int bBase, int[] bIds, int stride)
    {
        for (var index = 0; index < aIds.Length; index++)
            if (!a.AsSpan((aBase + aIds[index]) * stride, stride).SequenceEqual(b.AsSpan((bBase + bIds[index]) * stride, stride))) return false;
        return true;
    }

    /// <summary>Gives each lower LOD independent exact vertex copies and retains the union at every root LOD.</summary>
    /// <remarks>Original LOD-zero mesh IDs remain valid. Each selected mesh receives byte-identical copies for
    /// lower-LOD VTX references. Source flex restoration must replace its old mesh flex records before the rewritten
    /// model is used in the engine, because the old records still point at the shared vertices.</remarks>
    /// <param name="mdlPath">The compiled model.</param>
    /// <param name="flexedMeshes">The meshes to split, or null for all of them.</param>
    /// <param name="backupDir">Where the originals are kept when writing.</param>
    /// <param name="write">False prepares and verifies the result without touching the files.</param>
    public static LodVertexSplitResult DuplicateLodVertices(string mdlPath, IReadOnlySet<(int Part, int Choice, int Mesh)>? flexedMeshes = null, string? backupDir = null, bool write = true, int stripGroupSize = 33, int stripSize = 35)
    {
        var original = ReadCompiledVertices(mdlPath, stripGroupSize, stripSize);
        var selected = flexedMeshes ?? original.Meshes.Select(mesh => mesh.Key).ToHashSet();
        var absent = selected.Except(original.Meshes.Select(mesh => mesh.Key)).Order().ToList();
        if (absent.Count > 0) throw new CompiledVertexError($"Selected flex meshes are absent: [{string.Join(", ", absent.Select(key => CompiledText.Tuple(key.Part, key.Choice, key.Mesh)))}]");
        var mdl = (byte[])original.MdlBytes.Clone();
        var vtx = (byte[])original.VtxBytes.Clone();
        using var vertexRows = new MemoryStream();
        using var tangentRows = new MemoryStream();
        var mapping = new Dictionary<(int Part, int Choice, int Mesh, int Lod, int Id), int>();
        var reverse = new Dictionary<(int Part, int Choice, int Mesh, int Id), List<(int Part, int Choice, int Mesh, int Lod, int Id)>>();
        var meshReports = new JsonArray();
        var globalCursor = 0;
        foreach (var model in original.Models)
        {
            var modelBase = globalCursor;
            var meshCursor = 0;
            foreach (var mesh in model.Meshes)
            {
                var split = selected.Contains(mesh.Key);
                var localMaps = new Dictionary<int, int[]> { [0] = Enumerable.Range(0, mesh.VertexCount).ToArray() };
                var sourceRows = new List<int>(Enumerable.Range(0, mesh.VertexCount));
                for (var lod = 1; lod < original.LodCount; lod++)
                {
                    var ids = mesh.LodVertices[lod];
                    var map = new int[mesh.VertexCount];
                    if (split)
                    {
                        for (var ordinal = 0; ordinal < ids.Length; ordinal++) map[ids[ordinal]] = sourceRows.Count + ordinal;
                        sourceRows.AddRange(ids);
                    }
                    else foreach (var index in ids) map[index] = index;
                    localMaps[lod] = map;
                }
                if (sourceRows.Count > 65536) throw new CompiledVertexError($"Mesh {CompiledText.Tuple(mesh.Part, mesh.Choice, mesh.Mesh)} requires {sourceRows.Count} vertices, beyond unsigned 16-bit VTX IDs");
                foreach (var row in sourceRows)
                {
                    vertexRows.Write(original.Vertices, (mesh.VertexBase + row) * CompiledVertices.VertexStride, CompiledVertices.VertexStride);
                    tangentRows.Write(original.Tangents, (mesh.VertexBase + row) * CompiledVertices.TangentStride, CompiledVertices.TangentStride);
                }
                Pack(mdl, mesh.MdlOffset + 8, sourceRows.Count, meshCursor);
                Pack(mdl, mesh.MdlOffset + 52, Enumerable.Repeat(sourceRows.Count, 8).ToArray());
                foreach (var (lod, ids) in mesh.LodVertices)
                {
                    foreach (var oldId in ids)
                    {
                        var newId = localMaps[lod][oldId];
                        var key = (mesh.Part, mesh.Choice, mesh.Mesh, lod, oldId);
                        mapping[key] = newId;
                        var reverseKey = (mesh.Part, mesh.Choice, mesh.Mesh, newId);
                        if (!reverse.TryGetValue(reverseKey, out var list)) reverse[reverseKey] = list = [];
                        list.Add(key);
                    }
                }
                foreach (var group in mesh.StripGroups)
                {
                    if (split) vtx[group.Offset + 24] = (byte)(group.Flags | 4);
                    for (var index = 0; index < group.VertexOffsets.Length; index++)
                        BinaryPrimitives.WriteUInt16LittleEndian(vtx.AsSpan(group.VertexOffsets[index] + 4), (ushort)localMaps[group.Lod][group.OriginalIds[index]]);
                }
                var lodUsed = new JsonObject();
                foreach (var (lod, ids) in mesh.LodUsed) lodUsed[lod.ToString(System.Globalization.CultureInfo.InvariantCulture)] = ids.Length;
                meshReports.Add(new JsonObject
                {
                    ["mesh"] = CompiledText.Integers([mesh.Part, mesh.Choice, mesh.Mesh]), ["original_vertices"] = mesh.VertexCount, ["vertices"] = sourceRows.Count,
                    ["added_vertices"] = sourceRows.Count - mesh.VertexCount, ["split"] = split, ["global_vertex_base"] = globalCursor,
                    ["model_vertex_base"] = modelBase, ["mesh_vertex_base"] = meshCursor, ["lod_used"] = lodUsed
                });
                globalCursor += sourceRows.Count;
                meshCursor += sourceRows.Count;
            }
            Pack(mdl, model.MdlOffset + 80, meshCursor, modelBase * 48, modelBase * 16);
        }
        if (globalCursor > MaximumOffsetVertices) throw new CompiledVertexError("Rewritten VVD exceeds signed Source-one file offsets");
        var vvd = new byte[64 + vertexRows.Length + tangentRows.Length];
        original.VvdBytes.AsSpan(0, 64).CopyTo(vvd);
        for (var index = 0; index < 8; index++) BinaryPrimitives.WriteUInt32LittleEndian(vvd.AsSpan(16 + index * 4), (uint)globalCursor);
        Pack(vvd, 48, 0, 64, 64, 64 + (int)vertexRows.Length);
        vertexRows.GetBuffer().AsSpan(0, (int)vertexRows.Length).CopyTo(vvd.AsSpan(64));
        tangentRows.GetBuffer().AsSpan(0, (int)tangentRows.Length).CopyTo(vvd.AsSpan(64 + (int)vertexRows.Length));
        var compiled = ReadBytes(original.MdlPath, mdl, vvd, vtx, stripGroupSize, stripSize);
        var verified = 0;
        foreach (var (oldMesh, newMesh) in original.Meshes.Zip(compiled.Meshes))
        {
            foreach (var (oldGroup, newGroup) in oldMesh.StripGroups.Zip(newMesh.StripGroups))
            {
                if (oldGroup.Lod != newGroup.Lod || !oldGroup.Indices.AsSpan().SequenceEqual(newGroup.Indices)) throw new CompiledVertexError("VTX strip topology changed during vertex isolation");
                if (!SameRows(original.Vertices, oldMesh.VertexBase, oldGroup.OriginalIds, compiled.Vertices, newMesh.VertexBase, newGroup.OriginalIds, CompiledVertices.VertexStride))
                    throw new CompiledVertexError("VTX-referenced vertex attributes changed during isolation");
                if (!SameRows(original.Tangents, oldMesh.VertexBase, oldGroup.OriginalIds, compiled.Tangents, newMesh.VertexBase, newGroup.OriginalIds, CompiledVertices.TangentStride))
                    throw new CompiledVertexError("VTX-referenced tangent attributes changed during isolation");
                verified += oldGroup.OriginalIds.Length;
            }
        }
        var report = new JsonObject
        {
            ["status"] = "isolated", ["method"] = "independent_lod_vertex_pools_with_constant_root_lod_union", ["original_vertices"] = original.VertexCount, ["vertices"] = globalCursor,
            ["added_vertices"] = globalCursor - original.VertexCount, ["added_vertex_buffer_bytes"] = (globalCursor - original.VertexCount) * 64L, ["lods"] = original.LodCount,
            ["original_fixups"] = original.FixupCount, ["fixups"] = 0, ["checked_strip_vertex_records"] = verified, ["vertex_attributes_byte_identical"] = true,
            ["tangent_attributes_byte_identical"] = true, ["triangle_indices_byte_identical"] = true, ["meshes"] = meshReports, ["originals"] = new JsonArray()
        };
        if (write) report["originals"] = CompiledText.Strings(WriteCompiledVertices(original, compiled, backupDir));
        return new(original, compiled, mapping, reverse, report);
    }
}
