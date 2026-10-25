"""Read Source 2 Viewer DMX meshes without dropping authored LOD skinning."""

from pathlib import Path
from collections import Counter, defaultdict

import numpy as np

from s2kit.vendor import valve_datamodel


def smd_vertex_line(values, links) -> str:
    """Separate every vertex field for StudioMDL CE's tab-delimited parser."""
    total = sum(weight for _, weight in links)
    fields = [str(links[0][0]),
              " ".join(f"{value:.9f}" for value in values[:3]),
              " ".join(f"{value:.9f}" for value in values[3:6]),
              " ".join(f"{value:.9f}" for value in values[6:8])]
    if len(links) > 1:
        fields.append(str(len(links)) + "".join(f" {joint} {weight / total:.9f}" for joint, weight in links))
    return "\t".join(fields) + "\n"


def _reference_bone_count(header: str) -> int:
    """Count the bones a reference SMD's nodes block declares."""
    nodes = header.split("nodes\n", 1)[1].split("end\n", 1)[0]
    return sum(1 for line in nodes.splitlines() if line.strip())


def write_dmx_reference(dmx_path: Path, smd_path: Path, reference: Path,
                        bone_names: list[str]) -> int:
    """Reuse the verified native skeleton and export the DMX mesh in Source inches.

    An unweighted DMX - a static prop's LOD - binds every vertex to the
    reference's only bone, and is refused against a multi-bone skeleton.
    """
    data = valve_datamodel.load(str(dmx_path))
    models = [element for element in data.elements if element.type == "DmeModel"]
    meshes = [element for element in data.elements if element.type == "DmeMesh"]
    if len(models) != 1 or len(meshes) != 1:
        raise ValueError("LOD DMX must contain exactly one model and one mesh")
    model = models[0]
    joints = model["jointList"]
    mesh = meshes[0]
    state = mesh["currentState"]
    count = int(state["jointCount"])
    positions = np.asarray(state["position$0"])
    normals = np.asarray(state["normal$0"])
    uv = np.asarray(state["texcoord$0"])
    header = reference.read_text(encoding="utf-8").split("triangles\n", 1)[0]
    rigid = count == 0
    if rigid and _reference_bone_count(header) != 1:
        raise ValueError("An unweighted LOD DMX needs a single-bone reference skeleton")
    if rigid:
        weights = np.ones((len(positions), 1))
        indices = np.zeros((len(positions), 1), dtype=np.int64)
        mapped = {0: 0}
    else:
        weights = np.asarray(state["blendweights$0"]).reshape(-1, count)
        indices = np.asarray(state["blendindices$0"]).reshape(-1, count)
        bone_lookup = {name: index for index, name in enumerate(bone_names)}
        mapped = {index: bone_lookup[element.name] for index, element in enumerate(joints)
                  if element.name in bone_lookup}
    triangles = 0
    with smd_path.open("w", encoding="utf-8", newline="\n") as output:
        output.write(header + "triangles\n")
        for face_set in mesh["faceSets"]:
            material = Path(face_set["material"]["mtlName"]).stem
            face = []
            for vertex in face_set["faces"]:
                if vertex >= 0:
                    face.append(vertex)
                    continue
                if len(face) != 3:
                    raise ValueError("Source 2 mesh contains a non-triangle polygon")
                output.write(material + "\n")
                for vertex in face:
                    pindex = state["position$0Indices"][vertex]
                    position = positions[pindex]
                    normal = normals[state["normal$0Indices"][vertex]]
                    texcoord = uv[state["texcoord$0Indices"][vertex]]
                    links = [(mapped[int(joint)], float(weight))
                             for joint, weight in zip(indices[pindex], weights[pindex])
                             if weight > 0]
                    total = sum(weight for _, weight in links)
                    if not links or total <= 0:
                        raise ValueError("Unweighted DMX vertex")
                    values = [*position, *normal, texcoord[0], 1.0 - texcoord[1]]
                    output.write(smd_vertex_line(values, links))
                triangles += 1
                face = []
            if face:
                raise ValueError("Unterminated DMX face")
        output.write("end\n")
    return triangles


