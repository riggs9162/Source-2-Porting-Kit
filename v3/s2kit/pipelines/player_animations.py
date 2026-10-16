"""Cache the published GMod animation sources and retarget their complete graph."""

from __future__ import annotations

import hashlib
import io
import json
import re
import shlex
import threading
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation

from s2kit.pipelines.gltf_animation import GltfSkin
from s2kit.pipelines.porting import check_cancelled
from s2kit.pipelines.skeleton_retarget import skin_world_matrices


SOURCE_REVISION = "9a588af0cb1a2d53de35496ba1737305a877e50c"
SOURCE_URL = f"https://codeload.github.com/robotboy655/gmod-animations/zip/{SOURCE_REVISION}"
SOURCE_ARCHIVE_SHA256 = "e110fac7292fd946929fd98c4d98f61feddffd2c305a835fe308e86323c28ec7"
SOURCE_CREDITS = "Maxime Lebled, Facepunch Studios LTD / Valve Software"
PROFILES = {
    "male": "player_male_anims_m_anm.qc",
    "female": "player_female_anims_f_anm.qc",
    "zombie": "player_zombie_anims_z_anm.qc",
}
_NODE = re.compile(r'^\s*(\d+)\s+"([^"]+)"\s+(-?\d+)\s*$')


@dataclass
class SmdAnimation:
    """A bone hierarchy and absolute parent-local SMD frames."""

    names: list[str]
    parents: list[int]
    frames: np.ndarray
    times: list[int]


@dataclass
class PlayerAnimationResult:
    """A generated animation library and its source coverage."""

    qc: Path
    source_revision: str = SOURCE_REVISION
    files: list[Path] = field(default_factory=list)
    animations: int = 0
    frames: int = 0
    profile: str = "male"


def _qc_content(line: str) -> str:
    """Remove a QC line comment while retaining quoted strings verbatim."""
    quoted = False
    for index, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        if not quoted and line[index:index + 2] == "//":
            return line[:index]
    return line


def expand_animation_graph(qc_path: Path) -> str:
    """Expand upstream includes/macros without StudioMDL CE's small macro buffer."""
    root = Path(qc_path).parent
    macros = {}

    def expand_line(line: str, depth: int = 0) -> list[str]:
        if depth > 30:
            raise ValueError("Player animation graph contains recursive QC macros")
        content = _qc_content(line).strip()
        if not content:
            return []
        words = shlex.split(content, posix=False)
        if not words or words[0].lower() not in macros:
            return [content]
        arguments, body = macros[words[0].lower()]
        if len(words) - 1 < len(arguments):
            raise ValueError(f"Not enough arguments for player animation macro {words[0]}")
        for name, value in zip(arguments, words[1:]):
            body = re.sub(re.escape("$" + name + "$"), lambda match: value, body, flags=re.I)
        trailing = " ".join(words[len(arguments) + 1:])
        if trailing:
            body += " " + trailing
        output = []
        for value in body.splitlines():
            output.extend(expand_line(value, depth + 1))
        return output

    def read_file(path: Path, active: tuple[Path, ...] = ()) -> list[str]:
        if path in active:
            raise ValueError("Player animation graph contains a recursive include")
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        output = []
        index = 0
        while index < len(lines):
            content = _qc_content(lines[index]).rstrip()
            words = shlex.split(content, posix=False)
            if words and words[0].lower() == "$definemacro":
                continuation = content.endswith("\\\\")
                header = shlex.split(content[:-2] if continuation else content, posix=False)
                body = []
                while continuation:
                    index += 1
                    if index >= len(lines):
                        raise ValueError(f"Unterminated QC macro in {path}")
                    value = _qc_content(lines[index]).rstrip()
                    continuation = value.endswith("\\\\") or lines[index].rstrip().endswith("\\\\")
                    body.append(value[:-2].rstrip() if value.endswith("\\\\") else value)
                macros["$" + header[1].lstrip("$").lower()] = (header[2:], "\n".join(body))
            elif words and words[0].lower() == "$include":
                relative = words[1].strip('"').replace("\\", "/")
                include = root / relative
                if not include.is_file():
                    include = path.parent / relative
                output.extend(read_file(include, (*active, path)))
            else:
                output.extend(expand_line(content))
            index += 1
        return output

    return "\n".join(read_file(Path(qc_path))) + "\n"


