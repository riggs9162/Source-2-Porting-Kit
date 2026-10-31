using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace PortingKit.Addons.Porting;

/// <summary>Source files and explicit skeleton adaptations for an optional playermodel.</summary>
public sealed record PlayerModelResult(string Qc, string? AnimationQc, List<string> Files, JsonObject Report, List<string> Warnings)
{
    public int Bones { get; init; }
    public string Profile { get; init; } = "male";
    public IReadOnlyDictionary<string, string> BoneNameMap { get; init; } = new Dictionary<string, string>();
    public GltfSkin? TargetSkin { get; init; }
    public double UnitScale { get; init; } = 1.0;
    public PlayerAnimationResult? Animations { get; init; }
    public string? PhysicsAuditManifest { get; init; }
}

/// <summary>A source-sized ValveBiped hierarchy and its reversible source correspondence.</summary>
public sealed class PlayerRig
{
    public required GltfSkin Skin { get; init; }

    /// <summary>Source joint index to target joint index, for every source joint.</summary>
    public required Dictionary<int, int> Indices { get; init; }

    /// <summary>Source bone name to target bone name.</summary>
    public required OrderedDictionary<string, string> Names { get; init; }

    /// <summary>Carries source-space geometry into the target's facing, including any axis swap.</summary>
    public required M3 Rotation { get; init; }

    public required double UnitScale { get; init; }

    /// <summary>Original bind worlds in Source inches, by source joint index.</summary>
    public required Dictionary<int, M4> SourceWorld { get; init; }

    /// <summary>Target bind worlds, by target joint index.</summary>
    public required Dictionary<int, M4> World { get; init; }

    public required JsonObject Report { get; init; }

    public double AlignmentYaw => Report.Get("source_alignment_yaw_degrees").Dbl();
}

/// <summary>Builds a source-shaped humanoid playermodel with its own GMod animation graph.</summary>
/// <remarks>The model keeps its own build: every joint stays where the source put it and the mesh is only turned and scaled,
/// never deformed. What changes is each shared bone's axes, which take the donor's, so GMod's animations and IK chains drive
/// the rig as authored. Because a model-owned library is compiled against that exact bind, the sequences stop fighting the
/// mesh: a stock library's sequences replace shared bones with its own bind, which is why a model that merely includes
/// m_anm wears m_anm's proportions.</remarks>
public static partial class PlayermodelPort
{
    private const string Pelvis = "ValveBiped.Bip01_Pelvis";

    [GeneratedRegex(@"\AValveBiped\.Bip01_([LR])_Finger([0-4])2\z")]
    private static partial Regex FingerEndpoint();

    [GeneratedRegex("[^A-Za-z0-9_-]")]
    private static partial Regex UnsafeClipName();

    private static readonly string[] Digits = ["thumb", "index", "middle", "ring", "pinky"];

    /// <summary>Requires a pelvis, head and complete two-arm and two-leg chains.</summary>
    public static bool IsHumanoidSkin(GltfSkin skin)
    {
        var names = skin.Joints.Select(joint => ValveBiped.Name(joint.Name)).ToHashSet(StringComparer.Ordinal);
        return names.Contains(Pelvis) && names.Contains("ValveBiped.Bip01_Head1") && new[] { "L", "R" }
            .All(side => new[] { "UpperArm", "Forearm", "Hand", "Thigh", "Calf", "Foot" }.All(bone => names.Contains($"ValveBiped.Bip01_{side}_{bone}")));
    }

    /// <summary>Expresses original bind worlds in Source inches without fitting donor geometry.</summary>
    private static Dictionary<int, M4> NativeWorlds(GltfSkin skin, CoordinateMode coord)
    {
        var basis = coord.SwapAxes ? M3.Of(GltfDocument.Source2Basis) : M3.Identity;
        var worlds = SkeletonRetarget.SkinWorldMatrices(skin);
        var result = new Dictionary<int, M4>(worlds.Length);
        for (var index = 0; index < worlds.Length; index++) result[index] = (basis * M3.Of(worlds[index])).At(basis.Apply(worlds[index].Translation) * coord.Scale);
        return result;
    }

    /// <summary>Counts the ancestors in an already acyclic target hierarchy.</summary>
    private static int Depth(int index, IReadOnlyDictionary<int, int?> parents)
    {
        var result = 0;
        while (parents.GetValueOrDefault(index) is { } parent)
        {
            index = parent;
            result++;
            if (result > parents.Count) throw new InvalidDataException("Playermodel hierarchy contains a cycle");
        }
        return result;
    }

    /// <summary>Matches three-joint thumbs whether the native base is named meta or phalanx zero.</summary>
    private static (Dictionary<int, string> Names, List<string> AdaptedThumbs) SourceBoneNames(GltfSkin source)
    {
        var original = source.Joints.Select(joint => joint.Name).ToHashSet(StringComparer.Ordinal);
        var names = new Dictionary<int, string>();
        for (var index = 0; index < source.Joints.Count; index++) names[index] = ValveBiped.AuxiliaryName(source.Joints[index].Name);
        var adapted = new List<string>();
        foreach (var side in new[] { "L", "R" })
        {
            if (!original.Contains($"finger_thumb_meta_{side}") || original.Contains($"finger_thumb_2_{side}")) continue;
            var mapping = new Dictionary<string, string>(StringComparer.Ordinal)
            {
                [$"finger_thumb_meta_{side}"] = $"ValveBiped.Bip01_{side}_Finger0", [$"finger_thumb_0_{side}"] = $"ValveBiped.Bip01_{side}_Finger01",
                [$"finger_thumb_1_{side}"] = $"ValveBiped.Bip01_{side}_Finger02"
            };
            for (var index = 0; index < source.Joints.Count; index++)
                if (mapping.TryGetValue(source.Joints[index].Name, out var name)) names[index] = name;
            adapted.Add(side);
        }
        return (names, adapted);
    }

    /// <summary>A donor frame's parent-local transform.</summary>
    private static M3 EulerAt(double[] frame, int offset) => RotationMath.MatrixOf(frame[offset + 3], frame[offset + 4], frame[offset + 5]);

