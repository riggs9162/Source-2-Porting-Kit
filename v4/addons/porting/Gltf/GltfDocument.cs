using System.Buffers.Binary;
using System.Text;
using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>A decoded accessor flattened to doubles, with one row per element.</summary>
public sealed record AccessorData(double[] Values, int Count, int Width)
{
    public double this[int row, int column] => Values[row * Width + column];
    public V3 Vector(int row) => new(Values[row * Width], Values[row * Width + 1], Values[row * Width + 2]);
    public Q4 Quaternion(int row) => new(Values[row * Width], Values[row * Width + 1], Values[row * Width + 2], Values[row * Width + 3]);
    public V2 Texcoord(int row) => new(Values[row * Width], Values[row * Width + 1]);
    public int[] Integers() => Array.ConvertAll(Values, value => (int)value);
}

/// <summary>A glTF or GLB model with its resolved first buffer.</summary>
public sealed class GltfDocument
{
    public const double SourceUnitsPerMetre = 1.0 / 0.0254;
    public const string SpaceSource = "source";
    public const string SpaceMetres = "metres";

    /// <summary>The 120 degree rotation Source 2 Viewer's basis applies, expressed for joint transforms.</summary>
    public static readonly Q4 Source2BasisQuaternion = new(0.5, 0.5, 0.5, 0.5);

    /// <summary>Undoes Source 2 Viewer's basis, a cyclic axis permutation with no mirroring.</summary>
    public static readonly M4 Source2Basis = new([0, 0, 1, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1]);

    private readonly JsonArray accessors;
    private readonly JsonArray views;

    private GltfDocument(JsonObject root, byte[] buffer, string sourcePath)
    {
        Root = root;
        Buffer = buffer;
        SourcePath = sourcePath;
        accessors = root.Get("accessors").Arr();
        views = root.Get("bufferViews").Arr();
    }

    public JsonObject Root { get; }
    public byte[] Buffer { get; }
    public string SourcePath { get; }
    public JsonArray Nodes => Root.Get("nodes").Arr();
    public JsonArray Meshes => Root.Get("meshes").Arr();
    public JsonArray Skins => Root.Get("skins").Arr();
    public JsonArray Materials => Root.Get("materials").Arr();

    /// <summary>Reads either supported container without assuming its filename extension.</summary>
    public static GltfDocument Read(string path)
    {
        var bytes = File.ReadAllBytes(path);
        if (bytes.Length >= 12 && BinaryPrimitives.ReadUInt32LittleEndian(bytes) == 0x46546C67) return ReadBinary(bytes, path);
        var root = Json.Parse(bytes);
        return new(root, ExternalBuffer(root, Path.GetDirectoryName(Path.GetFullPath(path)) ?? ""), path);
    }

    private static GltfDocument ReadBinary(byte[] raw, string path)
    {
        var length = (int)BinaryPrimitives.ReadUInt32LittleEndian(raw.AsSpan(12));
        var root = Json.Parse(raw.AsSpan(20, length));
        var entry = root.Get("buffers").Arr().FirstOrDefault();
        if (entry.Has("uri")) return new(root, ExternalBuffer(root, Path.GetDirectoryName(Path.GetFullPath(path)) ?? ""), path);
        var at = 12;
        while (at + 8 <= raw.Length)
        {
            var size = (int)BinaryPrimitives.ReadUInt32LittleEndian(raw.AsSpan(at));
            var type = BinaryPrimitives.ReadUInt32LittleEndian(raw.AsSpan(at + 4));
            if (type == 0x004E4942) return new(root, raw.AsSpan(at + 8, size).ToArray(), path);
            at += 8 + size;
        }
        throw new InvalidDataException($"could not resolve the binary buffer of {path}");
    }

    private static byte[] ExternalBuffer(JsonObject root, string directory)
    {
        var uri = root.Get("buffers").Arr().FirstOrDefault().Get("uri").Str();
        if (uri.Length == 0) throw new InvalidDataException("The model has no readable first buffer.");
        if (uri.StartsWith("data:", StringComparison.Ordinal))
        {
            var comma = uri.IndexOf(',');
            return comma < 0 ? throw new InvalidDataException("Invalid data URI buffer.") : Convert.FromBase64String(uri[(comma + 1)..]);
        }
        return File.ReadAllBytes(Path.Combine(directory, uri));
    }

    /// <summary>Whether any node still carries the inches-to-metres matrix, judged by basis-vector length.</summary>
    public bool HasSourceUnitMatrix()
    {
        foreach (var node in Nodes)
        {
            var matrix = node.Get("matrix").Doubles();
            if (matrix.Length == 0) continue;
            for (var start = 0; start < 12; start += 4)
            {
                var length = Math.Sqrt(matrix[start] * matrix[start] + matrix[start + 1] * matrix[start + 1] + matrix[start + 2] * matrix[start + 2]);
                if (length is > 0.020 and < 0.030) return true;
            }
        }
        return false;
    }

    /// <summary>Which space raw POSITION values use, measured per file rather than by exporter version.</summary>
    /// <remarks>
    /// <see cref="SpaceSource"/> means the 0.0254 matrix is still on a node, so positions are vmdl-space inches.
    /// <see cref="SpaceMetres"/> means Source 2 Viewer 20 baked the conversion into a render mesh. Physics
    /// meshes keep arriving the old way, so treating them alike compiles a model 39.37 times too small.
    /// </remarks>
    public string PositionSpace()
    {
        if (HasSourceUnitMatrix()) return SpaceSource;
        return Root.Get("asset").Get("generator").Str().Contains("Source 2 Viewer", StringComparison.Ordinal) ? SpaceMetres : "";
    }

