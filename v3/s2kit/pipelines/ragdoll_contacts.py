"""Adapt inferred humanoid self-contact to Source 1 without changing authored joint flags."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any


_HUMANOID_ALIASES = {
    'pelvis': ('pelvis', 'valvebiped.bip01_pelvis'),
    'chest': ('spine_2', 'valvebiped.bip01_spine2'),
    'upper_arm_left': ('arm_upper_l', 'valvebiped.bip01_l_upperarm'),
    'upper_arm_right': ('arm_upper_r', 'valvebiped.bip01_r_upperarm'),
    'forearm_left': ('arm_lower_l', 'valvebiped.bip01_l_forearm'),
    'forearm_right': ('arm_lower_r', 'valvebiped.bip01_r_forearm'),
    'hand_left': ('hand_l', 'valvebiped.bip01_l_hand'),
    'hand_right': ('hand_r', 'valvebiped.bip01_r_hand'),
    'thigh_left': ('leg_upper_l', 'valvebiped.bip01_l_thigh'),
    'thigh_right': ('leg_upper_r', 'valvebiped.bip01_r_thigh'),
    'calf_left': ('leg_lower_l', 'valvebiped.bip01_l_calf'),
    'calf_right': ('leg_lower_r', 'valvebiped.bip01_r_calf'),
    'foot_left': ('ankle_l', 'valvebiped.bip01_l_foot'),
    'foot_right': ('ankle_r', 'valvebiped.bip01_r_foot'),
}


@dataclass(frozen=True)
class ContactPolicyResult:
    """Final helper-name collision pairs and durable source-part adaptation evidence."""

    pairs: list[list[str]]
    report: dict[str, Any]


def _pair(first: int, second: int) -> tuple[int, int]:
    """Canonicalize a non-self source-part pair."""
    if first == second:
        raise ValueError('A source collision flag refers to the same body twice')
    return tuple(sorted((first, second)))


def _pair_records(pairs: set[tuple[int, int]], bodies: dict[int, dict]) -> list[dict]:
    """Resolve source-part pairs without assuming source and compiled body indices match."""
    return [
        {'parts': list(pair),
         'source_bones': [bodies[part]['source_bone'] for part in pair],
         'helpers': [bodies[part]['helper'] for part in pair]}
        for pair in sorted(pairs)
    ]


def _humanoid_roles(bodies: dict[int, dict]) -> tuple[dict[str, dict], list[str]]:
    """Require uniquely authored bilateral limb semantics before applying a humanoid policy."""
    roles, unresolved = {}, []
    for role, aliases in _HUMANOID_ALIASES.items():
        candidates = [body for body in bodies.values()
                      if str(body['source_bone']).casefold() in aliases]
        if len(candidates) != 1:
            unresolved.append(f'{role}: {len(candidates)} matching source bodies')
        else:
            body = candidates[0]
            roles[role] = {key: body[key] for key in ('part', 'source_bone', 'helper')}
    return roles, unresolved


def _humanoid_pairs(roles: dict[str, dict]) -> set[tuple[int, int]]:
    """Keep the tested thirteen human support contacts while avoiding internal pinching loops."""
    role_pairs = [
        (forearm, other)
        for forearm in ('forearm_left', 'forearm_right')
        for other in ('pelvis', 'chest', 'thigh_left', 'thigh_right')
    ]
    role_pairs.extend([
        ('thigh_left', 'thigh_right'),
        ('calf_left', 'calf_right'),
        ('foot_left', 'foot_right'),
        ('foot_left', 'calf_right'),
        ('foot_right', 'calf_left'),
    ])
    return {_pair(roles[first]['part'], roles[second]['part'])
            for first, second in role_pairs}


def derive_ragdoll_contacts(bodies: list[dict], source_joints: list[dict]) -> ContactPolicyResult:
    """Adapt only inferred contacts; every explicit joint collision flag has precedence.

    PHYS joints provide collision flags for their own endpoints, rather than a complete
    pair table. The original fallback enables all remaining body pairs. Recognized human
    rigs instead retain thirteen tested support contacts and any explicitly enabled joint
    pair. Unrecognized rigs retain that original inference. World and inter-entity
    collision are unaffected. Source-part indices and authored flags remain in the report.
    """
    by_part = {int(body['part']): body for body in bodies}
    if len(by_part) != len(bodies) or len({body['helper'] for body in bodies}) != len(bodies):
        raise ValueError('Collision bodies require unique source parts and helper names')
    flags, enabled, disabled = [], set(), set()
    for index, joint in enumerate(source_joints):
        pair = _pair(int(joint['m_nBody1']), int(joint['m_nBody2']))
        if any(part not in by_part for part in pair):
            raise ValueError('A source collision flag refers to an absent physics body')
        value = joint.get('m_bEnableCollision', False)
        if not isinstance(value, (bool, int)) or value not in (False, True):
            raise ValueError('Source joint collision flags must be boolean')
        (enabled if value else disabled).add(pair)
        record = _pair_records({pair}, by_part)[0]
        generated = bool(joint.get('_generated'))
        authored = 'm_bEnableCollision' in joint and not generated
        record.update(source_joint=index, enabled=bool(value), authored=authored,
                      provenance=('generated_ao_proxy_joint' if generated else
                                  'authored_phys_joint' if authored else 'default_joint_flag'))
        flags.append(record)
    if enabled & disabled:
        raise ValueError('Conflicting source joint collision flags for the same body pair')
    parts = sorted(by_part)
    all_pairs = {(first, second) for first in parts for second in parts if first < second}
    original = all_pairs - disabled
    roles, unresolved = _humanoid_roles(by_part)
    recognized = not unresolved
    retained = ((_humanoid_pairs(roles) | enabled) - disabled) if recognized else original
    excluded = original - retained
    records = _pair_records(retained, by_part)
    report = {
        'policy': 'source1_humanoid_semantic_v1' if recognized else 'source_inferred_non_neighbor',
        'status': 'adapted' if excluded else 'preserved_inference',
        'rig_class': 'humanoid' if recognized else 'unclassified',
        'reason': ('Source 1 humanoid support contacts avoid unstable internal pinching loops; '
                   'this replaces an inferred all-non-neighbor pair set, not an authored full pair table.'
                   if recognized else 'Humanoid semantics are incomplete or ambiguous; retain the original pair inference.'),
        'world_and_other_entity_contacts': 'unchanged',
        'authored_joint_collision_flags_preserved': True,
        'roles': roles if recognized else {},
        'unresolved_roles': unresolved,
        'possible_pair_count': len(all_pairs),
        'original_inferred_pair_count': len(original),
        'retained_pair_count': len(retained),
        'excluded_inferred_pair_count': len(excluded),
        'source_joint_flags': flags,
        'original_inferred_pairs': _pair_records(original, by_part),
        'retained_pairs': records,
        'excluded_inferred_pairs': _pair_records(excluded, by_part),
    }
    return ContactPolicyResult([record['helpers'][:] for record in records], report)


def remap_contact_report(report: dict, helper_names: dict[str, str]) -> dict:
    """Rename a native contact report for a companion rig without changing source provenance."""
    result = copy.deepcopy(report)
    for role in result.get('roles', {}).values():
        role['helper'] = helper_names[role['helper']]
    for field in ('source_joint_flags', 'original_inferred_pairs', 'retained_pairs',
                  'excluded_inferred_pairs'):
        for pair in result.get(field, []):
            pair['helpers'] = [helper_names[name] for name in pair['helpers']]
    return result
