"""Authored Source 2 model metadata and its representable Source 1 QC fields.

The text reader accepts VRF's KV3 dump syntax, including typed resources,
multiline strings and binary blobs. Binary payloads are skipped: animation
samples still come from the glTF, while these sidecars retain authoring data.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Iterator


_SPACE = re.compile(r'(?:\s+|//[^\n]*(?:\n|$)|/\*.*?\*/|<!--.*?-->)*', re.S)
_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[{}\[\]=,:]|[^\s{}\[\]=,:"#]+')
_HEADER = re.compile(r'<!--\s*kv3\b.*?-->', re.S)


class MetadataError(ValueError):
    """A model metadata document is malformed or cannot be represented."""


class _Reader:
    """Read the nested text KV3 structures emitted by Source 2 Viewer."""

    def __init__(self, text: str, position: int = 0):
        self.text = text
        self.position = position

    def space(self) -> None:
        self.position = _SPACE.match(self.text, self.position).end()

    def token(self) -> str:
        self.space()
        match = _TOKEN.match(self.text, self.position)
        if match is None:
            raise MetadataError(f"Invalid KV3 token at character {self.position}")
        self.position = match.end()
        return match.group()

    def expect(self, value: str) -> None:
        actual = self.token()
        if actual != value:
            raise MetadataError(f"Expected {value!r}, found {actual!r}")

    def value(self) -> Any:
        self.space()
        if self.text.startswith('"""', self.position):
            start = self.position + 3
            end = self.text.find('"""', start)
            if end < 0:
                raise MetadataError("Unclosed multiline KV3 string")
            self.position = end + 3
            return self.text[start:end]
        if self.text.startswith('#[', self.position):
            end = self.text.find(']', self.position + 2)
            if end < 0:
                raise MetadataError("Unclosed KV3 binary blob")
            self.position = end + 1
            return None
        token = self.token()
        if token == '{':
            result = {}
            while True:
                key = self.token()
                if key == '}':
                    return result
                if key == ',':
                    continue
                self.expect('=')
                result[json.loads(key) if key.startswith('"') else key] = self.value()
        if token == '[':
            values = []
            while True:
                self.space()
                if self.position >= len(self.text):
                    raise MetadataError("Unclosed KV3 array")
                char = self.text[self.position]
                if char == ']':
                    self.position += 1
                    return values
                if char == ',':
                    self.position += 1
                    continue
                values.append(self.value())
        if token.startswith('"'):
            return json.loads(token)
        self.space()
        if self.text.startswith(':', self.position):
            self.position += 1
            return self.value()
        if token in ('true', 'false', 'null'):
            return {'true': True, 'false': False, 'null': None}[token]
        try:
            return int(token, 0)
        except ValueError:
            try:
                return float(token)
            except ValueError:
                return token


def parse_kv3_documents(text: str) -> list[dict[str, Any]]:
    """Read every KV3 document in a VRF block dump, ignoring its CLI banner."""
    documents = []
    position = 0
    while True:
        header = _HEADER.search(text, position)
        if header is None:
            if not documents and text.lstrip().startswith('{'):
                reader = _Reader(text)
            else:
                break
        else:
            reader = _Reader(text, header.end())
        value = reader.value()
        if not isinstance(value, dict):
            raise MetadataError("A model metadata document must be an object")
        documents.append(value)
        position = reader.position
    return documents


@dataclass(frozen=True)
class Attachment:
    """One authored attachment, local to its bone or the model's root transform."""

    name: str
    bone: str
    position: tuple[float, ...]
    rotation: tuple[float, ...]
    ignore_rotation: bool = False
    root_transform: bool = False
    influences: int = 1


@dataclass(frozen=True)
class Hitbox:
    """An authored bone-local hitbox, with its hitbox set and damage group."""

    set_name: str
    name: str
    bone: str
    group: int
    minimum: tuple[float, ...]
    maximum: tuple[float, ...]
    translation_only: bool = False


