"""
PBR -> Source 1 channel baking.

Source 2 authors materials the modern way: albedo, normal, roughness, metallic,
AO, each its own map, combined by a physically-based shader. Source 1 has no
metallic workflow at all - it has Phong, a cubemap and a rim term, and each of
those reads one channel of one texture. Converting between them means baking:
collapsing the PBR inputs into the specific channel layout the stock Source 1
shaders sample.

The layout this module produces (the "Fake PBR" convention):

===================  ====================================  ==================
File                 RGB                                   Alpha
===================  ====================================  ==================
``_color.vtf``       albedo, AO baked in, metal darkened   opacity or tint mask
``_normal.vtf``      tangent normal                        Phong mask
``_phong.vtf``       R = exponent, G = metallic            unused
``_envmask.vtf``     coloured reflection tint              unused
===================  ====================================  ==================

The approximation is not energy-conserving and never can be: Source 1 lights
diffuse and specular independently, so the metal energy split has to be faked by
darkening the albedo and pushing the difference into Phong and the cubemap. The
knobs below (`metal_diffuse_suppression`, `colored_metal_relief`,
`phong_tint_mode`) all exist to trade off where that faked energy lands.

Everything takes and returns numpy arrays; nothing touches disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

from s2kit.pipelines import textures

#: Floor used when dividing by linearised albedo luminance, so dark pixels do
#: not blow the compensated mask up. Matches the dielectric F0 baseline.
TINT_DIVIDE_FLOOR = 0.04

#: Residual diffuse light compensates for stock Source 1 cubemap limitations.
METAL_DIFFUSE_SUPPRESSION = 0.92
#: Colored metals retain their hue through specular tint rather than diffuse relief.
COLORED_METAL_RELIEF = 0.0

#: Filename tokens that state a normal map's green-channel convention outright.
#: An artist who bothered to label it knows better than any default.
_OPENGL_TOKENS = frozenset({"opengl", "gl", "ogl", "normalgl", "normal_opengl",
                            "normalopengl", "yup", "y+", "plusy"})
_DIRECTX_TOKENS = frozenset({"directx", "dx", "normaldx", "normal_dx",
                             "normaldirectx", "ydown", "y-", "minusy"})


@dataclass(frozen=True)
class MaterialStats:
    """Averages the VMT writer uses to pick Phong and envmap parameters."""

    avg_roughness: float
    avg_metallic: float
    avg_smoothness: float
    has_metal: bool
    has_reflective: bool
    is_rough_dielectric: bool


# ----------------------------------------------------------------------
# Scalar plumbing
# ----------------------------------------------------------------------

def scalar_map(data: Optional[np.ndarray], height: int, width: int,
               default: float) -> np.ndarray:
    """A clamped 2-D scalar map of exactly (height, width).

    Missing data becomes a constant. Present data is reduced to its first
    channel and resized when it does not already match. Callers combine several
    of these element-wise, so honouring the size contract here is what lets a
    2048px roughness sit next to a 4096px metallic without a broadcast error.
    """
    if data is None:
        return np.full((height, width), default, dtype=np.float32)
    values = data[:, :, 0] if data.ndim > 2 else data
    values = np.clip(np.asarray(values, dtype=np.float32), 0.0, 1.0)
    if values.shape != (height, width):
        values = textures.fit(values, height, width)
    return values


def _match(height: int, width: int, ao: Optional[np.ndarray],
           roughness: Optional[np.ndarray],
           metallic: Optional[np.ndarray]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The canonical AO / roughness / metallic trio at one shared size."""
    return (scalar_map(ao, height, width, 1.0),
            scalar_map(roughness, height, width, 0.5),
            scalar_map(metallic, height, width, 0.0))


def emissive_weight(selfillum: Optional[np.ndarray], height: int,
                    width: int) -> Optional[np.ndarray]:
    """How strongly each pixel is marked as glowing, from a self-illum mask.

    The strongest colour channel rather than the luminance: a saturated red glow
    is fully emissive even though it carries only 0.21 of luminance, and the
    question here is whether the artist marked the pixel, not how bright it reads.
    """
    if selfillum is None:
        return None
    if selfillum.ndim > 2 and selfillum.shape[2] >= 3:
        weight = np.max(np.clip(np.asarray(selfillum[:, :, :3], dtype=np.float32),
                                0.0, 1.0), axis=2)
        if weight.shape != (height, width):
            weight = textures.fit(weight, height, width)
        return weight
    return scalar_map(selfillum, height, width, 0.0)


