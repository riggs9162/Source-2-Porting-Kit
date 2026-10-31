using System.Text;

namespace PortingKit.Addons.Porting;

/// <summary>One <c>$bodygroup</c> slot: its name, its SMDs, and whether it may be blanked out.</summary>
public sealed record Bodygroup(string Name, List<string> Smds, bool AddBlank);

/// <summary>Folds per-mesh SMDs into bodygroup slots.</summary>
public static class GlbSkin
{
    private static readonly HashSet<string> SideTokens = new(["left", "right", "l", "r", "lt", "rt", "lh", "rh"], StringComparer.Ordinal);

    private static List<string> Tokens(string name) => name.ToLowerInvariant().Replace('-', '_').Split('_', StringSplitOptions.RemoveEmptyEntries).ToList();

    private static bool Digits(string token) => token.Length > 0 && token.All(char.IsDigit);

    /// <summary>Whether two mesh names look like variants of one interchangeable slot.</summary>
    /// <remarks>
    /// <para>True when one name is a token-prefix of the other (tank, tank_destroyed), when they first differ at a
    /// purely numeric token (helmet_default_00, helmet_default_01), or when they differ at exactly one token at
    /// either end (hazmat_forearm_left, zombie_forearm_left). Names diverging on a middle word stay apart.</para>
    /// <para>The end-token rule exists because Source 2 characters name outfit variants as a prefix. The Half-Life
    /// Alyx zombie ships 5 outfits by 3 limbs by 2 sides plus 6 bodies and 5 skins, and one slot per mesh produced
    /// 43 bodygroups. GMod's studiomdl refuses to compile 32 or more, and a model that slips past overflows the
    /// running product in the bodypart base value, crashing the engine on view. Grouping gives that zombie 10.</para>
    /// <para>Laterality words never merge: a slot's left and right halves are drawn together. What the names still
    /// share must name a slot rather than just a number, so head_01 and hands_01 stay separate pieces of gear.</para>
    /// </remarks>
    private static bool SameGroup(string a, string b)
    {
        List<string> first = Tokens(a), second = Tokens(b);
        var shared = 0;
        while (shared < first.Count && shared < second.Count && first[shared] == second[shared]) shared++;
        if (shared == first.Count || shared == second.Count) return true;
        if (Digits(first[shared]) && Digits(second[shared])) return true;
        if (first.Count != second.Count || first.Count < 2) return false;
        var different = Enumerable.Range(0, first.Count).Where(index => first[index] != second[index]).ToList();
        if (different.Count != 1 || (different[0] != 0 && different[0] != first.Count - 1)) return false;
        if (SideTokens.Contains(first[different[0]]) || SideTokens.Contains(second[different[0]])) return false;
        var rest = different[0] == 0 ? first.Skip(1) : first.Take(first.Count - 1);
        return rest.Any(token => !Digits(token));
    }

    /// <summary>Folds per-mesh SMDs into <c>$bodygroup</c> slots.</summary>
    /// <remarks>A Source bodypart with a single submodel is always drawn, so one group per mesh renders every helmet
    /// variant and both the intact and destroyed backpack at once, stacked inside each other. Variants of one slot
    /// are merged, and everything but the base body gains a blank, placed last to keep body index 0 on the first
    /// real variant. The heaviest slot is the character body itself and must never blank out.</remarks>
    public static List<Bodygroup> GroupBodygroups(IReadOnlyList<(string Name, string Smd, int VertCount)> sets)
    {
        var parent = Enumerable.Range(0, sets.Count).ToArray();
        int Find(int index)
        {
            while (parent[index] != index)
            {
                parent[index] = parent[parent[index]];
                index = parent[index];
            }
            return index;
        }
        for (var i = 0; i < sets.Count; i++)
            for (var j = i + 1; j < sets.Count; j++)
                if (SameGroup(sets[i].Name, sets[j].Name)) parent[Find(i)] = Find(j);
        var order = new List<int>();
        var members = new Dictionary<int, List<int>>();
        for (var i = 0; i < sets.Count; i++)
        {
            var root = Find(i);
            if (!members.TryGetValue(root, out var list))
            {
                members[root] = list = [];
                order.Add(root);
            }
            list.Add(i);
        }
        var heaviest = long.MinValue;
        var baseRoot = -1;
        foreach (var root in order)
        {
            var total = members[root].Sum(index => (long)sets[index].VertCount);
            if (total <= heaviest) continue;
            heaviest = total;
            baseRoot = root;
        }
        var groups = new List<Bodygroup>();
        foreach (var root in order)
        {
            var indices = members[root];
            var tokens = Tokens(sets[indices[0]].Name);
            foreach (var index in indices.Skip(1))
            {
                var other = Tokens(sets[index].Name);
                tokens = tokens.Where((token, at) => at < other.Count && other[at] == token).ToList();
            }
            var name = tokens.Count > 0 ? string.Join('_', tokens) : indices.Select(index => sets[index].Name).MinBy(item => item.Length)!;
            groups.Add(new(name, indices.Select(index => sets[index].Smd).ToList(), root != baseRoot));
        }
        return groups;
    }

    /// <summary>Renders bodygroups as QC <c>$bodygroup</c> blocks.</summary>
    public static List<string> BodygroupLines(IEnumerable<Bodygroup> groups) => groups.Select(group =>
    {
        var body = new StringBuilder();
        foreach (var smd in group.Smds) body.Append($"    studio \"{smd}\"\n");
        if (group.AddBlank) body.Append("    blank\n");
        return $"$bodygroup \"{group.Name}\"\n{{\n{body}}}";
    }).ToList();
}
