using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>Converts native attachment and hitbox metadata with explicit Source 1 adaptations.</summary>
public static class RagdollAttachments
{
    /// <summary>Collects original influence arrays from nested MDAT objects without replacing earlier LOD definitions.</summary>
    private static Dictionary<string, JsonObject> AttachmentRecords(JsonNode? value)
    {
        var result = new Dictionary<string, JsonObject>(StringComparer.Ordinal);
        var pending = new Stack<JsonNode?>();
        pending.Push(value);
        while (pending.Count > 0)
        {
            var item = pending.Pop();
            if (item is JsonArray array)
            {
                for (var index = array.Count - 1; index >= 0; index--) pending.Push(array[index]);
            }
            else if (item is JsonObject map)
            {
                if (map.ContainsKey("m_influenceWeights") && map.ContainsKey("m_name")) result.TryAdd(map.Get("m_name").Str(), map);
                var children = map.Select(pair => pair.Value).ToList();
                for (var index = children.Count - 1; index >= 0; index--) pending.Push(children[index]);
            }
        }
        return result;
    }

    /// <summary>Resolves authored attachment influences from typed MDAT or its retained raw block sidecar.</summary>
    public static Dictionary<string, JsonObject> LoadAttachmentInfluences(JsonObject fidelity, string sourceDirectory)
    {
        if (fidelity.Get("source_mdat").Bool()) return AttachmentRecords(fidelity.Get("source_mdat"));
        var records = AttachmentRecords(fidelity.Get("source_data"));
        var reference = fidelity.Get("raw_blocks").Get("MDAT").Str();
        if (reference.Length > 0)
        {
            var path = Path.Combine(sourceDirectory, reference);
            if (File.Exists(path))
            {
                var documents = new JsonArray(Kv3.ParseDocuments(PythonText.ReadBomless(path)).Select(document => (JsonNode?)document).ToArray());
                foreach (var (name, record) in AttachmentRecords(documents)) records.TryAdd(name, record);
            }
        }
        return records;
    }

    private static JsonObject Describe(Attachment item) => new()
    {
        ["name"] = item.Name, ["bone"] = item.Bone, ["position"] = JsonKeys.Vector(item.Position), ["rotation"] = JsonKeys.Quaternion(item.Rotation),
        ["ignore_rotation"] = item.IgnoreRotation, ["root_transform"] = item.RootTransform, ["influences"] = item.Influences
    };

    private static JsonObject Describe(Hitbox item) => new()
    {
        ["set_name"] = item.SetName, ["name"] = item.Name, ["bone"] = item.Bone, ["group"] = item.Group, ["minimum"] = JsonKeys.Vector(item.Minimum),
        ["maximum"] = JsonKeys.Vector(item.Maximum), ["translation_only"] = item.TranslationOnly
    };

    /// <summary>Retains the strongest authored attachment influence and records every omitted influence.</summary>
    private static (Attachment Item, JsonObject Adaptation) DominantAttachment(Attachment item, JsonObject? raw)
    {
        if (raw is null)
            return (item with { Influences = 1 }, new JsonObject
            {
                ["name"] = item.Name, ["kind"] = "weighted_attachment", ["adaptation"] = "first_active_influence", ["selected_bone"] = item.Bone,
                ["source_influence_count"] = item.Influences,
                ["reason"] = "Full source influence arrays are unavailable; the typed sidecar retains its first active influence",
                ["source"] = Describe(item), ["animated_weighted_blending_preserved"] = false
            });
        var weights = raw.Get("m_influenceWeights").Doubles();
        var active = Enumerable.Range(0, weights.Length).Where(index => weights[index] > 0).ToList();
        if (active.Count == 0) throw new InvalidDataException($"Attachment {item.Name} has no positive influence");
        var chosen = active[0];
        foreach (var candidate in active)
            if (weights[candidate] > weights[chosen]) chosen = candidate;
        var names = raw.Get("m_influenceNames").Arr();
        var roots = raw.Get("m_bInfluenceRootTransform").Arr();
        var selected = item with
        {
            Bone = chosen < names.Count ? names[chosen].Str() : "",
            Position = raw.Need("m_vInfluenceOffsets").Arr()[chosen].Vec3(),
            Rotation = raw.Need("m_vInfluenceRotations").Arr()[chosen].Quat(),
            RootTransform = chosen < roots.Count && roots[chosen].Bool(),
            Influences = 1
        };
        return (selected, new JsonObject
        {
            ["name"] = item.Name, ["kind"] = "weighted_attachment", ["adaptation"] = "dominant_weight_influence", ["selected_index"] = chosen,
            ["selected_bone"] = selected.Bone, ["selected_weight"] = weights[chosen], ["source_influence_count"] = active.Count, ["source"] = raw.DeepClone(),
            ["reason"] = "Source 1 attachments follow one bone; all source influences remain in this report", ["animated_weighted_blending_preserved"] = false
        });
    }

    /// <summary>The bind frame an adaptation measures from, or the identity when the bone has no recorded frame.</summary>
    private static M4 BindWorld(JsonObject? frame) => frame.Has("source_bind_world") ? JsonKeys.Matrix(frame.Get("source_bind_world")) : M4.Identity;