def relieve_ao(ao: Optional[np.ndarray], selfillum: Optional[np.ndarray],
               height: int, width: int, *,
               amount: float = 1.0) -> Optional[np.ndarray]:
    """Lift AO back towards unoccluded wherever the material glows.

    Ambient occlusion says how much *ambient* light reaches a surface, and an
    emissive panel makes its own - baking the crevice around a glowing strip over
    the strip itself dims it for a reason that does not apply to it. `amount` is
    how much of that occlusion to take back on a fully emissive pixel: 0 bakes AO
    exactly as authored, 1 removes it there entirely. Partially marked pixels get
    a proportional share, so the relief fades out with the mask instead of leaving
    a hard edge where it stops.

    Returns `ao` unchanged when there is nothing to relieve.
    """
    amount = float(np.clip(amount, 0.0, 1.0))
    if ao is None or amount <= 0.0:
        return ao
    weight = emissive_weight(selfillum, height, width)
    if weight is None:
        return ao
    values = scalar_map(ao, height, width, 1.0)
    return values + (1.0 - values) * (amount * weight)


def bokeh_blur(value: np.ndarray, radius: int = 3) -> np.ndarray:
    """Blur a scalar map with a circular aperture kernel."""
    radius = max(1, int(radius))
    yy, xx = np.mgrid[-radius:radius + 1, -radius:radius + 1]
    kernel = ((xx * xx + yy * yy) <= radius * radius).astype(np.float32)
    kernel /= kernel.sum()
    padded = np.pad(value.astype(np.float32), radius, mode="edge")
    result = np.zeros_like(value, dtype=np.float32)
    for y, x in zip(*np.nonzero(kernel)):
        result += kernel[y, x] * padded[y:y + value.shape[0],
                                        x:x + value.shape[1]]
    return result


def material_stats(roughness: Optional[np.ndarray],
                   metallic: Optional[np.ndarray],
                   height: int, width: int) -> MaterialStats:
    """Reduce the specular inputs to the handful of numbers the VMT needs."""
    rough = scalar_map(roughness, height, width, 0.5)
    metal = scalar_map(metallic, height, width, 0.0)
    avg_roughness = float(np.mean(rough))
    avg_metallic = float(np.mean(metal))
    return MaterialStats(
        avg_roughness=avg_roughness,
        avg_metallic=avg_metallic,
        avg_smoothness=1.0 - avg_roughness,
        has_metal=bool(np.max(metal) > 0.05),
        has_reflective=bool(
            np.max((1.0 - rough) * (0.04 + 0.96 * metal)) > 0.04),
        is_rough_dielectric=bool(avg_metallic < 0.05 and avg_roughness > 0.75),
    )


# ----------------------------------------------------------------------
# Fake PBR bakes
# ----------------------------------------------------------------------

