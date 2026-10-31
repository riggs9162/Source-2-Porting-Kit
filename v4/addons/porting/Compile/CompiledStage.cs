using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>What must happen to a compiled model before it may ship.</summary>
public static class CompiledStage
{
    private static JsonObject Audited(JsonObject result, string failure)
    {
        if (result.Get("ok").Bool()) return result;
        throw new InvalidDataException(failure + ": " + string.Join("; ", result.Get("errors").Arr().Select(error => error.Str())));
    }

    /// <summary>Repairs what studiomdl loses, then audits the result against its sources.</summary>
    /// <remarks>The order matters. The flex rig goes in first, because the morph repair works on the patched model.
    /// Morphs and LOD material replacements are restored next, and both change the checksummed files, so
    /// those are verified again. Physics is then rebuilt to the authored convex hulls before the audits run, so
    /// what they measure is what ships. Any failing audit stops the model; a passing compile alone proves nothing.</remarks>
    public static async Task FinalizeAsync(WorkflowOptions options, JsonObject descriptor, string destination, string qc, string mdl, string payload,
        JsonArray physicsAudits, JsonArray modelAudits, PortContext context)
    {
        var report = descriptor.Get("report").Obj();
        var appearance = report.Get("appearance").Obj();
        var flexes = appearance.Get("flexes").Obj();
        var backups = Path.Combine(destination, "compiler-backups", Path.GetFileNameWithoutExtension(qc));
        await Task.Run(() =>
        {
            RagdollFlex.InstallSourceFlexRig(mdl, flexes);
            context.Check();
            var morphs = Audited(CompiledMorphGeometry.RepairCompiledMorphGeometry(mdl, qc, appearance, backups, context), "Compiled morph restoration failed");
            context.Check();
            var materials = Audited(CompiledMaterials.FinalizeLodMaterialReplacements(mdl, qc, Path.Combine(backups, "materials"), context), "Compiled LOD material restoration failed");
            context.Check();
            var check = Studiomdl.CheckCompiledModel(mdl);
            if (!check.Ok) throw new InvalidDataException(string.Join("; ", check.Problems()));
            if (report.Get("physics_audit_manifest").Str().Length > 0) physicsAudits.Add(FinalizePhysics(options, report, mdl, context));
            var audit = CompiledModelAudit.AuditCompiledModel(mdl, qc, report, payload);
            var details = new (string Key, JsonObject Value)[]
            {
                ("source_animation_timing", RagdollAnimationAudit.AuditSourceClipTiming(mdl, report)),
                ("source_flex_programs", RagdollAnimationAudit.AuditSourceFlexPrograms(mdl, flexes)),
                ("source_morph_geometry", CompiledMorphGeometry.AuditCompiledMorphGeometry(mdl, qc, appearance, context)),
                ("native_procedurals", ProceduralNative.AuditNativeProcedurals(mdl, appearance.Get("procedural").Get("native_procedurals").Arr(), BoneTable))
            };
            audit["morph_geometry_finalization"] = morphs;
            audit["lod_material_finalization"] = materials;
            var errors = audit.Get("errors").Arr();
            foreach (var (key, value) in details)
            {
                audit[key] = value;
                foreach (var error in value.Get("errors").Arr()) errors.Add(error?.DeepClone());
            }
            audit["ok"] = errors.Count == 0;
            modelAudits.Add(Audited(audit, "Compiled model audit failed"));
        }, context.Token);
    }

    /// <summary>The compiled bone table in the form the procedural audit reads.</summary>
    private static JsonObject BoneTable(string mdl) => new()
    {
        ["bones"] = new JsonArray(CompiledModelAudit.ReadCompiledModel(mdl).Bones.Select(bone => (JsonNode?)new JsonObject
        {
            ["name"] = bone.Name, ["parent"] = bone.Parent, ["flags"] = bone.Flags, ["procedural_type"] = bone.ProceduralType, ["procedural_offset"] = bone.ProceduralOffset
        }).ToArray())
    };

    private static JsonObject FinalizePhysics(WorkflowOptions options, JsonObject report, string mdl, PortContext context)
    {
        var prepared = PhyAudit.LoadAuditManifest(report.Get("physics_audit_manifest").Str());
        var phy = Path.ChangeExtension(mdl, ".phy");
        if (!File.Exists(phy)) throw new InvalidDataException($"Compiled ragdoll has no physics file: {mdl}");
        var serializer = Path.Combine(WorkflowPaths.Project(options), "calibration", "convex_serializer");
        var geometry = PhyGeometry.RebuildCompiledPhyGeometry(phy, prepared, options.Compiler, serializer, context);
        var masses = PhyAudit.FinalizeCompiledPhy(phy, prepared);
        var audit = Audited(PhyAudit.AuditCompiledPhy(phy, prepared, maximumBoundsErrorIn: 1e-4, maximumVolumeError: 1e-5, allowGeometryAdaptation: false), "Compiled physics audit failed");
        foreach (var adaptation in audit.Get("adaptations").Arr()) context.Warn(adaptation.Str());
        var result = new JsonObject { ["model"] = mdl, ["geometry_finalization"] = geometry, ["mass_finalization"] = masses };
        foreach (var (key, value) in audit) result[key] = value?.DeepClone();
        return result;
    }

    /// <summary>Copies generated runtime drivers and their data into the addon payload.</summary>
    /// <remarks>Each driver's name carries a hash of its model, so two models can never overwrite each other's script.</remarks>
    public static IEnumerable<string> CopyDrivers(JsonObject descriptor, string payload)
    {
        var written = new List<string>();
        var identity = Digest.Text(descriptor.Get("modelname").Str().ToLowerInvariant())[..16];
        foreach (var entry in descriptor.Get("report").Get("addon_drivers").Arr())
        {
            var source = entry.Str();
            if (!Path.GetExtension(source).Equals(".lua", StringComparison.OrdinalIgnoreCase) || !File.Exists(source)) throw new InvalidDataException($"Missing generated addon driver: {source}");
            var target = Path.Combine(payload, "lua", "autorun", $"s2kit_{identity}_{Path.GetFileName(source)}");
            Copy(source, target);
            written.Add(target);
            var manifest = Path.ChangeExtension(source, ".payload.json");
            if (!File.Exists(manifest)) continue;
            foreach (var dependency in JsonNode.Parse(File.ReadAllBytes(manifest)).Arr())
            {
                var path = dependency.Get("source").Str();
                var relative = Resources.Relative(dependency.Get("relative").Str());
                if (!File.Exists(path) || !relative.StartsWith("lua/s2kit/", StringComparison.Ordinal)) throw new InvalidDataException($"Invalid generated model driver dependency: {path}");
                var output = Path.Combine(payload, relative.Replace('/', Path.DirectorySeparatorChar));
                Copy(path, output);
                written.Add(output);
            }
        }
        return written;
    }

    private static void Copy(string source, string target)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(target)!);
        File.Copy(source, target, true);
    }
}
