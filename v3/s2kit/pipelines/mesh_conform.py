"""Move the mesh onto the retargeted skeleton.

The whole of it is one similarity transform - a turn about Z, a uniform scale
and an offset - applied to every vertex alike. That is a deliberately small
amount of machinery for what sounds like a hard problem, so the measurement
that settled it is recorded here, because the obvious approach is wrong and
costs days.

**The mesh must not be conformed per bone.** The bind becomes the donor's, so
the intuition is that flesh has to be carried from each Source 2 bone's frame
into the corresponding donor bone's frame. Every version of that idea - full
frame delta, swing-only, swing plus a twist recovered from the joint bend
plane, linear or dual-quaternion blending - tears the model, because the two
rigs genuinely disagree about where some joints are: on an HLA grunt against
m_anm the clavicle sits 28 degrees apart and the donor's Spine4 is 8.94 units
where Source 2's is 5.13. Transporting flesh across that difference stretches
it, and no amount of care in the delta removes a disagreement that is real.

The proof is a hand-made 1:1 port of the same character, measured against the
Source 2 mesh it came from:

* per-bone, its flesh fits a **rigid** transform with residual 0.000 units;
* 28 of its 30 weight-bearing bones use a transform within **0.62 degrees** of
  one single global rotation, at per-bone scale 1.0000;
* 90 per cent of its edges are within **0.1 per cent** of their original
  length - the mesh is simply not deformed;
* the only vertices more than an inch off that one global transform are 592 on
  the pelvis, a local hand edit.

So the flesh stays exactly as Source 2 authored it and the donor's bones are
allowed to sit slightly off inside it. That is fine, and it is what ships: a
bone need not be at the anatomical joint for the limb to swing correctly, it
only needs to be rigidly related to the flesh it carries. Trying to close that
last gap by moving vertices is what produced arms rotated 180 degrees about
their own axis - the visible "twisting" this replaced.

Auxiliary bones ride the same transform, in
:func:`s2kit.pipelines.skeleton_retarget._new_world_matrices`, so bone and
flesh never separate.
"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional, Sequence

import numpy as np

from s2kit.pipelines.porting import LogFn, check_cancelled
from s2kit.pipelines.skeleton_retarget import RetargetResult, RigAlignment

__all__ = ["align_mesh", "measure_strain"]


def align_mesh(alignment: RigAlignment, arrays: Sequence[dict], *,
               on_log: Optional[LogFn] = None,
               cancel: Optional[threading.Event] = None) -> int:
    """Apply the source -> donor alignment to every vertex and normal.

    Normals take the rotation only: a uniform scale leaves a direction alone,
    and re-normalising a scaled normal would just undo it.
    """
    log = on_log or (lambda _m, _l="INFO": None)
    rotation = alignment.rotation
    moved = 0
    for prim in arrays:
        check_cancelled(cancel)
        prim["vertices"] = alignment.apply(prim["vertices"])
        normals = prim.get("normals")
        if normals is not None:
            prim["normals"] = np.asarray(normals, dtype=np.float64) @ rotation.T
        moved += len(prim["vertices"])
    log(f"mesh aligned onto the donor: {moved} vertices turned "
        f"{alignment.yaw:+.1f} deg and scaled x{alignment.scale:.4f}", "INFO")
    return moved


def measure_strain(before: Sequence[np.ndarray], arrays: Sequence[dict],
                   skin=None) -> dict:
    """How far the mesh moved from rigid, per bone. Needs no reference model.

    Under :func:`align_mesh` this should read **zero** everywhere but the
    uniform scale, which the metric divides out. It is kept as the regression
    guard for exactly that: any future stage that touches vertices per-bone
    will show up here as a strain tail, which is how the per-bone conform this
    module used to do was caught in the first place.

    Grading the port against itself matters because most models have no
    known-good version to diff against - and because a reference only ever
    tells you *that* you differ, never *where*.

    Returns the overall distribution plus the worst bones by median strain.
    """
    per_bone: Dict[int, List[float]] = {}
    ratios: List[np.ndarray] = []
    owners: List[np.ndarray] = []
    for source, prim in zip(before, arrays):
        target = np.asarray(prim["vertices"], dtype=np.float64)
        index = np.asarray(prim["indices"]).reshape(-1, 3)
        joints = np.asarray(prim["vj"])
        weights = np.asarray(prim["vw"])
        dominant = joints[np.arange(len(joints)), np.argmax(weights, axis=1)]
        for a, b in ((index[:, 0], index[:, 1]), (index[:, 1], index[:, 2]),
                     (index[:, 2], index[:, 0])):
            was = np.linalg.norm(source[b] - source[a], axis=1)
            now = np.linalg.norm(target[b] - target[a], axis=1)
            real = was > 1e-4
            ratios.append(now[real] / was[real])
            owners.append(dominant[a][real])

    if not ratios:
        return {"edges": 0}
    # Divide out the alignment's uniform scale, which is not deformation: what
    # is being graded is whether the mesh changed SHAPE.
    every = np.concatenate(ratios)
    uniform = float(np.median(every))
    allof = np.abs(every / uniform - 1.0) if uniform > 1e-9 else np.abs(every - 1.0)
    for bone, value in zip(np.concatenate(owners), allof):
        per_bone.setdefault(int(bone), []).append(float(value))
    worst = sorted(((float(np.median(v)), len(v), k)
                    for k, v in per_bone.items() if len(v) > 200), reverse=True)
    name = (lambda k: skin.joints[k].name) if skin is not None else (lambda k: str(k))
    return {
        "edges": int(allof.size),
        "median": float(np.median(allof)),
        "p99": float(np.percentile(allof, 99)),
        "over_10": float((allof > 0.10).mean()),
        "over_50": float((allof > 0.50).mean()),
        "worst_bones": [(name(k), med, n) for med, n, k in worst[:6]],
    }
