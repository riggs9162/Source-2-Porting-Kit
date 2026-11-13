namespace PortingKit.Addons.Soundscapes;

/// <summary>What happened to one sound.</summary>
/// <param name="Written">True when the WAV file was written.</param>
/// <param name="Looped">True when it carries loop markers.</param>
/// <param name="Problem">Why nothing was written, when nothing was.</param>
public sealed record SoundOutcome(bool Written, bool Looped, string? Problem = null);

/// <summary>Turns Source 2 sounds into the WAV files Source 1 plays.</summary>
/// <remarks>
/// <para>Half-Life: Alyx stores most sounds as MP3, and Source 1 only loops a WAV that carries a cue point. Every sound
/// is therefore written as 16-bit PCM. MP3 audio is decoded by ffmpeg and cut to the exact sample count the sound's
/// header gives, so a loop closes on the same sample it does in Source 2. Loop markers come from the header too.</para>
/// <para>Sounds stored as plain samples are wrapped in a WAV header without ffmpeg. Any other storage goes through
/// Source 2 Viewer first.</para>
/// </remarks>
public sealed class AudioPorter(GameSource source, IProcessRunner processes, Func<string> ffmpeg, Func<string> vrf, string scratch)
{
    private int counter;

    /// <summary>Ports one sound.</summary>
    /// <param name="path">The Source 2 path, such as <c>sounds/ambient/wind.vsnd</c>.</param>
    /// <param name="destination">The WAV file to write.</param>
    /// <param name="allowLoop">False for a sound only random rules use, which must stop on its own.</param>
    public async Task<SoundOutcome> PortAsync(string path, string destination, bool allowLoop, CancellationToken cancellationToken)
    {
        if (source.ReadSound(path) is not var (bytes, extension)) return new(false, false, "the source does not hold this sound");
        var resource = extension == ".vsnd_c" ? SoundResource.Read(bytes) : null;
        WaveAudio? audio;
        if (resource is { Encoding: SoundEncoding.Pcm16 or SoundEncoding.Pcm8 })
            audio = new(resource.SampleRate, resource.Channels, resource.Encoding == SoundEncoding.Pcm16 ? 16 : 8, resource.Audio.ToArray());
        else if (resource is { Encoding: SoundEncoding.Mp3 }) audio = await DecodeAsync(resource.Audio, ".mp3", cancellationToken);
        else if (extension == ".vsnd_c") audio = await DecodeAsync(await DecompileAsync(bytes, cancellationToken), cancellationToken);
        else audio = await DecodeAsync(bytes, extension, cancellationToken);
        if (audio is null) return new(false, false, "the sound could not be decoded");
        if (resource is not null) audio = audio.WithFrames(resource.SampleCount);
        var loop = allowLoop && resource is { Loops: true };
        WaveFile.Write(destination, audio, loop ? resource!.LoopStart : -1, loop ? resource!.LoopEnd : 0);
        return new(true, loop);
    }

    private string Scratch(string extension)
    {
        Directory.CreateDirectory(scratch);
        return Path.Combine(scratch, $"sound{Interlocked.Increment(ref counter)}{extension}");
    }

    private async Task<WaveAudio?> DecodeAsync(ReadOnlyMemory<byte> bytes, string extension, CancellationToken cancellationToken)
    {
        var input = Scratch(extension);
        await File.WriteAllBytesAsync(input, bytes, cancellationToken);
        return await DecodeAsync(input, cancellationToken);
    }

    private async Task<WaveAudio?> DecodeAsync(string? input, CancellationToken cancellationToken)
    {
        if (input is null) return null;
        try
        {
            if (Path.GetExtension(input).Equals(".wav", StringComparison.OrdinalIgnoreCase) && WaveFile.Read(input) is { BitsPerSample: 16 or 8 } ready) return ready;
            var output = Scratch(".wav");
            var outcome = await processes.RunAsync(new(ffmpeg(), ["-v", "error", "-y", "-i", input, "-vn", "-map_metadata", "-1", "-c:a", "pcm_s16le", "-f", "wav", output],
                Timeout: TimeSpan.FromMinutes(5)), null, cancellationToken);
            try
            {
                return outcome.ExitCode == 0 && File.Exists(output) ? WaveFile.Read(output) : null;
            }
            finally
            {
                File.Delete(output);
            }
        }
        finally
        {
            File.Delete(input);
        }
    }

    /// <summary>Has Source 2 Viewer decode a sound this add-on does not read itself.</summary>
    private async Task<string?> DecompileAsync(byte[] bytes, CancellationToken cancellationToken)
    {
        var folder = Path.Combine(scratch, $"viewer{Interlocked.Increment(ref counter)}");
        Directory.CreateDirectory(folder);
        var input = Path.Combine(folder, "sound.vsnd_c");
        await File.WriteAllBytesAsync(input, bytes, cancellationToken);
        var outcome = await processes.RunAsync(new(vrf(), ["-i", input, "-o", Path.Combine(folder, "out"), "-d"], Timeout: TimeSpan.FromMinutes(5)), null, cancellationToken);
        File.Delete(input);
        if (outcome.ExitCode != 0) return null;
        var decoded = Directory.EnumerateFiles(folder, "*", SearchOption.AllDirectories).FirstOrDefault();
        if (decoded is null) return null;
        var moved = Scratch(Path.GetExtension(decoded));
        File.Move(decoded, moved);
        Directory.Delete(folder, true);
        return moved;
    }

    /// <summary>Writes a sound in a form a media player opens, without converting it.</summary>
    /// <returns>The written file, or null when the sound is missing or needs a full port to be heard.</returns>
    public static string? WritePreview(GameSource source, string path, string folder)
    {
        if (source.ReadSound(path) is not var (bytes, extension)) return null;
        Directory.CreateDirectory(folder);
        var stem = Path.Combine(folder, Convert.ToHexString(System.Security.Cryptography.SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(source.Identity + "|" + path)))[..24]);
        if (extension != ".vsnd_c")
        {
            if (!File.Exists(stem + extension)) File.WriteAllBytes(stem + extension, bytes);
            return stem + extension;
        }
        var resource = SoundResource.Read(bytes);
        if (resource is { Encoding: SoundEncoding.Mp3 })
        {
            if (!File.Exists(stem + ".mp3")) File.WriteAllBytes(stem + ".mp3", resource.Audio.ToArray());
            return stem + ".mp3";
        }
        if (resource is not { Encoding: SoundEncoding.Pcm16 or SoundEncoding.Pcm8 }) return null;
        if (!File.Exists(stem + ".wav")) WaveFile.Write(stem + ".wav", new(resource.SampleRate, resource.Channels, resource.Encoding == SoundEncoding.Pcm16 ? 16 : 8, resource.Audio.ToArray()));
        return stem + ".wav";
    }
}
