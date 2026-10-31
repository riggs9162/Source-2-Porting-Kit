using System.Text.Json;
using System.Text.Json.Nodes;

namespace PortingKit.Addons.Porting;

/// <summary>Final helper-name collision pairs and durable source-part adaptation evidence.</summary>
public sealed record ContactPolicyResult(List<List<string>> Pairs, JsonObject Report);

/// <summary>Adapts inferred humanoid self-contact to Source 1 without changing authored joint flags.</summary>
public static class RagdollContacts
{
    private static readonly (string Role, string[] Aliases)[] HumanoidAliases =
    [
        ("pelvis", ["pelvis", "valvebiped.bip01_pelvis"]),
        ("chest", ["spine_2", "valvebiped.bip01_spine2"]),
        ("upper_arm_left", ["arm_upper_l", "valvebiped.bip01_l_upperarm"]),
        ("upper_arm_right", ["arm_upper_r", "valvebiped.bip01_r_upperarm"]),
        ("forearm_left", ["arm_lower_l", "valvebiped.bip01_l_forearm"]),
        ("forearm_right", ["arm_lower_r", "valvebiped.bip01_r_forearm"]),
        ("hand_left", ["hand_l", "valvebiped.bip01_l_hand"]),
        ("hand_right", ["hand_r", "valvebiped.bip01_r_hand"]),
        ("thigh_left", ["leg_upper_l", "valvebiped.bip01_l_thigh"]),
        ("thigh_right", ["leg_upper_r", "valvebiped.bip01_r_thigh"]),
        ("calf_left", ["leg_lower_l", "valvebiped.bip01_l_calf"]),
        ("calf_right", ["leg_lower_r", "valvebiped.bip01_r_calf"]),
        ("foot_left", ["ankle_l", "valvebiped.bip01_l_foot"]),
        ("foot_right", ["ankle_r", "valvebiped.bip01_r_foot"])
    ];

    /// <summary>Canonicalizes a non-self source-part pair.</summary>
    private static (int First, int Second) Pair(int first, int second)
    {
        if (first == second) throw new InvalidDataException("A source collision flag refers to the same body twice");
        return first < second ? (first, second) : (second, first);
    }

    /// <summary>Resolves source-part pairs without assuming source and compiled body indices match.</summary>
    private static JsonArray PairRecords(IEnumerable<(int First, int Second)> pairs, IReadOnlyDictionary<int, JsonObject> bodies) =>
        new(pairs.OrderBy(pair => pair.First).ThenBy(pair => pair.Second).Select(pair => (JsonNode?)Record(pair, bodies)).ToArray());

    private static JsonObject Record((int First, int Second) pair, IReadOnlyDictionary<int, JsonObject> bodies) => new()
    {
        ["parts"] = new JsonArray(pair.First, pair.Second),
        ["source_bones"] = new JsonArray(bodies[pair.First].Get("source_bone")?.DeepClone(), bodies[pair.Second].Get("source_bone")?.DeepClone()),
        ["helpers"] = new JsonArray(bodies[pair.First].Get("helper")?.DeepClone(), bodies[pair.Second].Get("helper")?.DeepClone())
    };

    /// <summary>Requires uniquely authored bilateral limb semantics before applying a humanoid policy.</summary>
    private static (JsonObject Roles, List<string> Unresolved) HumanoidRoles(IReadOnlyDictionary<int, JsonObject> bodies)
    {
        var roles = new JsonObject();
        var unresolved = new List<string>();
        foreach (var (role, aliases) in HumanoidAliases)
        {
            var candidates = bodies.Values.Where(body => aliases.Contains(NativePhysics.Fold(body.Get("source_bone").Str()))).ToList();
            if (candidates.Count != 1)
            {
                unresolved.Add($"{role}: {candidates.Count} matching source bodies");
                continue;
            }
            var body = candidates[0];
            roles[role] = new JsonObject
            {
                ["part"] = body.Get("part")?.DeepClone(), ["source_bone"] = body.Get("source_bone")?.DeepClone(), ["helper"] = body.Get("helper")?.DeepClone()
            };
        }
        return (roles, unresolved);
    }

    /// <summary>Keeps the tested thirteen human support contacts while avoiding internal pinching loops.</summary>
    private static HashSet<(int, int)> HumanoidPairs(JsonObject roles)
    {
        var pairs = new List<(string, string)>();
        foreach (var forearm in new[] { "forearm_left", "forearm_right" })
            foreach (var other in new[] { "pelvis", "chest", "thigh_left", "thigh_right" })
                pairs.Add((forearm, other));
        pairs.AddRange([("thigh_left", "thigh_right"), ("calf_left", "calf_right"), ("foot_left", "foot_right"), ("foot_left", "calf_right"), ("foot_right", "calf_left")]);
        return pairs.Select(pair => Pair(roles.Get(pair.Item1).Get("part").Int(), roles.Get(pair.Item2).Get("part").Int())).ToHashSet();
    }

