"""
Grouping loose textures into material sets by filename.

Not every asset arrives with a vmat. Marketplace packs, Substance exports and
scraped addons ship folders of PNGs whose only structure is their names -
``crate_01_BaseColor.png``, ``crate_01_Normal.png``, ``crate_01_ORM.png``. This
module turns that into material sets the builder can consume.

The rules, in the order they matter:

1. **Split the stem into tokens** on ``_``, ``-``, ``.`` and space.
2. **Strip role and noise tokens** to get the asset key. ``crate_01_BaseColor``
   and ``crate_01_Normal`` both reduce to ``crate_01``, so they group.
3. **Claim the packed ORM first.** A file whose tokens contain ``orm`` is the
   packed occlusion/roughness/metallic map, and it is removed from the pool
   before the other roles are matched - otherwise ``foo_orm`` scores on the
   ``r``/``m`` aliases and gets claimed as roughness.
4. **Score the rest per role**, preferring exact role words over weak ones
   (``base`` is weak - it appears in ``base_color`` but also in
   ``concrete_base``), and preferring files that sit beside the ORM.
5. **Refuse ties.** Two files scoring identically for one role means the naming
   is ambiguous; the set records a warning and leaves the role empty rather than
   baking a coin-flip into the material.

Grouping is per directory. Two folders can each hold a ``crate_01`` set without
one stealing the other's normal map.
"""

from __future__ import annotations

from s2kit.core.asset_defaults import MISSING_METAL_MODES

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np

from s2kit.pipelines import pbr_maps, textures
from s2kit.pipelines.materials import MaterialSource

#: Roles resolved by name, in the order they are claimed.
SET_ROLES: tuple[str, ...] = ("color", "normal", "ao", "roughness", "metallic",
                              "selfillum", "heightmap", "translucency")

#: Filename tokens that identify a role. Longest/most specific first is not
#: required - matching is by exact token, not prefix.
ROLE_TOKENS: dict[str, tuple[str, ...]] = {
    "orm": ("orm", "arm", "rma", "occlusionroughnessmetallic"),
    "color": ("color", "colour", "albedo", "diffuse", "basecolor",
              "base_color", "basecolour", "base", "col", "bc", "diff"),
    "normal": ("normal", "normalgl", "normal_opengl", "normaldx", "normal_dx",
               "nrm", "nor", "normals", "nrml"),
    "ao": ("ao", "occlusion", "ambientocclusion", "ambient_occlusion", "occ",
           "mixed_ao"),
    "roughness": ("roughness", "rough", "rgh"),
    "metallic": ("metallic", "metal", "metalness", "mtl"),
    "selfillum": ("emissive", "emission", "emit", "illum", "illumin", "glow",
                  "selfillum", "self_illum"),
    "heightmap": ("height", "displacement", "disp", "heightmap", "parallax"),
    "translucency": ("opacity", "transparency", "translucency", "alpha", "opac"),
}

#: Tokens that carry no role but do appear in names, so they must not become
#: part of the asset key or two resolutions of one asset would not group.
NOISE_TOKENS: frozenset[str] = frozenset({
    "psd", "tga", "png", "jpg", "jpeg", "tif", "tiff", "bmp",
    "1k", "2k", "4k", "8k", "16k",
    "512", "1024", "2048", "4096", "8192",
    "directx", "opengl", "dx", "gl", "ogl",
    "hi", "lo", "low", "high", "tile", "tiling", "map",
})

#: Role words weak enough that a stronger match on the same file should win.
WEAK_TOKENS: frozenset[str] = frozenset({"base", "d", "n", "r", "m"})

#: How a missing metallic map can be synthesised - see `MISSING_METAL_MODES`.


@dataclass
class TextureSet:
    """One asset's textures, grouped and role-resolved."""

    key: str
    name: str
    directory: Path
    files: list[Path] = field(default_factory=list)
    resolved: dict[str, Path] = field(default_factory=dict)
    orm: Optional[Path] = None
    warnings: list[str] = field(default_factory=list)

    def has(self, role: str) -> bool:
        if role in self.resolved:
            return True
        # A packed ORM stands in for all three of the maps it carries.
        return self.orm is not None and role in ("ao", "roughness", "metallic")

    @property
    def buildable(self) -> bool:
        return "color" in self.resolved

    def roles(self) -> str:
        """A compact "what did we find" string for the results table."""
        found = [role for role in SET_ROLES if self.has(role)]
        if self.orm is not None:
            found.append("orm")
        return ", ".join(found) or "-"

    def to_source(self, *, missing_metal: str = "off",
                  include_selfillum: bool = True) -> MaterialSource:
        """Bridge to the builder's input record, unpacking any ORM in memory.

        A packed ORM is split into three channel arrays rather than three
        temporary PNGs: the builder accepts arrays directly, so the round trip
        through disk (and the temp folder it needed cleaning up afterwards) is
        gone.
        """
        source = MaterialSource(
            color=self.resolved.get("color"),
            normal=self.resolved.get("normal"),
            ao=self.resolved.get("ao"),
            roughness=self.resolved.get("roughness"),
            metallic=self.resolved.get("metallic"),
            translucency=self.resolved.get("translucency"),
            heightmap=self.resolved.get("heightmap"),
            selfillum=(self.resolved.get("selfillum")
                       if include_selfillum else None),
        )

        if self.orm is not None:
            packed = textures.load_rgba(self.orm)
            if packed is not None:
                ao, roughness, metallic = pbr_maps.split_orm(packed)
                # Explicit single-channel maps beside an ORM are the more
                # deliberate authoring, so they win the role.
                source.ao = source.ao or ao
                source.roughness = source.roughness or roughness
                source.metallic = source.metallic or metallic

        if source.metallic is None and missing_metal != "off":
            source.metallic = synth_metallic(self.resolved.get("color"),
                                             missing_metal)
        return source