    /// <summary>Keeps source joint worlds and establishes canonical axes for GMod motion and IK.</summary>
    /// <remarks>
    /// <para>The rig is turned once, rigidly, by the yaw measured from the shared bones. A Source 2 rig faces a quarter turn from
    /// ValveBiped, and it only shows in positions, so that turn belongs to the positions and the mesh together.</para>
    /// <para>Each shared bone then takes the donor's rotation, swung onto the direction it actually has to point, its own
    /// segment to the child the donor aims at. Keeping the source's rotation instead arrives with the right hand mirrored,
    /// because Source 2 aims right-side bones down minus X where ValveBiped aims every bone down plus X. Keeping the donor's
    /// rotation untouched leaves no bone aiming at its own child, which renders as a hand splayed flat. The pelvis has no
    /// segment to aim along and takes the donor's rotation exactly.</para>
    /// </remarks>
    public static PlayerRig BuildPlayerRig(GltfSkin source, CoordinateMode coord, SmdAnimation donorBind, double? alignYaw = null)
    {
        if (!IsHumanoidSkin(source)) throw new InvalidDataException("This model has no supported humanoid playermodel skeleton");
        var original = NativeWorlds(source, coord);
        var (names, adaptedThumbs) = SourceBoneNames(source);
        var root = names.First(pair => pair.Value == Pelvis).Key;
        var (donorRotations, donorPositions) = PlayerAnimations.AnimationWorlds(donorBind);
        var donor = new Dictionary<string, M4>(StringComparer.Ordinal);
        for (var index = 0; index < donorBind.Names.Count; index++) donor[donorBind.Names[index]] = donorRotations[index][0].At(donorPositions[index][0]);
        var byName = new OrderedDictionary<string, M4>(StringComparer.Ordinal);
        for (var index = 0; index < source.Joints.Count; index++)
            if (donor.ContainsKey(names[index])) byName[names[index]] = original[index];
        var measured = SkeletonRetarget.MeasureAlignmentYaw(byName, donor, Pelvis);
        var yaw = alignYaw ?? measured;
        var turn = SkeletonRetarget.YawMatrix(yaw);
        var count = source.Joints.Count;
        var rotations = new M3[count];
        var positions = new V3[count];
        for (var index = 0; index < count; index++)
        {
            rotations[index] = turn * M3.Of(original[index]);
            positions[index] = turn.Apply(original[index].Translation);
        }
        var ancestors = new SortedSet<int>();
        for (var parent = source.Joints[root].Parent; parent is { } value; parent = source.Joints[value].Parent) ancestors.Add(value);
        var parents = new Dictionary<int, int?>(count);
        for (var index = 0; index < count; index++)
        {
            if (index == root)
            {
                parents[index] = null;
                continue;
            }
            var parent = source.Joints[index].Parent;
            if (ValveBiped.IsCanonical(names[index]))
                while (parent is { } value && !ValveBiped.IsCanonical(names.GetValueOrDefault(value, ""))) parent = source.Joints[value].Parent;
            parents[index] = parent ?? root;
        }
        var children = new Dictionary<int, List<int>>();
        for (var index = 0; index < count; index++)
        {
            if (parents[index] is not { } parent || !donor.ContainsKey(names[index])) continue;
            if (!children.TryGetValue(parent, out var list)) children[parent] = list = [];
            list.Add(index);
        }
        var changedAxes = new List<string>();
        for (var index = 0; index < count; index++)
        {
            var name = names[index];
            if (!donor.TryGetValue(name, out var reference)) continue;
            var referenceRotation = M3.Of(reference);
            var candidates = children.GetValueOrDefault(index, []).Where(child => donor.ContainsKey(names[child])).ToList();
            if (candidates.Count > 0 && name != Pelvis)
            {
                var axis = referenceRotation.Column0;
                var child = candidates[0];
                var best = double.NegativeInfinity;
                foreach (var candidate in candidates)
                {
                    var offset = donor[names[candidate]].Translation - reference.Translation;
                    var score = offset.Dot(axis) / Math.Max(offset.Length, 1e-9);
                    if (score <= best) continue;
                    best = score;
                    child = candidate;
                }
                rotations[index] = SkeletonRetarget.Swing(axis, positions[child] - positions[index]) * referenceRotation;
            }
            else rotations[index] = referenceRotation;
            changedAxes.Add(name);
        }
        var order = Enumerable.Range(0, count).OrderBy(index => Depth(index, parents)).ThenBy(index => index).ToList();
        var indexMap = new Dictionary<int, int>(count);
        for (var position = 0; position < order.Count; position++) indexMap[order[position]] = position;
        var worlds = new M4[count];
        for (var index = 0; index < count; index++) worlds[index] = rotations[index].At(positions[index]);
        var joints = new List<GltfJoint>(count);
        foreach (var old in order)
        {
            var parent = parents[old];
            var local = parent is { } value ? worlds[value].Inverse() * worlds[old] : worlds[old];
            joints.Add(new(names[old], source.Joints[old].NodeIndex, parent is { } owner ? indexMap[owner] : null, worlds[old].Inverse(), local.Translation, RotationMath.FromMatrix(M3.Of(local)), V3.One));
        }
        var target = new GltfSkin(joints, indexMap[root], source.SkinIndex);
        var compatibility = AddMissingChannels(target, donorBind);
        AddAttachmentBones(target, donorBind);
        if (target.Joints.Count > 256) throw new InvalidDataException("Playermodel plus required attachment bones exceeds GMod's 256-bone limit");
        var finalWorld = SkeletonRetarget.SkinWorldMatrices(target);
        var world = new Dictionary<int, M4>(finalWorld.Length);
        for (var index = 0; index < finalWorld.Length; index++) world[index] = finalWorld[index];
        var worst = order.Max(old => (finalWorld[indexMap[old]].Translation - positions[old]).Length);
        if (worst > 1e-5) throw new InvalidDataException($"Playermodel changed an original joint position by {Fmt.G(worst)} units");
        var report = new JsonObject
        {
            ["original_scale_preserved"] = true, ["mesh_deformation"] = false, ["maximum_joint_position_error"] = worst, ["source_bones"] = source.Joints.Count, ["target_bones"] = target.Joints.Count,
            ["source_alignment_yaw_degrees"] = yaw, ["compiler_basis_yaw_degrees"] = 90.0, ["bone_axes_adapted"] = JsonKeys.Strings(changedAxes),
            ["canonical_nonsegment_axes"] = JsonKeys.Strings([Pelvis]), ["compatibility_bones"] = compatibility, ["thumb_metacarpal_channels"] = JsonKeys.Strings(adaptedThumbs),
            ["reparented_root_helpers"] = JsonKeys.Strings(ancestors.Select(index => source.Joints[index].Name))
        };
        var nameMap = new OrderedDictionary<string, string>(StringComparer.Ordinal);
        for (var index = 0; index < source.Joints.Count; index++) nameMap[source.Joints[index].Name] = names[index];
        var basis = coord.SwapAxes ? M3.Of(GltfDocument.Source2Basis) : M3.Identity;
        return new() { Skin = target, Indices = indexMap, Names = nameMap, Rotation = turn * basis, UnitScale = coord.Scale, SourceWorld = original, World = world, Report = report };
    }

