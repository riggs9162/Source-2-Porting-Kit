"""Build a source-shaped humanoid playermodel with its own GMod animation graph."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from s2kit.pipelines.dmx_mesh import smd_vertex_line
from s2kit.pipelines.glb_skin import (
    bodygroup_lines, collect_skinned_prims, group_bodygroups, read_glb,
)
from s2kit.pipelines.gltf_animation import (
    GltfJoint, GltfSkin, SOURCE2_BASIS, derive_bind_pose_local, parse_clips, parse_skin,
)
from s2kit.pipelines.native_port import coordinate_mode, validate_native_data
from s2kit.pipelines.player_animations import (
    PlayerAnimationResult, animation_worlds, ensure_animation_sources, source_bind,
    write_animation_smd, write_player_animation_library,
)
from s2kit.pipelines.porting import check_cancelled
from s2kit.pipelines.ragdoll_animations import sample_authored_clip
from s2kit.pipelines.procedural_bones import build_vrd
from s2kit.pipelines.skeleton_retarget import (
    _swing, measure_alignment_yaw, skin_world_matrices, yaw_matrix,
)
from s2kit.pipelines.smd_animation_export import (
    CoordinateMode, SmdAnimationExporter, derive_definebone_lines,
)
from s2kit.pipelines.valvebiped import auxiliary_name, is_canonical, valvebiped_name


@dataclass
class PlayerModelResult:
    """Source files and explicit skeleton adaptations for an optional playermodel."""

    qc: Path
    animation_qc: Path
    files: list[Path] = field(default_factory=list)
    bones: int = 0
    profile: str = "male"
    warnings: list[str] = field(default_factory=list)
    report: dict = field(default_factory=dict)
    bone_name_map: dict[str, str] = field(default_factory=dict)
    target_skin: GltfSkin | None = None
    source_world: dict[int, np.ndarray] = field(default_factory=dict)
    target_world: dict[int, np.ndarray] = field(default_factory=dict)
    mesh_rotation: np.ndarray = field(default_factory=lambda: np.eye(3))
    unit_scale: float = 1.0
    animations: PlayerAnimationResult | None = None
    physics_audit_manifest: Path | None = None


@dataclass
class PlayerRig:
    """A source-sized ValveBiped hierarchy and its reversible source correspondence."""

    skin: GltfSkin
    indices: dict[int, int]
    names: dict[str, str]
    rotation: np.ndarray
    unit_scale: float
    source_world: dict[int, np.ndarray]
    world: dict[int, np.ndarray]
    report: dict


def is_humanoid_skin(skin: GltfSkin) -> bool:
    """Require a pelvis, head and complete two-arm/two-leg chains."""
    names = {valvebiped_name(joint.name) for joint in skin.joints}
    required = {"ValveBiped.Bip01_Pelvis", "ValveBiped.Bip01_Head1"}
    required.update(f"ValveBiped.Bip01_{side}_{bone}" for side in ("L", "R")
                    for bone in ("UpperArm", "Forearm", "Hand", "Thigh", "Calf", "Foot"))
    return required <= names


def _native_worlds(skin: GltfSkin, coord: CoordinateMode) -> dict[int, np.ndarray]:
    """Express original bind worlds in Source inches without fitting donor geometry."""
    basis = SOURCE2_BASIS[:3, :3] if coord.swap_axes else np.eye(3)
    worlds = dict(enumerate(skin_world_matrices(skin)))
    for matrix in worlds.values():
        matrix[:3, 3] = coord.scale * (basis @ matrix[:3, 3])
        matrix[:3, :3] = basis @ matrix[:3, :3]
    return worlds


def _depth(index: int, parents: dict[int, int | None]) -> int:
    """Count the ancestors in an already acyclic target hierarchy."""
    result = 0
    while parents.get(index) is not None:
        index = parents[index]
        result += 1
        if result > len(parents):
            raise ValueError("Playermodel hierarchy contains a cycle")
    return result


def _source_bone_names(source: GltfSkin) -> tuple[dict[int, str], list[str]]:
    """Match three-joint thumbs whether the native base is named meta or phalanx zero."""
    original = {joint.name for joint in source.joints}
    names = {index: auxiliary_name(joint.name) for index, joint in enumerate(source.joints)}
    adapted = []
    for side in ("L", "R"):
        if f"finger_thumb_meta_{side}" not in original or f"finger_thumb_2_{side}" in original:
            continue
        mapping = {
            f"finger_thumb_meta_{side}": f"ValveBiped.Bip01_{side}_Finger0",
            f"finger_thumb_0_{side}": f"ValveBiped.Bip01_{side}_Finger01",
            f"finger_thumb_1_{side}": f"ValveBiped.Bip01_{side}_Finger02",
        }
        for index, joint in enumerate(source.joints):
            if joint.name in mapping:
                names[index] = mapping[joint.name]
        adapted.append(side)
    return names, adapted


def build_player_rig(source: GltfSkin, coord: CoordinateMode, donor_bind,
                     *, align_yaw: float | None = None) -> PlayerRig:
    """Keep source joint worlds and establish canonical axes for GMod motion and IK."""
    if not is_humanoid_skin(source):
        raise ValueError("This model has no supported humanoid playermodel skeleton")
    original = _native_worlds(source, coord)
    names, adapted_thumbs = _source_bone_names(source)
    root = next(index for index, name in names.items() if name == "ValveBiped.Bip01_Pelvis")
    donor_rotations, donor_positions = animation_worlds(donor_bind)
    donor = {}
    for index, name in enumerate(donor_bind.names):
        matrix = np.eye(4)
        matrix[:3, :3] = donor_rotations[0, index]
        matrix[:3, 3] = donor_positions[0, index]
        donor[name] = matrix
    by_name = {name: original[index] for index, name in names.items() if name in donor}
    measured = measure_alignment_yaw(by_name, donor, "ValveBiped.Bip01_Pelvis")
    yaw = measured if align_yaw is None else float(align_yaw)
    turn = yaw_matrix(yaw)
    worlds = {}
    for index, matrix in original.items():
        placed = matrix.copy()
        placed[:3, 3] = turn @ matrix[:3, 3]
        placed[:3, :3] = turn @ matrix[:3, :3]
        worlds[index] = placed
    ancestors = set()
    parent = source.joints[root].parent_joint_idx
    while parent is not None:
        ancestors.add(parent)
        parent = source.joints[parent].parent_joint_idx
    kept = list(range(len(source.joints)))
    parents = {}
    for index in kept:
        if index == root:
            parents[index] = None
            continue
        parent = source.joints[index].parent_joint_idx
        if is_canonical(names[index]):
            while parent is not None and not is_canonical(names.get(parent, "")):
                parent = source.joints[parent].parent_joint_idx
        parents[index] = root if parent is None else parent
    children = {}
    for index, parent in parents.items():
        if parent is not None and names[index] in donor:
            children.setdefault(parent, []).append(index)
    changed_axes = []
    for index in kept:
        name = names[index]
        if name not in donor:
            continue
        reference = donor[name]
        candidates = [child for child in children.get(index, []) if names[child] in donor]
        if candidates and name != "ValveBiped.Bip01_Pelvis":
            axis = reference[:3, 0]
            child = max(candidates, key=lambda item: float(np.dot(
                donor[names[item]][:3, 3] - reference[:3, 3], axis)) /
                max(np.linalg.norm(donor[names[item]][:3, 3] - reference[:3, 3]), 1e-9))
            direction = worlds[child][:3, 3] - worlds[index][:3, 3]
            worlds[index][:3, :3] = _swing(axis, direction) @ reference[:3, :3]
        else:
            worlds[index][:3, :3] = reference[:3, :3]
        changed_axes.append(name)
    order = sorted(kept, key=lambda index: (_depth(index, parents), index))
    index_map = {old: new for new, old in enumerate(order)}
    joints = []
    for old in order:
        parent = parents[old]
        local = worlds[old] if parent is None else np.linalg.inv(worlds[parent]) @ worlds[old]
        joints.append(GltfJoint(names[old], source.joints[old].gltf_node_idx,
                                index_map[parent] if parent is not None else None,
                                np.linalg.inv(worlds[old]), local[:3, 3],
                                Rotation.from_matrix(local[:3, :3]).as_quat(), np.ones(3)))
    target = GltfSkin(joints, index_map[root], source.skin_idx)
    compatibility = _add_missing_channels(target, donor_bind)
    _add_attachment_bones(target, donor_bind)
    if len(target.joints) > 256:
        raise ValueError("Playermodel plus required attachment bones exceeds GMod's 256-bone limit")
    final_world = dict(enumerate(skin_world_matrices(target)))
    worst = max(float(np.linalg.norm(final_world[index_map[old]][:3, 3] - worlds[old][:3, 3]))
                for old in kept)
    if worst > 1e-5:
        raise ValueError(f"Playermodel changed an original joint position by {worst:g} units")
    basis = SOURCE2_BASIS[:3, :3] if coord.swap_axes else np.eye(3)
    report = dict(original_scale_preserved=True, mesh_deformation=False,
                  maximum_joint_position_error=worst, source_bones=len(source.joints),
                  target_bones=len(target.joints), source_alignment_yaw_degrees=yaw,
                  compiler_basis_yaw_degrees=90.0, bone_axes_adapted=changed_axes,
                  canonical_nonsegment_axes=["ValveBiped.Bip01_Pelvis"],
                  compatibility_bones=compatibility,
                  thumb_metacarpal_channels=adapted_thumbs,
                  reparented_root_helpers=[source.joints[index].name for index in sorted(ancestors)])
    return PlayerRig(target, index_map,
                     {joint.name: names[index] for index, joint in enumerate(source.joints)},
                     turn @ basis, coord.scale, original, final_world, report)


def _add_missing_channels(skin: GltfSkin, donor_bind) -> list[dict]:
    """Keep unweighted standard animation channels missing from a native digit rig."""
    names = {joint.name: index for index, joint in enumerate(skin.joints)}
    records = []
    for index, name in enumerate(donor_bind.names):
        if not is_canonical(name) or name in names:
            continue
        parent_index = donor_bind.parents[index]
        parent_name = donor_bind.names[parent_index] if parent_index >= 0 else ""
        if parent_name not in names or "Finger" not in name:
            raise ValueError(f"Native humanoid is missing required GMod animation bone {name}")
        target_parent = names[parent_name]
        worlds = skin_world_matrices(skin)
        parent_world = worlds[target_parent]
        endpoint = re.fullmatch(r"ValveBiped\.Bip01_([LR])_Finger([0-4])2", name)
        native_endpoint = None
        if endpoint:
            side, digit = endpoint.groups()
            finger = {"0": "thumb", "1": "index", "2": "middle", "3": "ring", "4": "pinky"}[digit]
            native_endpoint = names.get(f"z_finger_{finger}_end_{side.lower()}")
        values = donor_bind.frames[0, index].copy()
        if native_endpoint is not None:
            values[:3] = parent_world[:3, :3].T @ (worlds[native_endpoint][:3, 3] - parent_world[:3, 3])
            reason = "authored_digit_endpoint"
        else:
            reference_length = np.linalg.norm(donor_bind.frames[0, parent_index, :3])
            own_length = np.linalg.norm(skin.joints[target_parent].rest_translation)
            values[:3] *= own_length / reference_length if reference_length > 1e-6 else 1.0
            reason = "unweighted_animation_channel"
        local = np.eye(4)
        local[:3, :3] = Rotation.from_euler("xyz", values[3:]).as_matrix()
        local[:3, 3] = values[:3]
        world = parent_world @ local
        names[name] = len(skin.joints)
        skin.joints.append(GltfJoint(name, -1, target_parent, np.linalg.inv(world), values[:3],
                                     Rotation.from_matrix(local[:3, :3]).as_quat(), np.ones(3)))
        records.append(dict(name=name, reason=reason, weighted=False))
    return records


def _add_attachment_bones(skin: GltfSkin, donor_bind) -> None:
    """Keep the GMod hand attachment transforms as children of source-sized hands."""
    donor_indices = {name: index for index, name in enumerate(donor_bind.names)}
    names = {joint.name: index for index, joint in enumerate(skin.joints)}
    for side in ("RH", "LH"):
        name = f"ValveBiped.Anim_Attachment_{side}"
        parent = f"ValveBiped.Bip01_{side[0]}_Hand"
        if name in names or name not in donor_indices or parent not in names:
            continue
        values = donor_bind.frames[0, donor_indices[name]]
        local = np.eye(4)
        local[:3, :3] = Rotation.from_euler("xyz", values[3:]).as_matrix()
        local[:3, 3] = values[:3]
        world = skin_world_matrices(skin)[names[parent]] @ local
        skin.joints.append(GltfJoint(name, -1, names[parent], np.linalg.inv(world), values[:3].copy(),
                                     Rotation.from_euler("xyz", values[3:]).as_quat(), np.ones(3)))


def _source_hitboxes(rig: PlayerRig, source: GltfSkin,
                     metadata_path: Path) -> tuple[list[str], list[dict]]:
    """Keep authored hitbox boxes on nonintrusive branches with their original bone axes."""
    from s2kit.pipelines.model_metadata import ModelMetadata, hitbox_qc_lines, load_model_metadata

    if not metadata_path.is_file():
        return [], []
    metadata = load_model_metadata(metadata_path)
    original = {joint.name: index for index, joint in enumerate(source.joints)}
    target = {joint.name: index for index, joint in enumerate(rig.skin.joints)}
    turn = yaw_matrix(rig.report["source_alignment_yaw_degrees"])
    helper_by_bone = {}
    translated = ModelMetadata()
    audit = []
    for hitbox in metadata.hitboxes:
        record = dict(name=hitbox.name, set=hitbox.set_name, source_bone=hitbox.bone,
                      group=hitbox.group, minimum=list(hitbox.minimum), maximum=list(hitbox.maximum))
        if hitbox.translation_only:
            audit.append(dict(**record, status="unsupported", reason="translation_only_hitbox"))
            continue
        old = original.get(hitbox.bone)
        parent = target.get(rig.names.get(hitbox.bone))
        if old is None or parent is None:
            audit.append(dict(**record, status="unsupported", reason="hitbox_bone_absent"))
            continue
        expected = rig.source_world[old].copy()
        expected[:3, :3] = turn @ expected[:3, :3]
        expected[:3, 3] = turn @ expected[:3, 3]
        rotation_error = float(np.max(np.abs(expected[:3, :3] - rig.world[parent][:3, :3])))
        name = rig.skin.joints[parent].name
        helper = False
        if rotation_error > 1e-7:
            helper = True
            name = helper_by_bone.get(hitbox.bone)
            if name is None:
                name = f"s2kit_hitbox_{len(helper_by_bone):03d}"
                if name in target:
                    raise ValueError(f"Native skeleton already uses reserved hitbox helper {name}")
                local = np.linalg.inv(rig.world[parent]) @ expected
                index = len(rig.skin.joints)
                rig.skin.joints.append(GltfJoint(
                    name, -1, parent, np.linalg.inv(expected), local[:3, 3],
                    Rotation.from_matrix(local[:3, :3]).as_quat(), np.ones(3)))
                rig.world[index] = expected
                target[name] = index
                helper_by_bone[hitbox.bone] = name
        actual = rig.world[target[name]]
        audit.append(dict(**record, status="preserved", target_bone=name, axis_helper=helper,
                          maximum_bind_position_error=float(np.max(np.abs(actual[:3, 3] - expected[:3, 3]))),
                          bind_axis_error_degrees=float(np.degrees(Rotation.from_matrix(
                              actual[:3, :3].T @ expected[:3, :3]).magnitude()))))
        translated.hitboxes.append(replace(hitbox, bone=name))
    if len(rig.skin.joints) > 256:
        raise ValueError("Original-axis hitbox helpers exceed GMod's 256-bone limit")
    rig.report["target_bones"] = len(rig.skin.joints)
    rig.report["hitbox_axis_helpers"] = helper_by_bone
    return hitbox_qc_lines(translated), audit


def remap_source_animation_frames(source: GltfSkin, rig: PlayerRig,
                                  frames: list[dict]) -> np.ndarray:
    """Rebase authored HLA motion onto the PM axes without changing its deformation."""
    count = len(frames)
    local_positions = np.asarray([[frame[index][0] for index in range(len(source.joints))]
                                  for frame in frames], dtype=np.float64)
    local_quaternions = np.asarray([[frame[index][1] for index in range(len(source.joints))]
                                    for frame in frames], dtype=np.float64)
    local_rotations = Rotation.from_quat(local_quaternions.reshape(-1, 4)).as_matrix()
    local_rotations = local_rotations.reshape(count, len(source.joints), 3, 3)
    source_rotations = np.empty_like(local_rotations)
    source_positions = np.empty_like(local_positions)
    for index, joint in enumerate(source.joints):
        parent = joint.parent_joint_idx
        if parent is None:
            source_rotations[:, index] = local_rotations[:, index]
            source_positions[:, index] = local_positions[:, index]
        else:
            source_rotations[:, index] = source_rotations[:, parent] @ local_rotations[:, index]
            source_positions[:, index] = source_positions[:, parent] + np.einsum(
                "fij,fj->fi", source_rotations[:, parent], local_positions[:, index])
    source_rotations = rig.rotation @ source_rotations
    source_positions = (source_positions * rig.unit_scale) @ rig.rotation.T
    original = skin_world_matrices(source)
    by_target = {new: old for old, new in rig.indices.items()}
    target_rotations = {}
    target_positions = {}
    result = np.empty((count, len(rig.skin.joints), 6), dtype=np.float64)
    for index, joint in enumerate(rig.skin.joints):
        old = by_target.get(index)
        parent = joint.parent_joint_idx
        if old is not None:
            bind_rotation = rig.rotation @ original[old][:3, :3]
            bind_position = rig.rotation @ (original[old][:3, 3] * rig.unit_scale)
            delta = source_rotations[:, old] @ bind_rotation.T
            rotation = delta @ rig.world[index][:3, :3]
            position = source_positions[:, old] + np.einsum(
                "fij,j->fi", delta, rig.world[index][:3, 3] - bind_position)
        elif parent is not None:
            rotation = target_rotations[parent] @ Rotation.from_quat(joint.rest_rotation).as_matrix()
            position = target_positions[parent] + np.einsum(
                "fij,j->fi", target_rotations[parent], joint.rest_translation)
        else:
            rotation = np.broadcast_to(Rotation.from_quat(joint.rest_rotation).as_matrix(), (count, 3, 3))
            position = np.broadcast_to(joint.rest_translation, (count, 3))
        target_rotations[index] = rotation
        target_positions[index] = position
        if parent is not None:
            inverse = np.swapaxes(target_rotations[parent], -1, -2)
            position = np.einsum("fij,fj->fi", inverse, position - target_positions[parent])
            rotation = inverse @ rotation
        result[:, index, :3] = position
        result[:, index, 3:] = Rotation.from_matrix(rotation).as_euler("xyz")
    return result


def _source_sequences(gltf, buffer, source, rig, out_dir, metadata, fidelity,
                       *, on_log, cancel) -> tuple[list[str], list[Path], list[dict], list[str]]:
    """Write isolated HLA sequences and masks without overriding player activities."""
    clips = parse_clips(gltf, buffer, source)
    validate_native_data(gltf, source, clips)
    lines, files, audit, warnings = [], [], [], []
    composed = bool(fidelity.get("animation_export", {}).get("additive_composed", False))
    for name, weights in metadata.weightlists.items():
        target_name = "hla_" + name
        if any(char in target_name for char in ('"', "\r", "\n")):
            raise ValueError("Unsafe authored weightlist name")
        lines.append(f'$weightlist "{target_name}" {{')
        for bone, weight in weights.items():
            if bone not in rig.names:
                raise ValueError(f"Authored weightlist references an absent source bone: {bone}")
            lines.append(f'    "{rig.names[bone]}" {float(weight):g}')
        lines.append("}")
    for pose in metadata.poseparameters:
        name = "hla_" + str(pose["m_sName"])
        if any(char in name for char in ('"', "\r", "\n")):
            raise ValueError("Unsafe authored pose parameter name")
        loop = float(pose.get("m_flLoop", 0))
        suffix = f" loop {loop:g}" if loop else ""
        lines.append(f'$poseparameter "{name}" {float(pose["m_flStart"]):g} {float(pose["m_flEnd"]):g}{suffix}')
    for index, clip in enumerate(clips):
        check_cancelled(cancel)
        authored = metadata.animation_for_clip(clip.name)
        if authored is None:
            raise ValueError(f"Authored animation metadata is missing for {clip.name}")
        if authored.delta and not composed:
            warnings.append(f"{clip.name}: source additive channels were not composed; PM copy omitted")
            audit.append(dict(name=clip.name, status="unsupported", reason="uncomposed_additive"))
            continue
        if any(char in clip.name for char in ('"', "\r", "\n", "\\")):
            raise ValueError("Unsafe authored sequence name")
        frames, count, sampling = sample_authored_clip(clip, source, authored)
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", clip.name)[:90] or "animation"
        path = out_dir / f"hla_{index:04d}_{safe}.smd"
        write_animation_smd(path, rig.skin, remap_source_animation_frames(source, rig, frames), list(range(count)))
        options = [f"fps {authored.fps:g}"]
        if authored.loop:
            options.append("loop")
        if authored.snap:
            options.append("snap")
        if authored.hidden:
            options.append("hidden")
        if authored.weightlist:
            options.append(f'weightlist "hla_{authored.weightlist}"')
        target = "hla_" + clip.name
        lines.append(f'$sequence "{target}" "{path.name}" ' + " ".join(options))
        files.append(path)
        audit.append(dict(name=clip.name, target_name=target, frames=count, fps=authored.fps,
                          loop=authored.loop, activities=[], autoplay=False, sampling=sampling,
                          additive_composed_over_bind=bool(authored.delta and composed),
                          status="adapted" if authored.delta else "preserved"))
        on_log(f"Player source animation {index + 1}/{len(clips)}: {clip.name}", "INFO")
    return lines, files, audit, warnings


def player_ik_lines(skin: GltfSkin) -> list[str]:
    """Generate hand and foot chains calibrated to the target's original limbs."""
    names = {joint.name: index for index, joint in enumerate(skin.joints)}
    worlds = dict(enumerate(skin_world_matrices(skin)))
    lines = []
    for side in ("R", "L"):
        for kind, chain in (("Hand", "hand"), ("Foot", "foot")):
            index = names[f"ValveBiped.Bip01_{side}_{kind}"]
            middle = skin.joints[index].parent_joint_idx
            upper = skin.joints[middle].parent_joint_idx
            start, elbow, end = (worlds[item][:3, 3] for item in (upper, middle, index))
            axis = end - start
            axis /= max(float(np.linalg.norm(axis)), 1e-12)
            bend = elbow - start - axis * np.dot(elbow - start, axis)
            if np.linalg.norm(bend) < 1e-4:
                knee = np.array([0.707, -0.707 if chain == "foot" else 0.707, 0.0])
            else:
                knee = worlds[upper][:3, :3].T @ bend
                knee /= np.linalg.norm(knee)
            vector = " ".join(f"{value:.6f}" for value in knee)
            lines.append(f'$ikchain "{side.lower()}{chain}" "{skin.joints[index].name}" knee {vector}')
    lines.extend(['$ikautoplaylock "rfoot" 1 0.1', '$ikautoplaylock "lfoot" 1 0.1'])
    return lines


