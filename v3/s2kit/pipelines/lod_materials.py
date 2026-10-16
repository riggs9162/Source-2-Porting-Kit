"""Preserve lower-LOD materials through Source 1's fixed LOD0 mesh slots."""

from __future__ import annotations

import copy
import hashlib
from collections import Counter
from collections.abc import Callable
from pathlib import PurePosixPath

from s2kit.pipelines.model_fidelity import resource_token


def _skin_sources(material: str, fidelity: dict) -> list[str]:
    """Apply the authored default-to-selected material substitution independently per skin."""
    families = fidelity.get("skins", [])
    if not families:
        return [material]
    base = [resource_token(value) for value in families[0]["m_materials"]]
    result = []
    for family in families:
        values = [resource_token(value) for value in family["m_materials"]]
        if len(base) != len(values):
            raise ValueError("Source material families have incompatible slot counts")
        candidates = {target for source, target in zip(base, values) if source == material}
        if len(candidates) > 1:
            raise ValueError(f"Authored skin maps material {material!r} to conflicting resources")
        result.append(next(iter(candidates), material))
    return result


def plan_lod_materials(specifications: list[dict], meshes: dict, lookup: dict,
                       fidelity: dict, target_token: Callable[[str], str]) -> dict:
    """Allocate choice-local carrier aliases without changing any triangle or vertex attribute."""
    levels = max(1, len(fidelity.get("lod_distances", [])))
    streams = {}
    aliases = {}
    skin_slots = []
    replacements = {level: {} for level in range(1, levels)}
    records = []
    for specification in specifications:
        name = specification["name"]
        selected = {level: [primitive for index in specification["meshes"]
                           if meshes[index]["lod_mask"] & (1 << level)
                           for primitive in lookup.get(index, [])] for level in range(levels)}
        baseline = {primitive["source_material"] for primitive in selected[0]}
        assignments = {}
        carriers = set()
        for level in range(1, levels):
            current = {primitive["source_material"] for primitive in selected[level]}
            introduced = sorted(current - baseline)
            available = sorted(baseline - current)
            if len(introduced) > len(available):
                raise ValueError(f"Authored LOD {level} of {name!r} requires {len(introduced)} new material slots "
                                 f"but only {len(available)} unused LOD0 slots exist; refusing to discard or guess geometry")
            assignments[level] = dict(zip(introduced, available))
            carriers.update(assignments[level].values())
        carrier_aliases = {}
        for carrier in sorted(carriers):
            identity = "\0".join([str(fidelity.get("resource", "")), name, carrier])
            digest = hashlib.sha256(identity.encode()).hexdigest()[:24]
            base_alias = "__s2kit_lod_slots/" + digest
            sources = _skin_sources(carrier, fidelity)
            targets = [base_alias if skin == 0 else base_alias + f"_skin{skin}"
                       for skin in range(len(sources))]
            carrier_aliases[carrier] = targets
            aliases.update(zip(targets, sources))
            skin_slots.append(dict(compiler_material=base_alias, skin_materials=targets, source_materials=sources))
        for level in range(levels):
            values = []
            counts = Counter()
            for primitive in selected[level]:
                source_material = primitive["source_material"]
                carrier = assignments.get(level, {}).get(source_material, source_material)
                compiler_material = carrier_aliases[carrier][0] if carrier in carrier_aliases else primitive["material"]
                authored = _skin_sources(source_material, fidelity)
                runtime = [target_token(value) for value in authored]
                if level == 0 and carrier in carrier_aliases:
                    runtime = carrier_aliases[carrier]
                elif carrier in carrier_aliases:
                    for alias, target in zip(carrier_aliases[carrier], runtime):
                        previous = replacements[level].setdefault(alias, target)
                        if previous != target:
                            raise ValueError(f"Conflicting per-LOD skin material replacement for {alias}")
                value = copy.copy(primitive)
                value["material"] = compiler_material
                values.append(value)
                counts[(source_material, compiler_material, tuple(runtime), tuple(authored))] += len(primitive["indices"]) // 3
            streams[(name, level)] = values
            for (source_material, compiler_material, runtime, authored), count in counts.items():
                records.append(dict(part=specification["part"], choice=specification["compiled_choice"],
                    lod=level, specification=name, source_material=source_material,
                    compiler_material=compiler_material, runtime_material=runtime[0],
                    runtime_materials=list(runtime), authored_materials=list(authored), triangle_count=count))
    return dict(streams=streams, aliases=aliases, skin_slots=skin_slots,
                replacements=replacements, records=records)


def dmx_skin_material_qc(specifications: list[dict], material_plan: dict,
                          skin_count: int) -> tuple[list[str], list[dict]]:
    """Bind QC skins to actual DMX/SMD texture identities and restore their full paths.

    The Source compiler gives DMX materials a relative-path flag. QC's lookup
    uses the opposite flag and only matches their basename. Temporarily unique,
    bare names make that lookup exact without flattening final resource paths.
    A full path imported through both DMX and SMD has two texture identities;
    both receive their own temporary reference and the same authored skin rule.
    """
    formats = {}
    for specification in specifications:
        source = specification.get("source")
        if not source:
            continue
        kind = PurePosixPath(source).suffix.casefold()
        for primitive in material_plan["streams"][(specification["name"], 0)]:
            formats.setdefault(primitive["material"], set()).add(kind)
    if skin_count < 2 or not any(".dmx" in kinds for kinds in formats.values()):
        return [], []
    families = {}
    for record in material_plan["records"]:
        if record["lod"]:
            continue
        material = record["compiler_material"]
        values = record["runtime_materials"]
        if len(values) != skin_count:
            raise ValueError("Compiler material has an incompatible skin family count")
        previous = families.setdefault(material, values)
        if previous != values:
            raise ValueError(f"Compiler material {material!r} has conflicting authored skin rules")
    references = []
    by_material = {}
    reserved = {PurePosixPath(material).name.casefold() for material in formats}
    for material, kinds in sorted(formats.items()):
        identities = len(kinds) if "/" in material else 1
        for identity in range(identities):
            digest = hashlib.sha256((material + "\0" + str(identity)).encode()).hexdigest()[:24]
            reference = "__s2_skin_ref_" + digest
            if reference.casefold() in reserved:
                raise ValueError("Generated compiler skin reference collides with a source material")
            reserved.add(reference.casefold())
            record = dict(compiler_reference=reference, restored_material=material,
                          source_formats=sorted(kinds), texture_identity=identity)
            references.append(record)
            by_material.setdefault(material, []).append(reference)
    lines = [f'$renamematerial "{record["restored_material"]}" "{record["compiler_reference"]}"'
             for record in references]
    lines.extend(['$texturegroup "skinfamilies"', "{"])
    for skin in range(skin_count):
        row = []
        for record in references:
            material = record["restored_material"]
            target = families[material][skin]
            target_reference = (record["compiler_reference"] if target == material else
                                by_material[target][0] if target in by_material else target)
            row.append('"' + target_reference + '"')
        lines.append("    { " + " ".join(row) + " }")
    lines.append("}")
    lines.extend(f'$renamematerial "{record["compiler_reference"]}" "{record["restored_material"]}"'
                 for record in references)
    return lines, references