    private static Dictionary<string, int> JointIndex(GltfSkin skin)
    {
        var names = new Dictionary<string, int>(StringComparer.Ordinal);
        for (var index = 0; index < skin.Joints.Count; index++) names[skin.Joints[index].Name] = index;
        return names;
    }

    /// <summary>Keeps unweighted standard animation channels missing from a native digit rig.</summary>
    /// <remarks>An authored digit end bone gives the distal phalanx its true length. Without one the donor's offset is scaled by
    /// how the parent's length compares, so a shorter finger gets a proportionally shorter tip. Only a finger may be missing:
    /// any other absent bone would lose animation the model visibly needs.</remarks>
    private static JsonArray AddMissingChannels(GltfSkin skin, SmdAnimation donorBind)
    {
        var names = JointIndex(skin);
        var records = new JsonArray();
        var bindFrame = donorBind.Frames[0];
        for (var index = 0; index < donorBind.Names.Count; index++)
        {
            var name = donorBind.Names[index];
            if (!ValveBiped.IsCanonical(name) || names.ContainsKey(name)) continue;
            var parentIndex = donorBind.Parents[index];
            var parentName = parentIndex >= 0 ? donorBind.Names[parentIndex] : "";
            if (!names.TryGetValue(parentName, out var targetParent) || !name.Contains("Finger", StringComparison.Ordinal))
                throw new InvalidDataException($"Native humanoid is missing required GMod animation bone {name}");
            var worlds = SkeletonRetarget.SkinWorldMatrices(skin);
            var parentWorld = worlds[targetParent];
            int? nativeEndpoint = null;
            var endpoint = FingerEndpoint().Match(name);
            if (endpoint.Success)
            {
                var finger = Digits[endpoint.Groups[2].ValueSpan[0] - '0'];
                if (names.TryGetValue($"z_finger_{finger}_end_{endpoint.Groups[1].Value.ToLowerInvariant()}", out var found)) nativeEndpoint = found;
            }
            var translation = new V3(bindFrame[index * 6], bindFrame[index * 6 + 1], bindFrame[index * 6 + 2]);
            string reason;
            if (nativeEndpoint is { } tip)
            {
                translation = M3.Of(parentWorld).Transposed().Apply(worlds[tip].Translation - parentWorld.Translation);
                reason = "authored_digit_endpoint";
            }
            else
            {
                var referenceLength = new V3(bindFrame[parentIndex * 6], bindFrame[parentIndex * 6 + 1], bindFrame[parentIndex * 6 + 2]).Length;
                var ownLength = skin.Joints[targetParent].RestTranslation.Length;
                translation *= referenceLength > 1e-6 ? ownLength / referenceLength : 1.0;
                reason = "unweighted_animation_channel";
            }
            var rotation = EulerAt(bindFrame, index * 6);
            var world = parentWorld * rotation.At(translation);
            names[name] = skin.Joints.Count;
            skin.Joints.Add(new(name, -1, targetParent, world.Inverse(), translation, RotationMath.FromMatrix(rotation), V3.One));
            records.Add(new JsonObject { ["name"] = name, ["reason"] = reason, ["weighted"] = false });
        }
        return records;
    }

    /// <summary>Keeps the GMod hand attachment transforms as children of source-sized hands.</summary>
    /// <remarks>Weapons hold on to these. They carry no skin weight, so nothing else brings them over.</remarks>
    private static void AddAttachmentBones(GltfSkin skin, SmdAnimation donorBind)
    {
        var donorIndices = new Dictionary<string, int>(StringComparer.Ordinal);
        for (var index = 0; index < donorBind.Names.Count; index++) donorIndices[donorBind.Names[index]] = index;
        var names = JointIndex(skin);
        var frame = donorBind.Frames[0];
        foreach (var side in new[] { "RH", "LH" })
        {
            var name = $"ValveBiped.Anim_Attachment_{side}";
            var parent = $"ValveBiped.Bip01_{side[0]}_Hand";
            if (names.ContainsKey(name) || !donorIndices.TryGetValue(name, out var donorIndex) || !names.TryGetValue(parent, out var parentIndex)) continue;
            var offset = donorIndex * 6;
            var translation = new V3(frame[offset], frame[offset + 1], frame[offset + 2]);
            var world = SkeletonRetarget.SkinWorldMatrices(skin)[parentIndex] * EulerAt(frame, offset).At(translation);
            skin.Joints.Add(new(name, -1, parentIndex, world.Inverse(), translation, RotationMath.FromEuler(frame[offset + 3], frame[offset + 4], frame[offset + 5]), V3.One));
        }
    }

    private static double LargestAbsolute(in M3 a, in M3 b) => new[]
    {
        Math.Abs(a.M00 - b.M00), Math.Abs(a.M01 - b.M01), Math.Abs(a.M02 - b.M02), Math.Abs(a.M10 - b.M10), Math.Abs(a.M11 - b.M11),
        Math.Abs(a.M12 - b.M12), Math.Abs(a.M20 - b.M20), Math.Abs(a.M21 - b.M21), Math.Abs(a.M22 - b.M22)
    }.Max();

    private static double LargestAbsolute(V3 a, V3 b) => Math.Max(Math.Abs(a.X - b.X), Math.Max(Math.Abs(a.Y - b.Y), Math.Abs(a.Z - b.Z)));

    /// <summary>A source frame as the turned rig sees it, before any bone took the donor's axes.</summary>
    private static M4 Turned(in M3 turn, M4 world) => (turn * M3.Of(world)).At(turn.Apply(world.Translation));

