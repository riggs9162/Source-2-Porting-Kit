"""Normalize authored Source 2 blend layers into independent material documents."""

from __future__ import annotations

import re

from s2kit.pipelines.soundscape import KVBlock, parse_kv
from s2kit.pipelines.vmat import KEY_ALIASES


def layer_documents(text: str) -> list[tuple[str, str]]:
    """Retain shared parameters and exact layer overrides while excluding blend masks.

    Only explicit albedo-bearing layers qualify. Missing layer maps stay absent
    rather than inheriting another layer through filename inference.
    """
    document = parse_kv(text)
    if len(document.pairs) != 1 or not isinstance(document.pairs[0][1], KVBlock):
        return []
    block = document.pairs[0][1]
    fields = block.scalars()
    layers = sorted({match[1] for key in fields
                     if (match := re.fullmatch(r"Texture(?:Color|Albedo|Diffuse)([A-Z])", key))})
    if not layers:
        return []
    if "A" not in layers and any(key in fields for key in KEY_ALIASES["color"]):
        layers.insert(0, "A")
    aliases = {alias for names in KEY_ALIASES.values() for alias in names
               if not alias.endswith("A")}
    layered = {key for key in fields
               if re.fullmatch(r"(?:Texture\w+|g_fl\w+|g_v\w+)[A-Z]", key)
               and key[:-1] in aliases | {"g_flRoughness", "g_flMetalness", "g_vColorTint"}}
    shared = {key: value for key, value in fields.items() if key not in layered
              and key != "TextureMask"}
    outputs = []
    for layer in layers:
        values = dict(shared)
        for key in layered:
            if key.endswith(layer):
                values[key[:-1]] = fields[key]
        for role, names in KEY_ALIASES.items():
            explicit = next((name + layer for name in names
                             if not name.endswith("A") and name + layer in fields), None)
            if explicit:
                for name in names:
                    values.pop(name, None)
                values[names[0]] = fields[explicit]
        emissive_level = fields.get(f"g_flMaterial{ord(layer) - ord('A')}EmissiveLevel")
        if emissive_level is not None:
            values["g_flSelfIllumBrightness"] = emissive_level
        lines = [f'"{key}" "{value}"' for key, value in values.items()]
        attributes = block.block("SystemAttributes")
        if attributes is not None:
            lines += ['"SystemAttributes"', "{", *[
                f'"{key}" "{value}"' for key, value in attributes.scalars().items()], "}"]
        outputs.append((layer, '"Layer0"\n{\n' + "\n".join(lines) + "\n}\n"))
    return outputs
