using System.Buffers.Binary;

namespace PortingKit.Addons.Soundscapes;

public enum SoundEncoding { Pcm16, Pcm8, Mp3, Adpcm, Unknown }

/// <summary>A compiled Source 2 sound: its header and the audio stored after it.</summary>
/// <param name="Encoding">How the audio is stored.</param>
/// <param name="SampleRate">Samples per second.</param>
/// <param name="Channels">Channel count.</param>
/// <param name="LoopStart">The sample a loop returns to, or -1 when the sound plays once.</param>
/// <param name="LoopEnd">The last sample of the loop.</param>
/// <param name="SampleCount">Samples per channel in the decoded sound.</param>
/// <param name="Audio">The stored audio: a whole MP3 file, or raw samples.</param>
public sealed record SoundResource(SoundEncoding Encoding, int SampleRate, int Channels, int LoopStart, int LoopEnd, int SampleCount, ReadOnlyMemory<byte> Audio)
{
    private const int SupportedVersion = 4;
    private const int HeaderLength = 48;

    public bool Loops => LoopStart >= 0;

    /// <summary>Reads a <c>.vsnd_c</c> file.</summary>
    /// <remarks>The layout is the version 4 sound header Half-Life: Alyx ships: rate, encoding and channels, then the
    /// loop start, the sample count and the length of the audio, with the loop end in the last four bytes. The audio
    /// itself follows the resource's blocks.</remarks>
    /// <returns>Null when the file is another sound version, which the caller hands to Source 2 Viewer instead.</returns>
    public static SoundResource? Read(byte[] file)
    {
        if (file.Length < 16) return null;
        var span = file.AsSpan();
        var streamStart = BinaryPrimitives.ReadInt32LittleEndian(span);
        if (BinaryPrimitives.ReadUInt16LittleEndian(span[6..]) != SupportedVersion) return null;
        var blocks = 8 + BinaryPrimitives.ReadInt32LittleEndian(span[8..]);
        var count = BinaryPrimitives.ReadInt32LittleEndian(span[12..]);
        for (var index = 0; index < count; index++)
        {
            var at = blocks + index * 12;
            if (at + 12 > file.Length) return null;
            if (!span.Slice(at, 4).SequenceEqual("DATA"u8)) continue;
            var offset = at + 4 + BinaryPrimitives.ReadInt32LittleEndian(span[(at + 4)..]);
            var size = BinaryPrimitives.ReadInt32LittleEndian(span[(at + 8)..]);
            if (size < HeaderLength || offset + size > file.Length) return null;
            var data = span.Slice(offset, size);
            var encoding = data[2] switch { 0 => SoundEncoding.Pcm16, 1 => SoundEncoding.Pcm8, 2 => SoundEncoding.Mp3, 3 => SoundEncoding.Adpcm, _ => SoundEncoding.Unknown };
            var length = BinaryPrimitives.ReadInt32LittleEndian(data[28..]);
            if (streamStart < 0 || length < 0 || (long)streamStart + length > file.Length) return null;
            return new(encoding, BinaryPrimitives.ReadUInt16LittleEndian(data), data[3], BinaryPrimitives.ReadInt32LittleEndian(data[4..]),
                BinaryPrimitives.ReadInt32LittleEndian(data[44..]), BinaryPrimitives.ReadInt32LittleEndian(data[8..]), file.AsMemory(streamStart, length));
        }
        return null;
    }
}
