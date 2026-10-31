using System.Globalization;
using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>A bone hierarchy and absolute parent-local SMD frames.</summary>
/// <param name="Frames">One array per frame holding six numbers per bone: position, then rotation in x, y, z radians.</param>
public sealed record SmdAnimation(IReadOnlyList<string> Names, IReadOnlyList<int> Parents, IReadOnlyList<double[]> Frames, IReadOnlyList<int> Times);

/// <summary>A generated animation library and its source coverage.</summary>
public sealed class PlayerAnimationResult
{
    public required string Qc { get; init; }
    public required string Profile { get; init; }
    public string SourceRevision { get; init; } = PlayerAnimations.SourceRevision;
    public List<string> Files { get; } = [];
    public int Animations { get; set; }
    public int Frames { get; set; }
    public JsonObject Retargeting { get; set; } = [];
}

/// <summary>Caches the published GMod animation sources and retargets their complete graph onto a model's own skeleton.</summary>
public static partial class PlayerAnimations
{
    public const string SourceRevision = "9a588af0cb1a2d53de35496ba1737305a877e50c";
    public const string SourceUrl = "https://codeload.github.com/robotboy655/gmod-animations/zip/" + SourceRevision;
    public const string SourceArchiveSha256 = "e110fac7292fd946929fd98c4d98f61feddffd2c305a835fe308e86323c28ec7";
    public const string SourceCredits = "Maxime Lebled, Facepunch Studios LTD / Valve Software";

    private const string ProvenanceName = "s2kit_provenance.json";
    private const int DownloadStallSeconds = 90;
    private const int MacroDepthLimit = 30;

    /// <summary>The upstream QC that compiles each animation profile.</summary>
    public static readonly IReadOnlyDictionary<string, string> Profiles = new OrderedDictionary<string, string>(StringComparer.Ordinal)
    {
        ["male"] = "player_male_anims_m_anm.qc", ["female"] = "player_female_anims_f_anm.qc", ["zombie"] = "player_zombie_anims_z_anm.qc"
    };

    private static readonly HashSet<string> ArchiveExtensions = new([".qc", ".qci", ".smd", ".vrd", ".md"], StringComparer.Ordinal);
    private static readonly HashSet<string> PostureBones = new(["valvebiped.bip01_pelvis", "valvebiped.bip01_neck1", "valvebiped.bip01_head1", "valvebiped.bip01_l_clavicle", "valvebiped.bip01_r_clavicle"], StringComparer.Ordinal);

    [GeneratedRegex(@"^\s*(\d+)\s+""([^""]+)""\s+(-?\d+)\s*$")]
    private static partial Regex Node();

    [GeneratedRegex(@"^\s*\$definebone\s+""([^""]+)""\s+""([^""]*)""\s+([^\r\n]+)", RegexOptions.Multiline | RegexOptions.IgnoreCase | RegexOptions.CultureInvariant)]
    private static partial Regex DefineBone();

    [GeneratedRegex(@"^\s*\$modelname\s+.*$", RegexOptions.Multiline | RegexOptions.IgnoreCase | RegexOptions.CultureInvariant)]
    private static partial Regex ModelnameLine();

    [GeneratedRegex(@"^\s*\$attachment\s+.*$", RegexOptions.Multiline | RegexOptions.IgnoreCase | RegexOptions.CultureInvariant)]
    private static partial Regex AttachmentLine();

    [GeneratedRegex(@"^\s*\$include\s+""?Humans_sdk\\male\\hitbox.qci""?.*$", RegexOptions.Multiline | RegexOptions.IgnoreCase | RegexOptions.CultureInvariant)]
    private static partial Regex HitboxInclude();

    [GeneratedRegex(@"^\s*\$definebone\s+.*$", RegexOptions.Multiline | RegexOptions.IgnoreCase | RegexOptions.CultureInvariant)]
    private static partial Regex DefineBoneLine();

    /// <summary>Text as read with bad bytes replaced rather than refused.</summary>
    private static string ReadReplacing(string path) => File.ReadAllText(path, new UTF8Encoding(false));

    /// <summary>Text as read strictly, with line endings normalized to a newline.</summary>
    private static string ReadStrict(string path) => File.ReadAllText(path, new UTF8Encoding(false, true)).Replace("\r\n", "\n").Replace('\r', '\n');

    /// <summary>Removes a QC line comment while retaining quoted strings verbatim.</summary>
    private static string QcContent(string line)
    {
        var quoted = false;
        for (var index = 0; index < line.Length; index++)
        {
            if (line[index] == '"') quoted = !quoted;
            if (!quoted && index + 1 < line.Length && line[index] == '/' && line[index + 1] == '/') return line[..index];
        }
        return line;
    }

    private static bool WordSpace(char character) => character is ' ' or '\t' or '\r' or '\n';