    /// <summary>Keeps authored hitbox boxes on nonintrusive branches with their original bone axes.</summary>
    /// <remarks>A box is authored in its bone's axes, and the rig changed those. Where they differ, an unweighted helper child
    /// carries the original axes so the box lands exactly where it was authored.</remarks>
    private static (List<string> Lines, JsonArray Audit) SourceHitboxes(PlayerRig rig, GltfSkin source, string metadataPath)
    {
        if (!File.Exists(metadataPath)) return ([], []);
        var metadata = ModelMetadata.Load(metadataPath);
        var original = JointIndex(source);
        var target = JointIndex(rig.Skin);
        var turn = SkeletonRetarget.YawMatrix(rig.AlignmentYaw);
        var helperByBone = new OrderedDictionary<string, string>(StringComparer.Ordinal);
        var translated = new ModelMetadata();
        var audit = new JsonArray();
        foreach (var hitbox in metadata.Hitboxes)
        {
            JsonObject Record() => new()
            {
                ["name"] = hitbox.Name, ["set"] = hitbox.SetName, ["source_bone"] = hitbox.Bone, ["group"] = hitbox.Group,
                ["minimum"] = JsonKeys.Vector(hitbox.Minimum), ["maximum"] = JsonKeys.Vector(hitbox.Maximum)
            };
            JsonObject Unsupported(string reason)
            {
                var entry = Record();
                entry["status"] = "unsupported";
                entry["reason"] = reason;
                return entry;
            }
            if (hitbox.TranslationOnly)
            {
                audit.Add(Unsupported("translation_only_hitbox"));
                continue;
            }
            if (!original.TryGetValue(hitbox.Bone, out var old) || !rig.Names.TryGetValue(hitbox.Bone, out var mapped) || !target.TryGetValue(mapped, out var parent))
            {
                audit.Add(Unsupported("hitbox_bone_absent"));
                continue;
            }
            var expected = Turned(turn, rig.SourceWorld[old]);
            var rotationError = LargestAbsolute(M3.Of(expected), M3.Of(rig.World[parent]));
            var name = rig.Skin.Joints[parent].Name;
            var helper = false;
            if (rotationError > 1e-7)
            {
                helper = true;
                if (!helperByBone.TryGetValue(hitbox.Bone, out var existing))
                {
                    existing = $"s2kit_hitbox_{helperByBone.Count:D3}";
                    if (target.ContainsKey(existing)) throw new InvalidDataException($"Native skeleton already uses reserved hitbox helper {existing}");
                    var local = rig.World[parent].Inverse() * expected;
                    var index = rig.Skin.Joints.Count;
                    rig.Skin.Joints.Add(new(existing, -1, parent, expected.Inverse(), local.Translation, RotationMath.FromMatrix(M3.Of(local)), V3.One));
                    rig.World[index] = expected;
                    target[existing] = index;
                    helperByBone[hitbox.Bone] = existing;
                }
                name = existing;
            }
            var actual = rig.World[target[name]];
            var preserved = Record();
            preserved["status"] = "preserved";
            preserved["target_bone"] = name;
            preserved["axis_helper"] = helper;
            preserved["maximum_bind_position_error"] = LargestAbsolute(actual.Translation, expected.Translation);
            preserved["bind_axis_error_degrees"] = Fmt.Degrees(RotationMath.Magnitude(RotationMath.FromMatrix(M3.Of(actual).Transposed() * M3.Of(expected))));
            audit.Add(preserved);
            translated.Hitboxes.Add(hitbox with { Bone = name });
        }
        if (rig.Skin.Joints.Count > 256) throw new InvalidDataException("Original-axis hitbox helpers exceed GMod's 256-bone limit");
        rig.Report["target_bones"] = rig.Skin.Joints.Count;
        var helpers = new JsonObject();
        foreach (var (bone, name) in helperByBone) helpers[bone] = name;
        rig.Report["hitbox_axis_helpers"] = helpers;
        return (translated.HitboxQcLines(), audit);
    }

    /// <summary>Rebases authored HLA motion onto the playermodel axes without changing its deformation.</summary>
    /// <returns>One array per frame, six numbers per target bone.</returns>
    /// <remarks>Each source bone's motion is taken as the rotation away from its own bind, then applied to the target bone's
    /// bind, so a bone whose axes changed still moves flesh exactly as authored. Bones the source does not have follow their
    /// parent at their rest offset.</remarks>
    public static double[][] RemapSourceAnimationFrames(GltfSkin source, PlayerRig rig, IReadOnlyList<JointPose[]> frames)
    {
        var count = frames.Count;
        var sourceRotations = new M3[source.Joints.Count][];
        var sourcePositions = new V3[source.Joints.Count][];
        for (var index = 0; index < source.Joints.Count; index++)
        {
            var rotations = new M3[count];
            var positions = new V3[count];
            var parent = source.Joints[index].Parent;
            for (var frame = 0; frame < count; frame++)
            {
                var (translation, quaternion, _) = frames[frame][index];
                var local = RotationMath.MatrixOf(quaternion);
                if (parent is { } owner)
                {
                    rotations[frame] = sourceRotations[owner][frame] * local;
                    positions[frame] = sourcePositions[owner][frame] + sourceRotations[owner][frame].Apply(translation);
                }
                else
                {
                    rotations[frame] = local;
                    positions[frame] = translation;
                }
            }
            sourceRotations[index] = rotations;
            sourcePositions[index] = positions;
        }
        var placedRotations = Array.ConvertAll(sourceRotations, rotations => Array.ConvertAll(rotations, rotation => rig.Rotation * rotation));
        var placedPositions = Array.ConvertAll(sourcePositions, positions => Array.ConvertAll(positions, position => rig.Rotation.Apply(position * rig.UnitScale)));
        var original = SkeletonRetarget.SkinWorldMatrices(source);
        var byTarget = new Dictionary<int, int>();
        foreach (var (old, target) in rig.Indices) byTarget[target] = old;
        var joints = rig.Skin.Joints;
        var targetRotations = new M3[joints.Count][];
        var targetPositions = new V3[joints.Count][];
        var result = new double[count][];
        for (var frame = 0; frame < count; frame++) result[frame] = new double[joints.Count * 6];
        for (var index = 0; index < joints.Count; index++)
        {
            var joint = joints[index];
            var parent = joint.Parent;
            var rotations = new M3[count];
            var positions = new V3[count];
            if (byTarget.TryGetValue(index, out var old))
            {
                var bindRotation = rig.Rotation * M3.Of(original[old]);
                var bindPosition = rig.Rotation.Apply(original[old].Translation * rig.UnitScale);
                var bindInverse = bindRotation.Transposed();
                var world = M3.Of(rig.World[index]);
                var offset = rig.World[index].Translation - bindPosition;
                for (var frame = 0; frame < count; frame++)
                {
                    var delta = placedRotations[old][frame] * bindInverse;
                    rotations[frame] = delta * world;
                    positions[frame] = placedPositions[old][frame] + delta.Apply(offset);
                }
            }
            else
            {
                var local = RotationMath.MatrixOf(joint.RestRotation);
                for (var frame = 0; frame < count; frame++)
                    if (parent is { } owner)
                    {
                        rotations[frame] = targetRotations[owner][frame] * local;
                        positions[frame] = targetPositions[owner][frame] + targetRotations[owner][frame].Apply(joint.RestTranslation);
                    }
                    else
                    {
                        rotations[frame] = local;
                        positions[frame] = joint.RestTranslation;
                    }
            }
            targetRotations[index] = rotations;
            targetPositions[index] = positions;
            for (var frame = 0; frame < count; frame++)
            {
                var (position, rotation) = (positions[frame], rotations[frame]);
                if (parent is { } owner)
                {
                    var inverse = targetRotations[owner][frame].Transposed();
                    position = inverse.Apply(position - targetPositions[owner][frame]);
                    rotation = inverse * rotation;
                }
                var euler = RotationMath.AsEuler(RotationMath.FromMatrix(rotation));
                var row = result[frame];
                var at = index * 6;
                (row[at], row[at + 1], row[at + 2], row[at + 3], row[at + 4], row[at + 5]) = (position.X, position.Y, position.Z, euler.X, euler.Y, euler.Z);
            }
        }
        return result;
    }

