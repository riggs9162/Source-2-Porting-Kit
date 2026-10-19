"""
Building one Source 1 material from a set of PBR maps.

This is the engine every materials tool drives. It takes a :class:`MaterialSource`
- paths and/or uniform constants for each PBR role - loads them, bakes them into
whichever channel layout the target shader wants, encodes the VTFs and writes the
VMT. The three tools above it differ only in how they *discover* the source: from
a Source 2 `.vmat`, from a folder of textures grouped by filename, or from eight
file pickers.

Two output modes:

``fake``
    Stock Source 1 shaders (`VertexLitGeneric` / `LightmappedGeneric`) with PBR
    faked through Phong, a cubemap and a rim term. Runs anywhere, needs no addon.
``exo``
    The ExoPBR screenspace shader, which does real PBR from a packed ARM map.
    Better output, but the shader has to be installed on the target.

Nothing here is emitted speculatively. A material whose source has no roughness
or metallic gets no Phong block and no `_phong.vtf`; a flat normal gets no
bumpmap; a mask that bakes to pure black is dropped along with the VMT parameter
that would have referenced it. Inventing a flat default and writing it out fakes
a response the source material never asked for, and costs disk to do it.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from s2kit.pipelines import pbr_maps, textures, vmt

LogFn = Callable[[str], None]

#: Output keys that accept an individual resolution cap.
OUTPUT_KEYS: tuple[str, ...] = ("color", "normal", "phong", "envmask", "selfillum")

GLOW_MODES: tuple[tuple[str, str], ...] = (
    ("selfillum", "$selfillum (classic, breaks with tint/translucency)"),
    ("emissiveblend", "Emissive Blend (recommended)"),
    ("emissiveblend_basecolor", "Emissive Blend from the base colour"),
)


class MaterialCancelled(RuntimeError):
    """Raised inside a build when the caller set the cancel event."""


class MaterialError(RuntimeError):
    """A material could not be built - missing inputs, bad data, encode failure."""


def _check(cancel: Optional[threading.Event]) -> None:
    if cancel is not None and cancel.is_set():
        raise MaterialCancelled()


# ----------------------------------------------------------------------
# Inputs
# ----------------------------------------------------------------------

#: A role's pixels: a file to decode, an already-decoded float32 array in
#: [0, 1], or nothing.
Pixels = Any


@dataclass
class MaterialSource:
    """Where each role's pixels come from, and the vmat scalars that go with them.

    A role can be a file path, an in-memory float32 array, a uniform constant, or
    absent. Arrays are how a packed ORM map reaches the builder: it is split into
    three channels in memory rather than written out as three temporary PNGs and
    read back. Constants exist because Source 2 vmats write
    `"TextureColor" "[1 1 1 0]"` when the artist used a solid colour instead of a
    map - materialising that literal as a tiny uniform image lets the rest of the
    pipeline treat it as an ordinary texture. Pixels always win over constants
    for the same role.
    """

    color: Pixels = None
    normal: Pixels = None
    ao: Pixels = None
    roughness: Pixels = None
    metallic: Pixels = None
    translucency: Pixels = None
    selfillum: Pixels = None
    tintmask: Pixels = None
    heightmap: Pixels = None

    color_constant: Optional[tuple[float, float, float, float]] = None
    ao_constant: Optional[tuple[float, float, float, float]] = None
    translucency_constant: Optional[tuple[float, float, float, float]] = None
    selfillum_constant: Optional[tuple[float, float, float, float]] = None
    roughness_constant: Optional[float] = None
    metallic_constant: Optional[float] = None

    #: Source 2 g_vSelfIllumTint / g_flSelfIllumBrightness.
    selfillum_tint: Optional[tuple[float, float, float]] = None
    selfillum_brightness: Optional[float] = None
    selfillum_flow_phase: Pixels = None
    selfillum_flow_waveform: Pixels = None
    selfillum_flow_amount: Optional[float] = None
    selfillum_flow_speed: Optional[float] = None
    selfillum_flow_frequency: Optional[float] = None
    selfillum_overall_scale: Optional[float] = None
    glass_tint: Optional[tuple[float, float, float]] = None
    glass_max_opacity: Optional[float] = None
    glass_fresnel_exponent: Optional[float] = None
    #: Source 2 g_flModelTintAmount - scales the tint mask before baking.
    model_tint_amount: Optional[float] = None

    def has(self, role: str) -> bool:
        """Whether this role has any signal at all - a path or a constant."""
        if getattr(self, role, None) is not None:
            return True
        return getattr(self, f"{role}_constant", None) is not None


@dataclass
class MaterialOptions:
    """Everything that changes what a build produces."""

    #: fake | exo
    mode: str = "fake"
    generate_vtf: bool = True
    generate_vmt: bool = True
    generate_mipmaps: bool = True
    texture_format: str = "DXT5"

    #: Cap the working resolution before any maths runs. 0 = native.
    clamp_size: int = 0
    #: Per-output caps keyed by :data:`OUTPUT_KEYS`; 0 or absent = uncapped.
    output_clamps: dict[str, int] = field(default_factory=dict)
    #: Fill a missing normal with a flat tangent normal instead of failing.
    synthesize_missing_maps: bool = False

    # -- Fake PBR --------------------------------------------------------
    branch: str = "gmod"
    shader: str = "VertexLitGeneric"
    envmap: str = "env_cubemap"
    surfaceprop: str = ""
    ao_strength: float = 0.7
    ao_bokeh_strength: float = 0.35
    #: How much of the AO to take back where the glow mask marks the surface as
    #: emissive - 1.0 removes it there entirely, 0.0 bakes AO as authored. Only
    #: bites when the material carries both an AO map and a self-illum mask.
    emissive_ao_relief: float = 1.0
    gloss_gamma: float = 2.0
    metal_diffuse_suppression: float = pbr_maps.METAL_DIFFUSE_SUPPRESSION
    envmask_gamma: float = 1.5
    phong_strength: float = 0.75
    phong_tint_mode: str = "selective"
    colored_metal_relief: float = pbr_maps.COLORED_METAL_RELIEF
    #: Ship a normal map at all. Off drops $bumpmap and $phong with it. That is
    #: the combination a prop_static needs: every branch before the
    #: CS:GO engine throws away VRAD's baked per-vertex lighting on a model whose
    #: material carries $bumpmap, $normalmap or $phong, and lights it from a
    #: single point instead.
    emit_normal: bool = True
    #: Bake fixed normal relief into the base colour when normal output is off.
    bake_normal_detail: bool = False
    normal_detail_strength: float = 0.25
    #: Green-channel handling: auto | flip | keep. Source 2 authors normals the
    #: other way up from Source 1, so the default flips - see
    #: :func:`~s2kit.pipelines.pbr_maps.should_invert_green`.
    normal_green: str = "auto"
    translucent: bool = False
    alphatest: bool = False
    tintable: bool = False
    glow_mode: str = "emissiveblend"

    # -- Exo PBR ---------------------------------------------------------
    emission_scale: float = 0.0
    parallax_scale: float = 0.0
    alpha_blend: bool = False

    @property
    def transparent(self) -> bool:
        return self.translucent or self.alphatest

    @property
    def wants_normal(self) -> bool:
        """Whether a missing normal map should fail this build.

        Exo PBR always does - its shader reads one. Fake PBR only does when the
        material is shipping a `$bumpmap`. The optional base-colour relief bake
        uses an authored normal when present without requiring one.
        """
        return self.mode != "fake" or self.emit_normal


@dataclass
class MaterialResult:
    """What a build produced, and what it decided not to produce."""

    name: str
    material_path: str
    output_dir: Path
    width: int = 0
    height: int = 0
    written: list[Path] = field(default_factory=list)
    vmt: Optional[vmt.VmtResult] = None
    stats: Optional[pbr_maps.MaterialStats] = None
    #: Outputs deliberately skipped, as (name, reason) - shown in the log.
    dropped: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def vtf_files(self) -> list[Path]:
        return [path for path in self.written if path.suffix.lower() == ".vtf"]

    @property
    def vmt_file(self) -> Optional[Path]:
        for path in self.written:
            if path.suffix.lower() == ".vmt":
                return path
        return None

    def summary(self) -> str:
        parts = [f"{len(self.vtf_files)} VTF"]
        if self.vmt_file:
            parts.append("VMT")
        if self.width:
            parts.append(f"{self.width}x{self.height}")
        return ", ".join(parts)


@dataclass
class LoadedMaps:
    """Decoded inputs at one shared working resolution."""

    color: np.ndarray
    normal: np.ndarray
    width: int
    height: int
    ao: Optional[np.ndarray] = None
    roughness: Optional[np.ndarray] = None
    metallic: Optional[np.ndarray] = None
    translucency: Optional[np.ndarray] = None
    selfillum: Optional[np.ndarray] = None
    tintmask: Optional[np.ndarray] = None
    heightmap: Optional[np.ndarray] = None


# ----------------------------------------------------------------------
# Resolution planning
# ----------------------------------------------------------------------

def _working_cap(source: MaterialSource, options: MaterialOptions) -> int:
    """The longest-edge cap to decode at, from the tool cap and the output caps.

    Everything computed is downscaled to its own output cap immediately before
    encoding, so running the maths above the largest of those caps buys nothing
    and costs the square of the ratio in both memory and time - a 4K source with
    2048 caps burns four times the RAM to produce comparable output.

    Returns 0 (native resolution) when overrides are off, or when any output this
    run will emit is individually uncapped, since that one would genuinely lose
    detail. Predicting the emit flags from input *presence* is safe: a path that
    later fails to load only makes the cap more generous, never tighter.
    """
    caps = options.output_clamps or {}
    output_cap = 0
    if caps:
        keys = ["color", "normal"]
        if source.has("roughness") or source.has("metallic"):
            keys.append("phong")
        if source.has("metallic"):
            keys.append("envmask")
        if source.has("selfillum"):
            keys.append("selfillum")
        values = [int(caps.get(key, 0) or 0) for key in keys]
        output_cap = 0 if any(value <= 0 for value in values) else max(values)

    bounds = [cap for cap in (output_cap, int(options.clamp_size or 0)) if cap > 0]
    return min(bounds) if bounds else 0


def _apply_output_cap(pixels: Optional[np.ndarray], key: str,
                      options: MaterialOptions,
                      log: LogFn) -> Optional[np.ndarray]:
    """Downscale one baked buffer to its per-output cap, logging when it bites."""
    if pixels is None:
        return pixels
    cap = int((options.output_clamps or {}).get(key, 0) or 0)
    if cap <= 0:
        return pixels
    height, width = pixels.shape[:2]
    if max(height, width) <= cap:
        return pixels
    clamped = textures.clamp_u8(pixels, cap)
    log(f"  {key}: {width}x{height} -> {clamped.shape[1]}x{clamped.shape[0]} "
        f"(cap {cap})")
    return clamped


# ----------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------

def load_maps(source: MaterialSource, options: MaterialOptions, *,
              log: LogFn = lambda _: None,
              cancel: Optional[threading.Event] = None) -> LoadedMaps:
    """Decode every role the source declares, at one shared working resolution.

    Colour is always required; normal output also requires a normal unless
    synthesis is enabled. A normal-free build can use an optional authored
    normal for base-colour relief, falling back to flat when absent. Other
    missing roles stay None rather than inventing an unauthored signal.
    """
    cap = _working_cap(source, options)
    if cap:
        log(f"Decoding at <={cap}px longest edge")

    def scalar(role: str) -> Optional[np.ndarray]:
        """One channel of a role, from an array, a file, or a constant."""
        _check(cancel)
        value = getattr(source, role, None)
        if isinstance(value, np.ndarray):
            data = value[:, :, 0] if value.ndim > 2 else value
            log(f"  {role}: in memory {data.shape[1]}x{data.shape[0]}")
            return textures.clamp_float(data, cap)
        if value is not None:
            data = textures.load_scalar(value, max_edge=cap)
            if data is not None:
                log(f"  {role}: {Path(value).name}")
                return data
            log(f"  {role}: could not read {Path(value).name}")
        constant = getattr(source, f"{role}_constant", None)
        if constant is None:
            return None
        first = constant[0] if isinstance(constant, (tuple, list)) else constant
        log(f"  {role}: uniform {float(first):.3f}")
        return textures.uniform_scalar(float(first))

    def rgba(role: str) -> Optional[np.ndarray]:
        """All four channels of a role, from an array, a file, or a constant."""
        _check(cancel)
        value = getattr(source, role, None)
        if isinstance(value, np.ndarray):
            log(f"  {role}: in memory {value.shape[1]}x{value.shape[0]}")
            return textures.clamp_float(textures.as_rgba(value), cap)
        if value is not None:
            data = textures.load_rgba(value, max_edge=cap)
            if data is not None:
                log(f"  {role}: {Path(value).name}")
                return data
            log(f"  {role}: could not read {Path(value).name}")
        constant = getattr(source, f"{role}_constant", None)
        if constant is None:
            return None
        log(f"  {role}: uniform "
            + " ".join(f"{float(c):.3f}" for c in constant))
        return textures.uniform_rgba(constant)

    color = rgba("color")
    if color is None:
        raise MaterialError("No colour map (a material cannot be built without one)")

    normal = rgba("normal")
    if normal is None:
        if options.wants_normal and not options.synthesize_missing_maps:
            raise MaterialError("No normal map (enable 'Synthesise missing maps' "
                                "to build without one)")
        normal = textures.flat_normal()
        log("  normal: synthesised flat (no bump)")

    ao = scalar("ao")
    roughness = scalar("roughness")
    metallic = scalar("metallic")
    translucency = scalar("translucency")
    if source.glass_tint is not None:
        color = color.copy()
        color[:, :, :3] *= np.asarray(source.glass_tint, dtype=np.float32)
    if source.glass_max_opacity is not None:
        opacity = float(np.clip(source.glass_max_opacity, 0.0, 1.0))
        translucency = (textures.uniform_scalar(opacity) if translucency is None
                        else np.maximum(translucency, opacity))
    heightmap = scalar("heightmap")
    selfillum = rgba("selfillum")

    tintmask = None
    if source.tintmask is not None or source.has("tintmask"):
        if options.transparent:
            # Transparency owns the basetexture alpha - $blendtintbybasealpha and
            # $translucent both read it, and Source 1 cannot honour both. The prop
            # still tints uniformly via render colour at runtime.
            log("  tintmask: dropped, the alpha channel belongs to transparency")
        else:
            tintmask = scalar("tintmask")

    # Work at the largest input's resolution so a high-res colour map is not
    # thrown away because the roughness beside it was authored at 512. `cap` is
    # already the tightest of the tool and per-output limits, so applying it here
    # is the same bound the decode used - it only bites on synthesised uniforms,
    # which arrive at 4x4 and get scaled up.
    present = [color, normal, ao, roughness, metallic, translucency,
               selfillum, tintmask, heightmap]
    height = max(item.shape[0] for item in present if item is not None)
    width = max(item.shape[1] for item in present if item is not None)
    if cap > 0 and max(height, width) > cap:
        scale = cap / float(max(height, width))
        height = max(1, int(round(height * scale)))
        width = max(1, int(round(width * scale)))

    return LoadedMaps(
        color=textures.fit(color, height, width),
        normal=textures.fit(normal, height, width),
        width=width, height=height,
        ao=textures.fit(ao, height, width),
        roughness=textures.fit(roughness, height, width),
        metallic=textures.fit(metallic, height, width),
        translucency=textures.fit(translucency, height, width),
        selfillum=textures.fit(selfillum, height, width),
        tintmask=textures.fit(tintmask, height, width),
        heightmap=textures.fit(heightmap, height, width),
    )


# ----------------------------------------------------------------------
# Building
# ----------------------------------------------------------------------

def build_material(source: MaterialSource, output_dir: Any, name: str,
                   material_path: str, options: MaterialOptions, *,
                   log: LogFn = lambda _: None,
                   cancel: Optional[threading.Event] = None) -> MaterialResult:
    """Build one material and write its files. Raises :class:`MaterialError`."""
    if options.mode == "exo":
        return _build_exo(source, Path(output_dir), name, material_path,
                          options, log, cancel)
    return _build_fake(source, Path(output_dir), name, material_path,
                       options, log, cancel)


def is_already_built(output_dir: Any, name: str,
                     options: MaterialOptions) -> bool:
    """Whether a previous run already produced this material.

    The VMT is the canonical marker when it is being generated - it is the file
    a user hand-edits afterwards, so it is the one worth not clobbering. With VMT
    generation off, fall back to the basetexture.
    """
    folder = Path(output_dir)
    if options.generate_vmt and (folder / f"{name}.vmt").exists():
        return True
    if options.generate_vtf:
        base = "_base.vtf" if options.mode == "exo" else "_color.vtf"
        if (folder / f"{name}{base}").exists():
            return True
    return False


def _encode(pixels: np.ndarray, target: Path, result: MaterialResult,
            options: MaterialOptions, log: LogFn, *,
            image_format: Optional[str] = None, flags: int = 0) -> None:
    textures.encode_vtf(pixels, target,
                        image_format=image_format or options.texture_format, flags=flags,
                        mipmaps=options.generate_mipmaps)
    result.written.append(target)
    log(f"  wrote {target.name}")


# -- Fake PBR ----------------------------------------------------------

def _build_fake(source: MaterialSource, output_dir: Path, name: str,
                material_path: str, options: MaterialOptions,
                log: LogFn, cancel: Optional[threading.Event]) -> MaterialResult:
    result = MaterialResult(name=name, material_path=material_path,
                            output_dir=output_dir)

    # Which optional paths the source actually justifies. "Real" means the
    # material supplied the data; a default does not count.
    emit_phong = source.has("roughness") or source.has("metallic")
    emit_envmap = source.has("metallic")

    # $phong shades against the bumpmap's normal, so a material shipped without
    # one has no Phong path to write. $envmap survives it: a cubemap reflection
    # needs no tangent normal, and it is not what costs a prop_static its baked
    # lighting.
    phong_drop = "" if emit_phong else "no roughness or metallic signal"
    if emit_phong and not options.emit_normal:
        emit_phong, phong_drop = False, "shipped without a normal map"

    # $phongalbedotint makes the engine multiply Phong by the basetexture at
    # runtime, so the mask has to compensate for it. Branches without the
    # parameter never do that multiply, so the compensation would be wrong.
    caps = vmt.capabilities(options.branch)
    tint_mode = options.phong_tint_mode if caps.get("phong_albedo_tint") else "off"
    if options.phong_tint_mode != "off" and tint_mode == "off":
        log(f"Branch '{options.branch}' has no $phongalbedotint; "
            f"Phong tint compensation off")

    # A VMT-only run is the common case when iterating on emission or glow
    # against an already-exported texture set. Skipping the bakes turns that from
    # seconds into milliseconds; the flags then come from input presence alone,
    # since the content-based drops below need buffers we are not building.
    compute = options.generate_vtf

    log("Loading inputs")
    maps = load_maps(source, options, log=log, cancel=cancel)
    result.width, result.height = maps.width, maps.height
    log(f"Working resolution {maps.width}x{maps.height}")

    result.stats = pbr_maps.material_stats(maps.roughness, maps.metallic,
                                           maps.height, maps.width)
    flat_glass = (source.glass_tint is not None
                  and pbr_maps.is_flat_normal(maps.normal))
    if flat_glass:
        emit_phong, phong_drop = False, "flat glass uses the separate reflection mask"
    emit_bumpmap = options.emit_normal and not (
        pbr_maps.is_flat_normal(maps.normal) and not emit_phong)
    if not emit_bumpmap:
        result.dropped.append(
            ("bumpmap", "the material is shipping without one"
             if not options.emit_normal else "the normal map is flat"))

    base = normal = phong = envmask = selfillum = None
    tintable = False
    bake_detail = False
    if options.bake_normal_detail:
        if options.emit_normal:
            detail_note = "Normal detail bake skipped: Ship a normal map is on"
        elif not compute:
            detail_note = "Normal detail bake requires VTF texture generation"
        elif options.normal_detail_strength <= 0.0:
            detail_note = "Normal detail bake skipped: strength is zero"
        elif not source.has("normal"):
            detail_note = "Normal detail bake skipped: no source normal map"
        elif pbr_maps.is_flat_normal(maps.normal):
            detail_note = "Normal detail bake skipped: normal map is flat or unavailable"
        else:
            bake_detail = True
            detail_note = ("Normal detail baked into base texture "
                           f"(strength {options.normal_detail_strength:.2f})")
        if not bake_detail:
            result.notes.append(detail_note)
        log(detail_note)

    if compute:
        # Every bake below reads the same AO, so the emissive relief is applied
        # once here rather than three times with three chances to disagree.
        ao = pbr_maps.relieve_ao(maps.ao, maps.selfillum, maps.height, maps.width,
                                 amount=options.emissive_ao_relief)
        if ao is not maps.ao:
            log(f"  AO relieved on the glow mask "
                f"({options.emissive_ao_relief:.2f})")

        _check(cancel)
        log("Baking basetexture")
        # A tintable material with no mask still tints - Source 2 defaults the
        # mask to white, and the basetexture alpha the engine falls back to is
        # opaque, which means the same thing. Dropping the tint here instead
        # would silently make an HLA colourable prop uncolourable.
        tintable = options.tintable
        if tintable and maps.tintmask is None:
            log("  no tint mask - the whole surface will take the render colour")
        base = pbr_maps.bake_basetexture(
            maps.color, ao, maps.metallic,
            ao_strength=options.ao_strength,
            ao_bokeh_strength=options.ao_bokeh_strength,
            metal_diffuse_suppression=options.metal_diffuse_suppression,
            colored_metal_relief=(options.colored_metal_relief
                                  if tint_mode != "off" else 0.0),
            translucency=maps.translucency,
            tintmask=maps.tintmask if tintable else None,
            tint_amount=source.model_tint_amount,
            normal_detail=maps.normal if bake_detail else None,
            normal_detail_strength=options.normal_detail_strength,
            invert_normal_green=pbr_maps.should_invert_green(options.normal_green,
                                                             source.normal),
            selfillum=maps.selfillum)

        _check(cancel)
        if emit_bumpmap:
            # Decided from the source filename, not the decoded pixels: a normal
            # map's green channel looks equally plausible either way round.
            flip_green = pbr_maps.should_invert_green(options.normal_green,
                                                      source.normal)
            log("Packing bumpmap" + (f" (Phong mask, tint {tint_mode})"
                                     if emit_phong else " (no Phong mask)"))
            log(f"  green channel: {'flipped for Source 1' if flip_green else 'kept as authored'}")
            normal = pbr_maps.bake_bumpmap(
                maps.normal, ao, maps.metallic, maps.roughness,
                invert_green=flip_green,
                include_phong=emit_phong,
                phong_strength=options.phong_strength,
                color=maps.color if tint_mode != "off" else None,
                tint_mode=tint_mode,
                metal_diffuse_suppression=options.metal_diffuse_suppression,
                colored_metal_relief=(options.colored_metal_relief
                                      if tint_mode != "off" else 0.0))

        _check(cancel)
        if emit_phong:
            log("Baking Phong exponent")
            phong = pbr_maps.bake_phong_exponent(
                maps.roughness, maps.metallic, maps.height, maps.width,
                gloss_gamma=options.gloss_gamma,
                phong_strength=options.phong_strength)
            # Roughness ~1 with metallic ~0 bakes to black: the runtime Phong
            # contribution would be nil, so drop the file and the VMT block.
            if pbr_maps.is_effectively_black(phong):
                phong, emit_phong = None, False
                result.dropped.append(("phong", "bakes to black"))
        else:
            result.dropped.append(("phong", phong_drop))

        _check(cancel)
        if emit_envmap:
            log("Baking envmap mask")
            envmask = pbr_maps.bake_envmap_mask(
                maps.color, ao, maps.metallic, maps.roughness,
                envmask_gamma=options.envmask_gamma)
            if pbr_maps.is_effectively_black(envmask):
                envmask, emit_envmap = None, False
                result.dropped.append(("envmask", "bakes to black"))
        else:
            result.dropped.append(("envmask", "no metallic signal"))

        if maps.selfillum is not None:
            selfillum = pbr_maps.bake_selfillum(maps.selfillum)
            if pbr_maps.is_effectively_black(selfillum):
                selfillum = None
                result.dropped.append(("selfillum", "the glow mask is black"))

        # The float32 inputs are dead weight from here on, and at 4K each one is
        # 64-256 MiB. Drop the whole record before encoding: the DXT compressor
        # and mipmap chain want the headroom, and a parallel batch multiplies
        # whatever stays alive by the worker count. Nothing below reads `maps` -
        # the resolution and the self-illum decision are already captured.
        maps = ao = None
    else:
        log("VTF generation off - writing the VMT only")
        tintable = options.tintable

    _check(cancel)
    if options.generate_vtf and base is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        _encode(_apply_output_cap(base, "color", options, log),
                output_dir / f"{name}_color.vtf", result, options, log,
                flags=textures.VTF_FLAG_SRGB)
        if normal is not None:
            _encode(_apply_output_cap(normal, "normal", options, log),
                    output_dir / f"{name}_normal.vtf", result, options, log,
                    flags=textures.VTF_FLAG_NORMAL)
        if phong is not None:
            _encode(_apply_output_cap(phong, "phong", options, log),
                    output_dir / f"{name}_phong.vtf", result, options, log)
        if envmask is not None:
            _encode(_apply_output_cap(envmask, "envmask", options, log),
                    output_dir / f"{name}_envmask.vtf", result, options, log)
        if selfillum is not None:
            _encode(_apply_output_cap(selfillum, "selfillum", options, log),
                    output_dir / f"{name}_selfillum.vtf", result, options, log,
                    flags=textures.VTF_FLAG_SRGB)
            if _has_selfillum_flow(source, options):
                _build_selfillum_flow(source, output_dir, name, result, options, log)

    if options.generate_vmt:
        _check(cancel)
        extra = _fake_extra_params(source, options, name, material_path,
                                   has_selfillum=(selfillum is not None
                                                  if compute else source.has("selfillum")))
        if tintable and extra.get("$selfillum") == "1":
            result.notes.append(
                "$selfillum breaks the $blendtintbybasealpha combo - switch the "
                "glow mode to Emissive Blend to keep the prop colourable")
        result.vmt = vmt.fake_pbr(
            name, material_path,
            shader=options.shader, branch=options.branch, envmap=options.envmap,
            stats=result.stats,
            emit_bumpmap=emit_bumpmap and (normal is not None or not compute),
            emit_phong=emit_phong, emit_envmap=emit_envmap,
            has_envmap_mask=envmask is not None or not compute,
            surfaceprop=options.surfaceprop, tintable=tintable,
            tint_mode_used=tint_mode, extra=extra)
        result.notes += result.vmt.notes
        if source.glass_max_opacity is not None:
            result.notes.append(
                "Source 2 glass Fresnel opacity is approximated by its maximum "
                "opacity; Source 1 VertexLitGeneric has no matching opacity curve")
        if _has_selfillum_flow(source, options):
            result.notes.append(
                "Source 2 self-illumination uses its authored phase, waveform, "
                "flow speed and tint through the native EmissiveBlend pass")
        result.written.append(result.vmt.write(output_dir / f"{name}.vmt"))
        log(f"  wrote {name}.vmt")

    for what, why in result.dropped:
        log(f"  skipped {what}: {why}")
    return result


def _fake_extra_params(source: MaterialSource, options: MaterialOptions,
                       name: str, material_path: str,
                       has_selfillum: bool) -> dict[str, str]:
    """Transparency and glow parameters, which sit outside the standard blocks."""
    extra: dict[str, str] = {}
    if options.translucent:
        extra["$translucent"] = "1"
    elif options.alphatest:
        extra["$alphatest"] = "1"
        extra["$alphatestreference"] = "0.5"
    if options.transparent:
        # Alpha-blended draw and the normal-alpha envmap mask cannot coexist.
        extra["$nocull"] = "0"
        extra["$normalmapalphaenvmapmask"] = "0"
    if has_selfillum:
        brightness = (source.selfillum_brightness
                      if source.selfillum_brightness is not None else 1.0)
        brightness *= (source.selfillum_overall_scale
                       if source.selfillum_overall_scale is not None else 1.0)
        extra.update(vmt.selfillum_params(
            f"{material_path}/{name}_selfillum",
            f"{material_path}/{name}_color",
            mode=options.glow_mode,
            tint=source.selfillum_tint or (1.0, 1.0, 1.0),
            brightness=brightness))
        if _has_selfillum_flow(source, options):
            speed = (source.selfillum_flow_speed
                     if source.selfillum_flow_speed is not None else 1.0)
            extra["$EmissiveBlendBaseTexture"] = f"{material_path}/{name}_selfillum"
            extra["$EmissiveBlendTexture"] = f"{material_path}/{name}_waveform"
            extra["$EmissiveBlendFlowTexture"] = f"{material_path}/{name}_flow"
            extra["$EmissiveBlendScrollVector"] = f"[{speed:.6g} 0]"
    return extra


def _has_selfillum_flow(source: MaterialSource, options: MaterialOptions) -> bool:
    """Whether the native emissive pass can sample the authored phase waveform."""
    return (options.glow_mode.startswith("emissiveblend")
            and source.has("selfillum_flow_phase")
            and source.has("selfillum_flow_waveform"))


def _build_selfillum_flow(source: MaterialSource, output_dir: Path, name: str,
                         result: MaterialResult, options: MaterialOptions,
                         log: LogFn) -> None:
    """Encode scalar phase as waveform UV and retain a linear emission waveform.

    Source 1's emissive pass samples its emission texture at flow.xy + time *
    scroll. A one-dimensional waveform therefore uses the phase in R and a
    constant G. Flow is uncompressed linear data; the waveform is sRGB-encoded
    because the native emissive shader always enables sRGB on that sampler.
    """
    phase_source = source.selfillum_flow_phase
    waveform_source = source.selfillum_flow_waveform
    phase = (phase_source if isinstance(phase_source, np.ndarray)
             else textures.load_scalar(phase_source, max_edge=options.clamp_size))
    waveform = (waveform_source if isinstance(waveform_source, np.ndarray)
                else textures.load_rgba(waveform_source))
    if phase is None or waveform is None:
        raise MaterialError("Could not read the self-illumination phase or waveform")
    if phase.ndim == 3:
        phase = phase[:, :, 0]
    frequency = (source.selfillum_flow_frequency
                 if source.selfillum_flow_frequency is not None else 1.0)
    flow = np.zeros((*phase.shape, 4), dtype=np.float32)
    flow[:, :, 0] = phase if frequency == 1.0 else np.mod(phase * frequency, 1.0)
    flow[:, :, 1] = 0.5
    flow[:, :, 3] = 1.0
    amount = (source.selfillum_flow_amount
              if source.selfillum_flow_amount is not None else 1.0)
    waveform = textures.as_rgba(waveform).copy()
    waveform[:, :, :3] = textures.linear_to_srgb(
        (1.0 - np.clip(amount, 0.0, 1.0))
        + np.clip(amount, 0.0, 1.0) * waveform[:, :, :3])
    waveform[:, :, 3] = 1.0
    if waveform.shape[0] < 4:
        waveform = np.repeat(waveform[:1], 4, axis=0)
    _encode(_apply_output_cap(textures.to_uint8(flow), "selfillum", options, log),
            output_dir / f"{name}_flow.vtf", result, options, log,
            image_format="RGBA8888")
    _encode(textures.to_uint8(waveform), output_dir / f"{name}_waveform.vtf",
            result, options, log, image_format="RGBA8888")


# -- Exo PBR -----------------------------------------------------------

def _build_exo(source: MaterialSource, output_dir: Path, name: str,
               material_path: str, options: MaterialOptions,
               log: LogFn, cancel: Optional[threading.Event]) -> MaterialResult:
    result = MaterialResult(name=name, material_path=material_path,
                            output_dir=output_dir)

    log("Loading inputs")
    maps = load_maps(source, options, log=log, cancel=cancel)
    result.width, result.height = maps.width, maps.height
    log(f"Working resolution {maps.width}x{maps.height}")
    result.stats = pbr_maps.material_stats(maps.roughness, maps.metallic,
                                           maps.height, maps.width)

    has_emissive = maps.selfillum is not None
    if options.generate_vtf:
        _check(cancel)
        output_dir.mkdir(parents=True, exist_ok=True)
        log("Packing base, ARM and normal")
        _encode(_apply_output_cap(
            pbr_maps.bake_exo_base(maps.color, maps.translucency,
                                   maps.height, maps.width),
            "color", options, log),
            output_dir / f"{name}_base.vtf", result, options, log)
        _check(cancel)
        _encode(_apply_output_cap(
            pbr_maps.bake_arm(maps.ao, maps.roughness, maps.metallic,
                              maps.heightmap, maps.height, maps.width),
            "color", options, log),
            output_dir / f"{name}_arm.vtf", result, options, log)
        _check(cancel)
        _encode(_apply_output_cap(
            pbr_maps.bake_exo_normal(maps.normal, maps.height, maps.width),
            "normal", options, log),
            output_dir / f"{name}_normal.vtf", result, options, log)
        if has_emissive:
            _check(cancel)
            _encode(_apply_output_cap(
                pbr_maps.bake_selfillum(maps.selfillum), "selfillum", options, log),
                output_dir / f"{name}_emissive.vtf", result, options, log)

    if options.generate_vmt:
        # An emissive map with no emission scale would encode a texture the
        # shader never samples, so default it on; likewise parallax when the
        # source carried a height map.
        emission = options.emission_scale
        if has_emissive and not emission:
            emission = 1.0
            log("  $emissionscale 1.0 (emissive map present)")
        parallax = options.parallax_scale
        if maps.heightmap is not None and not parallax:
            parallax = 0.03
            log("  $parallaxscale 0.03 (height map present)")

        result.vmt = vmt.exo_pbr(
            name, material_path,
            emission_scale=emission, parallax_scale=parallax,
            alpha_blend=options.alpha_blend or options.transparent,
            has_emissive=has_emissive)
        result.written.append(result.vmt.write(output_dir / f"{name}.vmt"))
        log(f"  wrote {name}.vmt")

    return result


# ----------------------------------------------------------------------
# Reversing a build
# ----------------------------------------------------------------------

@dataclass
class ExtractOptions:
    """How to turn a built Fake PBR material back into editable PBR maps."""

    gloss_gamma: float = 2.0
    phong_strength: float = 0.75
    metal_diffuse_suppression: float = pbr_maps.METAL_DIFFUSE_SUPPRESSION
    #: Undo the metal darkening in the basetexture.
    recover_albedo: bool = True
    #: Flip the normal's green channel back on the way out. On by default so an
    #: extract feeds straight back into a converter: the converter flips Source 2
    #: -> Source 1, so the extractor has to flip Source 1 -> Source 2 or the
    #: round trip would leave every surface inverted.
    invert_green: bool = True
    #: Write an `_opacity.png` when the basetexture alpha is not fully opaque.
    write_opacity: bool = True
    clamp_size: int = 0
    overwrite: bool = False


def extract_material(sources: dict[str, Path], output_dir: Any, name: str,
                     options: ExtractOptions, *,
                     log: LogFn = lambda _: None) -> list[Path]:
    """Decode a built material's VTFs back into PBR PNGs.

    Exactly inverts what the Phong exponent bake did, which recovers roughness
    and metallic cleanly. Colour is the lossy direction: AO was multiplied into
    the same pixels the albedo lives in and there is no second equation to
    separate them, so what comes out is the AO-baked albedo with the metal
    darkening removed. Good enough to re-author from; not the original.

    The bumpmap alpha (the Phong mask) is skipped for the same reason.
    """
    folder = Path(output_dir)
    folder.mkdir(parents=True, exist_ok=True)

    targets = [folder / f"{name}_color.png", folder / f"{name}_normal.png"]
    if "phong" in sources:
        targets += [folder / f"{name}_roughness.png",
                    folder / f"{name}_metallic.png"]
    if not options.overwrite and all(target.exists() for target in targets):
        log(f"{name}: outputs exist")
        return []

    def decode(role: str) -> Optional[np.ndarray]:
        if role not in sources:
            return None
        pixels, _info = textures.decode_vtf(sources[role])
        return textures.clamp_u8(pixels, options.clamp_size)

    color = decode("color")
    normal = decode("normal")
    phong = decode("phong")

    written: list[Path] = []
    metallic: Optional[np.ndarray] = None

    if phong is not None:
        roughness = pbr_maps.unbake_roughness(
            phong[:, :, 0].astype(np.float32) / 255.0,
            phong_strength=options.phong_strength,
            gloss_gamma=options.gloss_gamma)
        metallic = np.clip(phong[:, :, 1].astype(np.float32) / 255.0, 0.0, 1.0)
        written.append(textures.save_scalar_png(
            roughness, folder / f"{name}_roughness.png"))
        written.append(textures.save_scalar_png(
            metallic, folder / f"{name}_metallic.png"))

    if normal is not None:
        rgb = normal[:, :, :3].copy()
        if options.invert_green:
            rgb[:, :, 1] = 255 - rgb[:, :, 1]
        written.append(textures.save_png(rgb, folder / f"{name}_normal.png"))

    if color is not None:
        if options.recover_albedo and metallic is not None:
            albedo = pbr_maps.unbake_albedo(
                color[:, :, :3].astype(np.float32) / 255.0, metallic,
                metal_diffuse_suppression=options.metal_diffuse_suppression)
            rgb = (albedo * 255.0 + 0.5).astype(np.uint8)
        else:
            rgb = color[:, :, :3]
        written.append(textures.save_png(rgb, folder / f"{name}_color.png"))
        alpha = color[:, :, 3]
        if options.write_opacity and alpha.min() < 255:
            written.append(textures.save_scalar_png(
                alpha.astype(np.float32) / 255.0, folder / f"{name}_opacity.png"))

    log(f"{name}: {len(written)} map(s)")
    return written
