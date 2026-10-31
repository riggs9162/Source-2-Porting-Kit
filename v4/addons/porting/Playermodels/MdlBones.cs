using System.Buffers.Binary;
using System.Text;

namespace PortingKit.Addons.Porting;

/// <summary>One bone of a compiled model, with its parent as an index and its bind relative to that parent.</summary>
public sealed record MdlBone(string Name, int Parent, V3 Position, Q4 Rotation);

/// <summary>One local <c>$poseparameter</c> of a compiled model.</summary>
public sealed record MdlPoseParam(string Name, int Flags, double Start, double End, double Loop);

/// <summary>One sequence of a compiled model with the activity it answers to.</summary>
public sealed record MdlSequence(string Label, string ActName, int ActWeight);

/// <summary>A minimal Source 1 model reader: bone table, local pose parameters and sequence activities.</summary>
/// <remarks>Reads v44 to v49 headers well enough to serve the player audit. Names decode as Latin-1, so a stray high byte
/// reads as a character instead of failing the audit.</remarks>
public static class MdlBones
{
    private const int VersionOffset = 0x04;
    private const int BoneCountOffset = 0x9C;
    private const int SequenceCountOffset = 0xBC;
    private const int PoseParamCountOffset = 0x12C;
    private const int BoneStride = 216;
    private const int PoseParamStride = 20;
    private const int SequenceStride = 212;

    private static int Int(byte[] data, int offset) => BinaryPrimitives.ReadInt32LittleEndian(data.AsSpan(offset, 4));
    private static double Float(byte[] data, int offset) => BinaryPrimitives.ReadSingleLittleEndian(data.AsSpan(offset, 4));

    private static string Text(byte[] data, int offset)
    {
        var end = Array.IndexOf(data, (byte)0, offset);
        if (end < 0) throw new InvalidDataException("subsection not found");
        return Encoding.Latin1.GetString(data, offset, end - offset);
    }

    /// <summary>Reads the bone table of a compiled model. Layouts from v44 on share it.</summary>
    public static List<MdlBone> ReadMdlBones(string path)
    {
        var data = File.ReadAllBytes(path);
        int count = Int(data, BoneCountOffset), table = Int(data, BoneCountOffset + 4);
        var bones = new List<MdlBone>(count);
        for (var index = 0; index < count; index++)
        {
            var offset = table + index * BoneStride;
            bones.Add(new(Text(data, offset + Int(data, offset)), Int(data, offset + 4),
                new(Float(data, offset + 32), Float(data, offset + 36), Float(data, offset + 40)),
                new(Float(data, offset + 44), Float(data, offset + 48), Float(data, offset + 52), Float(data, offset + 56))));
        }
        return bones;
    }

    /// <summary>Reads the local <c>$poseparameter</c> table of a compiled model.</summary>
    public static List<MdlPoseParam> ReadMdlPoseParams(string path)
    {
        var data = File.ReadAllBytes(path);
        int count = Int(data, PoseParamCountOffset), table = Int(data, PoseParamCountOffset + 4);
        var parameters = new List<MdlPoseParam>(count);
        for (var index = 0; index < count; index++)
        {
            var offset = table + index * PoseParamStride;
            parameters.Add(new(Text(data, offset + Int(data, offset)), Int(data, offset + 4), Float(data, offset + 8), Float(data, offset + 12), Float(data, offset + 16)));
        }
        return parameters;
    }

    /// <summary>The model format version, or null when the file is not a compiled model.</summary>
    public static int? MdlVersion(string path)
    {
        var data = File.ReadAllBytes(path);
        return data.AsSpan().StartsWith("IDST"u8) ? Int(data, VersionOffset) : null;
    }

    /// <summary>Reads every local sequence's label and activity.</summary>
    public static List<MdlSequence> ReadMdlSequences(string path)
    {
        var data = File.ReadAllBytes(path);
        if (!data.AsSpan().StartsWith("IDST"u8)) throw new InvalidDataException("Not a model!");
        var version = Int(data, VersionOffset);
        if (version is < 44 or > 49) throw new InvalidDataException($"Unknown MDL version {version}!");
        int count = Int(data, SequenceCountOffset), table = Int(data, SequenceCountOffset + 4);
        var sequences = new List<MdlSequence>(count);
        for (var index = 0; index < count; index++)
        {
            var offset = table + index * SequenceStride;
            sequences.Add(new(Text(data, offset + Int(data, offset + 4)), Text(data, offset + Int(data, offset + 8)), Int(data, offset + 20)));
        }
        return sequences;
    }
}
