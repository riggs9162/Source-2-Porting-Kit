using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>One authored attachment, local to its bone or to the model's root transform.</summary>
public sealed record Attachment(string Name, string Bone, V3 Position, Q4 Rotation, bool IgnoreRotation = false, bool RootTransform = false, int Influences = 1);

/// <summary>An authored bone-local hitbox with its set and damage group.</summary>
public sealed record Hitbox(string SetName, string Name, string Bone, int Group, V3 Minimum, V3 Maximum, bool TranslationOnly = false);

/// <summary>Clip timing and flags from ANIM. Delta describes the source encoding.</summary>
public sealed record AnimationMetadata(string Name, double Fps, int Frames, bool Loop, bool Delta, bool Hidden = false)
{
    public IReadOnlyList<(string Name, int Weight)> Activities { get; init; } = [];
    public string SourceName { get; init; } = "";
    public bool Snap { get; init; }
    public bool Autoplay { get; init; }
    public string Weightlist { get; init; } = "";
    public double FadeIn { get; init; } = 0.2;
    public double FadeOut { get; init; } = 0.2;
    public JsonArray Events { get; init; } = [];

    /// <summary>Whether two definitions agree on every field, including events.</summary>
    public bool Same(AnimationMetadata other) =>
        Name == other.Name && Fps == other.Fps && Frames == other.Frames && Loop == other.Loop && Delta == other.Delta && Hidden == other.Hidden &&
        Activities.SequenceEqual(other.Activities) && SourceName == other.SourceName && Snap == other.Snap && Autoplay == other.Autoplay &&
        Weightlist == other.Weightlist && FadeIn == other.FadeIn && FadeOut == other.FadeOut && Events.ToJsonString() == other.Events.ToJsonString();
}

/// <summary>Portable authored fields alongside diagnostics for data Source 1 cannot represent.</summary>
public sealed partial class ModelMetadata
{
    public string ModelName { get; set; } = "";
    public string Surfaceprop { get; set; } = "";
    public List<Attachment> Attachments { get; set; } = [];
    public List<Hitbox> Hitboxes { get; set; } = [];
    public Dictionary<string, AnimationMetadata> Animations { get; set; } = [];
    public List<JsonObject> Sequences { get; set; } = [];
    public Dictionary<string, Dictionary<string, double>> Weightlists { get; set; } = [];
    public List<JsonObject> PoseParameters { get; set; } = [];
    public List<string> Warnings { get; set; } = [];

    [GeneratedRegex(@"^[A-Za-z_][A-Za-z0-9_ ]*$")]
    private static partial Regex ActivityPattern();

    /// <summary>Matches an exact name, or an unambiguous leading-@ alias from the exporter.</summary>
    public AnimationMetadata? AnimationForClip(string name)
    {
        if (Animations.TryGetValue(name, out var exact)) return exact;
        var matches = Animations.Where(pair => pair.Key.TrimStart('@') == name.TrimStart('@')).Select(pair => pair.Value).ToList();
        return matches.Count == 1 ? matches[0] : null;
    }

    private static IEnumerable<JsonObject> Objects(JsonNode? value)
    {
        if (value is JsonObject map)
        {
            yield return map;
            foreach (var (_, child) in map)
                foreach (var nested in Objects(child)) yield return nested;
        }
        else if (value is JsonArray array)
        {
            foreach (var child in array)
                foreach (var nested in Objects(child)) yield return nested;
        }
    }

    private static V3 Vector3(JsonNode? value, string label)
    {
        var numbers = value.Doubles();
        if (numbers.Length != 3) throw new InvalidDataException($"{label} must contain 3 numbers.");
        if (!numbers.All(double.IsFinite)) throw new InvalidDataException($"{label} contains a non-finite number.");
        return new(numbers[0], numbers[1], numbers[2]);
    }