def _write_reference(path: Path, skin: GltfSkin, primitives: list[dict], rig: PlayerRig,
                     cancel=None) -> None:
    """Write unit/facing-converted geometry with Source 1's three-influence skin limit."""
    from s2kit.pipelines.ragdoll_appearance import source1_skin_links

    ok, error = SmdAnimationExporter.write_animation(
        skin, [derive_bind_pose_local(skin)], path, CoordinateMode())
    if not ok:
        raise ValueError(error)
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write("triangles\n")
        for primitive in primitives:
            check_cancelled(cancel)
            vertices = (np.asarray(primitive["vertices"]) * rig.unit_scale) @ rig.rotation.T
            normals = np.asarray(primitive["normals"]) @ rig.rotation.T
            for face in np.asarray(primitive["indices"]).reshape(-1, 3):
                output.write(primitive["material"] + "\n")
                for vertex in face:
                    uv = primitive["uv"][vertex]
                    links = {}
                    for old, weight in zip(primitive["vj"][vertex], primitive["vw"][vertex]):
                        if weight > 0:
                            new = rig.indices[int(old)]
                            links[new] = links.get(new, 0.0) + float(weight)
                    if not links:
                        raise ValueError("Playermodel vertex has no source skin weights")
                    values = [*vertices[vertex], *normals[vertex], uv[0], 1.0 - uv[1]]
                    output.write(smd_vertex_line(values, source1_skin_links(links.items())))
        output.write("end\n")