def ensure_animation_sources(cache: Path | None = None, *, on_log=None, cancel=None) -> Path:
    """Download the pinned upstream archive once into a verified source cache."""
    log = on_log or (lambda message, level="INFO": None)
    base = Path(cache) if cache else Path(__file__).resolve().parents[2] / ".cache" / "player_animations"
    destination = base / SOURCE_REVISION
    provenance = destination / "s2kit_provenance.json"
    if provenance.is_file():
        record = json.loads(provenance.read_text(encoding="utf-8"))
        if (record.get("revision") == SOURCE_REVISION
                and record.get("sha256") == SOURCE_ARCHIVE_SHA256 and all(
                (destination / name).is_file() for name in PROFILES.values())):
            return destination
    check_cancelled(cancel)
    log("Downloading the published Garry's Mod player animation sources")
    request = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "Source2PortingKit/3"})
    chunks = []
    with urllib.request.urlopen(request, timeout=90) as response:
        while data := response.read(1024 * 1024):
            check_cancelled(cancel)
            chunks.append(data)
    raw = b"".join(chunks)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != SOURCE_ARCHIVE_SHA256:
        raise ValueError("Published animation source archive failed its SHA-256 verification")
    staging = base / (SOURCE_REVISION + ".partial")
    staging.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for entry in archive.infolist():
            check_cancelled(cancel)
            relative = Path(*Path(entry.filename).parts[1:])
            if not relative.parts or entry.is_dir():
                continue
            if relative.suffix.lower() not in {".qc", ".qci", ".smd", ".vrd", ".md"}:
                continue
            target = staging / relative
            if not target.resolve().is_relative_to(staging.resolve()):
                raise ValueError("Animation source archive contains an unsafe path")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(entry))
    for name in PROFILES.values():
        if not (staging / name).is_file():
            raise ValueError(f"Animation source archive is missing {name}")
    record = dict(revision=SOURCE_REVISION, source=SOURCE_URL,
                  sha256=digest, credits=SOURCE_CREDITS)
    (staging / provenance.name).write_text(json.dumps(record, indent=2), encoding="utf-8")
    if destination.exists():
        raise ValueError(f"Incomplete animation cache already exists: {destination}")
    staging.rename(destination)
    log("Published player animation sources cached", "SUCCESS")
    return destination


def read_smd_animation(path: Path) -> SmdAnimation:
    """Read the skeleton blocks without importing mesh triangles or running tools."""
    lines = Path(path).read_text(encoding="utf-8-sig", errors="replace").splitlines()
    names, parents, indices = [], [], {}
    mode = ""
    frames = []
    times = []
    current = None
    for line in lines:
        value = line.strip()
        if value == "nodes":
            mode = "nodes"
        elif value == "skeleton":
            mode = "skeleton"
        elif value == "end":
            if mode == "skeleton":
                if current is not None:
                    frames.append(current)
                break
            mode = ""
        elif mode == "nodes":
            match = _NODE.match(value)
            if match:
                index, name, parent = match.groups()
                indices[int(index)] = len(names)
                names.append(name)
                parents.append(int(parent))
        elif mode == "skeleton" and value.startswith("time "):
            if current is not None:
                frames.append(current)
                current = current.copy()
            else:
                current = np.zeros((len(names), 6), dtype=np.float64)
            times.append(int(value.split()[1]))
        elif mode == "skeleton" and current is not None and value:
            numbers = np.fromstring(value, sep=" ")
            if len(numbers) == 7 and int(numbers[0]) in indices:
                current[indices[int(numbers[0])]] = numbers[1:]
    if not frames or not names:
        raise ValueError(f"Animation SMD has no skeleton: {path}")
    return SmdAnimation(names, [indices.get(parent, -1) for parent in parents],
                        np.asarray(frames), times)