    private static Q4 Vector4(JsonNode? value, string label)
    {
        var numbers = value.Doubles();
        if (numbers.Length != 4) throw new InvalidDataException($"{label} must contain 4 numbers.");
        if (!numbers.All(double.IsFinite)) throw new InvalidDataException($"{label} contains a non-finite number.");
        return new(numbers[0], numbers[1], numbers[2], numbers[3]);
    }

    private static Attachment Authored(JsonNode? data)
    {
        var label = data.Get("m_name").Str();
        var names = data.Get("m_influenceNames").Arr();
        var weights = data.Get("m_influenceWeights").Arr();
        var roots = data.Get("m_bInfluenceRootTransform").Arr();
        var active = new List<int>();
        for (var index = 0; index < weights.Count; index++)
        {
            var weight = weights[index].Dbl();
            if (!double.IsFinite(weight) || weight < 0) throw new InvalidDataException($"Attachment '{label}' has an invalid influence weight.");
            if (weight <= 0) continue;
            var named = index < names.Count && names[index].Bool();
            var root = index < roots.Count && roots[index].Bool();
            if (!named && !root) throw new InvalidDataException($"Attachment '{label}' has an unnamed weighted bone.");
            active.Add(index);
        }
        if (active.Count == 0) throw new InvalidDataException($"Attachment '{label}' has no weighted influence.");
        var first = active[0];
        return new(label, first < names.Count ? names[first].Str() : "",
            Vector3(data.Get("m_vInfluenceOffsets").Arr()[first], "Attachment offset"),
            Vector4(data.Get("m_vInfluenceRotations").Arr()[first], "Attachment quaternion"),
            data.Get("m_bIgnoreRotation").Bool(), first < roots.Count && roots[first].Bool(), active.Count);
    }

    private void AddUnique<T>(List<T> items, T value, Func<T, object> key) where T : notnull
    {
        var existing = items.FirstOrDefault(item => key(item).Equals(key(value)));
        if (existing is null)
        {
            items.Add(value);
            return;
        }
        if (existing.Equals(value)) return;
        var warning = $"Conflicting metadata for '{key(value)}'; keeping the first authored definition";
        if (!Warnings.Contains(warning)) Warnings.Add(warning);
    }

    private static IReadOnlyList<(string, int)> Activity(JsonNode? entries) =>
        entries.Arr().Select(item => (item.Get("m_name").Str(), item.Has("m_nWeight") ? item.Get("m_nWeight").Int() : 1)).ToList();

    /// <summary>Collects authored fields from block dumps, deduplicating copies repeated across LOD meshes.</summary>
    public static ModelMetadata Parse(string dataText = "", string mdatText = "", string animText = "", string aseqText = "")
    {
        var result = new ModelMetadata();
        foreach (var text in new[] { dataText, mdatText, animText, aseqText })
            foreach (var document in Kv3.ParseDocuments(text))
            {
                if (document.ContainsKey("m_modelInfo"))
                {
                    result.ModelName = document.Get("m_name").Str();
                    result.Surfaceprop = document.Get("m_modelInfo").Get("m_sSurfaceProperty").Str();
                }
                foreach (var entry in Objects(document).ToList()) result.Collect(entry);
            }
        result.ResolveSequences();
        return result;
    }