def synth_metallic(color_path: Optional[Path],
                   mode: str) -> Optional[np.ndarray]:
    """Build a metallic map for a set that has none.

    Source 2 assets sometimes ship without one because the shader's
    `g_flMetalness` covered it. `full` makes the whole surface metal; the
    `albedo_*` modes reuse an albedo channel as a rough stand-in, which works
    surprisingly well on painted-metal props where the artist already separated
    the metal by hue.
    """
    if mode == "full":
        # Size does not matter here: the builder scales every input up to the
        # working resolution, and a constant map scales exactly.
        return np.ones((4, 4), dtype=np.float32)
    index = {"albedo_r": 0, "albedo_g": 1, "albedo_b": 2}.get(mode)
    if index is None or color_path is None:
        return None
    albedo = textures.load_rgba(color_path)
    if albedo is None:
        return None
    return np.clip(albedo[:, :, index], 0.0, 1.0)


# ----------------------------------------------------------------------
# Scanning
# ----------------------------------------------------------------------

def tokenize(stem: str) -> list[str]:
    return [token.lower() for token in re.split(r"[\s._\-]+", stem) if token]


def _all_role_tokens() -> frozenset[str]:
    return frozenset(token for tokens in ROLE_TOKENS.values() for token in tokens)


def asset_key(stem: str) -> tuple[str, str]:
    """Reduce a filename stem to (grouping key, display name).

    The display name keeps the original casing of the surviving tokens, so
    ``Crate_01_BaseColor`` yields the key ``crate_01`` and the name ``Crate_01``
    - which is what the generated material ends up called.
    """
    original = [token for token in re.split(r"[\s._\-]+", stem) if token]
    drop = _all_role_tokens() | NOISE_TOKENS
    kept = [(token.lower(), token) for token in original
            if token.lower() not in drop]
    if not kept:
        return stem.lower(), stem
    return ("_".join(lower for lower, _ in kept),
            "_".join(raw for _, raw in kept))


def scan(root: Any, *, recursive: bool = True,
         extensions: Iterable[str] = textures.IMAGE_EXTENSIONS) -> list[TextureSet]:
    """Group every texture under `root` into role-resolved sets."""
    folder = Path(root)
    if not folder.is_dir():
        return []
    allowed = {ext.lower() for ext in extensions}

    groups: dict[tuple[Path, str], TextureSet] = {}
    for base, files in _walk(folder, recursive):
        for name in files:
            path = base / name
            if path.suffix.lower() not in allowed:
                continue
            key, pretty = asset_key(path.stem)
            group = groups.get((base, key))
            if group is None:
                group = TextureSet(key=key, name=pretty, directory=base)
                groups[(base, key)] = group
            group.files.append(path)

    sets = sorted(groups.values(), key=lambda item: (str(item.directory), item.key))
    for group in sets:
        resolve_roles(group)
    return sets


def _walk(folder: Path, recursive: bool):
    if recursive:
        for base, _dirs, files in os.walk(folder):
            yield Path(base), files
        return
    try:
        yield folder, [item.name for item in folder.iterdir() if item.is_file()]
    except OSError:
        return


def resolve_roles(group: TextureSet) -> TextureSet:
    """Assign each file in a set to at most one role."""
    claimed: set[Path] = set()

    packed = _best(group, ROLE_TOKENS["orm"], claimed, "orm")
    if packed is not None:
        group.orm = packed
        claimed.add(packed)

    for role in SET_ROLES:
        chosen = _best(group, ROLE_TOKENS[role], claimed, role)
        if chosen is not None:
            group.resolved[role] = chosen
            claimed.add(chosen)
    return group


def _best(group: TextureSet, tokens: Iterable[str], claimed: set[Path],
          role: str) -> Optional[Path]:
    """The single best file for one role, or None when absent or ambiguous.

    Ambiguity is judged on the token score alone. Two files that claim a role
    equally strongly - `foo_normal.png` beside `foo_nrm.png` - are a genuine
    question about which map the artist meant, and answering it by filename
    length would silently bake a coin flip into the material.
    """
    wanted = set(tokens)
    scored: list[tuple[float, Path]] = []

    for path in group.files:
        if path in claimed:
            continue
        path_tokens = set(tokenize(path.stem))
        matches = wanted.intersection(path_tokens)
        if not matches:
            continue
        score = max(50.0 if token in WEAK_TOKENS else 100.0 for token in matches)
        if role == "normal":
            if path_tokens & {"directx", "dx", "normaldx", "normal_dx",
                              "normaldirectx"}:
                score += 20.0
            elif path_tokens & {"opengl", "gl", "ogl", "normalgl",
                                "normal_opengl"}:
                score -= 10.0
        if group.orm is not None and path.parent == group.orm.parent:
            score += 10.0
        scored.append((score, path))

    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], len(item[1].stem), item[1].name))
    best = scored[0][0]
    tied = [path for score, path in scored if abs(score - best) < 1e-6]
    if len(tied) > 1:
        group.warnings.append(
            f"ambiguous {role}: " + ", ".join(path.name for path in tied[:4]))
        return None
    return scored[0][1]