def _smd_groups(path: Path):
    """Group intact triangle records by material and primary bone."""
    header, body = path.read_text(encoding="utf-8").split("triangles\n", 1)
    records = body.splitlines()
    groups = defaultdict(list)
    for index in range(0, len(records) - 1, 4):
        triangle = records[index:index + 4]
        if len(triangle) != 4:
            raise ValueError(f"Incomplete SMD triangle: {path}")
        primary = Counter(int(vertex.split()[0]) for vertex in triangle[1:])
        bone = min(primary, key=lambda key: (-primary[key], key))
        groups[(triangle[0], bone)].append(triangle)
    return header, groups


def partition_lod_meshes(reference: Path, lods: list[tuple[float, Path]],
                         max_vertices: int = 30000) -> tuple[list[str], list[Path]]:
    """Keep matching bodyparts below CE's split limit so all LOD replacements apply."""
    header, base = _smd_groups(reference)
    partitions = []
    active, active_vertices = [], set()
    for key, triangles in base.items():
        vertices = {vertex for triangle in triangles for vertex in triangle[1:]}
        if len(vertices) > max_vertices:
            raise ValueError(f"Material/bone group {key} exceeds the safe LOD bodypart limit")
        if active and len(active_vertices | vertices) > max_vertices:
            partitions.append(active)
            active, active_vertices = [], set()
        active.append(key)
        active_vertices.update(vertices)
    if active:
        partitions.append(active)
    if len(partitions) <= 1:
        return [], []
    outputs, qc = [], []
    sources = [(0.0, reference, header, base)]
    for distance, source in lods:
        lod_header, groups = _smd_groups(source)
        unknown = set(groups) - set(base)
        if unknown:
            raise ValueError(f"LOD introduced material/bone groups absent from the reference: {unknown}")
        sources.append((distance, source, lod_header, groups))
    for lod_index, (distance, source, current_header, groups) in enumerate(sources):
        if lod_index:
            qc.extend([f"$lod {distance:g}", "{"])
        for index, keys in enumerate(partitions):
            triangles = [triangle for key in keys for triangle in groups.get(key, [])]
            base_name = f"{reference.stem}_part{index}.smd"
            if not triangles:
                if lod_index:
                    qc.append(f'    removemodel "{base_name}"')
                continue
            destination = source.with_name(f"{source.stem}_part{index}.smd")
            destination.write_text(current_header + "triangles\n" +
                                   "\n".join(line for triangle in triangles for line in triangle) +
                                   "\nend\n", encoding="utf-8")
            outputs.append(destination)
            if lod_index:
                qc.append(f'    replacemodel "{base_name}" "{destination.name}"')
            else:
                qc.append(f'$body "body{index}" "{base_name}"')
        if lod_index:
            qc.append("}")
    return qc, outputs


def write_lod_references(reference: Path, lod_sources: list[tuple[float, Path]],
                         bone_names: list[str], log=None) -> tuple[list[str], list[Path], bool]:
    """Write authored LOD meshes beside a reference SMD and return their QC.

    Returns the QC lines, every file written and whether large meshes were
    partitioned; partitioned lines declare their own bodyparts and replace
    every `$body` line of the model.
    """
    log = log or (lambda _message: None)
    reference = Path(reference)
    lod_smds, files = [], []
    for index, (distance, source) in enumerate(lod_sources, start=1):
        destination = reference.with_name(f"{reference.stem}_lod{index}.smd")
        triangles = write_dmx_reference(source, destination, reference, bone_names)
        lod_smds.append((distance, destination))
        files.append(destination)
        log(f"LOD {index}: {triangles} triangles from {source.name}")
    if not lod_smds:
        return [], files, False
    partition_lines, partition_files = partition_lod_meshes(reference, lod_smds)
    if partition_lines:
        return partition_lines, [*files, *partition_files], True
    lines = []
    for distance, destination in lod_smds:
        lines.extend([f"$lod {distance:g}", "{",
                      f'    replacemodel "{reference.name}" "{destination.name}"', "}"])
    return lines, files, False