def bake_basetexture(color: np.ndarray,
                     ao: Optional[np.ndarray],
                     metallic: Optional[np.ndarray], *,
                     ao_strength: float = 0.7,
                     ao_bokeh_strength: float = 0.35,
                     metal_diffuse_suppression: float = METAL_DIFFUSE_SUPPRESSION,
                     colored_metal_relief: float = COLORED_METAL_RELIEF,
                     translucency: Optional[np.ndarray] = None,
                     tintmask: Optional[np.ndarray] = None,
                     tint_amount: Optional[float] = None) -> np.ndarray:
    """Bake the `_color` basetexture: RGB with AO and metal darkening, plus alpha.

    RGB is AO-multiplied and darkened where the surface is metallic, because
    Source 1 has no metallic energy split - a metal that keeps its full albedo
    reads as a bright plastic.

    Alpha carries one of three payloads, in priority order:

    1. `translucency` (Source 2 ``TextureTranslucency``) so ``$translucent`` /
       ``$alphatest`` see the authored opacity;
    2. `tintmask` (Source 2 ``TextureTintMask``) for opaque colourable props via
       ``$blendtintbybasealpha`` - white takes the entity's render colour, black
       keeps the authored albedo, the same polarity Source 2 uses;
    3. the colour texture's own alpha, or opaque.

    Transparency always wins the channel: Source 1 cannot honour both a blend
    and a masked tint at once. `tint_amount` (Source 2 ``g_flModelTintAmount``,
    default 1.0) scales the mask before baking.

    `colored_metal_relief` (0..1) reduces the metal suppression on chromatic
    pixels, so coloured metals - gold, copper, brass - keep more of their albedo.
    1.0 means fully saturated metals receive no suppression at all; grayscale
    metals always take the full factor so steel stays energy-conservative.
    """
    rgb = np.clip(color[:, :, :3], 0.0, 1.0)
    height, width = rgb.shape[:2]
    ao_values, _, metal_values = _match(height, width, ao, None, metallic)

    # np.clip already handed us a private copy, so both transfers run in place -
    # at 4K each full-size float32 buffer is 192 MiB.
    linear = textures.srgb_to_linear(rgb, out=rgb)
    bokeh_strength = float(np.clip(ao_bokeh_strength, 0.0, 1.0))
    if bokeh_strength > 0.0:
        soft_ao = bokeh_blur(ao_values)
        ao_values = (ao_values * (1.0 - bokeh_strength)
                     + soft_ao * bokeh_strength)
    linear *= np.power(ao_values[:, :, np.newaxis], ao_strength)

    relief = float(np.clip(colored_metal_relief, 0.0, 1.0))
    if relief > 0.0:
        suppression = metal_diffuse_suppression * (
            1.0 - relief * textures.chroma(linear))
    else:
        suppression = np.full((height, width),
                              float(metal_diffuse_suppression), dtype=np.float32)
    linear *= (1.0 - suppression[:, :, np.newaxis]
               * metal_values[:, :, np.newaxis])

    srgb = textures.linear_to_srgb(linear, out=linear)
    np.clip(srgb, 0.0, 1.0, out=srgb)

    if translucency is not None:
        alpha = scalar_map(translucency, height, width, 1.0)
    elif tintmask is not None:
        alpha = scalar_map(tintmask, height, width, 0.0)
        amount = 1.0 if tint_amount is None else float(np.clip(tint_amount, 0.0, 1.0))
        if amount < 1.0:
            alpha = alpha * amount
    elif color.shape[2] >= 4:
        alpha = np.clip(color[:, :, 3].astype(np.float32), 0.0, 1.0)
    else:
        alpha = np.ones((height, width), dtype=np.float32)

    return textures.to_uint8(np.dstack([srgb, alpha]))


def phong_mask(roughness: Optional[np.ndarray],
               metallic: Optional[np.ndarray],
               ao: Optional[np.ndarray],
               height: int, width: int, *,
               strength: float = 0.5,
               color: Optional[np.ndarray] = None,
               tint_mode: str = "off",
               metal_diffuse_suppression: float = METAL_DIFFUSE_SUPPRESSION,
               colored_metal_relief: float = COLORED_METAL_RELIEF) -> np.ndarray:
    """The bumpmap alpha that drives ``$phong``.

    The base formula is Fresnel-ish: smooth metals reflect strongly, dielectrics
    keep a 0.04 floor. `strength` scales the result - 0.5 is the default because
    the unscaled mask reads far too hot in-engine; 0.0 disables Phong entirely.

    `tint_mode` compensates for ``$phongalbedotint 1``, where the engine
    multiplies the Phong output by the basetexture colour at runtime:

    ``off``
        No compensation.
    ``selective``
        Only coloured metallic pixels get divide-by-luminance compensation;
        dielectrics are suppressed towards zero so their specular does not pick
        up an albedo cast (the cubemap provides their reflection instead).
    ``blanket``
        Divide-by-luminance everywhere.

    When compensating, `color` (sRGB albedo) must be supplied, and
    `metal_diffuse_suppression` / `colored_metal_relief` must match what
    `bake_basetexture` used - otherwise the division is against a basetexture the
    engine will never sample.
    """
    ao_values, rough, metal = _match(height, width, ao, roughness, metallic)

    base = ((1.0 - rough) * (0.04 + 0.96 * metal) + 0.04 * (1.0 - metal))

    mode = (tint_mode or "off").lower()
    if mode != "off" and color is not None:
        # Reproduce the darkening bake_basetexture applies, so we divide by what
        # the engine actually samples.
        rgb = np.clip(color[:, :, :3], 0.0, 1.0)
        linear = textures.srgb_to_linear(rgb, out=rgb)
        relief = float(np.clip(colored_metal_relief, 0.0, 1.0))
        albedo_chroma = textures.chroma(linear) if (relief > 0.0 or mode == "selective") else None
        if relief > 0.0:
            suppression = metal_diffuse_suppression * (1.0 - relief * albedo_chroma)
        else:
            suppression = np.full((height, width),
                                  float(metal_diffuse_suppression), dtype=np.float32)
        baked = linear * (1.0 - suppression[:, :, np.newaxis]
                          * metal[:, :, np.newaxis])
        luma = np.maximum(textures.luminance(baked), TINT_DIVIDE_FLOOR)
        compensated = base / luma

        if mode == "selective":
            weight = np.clip(metal * albedo_chroma, 0.0, 1.0)
            # Dielectric region drops the 0.04 floor so Phong does not tint
            # neutral surfaces; their specular comes via $envmap.
            dielectric = (1.0 - rough) * (0.04 + 0.96 * metal) * metal
            mask = dielectric * (1.0 - weight) + compensated * weight
        else:
            mask = compensated
    else:
        mask = base

    mask *= ao_values
    mask *= max(0.0, float(strength))
    return np.clip(mask, 0.0, 1.0)


