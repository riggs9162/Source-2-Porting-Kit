using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>Editable sources and measured feature decisions for a native ragdoll.</summary>
public sealed record RagdollPortResult(string Qc, List<string> Files, JsonObject Report, List<string> Warnings);

/// <summary>Assembles faithful native ragdoll sources from authored appearance and physics.</summary>
public static partial class RagdollPort
{
    [GeneratedRegex("[^a-zA-Z0-9_-]")]
    private static partial Regex UnsafeClipName();

    /// <summary>Writes complete animation samples in the prepared Source-inch skeleton.</summary>
    private static void WritePose(string path, GltfSkin skin, IReadOnlyList<JointPose[]> frames)
    {
        try
        {
            SmdSkeleton.WriteAnimation(skin, frames, path, new());
        }
        catch (InvalidDataException error)
        {
            throw new InvalidDataException($"{Path.GetFileName(path)}: {error.Message}", error);
        }
    }

    /// <summary>Gives distinct source clips portable files while retaining their QC names.</summary>
    private static string ClipFilename(int index, string name)
    {
        var safe = UnsafeClipName().Replace(name, "_").Trim('_');
        safe = safe.Length > 100 ? safe[..100] : safe;
        return $"anim_{index:D4}_{(safe.Length > 0 ? safe : "clip")}.smd";
    }

    private static bool IsSmd(string path) => Path.GetExtension(path).Equals(".smd", StringComparison.OrdinalIgnoreCase);