    private static bool Unsafe(string text, string characters) => text.AsSpan().IndexOfAny(characters) >= 0;

    /// <summary>Writes isolated HLA sequences and masks without overriding player activities.</summary>
    /// <remarks>Everything is prefixed <c>hla_</c> and carries no activity, so the player graph keeps answering every activity
    /// and these stay reachable by name. An additive clip is written only if the export composed it over the bind pose.</remarks>
    private static (List<string> Lines, List<string> Files, JsonArray Audit, List<string> Warnings) SourceSequences(GltfDocument document, GltfSkin source, PlayerRig rig, string outDir, ModelMetadata metadata,
        JsonObject fidelity, PortContext context)
    {
        var clips = GltfClip.ParseAll(document, source);
        SmdSkeleton.ValidateNativeData(document, source, clips);
        var lines = new List<string>();
        var files = new List<string>();
        var audit = new JsonArray();
        var warnings = new List<string>();
        var composed = fidelity.Get("animation_export").Get("additive_composed").Bool();
        foreach (var (name, weights) in metadata.Weightlists)
        {
            var targetName = "hla_" + name;
            if (Unsafe(targetName, "\"\r\n")) throw new InvalidDataException("Unsafe authored weightlist name");
            lines.Add($"$weightlist \"{targetName}\" {{");
            foreach (var (bone, weight) in weights)
            {
                if (!rig.Names.TryGetValue(bone, out var mapped)) throw new InvalidDataException($"Authored weightlist references an absent source bone: {bone}");
                lines.Add($"    \"{mapped}\" {Fmt.G(weight)}");
            }
            lines.Add("}");
        }
        foreach (var pose in metadata.PoseParameters)
        {
            var name = "hla_" + pose.Need("m_sName").Str();
            if (Unsafe(name, "\"\r\n")) throw new InvalidDataException("Unsafe authored pose parameter name");
            var loop = pose.Get("m_flLoop").Dbl();
            var suffix = loop != 0 ? $" loop {Fmt.G(loop)}" : "";
            lines.Add($"$poseparameter \"{name}\" {Fmt.G(pose.Need("m_flStart").Dbl())} {Fmt.G(pose.Need("m_flEnd").Dbl())}{suffix}");
        }
        for (var index = 0; index < clips.Count; index++)
        {
            context.Check();
            var clip = clips[index];
            var authored = metadata.AnimationForClip(clip.Name) ?? throw new InvalidDataException($"Authored animation metadata is missing for {clip.Name}");
            if (authored.Delta && !composed)
            {
                warnings.Add($"{clip.Name}: source additive channels were not composed; PM copy omitted");
                audit.Add(new JsonObject { ["name"] = clip.Name, ["status"] = "unsupported", ["reason"] = "uncomposed_additive" });
                continue;
            }
            if (Unsafe(clip.Name, "\"\r\n\\")) throw new InvalidDataException("Unsafe authored sequence name");
            var (frames, count, sampling) = clip.SampleAuthored(source, authored);
            var safe = UnsafeClipName().Replace(clip.Name, "_");
            safe = safe.Length > 90 ? safe[..90] : safe;
            var path = Path.Combine(outDir, $"hla_{index:D4}_{(safe.Length > 0 ? safe : "animation")}.smd");
            PlayerAnimations.WriteAnimationSmd(path, rig.Skin, RemapSourceAnimationFrames(source, rig, frames), Enumerable.Range(0, count).ToList());
            var options = new List<string> { $"fps {Fmt.G(authored.Fps)}" };
            if (authored.Loop) options.Add("loop");
            if (authored.Snap) options.Add("snap");
            if (authored.Hidden) options.Add("hidden");
            if (authored.Weightlist.Length > 0) options.Add($"weightlist \"hla_{authored.Weightlist}\"");
            var target = "hla_" + clip.Name;
            lines.Add($"$sequence \"{target}\" \"{Path.GetFileName(path)}\" " + string.Join(' ', options));
            files.Add(path);
            audit.Add(new JsonObject
            {
                ["name"] = clip.Name, ["target_name"] = target, ["frames"] = count, ["fps"] = authored.Fps, ["loop"] = authored.Loop, ["activities"] = new JsonArray(), ["autoplay"] = false,
                ["sampling"] = sampling, ["additive_composed_over_bind"] = authored.Delta && composed, ["status"] = authored.Delta ? "adapted" : "preserved"
            });
            context.Log($"Player source animation {index + 1}/{clips.Count}: {clip.Name}");
        }
        return (lines, files, audit, warnings);
    }

    /// <summary>Generates hand and foot chains calibrated to the target's original limbs.</summary>
    public static List<string> PlayerIkLines(GltfSkin skin)
    {
        var names = JointIndex(skin);
        var worlds = SkeletonRetarget.SkinWorldMatrices(skin);
        var lines = new List<string>();
        foreach (var side in new[] { "R", "L" })
            foreach (var (kind, chain) in new[] { ("Hand", "hand"), ("Foot", "foot") })
            {
                var index = names[$"ValveBiped.Bip01_{side}_{kind}"];
                var middle = skin.Joints[index].Parent!.Value;
                var upper = skin.Joints[middle].Parent!.Value;
                V3 start = worlds[upper].Translation, elbow = worlds[middle].Translation, end = worlds[index].Translation;
                var axis = end - start;
                axis /= Math.Max(axis.Length, 1e-12);
                var bend = elbow - start - axis * (elbow - start).Dot(axis);
                V3 knee;
                if (bend.Length < 1e-4) knee = new(0.707, chain == "foot" ? -0.707 : 0.707, 0.0);
                else
                {
                    knee = M3.Of(worlds[upper]).Transposed().Apply(bend);
                    knee /= knee.Length;
                }
                lines.Add($"$ikchain \"{side.ToLowerInvariant()}{chain}\" \"{skin.Joints[index].Name}\" knee {Fmt.F6(knee.X)} {Fmt.F6(knee.Y)} {Fmt.F6(knee.Z)}");
            }
        lines.AddRange(["$ikautoplaylock \"rfoot\" 1 0.1", "$ikautoplaylock \"lfoot\" 1 0.1"]);
        return lines;
    }