def normal_convention(source: Any) -> Optional[str]:
    """Read a normal map's stated convention off its filename.

    Returns ``"opengl"``, ``"directx"`` or None when the name says nothing.
    Only an explicit token counts - a bare `_normal` is not a claim, and
    guessing from pixels is not reliable: a normal map's green channel looks
    equally plausible either way unless you already know which way the surface
    bulges.
    """
    import re
    from pathlib import Path as _Path

    if source is None:
        return None
    stem = _Path(str(source)).stem.lower()
    tokens = {token for token in re.split(r"[\s._\-]+", stem) if token}
    if tokens & _DIRECTX_TOKENS:
        return "directx"
    if tokens & _OPENGL_TOKENS:
        return "opengl"
    return None


def should_invert_green(mode: str, source: Any = None) -> bool:
    """Whether to flip the green channel, for `mode` and this source file.

    ``flip`` always, ``keep`` never, ``auto`` flips unless the filename says the
    map is already in Source 1's convention.

    The direction, per the Valve Developer Community: *"Source and Source 2
    (even with DX11) have opposite interpretations of green channels in normal
    maps, with Source taking the DirectX format and Source 2 taking the OpenGL
    format."* Source 2's VR Standard shader - the one HLA materials use - takes
    OpenGL ``+Y``, and Source 2's own texture compiler calls the other setting
    *"Legacy Source 1 inverted normals"*. So a Source 2 export is green-up and
    Source 1 wants green-down: porting one to the other inverts green, always.

    `auto` is the default because that is what the kit converts. The exception
    is a map an artist labelled ``_normal_dx``: it is already in Source 1's
    convention, and flipping it would invert every surface on the model.
    """
    mode = (mode or "auto").lower()
    if mode == "flip":
        return True
    if mode == "keep":
        return False
    return normal_convention(source) != "directx"


def bake_bumpmap(normal: np.ndarray,
                 ao: Optional[np.ndarray],
                 metallic: Optional[np.ndarray],
                 roughness: Optional[np.ndarray], *,
                 invert_green: bool = False,
                 include_phong: bool = True,
                 phong_strength: float = 0.5,
                 color: Optional[np.ndarray] = None,
                 tint_mode: str = "off",
                 metal_diffuse_suppression: float = METAL_DIFFUSE_SUPPRESSION,
                 colored_metal_relief: float = COLORED_METAL_RELIEF) -> np.ndarray:
    """Pack the `_normal` bumpmap: RGB tangent normal, alpha Phong mask.

    With `include_phong` off the alpha is left opaque - a well-formed VTF that
    makes no specular claim, for materials whose source had no roughness or
    metallic signal at all.
    """
    rgb = np.clip(normal[:, :, :3].copy(), 0.0, 1.0)
    if invert_green:
        rgb[:, :, 1] = 1.0 - rgb[:, :, 1]
    height, width = rgb.shape[:2]

    if include_phong:
        alpha = phong_mask(
            roughness, metallic, ao, height, width,
            strength=phong_strength, color=color, tint_mode=tint_mode,
            metal_diffuse_suppression=metal_diffuse_suppression,
            colored_metal_relief=colored_metal_relief)
    else:
        alpha = np.ones((height, width), dtype=np.float32)
    return textures.to_uint8(np.dstack([rgb, alpha]))


