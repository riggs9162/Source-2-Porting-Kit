using System.Text.Json;
using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>Assembles a verified addon, then installs it without touching files the kit does not own.</summary>
public static class AddonPackager
{
    private static string Unique(string parent, string prefix) => Path.Combine(parent, $"{prefix}{DateTime.UtcNow.Ticks}");

    /// <summary>Activates a complete directory, restoring the previous version if the swap fails.</summary>
    private static string? ReplaceDirectory(string staging, string destination, string backupParent)
    {
        string? backup = null;
        if (Directory.Exists(destination))
        {
            backup = Unique(backupParent, Path.GetFileName(destination) + "-previous-");
            Directory.Move(destination, backup);
        }
        try
        {
            Directory.Move(staging, destination);
        }
        catch
        {
            if (backup is not null && Directory.Exists(backup) && !Directory.Exists(destination)) Directory.Move(backup, destination);
            throw;
        }
        return backup;
    }

    /// <summary>Removes only the uniquely named temporary tree this transaction created.</summary>
    private static void DiscardStaging(string path, string parent, string prefix)
    {
        if (!Directory.Exists(path)) return;
        if (!string.Equals(Path.GetDirectoryName(Path.GetFullPath(path)), Path.GetFullPath(parent).TrimEnd(Path.DirectorySeparatorChar), StringComparison.OrdinalIgnoreCase) ||
            !Path.GetFileName(path).StartsWith(prefix, StringComparison.Ordinal))
            throw new InvalidDataException($"Refusing to remove unexpected staging directory: {path}");
        Directory.Delete(path, true);
    }

