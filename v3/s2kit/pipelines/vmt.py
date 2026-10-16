"""
VMT generation.

A VMT is just KeyValues, but *which* keys are legal depends on the engine
branch: `$phongalbedotint`, `$envmapfresnel` as a float, `$blendtintbybasealpha`
and Phong on `LightmappedGeneric` all arrived after the HL2 shaders shipped.
Writing them into a VMT for a branch that predates them does not error - the
engine silently ignores the key and the material looks wrong for reasons nothing
reports. So the branch table below gates every parameter that is not universal.

Builders return the VMT text rather than writing it, so a tool can show the
material it is about to write before it writes it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

#: What each Source 1 branch actually supports. Anything absent here is assumed
#: to be HL2-era, which is the conservative choice - a missing parameter costs
#: fidelity, an illegal one costs a broken material.
BRANCH_CAPABILITIES: dict[str, dict[str, bool]] = {
    "hl2": {},
    "source2013_sp": {},
    "source2013_mp": {
        "phong_albedo_tint": True, "phong_exponent_factor": True,
        "envmap_fresnel_float": True, "envmap_lightscale": True,
        "blend_tint_by_base_alpha": True, "lightmapped_phong": True,
    },
    "gmod": {
        "phong_albedo_tint": True, "phong_exponent_factor": True,
        "envmap_fresnel_float": True, "envmap_lightscale": True,
        "blend_tint_by_base_alpha": True, "lightmapped_phong": True,
    },
    "tf2": {
        "phong_albedo_tint": True, "phong_exponent_factor": True,
        "envmap_fresnel_float": True, "envmap_lightscale": True,
        "blend_tint_by_base_alpha": True, "lightmapped_phong": True,
    },
    "l4d2": {
        "phong_albedo_tint": True, "phong_exponent_factor": True,
        "envmap_fresnel_float": True, "envmap_lightscale": True,
        "blend_tint_by_base_alpha": True, "lightmapped_phong": True,
    },
}

BRANCH_LABELS: tuple[tuple[str, str], ...] = (
    ("gmod", "Garry's Mod"),
    ("l4d2", "Left 4 Dead 2"),
    ("tf2", "Team Fortress 2"),
    ("source2013_mp", "Source 2013 multiplayer"),
    ("source2013_sp", "Source 2013 singleplayer"),
    ("hl2", "Half-Life 2 (no modern parameters)"),
)

#: Parameters grouped into the comment sections a generated VMT is written in.
SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Textures", ("$basetexture", "$bumpmap", "$phongexponenttexture",
                  "$texture1", "$texture2", "$texture3")),
    ("Shader", ("$emissionscale", "$blendemissionbycolor", "$parallaxscale",
                "$alphablend", "$cull")),
    ("Phong", ("$phong", "$phongboost", "$phongexponentfactor", "$phongtint",
               "$phongalbedotint", "$phongfresnelranges")),
    ("Environment map", ("$envmap", "$envmapmask", "$envmaptint",
                         "$envmapcontrast", "$envmapsaturation",
                         "$envmapfresnel", "$normalmapalphaenvmapmask",
                         "$envmaplightscale")),
    ("Rim light", ("$rimlight", "$rimlightexponent", "$rimlightboost",
                   "$rimmask")),
    ("Transparency", ("$translucent", "$alphatest", "$alphatestreference",
                      "$nocull")),
    ("Tint (colourable prop)", ("$color2", "$blendtintbybasealpha",
                                "$blendtintcoloroverbase")),
    ("Self-illumination", ("$selfillum", "$selfillummask", "$selfillumtint")),
    ("Emissive blend", ("$EmissiveBlendEnabled", "$EmissiveBlendStrength",
                        "$EmissiveBlendTexture", "$EmissiveBlendBaseTexture",
                        "$EmissiveBlendFlowTexture", "$EmissiveBlendTint",
                        "$EmissiveBlendScrollVector")),
    ("Surface", ("$surfaceprop", "$halflambert", "$model")),
)


def capabilities(branch: str) -> dict[str, bool]:
    return BRANCH_CAPABILITIES.get(branch, BRANCH_CAPABILITIES["hl2"])


def _number(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _vec3(values: Sequence[float]) -> str:
    return "[" + " ".join(_number(float(v)) for v in values) + "]"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class VmtResult:
    """A generated VMT plus what the generator decided along the way."""

    text: str
    shader: str
    params: dict[str, str] = field(default_factory=dict)
    #: Human-readable notes worth surfacing in the log ("tint skipped: ...").
    notes: list[str] = field(default_factory=list)

    def write(self, path: Any) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.text, encoding="utf-8")
        return target


def render(shader: str, params: dict[str, str],
           header: Sequence[str] = (),
           trailer: Sequence[str] = ()) -> str:
    """Format a parameter dict as VMT text, grouped into comment sections.

    `trailer` lines are emitted inside the shader block after the parameters,
    for the nested blocks that are not key/value pairs (a `Proxies` block).
    """
    lines: list[str] = [f"// {line}" for line in header]
    if lines:
        lines.append("")
    lines += [f'"{shader}"', "{"]

    emitted: set[str] = set()
    for title, keys in SECTIONS:
        present = [key for key in keys if key in params]
        if not present:
            continue
        if emitted:
            lines.append("")
        lines.append(f"    // {title}")
        for key in present:
            lines.append(f'    "{key}" "{params[key]}"')
            emitted.add(key)

    extra = [key for key in params if key not in emitted]
    if extra:
        lines.append("")
        lines.append("    // Other parameters")
        for key in extra:
            lines.append(f'    "{key}" "{params[key]}"')

    if trailer:
        lines.append("")
        lines.extend(f"    {line}" for line in trailer)

    lines.append("}")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------
# Fake PBR
# ----------------------------------------------------------------------

def fake_pbr(material_name: str, material_path: str, *,
             shader: str = "VertexLitGeneric",
             branch: str = "gmod",
             envmap: str = "env_cubemap",
             stats: Optional[Any] = None,
             emit_bumpmap: bool = True,
             emit_phong: bool = True,
             emit_envmap: bool = True,
             has_envmap_mask: bool = True,
             surfaceprop: str = "",
             tintable: bool = False,
             tint_mode_used: str = "off",
             extra: Optional[dict[str, str]] = None) -> VmtResult:
    """Build a stock-shader VMT that fakes PBR with Phong and a cubemap.

    The `emit_*` flags gate whole parameter blocks. Turn one off when the source
    material had no matching signal - no roughness or metallic means no Phong, a
    flat normal means no bumpmap - and skip the corresponding VTF too, so the
    VMT never references a file that was not written.

    Bump-mapped VertexLitGeneric uses the normal alpha for its reflection mask;
    its stock Phong path never loads the separate envmap-mask texture. That
    texture can remain an intermediate artifact but is not referenced here.

    `emit_bumpmap` takes Phong down with it: Phong shades against the bumpmap's
    normal and is ignored without one. That is also the shape a
    `prop_static` wants, since every branch before the CS:GO engine discards
    VRAD's baked per-vertex lighting on a model carrying any of the three.

    `tintable` enables the colourable-prop block. The caller must already have
    baked the tint mask into the basetexture alpha; the entity's render colour
    then tints only the masked regions, with no material proxy needed. It is
    silently skipped when the branch lacks the parameter, the shader is not
    `VertexLitGeneric`, or the material carries a conflicting parameter -
    `$selfillum`, `$translucent` and `$alphatest` each break the
    `$blendtintbybasealpha` shader combo.
    """
    caps = capabilities(branch)
    is_vertex_lit = shader == "VertexLitGeneric"
    is_lightmapped = shader == "LightmappedGeneric"
    notes: list[str] = []

    avg_roughness = float(getattr(stats, "avg_roughness", 0.5))
    avg_metallic = float(getattr(stats, "avg_metallic", 0.0))
    rough_dielectric = bool(getattr(stats, "is_rough_dielectric", False))
    smoothness = _clamp(1.0 - avg_roughness, 0.0, 1.0)

    params: dict[str, str] = {
        "$basetexture": f"{material_path}/{material_name}_color",
    }
    if emit_bumpmap:
        params["$bumpmap"] = f"{material_path}/{material_name}_normal"

    # Phong shades against the bumpmap's normal, so the block is dead weight
    # without one - and the two together are what costs a prop_static its
    # baked lighting on every branch before CS:GO.
    phong_written = emit_bumpmap and emit_phong and (
        is_vertex_lit or (is_lightmapped and caps.get("lightmapped_phong")))
    if phong_written:
        params.update({
            "$phong": "1",
            "$phongexponenttexture": f"{material_path}/{material_name}_phong",
            # Keep the direct-light highlight controlled while giving Phong a
            # clear contribution alongside the cubemap.
            "$phongboost": _number(_clamp(0.5 + 3.0 * smoothness ** 2, 0.5, 3.5)),
            "$phongfresnelranges": _fresnel_ranges(avg_metallic, avg_roughness),
        })
        if caps.get("phong_albedo_tint"):
            params["$phongalbedotint"] = "1"
        else:
            params["$phongtint"] = "[1 1 1]"
        if caps.get("phong_exponent_factor"):
            params["$phongexponentfactor"] = "90"

    if emit_envmap and not rough_dielectric:
        params.update({
            "$envmap": envmap,
            "$envmaptint": _vec3([_clamp(0.04 + 0.3 * smoothness ** 2, 0.04, 0.34)] * 3),
            "$envmapcontrast": "0",
            "$envmapsaturation": "1",
        })
        # $normalmapalphaenvmapmask reads the bumpmap alpha; without a bumpmap
        # the engine logs a missing-texture lookup and the mask path is dead.
        if emit_bumpmap:
            params["$normalmapalphaenvmapmask"] = "1"
        if has_envmap_mask and not (is_vertex_lit and emit_bumpmap):
            params["$envmapmask"] = f"{material_path}/{material_name}_envmask"
        elif has_envmap_mask:
            notes.append("envmap mask uses bumpmap alpha; separate mask texture is inactive")
        if caps.get("envmap_fresnel_float"):
            params["$envmapfresnel"] = "1.0"
        if is_lightmapped and caps.get("envmap_lightscale"):
            params["$envmaplightscale"] = "1"
    elif emit_envmap and rough_dielectric:
        notes.append("envmap skipped: rough dielectric, nothing would reflect")

    if is_vertex_lit:
        params.update({"$halflambert": "0", "$model": "1"})

    if surfaceprop.strip():
        params["$surfaceprop"] = surfaceprop.strip()
    if extra:
        params.update(extra)

    # Evaluated after the merge so the conflict check sees the final set.
    if tintable:
        conflicts = [key for key in ("$selfillum", "$translucent", "$alphatest")
                     if str(params.get(key, "0")).strip() == "1"]
        if not caps.get("blend_tint_by_base_alpha"):
            notes.append(f"tint skipped: '{branch}' has no $blendtintbybasealpha")
        elif not is_vertex_lit:
            notes.append(f"tint skipped: $blendtintbybasealpha is "
                         f"VertexLitGeneric-only, shader is '{shader}'")
        elif conflicts:
            notes.append("tint skipped: conflicts with "
                         + ", ".join(conflicts))
        else:
            # White $color2 and coloroverbase 0 are engine defaults, written out
            # so the tint contract is visible and tweakable in the VMT itself.
            params["$color2"] = "[1 1 1]"
            params["$blendtintbybasealpha"] = "1"
            params["$blendtintcoloroverbase"] = "0"
            notes.append("tint on: mask in basetexture alpha, white = tinted")

    header = [
        "Generated by Source 2 Porting Kit - Fake PBR",
        f"Material: {material_path}/{material_name}",
        f"Branch: {branch}   Shader: {shader}",
        f"Approximation from metallic={avg_metallic:.3f} "
        f"roughness={avg_roughness:.3f}; not energy-conserving",
        f"Phong tint mode: {tint_mode_used}",
    ]
    if not emit_bumpmap:
        header.append("No $bumpmap, and no $phong with it - a "
                      "prop_static using this keeps its baked lighting")
    header += [*notes, f"Date: {time.strftime('%Y-%m-%dT%H:%M:%S')}"]
    return VmtResult(render(shader, params, header), shader, params, notes)


def _fresnel_ranges(avg_metallic: float, avg_roughness: float) -> str:
    """Pick `$phongfresnelranges` from the material's average character.

    The three numbers are the Phong response at facing, mid and grazing angles.
    Metals retain a stronger facing response than dielectrics. The grazing
    response is bounded at one instead of amplifying rough surfaces by 4-8x.
    """
    facing = 0.04 + 0.46 * _clamp(avg_metallic, 0.0, 1.0)
    midpoint = 0.5 + 0.25 * _clamp(avg_metallic, 0.0, 1.0)
    return _vec3([facing, midpoint, 1.0])


def selfillum_params(glow_path: str, base_color_path: str, *,
                     mode: str = "selfillum",
                     tint: Sequence[float] = (1.0, 1.0, 1.0),
                     brightness: float = 1.0) -> dict[str, str]:
    """Build the glow parameters for one of the three techniques.

    ``selfillum``
        Classic `$selfillum` + `$selfillummask`. Brightness folds into
        `$selfillumtint` because older branches lack `$selfillummaskscale`.
        Incompatible with `$translucent` on some branches, and it breaks the
        `$blendtintbybasealpha` combo.
    ``emissiveblend``
        The `$EmissiveBlend*` family with the glow mask as the emission source.
        Coexists with `$translucent` and `$phong`, which is why it is the
        recommended technique on L4D2 and Alyx-derived targets.
    ``emissiveblend_basecolor``
        Same family, but the *base colour* map is the emission source and the
        glow mask decides where it glows - the look `$selfillum` gives (the
        diffuse glowing in its own colour) through the stronger path.
    """
    mode = (mode or "selfillum").lower()
    params: dict[str, str] = {}

    if mode in ("emissiveblend", "emissiveblend_basecolor"):
        params["$EmissiveBlendEnabled"] = "1"
        # $EmissiveBlendStrength clamps to [0, 1] in the shader, so anything
        # brighter has to ride along in the tint: strength absorbs everything up
        # to 1.0 and the remainder multiplies through the per-channel tint.
        value = max(0.0, float(brightness))
        params["$EmissiveBlendStrength"] = f"{min(1.0, value):.3f}"
        scale = max(1.0, value)
        if mode == "emissiveblend_basecolor":
            params["$EmissiveBlendTexture"] = glow_path
            params["$EmissiveBlendBaseTexture"] = base_color_path
        else:
            # $EmissiveBlendTexture is required even when the glow is static;
            # a stock white is the recommended placeholder.
            params["$EmissiveBlendTexture"] = "vgui/white"
            params["$EmissiveBlendBaseTexture"] = glow_path
        params["$EmissiveBlendFlowTexture"] = "vgui/white"
        params["$EmissiveBlendTint"] = _vec3(
            [max(0.0, float(component) * scale) for component in tint])
        params["$EmissiveBlendScrollVector"] = "[0 0]"
        return params

    params["$selfillum"] = "1"
    params["$selfillummask"] = glow_path
    scaled = tuple(max(0.0, float(component) * brightness) for component in tint)
    if scaled != (1.0, 1.0, 1.0):
        params["$selfillumtint"] = _vec3(scaled)
    return params


# ----------------------------------------------------------------------
# Exo PBR
# ----------------------------------------------------------------------

def exo_pbr(material_name: str, material_path: str, *,
            emission_scale: float = 0.0,
            parallax_scale: float = 0.0,
            alpha_blend: bool = False,
            has_emissive: bool = False) -> VmtResult:
    """Build a VMT for the ExoPBR `screenspace_general_8tex` shader.

    Unlike the Fake PBR path this is a real PBR shader, so the VMT is short: the
    three (or four) packed textures, and the scalars that switch the optional
    passes on.

    * `$basetexture` - albedo RGB, alpha opacity
    * `$texture1` - ARM: R AO, G roughness, B metallic, A height
    * `$texture2` - normal, DirectX Y- convention
    * `$texture3` - emission, gated by `$emissionscale`
    """
    shader = "screenspace_general_8tex"
    params: dict[str, str] = {
        "$basetexture": f"{material_path}/{material_name}_base",
        "$texture1": f"{material_path}/{material_name}_arm",
        "$texture2": f"{material_path}/{material_name}_normal",
    }
    if has_emissive:
        params["$texture3"] = f"{material_path}/{material_name}_emissive"
    if emission_scale:
        params["$emissionscale"] = _number(emission_scale)
        params["$blendemissionbycolor"] = "1"
    if parallax_scale:
        params["$parallaxscale"] = _number(parallax_scale)
    if alpha_blend:
        params["$alphablend"] = "1"
    params["$model"] = "1"
    params["$cull"] = "1"

    header = [
        "Generated by Source 2 Porting Kit - Exo PBR",
        f"Material: {material_path}/{material_name}",
        f"Date: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
    ]
    # The shader is driven by a material proxy, which is a nested block rather
    # than a key/value pair - hence the trailer.
    trailer = ('"Proxies"', "{", "    ExoPBR {}", "}")
    return VmtResult(render(shader, params, header, trailer), shader, params)


# ----------------------------------------------------------------------
# Reading
# ----------------------------------------------------------------------

#: VMT texture keys mapped to the role names the material tools use.
TEXTURE_KEYS: dict[str, str] = {
    "$basetexture": "color",
    "$bumpmap": "normal",
    "$phongexponenttexture": "phong",
    "$envmapmask": "envmask",
    "$selfillummask": "selfillum",
}


def read_texture_refs(path: Any) -> dict[str, str]:
    """Pull the texture references out of an existing VMT, keyed by role.

    Comments are stripped per line and the first value for a key wins, which is
    how the engine itself reads a VMT.
    """
    import re

    # Tolerates `"$key" "value"`, `$key value`, and the trailing whitespace real
    # VMTs sometimes carry inside the quoted key.
    pattern = re.compile(
        r'"?\s*(\$[A-Za-z_][A-Za-z_0-9]*)\s*"?\s+"?([^"\s{}]+)"?')

    refs: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return refs
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line or line in ("{", "}"):
            continue
        match = pattern.match(line)
        if not match:
            continue
        role = TEXTURE_KEYS.get(match.group(1).lower())
        if role is None or role in refs:
            continue
        refs[role] = match.group(2)
    return refs


def resolve_texture_ref(ref: str, vmt_dir: Path,
                        search_root: Optional[Path] = None) -> Optional[Path]:
    """Turn a VMT texture reference into a real `.vtf` path.

    References are written relative to the game's `materials/` folder, which may
    or may not be anywhere near the folder being scanned - so try the VMT's own
    directory, the scan root, and any `materials/` ancestor.
    """
    cleaned = ref.replace("\\", "/").strip()
    if not cleaned:
        return None
    stem = cleaned[:-4] if cleaned.lower().endswith(".vtf") else cleaned

    candidates = [vmt_dir / (Path(stem).name + ".vtf"), vmt_dir / (stem + ".vtf")]
    if search_root is not None:
        candidates.append(search_root / (stem + ".vtf"))
    materials = _materials_root(vmt_dir)
    if materials is not None:
        candidates.append(materials / (stem + ".vtf"))

    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _materials_root(start: Path) -> Optional[Path]:
    """Walk up from `start` looking for a `materials` directory."""
    for parent in [start, *start.parents]:
        if parent.name.lower() == "materials":
            return parent
        candidate = parent / "materials"
        if candidate.is_dir():
            return candidate
    return None