    private static (int Size, double Limit, bool Signed) Component(int type) => type switch
    {
        5120 => (1, sbyte.MaxValue, true),
        5121 => (1, byte.MaxValue, false),
        5122 => (2, short.MaxValue, true),
        5123 => (2, ushort.MaxValue, false),
        5125 => (4, uint.MaxValue, false),
        5126 => (4, 0, true),
        _ => throw new InvalidDataException($"Unsupported glTF component type {type}.")
    };

    private static (int Columns, int Rows) Shape(string type) => type switch
    {
        "SCALAR" => (1, 1),
        "VEC2" => (1, 2),
        "VEC3" => (1, 3),
        "VEC4" => (1, 4),
        "MAT2" => (2, 2),
        "MAT3" => (3, 3),
        "MAT4" => (4, 4),
        _ => throw new InvalidDataException($"Unsupported glTF accessor type {type}.")
    };

    private double Read(int offset, int type) => type switch
    {
        5120 => (sbyte)Buffer[offset],
        5121 => Buffer[offset],
        5122 => BinaryPrimitives.ReadInt16LittleEndian(Buffer.AsSpan(offset)),
        5123 => BinaryPrimitives.ReadUInt16LittleEndian(Buffer.AsSpan(offset)),
        5125 => BinaryPrimitives.ReadUInt32LittleEndian(Buffer.AsSpan(offset)),
        _ => BinaryPrimitives.ReadSingleLittleEndian(Buffer.AsSpan(offset))
    };

    private double[] Stream(int viewIndex, int offset, int count, int type, int columns, int rows, bool tightlyPacked = false)
    {
        if (viewIndex < 0 || viewIndex >= views.Count) throw new InvalidDataException($"Invalid glTF buffer view {viewIndex}.");
        var view = views[viewIndex];
        if (view.Get("buffer").Int() != 0) throw new InvalidDataException("This model reader requires glTF buffer zero.");
        var itemSize = Component(type).Size;
        var columnSize = rows * itemSize;
        var columnStride = columns > 1 ? (columnSize + 3) / 4 * 4 : columnSize;
        var elementSize = columns * columnStride;
        var stride = tightlyPacked ? elementSize : view.Has("byteStride") ? view.Get("byteStride").Int() : elementSize;
        var viewStart = view.Get("byteOffset").Int();
        var viewLength = view.Get("byteLength").Int(-1);
        var required = count == 0 ? 0L : (long)(count - 1) * stride + elementSize;
        if (Math.Min(Math.Min(offset, count), Math.Min(viewStart, viewLength)) < 0 || stride < elementSize)
            throw new InvalidDataException("Invalid glTF accessor offset, count or stride.");
        if (offset + required > viewLength || (long)viewStart + viewLength > Buffer.Length)
            throw new InvalidDataException("glTF accessor extends beyond its buffer view.");
        var result = new double[count * columns * rows];
        var write = 0;
        for (var element = 0; element < count; element++)
            for (var column = 0; column < columns; column++)
                for (var row = 0; row < rows; row++)
                    result[write++] = Read(viewStart + offset + element * stride + column * columnStride + row * itemSize, type);
        return result;
    }

    /// <summary>Decodes a dense, sparse or normalized accessor into rows of doubles.</summary>
    public AccessorData Accessor(int index)
    {
        if (index < 0 || index >= accessors.Count) throw new InvalidDataException($"Invalid glTF accessor {index}.");
        var accessor = accessors[index];
        var type = accessor.Get("componentType").Int();
        var (columns, rows) = Shape(accessor.Get("type").Str());
        var (_, limit, signed) = Component(type);
        var count = accessor.Get("count").Int(-1);
        if (count < 0) throw new InvalidDataException("Negative glTF accessor count.");
        var width = columns * rows;
        double[] data;
        if (accessor.Has("bufferView")) data = Stream(accessor.Get("bufferView").Int(), accessor.Get("byteOffset").Int(), count, type, columns, rows);
        else if (accessor.Get("byteOffset").Int() != 0) throw new InvalidDataException("A glTF accessor without a buffer view cannot have an offset.");
        else data = new double[count * width];
        if (accessor.Get("sparse") is { } sparse)
        {
            var sparseCount = sparse.Get("count").Int();
            if (sparseCount < 0 || sparseCount > count) throw new InvalidDataException("Invalid sparse glTF accessor count.");
            var indices = sparse.Get("indices");
            var indexType = indices.Get("componentType").Int();
            if (indexType is not (5121 or 5123 or 5125)) throw new InvalidDataException("Sparse glTF indices must be unsigned integers.");
            var selection = Stream(indices.Get("bufferView").Int(), indices.Get("byteOffset").Int(), sparseCount, indexType, 1, 1, true);
            for (var i = 0; i < sparseCount; i++)
                if (selection[i] >= count || (i > 0 && selection[i] <= selection[i - 1]))
                    throw new InvalidDataException("Sparse glTF indices must increase within the accessor.");
            var values = sparse.Get("values");
            var overrides = Stream(values.Get("bufferView").Int(), values.Get("byteOffset").Int(), sparseCount, type, columns, rows, true);
            for (var i = 0; i < sparseCount; i++)
                Array.Copy(overrides, i * width, data, (long)selection[i] * width, width);
        }
        if (accessor.Get("normalized").Bool() && type != 5126)
            for (var i = 0; i < data.Length; i++)
                data[i] = signed ? Math.Max(data[i] / limit, -1.0) : data[i] / limit;
        return new(data, count, width);
    }

    /// <summary>Reads every chunk name the container declares; used by diagnostics.</summary>
    public string Describe() => new StringBuilder()
        .Append(Path.GetFileName(SourcePath)).Append(": ").Append(Meshes.Count).Append(" meshes, ")
        .Append(Skins.Count).Append(" skins, ").Append(Root.Get("animations").Arr().Count).Append(" animations").ToString();
}
