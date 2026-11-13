using System.Buffers.Binary;
using System.Text;

namespace PortingKit.Addons.Soundscapes;

/// <summary>Uncompressed audio with the format a WAV header describes.</summary>
public sealed record WaveAudio(int SampleRate, int Channels, int BitsPerSample, byte[] Samples)
{
    public int FrameLength => Channels * BitsPerSample / 8;

    public int Frames => FrameLength == 0 ? 0 : Samples.Length / FrameLength;

    /// <summary>Cuts or pads the audio to an exact number of frames.</summary>
    public WaveAudio WithFrames(int frames)
    {
        if (frames <= 0 || frames == Frames) return this;
        var samples = new byte[frames * FrameLength];
        Samples.AsSpan(0, Math.Min(Samples.Length, samples.Length)).CopyTo(samples);
        if (BitsPerSample == 8) samples.AsSpan(Math.Min(Samples.Length, samples.Length)).Fill(0x80);
        return this with { Samples = samples };
    }
}

/// <summary>Reads and writes PCM WAV files, with the loop markers Source 1 needs.</summary>
public static class WaveFile
{
    private const ushort PcmFormat = 1;
    private const ushort ExtensibleFormat = 0xFFFE;

    /// <summary>Reads a PCM WAV file.</summary>
    /// <returns>Null when the file is not uncompressed PCM.</returns>
    public static WaveAudio? Read(string path)
    {
        var file = File.ReadAllBytes(path);
        var span = file.AsSpan();
        if (file.Length < 12 || !span[..4].SequenceEqual("RIFF"u8) || !span.Slice(8, 4).SequenceEqual("WAVE"u8)) return null;
        int rate = 0, channels = 0, bits = 0;
        var position = 12;
        while (position + 8 <= file.Length)
        {
            var size = (long)BinaryPrimitives.ReadUInt32LittleEndian(span[(position + 4)..]);
            var body = position + 8;
            if (span.Slice(position, 4).SequenceEqual("fmt "u8) && body + 16 <= file.Length)
            {
                var format = BinaryPrimitives.ReadUInt16LittleEndian(span[body..]);
                if (format is not (PcmFormat or ExtensibleFormat)) return null;
                channels = BinaryPrimitives.ReadUInt16LittleEndian(span[(body + 2)..]);
                rate = BinaryPrimitives.ReadInt32LittleEndian(span[(body + 4)..]);
                bits = BinaryPrimitives.ReadUInt16LittleEndian(span[(body + 14)..]);
            }
            else if (span.Slice(position, 4).SequenceEqual("data"u8))
            {
                if (channels == 0) return null;
                var length = (int)Math.Min(size, file.Length - body);
                return new(rate, channels, bits, span.Slice(body, length).ToArray());
            }
            position = (int)Math.Min(int.MaxValue, body + size + (size & 1));
        }
        return null;
    }

    /// <summary>Writes a PCM WAV file, with a loop when one is given.</summary>
    /// <remarks>Source 1 loops a sound only when its file carries a cue point, and reads the loop's end from a sampler
    /// chunk. Both are written, from the loop's exact sample positions.</remarks>
    /// <param name="loopStart">First sample of the loop, or a negative number for a sound that plays once.</param>
    /// <param name="loopEnd">Last sample of the loop. A value that is not after the start loops to the end of the file.</param>
    public static void Write(string path, WaveAudio audio, int loopStart = -1, int loopEnd = 0)
    {
        var loops = loopStart >= 0 && loopStart < audio.Frames;
        if (loopEnd <= loopStart || loopEnd >= audio.Frames) loopEnd = audio.Frames - 1;
        var padding = audio.Samples.Length & 1;
        var extra = loops ? 8 + 28 + 8 + 60 : 0;
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
        using var stream = File.Create(path);
        using var writer = new BinaryWriter(stream, Encoding.ASCII);
        writer.Write("RIFF"u8);
        writer.Write(4 + 8 + 16 + 8 + audio.Samples.Length + padding + extra);
        writer.Write("WAVE"u8);
        writer.Write("fmt "u8);
        writer.Write(16);
        writer.Write(PcmFormat);
        writer.Write((ushort)audio.Channels);
        writer.Write(audio.SampleRate);
        writer.Write(audio.SampleRate * audio.FrameLength);
        writer.Write((ushort)audio.FrameLength);
        writer.Write((ushort)audio.BitsPerSample);
        writer.Write("data"u8);
        writer.Write(audio.Samples.Length);
        writer.Write(audio.Samples);
        if (padding == 1) writer.Write((byte)0);
        if (!loops) return;
        writer.Write("cue "u8);
        writer.Write(28);
        writer.Write(1);
        writer.Write(1);
        writer.Write(loopStart);
        writer.Write("data"u8);
        writer.Write(0);
        writer.Write(0);
        writer.Write(loopStart);
        writer.Write("smpl"u8);
        writer.Write(60);
        writer.Write(0);
        writer.Write(0);
        writer.Write(audio.SampleRate > 0 ? 1_000_000_000 / audio.SampleRate : 0);
        writer.Write(60);
        writer.Write(0);
        writer.Write(0);
        writer.Write(0);
        writer.Write(1);
        writer.Write(0);
        writer.Write(1);
        writer.Write(0);
        writer.Write(loopStart);
        writer.Write(loopEnd);
        writer.Write(0);
        writer.Write(0);
    }
}