def _attachment_lines(rig: PlayerRig, source: GltfSkin, metadata_path: Path | None,
                       audit: list[dict] | None = None) -> list[str]:
    """Retain source attachment frames under aliases when GMod reserves their names."""
    from s2kit.pipelines.model_metadata import (
        ModelMetadata, attachment_qc_lines, load_model_metadata,
    )

    output = []
    audit = audit if audit is not None else []
    authored = set()
    reserved = {f"anim_attachment_{side}".casefold() for side in ("RH", "LH")}
    target_names = {joint.name: index for index, joint in enumerate(rig.skin.joints)}
    if metadata_path and metadata_path.is_file():
        metadata = load_model_metadata(metadata_path)
        used_names = {attachment.name.casefold() for attachment in metadata.attachments} | reserved
        source_names = {joint.name: index for index, joint in enumerate(source.joints)}
        rebased = ModelMetadata()
        for attachment in metadata.attachments:
            if attachment.root_transform or attachment.influences != 1 or attachment.ignore_rotation:
                audit.append(dict(name=attachment.name, status="unsupported",
                                  reason="model_space_or_weighted_attachment_transform"))
                continue
            old = source_names.get(attachment.bone)
            name = rig.names.get(attachment.bone)
            if old is None or name not in target_names:
                audit.append(dict(name=attachment.name, status="unsupported",
                                  reason="attachment_bone_absent"))
                continue
            source_matrix = rig.source_world[old].copy()
            original = np.eye(4)
            original[:3, :3] = Rotation.from_quat(attachment.rotation).as_matrix()
            original[:3, 3] = attachment.position
            placed = source_matrix @ original
            turn = yaw_matrix(rig.report["source_alignment_yaw_degrees"])
            placed[:3, :3] = turn @ placed[:3, :3]
            placed[:3, 3] = turn @ placed[:3, 3]
            local = np.linalg.inv(rig.world[target_names[name]]) @ placed
            output_name = attachment.name
            entry = dict(name=output_name, bone=name, status="preserved")
            if output_name.casefold() in reserved:
                base = f"hla_{output_name}"
                output_name = base
                suffix = 1
                while output_name.casefold() in used_names:
                    output_name = f"{base}_{suffix}"
                    suffix += 1
                used_names.add(output_name.casefold())
                entry.update(name=output_name, source_name=attachment.name, status="adapted",
                             adaptation="Source frame retained under an alias for a reserved GMod attachment")
            rebased.attachments.append(replace(attachment, name=output_name, bone=name,
                                                position=tuple(local[:3, 3]),
                                                rotation=tuple(Rotation.from_matrix(local[:3, :3]).as_quat())))
            authored.add(output_name.casefold())
            audit.append(entry)
        output.extend(attachment_qc_lines(rebased))
    for side in ("RH", "LH"):
        name = f"anim_attachment_{side}"
        output.append(f'$attachment "{name}" "ValveBiped.Anim_Attachment_{side}" 0 0 0 rotate -90 0 -90')
        audit.append(dict(name=name, status="generated", purpose="GMod animation attachment"))
    if "eyes" not in authored:
        output.append('$attachment "eyes" "ValveBiped.Bip01_Head1" 0 0 0')
        audit.append(dict(name="eyes", status="generated", purpose="GMod eye attachment"))
    return output