@dataclass(frozen=True)
class AnimationMetadata:
    """Clip timing and flags from ANIM; delta describes the source encoding."""

    name: str
    fps: float
    frames: int
    loop: bool
    delta: bool
    hidden: bool = False
    activities: tuple[tuple[str, int], ...] = ()
    source_name: str = ''
    snap: bool = False
    autoplay: bool = False
    weightlist: str = ''
    fade_in: float = 0.2
    fade_out: float = 0.2
    events: tuple[dict[str, Any], ...] = ()


@dataclass
class ModelMetadata:
    """Portable authored fields alongside diagnostics for unsupported data."""

    model_name: str = ''
    surfaceprop: str = ''
    attachments: list[Attachment] = field(default_factory=list)
    hitboxes: list[Hitbox] = field(default_factory=list)
    animations: dict[str, AnimationMetadata] = field(default_factory=dict)
    sequences: list[dict[str, Any]] = field(default_factory=list)
    weightlists: dict[str, dict[str, float]] = field(default_factory=dict)
    poseparameters: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def animation_for_clip(self, name: str) -> AnimationMetadata | None:
        """Match exact names, or an unambiguous VRF leading-@ alias."""
        if name in self.animations:
            return self.animations[name]
        matches = [value for key, value in self.animations.items()
                   if key.lstrip('@') == name.lstrip('@')]
        return matches[0] if len(matches) == 1 else None


