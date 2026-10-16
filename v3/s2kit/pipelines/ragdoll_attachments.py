"""Convert native attachment and hitbox metadata with explicit Source 1 adaptations."""

from __future__ import annotations

import itertools
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np

from s2kit.pipelines.model_metadata import ModelMetadata, parse_kv3_documents
from s2kit.pipelines.skeleton_retarget import _m2q


def _attachment_records(value) -> dict[str, dict]:
    """Collect original influence arrays from nested MDAT objects without replacing earlier LOD definitions."""
    result = {}
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, list):
            pending.extend(reversed(item))
        elif isinstance(item, dict):
            if "m_influenceWeights" in item and "m_name" in item:
                result.setdefault(str(item["m_name"]), item)
            pending.extend(reversed(list(item.values())))
    return result


def load_attachment_influences(fidelity: dict, source_directory: Path) -> dict[str, dict]:
    """Resolve authored attachment influences from typed MDAT or its retained raw block sidecar."""
    documents = fidelity.get("source_mdat")
    if documents:
        return _attachment_records(documents)
    records = _attachment_records(fidelity.get("source_data", {}))
    reference = fidelity.get("raw_blocks", {}).get("MDAT")
    if reference:
        path = Path(source_directory) / reference
        if path.is_file():
            for name, record in _attachment_records(parse_kv3_documents(path.read_text(encoding="utf-8"))).items():
                records.setdefault(name, record)
    return records


def _dominant_attachment(item, raw: dict | None) -> tuple[object, dict]:
    """Retain the strongest authored attachment influence and record every omitted influence."""
    if raw is None:
        return replace(item, influences=1), dict(
            name=item.name, kind="weighted_attachment", adaptation="first_active_influence",
            selected_bone=item.bone, source_influence_count=item.influences,
            reason="Full source influence arrays are unavailable; the typed sidecar retains its first active influence",
            source=asdict(item), animated_weighted_blending_preserved=False,
        )
    weights = raw.get("m_influenceWeights", [])
    active = [index for index, weight in enumerate(weights) if float(weight) > 0]
    if not active:
        raise ValueError(f"Attachment {item.name} has no positive influence")
    index = max(active, key=lambda candidate: float(weights[candidate]))
    names = raw.get("m_influenceNames", [])
    roots = raw.get("m_bInfluenceRootTransform", [])
    selected = replace(item, bone=str(names[index]) if index < len(names) else "",
                       position=tuple(float(value) for value in raw["m_vInfluenceOffsets"][index]),
                       rotation=tuple(float(value) for value in raw["m_vInfluenceRotations"][index]),
                       root_transform=bool(roots[index]) if index < len(roots) else False,
                       influences=1)
    return selected, dict(name=item.name, kind="weighted_attachment", adaptation="dominant_weight_influence",
                          selected_index=index, selected_bone=selected.bone, selected_weight=float(weights[index]),
                          source_influence_count=len(active), source=raw,
                          reason="Source 1 attachments follow one bone; all source influences remain in this report",
                          animated_weighted_blending_preserved=False)


def adapt_native_metadata(metadata: ModelMetadata, *, source_frames: dict | None = None,
                          raw_attachments: dict[str, dict] | None = None) -> tuple[ModelMetadata, dict]:
    """Preserve representable metadata and explicitly adapt weighted or rotation-independent fields."""
    frames, raw = source_frames or {}, raw_attachments or {}
    attachments, hitboxes = [], []
    attachment_adaptations, hitbox_adaptations = [], []
    for original in metadata.attachments:
        item = original
        if item.influences != 1:
            item, adaptation = _dominant_attachment(item, raw.get(item.name))
            attachment_adaptations.append(adaptation)
        if item.ignore_rotation:
            frame = frames.get(item.bone, {})
            world = np.asarray(frame.get("source_bind_world", np.eye(4)), dtype=float)
            rotation = np.eye(3) if item.root_transform else world[:3, :3].T
            item = replace(item, rotation=tuple(_m2q(rotation)), ignore_rotation=False)
            attachment_adaptations.append(dict(
                name=item.name, kind="ignore_attachment_rotation", adaptation="cancel_source_bind_rotation",
                source=asdict(original), bind_orientation_preserved=bool(frame) or item.root_transform,
                animated_rotation_independence_preserved=False,
                reason="The Source 1 attachment retains model-space orientation at bind and subsequently follows its bone rotation",
            ))
        attachments.append(item)
    for original in metadata.hitboxes:
        item = original
        if item.translation_only:
            frame = frames.get(item.bone, {})
            world = np.asarray(frame.get("source_bind_world", np.eye(4)), dtype=float)
            corners = np.asarray(list(itertools.product(*zip(item.minimum, item.maximum))))
            local = corners @ world[:3, :3]
            minimum, maximum = local.min(axis=0), local.max(axis=0)
            original_volume = float(np.prod(np.asarray(item.maximum) - item.minimum))
            export_volume = float(np.prod(maximum - minimum))
            item = replace(item, minimum=tuple(minimum), maximum=tuple(maximum), translation_only=False)
            hitbox_adaptations.append(dict(
                name=item.name, bone=item.bone, set_name=item.set_name,
                kind="translation_only_hitbox", adaptation="bone_local_enclosing_box_at_bind",
                source=asdict(original), exported_minimum=list(minimum), exported_maximum=list(maximum),
                source_bind_volume_in3=original_volume, exported_volume_in3=export_volume,
                bind_volume_ratio=export_volume / original_volume if original_volume > 0 else None,
                source_bind_available=bool(frame), animated_rotation_independence_preserved=False,
                reason="The bone-local box encloses the source translation-only box at bind and subsequently rotates with its bone",
            ))
        hitboxes.append(item)
    adapted_names = {record["name"] for record in attachment_adaptations + hitbox_adaptations}
    warnings = [warning for warning in metadata.warnings
                if not any(warning.startswith(name + ":") for name in adapted_names)]
    result = replace(metadata, attachments=attachments, hitboxes=hitboxes, warnings=warnings)
    report = dict(
        attachments=dict(status="adapted" if attachment_adaptations else "preserved", count=len(attachments),
                         adaptations=attachment_adaptations),
        hitboxes=dict(status="adapted" if hitbox_adaptations else "preserved", count=len(hitboxes),
                      source_count=len(metadata.hitboxes), adaptations=hitbox_adaptations),
    )
    if not metadata.hitboxes:
        report["hitboxes"].update(
            status="generated", count=None,
            reason="No authored source hitboxes; Source 1 generates generic boxes from the native mesh",
        )
    return result, report