def bake_phong_exponent(roughness: Optional[np.ndarray],
                        metallic: Optional[np.ndarray],
                        height: int, width: int, *,
                        gloss_gamma: float = 2.0,
                        phong_strength: float = 0.5) -> np.ndarray:
    """The `_phong` exponent texture: R = exponent, G = metallic, B = 0, A = 1.

    R is ``(1 - roughness) ^ gloss_gamma``, scaled by `phong_strength` so a
    lower strength yields a softer specular response rather than only a dimmer
    mask.
    """
    # No AO here on purpose: the exponent controls highlight *tightness*, and
    # occluding a highlight by making it broader is not what AO means. AO acts on
    # the Phong mask instead, which controls the highlight's strength.
    _ao, rough, metal = _match(height, width, None, roughness, metallic)
    exponent = np.power(np.clip(1.0 - rough, 0.0, 1.0), gloss_gamma)
    exponent = np.clip(exponent * max(0.0, float(phong_strength)), 0.0, 1.0)
    blue = np.zeros((height, width), dtype=np.float32)
    opaque = np.ones((height, width), dtype=np.float32)
    return textures.to_uint8(np.dstack([exponent, metal, blue, opaque]))


def bake_envmap_mask(color: np.ndarray,
                     ao: Optional[np.ndarray],
                     metallic: Optional[np.ndarray],
                     roughness: Optional[np.ndarray], *,
                     envmask_gamma: float = 1.5) -> np.ndarray:
    """The `_envmask` texture: a coloured ``$envmapmask`` for metal tint fakery.

    Reflection colour is ``mix(0.04, albedo, metallic)`` - the dielectric F0
    baseline for non-metals, the albedo itself for metals, which is what makes
    gold reflect gold - scaled by smoothness and AO.
    """
    rgb = np.clip(color[:, :, :3], 0.0, 1.0)
    height, width = rgb.shape[:2]
    ao_values, rough, metal = _match(height, width, ao, roughness, metallic)

    # In place on the clipped copy: 0.04*(1-m) + albedo*m is algebraically
    # 0.04 + m*(albedo - 0.04), which avoids both the full-size constant array
    # and the two full-size products the straight mix would build.
    mask = textures.srgb_to_linear(rgb, out=rgb)
    mask -= 0.04
    mask *= metal[:, :, np.newaxis]
    mask += 0.04
    mask *= np.power(np.clip(1.0 - rough, 0.0, 1.0),
                     envmask_gamma)[:, :, np.newaxis]
    mask *= ao_values[:, :, np.newaxis]
    mask = np.clip(mask * 3.0, 0.0, 1.0)

    opaque = np.ones((height, width), dtype=np.float32)
    return textures.to_uint8(np.dstack([mask, opaque]))


def bake_selfillum(selfillum: np.ndarray) -> np.ndarray:
    """The `_selfillum` mask: an sRGB colour map where non-black pixels glow."""
    height, width = selfillum.shape[:2]
    if selfillum.ndim == 3 and selfillum.shape[2] >= 3:
        rgb = np.clip(selfillum[:, :, :3], 0.0, 1.0)
        alpha = (np.clip(selfillum[:, :, 3], 0.0, 1.0)
                 if selfillum.shape[2] >= 4
                 else np.ones((height, width), dtype=np.float32))
    else:
        grey = scalar_map(selfillum, height, width, 0.0)
        rgb = np.dstack([grey, grey, grey])
        alpha = np.ones((height, width), dtype=np.float32)
    return textures.to_uint8(np.dstack([rgb, alpha]))


# ----------------------------------------------------------------------
# Exo PBR bakes
# ----------------------------------------------------------------------

def bake_arm(ao: Optional[np.ndarray],
             roughness: Optional[np.ndarray],
             metallic: Optional[np.ndarray],
             heightmap: Optional[np.ndarray],
             height: int, width: int) -> np.ndarray:
    """The packed ARM texture the ExoPBR shader samples.

    R = AO, G = roughness, B = metallic, A = height (parallax). Unlike the Fake
    PBR bakes this is a straight repack - the shader does the PBR maths itself,
    so nothing is approximated here.
    """
    return textures.to_uint8(np.dstack([
        scalar_map(ao, height, width, 1.0),
        scalar_map(roughness, height, width, 1.0),
        scalar_map(metallic, height, width, 0.0),
        scalar_map(heightmap, height, width, 1.0),
    ]))


def bake_exo_normal(normal: np.ndarray, height: int, width: int) -> np.ndarray:
    """The ExoPBR normal: RGB inverted, blue zeroed, alpha preserved.

    The screenspace shader reconstructs Z itself and reads the inverted XY, so
    the blue channel is dead weight and is cleared rather than carried.
    """
    rgb = np.clip(normal[:, :, :3], 0.0, 1.0)
    alpha = (np.clip(normal[:, :, 3], 0.0, 1.0) if normal.shape[2] >= 4
             else np.ones((height, width), dtype=np.float32))
    inverted = 1.0 - rgb
    blue = np.zeros((height, width), dtype=np.float32)
    return textures.to_uint8(np.dstack([
        inverted[:, :, 0], inverted[:, :, 1], blue, alpha]))


