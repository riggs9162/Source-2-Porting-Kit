"""Compile configured bone-local display surfaces into native model geometry."""
from __future__ import annotations

import math
from pathlib import Path
import re
import shlex
from typing import Any

import numpy as np


def overlay_skin_material(surface: dict[str, Any], index: int) -> str:
    """Use the configured skin material, repeating its last entry for extra skins."""
    skins = surface.get("skins", [surface["material"]])
    if not skins or not isinstance(skins, list):
        raise ValueError("Overlay skins must be a nonempty material list")
    material = str(skins[min(index, len(skins) - 1)])
    if not re.fullmatch(r"[a-zA-Z0-9_./]+", material):
        raise ValueError("Invalid overlay skin material path")
    return material


def write_bone_overlays(reference: Path, surfaces: list[dict[str, Any]], output: Path) -> list[Path]:
    """Write rigid quad SMDs sharing the reference skeleton and its bind transforms."""
    if not surfaces:
        return []
    text = reference.read_text(encoding="utf-8")
    header = text.split("triangles\n", 1)[0]
    nodes = {}
    for line in header.split("nodes\n", 1)[1].split("\nend", 1)[0].splitlines():
        index, name, parent = shlex.split(line)
        nodes[int(index)] = (name, int(parent))
    transforms = {}
    skeleton = header.split("skeleton\n", 1)[1].split("\nend", 1)[0]
    for line in skeleton.splitlines():
        fields = line.split()
        if len(fields) != 7:
            continue
        index = int(fields[0])
        x, y, z, rx, ry, rz = map(float, fields[1:])
        sx, sy, sz = math.sin(rx), math.sin(ry), math.sin(rz)
        cx, cy, cz = math.cos(rx), math.cos(ry), math.cos(rz)
        rotation = np.array([[cz*cy, cz*sy*sx-sz*cx, cz*sy*cx+sz*sx],
                             [sz*cy, sz*sy*sx+cz*cx, sz*sy*cx-cz*sx],
                             [-sy, cy*sx, cy*cx]])
        local = np.eye(4)
        local[:3, :3] = rotation
        local[:3, 3] = [x, y, z]
        transforms[index] = local
    world = {}

    def resolve(index):
        """Resolve each bone once, including independently rooted chains."""
        if index not in world:
            parent = nodes[index][1]
            world[index] = transforms[index] if parent < 0 else resolve(parent) @ transforms[index]
        return world[index]

    names = {name: index for index, (name, _) in nodes.items()}
    paths = []
    seen = set()
    for surface in surfaces:
        name = str(surface["name"])
        material = str(surface["material"])
        if not re.fullmatch(r"[a-zA-Z0-9_]+", name) or name in seen:
            raise ValueError("Overlay names must be unique safe identifiers")
        if not re.fullmatch(r"[a-zA-Z0-9_./]+", material):
            raise ValueError("Invalid overlay material path")
        seen.add(name)
        bone = names[str(surface["bone"])]
        vertices = np.asarray(surface["vertices"], dtype=float)
        uv = np.asarray(surface["uv"], dtype=float)
        if vertices.shape != (4, 3) or uv.shape != (4, 2) or not np.isfinite(vertices).all() or not np.isfinite(uv).all():
            raise ValueError("Overlay quads require four finite positions and UV pairs")
        matrix = resolve(bone)
        positions = (matrix @ np.column_stack([vertices, np.ones(4)]).T).T[:, :3]
        normal = np.cross(positions[1] - positions[0], positions[2] - positions[0])
        length = np.linalg.norm(normal)
        if length < 1e-8:
            raise ValueError("Overlay quad has no surface area")
        normal /= length
        lines = [header.rstrip(), "triangles"]
        for triangle in ((0, 1, 2), (0, 2, 3)):
            lines.append(material)
            for index in triangle:
                values = [*positions[index], *normal, *uv[index]]
                lines.append(str(bone) + " " + " ".join(f"{v:.9f}" for v in values) + f" 1 {bone} 1")
        lines.append("end")
        path = output / (name + ".smd")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        paths.append(path)
    return paths
