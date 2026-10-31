namespace PortingKit.Addons.Porting;

/// <summary>The Source 2 to ValveBiped bone-name map and the canonical ValveBiped set.</summary>
/// <remarks>Reference data, not policy. The map says what a Source 2 bone is called in Source 1; <see cref="Bones"/> says
/// which of those names an animation library and every ValveBiped-aware addon actually look for.</remarks>
public static class ValveBiped
{
    /// <summary>Source 2 humanoid bone name to ValveBiped (Source 1) bone name.</summary>
    /// <remarks>
    /// <para>ValveBiped's FingerN, N1 and N2 are the three phalanges; the metacarpals live inside the palm and have no
    /// ValveBiped counterpart. Mapping a metacarpal onto FingerN shifts every finger one bone toward the palm and drops the
    /// fingertip entirely: it puts a 0.69 unit stub onto a 3.91 unit phalanx, a 5.6x length mismatch. So the metacarpals are
    /// named Finger{N}m, which lands outside the canonical set: they survive as weight-bearing leaves while their phalanges
    /// reparent to the hand.</para>
    /// <para>The helper and deform bones are not in any HL2 animation library, so they are never canonical, but naming them
    /// gives the kept helpers proper identities. They carry real skin weight: on the metrocop the arm and leg twists dominate
    /// about 6000 vertices, and folding them away collapses the twist deformation into the parent bone.</para>
    /// <para>Helper names follow a hand-authored reference rig, because <see cref="ProceduralBones"/> keys its twist rules off
    /// them: Pect, Hip, Quad, Scap, Tibia and Soleus rather than the longer forms. A rule whose helper it cannot find is
    /// skipped, so a spelling that drifts here silently costs the rig its twist deformation.</para>
    /// <para>The lower-leg twists have the forearm's two-stage shape, the far one taking most of the ankle's roll.
    /// <c>scap_0</c> maps to Trapezius; the tip bone is the scapula proper.</para>
    /// <para><c>mini_root</c> is the HLA zombie's headcrab root. z_anm animates HC_Body_Bone parented to Spine4, so mapping the
    /// crab root to it gives an authentic crab wiggle instead of a subtree that dead-ends during fold resolution.</para>
    /// </remarks>
    public static readonly IReadOnlyDictionary<string, string> Source2ToValveBiped = new Dictionary<string, string>(StringComparer.Ordinal)
    {
        ["pelvis"] = "ValveBiped.Bip01_Pelvis",

        ["spine_0"] = "ValveBiped.Bip01_Spine",
        ["spine_1"] = "ValveBiped.Bip01_Spine1",
        ["spine_2"] = "ValveBiped.Bip01_Spine2",
        ["spine_3"] = "ValveBiped.Bip01_Spine4",

        ["neck_0"] = "ValveBiped.Bip01_Neck1",
        ["head"] = "ValveBiped.Bip01_Head1",

        ["clavicle_L"] = "ValveBiped.Bip01_L_Clavicle",
        ["arm_upper_L"] = "ValveBiped.Bip01_L_UpperArm",
        ["arm_lower_L"] = "ValveBiped.Bip01_L_Forearm",
        ["hand_L"] = "ValveBiped.Bip01_L_Hand",

        ["finger_thumb_0_L"] = "ValveBiped.Bip01_L_Finger0",
        ["finger_thumb_1_L"] = "ValveBiped.Bip01_L_Finger01",
        ["finger_thumb_2_L"] = "ValveBiped.Bip01_L_Finger02",

        ["finger_index_0_L"] = "ValveBiped.Bip01_L_Finger1",
        ["finger_index_1_L"] = "ValveBiped.Bip01_L_Finger11",
        ["finger_index_2_L"] = "ValveBiped.Bip01_L_Finger12",

        ["finger_middle_0_L"] = "ValveBiped.Bip01_L_Finger2",
        ["finger_middle_1_L"] = "ValveBiped.Bip01_L_Finger21",
        ["finger_middle_2_L"] = "ValveBiped.Bip01_L_Finger22",

        ["finger_ring_0_L"] = "ValveBiped.Bip01_L_Finger3",
        ["finger_ring_1_L"] = "ValveBiped.Bip01_L_Finger31",
        ["finger_ring_2_L"] = "ValveBiped.Bip01_L_Finger32",

        ["finger_pinky_0_L"] = "ValveBiped.Bip01_L_Finger4",
        ["finger_pinky_1_L"] = "ValveBiped.Bip01_L_Finger41",
        ["finger_pinky_2_L"] = "ValveBiped.Bip01_L_Finger42",

        ["clavicle_R"] = "ValveBiped.Bip01_R_Clavicle",
        ["arm_upper_R"] = "ValveBiped.Bip01_R_UpperArm",
        ["arm_lower_R"] = "ValveBiped.Bip01_R_Forearm",
        ["hand_R"] = "ValveBiped.Bip01_R_Hand",

        ["finger_thumb_0_R"] = "ValveBiped.Bip01_R_Finger0",
        ["finger_thumb_1_R"] = "ValveBiped.Bip01_R_Finger01",
        ["finger_thumb_2_R"] = "ValveBiped.Bip01_R_Finger02",

        ["finger_index_0_R"] = "ValveBiped.Bip01_R_Finger1",
        ["finger_index_1_R"] = "ValveBiped.Bip01_R_Finger11",
        ["finger_index_2_R"] = "ValveBiped.Bip01_R_Finger12",

        ["finger_middle_0_R"] = "ValveBiped.Bip01_R_Finger2",
        ["finger_middle_1_R"] = "ValveBiped.Bip01_R_Finger21",
        ["finger_middle_2_R"] = "ValveBiped.Bip01_R_Finger22",

        ["finger_ring_0_R"] = "ValveBiped.Bip01_R_Finger3",
        ["finger_ring_1_R"] = "ValveBiped.Bip01_R_Finger31",
        ["finger_ring_2_R"] = "ValveBiped.Bip01_R_Finger32",

        ["finger_pinky_0_R"] = "ValveBiped.Bip01_R_Finger4",
        ["finger_pinky_1_R"] = "ValveBiped.Bip01_R_Finger41",
        ["finger_pinky_2_R"] = "ValveBiped.Bip01_R_Finger42",

        ["arm_upper_L_TWIST"] = "ValveBiped.Bip01_L_Shoulder",
        ["arm_upper_L_TWIST1"] = "ValveBiped.Bip01_L_Bicep",
        ["arm_lower_L_TWIST"] = "ValveBiped.Bip01_L_Ulna",
        ["arm_lower_L_TWIST1"] = "ValveBiped.Bip01_L_Wrist",
        ["arm_upper_R_TWIST"] = "ValveBiped.Bip01_R_Shoulder",
        ["arm_upper_R_TWIST1"] = "ValveBiped.Bip01_R_Bicep",
        ["arm_lower_R_TWIST"] = "ValveBiped.Bip01_R_Ulna",
        ["arm_lower_R_TWIST1"] = "ValveBiped.Bip01_R_Wrist",
        ["scap_0_L"] = "ValveBiped.Bip01_L_Trapezius",
        ["scap_0_R"] = "ValveBiped.Bip01_R_Trapezius",
        ["pect_0_L"] = "ValveBiped.Bip01_L_Pect",
        ["pect_0_R"] = "ValveBiped.Bip01_R_Pect",
        ["neck_0_TWIST"] = "ValveBiped.Bip01_Neck2",

        ["finger_index_meta_L"] = "ValveBiped.Bip01_L_Finger1m",
        ["finger_middle_meta_L"] = "ValveBiped.Bip01_L_Finger2m",
        ["finger_ring_meta_L"] = "ValveBiped.Bip01_L_Finger3m",
        ["finger_pinky_meta_L"] = "ValveBiped.Bip01_L_Finger4m",
        ["finger_index_meta_R"] = "ValveBiped.Bip01_R_Finger1m",
        ["finger_middle_meta_R"] = "ValveBiped.Bip01_R_Finger2m",
        ["finger_ring_meta_R"] = "ValveBiped.Bip01_R_Finger3m",
        ["finger_pinky_meta_R"] = "ValveBiped.Bip01_R_Finger4m",

        ["leg_upper_L_TWIST"] = "ValveBiped.Bip01_L_Hip",
        ["leg_upper_L_TWIST1"] = "ValveBiped.Bip01_L_Quad",
        ["leg_upper_R_TWIST"] = "ValveBiped.Bip01_R_Hip",
        ["leg_upper_R_TWIST1"] = "ValveBiped.Bip01_R_Quad",

        ["leg_lower_L_TWIST"] = "ValveBiped.Bip01_L_Tibia",
        ["leg_lower_L_TWIST1"] = "ValveBiped.Bip01_L_Soleus",
        ["leg_lower_R_TWIST"] = "ValveBiped.Bip01_R_Tibia",
        ["leg_lower_R_TWIST1"] = "ValveBiped.Bip01_R_Soleus",

        ["scap_1_L"] = "ValveBiped.Bip01_L_Scap",
        ["scap_1_R"] = "ValveBiped.Bip01_R_Scap",

        ["leg_upper_L"] = "ValveBiped.Bip01_L_Thigh",
        ["leg_lower_L"] = "ValveBiped.Bip01_L_Calf",
        ["ankle_L"] = "ValveBiped.Bip01_L_Foot",
        ["ball_L"] = "ValveBiped.Bip01_L_Toe0",

        ["leg_upper_R"] = "ValveBiped.Bip01_R_Thigh",
        ["leg_lower_R"] = "ValveBiped.Bip01_R_Calf",
        ["ankle_R"] = "ValveBiped.Bip01_R_Foot",
        ["ball_R"] = "ValveBiped.Bip01_R_Toe0",

        ["mini_root"] = "ValveBiped.HC_Body_Bone"
    };

