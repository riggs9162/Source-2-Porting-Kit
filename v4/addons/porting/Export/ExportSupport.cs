using System.ComponentModel;
using System.Text;
using System.Text.Json;

namespace PortingKit.Addons.Porting;

/// <summary>A setup failure: a missing binary, a bad mode, or an input that does not exist.</summary>
public sealed class VrfRunnerException(string message, Exception? inner = null) : Exception(message, inner);

/// <summary>The Python path and text rules the export layout, sidecars and log lines depend on.</summary>
internal static class PathText
{
    /// <summary>A path spelled the way a Windows path object prints it.</summary>
    public static string Native(string path) => path.Replace('/', Path.DirectorySeparatorChar);

    public static string Posix(string path) => path.Replace('\\', '/');

    /// <summary>The last extension of a name; a leading or trailing dot is not one.</summary>
    public static string Suffix(string path)
    {
        var name = Path.GetFileName(path);
        var dot = name.LastIndexOf('.');
        return dot > 0 && dot < name.Length - 1 ? name[dot..] : "";
    }

    public static string Stem(string path)
    {
        var name = Path.GetFileName(path);
        return name[..(name.Length - Suffix(path).Length)];
    }

    /// <summary>Splits on every boundary Python's text splitter honours, which is more than newlines.</summary>
    /// <remarks>A section rebuilt from these lines is what <c>_phys.txt</c> holds, so a form feed inside a
    /// dump must end a line here exactly as it did in v3.</remarks>
    public static List<string> SplitLines(string text)
    {
        var lines = new List<string>();
        var start = 0;
        for (var index = 0; index < text.Length; index++)
        {
            var character = text[index];
            if (character is not ('\n' or '\r' or '\v' or '\f' or '\u001c' or '\u001d' or '\u001e' or '\u0085' or '\u2028' or '\u2029')) continue;
            lines.Add(text[start..index]);
            if (character == '\r' && index + 1 < text.Length && text[index + 1] == '\n') index++;
            start = index + 1;
        }
        if (start < text.Length) lines.Add(text[start..]);
        return lines;
    }

    /// <summary>Quotes one argument for display in a log line.</summary>
    public static string Quote(string argument) => argument.Contains(' ') ? $"\"{argument}\"" : argument;

    /// <summary>The command line as the user would type it, for the log.</summary>
    public static string Format(IEnumerable<string> arguments) => string.Join(' ', arguments.Select(Quote));

    /// <summary>The command line Windows receives, with its quoting, so its length can be measured.</summary>
    public static string CommandLine(IEnumerable<string> arguments)
    {
        var result = new StringBuilder();
        foreach (var argument in arguments)
        {
            if (result.Length > 0) result.Append(' ');
            var quoted = argument.Length == 0 || argument.Contains(' ') || argument.Contains('\t');
            if (quoted) result.Append('"');
            var slashes = 0;
            foreach (var character in argument)
            {
                if (character == '\\')
                {
                    slashes++;
                    continue;
                }
                if (character == '"') result.Append('\\', slashes * 2 + 1).Append('"');
                else result.Append('\\', slashes).Append(character);
                slashes = 0;
            }
            result.Append('\\', slashes);
            if (quoted) result.Append('\\', slashes).Append('"');
        }
        return result.ToString();
    }

    /// <summary>A value as Python's repr shows it inside an error message.</summary>
    public static string Repr(string value)
    {
        var quote = value.Contains('\'') && !value.Contains('"') ? '"' : '\'';
        var escaped = value.Replace("\\", "\\\\").Replace("\n", "\\n").Replace("\r", "\\r").Replace("\t", "\\t");
        return quote + (quote == '\'' ? escaped.Replace("'", "\\'") : escaped) + quote;
    }

    /// <summary>A sorted name list as Python prints it inside an error message.</summary>
    public static string Reprs(IEnumerable<string> values) => "[" + string.Join(", ", values.Order(StringComparer.Ordinal).Select(Repr)) + "]";
}

/// <summary>Sorts exceptions into the families v3's except clauses named.</summary>
internal static class ExportErrors
{
    /// <summary>A malformed document: the failure a stage records and moves past.</summary>
    public static bool IsData(Exception exception) => exception is InvalidDataException or NativeLodException or KeyNotFoundException or InvalidCastException
        or JsonException or FormatException or OverflowException or InvalidOperationException or ArgumentException or IndexOutOfRangeException;

    /// <summary>A file operation refused by the operating system.</summary>
    public static bool IsDisk(Exception exception) => exception is IOException or UnauthorizedAccessException;

    /// <summary>The program never started. A cancelled run is deliberately not this.</summary>
    public static bool IsStart(Exception exception) => exception is Win32Exception;

    public static string Start(string executable, Exception exception) => $"Could not start {Path.GetFileName(executable)}: {exception.Message}";
}