    /// <summary>Writes unit and facing converted geometry with Source 1's three-influence skin limit.</summary>
    private static void WriteReference(string path, GltfSkin skin, IReadOnlyList<SkinnedPrimitive> primitives, PlayerRig rig, PortContext context)
    {
        SmdSkeleton.WriteAnimation(skin, [skin.BindPose()], path, new());
        using var output = Fmt.OpenText(path, true);
        output.Write("triangles\n");
        foreach (var primitive in primitives)
        {
            context.Check();
            var vertices = Array.ConvertAll(primitive.Vertices, vertex => rig.Rotation.Apply(vertex * rig.UnitScale));
            var normals = Array.ConvertAll(primitive.Normals ?? throw new InvalidDataException("Playermodel primitive has no normals"), rig.Rotation.Apply);
            var texcoords = primitive.Texcoords ?? throw new InvalidDataException("Playermodel primitive has no texture coordinates");
            for (var corner = 0; corner < primitive.Indices.Length; corner++)
            {
                if (corner % 3 == 0)
                {
                    output.Write(primitive.Material);
                    output.Write('\n');
                }
                var vertex = primitive.Indices[corner];
                var links = new OrderedDictionary<int, double>();
                for (var slot = 0; slot < primitive.Slots; slot++)
                {
                    var weight = primitive.Weight(vertex, slot);
                    if (weight <= 0) continue;
                    var joint = rig.Indices[primitive.Joint(vertex, slot)];
                    links[joint] = links.GetValueOrDefault(joint, 0.0) + weight;
                }
                if (links.Count == 0) throw new InvalidDataException("Playermodel vertex has no source skin weights");
                var uv = texcoords[vertex];
                output.Write(SmdSkeleton.VertexLine(vertices[vertex], normals[vertex], new(uv.X, 1.0 - uv.Y), RagdollAppearance.Source1SkinLinks(links.Select(pair => (pair.Key, pair.Value)))));
            }
        }
        output.Write("end\n");
    }

    /// <summary>Retains source attachment frames under aliases when GMod reserves their names.</summary>
    /// <remarks>GMod's weapon code looks up <c>anim_attachment_RH</c> and <c>anim_attachment_LH</c> and expects its own hand
    /// frames, so those two always point at the donor's attachment bones. A source attachment of the same name survives under
    /// an <c>hla_</c> alias. Every other frame is rebased onto the bone's new axes, which leaves it exactly where it was.</remarks>
    private static List<string> AttachmentLines(PlayerRig rig, GltfSkin source, string? metadataPath, JsonArray audit)
    {
        var output = new List<string>();
        var authored = new HashSet<string>(StringComparer.Ordinal);
        var reserved = new[] { "RH", "LH" }.Select(side => $"anim_attachment_{side}".ToLowerInvariant()).ToHashSet(StringComparer.Ordinal);
        var targetNames = JointIndex(rig.Skin);
        if (metadataPath is not null && File.Exists(metadataPath))
        {
            var metadata = ModelMetadata.Load(metadataPath);
            var usedNames = metadata.Attachments.Select(attachment => attachment.Name.ToLowerInvariant()).ToHashSet(StringComparer.Ordinal);
            usedNames.UnionWith(reserved);
            var sourceNames = JointIndex(source);
            var rebased = new ModelMetadata();
            foreach (var attachment in metadata.Attachments)
            {
                if (attachment.RootTransform || attachment.Influences != 1 || attachment.IgnoreRotation)
                {
                    audit.Add(new JsonObject { ["name"] = attachment.Name, ["status"] = "unsupported", ["reason"] = "model_space_or_weighted_attachment_transform" });
                    continue;
                }
                if (!sourceNames.TryGetValue(attachment.Bone, out var old) || !rig.Names.TryGetValue(attachment.Bone, out var name) || !targetNames.TryGetValue(name, out var target))
                {
                    audit.Add(new JsonObject { ["name"] = attachment.Name, ["status"] = "unsupported", ["reason"] = "attachment_bone_absent" });
                    continue;
                }
                var frame = RotationMath.MatrixOf(attachment.Rotation).At(attachment.Position);
                var placed = Turned(SkeletonRetarget.YawMatrix(rig.AlignmentYaw), rig.SourceWorld[old] * frame);
                var local = rig.World[target].Inverse() * placed;
                var outputName = attachment.Name;
                var entry = new JsonObject { ["name"] = outputName, ["bone"] = name, ["status"] = "preserved" };
                if (reserved.Contains(outputName.ToLowerInvariant()))
                {
                    var alias = $"hla_{outputName}";
                    outputName = alias;
                    var suffix = 1;
                    while (usedNames.Contains(outputName.ToLowerInvariant())) outputName = $"{alias}_{suffix++}";
                    usedNames.Add(outputName.ToLowerInvariant());
                    entry["name"] = outputName;
                    entry["status"] = "adapted";
                    entry["source_name"] = attachment.Name;
                    entry["adaptation"] = "Source frame retained under an alias for a reserved GMod attachment";
                }
                rebased.Attachments.Add(attachment with { Name = outputName, Bone = name, Position = local.Translation, Rotation = RotationMath.FromMatrix(M3.Of(local)) });
                authored.Add(outputName.ToLowerInvariant());
                audit.Add(entry);
            }
            output.AddRange(rebased.AttachmentQcLines());
        }
        foreach (var side in new[] { "RH", "LH" })
        {
            var name = $"anim_attachment_{side}";
            output.Add($"$attachment \"{name}\" \"ValveBiped.Anim_Attachment_{side}\" 0 0 0 rotate -90 0 -90");
            audit.Add(new JsonObject { ["name"] = name, ["status"] = "generated", ["purpose"] = "GMod animation attachment" });
        }
        if (!authored.Contains("eyes"))
        {
            output.Add("$attachment \"eyes\" \"ValveBiped.Bip01_Head1\" 0 0 0");
            audit.Add(new JsonObject { ["name"] = "eyes", ["status"] = "generated", ["purpose"] = "GMod eye attachment" });
        }
        return output;
    }

    /// <summary>Turns and scales every stream of a primitive in place, because the primitive owns its own arrays.</summary>
    private static void TransformPrimitive(AppearancePrimitive primitive, PlayerRig rig)
    {
        var (rotation, scale) = (rig.Rotation, rig.UnitScale);
        for (var index = 0; index < primitive.Vertices.Length; index++) primitive.Vertices[index] = rotation.Apply(primitive.Vertices[index] * scale);
        for (var index = 0; index < primitive.Normals.Length; index++) primitive.Normals[index] = rotation.Apply(primitive.Normals[index]);
        foreach (var target in primitive.Targets.Values)
        {
            target.Position = target.Position?.Map(value => rotation.Apply(value * scale));
            target.Normal = target.Normal?.Map(rotation.Apply);
        }
    }