    private void Collect(JsonObject item)
    {
        foreach (var entry in item.Get("m_attachments").Arr())
            AddUnique(Attachments, Authored(entry.Has("value") ? entry.Get("value") : entry), attachment => attachment.Name);
        foreach (var entry in item.Get("m_hitboxsets").Arr())
        {
            var hitset = entry.Has("value") ? entry.Get("value") : entry;
            var setName = hitset.Has("m_name") ? hitset.Get("m_name").Str() : entry.Has("key") ? entry.Get("key").Str() : "default";
            foreach (var box in hitset.Get("m_HitBoxes").Arr())
            {
                var hitbox = new Hitbox(setName, box.Get("m_name").Str(), box.Get("m_sBoneName").Str(), box.Get("m_nGroupId").Int(),
                    Vector3(box.Get("m_vMinBounds"), "Hitbox minimum"), Vector3(box.Get("m_vMaxBounds"), "Hitbox maximum"),
                    box.Get("m_bTranslationOnly").Bool());
                AddUnique(Hitboxes, hitbox, value => (value.SetName, value.Name, value.Bone));
            }
        }
        foreach (var clip in item.Get("m_animArray").Arr())
        {
            var flags = clip.Get("m_flags");
            var name = clip.Get("m_name").Str();
            var animation = new AnimationMetadata(name, clip.Get("fps").Dbl(30.0), clip.Get("m_pData").Get("m_nFrames").Int(),
                flags.Get("m_bLooping").Bool(), flags.Get("m_bDelta").Bool(), flags.Get("m_bHidden").Bool())
            {
                Activities = Activity(clip.Get("m_activityArray")),
                SourceName = name,
                Events = (JsonArray)clip.Get("m_eventArray").Arr().DeepClone()
            };
            if (!double.IsFinite(animation.Fps) || animation.Fps <= 0) throw new InvalidDataException($"Invalid authored fps for '{name}'.");
            if (Animations.TryGetValue(name, out var previous) && !previous.Same(animation)) Warnings.Add($"Conflicting animation metadata for '{name}'");
            else Animations[name] = animation;
            if (animation.Events.Count > 0) Warnings.Add($"{name}: Source 2 animation events require manual translation");
        }
        foreach (var sequence in item.Get("m_localS1SeqDescArray").Arr())
        {
            var names = item.Get("m_localSequenceNameArray").Arr();
            var sources = new JsonArray(sequence.Get("m_fetch").Get("m_localReferenceArray").Arr().Select(index => (JsonNode?)names[index.Int()].Str()).ToArray());
            var masks = item.Get("m_localBoneMaskArray").Arr();
            var maskName = masks.Count > 0 ? masks[sequence.Get("m_nLocalWeightlist").Int()].Get("m_sName").Str() : "";
            var copy = sequence.Obj().DeepClone().AsObject();
            copy["source_animations"] = sources;
            copy["weightlist"] = maskName;
            Sequences.Add(copy);
        }
        foreach (var mask in item.Get("m_localBoneMaskArray").Arr())
        {
            var boneNames = item.Get("m_localBoneNameArray").Arr();
            var indices = mask.Get("m_nLocalBoneArray").Arr();
            var weights = mask.Get("m_flBoneWeightArray").Arr();
            if (indices.Count != weights.Count) throw new InvalidDataException("Bone mask indices and weights have different lengths.");
            var map = new Dictionary<string, double>();
            for (var index = 0; index < indices.Count; index++) map[boneNames[indices[index].Int()].Str()] = weights[index].Dbl();
            Weightlists[mask.Get("m_sName").Str()] = map;
        }
        PoseParameters.AddRange(item.Get("m_localPoseParamArray").Arr().Select(entry => entry.Obj().DeepClone().AsObject()));
    }