    /// <summary>The canonical ValveBiped skeleton, the bone names GMod's animation set and every ValveBiped-aware addon
    /// (JMod, WiltOS and others) actually look for. Source: https://gmodwiki.com/ValveBiped_Bones</summary>
    /// <remarks>A Source 2 bone is only treated as canonical when its mapped name is in here. Names outside it, such as Ulna,
    /// Wrist, Bicep, Shoulder, Trapezius, Neck2 and Finger1m, exist on some Valve models but are not part of the contract, and
    /// inventing them makes a rig look compliant to addons that then fail to find what they expect.</remarks>
    public static readonly IReadOnlySet<string> Bones = BuildBones();

    private static HashSet<string> BuildBones()
    {
        var bones = new HashSet<string>(StringComparer.Ordinal)
        {
            "ValveBiped.Bip01_Pelvis", "ValveBiped.Bip01_Spine", "ValveBiped.Bip01_Spine1", "ValveBiped.Bip01_Spine2",
            "ValveBiped.Bip01_Spine4", "ValveBiped.Bip01_Neck1", "ValveBiped.Bip01_Head1"
        };
        foreach (var side in new[] { "L", "R" })
        {
            foreach (var bone in new[] { "Clavicle", "UpperArm", "Forearm", "Hand", "Thigh", "Calf", "Foot", "Toe0" }) bones.Add($"ValveBiped.Bip01_{side}_{bone}");
            for (var digit = 0; digit < 5; digit++)
            {
                bones.Add($"ValveBiped.Bip01_{side}_Finger{digit}");
                bones.Add($"ValveBiped.Bip01_{side}_Finger{digit}1");
                bones.Add($"ValveBiped.Bip01_{side}_Finger{digit}2");
            }
        }
        return bones;
    }

