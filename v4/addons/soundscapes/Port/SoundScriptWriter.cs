using System.Globalization;
using System.Text;

namespace PortingKit.Addons.Soundscapes;

/// <summary>Writes sound events as Garry's Mod Lua.</summary>
/// <remarks>Each event becomes one <c>sound.Add</c> call under the event's own name, so anything that plays the event
/// by name in Source 2 can play it by the same name in Garry's Mod. A range is written as a two-value table, which the
/// game picks between on every play.</remarks>
public static class SoundScriptWriter
{
    public static string Write(IReadOnlyList<SoundDefinition> definitions)
    {
        var builder = new StringBuilder();
        builder.Append("--- Sound events ported from Source 2 by Source Backport, each registered under its original event name.\n");
        foreach (var definition in definitions)
        {
            builder.Append("\nsound.Add({\n");
            builder.Append("    name = ").Append(Quote(definition.Name)).Append(",\n");
            builder.Append("    channel = CHAN_STATIC,\n");
            builder.Append("    volume = ").Append(Range(Number(definition.VolumeLow), Number(definition.VolumeHigh))).Append(",\n");
            builder.Append("    level = ").Append(definition.Level.ToString(CultureInfo.InvariantCulture)).Append(",\n");
            builder.Append("    pitch = ").Append(Range(definition.PitchLow.ToString(CultureInfo.InvariantCulture), definition.PitchHigh.ToString(CultureInfo.InvariantCulture))).Append(",\n");
            if (definition.Waves.Count == 1) builder.Append("    sound = ").Append(Quote(definition.Waves[0])).Append(",\n");
            else
            {
                builder.Append("    sound = {\n");
                foreach (var wave in definition.Waves) builder.Append("        ").Append(Quote(wave)).Append(",\n");
                builder.Append("    },\n");
            }
            builder.Append("})\n");
        }
        return builder.ToString();
    }

    private static string Number(double value) => value.ToString("0.###", CultureInfo.InvariantCulture);

    private static string Range(string low, string high) => low == high ? low : $"{{{low}, {high}}}";

    private static string Quote(string text) => "\"" + text.Replace("\\", "\\\\").Replace("\"", "\\\"") + "\"";
}