def animation_worlds(animation: SmdAnimation) -> tuple[np.ndarray, np.ndarray]:
    """Evaluate all frames with vectorised rigid forward kinematics."""
    count, bones = animation.frames.shape[:2]
    local = Rotation.from_euler("xyz", animation.frames[:, :, 3:].reshape(-1, 3)).as_matrix()
    local = local.reshape(count, bones, 3, 3)
    rotations = np.empty_like(local)
    positions = np.empty((count, bones, 3), dtype=np.float64)
    completed = set()

    def evaluate(index: int) -> None:
        if index in completed:
            return
        parent = animation.parents[index]
        if parent < 0:
            rotations[:, index] = local[:, index]
            positions[:, index] = animation.frames[:, index, :3]
        else:
            evaluate(parent)
            rotations[:, index] = rotations[:, parent] @ local[:, index]
            positions[:, index] = positions[:, parent] + np.einsum(
                "fij,fj->fi", rotations[:, parent], animation.frames[:, index, :3])
        completed.add(index)

    for index in range(bones):
        evaluate(index)
    return rotations, positions


def source_bind(source_root: Path, profile: str = "male") -> SmdAnimation:
    """Read the upstream explicit bind, including hand attachment bones."""
    sex = "Female" if profile == "female" else "Male"
    text = (source_root / "humans_sdk" / sex / "commonbones.qci").read_text(encoding="utf-8-sig")
    records = []
    for match in re.finditer(r'(?im)^\s*\$definebone\s+"([^"]+)"\s+"([^"]*)"\s+([^\r\n]+)', text):
        name, parent, data = match.groups()
        values = np.fromstring(data, sep=" ")
        if len(values) < 6:
            raise ValueError(f"Malformed source bone definition: {name}")
        records.append((name, parent, np.r_[values[:3], np.radians(values[[5, 3, 4]])]))
    names = [name for name, parent, frame in records]
    indices = {name.lower(): index for index, name in enumerate(names)}
    parents = [indices.get(parent.lower(), -1) for name, parent, frame in records]
    return SmdAnimation(names, parents, np.asarray([[frame for name, parent, frame in records]]), [0])


def write_animation_smd(path: Path, skin: GltfSkin, frames: np.ndarray,
                        times: list[int]) -> None:
    """Write absolute target-local transforms with stable bone order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="ascii", newline="\n") as output:
        output.write("version 1\nnodes\n")
        for index, joint in enumerate(skin.joints):
            parent = joint.parent_joint_idx if joint.parent_joint_idx is not None else -1
            output.write(f'{index} "{joint.name}" {parent}\n')
        output.write("end\nskeleton\n")
        for time, frame in zip(times, frames):
            output.write(f"time {time}\n")
            for index, row in enumerate(frame):
                output.write(str(index) + " " + " ".join(f"{value:.7f}" for value in row) + "\n")
        output.write("end\n")


def retarget_animation(animation: SmdAnimation, bind: SmdAnimation,
                       target: GltfSkin) -> np.ndarray:
    """Transfer donor motion while leaving the target's bind and limb lengths intact."""
    donor_rotation, donor_position = animation_worlds(animation)
    bind_rotation, bind_position = animation_worlds(bind)
    target_world = dict(enumerate(skin_world_matrices(target)))
    source_names = {name.lower(): index for index, name in enumerate(animation.names)}
    bind_names = {name.lower(): index for index, name in enumerate(bind.names)}
    frames = np.zeros((len(animation.frames), len(target.joints), 6), dtype=np.float64)
    world_rotations = {}
    target_bind_rotations = {index: matrix[:3, :3] for index, matrix in target_world.items()}
    for index, joint in enumerate(target.joints):
        name = joint.name.lower()
        source = source_names.get(name)
        rest = bind_names.get(name)
        parent = joint.parent_joint_idx
        local_bind = Rotation.from_quat(joint.rest_rotation).as_matrix()
        if source is not None and rest is not None:
            correction = bind_rotation[0, rest].T @ target_bind_rotations[index]
            world = donor_rotation[:, source] @ correction
        elif parent is not None:
            world = world_rotations[parent] @ local_bind
        else:
            world = np.broadcast_to(local_bind, (len(frames), 3, 3)).copy()
        local = world if parent is None else np.swapaxes(world_rotations[parent], -1, -2) @ world
        frames[:, index, 3:] = Rotation.from_matrix(local).as_euler("xyz")
        frames[:, index, :3] = joint.rest_translation
        if parent is None and source is not None and rest is not None:
            frames[:, index, :3] += donor_position[:, source] - bind_position[0, rest]
        world_rotations[index] = world
    if not np.isfinite(frames).all():
        raise ValueError("Animation retarget produced a non-finite transform")
    return frames


