"""Audit compiled source-clip timing and exact flex programs using Valve studio.h tables."""

from __future__ import annotations

import math
import struct
from pathlib import Path

from s2kit.pipelines.compiled_model_audit import ModelAuditError, _Binary


def read_compiled_animation_timing(model_path: Path) -> dict:
    """Resolve local v48/v49 sequence blend indices into 100-byte animation records."""
    source = _Binary(model_path)
    source.require(0, 196)
    if source.data[:4] != b"IDST" or source.unpack("i", 4)[0] not in (48, 49):
        raise ModelAuditError("Animation timing requires a Source 1 v48/v49 studio model")
    count, index = source.unpack("ii", 180)
    animations = []
    for offset in source.table(count, index, 100):
        _, name, fps, flags, frames = source.unpack("iifii", offset)
        if not math.isfinite(fps) or fps <= 0 or frames < 1:
            raise ModelAuditError("Compiled animation has invalid FPS or frame count")
        animations.append(dict(name=source.string(offset + name), fps=fps, frames=frames,
                               duration=(frames - 1) / fps, flags=flags, loop=bool(flags & 1)))
    count, index = source.unpack("ii", 188)
    sequences = []
    for offset in source.table(count, index, 212):
        label, activity, flags = source.unpack("iii", offset + 4)
        blends, blend_index = source.unpack("ii", offset + 56)
        width, height = source.unpack("ii", offset + 68)
        if width < 1 or height < 1 or width * height != blends:
            raise ModelAuditError("Compiled sequence blend dimensions do not match its count")
        blend_offsets = source.table(blends, offset + blend_index, 2)
        indices = [source.unpack("h", blend_offset)[0] for blend_offset in blend_offsets]
        if any(index < 0 or index >= len(animations) for index in indices):
            raise ModelAuditError("Compiled sequence references an absent local animation")
        sequences.append(dict(name=source.string(offset + label),
                              activity=source.string(offset + activity) if activity else "",
                              flags=flags, loop=bool(flags & 1), delta=bool(flags & 4),
                              autoplay=bool(flags & 8), last_frame=source.unpack("f", offset + 132)[0],
                              animation_indices=indices, animations=[animations[index] for index in indices]))
    return dict(animations=animations, sequences=sequences)


def audit_source_clip_timing(model_path: Path, report: dict) -> dict:
    """Check each generated original clip against its authored FPS, frame count and loop flag."""
    errors = []
    result = dict(ok=False, errors=errors, clips=[], intentional_sequences=[])
    try:
        compiled = read_compiled_animation_timing(model_path)
        records = report.get("animations", {})
        expected = records.get("clips", records.get("source_clips", []))
        lookup = {sequence["name"]: sequence for sequence in compiled["sequences"]}
        if len(lookup) != len(compiled["sequences"]):
            errors.append("Compiled sequence names are not unique")
        for authored in expected:
            if "target_name" not in authored:
                continue
            name = authored["target_name"]
            sequence = lookup.get(name)
            if sequence is None:
                errors.append(f"Missing compiled source sequence {name}")
                continue
            if len(sequence["animations"]) != 1:
                errors.append(f"Source sequence {name} unexpectedly contains blended animations")
                continue
            animation = sequence["animations"][0]
            if animation["frames"] != authored["frames"]:
                errors.append(f"Source sequence {name} has {animation['frames']} frames versus authored {authored['frames']}")
            if not math.isclose(animation["fps"], authored["fps"], rel_tol=1e-6, abs_tol=1e-5):
                errors.append(f"Source sequence {name} FPS differs from authored {authored['fps']}")
            if sequence["loop"] != authored["loop"]:
                errors.append(f"Source sequence {name} loop flag differs from authored metadata")
            if sequence["delta"]:
                errors.append(f"Source sequence {name} is marked delta despite absolute exported poses")
            if "autoplay" in authored and sequence["autoplay"] != authored["autoplay"]:
                errors.append(f"Source sequence {name} autoplay flag differs from the selected playback policy")
            result["clips"].append(dict(source_name=authored["name"], target_name=name,
                frames=animation["frames"], fps=animation["fps"], loop=sequence["loop"],
                duration=animation["duration"], compiled_last_frame=sequence["last_frame"],
                authored_frames=authored["frames"], authored_fps=authored["fps"],
                sampling=authored.get("sampling", {})))
        native_bind = lookup.get("ragdoll")
        if native_bind is not None:
            result["intentional_sequences"].append(dict(name="ragdoll",
                purpose="Native physics bind sequence", frames=[a["frames"] for a in native_bind["animations"]]))
        if "source_ragdoll" in lookup:
            result["intentional_sequences"].append(dict(name="source_ragdoll",
                source_name="ragdoll", purpose="Original authored clip renamed to preserve the native physics bind",
                frames=[a["frames"] for a in lookup["source_ragdoll"]["animations"]]))
        result.update(compiled_sequence_count=len(compiled["sequences"]),
                      compiled_animation_count=len(compiled["animations"]),
                      checked_source_clips=len(result["clips"]))
    except (ModelAuditError, ValueError, KeyError, struct.error) as exc:
        errors.append(str(exc))
    result["ok"] = not errors
    return result