def port_ragdoll_playermodel(glb_path: Path, out_dir: Path, *, modelname: str,
                             cdmaterials: str = "models/", surfaceprop: str = "flesh",
                             metadata_path: Path | None = None, fidelity=None,
                             animation_cache: Path | None = None, profile: str = "male",
                             align_yaw: float | None = None, friction_scale: float = 1.0,
                             native_physics=None, include_source_animations: bool = False,
                             on_log=None, cancel=None
                             ) -> PlayerModelResult:
    """Generate an optional playermodel and a complete model-owned animation library."""
    log = on_log or (lambda message, level="INFO": None)
    glb_path, out_dir = Path(glb_path), Path(out_dir)
    gltf, buffer = read_glb(glb_path)
    source = parse_skin(gltf, buffer)
    if source is None:
        raise ValueError("Playermodel generation requires a skinned model")
    validate_native_data(gltf, source, [])
    sources = ensure_animation_sources(animation_cache, on_log=log, cancel=cancel)
    rig = build_player_rig(source, coordinate_mode(gltf), source_bind(sources, profile), align_yaw=align_yaw)
    metadata_path = Path(metadata_path) if metadata_path else glb_path.with_suffix(".metadata.json")
    hitbox_lines, hitbox_audit = _source_hitboxes(rig, source, metadata_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    body_lines = []
    appearance_report = {}
    appearance_drivers = {}
    fidelity_data = {}
    fidelity = fidelity if fidelity is not None else glb_path.with_suffix(".fidelity.json")
    if isinstance(fidelity, dict) or Path(fidelity).is_file():
        from s2kit.pipelines.model_fidelity import load_model_fidelity, write_appearance

        fidelity_data = fidelity if isinstance(fidelity, dict) else load_model_fidelity(fidelity)

        def transform_primitives(primitives):
            for primitive in primitives:
                primitive["vertices"] = (primitive["vertices"] * rig.unit_scale) @ rig.rotation.T
                primitive["normals"] = primitive["normals"] @ rig.rotation.T
                for target in primitive.get("targets", {}).values():
                    if "POSITION" in target:
                        target["POSITION"] = (target["POSITION"] * rig.unit_scale) @ rig.rotation.T
                    if "NORMAL" in target:
                        target["NORMAL"] = target["NORMAL"] @ rig.rotation.T

        appearance = write_appearance(gltf, buffer, rig.skin, CoordinateMode(), out_dir,
                                      fidelity=fidelity_data, modelname=modelname,
                                      bone_name_map=rig.names, primitive_transform=transform_primitives,
                                      source_model_rotation=yaw_matrix(rig.report["source_alignment_yaw_degrees"]),
                                      include_source_animations=include_source_animations,
                                      cancel=cancel)
        body_lines.extend(appearance.qc_lines)
        files.extend(appearance.files)
        appearance_report = appearance.report
        appearance_drivers = appearance.addon_drivers
    else:
        mesh_sets, arrays = collect_skinned_prims(gltf, buffer, log)
        sets = []
        for index, (name, primitives) in enumerate(mesh_sets):
            path = out_dir / f"pm_ref_{index:03d}.smd"
            _write_reference(path, rig.skin, primitives, rig, cancel)
            sets.append((name, path.name, sum(len(item["vertices"]) for item in primitives)))
            files.append(path)
        body_lines.extend(bodygroup_lines(group_bodygroups(sets)))
    bind_path = out_dir / "pm_bind.smd"
    ok, error = SmdAnimationExporter.write_animation(
        rig.skin, [derive_bind_pose_local(rig.skin)], bind_path, CoordinateMode())
    if not ok:
        raise ValueError(error)
    files.append(bind_path)
    bind_lines = derive_definebone_lines(rig.skin, CoordinateMode(), 90.0)
    bind_lines.extend(f'$bonemerge "{joint.name}"' for joint in rig.skin.joints
                      if joint.name.startswith("ValveBiped.Anim_Attachment_"))
    ik_lines = player_ik_lines(rig.skin)
    attachment_audit = []
    attachment_lines = _attachment_lines(rig, source, metadata_path, attachment_audit)
    animation_modelname = str(Path(modelname).with_suffix("")) + "_animations.mdl"
    animation = write_player_animation_library(
        sources, out_dir / "player_animations", rig.skin,
        modelname=animation_modelname.replace("\\", "/"), bind_lines=[*bind_lines, *attachment_lines],
        ik_lines=ik_lines, profile=profile, on_log=log, cancel=cancel)
    files.extend(animation.files)
    lines = [f'$modelname "{modelname}"', f'$cdmaterials "{cdmaterials}"',
             f'$surfaceprop "{surfaceprop}"', '$contents "solid"',
             *body_lines, *bind_lines, f'$root "{rig.skin.joints[rig.skin.root_joint_idx].name}"',
             *attachment_lines, *ik_lines, *hitbox_lines]
    vrd = "" if appearance_drivers.get("twist_constraints") else build_vrd(rig.skin, on_log=log)
    if "<helper>" in vrd:
        path = out_dir / "pm_helpers.vrd"
        path.write_text(vrd, encoding="ascii")
        files.append(path)
        lines.append(f'$proceduralbones "{path.name}"')
    from s2kit.pipelines.ragdoll_physics import (
        adapt_physics_to_playermodel, prepare_ragdoll_physics,
    )
    from s2kit.pipelines.phy_audit import write_audit_manifest

    if native_physics is None:
        physical_source = glb_path.with_name(glb_path.stem + "_phys.txt")
        native_physics = prepare_ragdoll_physics(
            physical_source if physical_source.is_file() else None,
            source, coordinate_mode(gltf), model_data=fidelity_data.get("source_data"),
            friction_scale=friction_scale)
    physics = adapt_physics_to_playermodel(
        native_physics, rig.skin, rig.names,
        model_rotation=yaw_matrix(rig.report["source_alignment_yaw_degrees"]))
    collision = out_dir / "pm_collision.smd"
    collision_lines = physics.write_collision(collision)
    files.append(collision)
    audit_manifest = write_audit_manifest(out_dir / "pm_physics_audit.json", physics)
    files.append(audit_manifest)
    source_audit = []
    source_warnings = []
    morph_audit = {"clips": [], "unsupported": []}
    if include_source_animations:
        from s2kit.pipelines.model_metadata import load_model_metadata
        from s2kit.pipelines.morph_animation import export_morph_animations

        metadata = load_model_metadata(metadata_path)
        source_lines, source_files, source_audit, source_warnings = _source_sequences(
            gltf, buffer, source, rig, out_dir, metadata, fidelity_data,
            on_log=log, cancel=cancel)
        lines.extend(source_lines)
        files.extend(source_files)
        exported = [item for item in source_audit if item["status"] in {"preserved", "adapted"}]
        morph = export_morph_animations(
            gltf, buffer,
            names={item["name"]: item["target_name"] for item in exported},
            durations={item["name"]: (item["frames"] - 1) / item["fps"] for item in exported},
            cancel=cancel)
        morph_audit = morph.report
        if morph.clips:
            appearance_drivers["morph_animations"] = morph.clips
    lines.extend(['$sequence "ragdoll" "pm_bind.smd" fps 30 activity "ACT_DIERAGDOLL" 1',
                  f'$includemodel "{animation_modelname.replace(chr(92), "/")}"',
                  *collision_lines])
    qc = out_dir / (Path(modelname).stem + ".qc")
    qc.write_text("\n".join(lines) + "\n", encoding="utf-8")
    files.append(qc)
    report = dict(**rig.report, profile=profile, animation_source_revision=animation.source_revision,
                  compiled_bones=len(rig.skin.joints),
                  retargeted_animations=animation.animations, retargeted_frames=animation.frames,
                  motion_retargeting=animation.retargeting,
                  animation_library=animation_modelname.replace("\\", "/"),
                  original_bone_names=rig.names, appearance=appearance_report,
                  attachments=dict(count=sum(item["status"] != "unsupported" for item in attachment_audit),
                                   source_count=sum(item["status"] != "generated" for item in attachment_audit),
                                   entries=attachment_audit),
                  hitboxes=dict(count=sum(item["status"] == "preserved" for item in hitbox_audit)
                                if hitbox_audit else None,
                                status=("adapted" if any(item["status"] == "unsupported" for item in hitbox_audit)
                                        else "preserved") if hitbox_audit else "generated",
                                source_count=len(hitbox_audit), entries=hitbox_audit,
                                adaptations=[] if hitbox_audit else [
                                    "No authored source hitboxes; Source 1 generates generic boxes from the playermodel mesh"]),
                  morph_animations=morph_audit,
                  physics=physics.report, physics_audit_manifest=str(audit_manifest),
                  material_map=appearance_report.get("material_map", {}),
                  animations=dict(include_source_animations=include_source_animations,
                                  source_clips=source_audit, profile=profile,
                                  own_library=animation_modelname.replace("\\", "/")),
                  physics_adaptation="Source collision rebound to the generated playermodel bone frames")
    if appearance_drivers:
        from s2kit.pipelines.ragdoll_runtime import model_driver_files, write_model_driver

        driver = write_model_driver(out_dir, modelname, appearance_drivers)
        report["addon_drivers"] = [str(driver)]
        files.extend(model_driver_files(driver))
    path = out_dir / "playermodel_report.json"
    attachment_warnings = [f'{item["name"]}: PM attachment requires manual authoring ({item["reason"]})'
                           for item in attachment_audit if item["status"] == "unsupported"]
    attachment_warnings.extend(
        f'{item["source_name"]}: source frame is available as {item["name"]}; GMod uses its canonical hand frame'
        for item in attachment_audit if item.get("source_name"))
    hitbox_warnings = [f'{item["name"]}: PM hitbox requires manual authoring ({item["reason"]})'
                       for item in hitbox_audit if item["status"] == "unsupported"]
    if not hitbox_audit:
        hitbox_warnings.append("No authored source hitboxes; Source 1 generates generic playermodel hitboxes")
    report["warnings"] = list(dict.fromkeys(
        [*physics.warnings, *source_warnings, *attachment_warnings, *hitbox_warnings]))
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    files.append(path)
    log(f"Original-shape playermodel sources written: {len(rig.skin.joints)} bones", "SUCCESS")
    return PlayerModelResult(qc, animation.qc, files, len(rig.skin.joints), profile,
                              warnings=report["warnings"],
                              report=report, bone_name_map=rig.names, target_skin=rig.skin,
                              source_world=rig.source_world, target_world=rig.world,
                              mesh_rotation=rig.rotation, unit_scale=rig.unit_scale,
                              animations=animation, physics_audit_manifest=audit_manifest)