    /// <summary>Reads an authored collision flag, which Python accepts as a boolean or as 0 or 1.</summary>
    private static bool Flag(JsonNode? joint)
    {
        if (!joint.Has("m_bEnableCollision")) return false;
        var value = joint.Get("m_bEnableCollision");
        if (value is JsonValue item)
        {
            if (item.GetValueKind() == JsonValueKind.True) return true;
            if (item.GetValueKind() == JsonValueKind.False) return false;
            if (item.TryGetValue<long>(out var number) && number is 0 or 1) return number == 1;
        }
        throw new InvalidDataException("Source joint collision flags must be boolean");
    }

    /// <summary>Adapts only inferred contacts; every explicit joint collision flag has precedence.</summary>
    /// <remarks>PHYS joints provide collision flags for their own endpoints rather than a complete pair table, and the
    /// original fallback enables all remaining body pairs. Recognized human rigs instead retain thirteen tested support
    /// contacts and any explicitly enabled joint pair. Unrecognized rigs retain the original inference. World and
    /// inter-entity collision are unaffected. Source-part indices and authored flags remain in the report.</remarks>
    public static ContactPolicyResult Derive(IReadOnlyList<JsonObject> bodies, IEnumerable<JsonNode?> sourceJoints)
    {
        var byPart = new Dictionary<int, JsonObject>();
        foreach (var body in bodies) byPart[body.Get("part").Int()] = body;
        if (byPart.Count != bodies.Count || bodies.Select(body => body.Get("helper").Str()).Distinct().Count() != bodies.Count)
            throw new InvalidDataException("Collision bodies require unique source parts and helper names");
        var flags = new JsonArray();
        var enabled = new HashSet<(int, int)>();
        var disabled = new HashSet<(int, int)>();
        var ordinal = -1;
        foreach (var joint in sourceJoints)
        {
            ordinal++;
            var pair = Pair(joint.Get("m_nBody1").Int(), joint.Get("m_nBody2").Int());
            if (!byPart.ContainsKey(pair.First) || !byPart.ContainsKey(pair.Second))
                throw new InvalidDataException("A source collision flag refers to an absent physics body");
            var value = Flag(joint);
            (value ? enabled : disabled).Add(pair);
            var generated = joint.Get("_generated").Bool();
            var authored = joint.Has("m_bEnableCollision") && !generated;
            var record = Record(pair, byPart);
            record["source_joint"] = ordinal;
            record["enabled"] = value;
            record["authored"] = authored;
            record["provenance"] = generated ? "generated_ao_proxy_joint" : authored ? "authored_phys_joint" : "default_joint_flag";
            flags.Add(record);
        }
        if (enabled.Overlaps(disabled)) throw new InvalidDataException("Conflicting source joint collision flags for the same body pair");
        var parts = byPart.Keys.Order().ToList();
        var allPairs = new HashSet<(int, int)>();
        foreach (var first in parts)
            foreach (var second in parts)
                if (first < second) allPairs.Add((first, second));
        var original = allPairs.Except(disabled).ToHashSet();
        var (roles, unresolved) = HumanoidRoles(byPart);
        var recognized = unresolved.Count == 0;
        var retained = recognized ? HumanoidPairs(roles).Union(enabled).Except(disabled).ToHashSet() : original;
        var excluded = original.Except(retained).ToHashSet();
        var records = PairRecords(retained, byPart);
        var report = new JsonObject
        {
            ["policy"] = recognized ? "source1_humanoid_semantic_v1" : "source_inferred_non_neighbor",
            ["status"] = excluded.Count > 0 ? "adapted" : "preserved_inference",
            ["rig_class"] = recognized ? "humanoid" : "unclassified",
            ["reason"] = recognized
                ? "Source 1 humanoid support contacts avoid unstable internal pinching loops; this replaces an inferred all-non-neighbor pair set, not an authored full pair table."
                : "Humanoid semantics are incomplete or ambiguous; retain the original pair inference.",
            ["world_and_other_entity_contacts"] = "unchanged",
            ["authored_joint_collision_flags_preserved"] = true,
            ["roles"] = recognized ? roles : new JsonObject(),
            ["unresolved_roles"] = NativePhysics.Strings(unresolved),
            ["possible_pair_count"] = allPairs.Count,
            ["original_inferred_pair_count"] = original.Count,
            ["retained_pair_count"] = retained.Count,
            ["excluded_inferred_pair_count"] = excluded.Count,
            ["source_joint_flags"] = flags,
            ["original_inferred_pairs"] = PairRecords(original, byPart),
            ["retained_pairs"] = records,
            ["excluded_inferred_pairs"] = PairRecords(excluded, byPart)
        };
        return new(records.Select(record => record.Get("helpers").Arr().Select(name => name.Str()).ToList()).ToList(), report);
    }

    /// <summary>Renames a native contact report for a companion rig without changing source provenance.</summary>
    public static JsonObject RemapReport(JsonObject report, IReadOnlyDictionary<string, string> helperNames)
    {
        var result = report.DeepClone().AsObject();
        foreach (var (_, role) in result.Get("roles").Obj())
            role!["helper"] = helperNames[role.Get("helper").Str()];
        foreach (var field in new[] { "source_joint_flags", "original_inferred_pairs", "retained_pairs", "excluded_inferred_pairs" })
            foreach (var pair in result.Get(field).Arr())
                pair!["helpers"] = NativePhysics.Strings(pair.Get("helpers").Arr().Select(name => helperNames[name.Str()]));
        return result;
    }
}