    private void ResolveSequences()
    {
        foreach (var sequence in Sequences)
        {
            var name = sequence.Get("m_sName").Str();
            var sources = sequence.Get("source_animations").Arr();
            if (sources.Count != 1 || !Animations.TryGetValue(sources[0].Str(), out var source))
            {
                Warnings.Add($"{name}: sequence blend or missing animation needs manual authoring");
                continue;
            }
            var flags = sequence.Get("m_flags");
            var transition = sequence.Get("m_transition");
            var activities = Activity(sequence.Get("m_activityArray"));
            var weightlist = sequence.Get("weightlist").Str();
            Animations[name] = source with
            {
                Name = name,
                Loop = flags.Has("m_bLooping") ? flags.Get("m_bLooping").Bool() : source.Loop,
                Hidden = flags.Has("m_bHidden") ? flags.Get("m_bHidden").Bool() : source.Hidden,
                Delta = flags.Has("m_bLegacyDelta") ? flags.Get("m_bLegacyDelta").Bool() : source.Delta,
                Snap = flags.Get("m_bSnap").Bool(),
                Autoplay = flags.Get("m_bAutoplay").Bool(),
                Weightlist = Weightlists.TryGetValue(weightlist, out var weights) && weights.Count > 0 ? weightlist : "",
                FadeIn = transition.Get("m_flFadeInTime").Dbl(0.2),
                FadeOut = transition.Get("m_flFadeOutTime").Dbl(0.2),
                Activities = activities.Count > 0 ? activities : source.Activities
            };
        }
        foreach (var attachment in Attachments)
        {
            if (attachment.Influences != 1 || attachment.IgnoreRotation) Warnings.Add($"{attachment.Name}: attachment transform needs manual Source 1 authoring");
            else if (attachment.RootTransform) Warnings.Add($"{attachment.Name}: model-space attachment requires a stationary identity root bone");
        }
        foreach (var hitbox in Hitboxes)
            if (hitbox.TranslationOnly) Warnings.Add($"{hitbox.Name}: translation-only hitbox has no direct QC equivalent");
    }

    private static JsonArray Numbers(params double[] values) => new(values.Select(value => (JsonNode?)value).ToArray());

    /// <summary>Writes the compact sidecar, without geometry or encoded animation.</summary>
    public void Write(string path)
    {
        var animations = new JsonObject();
        foreach (var (name, item) in Animations)
            animations[name] = new JsonObject
            {
                ["name"] = item.Name, ["fps"] = item.Fps, ["frames"] = item.Frames, ["loop"] = item.Loop, ["delta"] = item.Delta, ["hidden"] = item.Hidden,
                ["activities"] = new JsonArray(item.Activities.Select(activity => (JsonNode?)new JsonArray(activity.Name, activity.Weight)).ToArray()),
                ["source_name"] = item.SourceName, ["snap"] = item.Snap, ["autoplay"] = item.Autoplay, ["weightlist"] = item.Weightlist,
                ["fade_in"] = item.FadeIn, ["fade_out"] = item.FadeOut, ["events"] = item.Events.DeepClone()
            };
        var weightlists = new JsonObject();
        foreach (var (name, weights) in Weightlists)
        {
            var map = new JsonObject();
            foreach (var (bone, weight) in weights) map[bone] = weight;
            weightlists[name] = map;
        }
        Json.Save(path, new JsonObject
        {
            ["version"] = 1, ["model_name"] = ModelName, ["surfaceprop"] = Surfaceprop,
            ["attachments"] = new JsonArray(Attachments.Select(item => (JsonNode?)new JsonObject
            {
                ["name"] = item.Name, ["bone"] = item.Bone, ["position"] = Numbers(item.Position.X, item.Position.Y, item.Position.Z),
                ["rotation"] = Numbers(item.Rotation.X, item.Rotation.Y, item.Rotation.Z, item.Rotation.W), ["ignore_rotation"] = item.IgnoreRotation,
                ["root_transform"] = item.RootTransform, ["influences"] = item.Influences
            }).ToArray()),
            ["hitboxes"] = new JsonArray(Hitboxes.Select(item => (JsonNode?)new JsonObject
            {
                ["set_name"] = item.SetName, ["name"] = item.Name, ["bone"] = item.Bone, ["group"] = item.Group,
                ["minimum"] = Numbers(item.Minimum.X, item.Minimum.Y, item.Minimum.Z), ["maximum"] = Numbers(item.Maximum.X, item.Maximum.Y, item.Maximum.Z),
                ["translation_only"] = item.TranslationOnly
            }).ToArray()),
            ["animations"] = animations,
            ["sequences"] = new JsonArray(Sequences.Select(item => item.DeepClone()).ToArray()),
            ["weightlists"] = weightlists,
            ["poseparameters"] = new JsonArray(PoseParameters.Select(item => item.DeepClone()).ToArray()),
            ["warnings"] = new JsonArray(Warnings.Select(item => (JsonNode?)item).ToArray())
        });
    }