    private static int Count(JsonArray audit, Func<string, bool> status) => audit.Count(item => status(item.Need("status").Str()));

    /// <summary>Generates an optional playermodel and a complete model-owned animation library.</summary>
    /// <param name="animationCache">The folder holding the verified animation sources, shared between models. Null keeps a
    /// private copy under <paramref name="outDir"/>, which downloads them again.</param>
    /// <param name="metadataPath">The authored metadata sidecar, by default the one beside the model.</param>
    /// <param name="fidelity">The fidelity sidecar, by default the one beside the model. A model without one falls back to
    /// mesh names for its bodygroups.</param>
    /// <param name="alignYaw">Overrides the measured facing, in degrees.</param>
    /// <param name="nativePhysics">The model's prepared ragdoll physics, to save preparing it again.</param>
    /// <remarks>
    /// <para>The physics binds the unchanged source collision to this skeleton, so its shapes follow the same yaw as the mesh.</para>
    /// <para>A rig whose twists are driven by authored constraints gets no twist helper file, because the runtime driver owns
    /// those bones and a second driver would fight it.</para>
    /// </remarks>
    public static PlayerModelResult Port(string glb, string outDir, string modelname, string cdmaterials, string? animationCache, double frictionScale, bool includeSourceAnimations, PortContext context,
        string surfaceprop = "flesh", string? metadataPath = null, JsonObject? fidelity = null, string profile = "male", double? alignYaw = null, RagdollPhysicsResult? nativePhysics = null)
    {
        glb = Path.GetFullPath(glb);
        outDir = Path.GetFullPath(outDir);
        var document = GltfDocument.Read(glb);
        var source = GltfSkin.Parse(document) ?? throw new InvalidDataException("Playermodel generation requires a skinned model");
        SmdSkeleton.ValidateNativeData(document, source, []);
        var sources = PlayerAnimations.EnsureAnimationSources(animationCache, Path.Combine(outDir, ".cache"), context);
        var coord = CoordinateMode.For(document);
        var rig = BuildPlayerRig(source, coord, PlayerAnimations.SourceBind(sources, profile), alignYaw);
        metadataPath ??= Path.ChangeExtension(glb, ".metadata.json");
        var (hitboxLines, hitboxAudit) = SourceHitboxes(rig, source, metadataPath);
        Directory.CreateDirectory(outDir);
        var files = new List<string>();
        var bodyLines = new List<string>();
        var appearanceReport = new JsonObject();
        var appearanceDrivers = new JsonObject();
        var fidelityPath = Path.ChangeExtension(glb, ".fidelity.json");
        var fidelityData = fidelity ?? (File.Exists(fidelityPath) ? ModelFidelity.Load(fidelityPath) : null);
        var yawMatrix = SkeletonRetarget.YawMatrix(rig.AlignmentYaw).ToM4();
        if (fidelityData is not null)
        {
            var appearance = RagdollAppearance.WriteAppearance(document, rig.Skin, new(), outDir, fidelityData, new AppearanceOptions
            {
                Modelname = modelname, BoneNameMap = rig.Names, SourceModelRotation = yawMatrix, IncludeSourceAnimations = includeSourceAnimations,
                PrimitiveTransform = primitives =>
                {
                    foreach (var primitive in primitives) TransformPrimitive(primitive, rig);
                    return null;
                }
            }, context);
            bodyLines.AddRange(appearance.QcLines);
            files.AddRange(appearance.Files);
            appearanceReport = appearance.Report;
            appearanceDrivers = appearance.AddonDrivers;
        }
        else
        {
            var (meshSets, _) = SkinnedPrimitive.Collect(document, context);
            var sets = new List<(string Name, string Smd, int VertCount)>();
            for (var index = 0; index < meshSets.Count; index++)
            {
                var (name, primitives) = meshSets[index];
                var path = Path.Combine(outDir, $"pm_ref_{index:D3}.smd");
                WriteReference(path, rig.Skin, primitives, rig, context);
                sets.Add((name, Path.GetFileName(path), primitives.Sum(primitive => primitive.Vertices.Length)));
                files.Add(path);
            }
            bodyLines.AddRange(GlbSkin.BodygroupLines(GlbSkin.GroupBodygroups(sets)));
        }
        var bindPath = Path.Combine(outDir, "pm_bind.smd");
        SmdSkeleton.WriteAnimation(rig.Skin, [rig.Skin.BindPose()], bindPath, new());
        files.Add(bindPath);
        var bindLines = SmdSkeleton.DefineBoneLines(rig.Skin, new(), 90.0);
        bindLines.AddRange(rig.Skin.Joints.Where(joint => joint.Name.StartsWith("ValveBiped.Anim_Attachment_", StringComparison.Ordinal)).Select(joint => $"$bonemerge \"{joint.Name}\""));
        var ikLines = PlayerIkLines(rig.Skin);
        var attachmentAudit = new JsonArray();
        var attachmentLines = AttachmentLines(rig, source, metadataPath, attachmentAudit);
        var animationModelname = (Path.ChangeExtension(modelname, null) + "_animations.mdl").Replace('\\', '/');
        var animation = PlayerAnimations.WritePlayerAnimationLibrary(sources, Path.Combine(outDir, "player_animations"), rig.Skin, animationModelname, [.. bindLines, .. attachmentLines], ikLines, profile, context);
        files.AddRange(animation.Files);
        var lines = new List<string> { $"$modelname \"{modelname}\"", $"$cdmaterials \"{cdmaterials}\"", $"$surfaceprop \"{surfaceprop}\"", "$contents \"solid\"" };
        lines.AddRange(bodyLines);
        lines.AddRange(bindLines);
        lines.Add($"$root \"{rig.Skin.Joints[rig.Skin.RootIndex].Name}\"");
        lines.AddRange(attachmentLines);
        lines.AddRange(ikLines);
        lines.AddRange(hitboxLines);
        var vrd = appearanceDrivers.Get("twist_constraints").Bool() ? "" : ProceduralBones.BuildVrd(rig.Skin, context);
        if (vrd.Contains("<helper>", StringComparison.Ordinal))
        {
            var path = Path.Combine(outDir, "pm_helpers.vrd");
            PythonText.Write(path, vrd);
            files.Add(path);
            lines.Add($"$proceduralbones \"{Path.GetFileName(path)}\"");
        }
        if (nativePhysics is null)
        {
            var physicalSource = Path.Combine(Path.GetDirectoryName(glb) ?? "", PathText.Stem(glb) + "_phys.txt");
            nativePhysics = RagdollPhysics.Prepare(File.Exists(physicalSource) ? physicalSource : null, source, coord, fidelityData.Get("source_data") as JsonObject, null, frictionScale);
        }
        var physics = RagdollPhysics.AdaptToPlayermodel(nativePhysics, rig.Skin, rig.Names, yawMatrix);
        var collision = Path.Combine(outDir, "pm_collision.smd");
        var collisionLines = physics.WriteCollision(collision);
        files.Add(collision);
        var auditManifest = PhyAudit.WriteAuditManifest(Path.Combine(outDir, "pm_physics_audit.json"), physics);
        files.Add(auditManifest);
        var sourceAudit = new JsonArray();
        var sourceWarnings = new List<string>();
        var morphAudit = new JsonObject { ["clips"] = new JsonArray(), ["unsupported"] = new JsonArray() };
        if (includeSourceAnimations)
        {
            var metadata = ModelMetadata.Load(metadataPath);
            var (sourceLines, sourceFiles, audit, warnings) = SourceSequences(document, source, rig, outDir, metadata, fidelityData ?? [], context);
            sourceAudit = audit;
            sourceWarnings = warnings;
            lines.AddRange(sourceLines);
            files.AddRange(sourceFiles);
            var exported = sourceAudit.Where(item => item.Need("status").Str() is "preserved" or "adapted").ToList();
            var names = exported.ToDictionary(item => item.Need("name").Str(), item => item.Need("target_name").Str(), StringComparer.Ordinal);
            var durations = exported.ToDictionary(item => item.Need("name").Str(), item => (item.Need("frames").Int() - 1) / item.Need("fps").Dbl(), StringComparer.Ordinal);
            var morph = MorphAnimation.ExportMorphAnimations(document, names, durations, context: context);
            morphAudit = morph.Report;
            if (morph.Clips.Count > 0) appearanceDrivers["morph_animations"] = morph.Clips;
        }
        lines.AddRange(["$sequence \"ragdoll\" \"pm_bind.smd\" fps 30 activity \"ACT_DIERAGDOLL\" 1", $"$includemodel \"{animationModelname}\""]);
        lines.AddRange(collisionLines);
        var qc = Path.Combine(outDir, PathText.Stem(modelname) + ".qc");
        PythonText.Write(qc, string.Join('\n', lines) + "\n");
        files.Add(qc);
        var report = rig.Report.DeepClone().AsObject();
        report["profile"] = profile;
        report["animation_source_revision"] = animation.SourceRevision;
        report["compiled_bones"] = rig.Skin.Joints.Count;
        report["retargeted_animations"] = animation.Animations;
        report["retargeted_frames"] = animation.Frames;
        report["motion_retargeting"] = animation.Retargeting.DeepClone();
        report["animation_library"] = animationModelname;
        var boneNames = new JsonObject();
        foreach (var (name, target) in rig.Names) boneNames[name] = target;
        report["original_bone_names"] = boneNames;
        report["appearance"] = appearanceReport.DeepClone();
        report["attachments"] = new JsonObject
        {
            ["count"] = Count(attachmentAudit, status => status != "unsupported"), ["source_count"] = Count(attachmentAudit, status => status != "generated"), ["entries"] = attachmentAudit.DeepClone()
        };
        var hitboxAdaptations = new JsonArray();
        if (hitboxAudit.Count == 0) hitboxAdaptations.Add("No authored source hitboxes; Source 1 generates generic boxes from the playermodel mesh");
        report["hitboxes"] = new JsonObject
        {
            ["count"] = hitboxAudit.Count > 0 ? Count(hitboxAudit, status => status == "preserved") : null,
            ["status"] = hitboxAudit.Count == 0 ? "generated" : Count(hitboxAudit, status => status == "unsupported") > 0 ? "adapted" : "preserved",
            ["source_count"] = hitboxAudit.Count, ["entries"] = hitboxAudit.DeepClone(), ["adaptations"] = hitboxAdaptations
        };
        report["morph_animations"] = morphAudit.DeepClone();
        report["physics"] = physics.Report.DeepClone();
        report["physics_audit_manifest"] = auditManifest;
        report["material_map"] = appearanceReport.Get("material_map")?.DeepClone() ?? new JsonObject();
        report["material_aliases"] = appearanceReport.Get("material_aliases")?.DeepClone() ?? new JsonObject();
        report["animations"] = new JsonObject
        {
            ["include_source_animations"] = includeSourceAnimations, ["source_clips"] = sourceAudit.DeepClone(), ["profile"] = profile, ["own_library"] = animationModelname
        };
        report["physics_adaptation"] = "Source collision rebound to the generated playermodel bone frames";
        if (appearanceDrivers.Count > 0)
        {
            var driver = RagdollRuntime.WriteModelDriver(outDir, modelname, appearanceDrivers);
            report["addon_drivers"] = JsonKeys.Strings([driver]);
            files.AddRange(RagdollRuntime.ModelDriverFiles(driver));
        }
        var attachmentWarnings = attachmentAudit.Where(item => item.Need("status").Str() == "unsupported")
            .Select(item => $"{item.Need("name").Str()}: PM attachment requires manual authoring ({item.Need("reason").Str()})").ToList();
        attachmentWarnings.AddRange(attachmentAudit.Where(item => item.Get("source_name").Bool())
            .Select(item => $"{item.Need("source_name").Str()}: source frame is available as {item.Need("name").Str()}; GMod uses its canonical hand frame"));
        var hitboxWarnings = hitboxAudit.Where(item => item.Need("status").Str() == "unsupported")
            .Select(item => $"{item.Need("name").Str()}: PM hitbox requires manual authoring ({item.Need("reason").Str()})").ToList();
        if (hitboxAudit.Count == 0) hitboxWarnings.Add("No authored source hitboxes; Source 1 generates generic playermodel hitboxes");
        var distinctWarnings = physics.Warnings.Concat(sourceWarnings).Concat(attachmentWarnings).Concat(hitboxWarnings).Distinct(StringComparer.Ordinal).ToList();
        report["warnings"] = JsonKeys.Strings(distinctWarnings);
        var reportPath = Path.Combine(outDir, "playermodel_report.json");
        PythonText.Write(reportPath, PythonJson.Indented(report));
        files.Add(reportPath);
        context.Log($"Original-shape playermodel sources written: {rig.Skin.Joints.Count} bones", LogLevel.Success);
        return new(qc, animation.Qc, files, report, distinctWarnings)
        {
            Bones = rig.Skin.Joints.Count, Profile = profile, BoneNameMap = rig.Names, TargetSkin = rig.Skin, UnitScale = rig.UnitScale, Animations = animation, PhysicsAuditManifest = auditManifest
        };
    }
}
