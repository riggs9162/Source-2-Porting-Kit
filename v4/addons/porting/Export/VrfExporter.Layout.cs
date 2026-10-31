using System.Text;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

public static partial class VrfExporter
{
    private static readonly HashSet<string> ImageSuffixes = [".png", ".tga", ".jpg", ".jpeg", ".exr", ".hdr"];
    private static readonly HashSet<string> GltfSuffixes = [".gltf", ".glb"];

    /// <summary>Source 2 compiled-texture files. The Source 1 pipeline reads the decoded PNG, so these are clutter.</summary>
    private static readonly HashSet<string> CompiledTextureSuffixes = [".vtex", ".vtex_c"];
    private static readonly UTF8Encoding Utf8 = new(false);

    /// <summary>Any quoted path in a VMAT ending in an image or VTEX extension. This covers both authored
    /// <c>"TextureColor"</c> values and the hash-suffixed values under <c>"Compiled Textures"</c>.</summary>
    [GeneratedRegex(@"""([^""\s][^""]*?\.(?:png|tga|jpg|jpeg|exr|hdr|vtex))""", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant)]
    private static partial Regex VmatTexture();

    [GeneratedRegex(@"""Compiled Textures""\s*\{([^{}]*)\}", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant | RegexOptions.Singleline)]
    private static partial Regex CompiledTexturesBlock();

    [GeneratedRegex(@"_(?:png|tga|psd|jpg|jpeg|tif|tiff|exr|hdr)_[0-9a-f]{8}$", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant)]
    private static partial Regex HashedStem();

    private static List<string> Files(string root) => Directory.EnumerateFiles(root, "*", SearchOption.AllDirectories).ToList();

    private static List<string> Folders(string root) => Directory.EnumerateDirectories(root, "*", SearchOption.AllDirectories).ToList();

    private static List<string> FoldersNamed(string root, string name) =>
        Folders(root).Where(path => Path.GetFileName(path).Equals(name, StringComparison.OrdinalIgnoreCase)).ToList();

    private static bool HasSuffix(string path, HashSet<string> suffixes) => suffixes.Contains(PathText.Suffix(path).ToLowerInvariant());

    private static string RelativePosix(string root, string path) => PathText.Posix(Path.GetRelativePath(root, path));

    private static bool IsVmat(string path) => path.EndsWith(".vmat", StringComparison.OrdinalIgnoreCase);

    private static void DeleteTree(string path)
    {
        try
        {
            if (Directory.Exists(path)) Directory.Delete(path, true);
        }
        catch (Exception exception) when (ExportErrors.IsDisk(exception))
        {
        }
    }

    private static bool TryDelete(string path, PortContext context)
    {
        try
        {
            File.Delete(path);
            return true;
        }
        catch (Exception exception) when (ExportErrors.IsDisk(exception))
        {
            context.Log($"Could not delete {path}: {exception.Message}");
            return false;
        }
    }

    /// <summary>Whether any folder in a path is exactly <c>materials</c>, in any case.</summary>
    private static bool UnderMaterials(string path) => path.Split('/', '\\').Any(part => part.Equals("materials", StringComparison.OrdinalIgnoreCase));

    /// <summary>Deletes images sitting directly beside any glTF or GLB, the viewer's stray decoded files.</summary>
    /// <remarks>Non-recursive per folder, and files under any folder named <c>materials</c> are never touched.</remarks>
    public static int PurgeStrayImagesNextToGltf(string root, PortContext context)
    {
        if (!Directory.Exists(root)) return 0;
        var deleted = 0;
        foreach (var folder in Files(root).Where(path => HasSuffix(path, GltfSuffixes)).Select(path => Path.GetDirectoryName(path)!).Distinct().ToList())
        {
            if (UnderMaterials(folder)) continue;
            foreach (var entry in Directory.EnumerateFiles(folder).Where(path => HasSuffix(path, ImageSuffixes)).ToList())
                if (TryDelete(entry, context)) deleted++;
        }
        context.Log($"Removed {deleted} stray image file(s) next to GLTF outputs");
        return deleted;
    }

    /// <summary>Moves every file under a folder into a target, keeping structure and overwriting, then removes the
    /// source. The list is taken first because a target can sit beside its own source's parent.</summary>
    private static int MoveTreeInto(string source, string target, PortContext context)
    {
        if (!Directory.Exists(source)) return 0;
        Directory.CreateDirectory(target);
        var moved = 0;
        foreach (var file in Files(source))
        {
            var destination = Path.Combine(target, Path.GetRelativePath(source, file));
            Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
            try
            {
                File.Move(file, destination, true);
                moved++;
            }
            catch (Exception exception) when (ExportErrors.IsDisk(exception))
            {
                context.Log($"Could not move {file}: {exception.Message}");
            }
        }
        DeleteTree(source);
        return moved;
    }

    /// <summary>Moves each <c>models/&lt;rest&gt;/materials</c> tree up to <c>materials/models/&lt;rest&gt;</c>.</summary>
    /// <remarks>Half-Life: Alyx ships materials inside each model's own folder rather than the parallel tree, and the
    /// viewer extracts them where they live. Without this hoist they would land in <c>modelsrc</c>, or be deleted as
    /// stray duplicates. This is also why a reference may name a <c>materials</c> folder its file no longer sits in.</remarks>
    public static int HoistInnerMaterialsToTop(string staging, PortContext context)
    {
        var modelsRoot = Path.Combine(staging, "models");
        if (!Directory.Exists(modelsRoot)) return 0;
        var materialsTop = Path.Combine(staging, "materials");
        var moved = 0;
        foreach (var source in new[] { "materials", "material" }.SelectMany(name => FoldersNamed(modelsRoot, name)).ToList())
            moved += MoveTreeInto(source, Path.Combine(materialsTop, Path.GetRelativePath(staging, Path.GetDirectoryName(source)!)), context);
        if (moved > 0) context.Log($"Hoisted {moved} file(s) from inner models/.../materials/ to top-level materials/");
        return moved;
    }

    /// <summary>Lays the staged output out as a Source 1 project root.</summary>
    /// <remarks>The inner <c>models</c> and <c>materials</c> levels are dropped as redundant under folders already
    /// named <c>modelsrc</c> and <c>materialsrc</c>. Existing targets merge.</remarks>
    public static void ReorganizeToProjectLayout(string staging, string output, PortContext context)
    {
        foreach (var (source, targetName, noun) in new[] { ("models", "modelsrc", "model"), ("materials", "materialsrc", "material") })
        {
            var from = Path.Combine(staging, source);
            if (!Directory.Exists(from))
            {
                context.Log($"No {source}/ directory to reorganize");
                continue;
            }
            var target = Path.Combine(output, targetName);
            context.Log($"Reorganized {MoveTreeInto(from, target, context)} {noun} file(s) -> {target}");
        }
    }

    /// <summary>Finds Source 2 addon packages: a folder under <c>models</c> holding both <c>models</c> and <c>materials</c>.</summary>
    /// <remarks>Workshop ports often pack a whole Source 1 addon under the Source 2 models root, so the generic hoist
    /// would leave model and materials with their segments in swapped order. This runs before the hoist erases the
    /// signature, and the paths drive <see cref="FlattenNestedPackages"/>. The Alyx case, with materials beside the
    /// model file and no inner models folder, deliberately does not match.</remarks>
    public static List<string> DetectNestedPackageDirs(string staging)
    {
        var root = Path.Combine(staging, "models");
        if (!Directory.Exists(root)) return [];
        var found = new List<string>();
        foreach (var materials in FoldersNamed(root, "materials"))
        {
            var package = Path.GetDirectoryName(materials)!;
            if (!Directory.Exists(Path.Combine(package, "models"))) continue;
            var relative = RelativePosix(root, package);
            if (!found.Contains(relative)) found.Add(relative);
        }
        return found;
    }

    /// <summary>Collapses nested addon packages to a clean <c>&lt;package&gt;</c> root shared by model and materials.</summary>
    /// <remarks>This must run after the texture purges: their reference matching keeps the <c>models/</c> prefix, so
    /// flattening earlier would make every texture look unreferenced and delete it. A pure relocation otherwise.</remarks>
    public static int FlattenNestedPackages(string output, IEnumerable<string> packages, PortContext context)
    {
        var modelsrc = Path.Combine(output, "modelsrc");
        var materialsrc = Path.Combine(output, "materialsrc");
        var flattened = 0;
        foreach (var raw in packages)
        {
            var relative = PathText.Native(raw.Trim().Trim('/'));
            if (relative.Length == 0) continue;
            var moved = false;
            var innerModels = Path.Combine(modelsrc, relative, "models");
            if (Directory.Exists(innerModels))
            {
                MoveTreeInto(innerModels, Path.Combine(modelsrc, relative), context);
                moved = true;
            }
            var packageMaterials = Path.Combine(materialsrc, "models", relative);
            if (Directory.Exists(packageMaterials))
            {
                MoveTreeInto(packageMaterials, Path.Combine(materialsrc, relative), context);
                moved = true;
            }
            if (moved) flattened++;
        }
        var leftover = Path.Combine(materialsrc, "models");
        if (Directory.Exists(leftover) && !Directory.EnumerateFileSystemEntries(leftover).Any())
        {
            try
            {
                Directory.Delete(leftover);
            }
            catch (Exception exception) when (ExportErrors.IsDisk(exception))
            {
            }
        }
        if (flattened > 0) context.Log($"Flattened {flattened} nested package layout(s) to clean <pkg>/ roots");
        return flattened;
    }

    /// <summary>Removes empty folders beneath a root, deepest first, leaving the root itself.</summary>
    private static void PruneEmptyDirs(string root)
    {
        foreach (var folder in Folders(root).OrderByDescending(path => path.Split(Path.DirectorySeparatorChar).Length).ToList())
        {
            try
            {
                if (!Directory.EnumerateFileSystemEntries(folder).Any()) Directory.Delete(folder);
            }
            catch (Exception exception) when (ExportErrors.IsDisk(exception))
            {
            }
        }
    }

    /// <summary>Moves every image and VMAT into the root of <c>materialsrc</c>, flattening its structure.</summary>
    /// <remarks>Strays under <c>modelsrc</c> are swept first, images only, so that a same-named canonical texture moved
    /// afterwards overwrites them. Empty folders left behind are pruned.</remarks>
    public static int FlattenMaterialsToRoot(string output, PortContext context)
    {
        var materialsrc = Path.Combine(output, "materialsrc");
        Directory.CreateDirectory(materialsrc);
        var sources = new List<(string Root, HashSet<string> Suffixes)>();
        var modelsrc = Path.Combine(output, "modelsrc");
        if (Directory.Exists(modelsrc)) sources.Add((modelsrc, ImageSuffixes));
        sources.Add((materialsrc, [.. ImageSuffixes, ".vmat"]));
        var moved = 0;
        foreach (var (root, suffixes) in sources)
            foreach (var file in Files(root))
            {
                if (!HasSuffix(file, suffixes) || Path.GetDirectoryName(file) == materialsrc) continue;
                try
                {
                    File.Move(file, Path.Combine(materialsrc, Path.GetFileName(file)), true);
                    moved++;
                }
                catch (Exception exception) when (ExportErrors.IsDisk(exception))
                {
                    context.Log($"Could not move {file}: {exception.Message}");
                }
            }
        PruneEmptyDirs(materialsrc);
        context.Log($"Flattened {moved} image/vmat file(s) into {Path.GetFileName(materialsrc)}/ root");
        return moved;
    }

    /// <summary>Removes any folder literally named <c>materials</c> under <c>modelsrc</c>.</summary>
    /// <remarks>The viewer sometimes writes one next to each glTF, duplicating the canonical tree already moved.</remarks>
    public static int PurgeInnerMaterialsDirs(string modelsrc, PortContext context)
    {
        if (!Directory.Exists(modelsrc)) return 0;
        var removed = 0;
        foreach (var folder in FoldersNamed(modelsrc, "materials"))
        {
            try
            {
                Directory.Delete(folder, true);
                removed++;
            }
            catch (Exception exception) when (ExportErrors.IsDisk(exception))
            {
                context.Log($"Could not remove {folder}: {exception.Message}");
            }
        }
        context.Log($"Removed {removed} stray materials/ folder(s) inside {Path.GetFileName(modelsrc)}/");
        return removed;
    }

    /// <summary>Converts a texture reference to the lowercase folder and stem it has under <c>materialsrc</c>.</summary>
    /// <remarks>Every <c>materials</c> segment is stripped: the leading one of parallel-tree references, and any
    /// mid-path one of Alyx-style inline references, which the hoist drops when moving the file. The extension goes
    /// too, so a <c>.vtex</c> reference matches the PNG actually decoded. Returns null for an unusable input.</remarks>
    private static (string Folder, string Stem)? NormalizeVmatReference(string reference)
    {
        var text = reference.Replace('\\', '/').Trim().Trim('"').ToLowerInvariant().TrimStart('/');
        while (true)
        {
            if (text.StartsWith("materials/", StringComparison.Ordinal)) text = text["materials/".Length..];
            else if (text.IndexOf("/materials/", StringComparison.Ordinal) is var at && at >= 0) text = text[..at] + "/" + text[(at + "/materials/".Length)..];
            else break;
        }
        var dot = text.LastIndexOf('.');
        if (dot > 0) text = text[..dot];
        if (text.Length == 0) return null;
        var slash = text.LastIndexOf('/');
        return slash < 0 ? ("", text) : (text[..slash], text[(slash + 1)..]);
    }

    private static string? ReadVmat(string path)
    {
        try
        {
            return File.ReadAllText(path, Utf8);
        }
        catch (Exception exception) when (ExportErrors.IsDisk(exception))
        {
            return null;
        }
    }

    private static string VmatFolder(string root, string vmat)
    {
        var folder = RelativePosix(root, Path.GetDirectoryName(vmat)!).ToLowerInvariant();
        return folder == "." ? "" : folder;
    }

    /// <summary>Collects every texture a VMAT names, as folder and stem keys relative to the root.</summary>
    /// <remarks>Each stem is also keyed against the VMAT's own folder. The viewer decodes a VMAT's textures next to it
    /// whatever path was authored, and that path can point into another model's tree entirely: HLA's
    /// <c>bodybag_cleanup.vmat</c> sits under props/hazmat but references props/quarantine. Without the same-folder key
    /// those files would look unreferenced and be purged, orphaning the VMAT.</remarks>
    private static HashSet<(string Folder, string Stem)> CollectVmatReferences(string materialsrc)
    {
        var references = new HashSet<(string Folder, string Stem)>();
        foreach (var vmat in Files(materialsrc).Where(IsVmat))
        {
            if (ReadVmat(vmat) is not { } text) continue;
            var folder = VmatFolder(materialsrc, vmat);
            foreach (Match match in VmatTexture().Matches(text))
            {
                if (NormalizeVmatReference(match.Groups[1].Value) is not { } key) continue;
                references.Add(key);
                references.Add((folder, key.Stem));
            }
        }
        return references;
    }

    /// <summary>Deletes images under <c>materialsrc</c> no VMAT in the tree references.</summary>
    /// <remarks>Matching ignores the extension, so a reference to <c>foo.vtex</c> keeps <c>foo.png</c>. VMATs are never
    /// deleted. An empty reference set means unknown, so nothing is deleted.</remarks>
    public static int PurgeUnreferencedTextures(string materialsrc, PortContext context)
    {
        if (!Directory.Exists(materialsrc)) return 0;
        var references = CollectVmatReferences(materialsrc);
        if (references.Count == 0)
        {
            context.Log("No .vmat references found; skipping unreferenced-texture purge");
            return 0;
        }
        var deleted = 0;
        foreach (var image in Files(materialsrc).Where(path => HasSuffix(path, ImageSuffixes)))
        {
            var relative = RelativePosix(materialsrc, image).ToLowerInvariant();
            var dot = relative.LastIndexOf('.');
            var stem = dot > 0 ? relative[..dot] : relative;
            var slash = stem.LastIndexOf('/');
            var key = slash >= 0 ? (stem[..slash], stem[(slash + 1)..]) : ("", stem);
            if (!references.Contains(key) && TryDelete(image, context)) deleted++;
        }
        context.Log($"Removed {deleted} unreferenced texture file(s) from {Path.GetFileName(materialsrc)}/");
        return deleted;
    }

    /// <summary>Deletes compiled-image duplicates, but only when their authored replacement exists.</summary>
    /// <remarks>A compiled block can hold the only decoded copy of a texture. Direct references to hashed names survive,
    /// as do hashes whose clean-name replacement was not exported. The viewer's <c>name_extension_hash</c> naming proves
    /// a replacement; uncertain names are retained. References use both their authored folder and the VMAT's own folder,
    /// because the hoist moves files.</remarks>
    public static int PurgeCompiledTextureImages(string materialsrc, PortContext context)
    {
        if (!Directory.Exists(materialsrc)) return 0;
        var images = new Dictionary<(string Folder, string Stem), string>();
        foreach (var image in Files(materialsrc).Where(path => HasSuffix(path, ImageSuffixes)))
            if (NormalizeVmatReference(RelativePosix(materialsrc, image)) is { } key) images[key] = image;
        var targets = new HashSet<(string Folder, string Stem)>();
        var direct = new HashSet<(string Folder, string Stem)>();
        var replaceable = new HashSet<(string Folder, string Stem)>();
        foreach (var vmat in Files(materialsrc).Where(IsVmat))
        {
            if (ReadVmat(vmat) is not { } text) continue;
            var folder = VmatFolder(materialsrc, vmat);
            var authored = new HashSet<(string Folder, string Stem)>();
            foreach (Match match in VmatTexture().Matches(CompiledTexturesBlock().Replace(text, "")))
            {
                if (NormalizeVmatReference(match.Groups[1].Value) is not { } key) continue;
                authored.Add(key);
                authored.Add((folder, key.Stem));
            }
            direct.UnionWith(authored);
            foreach (Match block in CompiledTexturesBlock().Matches(text))
                foreach (Match match in VmatTexture().Matches(block.Groups[1].Value))
                {
                    if (NormalizeVmatReference(match.Groups[1].Value) is not { } key) continue;
                    (string, string)[] keys = [key, (folder, key.Stem)];
                    targets.UnionWith(keys);
                    var clean = HashedStem().Replace(key.Stem, "");
                    if (clean != key.Stem && authored.Any(reference => reference.Stem == clean && images.ContainsKey(reference))) replaceable.UnionWith(keys);
                }
        }
        if (targets.Count == 0)
        {
            context.Log("No 'Compiled Textures' references found");
            return 0;
        }
        var deleted = images.Count(image => replaceable.Contains(image.Key) && !direct.Contains(image.Key) && TryDelete(image.Value, context));
        context.Log($"Deleted {deleted} image file(s) referenced in 'Compiled Textures' blocks");
        return deleted;
    }

    /// <summary>Deletes VTEX descriptors anywhere under <c>materialsrc</c>, since the pipeline reads the decoded PNG.</summary>
    public static int PurgeCompiledTextures(string materialsrc, PortContext context)
    {
        if (!Directory.Exists(materialsrc)) return 0;
        var deleted = Files(materialsrc).Count(path => HasSuffix(path, CompiledTextureSuffixes) && TryDelete(path, context));
        context.Log($"Removed {deleted} compiled-texture file(s) from {Path.GetFileName(materialsrc)}/");
        return deleted;
    }
}
