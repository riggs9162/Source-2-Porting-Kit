"""Sample source clips on their authored frame grid, retaining optimized constant tails."""

from __future__ import annotations

import math
from dataclasses import replace

from s2kit.pipelines.gltf_animation import GltfClip, GltfSkin, sample_clip
from s2kit.pipelines.model_metadata import AnimationMetadata


def sample_authored_clip(clip: GltfClip, skin: GltfSkin,
                          authored: AnimationMetadata) -> tuple[list[dict], int, dict]:
    """Use source frame count and FPS when glTF omits redundant terminal keys.

    glTF defines endpoint holding for every sampler. ValveResourceFormat can
    therefore export a multi-frame constant clip with one key at time zero.
    The compiled source metadata retains its full duration; sampling on that
    frame grid restores the omitted tail without inventing new motion.
    """
    fps = float(authored.fps)
    frames = int(authored.frames)
    if not math.isfinite(fps) or fps <= 0 or frames < 1:
        raise ValueError(f"{clip.name}: authored animation requires positive FPS and frame count")
    duration = (frames - 1) / fps
    exported_end = float(clip.duration)
    if not math.isfinite(exported_end) or exported_end < 0:
        raise ValueError(f"{clip.name}: exported animation duration is invalid")
    tolerance = max(1e-5, duration * 4.0 * 1.1920928955078125e-7)
    if exported_end > duration + tolerance:
        raise ValueError(f"{clip.name}: exported keys end at {exported_end:g}s beyond "
                         f"the authored {frames}-frame duration {duration:g}s")
    samples, count = sample_clip(replace(clip, duration=duration), skin, fps)
    if count != frames:
        raise ValueError(f"{clip.name}: authored frame sampling produced {count} frames instead of {frames}")
    tail = max(0.0, duration - exported_end)
    report = dict(authored_frames=frames, authored_duration=duration,
                  exported_last_key_time=exported_end,
                  constant_tail_restored=tail if tail > tolerance else 0.0,
                  method="authored_frame_grid_with_sampler_endpoint_hold")
    return samples, count, report