    /// <summary>Writes an original-sized native ragdoll with individually audited adaptations.</summary>
    /// <remarks>Rendering bones keep their names and world binds; physics inserts pivot-aligned helpers around them, so
    /// every SMD written before physics is remapped onto the combined skeleton. A model-space attachment gets a stationary
    /// origin bone, because no authored root can promise to stay put. Additive clips are written only when the export
    /// composed them over the bind pose, since a Source 1 sequence here cannot carry their delta encoding.</remarks>
    /// <param name="overrides">The project's loop choices; extra skins are not supported on ragdolls.</param>
    public static RagdollPortResult Port(string glb, string outDir, string modelname, string cdmaterials = "models/", bool includeSourceAnimations = false,
        double frictionScale = 1.0, PortContext? context = null, ModelOverrides? overrides = null)
    {
        overrides ??= ModelOverrides.None;
        glb = Path.GetFullPath(glb);
        outDir = Path.GetFullPath(outDir);
        Directory.CreateDirectory(outDir);
        var document = GltfDocument.Read(glb);
        var skin = GltfSkin.Parse(document) ?? throw new InvalidDataException("A ragdoll requires a skinned source model");
        var coord = CoordinateMode.For(document);
        var clips = includeSourceAnimations ? GltfClip.ParseAll(document, skin) : [];
        SmdSkeleton.ValidateNativeData(document, skin, clips);
        var metadata = ModelMetadata.Load(glb);
        var fidelity = ModelFidelity.Load(glb);
        context?.Check();
        var stem = PathText.Stem(glb);
        var physicsPath = Path.Combine(Path.GetDirectoryName(glb) ?? "", stem + "_phys.txt");
        var physics = RagdollPhysics.Prepare(File.Exists(physicsPath) ? physicsPath : null, skin, coord, fidelity.Need("source_data").Obj(), null, frictionScale);
        var appearance = RagdollAppearance.WriteAppearance(document, skin, coord, outDir, fidelity,
            new AppearanceOptions { Modelname = modelname, IncludeSourceAnimations = includeSourceAnimations, ProceduralSkin = physics.Skin }, context);
        var rawAttachments = metadata.Attachments.Exists(item => item.Influences != 1)
            ? RagdollAttachments.LoadAttachmentInfluences(fidelity, Path.GetDirectoryName(glb) ?? "") : null;
        var (adapted, metadataReport) = RagdollAttachments.AdaptNativeMetadata(metadata, appearance.AddonDrivers.Get("source_frames").Obj(), rawAttachments);
        metadata = adapted;
        var files = new List<string>(appearance.Files);
        foreach (var path in files.Where(IsSmd)) physics.RemapSmd(path);
        RagdollAppearance.SyncAppearanceSkeleton(appearance);
        var collision = Path.Combine(outDir, "collision.smd");
        var collisionLines = physics.WriteCollision(collision);
        files.Add(collision);
        var combined = physics.Skin;
        var bind = Path.Combine(outDir, "ragdoll.smd");
        WritePose(bind, combined, [combined.BindPose()]);
        files.Add(bind);
        var origin = "";
        if (metadata.Attachments.Exists(item => item.RootTransform))
        {
            if (combined.Joints.Count >= 256) throw new InvalidDataException("Model-space attachments need a compatibility bone within the 256-bone limit");
            origin = "__s2_model_origin";
            var taken = combined.Joints.Select(joint => joint.Name.ToLowerInvariant()).ToHashSet(StringComparer.Ordinal);
            while (taken.Contains(origin.ToLowerInvariant())) origin += "_";
            foreach (var path in files.Where(IsSmd)) SmdSkeleton.AddModelOriginBone(path, combined.Joints.Count, origin);
        }
        var lines = new List<string> { $"$modelname \"{modelname}\"", "$maxverts 65535", RagdollAppearance.CdmaterialsLine(cdmaterials, appearance.Report), "$surfaceprop \"flesh\"", "$contents \"solid\"" };
        lines.AddRange(appearance.QcLines);
        lines.AddRange(SmdSkeleton.DefineBoneLines(combined, new(), 90.0));
        lines.Add($"$root \"{physics.RootBone}\"");
        lines.AddRange(metadata.AttachmentQcLines(origin));
        lines.AddRange(metadata.HitboxQcLines());
        lines.AddRange(metadata.PoseParameterQcLines());
        lines.AddRange(metadata.WeightlistQcLines());
        lines.Add("$sequence \"ragdoll\" \"ragdoll.smd\" fps 30 activity \"ACT_DIERAGDOLL\" 1");
        if (origin.Length > 0) lines.Add($"$definebone \"{origin}\" \"\" 0 0 0 0 90 0 0 0 0 0 0 0");
        var warnings = new List<string>(physics.Warnings);
        warnings.AddRange(appearance.Report.Get("warnings").Arr().Select(warning => warning.Str()));
        foreach (var key in new[] { "attachments", "hitboxes" })
            foreach (var adaptation in metadataReport.Need(key).Need("adaptations").Arr())
                warnings.Add($"{adaptation.Need("name").Str()}: {adaptation.Need("reason").Str()}");
        var sequenceAudit = new JsonArray();
        var exported = new List<(string Name, string Target, int Frames, double Fps)>();
        var composed = fidelity.Get("animation_export").Get("additive_composed").Bool();
        for (var index = 0; index < clips.Count; index++)
        {
            context?.Check();
            var clip = clips[index];
            var authored = overrides.Apply(metadata.AnimationForClip(clip.Name) ?? throw new InvalidDataException($"Missing authored metadata for animation {clip.Name}"), clip.Name);
            if (authored.Delta && !composed)
            {
                warnings.Add($"{clip.Name}: source additive channels require explicit composition; clip omitted");
                sequenceAudit.Add(new JsonObject { ["name"] = clip.Name, ["status"] = "unsupported", ["reason"] = "additive_encoding" });
                continue;
            }
            var (frames, count, sampling) = clip.SampleAuthored(skin, authored);
            var target = Path.Combine(outDir, ClipFilename(index, clip.Name));
            WritePose(target, combined, frames.ConvertAll(frame => physics.RemapFrame(frame)));
            if (origin.Length > 0) SmdSkeleton.AddModelOriginBone(target, combined.Joints.Count, origin);
            files.Add(target);
            var name = !clip.Name.Equals("ragdoll", StringComparison.OrdinalIgnoreCase) ? clip.Name : "source_ragdoll";
            if (name.AsSpan().IndexOfAny("\"\r\n\\") >= 0) throw new InvalidDataException($"Unsafe source animation name: {PythonText.Repr(name)}");
            lines.Add($"$sequence \"{name}\" \"{Path.GetFileName(target)}\" " + string.Join(' ', ModelMetadata.SequenceQcOptions(authored, false)));
            sequenceAudit.Add(new JsonObject
            {
                ["name"] = clip.Name, ["target_name"] = name, ["status"] = authored.Delta ? "adapted" : "preserved", ["additive_composed_over_bind"] = authored.Delta,
                ["frames"] = count, ["fps"] = authored.Fps, ["loop"] = authored.Loop, ["sampling"] = sampling
            });
            exported.Add((clip.Name, name, count, authored.Fps));
            context?.Log($"Animation {index + 1}/{clips.Count}: {clip.Name}");
        }
        lines.AddRange(collisionLines);
        var qc = Path.Combine(outDir, stem + ".qc");
        PythonText.Write(qc, string.Join('\n', lines) + "\n");
        files.Add(qc);
        var distinctWarnings = warnings.Distinct(StringComparer.Ordinal).ToList();
        var animations = new JsonObject { ["requested"] = includeSourceAnimations, ["clips"] = sequenceAudit };
        var report = new JsonObject
        {
            ["version"] = 1, ["source"] = glb, ["modelname"] = modelname,
            ["original_bones"] = physics.Report.Has("original_bone_count") ? physics.Report.Get("original_bone_count")?.DeepClone() : skin.Joints.Count,
            ["compiled_bones"] = combined.Joints.Count + (origin.Length > 0 ? 1 : 0), ["physics"] = physics.Report.DeepClone(), ["appearance"] = appearance.Report.DeepClone(),
            ["material_map"] = appearance.Report.Get("material_map")?.DeepClone() ?? new JsonObject(), ["material_aliases"] = appearance.Report.Get("material_aliases")?.DeepClone() ?? new JsonObject(),
            ["animations"] = animations, ["attachments"] = metadataReport.Need("attachments")?.DeepClone(), ["hitboxes"] = metadataReport.Need("hitboxes")?.DeepClone(),
            ["engine_validation"] = "not_run", ["warnings"] = JsonKeys.Strings(distinctWarnings)
        };
        var auditManifest = PhyAudit.WriteAuditManifest(Path.Combine(outDir, "physics-audit-input.json"), physics);
        files.Add(auditManifest);
        report["physics_audit_manifest"] = auditManifest;
        if (includeSourceAnimations)
        {
            var names = exported.ToDictionary(item => item.Name, item => item.Target, StringComparer.Ordinal);
            var durations = exported.ToDictionary(item => item.Name, item => (item.Frames - 1) / item.Fps, StringComparer.Ordinal);
            var morphs = MorphAnimation.ExportMorphAnimations(document, names, durations, context: context);
            appearance.AddonDrivers["morph_animations"] = morphs.Clips;
            animations["morphs"] = morphs.Report;
        }
        if (appearance.AddonDrivers.Count > 0)
        {
            var driver = RagdollRuntime.WriteModelDriver(outDir, modelname, appearance.AddonDrivers);
            report["addon_drivers"] = JsonKeys.Strings([driver]);
            files.AddRange(RagdollRuntime.ModelDriverFiles(driver));
        }
        var reportPath = Path.Combine(outDir, "ragdoll-report.json");
        PythonText.Write(reportPath, PythonJson.Indented(report) + "\n");
        files.Add(reportPath);
        foreach (var warning in distinctWarnings) context?.Warn(warning);
        context?.Log($"Wrote {physics.Report.Get("source_body_count").Int()} physics bodies and {combined.Joints.Count} bones", LogLevel.Success);
        return new(qc, files, report, distinctWarnings);
    }
}