def _objects(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def _vector(value: Any, count: int, label: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != count:
        raise MetadataError(f"{label} must contain {count} numbers")
    result = tuple(float(number) for number in value)
    if not all(math.isfinite(number) for number in result):
        raise MetadataError(f"{label} contains a non-finite number")
    return result


def _authored_attachment(data: dict[str, Any]) -> Attachment:
    """Preserve weighted model-space influences even when their bone name is empty."""
    names = data.get('m_influenceNames') or []
    weights = data.get('m_influenceWeights') or []
    roots = data.get('m_bInfluenceRootTransform') or []
    active = []
    for index, weight in enumerate(weights):
        weight = float(weight)
        if not math.isfinite(weight) or weight < 0:
            raise MetadataError(f"Attachment {data.get('m_name')!r} has an invalid influence weight")
        if weight <= 0:
            continue
        named = index < len(names) and bool(names[index])
        root_transform = index < len(roots) and bool(roots[index])
        if not named and not root_transform:
            raise MetadataError(f"Attachment {data.get('m_name')!r} has an unnamed weighted bone")
        active.append(index)
    if not active:
        raise MetadataError(f"Attachment {data.get('m_name')!r} has no weighted influence")
    index = active[0]
    bone = str(names[index]) if index < len(names) else ''
    root_transform = index < len(roots) and bool(roots[index])
    return Attachment(
        str(data['m_name']), bone,
        _vector(data['m_vInfluenceOffsets'][index], 3, 'Attachment offset'),
        _vector(data['m_vInfluenceRotations'][index], 4, 'Attachment quaternion'),
        bool(data.get('m_bIgnoreRotation')), root_transform, len(active),
    )


def _append_unique(items: list, value: Any, key: Any, warnings: list[str]) -> None:
    existing = next((item for item in items if key(item) == key(value)), None)
    if existing is None:
        items.append(value)
    elif existing != value:
        warning = f"Conflicting metadata for {key(value)!r}; keeping the first authored definition"
        if warning not in warnings:
            warnings.append(warning)


def parse_model_metadata(data_text: str = '', mdat_text: str = '',
                         anim_text: str = '', aseq_text: str = '') -> ModelMetadata:
    """Collect authored fields, deduplicating identical copies in LOD meshes."""
    result = ModelMetadata()
    for text in (data_text, mdat_text, anim_text, aseq_text):
        for document in parse_kv3_documents(text):
            if 'm_modelInfo' in document:
                result.model_name = str(document.get('m_name', ''))
                result.surfaceprop = str(document['m_modelInfo'].get('m_sSurfaceProperty', ''))
            for obj in _objects(document):
                for entry in obj.get('m_attachments', []):
                    attachment = _authored_attachment(entry.get('value', entry))
                    _append_unique(result.attachments, attachment, lambda item: item.name, result.warnings)
                for entry in obj.get('m_hitboxsets', []):
                    hitset = entry.get('value', entry)
                    set_name = str(hitset.get('m_name', entry.get('key', 'default')))
                    for box in hitset.get('m_HitBoxes', []):
                        hitbox = Hitbox(
                            set_name, str(box.get('m_name', '')), str(box['m_sBoneName']),
                            int(box.get('m_nGroupId', 0)),
                            _vector(box['m_vMinBounds'], 3, 'Hitbox minimum'),
                            _vector(box['m_vMaxBounds'], 3, 'Hitbox maximum'),
                            bool(box.get('m_bTranslationOnly')),
                        )
                        _append_unique(result.hitboxes, hitbox,
                                       lambda item: (item.set_name, item.name, item.bone), result.warnings)
                for clip in obj.get('m_animArray', []):
                    flags = clip.get('m_flags') or {}
                    animation = AnimationMetadata(
                        str(clip['m_name']), float(clip.get('fps', 30.0)),
                        int((clip.get('m_pData') or {}).get('m_nFrames', 0)),
                        bool(flags.get('m_bLooping')), bool(flags.get('m_bDelta')),
                        bool(flags.get('m_bHidden')),
                        tuple((str(activity['m_name']), int(activity.get('m_nWeight', 1)))
                              for activity in clip.get('m_activityArray', [])),
                        source_name=str(clip['m_name']),
                        events=tuple(clip.get('m_eventArray') or []),
                    )
                    if not math.isfinite(animation.fps) or animation.fps <= 0:
                        raise MetadataError(f"Invalid authored fps for {animation.name!r}")
                    previous = result.animations.get(animation.name)
                    if previous is not None and previous != animation:
                        result.warnings.append(f"Conflicting animation metadata for {animation.name!r}")
                    else:
                        result.animations[animation.name] = animation
                    if clip.get('m_eventArray'):
                        result.warnings.append(f"{animation.name}: Source 2 animation events require manual translation")
                for sequence in obj.get('m_localS1SeqDescArray', []):
                    names = obj.get('m_localSequenceNameArray') or []
                    indices = (sequence.get('m_fetch') or {}).get('m_localReferenceArray') or []
                    sources = [names[int(index)] for index in indices]
                    masks = obj.get('m_localBoneMaskArray') or []
                    mask_index = int(sequence.get('m_nLocalWeightlist', 0))
                    mask_name = str(masks[mask_index].get('m_sName', '')) if masks else ''
                    result.sequences.append({**sequence, 'source_animations': sources,
                                             'weightlist': mask_name})
                for mask in obj.get('m_localBoneMaskArray', []):
                    bone_names = obj.get('m_localBoneNameArray') or []
                    indices = mask.get('m_nLocalBoneArray') or []
                    weights = mask.get('m_flBoneWeightArray') or []
                    if len(indices) != len(weights):
                        raise MetadataError('Bone mask indices and weights have different lengths')
                    result.weightlists[str(mask['m_sName'])] = {
                        str(bone_names[int(index)]): float(weight)
                        for index, weight in zip(indices, weights)
                    }
                result.poseparameters.extend(obj.get('m_localPoseParamArray') or [])
    for sequence in result.sequences:
        name = str(sequence['m_sName'])
        sources = sequence['source_animations']
        if len(sources) != 1 or sources[0] not in result.animations:
            result.warnings.append(f'{name}: sequence blend or missing animation needs manual authoring')
            continue
        source = result.animations[sources[0]]
        flags = sequence.get('m_flags') or {}
        transition = sequence.get('m_transition') or {}
        activities = tuple((str(activity['m_name']), int(activity.get('m_nWeight', 1)))
                           for activity in sequence.get('m_activityArray', []))
        weightlist = str(sequence.get('weightlist', ''))
        result.animations[name] = replace(
            source, name=name, loop=bool(flags.get('m_bLooping', source.loop)),
            hidden=bool(flags.get('m_bHidden', source.hidden)),
            delta=bool(flags.get('m_bLegacyDelta', source.delta)),
            snap=bool(flags.get('m_bSnap')), autoplay=bool(flags.get('m_bAutoplay')),
            weightlist=weightlist if result.weightlists.get(weightlist) else '',
            fade_in=float(transition.get('m_flFadeInTime', 0.2)),
            fade_out=float(transition.get('m_flFadeOutTime', 0.2)),
            activities=activities or source.activities,
        )
    for attachment in result.attachments:
        if attachment.influences != 1 or attachment.ignore_rotation:
            result.warnings.append(f"{attachment.name}: attachment transform needs manual Source 1 authoring")
        elif attachment.root_transform:
            result.warnings.append(f"{attachment.name}: model-space attachment requires a stationary identity root bone")
    for hitbox in result.hitboxes:
        if hitbox.translation_only:
            result.warnings.append(f"{hitbox.name}: translation-only hitbox has no direct QC equivalent")
    return result


def write_model_metadata(path: Path, metadata: ModelMetadata) -> None:
    """Write a compact metadata sidecar without geometry or encoded animation."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'version': 1, **asdict(metadata)}, indent=2) + '\n', encoding='utf-8')


def load_model_metadata(path: Path) -> ModelMetadata:
    """Load a sidecar path, or the `.metadata.json` sibling of a render glTF."""
    path = Path(path)
    if path.suffix.lower() in ('.glb', '.gltf'):
        path = path.with_suffix('.metadata.json')
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.pop('version', None) != 1:
        raise MetadataError('Unsupported model metadata sidecar version')
    data['attachments'] = [Attachment(**{**item, 'position': tuple(item['position']),
                                       'rotation': tuple(item['rotation'])})
                           for item in data.get('attachments', [])]
    data['hitboxes'] = [Hitbox(**{**item, 'minimum': tuple(item['minimum']),
                                'maximum': tuple(item['maximum'])})
                       for item in data.get('hitboxes', [])]
    data['animations'] = {
        name: AnimationMetadata(**{**item, 'activities': tuple(tuple(activity) for activity in item['activities']),
                                   'events': tuple(item.get('events') or [])})
        for name, item in data.get('animations', {}).items()
    }
    return ModelMetadata(**data)


def _quoted(value: str) -> str:
    if any(char in value for char in ('"', '\n', '\r', '\\')):
        raise MetadataError(f"Name cannot be represented safely in QC: {value!r}")
    return f'"{value}"'


def quaternion_to_qangle(rotation: tuple[float, ...]) -> tuple[float, float, float]:
    """Convert XYZW to pitch/yaw/roll, retaining yaw at Euler singularities."""
    length = math.sqrt(sum(number * number for number in rotation))
    if length < 1e-12:
        raise MetadataError('Cannot convert a zero quaternion')
    x, y, z, w = (number / length for number in rotation)
    r00 = 1 - 2 * (y * y + z * z)
    r10 = 2 * (x * y + w * z)
    r20 = 2 * (x * z - w * y)
    r21 = 2 * (y * z + w * x)
    r22 = 1 - 2 * (x * x + y * y)
    horizontal = math.hypot(r00, r10)
    pitch = math.atan2(-r20, horizontal)
    if horizontal > 1e-8:
        yaw = math.atan2(r10, r00)
        roll = math.atan2(r21, r22)
    else:
        r01 = 2 * (x * y - w * z)
        r11 = 1 - 2 * (x * x + z * z)
        yaw = math.atan2(-r01, r11)
        roll = 0.0
    return tuple(math.degrees(number) for number in (pitch, yaw, roll))


def attachment_qc_lines(metadata: ModelMetadata, *, model_space_bone: str = '') -> list[str]:
    """Emit attachments in source-local inches and QAngle degrees.

    A model-space influence has no authored bone. Its caller must supply an
    identity-bind root bone that stays unanimated in every exported sequence.
    The bone must use the same model coordinate basis as the authored offsets.
    Existing skeleton roots are never chosen implicitly because they can move.
    """
    lines = []
    for item in metadata.attachments:
        if item.influences != 1 or item.ignore_rotation:
            raise MetadataError(f"Attachment {item.name!r} needs manual Source 1 authoring")
        if item.root_transform and not model_space_bone:
            raise MetadataError(f"Attachment {item.name!r} needs an explicit stationary model-space bone")
        bone = model_space_bone if item.root_transform else item.bone
        if not bone:
            raise MetadataError(f"Attachment {item.name!r} has no bone")
        angles = quaternion_to_qangle(item.rotation)
        position = ' '.join(f'{number:.6f}' for number in item.position)
        rotate = ' '.join(f'{number:.6f}' for number in angles)
        lines.append(f'$attachment {_quoted(item.name)} {_quoted(bone)} {position} rotate {rotate}')
    return lines


def hitbox_qc_lines(metadata: ModelMetadata) -> list[str]:
    """Emit authored hitbox sets using the six-bound syntax shared by compilers.

    Display names remain in the sidecar: Source 2013 accepts a name here,
    while CS:GO-derived StudioMDL CE expects additional numeric shape fields.
    """
    lines = []
    active_set = None
    for item in metadata.hitboxes:
        if item.translation_only:
            raise MetadataError(f"Hitbox {item.name!r} is translation-only")
        if item.set_name != active_set:
            lines.append(f'$hboxset {_quoted(item.set_name)}')
            active_set = item.set_name
        bounds = ' '.join(f'{number:.6f}' for number in (*item.minimum, *item.maximum))
        lines.append(f'$hbox {item.group} {_quoted(item.bone)} {bounds}')
    return lines


def weightlist_qc_lines(metadata: ModelMetadata) -> list[str]:
    """Emit authored bone masks; unspecified bones start at zero weight."""
    lines = []
    for name, weights in metadata.weightlists.items():
        if not weights:
            continue
        lines.append(f'$weightlist {_quoted(name)} {{')
        lines.extend(f'    {_quoted(bone)} {weight:g}' for bone, weight in weights.items())
        lines.append('}')
    return lines


def poseparameter_qc_lines(metadata: ModelMetadata) -> list[str]:
    """Declare authored pose-parameter ranges without inventing blend graphs."""
    lines = []
    for item in metadata.poseparameters:
        loop = float(item.get('m_flLoop', 0))
        suffix = f' loop {loop:g}' if loop else (' wrap' if item.get('m_bLooping') else '')
        lines.append(f'$poseparameter {_quoted(str(item["m_sName"]))} '
                     f'{float(item["m_flStart"]):g} {float(item["m_flEnd"]):g}{suffix}')
    return lines


def sequence_qc_options(animation: AnimationMetadata, *, include_delta: bool = True) -> list[str]:
    """Return authored sequence flags; callers control exported delta encoding."""
    options = [f'fps {animation.fps:g}']
    for option, enabled in (('loop', animation.loop), ('hidden', animation.hidden),
                            ('snap', animation.snap), ('autoplay', animation.autoplay),
                            ('delta', animation.delta and include_delta)):
        if enabled:
            options.append(option)
    if animation.weightlist:
        options.append(f'weightlist {_quoted(animation.weightlist)}')
    options.extend((f'fadein {animation.fade_in:g}', f'fadeout {animation.fade_out:g}'))
    for name, weight in animation.activities[:1]:
        if not re.fullmatch(r'ACT_[A-Z0-9_]+', name):
            raise MetadataError(f'Unsafe activity identifier: {name!r}')
        options.append(f'activity {name} {weight}')
    return options
