using System.Security.Cryptography;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>Validates game-relative resource paths before they reach an output tree or a QC.</summary>
public static partial class Resources
{
    private const string NamespaceRule = "Use lowercase letters, digits and underscores, with / or \\ between folders, for example my_addon/props";

    [GeneratedRegex(@"^[a-z0-9_]+(?:/[a-z0-9_]+)*$")]
    private static partial Regex NamespacePattern();

    /// <summary>Normalizes a relative path the way a POSIX path object does, or returns null when it escapes.</summary>
    private static string? Normalize(string value)
    {
        if (value.StartsWith('/') || value.Contains(':')) return null;
        var parts = value.Split('/').Where(part => part.Length > 0 && part != ".").ToArray();
        return parts.Contains("..") || parts.Length == 0 ? null : string.Join('/', parts);
    }

    /// <summary>Normalizes a material reference without collapsing its directory identity.</summary>
    public static string Token(string value)
    {
        var text = value.Replace('\\', '/').Trim().ToLowerInvariant();
        if (text.StartsWith("materials/", StringComparison.Ordinal)) text = text["materials/".Length..];
        foreach (var suffix in new[] { ".vmat_c", ".vmat", ".vmt" })
        {
            if (!text.EndsWith(suffix, StringComparison.Ordinal)) continue;
            text = text[..^suffix.Length];
            break;
        }
        return (text.Contains('"') ? null : Normalize(text)) ?? throw new InvalidDataException($"Unsafe material resource path: {value}");
    }

    /// <summary>Drops a material's own models root, because the addon's models folder already starts the path.</summary>
    public static string AddonMaterial(string value)
    {
        var token = Token(value);
        return token.StartsWith("models/", StringComparison.Ordinal) ? token["models/".Length..] : token;
    }

    /// <summary>Validates a relative game path before it is used beneath an output root.</summary>
    public static string Relative(string value)
    {
        var text = value.Replace('\\', '/');
        return Normalize(text) ?? throw new InvalidDataException($"Unsafe source resource path: {value}");
    }

    /// <summary>Accepts a namespace as typed on Windows: backslashes, capitals, spaces and stray separators.</summary>
    public static string NormalizeNamespace(string value)
    {
        var parts = value.Replace('\\', '/').Split('/').Select(part => Regex.Replace(part.Trim(), @"\s+", "_")).Where(part => part.Length > 0);
        return string.Join('/', parts).ToLowerInvariant();
    }

    /// <summary>Places a resource folder beneath a namespace, using any folder they share only once.</summary>
    /// <remarks>Someone porting <c>materials/tiles</c> naturally names the namespace <c>my_addon/tiles</c>, and joining
    /// the two blindly would give <c>my_addon/tiles/tiles</c>. The longest overlap between the end of the namespace
    /// and the start of the folder is collapsed. Model materials must not use this: a compiled model looks them up at
    /// its exact <c>$cdmaterials</c> path.</remarks>
    public static string UnderNamespace(string space, string folder)
    {
        var parts = space.Split('/');
        var rest = folder.Split('/', StringSplitOptions.RemoveEmptyEntries);
        for (var overlap = Math.Min(parts.Length, rest.Length); overlap > 0; overlap--)
            if (parts.TakeLast(overlap).SequenceEqual(rest.Take(overlap), StringComparer.OrdinalIgnoreCase))
                return string.Join('/', parts.Concat(rest.Skip(overlap)));
        return string.Join('/', parts.Concat(rest));
    }

    /// <summary>Explains why a normalized namespace cannot be used, or returns an empty string.</summary>
    public static string NamespaceProblem(string value)
    {
        if (NamespacePattern().IsMatch(value)) return "";
        var invalid = value.Where(c => !(char.IsAsciiLetterLower(c) || char.IsAsciiDigit(c) || c is '_' or '/')).Distinct().Order().ToArray();
        return invalid.Length > 0 ? $"{NamespaceRule}. Remove: {string.Join(' ', invalid)}" : NamespaceRule;
    }
}

/// <summary>Hashes large assets without loading them into memory.</summary>
public static class Digest
{
    public static string File(string path)
    {
        using var stream = System.IO.File.OpenRead(path);
        return Convert.ToHexStringLower(SHA256.HashData(stream));
    }

    public static string Text(string value) => Convert.ToHexStringLower(SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(value)));
}