    /// <summary>Loads a sidecar path, or the sidecar beside a render glTF.</summary>
    public static ModelMetadata Load(string path)
    {
        if (Path.GetExtension(path).ToLowerInvariant() is ".glb" or ".gltf") path = Path.ChangeExtension(path, ".metadata.json");
        var data = Json.Load(path);
        if (data.Get("version").Int() != 1) throw new InvalidDataException("Unsupported model metadata sidecar version.");
        var result = new ModelMetadata
        {
            ModelName = data.Get("model_name").Str(),
            Surfaceprop = data.Get("surfaceprop").Str(),
            Attachments = data.Get("attachments").Arr().Select(item => new Attachment(item.Get("name").Str(), item.Get("bone").Str(), item.Get("position").Vec3(),
                item.Get("rotation").Quat(), item.Get("ignore_rotation").Bool(), item.Get("root_transform").Bool(), item.Get("influences").Int(1))).ToList(),
            Hitboxes = data.Get("hitboxes").Arr().Select(item => new Hitbox(item.Get("set_name").Str(), item.Get("name").Str(), item.Get("bone").Str(), item.Get("group").Int(),
                item.Get("minimum").Vec3(), item.Get("maximum").Vec3(), item.Get("translation_only").Bool())).ToList(),
            Sequences = data.Get("sequences").Arr().Select(item => item.Obj().DeepClone().AsObject()).ToList(),
            PoseParameters = data.Get("poseparameters").Arr().Select(item => item.Obj().DeepClone().AsObject()).ToList(),
            Warnings = data.Get("warnings").Arr().Select(item => item.Str()).ToList()
        };
        foreach (var (name, item) in data.Get("animations").Obj())
            result.Animations[name] = new(item.Get("name").Str(), item.Get("fps").Dbl(), item.Get("frames").Int(), item.Get("loop").Bool(), item.Get("delta").Bool(), item.Get("hidden").Bool())
            {
                Activities = item.Get("activities").Arr().Select(activity => (activity.Arr()[0].Str(), activity.Arr()[1].Int())).ToList(),
                SourceName = item.Get("source_name").Str(),
                Snap = item.Get("snap").Bool(),
                Autoplay = item.Get("autoplay").Bool(),
                Weightlist = item.Get("weightlist").Str(),
                FadeIn = item.Get("fade_in").Dbl(0.2),
                FadeOut = item.Get("fade_out").Dbl(0.2),
                Events = (JsonArray)item.Get("events").Arr().DeepClone()
            };
        foreach (var (name, weights) in data.Get("weightlists").Obj())
            result.Weightlists[name] = weights.Obj().ToDictionary(pair => pair.Key, pair => pair.Value.Dbl());
        return result;
    }

    /// <summary>Quotes a name for QC, refusing characters a QC token cannot carry.</summary>
    public static string Quoted(string value)
    {
        if (value.AsSpan().IndexOfAny("\"\n\r\\") >= 0) throw new InvalidDataException($"Name cannot be represented safely in QC: {value}");
        return $"\"{value}\"";
    }

    /// <summary>Emits attachments in source-local inches and QAngle degrees.</summary>
    /// <param name="modelSpaceBone">An identity-bind root that stays unanimated in every sequence. Existing
    /// skeleton roots are never chosen implicitly because they can move.</param>
    public List<string> AttachmentQcLines(string modelSpaceBone = "")
    {
        var lines = new List<string>();
        foreach (var item in Attachments)
        {
            if (item.Influences != 1 || item.IgnoreRotation) throw new InvalidDataException($"Attachment '{item.Name}' needs manual Source 1 authoring.");
            if (item.RootTransform && modelSpaceBone.Length == 0) throw new InvalidDataException($"Attachment '{item.Name}' needs an explicit stationary model-space bone.");
            var bone = item.RootTransform ? modelSpaceBone : item.Bone;
            if (bone.Length == 0) throw new InvalidDataException($"Attachment '{item.Name}' has no bone.");
            var (pitch, yaw, roll) = item.Rotation.ToQAngle();
            lines.Add($"$attachment {Quoted(item.Name)} {Quoted(bone)} {Fmt.F6(item.Position.X)} {Fmt.F6(item.Position.Y)} {Fmt.F6(item.Position.Z)} rotate {Fmt.F6(pitch)} {Fmt.F6(yaw)} {Fmt.F6(roll)}");
        }
        return lines;
    }

