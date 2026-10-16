"""Preserve exported morph-weight animation independently of skeletal channels."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from s2kit.pipelines.gltf_accessors import decode_accessor


@dataclass
class MorphAnimationResult:
    """Named output-weight curves and measured reductions suitable for addon playback."""

    clips: dict = field(default_factory=dict)
    report: dict = field(default_factory=lambda: {"clips": [], "unsupported": []})


def sample_morph_curve(curve: dict, times) -> np.ndarray:
    """Evaluate glTF scalar weights with the authored interpolation and tangent units."""
    keys = np.asarray(curve["times"], dtype=np.float64)
    values = np.asarray(curve["values"], dtype=np.float64)
    query = np.asarray(times, dtype=np.float64)
    mode = curve["interpolation"]
    if len(keys) == 1:
        value = values[0, 1] if mode == "CUBICSPLINE" else values[0]
        return np.full(query.shape, value, dtype=np.float64)
    lower = np.clip(np.searchsorted(keys, query, side="right") - 1, 0, len(keys) - 2)
    interval = keys[lower + 1] - keys[lower]
    amount = np.clip((query - keys[lower]) / interval, 0.0, 1.0)
    if mode == "STEP":
        return values[np.clip(np.searchsorted(keys, query, side="right") - 1, 0, len(keys) - 1)]
    if mode == "LINEAR":
        return values[lower] * (1.0 - amount) + values[lower + 1] * amount
    if mode != "CUBICSPLINE":
        raise ValueError(f"Unsupported morph interpolation: {mode}")
    square, cube = amount * amount, amount * amount * amount
    result = ((2 * cube - 3 * square + 1) * values[lower, 1]
              + (cube - 2 * square + amount) * interval * values[lower, 2]
              + (-2 * cube + 3 * square) * values[lower + 1, 1]
              + (cube - square) * interval * values[lower + 1, 0])
    return np.where(query <= keys[0], values[0, 1],
                    np.where(query >= keys[-1], values[-1, 1], result))


def _target_names(document: dict, node_index: int) -> list[str]:
    """Resolve each weight slot without depending on optional glTF targetNames extras."""
    mesh = document["meshes"][document["nodes"][node_index]["mesh"]]
    primitives = mesh.get("primitives", [])
    if not primitives:
        raise ValueError("An animated morph mesh has no primitives")
    names = mesh.get("extras", {}).get("targetNames", [])
    count = len(primitives[0].get("targets", []))
    if len(names) == count:
        return names
    names = []
    for target in primitives[0].get("targets", []):
        accessor = target.get("POSITION", target.get("NORMAL"))
        name = document["accessors"][accessor].get("name", "") if accessor is not None else ""
        if not name:
            raise ValueError("An animated morph target has no authored name")
        names.append(name)
    if len(set(names)) != len(names):
        raise ValueError("An animated morph mesh repeats a target name")
    return names


def _reduce_curve(curve: dict, tolerance: float) -> tuple[dict, float]:
    """Remove redundant linear keys with a bounded maximum weight error at source keys."""
    times = np.asarray(curve["times"], dtype=np.float64)
    values = np.asarray(curve["values"], dtype=np.float64)
    if curve["interpolation"] != "LINEAR" or len(times) <= 2:
        return curve, 0.0
    retained = {0, len(times) - 1}
    pending = [(0, len(times) - 1)]
    while pending:
        first, last = pending.pop()
        if last - first <= 1:
            continue
        amount = (times[first + 1:last] - times[first]) / (times[last] - times[first])
        expected = values[first] * (1.0 - amount) + values[last] * amount
        differences = np.abs(values[first + 1:last] - expected)
        largest = int(np.argmax(differences))
        if differences[largest] > tolerance:
            pivot = first + largest + 1
            retained.add(pivot)
            pending.extend([(first, pivot), (pivot, last)])
    indices = sorted(retained)
    reduced = dict(curve, times=times[indices].tolist(), values=values[indices].tolist())
    maximum = float(np.max(np.abs(sample_morph_curve(reduced, times) - values)))
    return reduced, maximum


def _interval_coefficients(curve: dict, edges: np.ndarray) -> np.ndarray:
    """Express each piece as a cubic in normalized time over the supplied intervals."""
    keys = np.asarray(curve["times"], dtype=np.float64)
    values = np.asarray(curve["values"], dtype=np.float64)
    starts, ends = edges[:-1], edges[1:]
    middle = starts + (ends - starts) * 0.5
    coefficients = np.zeros((len(starts), 4), dtype=np.float64)
    coefficients[:, 0] = sample_morph_curve(curve, middle)
    if len(keys) == 1 or curve["interpolation"] == "STEP":
        return coefficients
    if curve["interpolation"] == "LINEAR":
        coefficients[:, 0] = sample_morph_curve(curve, starts)
        coefficients[:, 1] = sample_morph_curve(curve, ends) - coefficients[:, 0]
        return coefficients
    lower = np.clip(np.searchsorted(keys, middle, side="right") - 1, 0, len(keys) - 2)
    duration = keys[lower + 1] - keys[lower]
    first, last = values[lower, 1], values[lower + 1, 1]
    outgoing, incoming = values[lower, 2] * duration, values[lower + 1, 0] * duration
    polynomial = np.column_stack([first, outgoing,
        -3 * first - 2 * outgoing + 3 * last - incoming,
        2 * first + outgoing - 2 * last + incoming])
    origin = (starts - keys[lower]) / duration
    scale = (ends - starts) / duration
    coefficients[:, 0] = (polynomial[:, 0] + polynomial[:, 1] * origin
                            + polynomial[:, 2] * origin ** 2 + polynomial[:, 3] * origin ** 3)
    coefficients[:, 1] = scale * (polynomial[:, 1] + 2 * polynomial[:, 2] * origin
                                   + 3 * polynomial[:, 3] * origin ** 2)
    coefficients[:, 2] = scale ** 2 * (polynomial[:, 2] + 3 * polynomial[:, 3] * origin)
    coefficients[:, 3] = scale ** 3 * polynomial[:, 3]
    outside = (middle < keys[0]) | (middle > keys[-1])
    coefficients[outside] = 0
    coefficients[outside, 0] = sample_morph_curve(curve, middle[outside])
    return coefficients


def _curves_agree(first: dict, second: dict, tolerance: float = 1e-6) -> bool:
    """Compare complete curves, including step left limits and cubic interior extrema."""
    if first["interpolation"] == second["interpolation"] and first["times"] == second["times"]:
        if first["values"] == second["values"]:
            return True
    edges = np.unique(np.concatenate([first["times"], second["times"]]))
    if not np.allclose(sample_morph_curve(first, edges), sample_morph_curve(second, edges),
                       atol=tolerance, rtol=0):
        return False
    if len(edges) < 2 or first["interpolation"] == second["interpolation"] == "LINEAR":
        return True
    differences = _interval_coefficients(first, edges) - _interval_coefficients(second, edges)
    if np.any(np.abs(differences[:, 0]) > tolerance) or np.any(np.abs(differences.sum(axis=1)) > tolerance):
        return False
    for coefficients in differences:
        for critical in np.roots([3 * coefficients[3], 2 * coefficients[2], coefficients[1]]):
            if np.isreal(critical) and 0 < float(critical.real) < 1:
                if abs(np.polynomial.polynomial.polyval(float(critical.real), coefficients)) > tolerance:
                    return False
    return True


def export_morph_animations(document: dict, buffer: bytes, *, names: dict[str, str] | None = None,
                            durations: dict[str, float] | None = None,
                            tolerance: float = 1e-6, cancel=None) -> MorphAnimationResult:
    """Collect sequence-synchronised curves; conflicting per-mesh weights remain explicit."""
    from s2kit.pipelines.porting import check_cancelled

    result = MorphAnimationResult()
    for animation in document.get("animations", []):
        check_cancelled(cancel)
        original_name = animation.get("name", "")
        if names is not None and original_name not in names:
            continue
        curves = {}
        all_targets = set()
        source_keys = 0
        conflicts = []
        for channel in animation.get("channels", []):
            target = channel["target"]
            if target["path"] != "weights":
                continue
            targets = _target_names(document, target["node"])
            sampler = animation["samplers"][channel["sampler"]]
            times = decode_accessor(document, buffer, sampler["input"]).astype(np.float64).reshape(-1)
            if not len(times) or not np.isfinite(times).all() or np.any(np.diff(times) <= 0):
                raise ValueError(f"{original_name}: morph times must be finite and increasing")
            mode = sampler.get("interpolation", "LINEAR")
            if mode not in ("LINEAR", "STEP", "CUBICSPLINE"):
                raise ValueError(f"{original_name}: unsupported morph interpolation {mode}")
            multiplier = 3 if mode == "CUBICSPLINE" else 1
            values = decode_accessor(document, buffer, sampler["output"]).astype(np.float64)
            if not targets or values.size != len(times) * len(targets) * multiplier:
                raise ValueError(f"{original_name}: morph weights do not match target and key counts")
            values = values.reshape((len(times), 3, len(targets)) if multiplier == 3 else (len(times), len(targets)))
            if not np.isfinite(values).all():
                raise ValueError(f"{original_name}: morph weights contain non-finite values")
            for index, name in enumerate(targets):
                all_targets.add(name)
                curve = dict(times=times.tolist(), values=values[..., index].tolist(), interpolation=mode)
                source_keys += len(times)
                if name in curves:
                    if not _curves_agree(curve, curves[name]):
                        conflicts.append(name)
                else:
                    curves[name] = curve
        if not all_targets:
            continue
        if conflicts:
            result.report["unsupported"].append(dict(name=original_name,
                reason="Per-mesh weights disagree for shared Source 1 morph outputs", targets=sorted(set(conflicts))))
            continue
        reduced, maximum = {}, 0.0
        for name, curve in curves.items():
            curve, error = _reduce_curve(curve, tolerance)
            maximum = max(maximum, error)
            if np.any(np.abs(curve["values"]) > 0):
                reduced[name] = curve
        name = names.get(original_name, original_name) if names is not None else original_name
        last_key = max(curve["times"][-1] for curve in curves.values())
        duration = float(durations.get(original_name, last_key)) if durations is not None else last_key
        if not np.isfinite(duration) or duration < 0:
            raise ValueError(f"{original_name}: authored morph duration must be finite and nonnegative")
        allowance = max(1e-5, abs(duration) * 4.0 * np.finfo(np.float32).eps)
        if last_key > duration + allowance:
            raise ValueError(f"{original_name}: morph keys extend beyond the authored sequence duration")
        result.clips[name] = dict(duration=duration, targets=sorted(all_targets), curves=reduced)
        result.report["clips"].append(dict(name=original_name, target_name=name, status="adapted",
            method="sequence_cycle_flex_output_callback", duration=duration, targets=len(all_targets),
            exported_last_key_time=last_key, constant_tail_restored=max(0.0, duration - last_key),
            source_keys=source_keys, retained_keys=sum(len(curve["times"]) for curve in reduced.values()),
            maximum_weight_error=maximum))
    return result