    private static void CopyInto(string source, string target)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(target) ?? "");
        File.Copy(source, target, true);
    }

    private static string BuiltKind(WorkflowOptions options, PortAsset asset) =>
        Json.Load(Path.Combine(WorkflowPaths.Sources(options, asset), "build.json")).Get("kind").Str("ragdoll");

    /// <summary>The Lua lines that put one playermodel in Garry's Mod's player list.</summary>
    private static IEnumerable<string> PlayerRegistration(WorkflowOptions options, PortAsset asset)
    {
        var stem = Path.GetFileName(asset.Relative);
        var words = stem.Split('_', StringSplitOptions.RemoveEmptyEntries).Select(word => char.ToUpperInvariant(word[0]) + word[1..].ToLowerInvariant());
        var display = JsonSerializer.Serialize($"{string.Join(' ', words)} [{options.Namespace}]", Json.Options);
        var model = JsonSerializer.Serialize($"models/{WorkflowPaths.Model(options, asset)}_pm.mdl", Json.Options);
        return [$"player_manager.AddValidModel({display}, {model})", $"list.Set(\"PlayerOptionsModel\", {display}, {model})"];
    }

    /// <summary>Builds the addon completely before replacing the previous package.</summary>
    /// <remarks>Only models whose conversion, materials and compile all succeeded go in, and a static form goes in
    /// only when its own materials and compile succeeded, so a failed static build never costs the regular model.
    /// Every file is checked against the hash its stage recorded, so a payload edited after validation cannot ship.</remarks>
    public static IReadOnlyList<string> Package(WorkflowOptions options, IReadOnlyList<PortAsset> assets, WorkflowState state, PortContext context)
    {
        var project = WorkflowPaths.Project(options);
        var addon = Path.Combine(project, "addon");
        var successful = assets.Where(asset => new[] { "native", "materials", "compile" }.All(kind => state.Tasks.GetValueOrDefault($"{kind}:{asset.Key}")?.Status == "success")).ToList();
        if (successful.Count == 0) throw new InvalidDataException("No validated model with complete materials is available to package.");
        var staging = Unique(project, "addon-staging-");
        try
        {
            Directory.CreateDirectory(staging);
            var copied = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            var written = new List<string>();
            var registrations = new List<string>();
            foreach (var asset in successful)
            {
                context.Check();
                var payload = WorkflowPaths.Payload(options, asset);
                var owned = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                var stages = new List<string> { "materials", "compile" };
                if (state.Tasks.GetValueOrDefault($"static_materials:{asset.Key}")?.Status == "success" && state.Tasks.GetValueOrDefault($"static_compile:{asset.Key}")?.Status == "success")
                    stages.AddRange(["static_materials", "static_compile"]);
                var player = state.Tasks.GetValueOrDefault($"compile_pm:{asset.Key}")?.Status == "success";
                if (player)
                {
                    stages.Add("compile_pm");
                    registrations.AddRange(PlayerRegistration(options, asset));
                }
                foreach (var kind in stages)
                    foreach (var (name, digest) in state.Tasks[$"{kind}:{asset.Key}"].Outputs)
                    {
                        if (!WorkflowPaths.Within(name, payload)) continue;
                        if (owned.TryGetValue(name, out var existing) && existing != digest) throw new InvalidDataException($"Conflicting validated output hashes: {name}");
                        owned[name] = digest;
                    }
                foreach (var (source, expected) in owned.OrderBy(pair => pair.Key, StringComparer.Ordinal))
                {
                    context.Check();
                    var relative = Path.GetRelativePath(payload, source);
                    var digest = Digest.File(source);
                    if (digest != expected) throw new InvalidDataException($"Validated payload changed before packaging: {source}");
                    var name = relative.Replace('\\', '/');
                    if (copied.TryGetValue(name, out var previous))
                    {
                        if (previous != digest) throw new InvalidDataException($"Conflicting addon payload: {relative}");
                        continue;
                    }
                    var target = Path.Combine(staging, relative);
                    CopyInto(source, target);
                    if (Digest.File(target) != digest) throw new InvalidDataException($"Validated payload changed during packaging: {source}");
                    copied[name] = digest;
                    written.Add(target);
                }
                context.Log($"Packaged {asset.Relative}" + (player ? " and playermodel" : ""));
            }
            if (registrations.Count > 0)
            {
                var identity = Digest.Text(options.Namespace)[..16];
                var script = Path.Combine(staging, "lua", "autorun", $"s2kit_{identity}_playermodels.lua");
                Fmt.WriteText(script, "if ( SERVER ) then\n    AddCSLuaFile()\nend\n\n" + string.Join('\n', registrations) + "\n");
                written.Add(script);
            }
            var kinds = successful.Select(asset => BuiltKind(options, asset)).Distinct().ToList();
            var contents = kinds is ["ragdoll"] ? "ragdolls" : kinds is ["prop"] ? "props" : "models";
            var descriptor = Path.Combine(staging, "addon.json");
            Json.Save(descriptor, new JsonObject
            {
                ["title"] = options.Namespace.Replace('/', ' ') + " " + contents,
                ["type"] = "model",
                ["tags"] = new JsonArray("realism"),
                ["ignore"] = new JsonArray()
            });
            written.Add(descriptor);
            var files = new JsonObject();
            foreach (var path in written) files[Path.GetRelativePath(staging, path).Replace('\\', '/')] = Digest.File(path);
            var manifest = new JsonObject
            {
                ["version"] = 1,
                ["assets"] = new JsonArray(successful.Select(asset => (JsonNode?)new JsonObject
                {
                    ["key"] = asset.Key, ["source"] = asset.Source, ["resource"] = asset.Resource, ["relative"] = asset.Relative, ["material_root"] = asset.MaterialRoot
                }).ToArray()),
                ["files"] = files
            };
            context.Check();
            ReplaceDirectory(staging, addon, project);
            var manifestPath = Path.Combine(project, "addon-manifest.json");
            Json.Save(manifestPath, manifest);
            return [.. written.Select(path => Path.Combine(addon, Path.GetRelativePath(staging, path))), manifestPath];
        }
        finally
        {
            DiscardStaging(staging, project, "addon-staging-");
        }
    }

    private static bool IsLink(string path) => (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0;

    private static void CopyTree(string source, string target, PortContext context)
    {
        Directory.CreateDirectory(target);
        foreach (var directory in Directory.EnumerateDirectories(source, "*", SearchOption.AllDirectories))
            Directory.CreateDirectory(Path.Combine(target, Path.GetRelativePath(source, directory)));
        foreach (var file in Directory.EnumerateFiles(source, "*", SearchOption.AllDirectories))
        {
            context.Check();
            File.Copy(file, Path.Combine(target, Path.GetRelativePath(source, file)), true);
        }
    }

    /// <summary>Installs verified owned files, preserving unrelated files and the previous version.</summary>
    /// <remarks>An existing file is replaced only when it matches the new content or the hash this kit recorded
    /// when it last installed. Anything else is a user's edit or another addon's file, and installation stops.</remarks>
    public static IReadOnlyList<string> Install(WorkflowOptions options, PortContext context)
    {
        var project = WorkflowPaths.Project(options);
        var manifest = Json.Load(Path.Combine(project, "addon-manifest.json"));
        var addons = Path.GetFullPath(options.GmodAddons).TrimEnd(Path.DirectorySeparatorChar);
        var destination = Path.Combine(addons, "s2kit_" + options.Namespace.Replace('/', '_'));
        if (!string.Equals(Path.GetDirectoryName(destination), addons, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException($"Installation addon resolves outside its configured folder: {destination}");
        var ownedPath = Path.Combine(destination, "s2kit-installed.json");
        var previous = File.Exists(ownedPath) ? Json.Load(ownedPath).Get("files").Obj() : [];
        var files = manifest.Get("files").Obj();
        var inputs = new List<(string Relative, string Source, string Digest)>();
        foreach (var (relative, value) in files)
        {
            context.Check();
            var digest = value.Str();
            var path = Resources.Relative(relative);
            var source = Path.Combine(project, "addon", path);
            if (Digest.File(source) != digest) throw new InvalidDataException($"Packaged file changed before installation: {relative}");
            var target = Path.Combine(destination, path);
            if (!WorkflowPaths.Within(target, destination)) throw new InvalidDataException($"Installation target escapes its addon: {target}");
            if (Directory.Exists(target)) throw new InvalidDataException($"Installation would replace an existing directory or linked file: {target}");
            if (File.Exists(target))
            {
                if (IsLink(target)) throw new InvalidDataException($"Installation would replace an existing directory or linked file: {target}");
                var actual = Digest.File(target);
                if (actual != digest && previous.Get(relative).Str() != actual) throw new InvalidDataException($"Installation would overwrite an unowned or modified file: {target}");
            }
            inputs.Add((path, source, digest));
        }
        var parent = Path.GetDirectoryName(addons) ?? addons;
        var staging = Unique(parent, ".s2kit-install-staging-");
        try
        {
            if (Directory.Exists(destination)) CopyTree(destination, staging, context);
            else Directory.CreateDirectory(staging);
            foreach (var (relative, source, digest) in inputs)
            {
                context.Check();
                var target = Path.Combine(staging, relative);
                if (!WorkflowPaths.Within(target, staging)) throw new InvalidDataException($"Installation staging path escapes its addon: {relative}");
                CopyInto(source, target);
                if (Digest.File(target) != digest) throw new InvalidDataException($"Installation verification failed: {target}");
            }
            foreach (var (relative, value) in previous)
            {
                context.Check();
                if (files.ContainsKey(relative)) continue;
                var target = Path.Combine(staging, Resources.Relative(relative));
                if (!WorkflowPaths.Within(target, staging)) throw new InvalidDataException($"Previous installation path escapes its addon: {relative}");
                if (File.Exists(target) && !IsLink(target) && Digest.File(target) == value.Str()) File.Delete(target);
            }
            Json.Save(Path.Combine(staging, "s2kit-installed.json"), new JsonObject { ["version"] = 1, ["files"] = files.DeepClone() });
            context.Check();
            var backup = ReplaceDirectory(staging, destination, parent);
            if (backup is not null) context.Log($"Preserved previous addon under {backup}");
        }
        finally
        {
            DiscardStaging(staging, parent, ".s2kit-install-staging-");
        }
        List<string> written = [.. inputs.Select(input => Path.Combine(destination, input.Relative)), ownedPath];
        context.Log($"Installed and verified {written.Count} files under {destination}", LogLevel.Success);
        return written;
    }
}