def _expected_program(operations: list[dict], controllers: dict[str, int]) -> list[tuple[int, int]]:
    """Translate authored postfix expressions independently for a binary equality check."""
    codes = {"CONST": 1, "FETCH1": 2, "ADD": 4, "SUB": 5, "MUL": 6,
             "DIV": 7, "MAX": 13, "MIN": 14}
    stack = []
    for operation in operations:
        name = operation["opcode"]
        if name in {"CONST", "FETCH1"}:
            operand = (int(operation["operand"]) & 0xffffffff if name == "CONST" else
                       controllers[operation["controller"]])
            stack.append([(codes[name], operand)])
        elif name in {"NEG", "ABS"}:
            value = stack.pop()
            negated = [(1, 0), *value, (5, 0)]
            stack.append(negated if name == "NEG" else [*value, *negated, (13, 0)])
        else:
            second, first = stack.pop(), stack.pop()
            stack.append([*first, *second, (codes[name], 0)])
    if len(stack) != 1:
        raise ValueError("Authored flex program has an invalid operand stack")
    return stack[0]


def audit_source_flex_programs(model_path: Path, rig: dict) -> dict:
    """Compare every controller, range, UI pointer, rule opcode and operand to its source rig."""
    errors = []
    result = dict(ok=False, errors=errors, controllers=[], rules=[])
    try:
        source = _Binary(model_path)
        source.require(0, 392)
        expected_controllers = rig.get("controllers", [])
        expected_rules = rig.get("rules", {})
        count, index = source.unpack("ii", 260)
        descriptors = [source.string(offset + source.unpack("i", offset)[0])
                       for offset in source.table(count, index, 4)]
        count, index = source.unpack("ii", 268)
        controller_offsets = list(source.table(count, index, 20))
        for offset in controller_offsets:
            kind, name, _, low, high = source.unpack("iiiff", offset)
            result["controllers"].append(dict(name=source.string(offset + name),
                type=source.string(offset + kind), minimum=low, maximum=high))
        if len(result["controllers"]) != len(expected_controllers):
            errors.append("Compiled controller count differs from the authored rig")
        for actual, expected in zip(result["controllers"], expected_controllers):
            name = expected["m_szName"]
            wanted_low, wanted_high = struct.unpack("<ff", struct.pack("<ff", expected.get("min", 0), expected.get("max", 1)))
            if (actual["name"] != name or actual["type"] != expected.get("m_szType", "default")
                    or actual["minimum"] != wanted_low or actual["maximum"] != wanted_high):
                errors.append(f"Controller order/type/range differs for {name}")
        controller_indices = {controller["m_szName"]: index for index, controller in enumerate(expected_controllers)}
        count, index = source.unpack("ii", 276)
        programs = {}
        for offset in source.table(count, index, 12):
            descriptor, operation_count, operation_offset = source.unpack("iii", offset)
            if not 0 <= descriptor < len(descriptors):
                raise ModelAuditError("Flex rule references an absent morph description")
            name = descriptors[descriptor]
            if name in programs:
                errors.append(f"Compiled model repeats flex rule {name}")
            programs[name] = [source.unpack("iI", op) for op in
                              source.table(operation_count, offset + operation_offset, 8)]
        if set(programs) != set(expected_rules):
            errors.append("Compiled flex-rule targets differ from the source graph")
        for name, operations in expected_rules.items():
            expected = _expected_program(operations, controller_indices)
            actual = programs.get(name)
            if actual != expected:
                errors.append(f"Compiled flex opcodes or operands differ for {name}")
            result["rules"].append(dict(name=name, operations=len(expected), exact=actual == expected))
        count, index = source.unpack("ii", 384)
        if count != len(expected_controllers):
            errors.append("Compiled flex UI count differs from the authored controller count")
        for ui, offset in enumerate(source.table(count, index, 20)):
            name, controller, _, _, remap, stereo = source.unpack("iiiiBB", offset)
            if (ui >= len(controller_offsets) or offset + controller != controller_offsets[ui]
                    or source.string(offset + name) != result["controllers"][ui]["name"] or remap or stereo):
                errors.append(f"Compiled UI record {ui} does not reference its authored controller")
        result.update(controller_count=len(result["controllers"]), rule_count=len(programs),
                      operation_count=sum(len(program) for program in programs.values()),
                      ui_count=count)
    except (ModelAuditError, ValueError, KeyError, IndexError, struct.error) as exc:
        errors.append(str(exc))
    result["ok"] = not errors
    return result
