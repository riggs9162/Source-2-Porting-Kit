"""Minimal Source 1 .mdl reader: bone table and local pose parameters.

Reads studiomdl v44-v49 headers well enough to serve as the *donor* side of
the ValveBiped conform pipeline (see tools/valvebiped_conform.py): bone
names, parents, bind locals (pos + quat) and the $poseparameter list of an
animation-library model such as GMod's z_anm.mdl / m_anm.mdl.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

# studiohdr_t offsets (v48/v49)
_OFF_VERSION = 0x04
_OFF_NUMBONES = 0x9C
_OFF_BONEINDEX = 0xA0
_OFF_NUMPOSEPARAM = 0x12C
_OFF_POSEPARAMINDEX = 0x130
_BONE_STRIDE = 216           # mstudiobone_t


@dataclass
class MdlBone:
    name: str
    parent: int               # index into the bone list, -1 for root
    pos: tuple                # (3,) parent-local bind position
    quat: tuple               # (4,) [x,y,z,w] parent-local bind rotation


@dataclass
class MdlPoseParam:
    name: str
    flags: int
    start: float
    end: float
    loop: float


def _cstr(d: bytes, off: int) -> str:
    return d[off:d.index(b"\0", off)].decode("latin-1")


def read_mdl_bones(path: Path) -> List[MdlBone]:
    """Read the bone table of a compiled .mdl (v44+ layouts share it)."""
    d = Path(path).read_bytes()
    numbones, boneindex = struct.unpack_from("<ii", d, _OFF_NUMBONES)
    bones: List[MdlBone] = []
    for i in range(numbones):
        off = boneindex + i * _BONE_STRIDE
        sznameindex, parent = struct.unpack_from("<ii", d, off)
        bones.append(MdlBone(
            name=_cstr(d, off + sznameindex),
            parent=parent,
            pos=struct.unpack_from("<3f", d, off + 32),
            quat=struct.unpack_from("<4f", d, off + 44),
        ))
    return bones


def read_mdl_poseparams(path: Path) -> List[MdlPoseParam]:
    """Read the local $poseparameter table of a compiled .mdl."""
    d = Path(path).read_bytes()
    n, idx = struct.unpack_from("<ii", d, _OFF_NUMPOSEPARAM)
    out: List[MdlPoseParam] = []
    for i in range(n):
        off = idx + i * 20
        sznameindex, flags = struct.unpack_from("<ii", d, off)
        start, end, loop = struct.unpack_from("<3f", d, off + 8)
        out.append(MdlPoseParam(_cstr(d, off + sznameindex), flags, start, end, loop))
    return out


def mdl_version(path: Path) -> Optional[int]:
    """Return the studiomdl format version, or None if not a .mdl."""
    d = Path(path).read_bytes()
    if d[:4] != b"IDST":
        return None
    return struct.unpack_from("<i", d, _OFF_VERSION)[0]