def bake_exo_base(color: np.ndarray,
                  transparency: Optional[np.ndarray],
                  height: int, width: int) -> np.ndarray:
    """The ExoPBR base texture: RGB albedo untouched, alpha = opacity."""
    if color.ndim == 3 and color.shape[2] >= 3:
        rgb = np.clip(color[:, :, :3], 0.0, 1.0)
        alpha = (np.clip(color[:, :, 3], 0.0, 1.0) if color.shape[2] >= 4
                 else np.ones((height, width), dtype=np.float32))
    else:
        grey = scalar_map(color, height, width, 1.0)
        rgb = np.dstack([grey, grey, grey])
        alpha = np.ones((height, width), dtype=np.float32)
    if transparency is not None:
        alpha = scalar_map(transparency, height, width, 1.0)
    return textures.to_uint8(np.dstack([rgb, alpha]))


# ----------------------------------------------------------------------
# Reversing a bake
# ----------------------------------------------------------------------

def unbake_roughness(phong_red: np.ndarray, *, phong_strength: float = 0.5,
                     gloss_gamma: float = 2.0) -> np.ndarray:
    """Recover roughness from a `_phong` red channel.

    Exact inverse of `bake_phong_exponent`'s R, given the same two knobs. Values
    the bake clipped to 0 or 1 come back at the clip boundary, so a material
    authored fully rough round-trips to 1.0 rather than to noise.
    """
    gloss = np.clip(np.asarray(phong_red, dtype=np.float32)
                    / max(1e-4, float(phong_strength)), 0.0, 1.0)
    return np.clip(1.0 - np.power(gloss, 1.0 / max(0.01, float(gloss_gamma))),
                   0.0, 1.0)


def unbake_albedo(color_rgb: np.ndarray, metallic: Optional[np.ndarray], *,
                  metal_diffuse_suppression: float = METAL_DIFFUSE_SUPPRESSION) -> np.ndarray:
    """Undo the metal darkening in a baked basetexture.

    Only the metal term is recoverable. AO was multiplied into the same pixels
    and there is no second equation to separate it from the albedo, so what
    comes back is the AO-baked albedo with the metal darkening removed - good
    enough to re-author from, not a bit-exact original.
    """
    rgb = np.clip(np.asarray(color_rgb, dtype=np.float32), 0.0, 1.0)
    if metallic is None or metal_diffuse_suppression <= 0.0:
        return rgb
    metal = scalar_map(metallic, rgb.shape[0], rgb.shape[1], 0.0)
    divisor = np.maximum(1.0 - metal_diffuse_suppression * metal, 1e-3)
    return np.clip(rgb / divisor[:, :, np.newaxis], 0.0, 1.0)


def split_orm(orm: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Split a packed ORM texture into (AO, roughness, metallic) scalar maps."""
    if orm.ndim < 3 or orm.shape[2] < 3:
        raise ValueError("An ORM map needs at least three channels")
    return (np.clip(orm[:, :, 0], 0.0, 1.0),
            np.clip(orm[:, :, 1], 0.0, 1.0),
            np.clip(orm[:, :, 2], 0.0, 1.0))


def is_flat_normal(normal: np.ndarray, tolerance: float = 2.0 / 255.0) -> bool:
    """Whether a normal map is uniformly (0.5, 0.5, 1.0) - i.e. carries no bump.

    A material whose source authored no bump detail should not reference a
    `$bumpmap` at all; the VTF costs disk and the shader combo costs fill rate
    for a texture that says "flat" at every pixel.
    """
    rgb = np.clip(normal[:, :, :3], 0.0, 1.0)
    flat = np.array([0.5, 0.5, 1.0], dtype=np.float32)
    return bool(np.abs(rgb - flat).max() < tolerance)


def is_effectively_black(pixels: np.ndarray, threshold: int = 4) -> bool:
    """Whether a baked uint8 RGB buffer is black enough to not be worth writing.

    Threshold 4/255 tolerates DXT encoder noise. A vmat with ``g_flMetalness 0``
    bakes a pure-black envmask; writing it costs disk for a runtime contribution
    of exactly nothing.
    """
    return bool(pixels[:, :, :3].max() < threshold)
