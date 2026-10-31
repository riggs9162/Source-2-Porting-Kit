using System.Text;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>Text handling that keeps the exact on-disk result of the Python stages on Windows.</summary>
/// <remarks>Python's text mode writes CRLF and reads any line ending as a bare newline. QC files, overlay SMDs
/// and partitioned LOD SMDs were written that way; reference and animation SMDs were written with bare
/// newlines. Keeping that split means these files stay byte-identical to v3's, so anything reading them must
/// go through <see cref="Read"/> rather than splitting on a newline itself.</remarks>
internal static partial class PythonText
{
    [GeneratedRegex("\r\n|\r")]
    private static partial Regex LineEndings();

    [GeneratedRegex(@"\r\n|[\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029]")]
    private static partial Regex LineBreaks();

    /// <summary>Reads UTF-8 text with every line ending normalized to a newline.</summary>
    public static string Read(string path) => LineEndings().Replace(File.ReadAllText(path, new UTF8Encoding(false)), "\n");

    /// <summary>Reads a block dump that may start with a byte order mark.</summary>
    public static string ReadBomless(string path) => Read(path).TrimStart('\uFEFF');

    /// <summary>Writes UTF-8 text the way Python's default text mode does on Windows.</summary>
    public static void Write(string path, string text) => Fmt.WriteText(path, text.Replace("\n", "\r\n"));

    /// <summary>Splits like <c>str.splitlines</c>, which also breaks on form feeds and Unicode separators.</summary>
    public static List<string> SplitLines(string text)
    {
        var lines = LineBreaks().Split(text).ToList();
        if (lines.Count > 0 && lines[^1].Length == 0) lines.RemoveAt(lines.Count - 1);
        return lines;
    }

    /// <summary>A string as <c>repr</c> shows it, for messages that quote a name.</summary>
    public static string Repr(string value) => value.Contains('\'') && !value.Contains('"') ? $"\"{value}\"" : $"'{value.Replace("'", "\\'")}'";

    /// <summary>The part after the last slash of a POSIX-style resource path.</summary>
    public static string PosixName(string path) => path[(path.LastIndexOf('/') + 1)..];

    /// <summary>The folder of a POSIX-style resource path, or a dot when it has none.</summary>
    public static string PosixParent(string path)
    {
        var at = path.LastIndexOf('/');
        return at < 0 ? "." : at == 0 ? "/" : path[..at];
    }

    /// <summary>Tests a file name against a shell pattern the way <c>fnmatch</c> does.</summary>
    public static bool FnMatch(string name, string pattern)
    {
        var expression = new StringBuilder("^");
        for (var index = 0; index < pattern.Length; index++)
        {
            var character = pattern[index];
            switch (character)
            {
                case '*': expression.Append(".*"); break;
                case '?': expression.Append('.'); break;
                case '[':
                    var end = index + 1;
                    if (end < pattern.Length && pattern[end] == '!') end++;
                    if (end < pattern.Length && pattern[end] == ']') end++;
                    while (end < pattern.Length && pattern[end] != ']') end++;
                    if (end >= pattern.Length)
                    {
                        expression.Append(@"\[");
                        break;
                    }
                    var set = pattern[(index + 1)..end].Replace(@"\", @"\\");
                    expression.Append('[').Append(set.StartsWith('!') ? "^" + set[1..] : set).Append(']');
                    index = end;
                    break;
                default: expression.Append(Regex.Escape(character.ToString())); break;
            }
        }
        return Regex.IsMatch(name, expression.Append(@"\z").ToString(), RegexOptions.Singleline | RegexOptions.CultureInvariant);
    }
}