    /// <summary>Splits QC words, keeping quotes on a word that starts with one, as a shell-style splitter in its plain mode does.</summary>
    private static List<string> Words(string text)
    {
        var words = new List<string>();
        var index = 0;
        while (index < text.Length)
        {
            var character = text[index];
            if (WordSpace(character))
            {
                index++;
                continue;
            }
            if (character is '"' or '\'')
            {
                var end = text.IndexOf(character, index + 1);
                if (end < 0) throw new InvalidDataException("No closing quotation");
                words.Add(text[index..(end + 1)]);
                index = end + 1;
                continue;
            }
            var start = index;
            while (index < text.Length && !WordSpace(text[index])) index++;
            words.Add(text[start..index]);
        }
        return words;
    }

    /// <summary>Expands upstream includes and macros, because StudioMDL CE's macro buffer is too small for them.</summary>
    public static string ExpandAnimationGraph(string qcPath)
    {
        var root = Path.GetDirectoryName(Path.GetFullPath(qcPath)) ?? ".";
        var macros = new Dictionary<string, (List<string> Arguments, string Body)>(StringComparer.Ordinal);

        List<string> ExpandLine(string line, int depth = 0)
        {
            if (depth > MacroDepthLimit) throw new InvalidDataException("Player animation graph contains recursive QC macros");
            var content = QcContent(line).Trim();
            if (content.Length == 0) return [];
            var words = Words(content);
            if (words.Count == 0 || !macros.TryGetValue(words[0].ToLowerInvariant(), out var macro)) return [content];
            var (arguments, body) = macro;
            if (words.Count - 1 < arguments.Count) throw new InvalidDataException($"Not enough arguments for player animation macro {words[0]}");
            for (var index = 0; index < arguments.Count; index++)
            {
                var value = words[index + 1];
                body = Regex.Replace(body, Regex.Escape("$" + arguments[index] + "$"), _ => value, RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
            }
            var trailing = string.Join(' ', words.Skip(arguments.Count + 1));
            if (trailing.Length > 0) body += " " + trailing;
            var output = new List<string>();
            foreach (var value in PathText.SplitLines(body)) output.AddRange(ExpandLine(value, depth + 1));
            return output;
        }

        List<string> ReadFile(string path, IReadOnlyList<string> active)
        {
            var identity = Path.GetFullPath(path);
            if (active.Contains(identity, StringComparer.OrdinalIgnoreCase)) throw new InvalidDataException("Player animation graph contains a recursive include");
            var lines = PathText.SplitLines(File.ReadAllText(path, new UTF8Encoding(false, true)));
            var output = new List<string>();
            for (var index = 0; index < lines.Count; index++)
            {
                var content = QcContent(lines[index]).TrimEnd();
                var words = Words(content);
                if (words.Count > 0 && words[0].Equals("$definemacro", StringComparison.OrdinalIgnoreCase))
                {
                    var continuation = content.EndsWith(@"\\", StringComparison.Ordinal);
                    var header = Words(continuation ? content[..^2] : content);
                    var body = new List<string>();
                    while (continuation)
                    {
                        index++;
                        if (index >= lines.Count) throw new InvalidDataException($"Unterminated QC macro in {PathText.Native(path)}");
                        var value = QcContent(lines[index]).TrimEnd();
                        var wrapped = value.EndsWith(@"\\", StringComparison.Ordinal);
                        continuation = wrapped || lines[index].TrimEnd().EndsWith(@"\\", StringComparison.Ordinal);
                        body.Add(wrapped ? value[..^2].TrimEnd() : value);
                    }
                    macros["$" + header[1].TrimStart('$').ToLowerInvariant()] = (header.Skip(2).ToList(), string.Join('\n', body));
                }
                else if (words.Count > 0 && words[0].Equals("$include", StringComparison.OrdinalIgnoreCase))
                {
                    var relative = words[1].Trim('"').Replace('\\', '/');
                    var include = Path.Combine(root, relative);
                    if (!File.Exists(include)) include = Path.Combine(Path.GetDirectoryName(path) ?? ".", relative);
                    output.AddRange(ReadFile(include, [.. active, identity]));
                }
                else output.AddRange(ExpandLine(content));
            }
            return output;
        }

        return string.Join('\n', ReadFile(qcPath, [])) + "\n";
    }

    private static async Task<byte[]> DownloadAsync(PortContext? context)
    {
        var cancellation = context?.Token ?? CancellationToken.None;
        using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(DownloadStallSeconds) };
        using var request = new HttpRequestMessage(HttpMethod.Get, SourceUrl);
        request.Headers.TryAddWithoutValidation("User-Agent", "Source2PortingKit/3");
        using var response = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, cancellation).ConfigureAwait(false);
        response.EnsureSuccessStatusCode();
        await using var stream = await response.Content.ReadAsStreamAsync(cancellation).ConfigureAwait(false);
        using var memory = new MemoryStream();
        var buffer = new byte[1024 * 1024];
        while (true)
        {
            using var stall = CancellationTokenSource.CreateLinkedTokenSource(cancellation);
            stall.CancelAfter(TimeSpan.FromSeconds(DownloadStallSeconds));
            var read = await stream.ReadAsync(buffer, stall.Token).ConfigureAwait(false);
            if (read == 0) return memory.ToArray();
            context?.Check();
            memory.Write(buffer, 0, read);
        }
    }

    /// <summary>An archive entry's path below the upstream top-level folder.</summary>
    /// <remarks>A leading root or drive counts as the first part, so a rooted entry loses it instead of a real folder. What
    /// stays is exactly what could climb out of the staging folder, and the caller's check is what refuses it.</remarks>
    private static string[] ArchiveRelative(string entry)
    {
        var name = entry.Replace('/', Path.DirectorySeparatorChar);
        var anchor = Path.GetPathRoot(name) ?? "";
        var parts = name[anchor.Length..].Split(Path.DirectorySeparatorChar, StringSplitOptions.RemoveEmptyEntries).Where(part => part != ".").ToList();
        return (anchor.Length > 0 ? parts : parts.Skip(1)).ToArray();
    }

    /// <summary>Downloads the pinned upstream archive once into a verified source cache.</summary>
    /// <param name="cache">The folder that holds the revision, or null for the default place.</param>
    /// <param name="defaultFolder">The caller's general cache folder, which gains a <c>player_animations</c> folder when
    /// <paramref name="cache"/> is null.</param>
    /// <returns>The folder holding the unpacked sources.</returns>
    public static string EnsureAnimationSources(string? cache, string defaultFolder, PortContext? context = null)
    {
        var folder = !string.IsNullOrEmpty(cache) ? cache : Path.Combine(defaultFolder, "player_animations");
        var destination = Path.Combine(folder, SourceRevision);
        var provenance = Path.Combine(destination, ProvenanceName);
        if (File.Exists(provenance))
        {
            var known = Json.Load(provenance);
            if (known.Get("revision").Str() == SourceRevision && known.Get("sha256").Str() == SourceArchiveSha256
                && Profiles.Values.All(name => File.Exists(Path.Combine(destination, name)))) return destination;
        }
        context?.Check();
        context?.Log("Downloading the published Garry's Mod player animation sources");
        var raw = DownloadAsync(context).GetAwaiter().GetResult();
        var digest = Convert.ToHexStringLower(SHA256.HashData(raw));
        if (digest != SourceArchiveSha256) throw new InvalidDataException("Published animation source archive failed its SHA-256 verification");
        Install(raw, digest, folder, destination, context);
        context?.Log("Published player animation sources cached", LogLevel.Success);
        return destination;
    }

    /// <summary>Unpacks a verified archive beside the cache and only then moves it into place.</summary>
    /// <remarks>A half-written copy therefore never looks finished. Only the file kinds the graph reads are taken, and an entry
    /// that would land outside the staging folder refuses the whole archive.</remarks>
    private static void Install(byte[] raw, string digest, string folder, string destination, PortContext? context)
    {
        var staging = Path.GetFullPath(Path.Combine(folder, SourceRevision + ".partial"));
        Directory.CreateDirectory(staging);
        using (var archive = new ZipArchive(new MemoryStream(raw), ZipArchiveMode.Read))
            foreach (var entry in archive.Entries)
            {
                context?.Check();
                var parts = ArchiveRelative(entry.FullName);
                if (parts.Length == 0 || entry.FullName.EndsWith('/')) continue;
                if (!ArchiveExtensions.Contains(PathText.Suffix(parts[^1]).ToLowerInvariant())) continue;
                var target = Path.GetFullPath(Path.Combine([staging, .. parts]));
                if (!WorkflowPaths.Within(target, staging)) throw new InvalidDataException("Animation source archive contains an unsafe path");
                Directory.CreateDirectory(Path.GetDirectoryName(target)!);
                using var input = entry.Open();
                using var output = File.Create(target);
                input.CopyTo(output);
            }
        foreach (var name in Profiles.Values)
            if (!File.Exists(Path.Combine(staging, name))) throw new InvalidDataException($"Animation source archive is missing {name}");
        var record = new JsonObject { ["revision"] = SourceRevision, ["source"] = SourceUrl, ["sha256"] = digest, ["credits"] = SourceCredits };
        PythonText.Write(Path.Combine(staging, ProvenanceName), PythonJson.Indented(record));
        if (Path.Exists(destination)) throw new InvalidDataException($"Incomplete animation cache already exists: {PathText.Native(destination)}");
        Directory.Move(staging, destination);
    }

    /// <summary>The length of the number a span starts with, or zero when it starts with something else.</summary>
    private static int NumberLength(ReadOnlySpan<char> text)
    {
        var at = 0;
        if (at < text.Length && text[at] is '+' or '-') at++;
        var digits = 0;
        while (at < text.Length && char.IsAsciiDigit(text[at]))
        {
            at++;
            digits++;
        }
        if (at < text.Length && text[at] == '.')
        {
            at++;
            while (at < text.Length && char.IsAsciiDigit(text[at]))
            {
                at++;
                digits++;
            }
        }
        if (digits == 0) return 0;
        if (at < text.Length && text[at] is 'e' or 'E')
        {
            var exponent = at + 1;
            if (exponent < text.Length && text[exponent] is '+' or '-') exponent++;
            var start = exponent;
            while (exponent < text.Length && char.IsAsciiDigit(text[exponent])) exponent++;
            if (exponent > start) at = exponent;
        }
        return at;
    }

    /// <summary>Reads whitespace-separated numbers until the first thing that is not one, or until the output is full.</summary>
    /// <returns>How many numbers were read.</returns>
    private static int ScanNumbers(ReadOnlySpan<char> text, Span<double> output)
    {
        var count = 0;
        var at = 0;
        while (count < output.Length)
        {
            while (at < text.Length && char.IsWhiteSpace(text[at])) at++;
            var length = NumberLength(text[at..]);
            if (length == 0) break;
            output[count++] = double.Parse(text.Slice(at, length), NumberStyles.Float, CultureInfo.InvariantCulture);
            at += length;
        }
        return count;
    }

    /// <summary>Reads the skeleton blocks without importing mesh triangles or running tools.</summary>
    /// <remarks>A frame starts as a copy of the one before it, so a sparse frame keeps the bones it leaves out. A skeleton
    /// block with no closing line loses its last frame, as it always has.</remarks>
    public static SmdAnimation ReadSmdAnimation(string path)
    {
        var names = new List<string>();
        var parents = new List<int>();
        var indices = new Dictionary<int, int>();
        var mode = "";
        var frames = new List<double[]>();
        var times = new List<int>();
        double[]? current = null;
        Span<double> numbers = stackalloc double[8];
        foreach (var line in PathText.SplitLines(ReadReplacing(path)))
        {
            var value = line.AsSpan().Trim();
            if (value.SequenceEqual("nodes")) mode = "nodes";
            else if (value.SequenceEqual("skeleton")) mode = "skeleton";
            else if (value.SequenceEqual("end"))
            {
                if (mode == "skeleton")
                {
                    if (current is not null) frames.Add(current);
                    break;
                }
                mode = "";
            }
            else if (mode == "nodes")
            {
                var match = Node().Match(value.ToString());
                if (!match.Success) continue;
                indices[int.Parse(match.Groups[1].ValueSpan, CultureInfo.InvariantCulture)] = names.Count;
                names.Add(match.Groups[2].Value);
                parents.Add(int.Parse(match.Groups[3].ValueSpan, CultureInfo.InvariantCulture));
            }
            else if (mode == "skeleton" && value.StartsWith("time "))
            {
                if (current is not null)
                {
                    frames.Add(current);
                    current = (double[])current.Clone();
                }
                else current = new double[names.Count * 6];
                var rest = value["time ".Length..].TrimStart();
                var end = rest.IndexOfAny(" \t");
                times.Add(int.Parse(end < 0 ? rest : rest[..end], CultureInfo.InvariantCulture));
            }
            else if (mode == "skeleton" && current is not null && value.Length > 0)
            {
                if (ScanNumbers(value, numbers) != 7 || !indices.TryGetValue((int)numbers[0], out var bone)) continue;
                numbers[1..7].CopyTo(current.AsSpan(bone * 6, 6));
            }
        }
        if (frames.Count == 0 || names.Count == 0) throw new InvalidDataException($"Animation SMD has no skeleton: {PathText.Native(path)}");
        return new(names, parents.ConvertAll(parent => indices.GetValueOrDefault(parent, -1)), frames, times);
    }

    /// <summary>Evaluates all frames with rigid forward kinematics.</summary>
    /// <returns>World rotations and positions, indexed by bone and then by frame.</returns>
    public static (M3[][] Rotations, V3[][] Positions) AnimationWorlds(SmdAnimation animation)
    {
        int count = animation.Frames.Count, bones = animation.Names.Count;
        var rotations = new M3[bones][];
        var positions = new V3[bones][];

        void Evaluate(int index)
        {
            if (rotations[index] is not null) return;
            var parent = animation.Parents[index];
            if (parent >= 0) Evaluate(parent);
            var worldRotations = new M3[count];
            var worldPositions = new V3[count];
            var offset = index * 6;
            for (var frame = 0; frame < count; frame++)
            {
                var row = animation.Frames[frame];
                var local = RotationMath.MatrixOf(row[offset + 3], row[offset + 4], row[offset + 5]);
                var translation = new V3(row[offset], row[offset + 1], row[offset + 2]);
                if (parent < 0)
                {
                    worldRotations[frame] = local;
                    worldPositions[frame] = translation;
                }
                else
                {
                    worldRotations[frame] = rotations[parent][frame] * local;
                    worldPositions[frame] = positions[parent][frame] + rotations[parent][frame].ApplyInStack(translation);
                }
            }
            rotations[index] = worldRotations;
            positions[index] = worldPositions;
        }

        for (var index = 0; index < bones; index++) Evaluate(index);
        return (rotations, positions);
    }

    /// <summary>Reads the upstream explicit bind, including the hand attachment bones.</summary>
    public static SmdAnimation SourceBind(string sourceRoot, string profile = "male")
    {
        var sex = profile == "female" ? "Female" : "Male";
        var text = ReadStrict(Path.Combine(sourceRoot, "humans_sdk", sex, "commonbones.qci"));
        var names = new List<string>();
        var parentNames = new List<string>();
        var frame = new List<double>();
        Span<double> values = stackalloc double[16];
        foreach (Match match in DefineBone().Matches(text))
        {
            var name = match.Groups[1].Value;
            if (ScanNumbers(match.Groups[3].ValueSpan, values) < 6) throw new InvalidDataException($"Malformed source bone definition: {name}");
            names.Add(name);
            parentNames.Add(match.Groups[2].Value);
            frame.AddRange([values[0], values[1], values[2], Fmt.Radians(values[5]), Fmt.Radians(values[3]), Fmt.Radians(values[4])]);
        }
        var indices = new Dictionary<string, int>(StringComparer.Ordinal);
        for (var index = 0; index < names.Count; index++) indices[names[index].ToLowerInvariant()] = index;
        return new(names, parentNames.ConvertAll(parent => indices.GetValueOrDefault(parent.ToLowerInvariant(), -1)), [frame.ToArray()], [0]);
    }

    /// <summary>Writes absolute target-local transforms with stable bone order.</summary>
    public static void WriteAnimationSmd(string path, GltfSkin skin, IReadOnlyList<double[]> frames, IReadOnlyList<int> times)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
        using var output = new StreamWriter(path, false, new ASCIIEncoding(), 1 << 16) { NewLine = "\n" };
        output.Write("version 1\nnodes\n");
        for (var index = 0; index < skin.Joints.Count; index++)
        {
            var joint = skin.Joints[index];
            if (!Ascii.IsValid(joint.Name)) throw new InvalidDataException($"Bone name is not ASCII: {joint.Name}");
            output.Write($"{Fmt.Int(index)} \"{joint.Name}\" {Fmt.Int(joint.Parent ?? -1)}\n");
        }
        output.Write("end\nskeleton\n");
        Span<char> number = stackalloc char[64];
        for (var frame = 0; frame < Math.Min(times.Count, frames.Count); frame++)
        {
            output.Write($"time {Fmt.Int(times[frame])}\n");
            var row = frames[frame];
            for (var index = 0; index < skin.Joints.Count; index++)
            {
                output.Write(Fmt.Int(index));
                for (var value = 0; value < 6; value++)
                {
                    output.Write(' ');
                    row[index * 6 + value].TryFormat(number, out var written, "F7", CultureInfo.InvariantCulture);
                    output.Write(number[..written]);
                }
                output.Write('\n');
            }
        }
        output.Write("end\n");
    }

    /// <summary>Keeps source torso posture and canonical limb directions in the model-owned player graph.</summary>
    public static string PlayerBoneMotionMode(string name)
    {
        var folded = name.ToLowerInvariant();
        return PostureBones.Contains(folded) || folded.StartsWith("valvebiped.bip01_spine", StringComparison.Ordinal) ? "bind_relative" : "canonical";
    }

    private static Dictionary<string, int> LowerIndex(IReadOnlyList<string> names)
    {
        var indices = new Dictionary<string, int>(StringComparer.Ordinal);
        for (var index = 0; index < names.Count; index++) indices[names[index].ToLowerInvariant()] = index;
        return indices;
    }

    /// <summary>Preserves source torso posture, canonical limb motion and native joint offsets.</summary>
    /// <returns>One array per frame, six numbers per target bone.</returns>
    public static double[][] RetargetAnimation(SmdAnimation animation, SmdAnimation bind, GltfSkin target)
    {
        var (donorRotation, donorPosition) = AnimationWorlds(animation);
        var (bindRotation, bindPosition) = AnimationWorlds(bind);
        var targetWorlds = SkeletonRetarget.SkinWorldMatrices(target);
        var sourceNames = LowerIndex(animation.Names);
        var bindNames = LowerIndex(bind.Names);
        var count = animation.Frames.Count;
        var frames = new double[count][];
        for (var frame = 0; frame < count; frame++) frames[frame] = new double[target.Joints.Count * 6];
        var worldRotations = new M3[target.Joints.Count][];
        for (var index = 0; index < target.Joints.Count; index++)
        {
            var joint = target.Joints[index];
            var name = joint.Name.ToLowerInvariant();
            var shared = sourceNames.TryGetValue(name, out var source) & bindNames.TryGetValue(name, out var rest);
            var parentWorld = joint.Parent is { } parent ? worldRotations[parent] : null;
            var localBind = RotationMath.MatrixOf(joint.RestRotation);
            M3[] world;
            if (shared)
            {
                world = donorRotation[source];
                if (PlayerBoneMotionMode(joint.Name) == "bind_relative")
                {
                    var relative = new M3[count];
                    var bindInverse = bindRotation[rest][0].Transposed();
                    var targetRotation = M3.Of(targetWorlds[index]);
                    for (var frame = 0; frame < count; frame++) relative[frame] = world[frame] * bindInverse * targetRotation;
                    world = relative;
                }
            }
            else
            {
                world = new M3[count];
                for (var frame = 0; frame < count; frame++) world[frame] = parentWorld is null ? localBind : parentWorld[frame] * localBind;
            }
            var offset = index * 6;
            for (var frame = 0; frame < count; frame++)
            {
                var local = parentWorld is null ? world[frame] : parentWorld[frame].Transposed() * world[frame];
                var euler = RotationMath.AsEuler(RotationMath.FromMatrix(local));
                var translation = joint.RestTranslation;
                if (parentWorld is null && shared) translation += donorPosition[source][frame] - bindPosition[rest][0];
                var row = frames[frame];
                (row[offset], row[offset + 1], row[offset + 2], row[offset + 3], row[offset + 4], row[offset + 5]) = (translation.X, translation.Y, translation.Z, euler.X, euler.Y, euler.Z);
            }
            worldRotations[index] = world;
        }
        if (!frames.All(row => row.All(double.IsFinite))) throw new InvalidDataException("Animation retarget produced a non-finite transform");
        return frames;
    }

    /// <summary>Verifies source torso reference identity and discloses canonical limb reference adaptations.</summary>
    private static JsonObject ReferenceMotionAudit(SmdAnimation bind, GltfSkin target)
    {
        var frames = RetargetAnimation(bind, bind, target);
        var parents = target.Joints.ConvertAll(joint => joint.Parent ?? -1);
        var (rotations, positions) = AnimationWorlds(new(target.JointNames, parents, frames, bind.Times));
        var targetWorlds = SkeletonRetarget.SkinWorldMatrices(target);
        var references = LowerIndex(bind.Names);
        var modes = new JsonArray();
        var adaptations = new JsonArray();
        var positionErrors = new double[target.Joints.Count];
        var angularErrors = new double[target.Joints.Count];
        double postureMaximum = 0.0, postureAngular = 0.0, translationError = 0.0;
        for (var index = 0; index < target.Joints.Count; index++)
        {
            var joint = target.Joints[index];
            positionErrors[index] = (positions[index][0] - targetWorlds[index].Translation).Length;
            angularErrors[index] = Fmt.Degrees(RotationMath.Magnitude(RotationMath.FromMatrix(rotations[index][0] * M3.Of(targetWorlds[index]).Transposed())));
            var mode = references.ContainsKey(joint.Name.ToLowerInvariant()) ? PlayerBoneMotionMode(joint.Name) : "parent_bind";
            modes.Add(new JsonObject { ["bone"] = joint.Name, ["mode"] = mode });
            if (mode == "bind_relative")
            {
                postureMaximum = Math.Max(postureMaximum, positionErrors[index]);
                postureAngular = Math.Max(postureAngular, angularErrors[index]);
            }
            if (positionErrors[index] > 1e-5 || angularErrors[index] > 1e-5)
                adaptations.Add(new JsonObject { ["bone"] = joint.Name, ["mode"] = mode, ["position_delta_in"] = positionErrors[index], ["rotation_delta_degrees"] = angularErrors[index] });
            foreach (var row in frames)
                translationError = new[] { translationError, Math.Abs(row[index * 6] - joint.RestTranslation.X), Math.Abs(row[index * 6 + 1] - joint.RestTranslation.Y), Math.Abs(row[index * 6 + 2] - joint.RestTranslation.Z) }.Max();
        }
        var report = new JsonObject
        {
            ["method"] = "Bind-relative source torso, neck, head and clavicles with canonical GMod limbs",
            ["native_joint_offsets"] = "unchanged", ["donor_root_motion"] = "preserved", ["upstream_graph"] = "unchanged",
            ["maximum_posture_reference_position_error_in"] = postureMaximum, ["maximum_posture_reference_rotation_error_degrees"] = postureAngular,
            ["maximum_reference_position_adaptation_in"] = positionErrors.Max(), ["maximum_reference_rotation_adaptation_degrees"] = angularErrors.Max(),
            ["maximum_reference_local_translation_error_in"] = translationError, ["bone_modes"] = modes, ["reference_adaptations"] = adaptations
        };
        if (postureMaximum > 1e-5 || postureAngular > 1e-5 || translationError > 1e-8)
            throw new InvalidDataException("Player motion does not preserve the source torso reference and native joint offsets");
        return report;
    }

    /// <summary>Lists files with one extension beneath a folder, ordered as Windows path objects sort: by lowercase path parts.</summary>
    private static List<string> SortedFiles(string root, string extension)
    {
        var comparer = Comparer<string[]>.Create((first, second) =>
        {
            for (var index = 0; index < Math.Min(first.Length, second.Length); index++)
            {
                var order = string.CompareOrdinal(first[index], second[index]);
                if (order != 0) return order;
            }
            return first.Length.CompareTo(second.Length);
        });
        return Directory.EnumerateFiles(root, "*", SearchOption.AllDirectories)
            .Where(path => Path.GetExtension(path).Equals(extension, StringComparison.OrdinalIgnoreCase))
            .OrderBy(path => Path.GetRelativePath(root, path).ToLowerInvariant().Split(Path.DirectorySeparatorChar), comparer).ToList();
    }

    private static void WriteLines(string path, string text)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
        Fmt.WriteText(path, text);
    }

    /// <summary>Mirrors and retargets the upstream graph into a model-owned animation library.</summary>
    /// <remarks>Bone data, the root, IK chains and hitboxes belong to the model, so those include files are rewritten; every
    /// other include is the upstream graph unchanged. The QC is expanded last, because StudioMDL CE cannot hold the macros.</remarks>
    public static PlayerAnimationResult WritePlayerAnimationLibrary(string sourceRoot, string outDir, GltfSkin skin, string modelname, IReadOnlyList<string> bindLines, IReadOnlyList<string> ikLines,
        string profile = "male", PortContext? context = null)
    {
        if (!Profiles.TryGetValue(profile, out var qcName)) throw new InvalidDataException($"Unknown player animation profile: {profile}");
        Directory.CreateDirectory(outDir);
        var result = new PlayerAnimationResult { Qc = Path.Combine(outDir, "player_animations.qc"), Profile = profile };
        var bind = SourceBind(sourceRoot, profile);
        result.Retargeting = ReferenceMotionAudit(bind, skin);
        var sources = SortedFiles(sourceRoot, ".smd");
        for (var index = 0; index < sources.Count; index++)
        {
            context?.Check();
            var animation = ReadSmdAnimation(sources[index]);
            if (!animation.Names.Any(name => name.Equals("valvebiped.bip01_pelvis", StringComparison.OrdinalIgnoreCase))) continue;
            var destination = Path.Combine(outDir, Path.GetRelativePath(sourceRoot, sources[index]));
            var frames = RetargetAnimation(animation, bind, skin);
            WriteAnimationSmd(destination, skin, frames, animation.Times);
            result.Animations++;
            result.Frames += frames.Length;
            result.Files.Add(destination);
            if (index % 25 == 0) context?.Log($"Retargeting player animation sources: {index + 1}/{sources.Count}");
        }
        foreach (var path in SortedFiles(sourceRoot, ".qci"))
        {
            var relative = Path.GetRelativePath(sourceRoot, path);
            var destination = Path.Combine(outDir, relative);
            var name = PathText.Posix(relative).ToLowerInvariant();
            string text;
            if (name.EndsWith("/commonbones.qci", StringComparison.Ordinal)) text = string.Join('\n', bindLines) + "\n";
            else if (name == "humans_sdk/standardhierarchy.qci") text = $"$root \"{skin.Joints[skin.RootIndex].Name}\"\n";
            else if (name == "humans_sdk/standardikchains.qci") text = string.Join('\n', ikLines) + "\n";
            else if (name.EndsWith("/hitbox.qci", StringComparison.Ordinal)) text = "";
            else text = ReadReplacing(path).Replace("\r\n", "\n").Replace('\r', '\n');
            WriteLines(destination, text);
            result.Files.Add(destination);
        }
        var qc = ReadStrict(Path.Combine(sourceRoot, qcName));
        qc = ModelnameLine().Replace(qc, _ => $"$modelname \"{modelname}\"");
        qc = AttachmentLine().Replace(qc, "");
        qc = HitboxInclude().Replace(qc, "");
        if (profile == "zombie")
        {
            qc = DefineBoneLine().Replace(qc, "");
            qc = qc.Replace("$pushd\t\"player\"", "$pushd \"player\"\n$include \"humans_sdk/male/commonbones.qci\"", StringComparison.Ordinal);
        }
        WriteLines(result.Qc, qc);
        var upstream = Path.Combine(outDir, "upstream_graph.qci");
        WriteLines(upstream, qc);
        result.Files.Add(upstream);
        WriteLines(result.Qc, ExpandAnimationGraph(result.Qc));
        result.Files.Add(result.Qc);
        var provenance = Path.Combine(outDir, "animation_provenance.json");
        PythonText.Write(provenance, PythonJson.Indented(new JsonObject
        {
            ["revision"] = SourceRevision, ["source"] = SourceUrl, ["credits"] = SourceCredits, ["profile"] = profile, ["animations"] = result.Animations,
            ["frames"] = result.Frames, ["retargeting"] = result.Retargeting.DeepClone()
        }));
        result.Files.Add(provenance);
        context?.Log($"Retargeted {result.Animations} animation sources, {result.Frames} frames", LogLevel.Success);
        return result;
    }

    private static double RotationError(Q4 a, Q4 b)
    {
        var similarity = Math.Abs(a.Dot(b) / (a.Length * b.Length));
        return Fmt.Degrees(2 * Math.Acos(Math.Clamp(similarity, 0.0, 1.0)));
    }

    private static JsonArray Activities(IEnumerable<(string Label, string Act, int Weight)> values) => new(values.OrderBy(item => item.Label, StringComparer.Ordinal)
        .ThenBy(item => item.Act, StringComparer.Ordinal).ThenBy(item => item.Weight).Select(item => (JsonNode?)new JsonArray(item.Label, item.Act, item.Weight)).ToArray());

    /// <summary>Checks owned bind consistency and the stock player activity and sequence contract.</summary>
    /// <param name="stockMdl">A stock model whose sequences and activities the library must match exactly, when one is given.</param>
    /// <remarks>The two models must agree bone for bone: the library's bind is what its sequences were compiled against, and a
    /// sequence initializes its shared bones from that bind, so any drift becomes a permanent offset on every animation.</remarks>
    public static JsonObject AuditCompiledPlayerModel(string modelPath, string animationPath, string? stockMdl = null, string profile = "male")
    {
        var modelBones = MdlBones.ReadMdlBones(modelPath);
        var animationBones = new Dictionary<string, MdlBone>(StringComparer.Ordinal);
        foreach (var bone in MdlBones.ReadMdlBones(animationPath)) animationBones[bone.Name] = bone;
        double positionError = 0.0, rotationError = 0.0;
        var missing = new List<string>();
        foreach (var bone in modelBones)
        {
            if (!animationBones.TryGetValue(bone.Name, out var other))
            {
                missing.Add(bone.Name);
                continue;
            }
            positionError = Math.Max(positionError, (bone.Position - other.Position).Length);
            rotationError = Math.Max(rotationError, RotationError(bone.Rotation, other.Rotation));
        }
        var sequences = MdlBones.ReadMdlSequences(animationPath);
        var activityCount = sequences.Select(sequence => sequence.ActName).Where(name => name.Length > 0).Distinct(StringComparer.Ordinal).Count();
        var report = new JsonObject
        {
            ["model_bones"] = modelBones.Count, ["library_bones"] = animationBones.Count, ["missing_library_bones"] = JsonKeys.Strings(missing),
            ["maximum_bind_position_error"] = positionError, ["maximum_bind_rotation_error_degrees"] = rotationError, ["sequences"] = sequences.Count, ["activities"] = activityCount,
            ["poseparameters"] = new JsonArray(MdlBones.ReadMdlPoseParams(animationPath).Select(pose => (JsonNode?)new JsonObject
            {
                ["name"] = pose.Name, ["start"] = pose.Start, ["end"] = pose.End, ["loop"] = pose.Loop
            }).ToArray()),
            ["status"] = "verified"
        };
        if (missing.Count > 0 || positionError > 1e-5 || rotationError > 0.01) throw new InvalidDataException("Playermodel and its generated animation library have different bind skeletons");
        var expectedSequences = profile == "female" ? 463 : 464;
        if (sequences.Count != expectedSequences || activityCount != 271) throw new InvalidDataException("Generated player animation graph does not match the published complete profile");
        if (stockMdl is null) return report;
        var stock = MdlBones.ReadMdlSequences(stockMdl);
        var names = sequences.Select(sequence => sequence.Label).ToHashSet(StringComparer.Ordinal);
        var wantedNames = stock.Select(sequence => sequence.Label).ToHashSet(StringComparer.Ordinal);
        var activities = sequences.Select(sequence => (sequence.Label, sequence.ActName, sequence.ActWeight)).ToHashSet();
        var wantedActivities = stock.Select(sequence => (sequence.Label, sequence.ActName, sequence.ActWeight)).ToHashSet();
        report["missing_sequences"] = JsonKeys.Strings(wantedNames.Except(names).Order(StringComparer.Ordinal));
        report["extra_sequences"] = JsonKeys.Strings(names.Except(wantedNames).Order(StringComparer.Ordinal));
        report["missing_activity_assignments"] = Activities(wantedActivities.Except(activities));
        report["extra_activity_assignments"] = Activities(activities.Except(wantedActivities));
        if (new[] { "missing_sequences", "extra_sequences", "missing_activity_assignments", "extra_activity_assignments" }.Any(key => report.Get(key).Bool()))
            throw new InvalidDataException("Generated animation library does not preserve the selected stock activity graph");
        return report;
    }
}