    /// <summary>The ValveBiped name for a Source 2 bone, or the input unchanged.</summary>
    /// <remarks>Unmapped names pass through: cloth, custom and already-Source-1 bones keep whatever they were called.</remarks>
    public static string Name(string source2Name) => Source2ToValveBiped.GetValueOrDefault(source2Name, source2Name);

    /// <summary>True when a name is one of the ValveBiped names an animation library defines.</summary>
    public static bool IsCanonical(string name) => Bones.Contains(name);

    /// <summary>The name a kept but non-canonical bone compiles under.</summary>
    /// <remarks>
    /// <para>A helper the map gives a proper ValveBiped name, such as a twist, a scapula or a metacarpal, keeps it, for two
    /// reasons. <c>$proceduralbones</c> resolves its helper and control bones by name, so a twist renamed out of the
    /// ValveBiped family cannot be driven and studiomdl aborts the compile looking for it. And a working reference rig does
    /// exactly this: its helpers are <c>ValveBiped.Bip01_L_Ulna</c>, and only its bespoke jiggle bones sit in a lowercase
    /// family of their own.</para>
    /// <para>Everything else, such as cloth, jaw helpers and accessory mounts, is folded into one all-lowercase <c>z_</c>
    /// family. studiomdl stricmp-sorts <c>bonetablebyname</c> while the runtime searches it case-sensitively, so a rig mixing
    /// <c>backTooth_R</c> and <c>jaw</c> in among <c>ValveBiped.*</c> produces a table that cannot be binary searched: 75 of
    /// 123 bones went unfindable on the zombie playermodel. <c>ValveBiped.*</c> and <c>z_*</c> order the same way under both
    /// comparisons, and within each family the case pattern is uniform, so the two agree.</para>
    /// </remarks>
    public static string AuxiliaryName(string source2Name)
    {
        var mapped = Name(source2Name);
        return mapped.StartsWith("ValveBiped.", StringComparison.Ordinal) ? mapped : "z_" + mapped.ToLowerInvariant();
    }
}