    /// <summary>Preserves representable metadata and explicitly adapts weighted or rotation-independent fields.</summary>
    /// <remarks>An attachment that ignores bone rotation cancels the bind rotation, so it starts upright in model space and
    /// then follows the bone. A translation-only hitbox becomes the bone-local box enclosing its bind pose corners, which
    /// also rotates with the bone. Neither keeps its behaviour under animation, and the report says so per record.</remarks>
    public static (ModelMetadata Metadata, JsonObject Report) AdaptNativeMetadata(ModelMetadata metadata, JsonObject? sourceFrames = null,
        IReadOnlyDictionary<string, JsonObject>? rawAttachments = null)
    {
        var frames = sourceFrames ?? [];
        var attachments = new List<Attachment>();
        var hitboxes = new List<Hitbox>();
        var attachmentAdaptations = new List<JsonObject>();
        var hitboxAdaptations = new List<JsonObject>();
        foreach (var original in metadata.Attachments)
        {
            var item = original;
            if (item.Influences != 1)
            {
                (item, var adaptation) = DominantAttachment(item, rawAttachments?.GetValueOrDefault(item.Name));
                attachmentAdaptations.Add(adaptation);
            }
            if (item.IgnoreRotation)
            {
                var frame = frames.Get(item.Bone).Obj();
                var rotation = item.RootTransform ? M4.Identity : BindWorld(frame).Transposed();
                item = item with { Rotation = rotation.ToQuaternion(), IgnoreRotation = false };
                attachmentAdaptations.Add(new JsonObject
                {
                    ["name"] = item.Name, ["kind"] = "ignore_attachment_rotation", ["adaptation"] = "cancel_source_bind_rotation", ["source"] = Describe(original),
                    ["bind_orientation_preserved"] = frame.Count > 0 || item.RootTransform, ["animated_rotation_independence_preserved"] = false,
                    ["reason"] = "The Source 1 attachment retains model-space orientation at bind and subsequently follows its bone rotation"
                });
            }
            attachments.Add(item);
        }
        foreach (var original in metadata.Hitboxes)
        {
            var item = original;
            if (item.TranslationOnly)
            {
                var frame = frames.Get(item.Bone).Obj();
                var world = BindWorld(frame);
                V3 low = new(double.PositiveInfinity, double.PositiveInfinity, double.PositiveInfinity), high = -low;
                foreach (var x in new[] { item.Minimum.X, item.Maximum.X })
                    foreach (var y in new[] { item.Minimum.Y, item.Maximum.Y })
                        foreach (var z in new[] { item.Minimum.Z, item.Maximum.Z })
                        {
                            var corner = new V3(world[0, 0] * x + world[1, 0] * y + world[2, 0] * z, world[0, 1] * x + world[1, 1] * y + world[2, 1] * z, world[0, 2] * x + world[1, 2] * y + world[2, 2] * z);
                            low = new(Math.Min(low.X, corner.X), Math.Min(low.Y, corner.Y), Math.Min(low.Z, corner.Z));
                            high = new(Math.Max(high.X, corner.X), Math.Max(high.Y, corner.Y), Math.Max(high.Z, corner.Z));
                        }
                var authored = item.Maximum - item.Minimum;
                var exported = high - low;
                var originalVolume = authored.X * authored.Y * authored.Z;
                var exportVolume = exported.X * exported.Y * exported.Z;
                item = item with { Minimum = low, Maximum = high, TranslationOnly = false };
                hitboxAdaptations.Add(new JsonObject
                {
                    ["name"] = item.Name, ["bone"] = item.Bone, ["set_name"] = item.SetName, ["kind"] = "translation_only_hitbox", ["adaptation"] = "bone_local_enclosing_box_at_bind",
                    ["source"] = Describe(original), ["exported_minimum"] = JsonKeys.Vector(low), ["exported_maximum"] = JsonKeys.Vector(high),
                    ["source_bind_volume_in3"] = originalVolume, ["exported_volume_in3"] = exportVolume,
                    ["bind_volume_ratio"] = originalVolume > 0 ? exportVolume / originalVolume : null, ["source_bind_available"] = frame.Count > 0,
                    ["animated_rotation_independence_preserved"] = false,
                    ["reason"] = "The bone-local box encloses the source translation-only box at bind and subsequently rotates with its bone"
                });
            }
            hitboxes.Add(item);
        }
        var adaptedNames = attachmentAdaptations.Concat(hitboxAdaptations).Select(record => record.Get("name").Str()).ToHashSet(StringComparer.Ordinal);
        var warnings = metadata.Warnings.Where(warning => !adaptedNames.Any(name => warning.StartsWith(name + ":", StringComparison.Ordinal))).ToList();
        var result = new ModelMetadata
        {
            ModelName = metadata.ModelName, Surfaceprop = metadata.Surfaceprop, Attachments = attachments, Hitboxes = hitboxes, Animations = metadata.Animations,
            Sequences = metadata.Sequences, Weightlists = metadata.Weightlists, PoseParameters = metadata.PoseParameters, Warnings = warnings
        };
        var hitboxReport = new JsonObject
        {
            ["status"] = hitboxAdaptations.Count > 0 ? "adapted" : "preserved", ["count"] = hitboxes.Count, ["source_count"] = metadata.Hitboxes.Count,
            ["adaptations"] = RagdollPhysics.Nodes(hitboxAdaptations)
        };
        if (metadata.Hitboxes.Count == 0)
        {
            hitboxReport["status"] = "generated";
            hitboxReport["count"] = null;
            hitboxReport["reason"] = "No authored source hitboxes; Source 1 generates generic boxes from the native mesh";
        }
        var report = new JsonObject
        {
            ["attachments"] = new JsonObject
            {
                ["status"] = attachmentAdaptations.Count > 0 ? "adapted" : "preserved", ["count"] = attachments.Count, ["adaptations"] = RagdollPhysics.Nodes(attachmentAdaptations)
            },
            ["hitboxes"] = hitboxReport
        };
        return (result, report);
    }
}
