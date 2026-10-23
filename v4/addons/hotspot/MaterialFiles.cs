namespace PortingKit.Addons.Hotspot;

public static class MaterialFiles
{
    private sealed record Entry(ValveToken Key, ValveToken Value);

    private static (List<Entry> Entries, ValveToken Opening) Read(string text)
    {
        var reader = new ValveText(text);
        reader.Take();
        var opening = reader.Tokens.ElementAtOrDefault(reader.Position) ?? throw new InvalidDataException("Missing VMT shader block.");
        reader.Expect("{");
        var entries = new List<Entry>();
        var depth = 1;
        while (depth > 0 && !reader.End)
        {
            if (reader.Accept("}")) { depth--; continue; }
            if (reader.Accept("{")) { depth++; continue; }
            var key = reader.Tokens[reader.Position++];
            if (reader.Peek == "{") continue;
            if (reader.End || reader.Peek == "}") throw new InvalidDataException("Missing VMT value.");
            var value = reader.Tokens[reader.Position++];
            if (depth == 1) entries.Add(new(key, value));
        }
        if (depth != 0 || !reader.End) throw new InvalidDataException("Malformed VMT shader block.");
        return (entries, opening);
    }

    public static string? Reference(string material, string key)
    {
        var result = Read(ReadBounded(material)).Entries.FirstOrDefault(e => e.Key.Value.Equals(key, StringComparison.OrdinalIgnoreCase));
        return result?.Value.Value;
    }

    private static string ReadBounded(string path)
    {
        if (new FileInfo(path).Length > 4 * 1024 * 1024) throw new InvalidDataException("VMT exceeds 4 MiB.");
        return File.ReadAllText(path);
    }

    public static string? MaterialsRoot(string path)
    {
        for (var dir = new DirectoryInfo(Path.GetDirectoryName(Path.GetFullPath(path))!); dir is not null; dir = dir.Parent)
            if (dir.Name.Equals("materials", StringComparison.OrdinalIgnoreCase)) return dir.FullName;
        return null;
    }

    public static string Resolve(string material, string reference, string extension)
    {
        reference = reference.Replace('\\', '/').Trim('/');
        if (reference.StartsWith("materials/", StringComparison.OrdinalIgnoreCase)) reference = reference[10..];
        if (!reference.EndsWith(extension, StringComparison.OrdinalIgnoreCase)) reference += extension;
        if (Path.IsPathRooted(reference) || reference.Split('/').Any(p => p == "..")) throw new InvalidDataException("Material references must be content-relative paths.");
        return Path.GetFullPath(Path.Combine(MaterialsRoot(material) ?? Path.GetDirectoryName(Path.GetFullPath(material))!, reference));
    }

    public static string? TextureFor(string material)
    {
        var reference = Reference(material, "$basetexture");
        if (string.IsNullOrWhiteSpace(reference)) return null;
        var withoutExtension = reference.EndsWith(".vtf", StringComparison.OrdinalIgnoreCase) ? reference[..^4] : reference;
        var root = MaterialsRoot(material);
        var roots = new List<string>();
        if (root is not null)
        {
            roots.Add(root);
            var parent = Directory.GetParent(root)?.Parent;
            if (parent?.Name.Equals("addons", StringComparison.OrdinalIgnoreCase) == true)
            {
                roots.AddRange(parent.EnumerateDirectories().Take(2000).Select(d => Path.Combine(d.FullName, "materials")));
                roots.Add(Path.Combine(parent.Parent!.FullName, "materials"));
            }
        }
        foreach (var ext in new[] { ".vtf", ".png", ".tga", ".jpg", ".bmp" })
        {
            var resolved = Resolve(material, withoutExtension, ext);
            if (File.Exists(resolved)) return resolved;
            var sibling = Path.Combine(Path.GetDirectoryName(Path.GetFullPath(material))!, Path.GetFileName(withoutExtension) + ext);
            if (File.Exists(sibling)) return sibling;
            foreach (var candidateRoot in roots)
            {
                var relative = root is null ? Path.GetFileName(resolved) : Path.GetRelativePath(root, resolved);
                var path = Path.Combine(candidateRoot, relative);
                if (File.Exists(path)) return path;
            }
        }
        return null;
    }

    public static string RectFor(string material)
    {
        var reference = Reference(material, "%rectanglemap") ?? Reference(material, "$rectanglemap");
        return string.IsNullOrWhiteSpace(reference) ? Path.ChangeExtension(material, ".rect") : Resolve(material, reference, ".rect");
    }

    public static string? MaterialFor(string texture)
    {
        var sibling = Path.ChangeExtension(texture, ".vmt");
        if (File.Exists(sibling)) return sibling;
        foreach (var material in Directory.EnumerateFiles(Path.GetDirectoryName(texture)!, "*.vmt").Take(500))
        {
            try
            {
                var reference = Reference(material, "$basetexture");
                if (reference is not null && Path.GetFileNameWithoutExtension(reference.Replace('\\', '/')).Equals(Path.GetFileNameWithoutExtension(texture), StringComparison.OrdinalIgnoreCase)) return material;
            }
            catch (InvalidDataException) { }
        }
        return null;
    }

    public static string LinkText(string material, string rect)
    {
        var root = MaterialsRoot(material) ?? throw new InvalidDataException("Put the VMT and .rect under the same materials folder before linking.");
        var relative = Path.GetRelativePath(root, Path.GetFullPath(rect));
        if (Path.IsPathRooted(relative) || relative.StartsWith("..", StringComparison.Ordinal)) throw new InvalidDataException("The .rect must be under the VMT's materials folder.");
        var reference = Path.ChangeExtension(relative, null).Replace('\\', '/');
        if (reference.Any(c => c is '"' or '\r' or '\n')) throw new InvalidDataException("Invalid rectangle path.");
        var text = ReadBounded(material);
        var (entries, opening) = Read(text);
        var existing = entries.Where(e => e.Key.Value.Equals("%rectanglemap", StringComparison.OrdinalIgnoreCase) || e.Key.Value.Equals("$rectanglemap", StringComparison.OrdinalIgnoreCase)).ToArray();
        if (existing.Length > 1) throw new InvalidDataException("The VMT has multiple rectangle-map keys; remove the ambiguity before linking.");
        if (existing.Length == 1)
        {
            var value = existing[0].Value;
            return text[..value.Start] + '"' + reference + '"' + text[value.End..];
        }
        var newline = text.Contains("\r\n", StringComparison.Ordinal) ? "\r\n" : "\n";
        var baseTexture = entries.FirstOrDefault(e => e.Key.Value.Equals("$basetexture", StringComparison.OrdinalIgnoreCase));
        var at = baseTexture?.Value.End ?? opening.End;
        return text[..at] + newline + "\t\"%rectanglemap\" \"" + reference + "\"" + newline + text[at..];
    }
}
