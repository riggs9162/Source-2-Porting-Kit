using System.Globalization;
using System.Runtime.InteropServices;
using System.Text;

namespace PortingKit.Addons.Soundscapes;

/// <summary>Plays several sound files at once through Windows' media control interface.</summary>
/// <remarks>The interface ties a sound to the thread that opened it, so every call must come from the UI thread.</remarks>
internal sealed class SoundPlayer : IDisposable
{
    private readonly List<string> open = [];
    private int next;

    [DllImport("winmm.dll", EntryPoint = "mciSendStringW", CharSet = CharSet.Unicode)]
    private static extern int Send(string command, StringBuilder? result, int capacity, IntPtr callback);

    /// <summary>Starts a file.</summary>
    /// <param name="volume">From 0 to 1.</param>
    /// <param name="repeat">Restarts the file whenever it ends.</param>
    /// <returns>False when Windows cannot play the file.</returns>
    public bool Play(string file, double volume, bool repeat)
    {
        CloseFinished();
        var alias = $"s2kit_scape_{++next}";
        if (Send($"open \"{file}\" type mpegvideo alias {alias}", null, 0, IntPtr.Zero) != 0) return false;
        Send($"setaudio {alias} volume to {((int)Math.Round(Math.Clamp(volume, 0, 1) * 1000)).ToString(CultureInfo.InvariantCulture)}", null, 0, IntPtr.Zero);
        if (Send($"play {alias}{(repeat ? " repeat" : "")}", null, 0, IntPtr.Zero) != 0)
        {
            Send($"close {alias}", null, 0, IntPtr.Zero);
            return false;
        }
        open.Add(alias);
        return true;
    }

    public void StopAll()
    {
        foreach (var alias in open) Send($"close {alias}", null, 0, IntPtr.Zero);
        open.Clear();
    }

    private void CloseFinished()
    {
        var status = new StringBuilder(64);
        for (var index = open.Count - 1; index >= 0; index--)
        {
            status.Clear();
            if (Send($"status {open[index]} mode", status, status.Capacity, IntPtr.Zero) == 0 && status.ToString() != "stopped") continue;
            Send($"close {open[index]}", null, 0, IntPtr.Zero);
            open.RemoveAt(index);
        }
    }

    public void Dispose() => StopAll();
}