def write_player_animation_library(source_root: Path, out_dir: Path, skin: GltfSkin,
                                   *, modelname: str, bind_lines: list[str],
                                   ik_lines: list[str], profile: str = "male",
                                   on_log=None, cancel: threading.Event | None = None
                                   ) -> PlayerAnimationResult:
    """Mirror and retarget the upstream graph into a model-owned animation library."""
    if profile not in PROFILES:
        raise ValueError(f"Unknown player animation profile: {profile}")
    log = on_log or (lambda message, level="INFO": None)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = PlayerAnimationResult(out_dir / "player_animations.qc", profile=profile)
    bind = source_bind(source_root, profile)
    sources = sorted(source_root.rglob("*.smd"))
    for index, path in enumerate(sources):
        check_cancelled(cancel)
        animation = read_smd_animation(path)
        if "valvebiped.bip01_pelvis" not in {name.lower() for name in animation.names}:
            continue
        destination = out_dir / path.relative_to(source_root)
        frames = retarget_animation(animation, bind, skin)
        write_animation_smd(destination, skin, frames, animation.times)
        result.animations += 1
        result.frames += len(frames)
        result.files.append(destination)
        if index % 25 == 0:
            log(f"Retargeting player animation sources: {index + 1}/{len(sources)}")
    for path in sorted(source_root.rglob("*.qci")):
        destination = out_dir / path.relative_to(source_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        relative = path.relative_to(source_root).as_posix().lower()
        if relative.endswith("/commonbones.qci"):
            text = "\n".join(bind_lines) + "\n"
        elif relative == "humans_sdk/standardhierarchy.qci":
            text = f'$root "{skin.joints[skin.root_joint_idx].name}"\n'
        elif relative == "humans_sdk/standardikchains.qci":
            text = "\n".join(ik_lines) + "\n"
        elif relative.endswith("/hitbox.qci"):
            text = ""
        else:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        destination.write_text(text, encoding="utf-8", newline="\n")
        result.files.append(destination)
    qc = (source_root / PROFILES[profile]).read_text(encoding="utf-8-sig")
    qc = re.sub(r"(?im)^\s*\$modelname\s+.*$", f'$modelname "{modelname}"', qc)
    qc = re.sub(r"(?im)^\s*\$attachment\s+.*$", "", qc)
    qc = re.sub(r"(?im)^\s*\$include\s+\"?Humans_sdk\\male\\hitbox.qci\"?.*$", "", qc)
    if profile == "zombie":
        qc = re.sub(r"(?im)^\s*\$definebone\s+.*$", "", qc)
        qc = qc.replace('$pushd\t"player"', '$pushd "player"\n$include "humans_sdk/male/commonbones.qci"')
    result.qc.write_text(qc, encoding="utf-8", newline="\n")
    upstream_qc = out_dir / "upstream_graph.qci"
    upstream_qc.write_text(qc, encoding="utf-8", newline="\n")
    result.files.append(upstream_qc)
    result.qc.write_text(expand_animation_graph(result.qc), encoding="utf-8", newline="\n")
    result.files.append(result.qc)
    provenance = out_dir / "animation_provenance.json"
    provenance.write_text(json.dumps(dict(revision=SOURCE_REVISION, source=SOURCE_URL,
                                         credits=SOURCE_CREDITS, profile=profile,
                                         animations=result.animations, frames=result.frames),
                                    indent=2), encoding="utf-8")
    result.files.append(provenance)
    log(f"Retargeted {result.animations} animation sources, {result.frames} frames", "SUCCESS")
    return result


def audit_compiled_player_model(model_path: Path, animation_path: Path, *,
                                stock_mdl: Path | None = None, profile: str = "male") -> dict:
    """Check owned bind consistency and the stock player activity/sequence contract."""
    from srctools.filesys import RawFileSystem
    from srctools.mdl import Model
    from s2kit.pipelines.mdl_bones import read_mdl_bones, read_mdl_poseparams

    model_path, animation_path = Path(model_path), Path(animation_path)
    model_bones = read_mdl_bones(model_path)
    animation_bones = {bone.name: bone for bone in read_mdl_bones(animation_path)}
    position_errors = []
    rotation_errors = []
    missing = []
    for bone in model_bones:
        other = animation_bones.get(bone.name)
        if other is None:
            missing.append(bone.name)
            continue
        position_errors.append(float(np.linalg.norm(np.asarray(bone.pos) - other.pos)))
        first, second = np.asarray(bone.quat), np.asarray(other.quat)
        similarity = abs(float(np.dot(first, second) / (np.linalg.norm(first) * np.linalg.norm(second))))
        rotation_errors.append(float(np.degrees(2 * np.arccos(np.clip(similarity, 0, 1)))))
    fs = RawFileSystem(str(animation_path.parent))
    animation = Model(fs, fs[animation_path.name])
    sequence_names = {sequence.label for sequence in animation.sequences}
    activities = {(sequence.label, sequence.act_name, sequence.act_weight)
                  for sequence in animation.sequences}
    poses = read_mdl_poseparams(animation_path)
    report = dict(model_bones=len(model_bones), library_bones=len(animation_bones),
                  missing_library_bones=missing,
                  maximum_bind_position_error=max(position_errors, default=0.0),
                  maximum_bind_rotation_error_degrees=max(rotation_errors, default=0.0),
                  sequences=len(animation.sequences),
                  activities=len({sequence.act_name for sequence in animation.sequences if sequence.act_name}),
                  poseparameters=[dict(name=pose.name, start=pose.start, end=pose.end, loop=pose.loop)
                                  for pose in poses], status="verified")
    if missing or report["maximum_bind_position_error"] > 1e-5 or report["maximum_bind_rotation_error_degrees"] > 0.01:
        raise ValueError("Playermodel and its generated animation library have different bind skeletons")
    expected_sequences = 463 if profile == "female" else 464
    if len(animation.sequences) != expected_sequences or report["activities"] != 271:
        raise ValueError("Generated player animation graph does not match the published complete profile")
    if stock_mdl:
        stock_mdl = Path(stock_mdl)
        stock_fs = RawFileSystem(str(stock_mdl.parent))
        stock = Model(stock_fs, stock_fs[stock_mdl.name])
        wanted_names = {sequence.label for sequence in stock.sequences}
        wanted_activities = {(sequence.label, sequence.act_name, sequence.act_weight)
                             for sequence in stock.sequences}
        report.update(missing_sequences=sorted(wanted_names - sequence_names),
                      extra_sequences=sorted(sequence_names - wanted_names),
                      missing_activity_assignments=sorted(wanted_activities - activities),
                      extra_activity_assignments=sorted(activities - wanted_activities))
        if any(report[key] for key in ("missing_sequences", "extra_sequences",
                                       "missing_activity_assignments", "extra_activity_assignments")):
            raise ValueError("Generated animation library does not preserve the selected stock activity graph")
    return report