    /// <summary>Emits hitbox sets using the six-bound syntax shared by compilers.</summary>
    /// <remarks>Display names stay in the sidecar: Source 2013 accepts a name here, while CS:GO-derived
    /// StudioMDL CE expects additional numeric shape fields.</remarks>
    public List<string> HitboxQcLines()
    {
        var lines = new List<string>();
        string? active = null;
        foreach (var item in Hitboxes)
        {
            if (item.TranslationOnly) throw new InvalidDataException($"Hitbox '{item.Name}' is translation-only.");
            if (item.SetName != active)
            {
                lines.Add($"$hboxset {Quoted(item.SetName)}");
                active = item.SetName;
            }
            lines.Add($"$hbox {item.Group} {Quoted(item.Bone)} {Fmt.F6(item.Minimum.X)} {Fmt.F6(item.Minimum.Y)} {Fmt.F6(item.Minimum.Z)} {Fmt.F6(item.Maximum.X)} {Fmt.F6(item.Maximum.Y)} {Fmt.F6(item.Maximum.Z)}");
        }
        return lines;
    }

    /// <summary>Emits authored bone masks; unspecified bones start at zero weight.</summary>
    public List<string> WeightlistQcLines()
    {
        var lines = new List<string>();
        foreach (var (name, weights) in Weightlists)
        {
            if (weights.Count == 0) continue;
            lines.Add($"$weightlist {Quoted(name)} {{");
            lines.AddRange(weights.Select(pair => $"    {Quoted(pair.Key)} {Fmt.G(pair.Value)}"));
            lines.Add("}");
        }
        return lines;
    }

    /// <summary>Declares authored pose-parameter ranges without inventing blend graphs.</summary>
    public List<string> PoseParameterQcLines()
    {
        var lines = new List<string>();
        foreach (var item in PoseParameters)
        {
            var loop = item.Get("m_flLoop").Dbl();
            var suffix = loop != 0 ? $" loop {Fmt.G(loop)}" : item.Get("m_bLooping").Bool() ? " wrap" : "";
            lines.Add($"$poseparameter {Quoted(item.Get("m_sName").Str())} {Fmt.G(item.Get("m_flStart").Dbl())} {Fmt.G(item.Get("m_flEnd").Dbl())}{suffix}");
        }
        return lines;
    }

    /// <summary>Returns authored sequence flags; callers control exported delta encoding.</summary>
    public static List<string> SequenceQcOptions(AnimationMetadata animation, bool includeDelta = true)
    {
        var options = new List<string> { $"fps {Fmt.G(animation.Fps)}" };
        if (animation.Loop) options.Add("loop");
        if (animation.Hidden) options.Add("hidden");
        if (animation.Snap) options.Add("snap");
        if (animation.Autoplay) options.Add("autoplay");
        if (animation.Delta && includeDelta) options.Add("delta");
        if (animation.Weightlist.Length > 0) options.Add($"weightlist {Quoted(animation.Weightlist)}");
        options.Add($"fadein {Fmt.G(animation.FadeIn)}");
        options.Add($"fadeout {Fmt.G(animation.FadeOut)}");
        if (animation.Activities.Count > 0)
        {
            var (name, weight) = animation.Activities[0];
            if (!ActivityPattern().IsMatch(name)) throw new InvalidDataException($"Unsafe activity identifier: {name}");
            options.Add($"activity {Quoted(name)} {weight}");
        }
        return options;
    }
}
