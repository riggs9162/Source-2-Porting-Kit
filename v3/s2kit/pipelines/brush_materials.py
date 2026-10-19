"""Translate Source 2 brush surface names into stock Source 1 properties."""

from __future__ import annotations


def source1_surfaceprop(value: str) -> str:
    """Map namespaced Alyx properties to their closest standard Source 1 surface."""
    name = value.strip().casefold()
    name = name.rsplit(".", 1)[-1]
    aliases = {
        "concrete_flooded": "concrete", "tile_floor": "tile",
        "metal_panel": "metalpanel", "metal_combine": "metal",
        "metal_thin": "metal", "metal_grate": "metalgrate",
        "metal_box": "metal_box", "wood_solid": "wood", "wood_plank": "wood",
        "mud_puddles": "mud", "linoleum_wet": "tile", "foliage": "grass",
        "asphalt": "concrete", "cloth": "carpet", "drywall": "plaster",
    }
    return aliases.get(name, name)
