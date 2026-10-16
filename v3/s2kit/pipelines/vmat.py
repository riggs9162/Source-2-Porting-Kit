"""
Reading Source 2 `.vmat` materials.

A vmat is the material half of what VRF Batch Export produces. It is plain text
KeyValues naming a shader, a set of feature flags (`F_TRANSLUCENT`,
`F_SELF_ILLUM`, `F_TINT_MASK`), scalar constants (`g_flRoughness`,
`g_flMetalness`) and the textures for each PBR role.

Three things make parsing it more than a regex sweep:

* **Texture keys are not stable.** Albedo is `TextureColor` in one shader and
  `TextureDiffuse` or `TextureBaseColor` in another, so every role needs an alias
  list.
* **A texture value is sometimes not a texture.** When the artist used a solid
  colour, VRF writes `"TextureColor" "[1.0 1.0 1.0 0.0]"`. That literal is the
  material's actual albedo and has to be carried through as a constant, not
  reported as a missing file.
* **The paths rarely resolve.** They are game-relative
  (`models/props/foo_color.png`) while the exported PNG sits next to the vmat.
  Resolution therefore tries the texture root, then the vmat's own folder, then
  falls back to matching siblings by filename role tokens.

The parser produces a :class:`VmatEntry`; `to_source` turns that into the
:class:`~s2kit.pipelines.materials.MaterialSource` the builder consumes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from s2kit.pipelines import textures
from s2kit.pipelines.materials import MaterialSource

#: Source 2 texture keys per role, most specific first.
KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "color": ("TextureColor", "TextureDiffuse", "TextureAlbedo",
              "TextureBaseColor", "TextureBasecolor", "TextureBase", "TextureColorA"),
    "normal": ("TextureNormal", "TextureNormalMap", "TextureNormalA"),
    "ao": ("TextureAmbientOcclusion", "TextureAO", "TextureOcclusion", "TextureAmbientOcclusionA"),
    "roughness": ("TextureRoughness", "TextureRough", "TextureRoughnessA"),
    "metallic": ("TextureMetalness", "TextureMetallic", "TextureMetal", "TextureMetalnessA"),
    "selfillum": ("TextureSelfIllumMask", "TextureSelfIllum", "TextureEmissive",
                  "TextureEmission", "TextureGlow"),
    "selfillum_flow_phase": ("TextureSelfIllumFlowPhase",),
    "selfillum_flow_waveform": ("TextureSelfIllumFlowWaveform",),
    "translucency": ("TextureTranslucency", "TextureOpacity", "TextureAlpha"),
    "tintmask": ("TextureTintMask",),
    "heightmap": ("TextureHeight", "TextureDisplacement"),
}

#: Filename tokens used when falling back to sibling auto-detect. Includes the
#: `g_t*` engine-internal names, which surface in VRF's compiled-texture
#: filenames as suffixes like `_g_tnormal_<hash>`.
ROLE_TOKENS: dict[str, tuple[str, ...]] = {
    "color": ("color", "albedo", "basecolor", "base_color", "basecolour",
              "base_colour", "diffuse", "base", "col", "bc", "diff", "d",
              "tcolor", "tdiffuse", "talbedo", "tbasecolor"),
    "normal": ("normal_opengl", "normal", "normalmap", "normal_map", "nrm",
               "normals", "normalgl", "normal_dx", "nrml", "nor", "n",
               "tnormal", "tnormalmap"),
    "ao": ("ao", "ambientocclusion", "ambient_occlusion", "occlusion", "occ",
           "aoc", "mixed_ao", "tao", "tambientocclusion", "tocclusion"),
    "roughness": ("roughness", "rough", "rgh", "r", "rghness", "troughness",
                  "trough"),
    "metallic": ("metallic", "metal", "metalness", "mtl", "m", "tmetalness",
                 "tmetal", "tmetallic"),
    "selfillum": ("emissive", "emit", "emission", "selfillum", "self_illum",
                  "illum", "illumin", "glow", "temission", "temissive",
                  "tselfillum"),
    "translucency": ("trans", "translucency", "translucent", "opacity", "alpha",
                     "opac", "ttranslucency", "topacity"),
    # "mask" alone is deliberately generic - Half-Life: Alyx names tint masks
    # `<name>_mask.png` - so sibling detection for this role only runs when the
    # vmat actually declares F_TINT_MASK.
    "tintmask": ("tintmask", "tint_mask", "tint", "mask", "ttintmask"),
    "heightmap": ("height", "displacement", "disp", "heightmap", "parallax"),
}

#: Roles stored as RGBA literals. Roughness and metallic literals use their
#: dedicated scalar fields, taking the first component of the uniform texture.
CONSTANT_ROLES = ("color", "ao", "selfillum", "translucency")

#: Shaders that render transparent even without an explicit blend flag.
TRANSLUCENT_SHADERS = ("vr_glass.vfx", "vr_unlit.vfx", "vr_monitor.vfx")

_KEY_RE = re.compile(r'"(?P<key>Texture[A-Za-z0-9_]+|GlassTintColor)"\s+"(?P<value>[^"]+)"')
_SHADER_RE = re.compile(r'"shader"\s+"(?P<value>[^"]+)"', re.IGNORECASE)
_FLAG_RE = re.compile(r'"(?P<key>F_[A-Z_]+)"\s+"(?P<value>[01])"')
_SCALAR_RE = re.compile(r'"(?P<key>g_fl[A-Za-z0-9_]+)"\s+"(?P<value>-?[0-9.]+)"')
_VECTOR_RE = re.compile(r'"(?P<key>g_v[A-Za-z0-9_]+)"\s+"(?P<value>\[[^"]+\])"')


@dataclass
class VmatEntry:
    """One parsed vmat: resolved textures, constants and rendering intent."""

    path: Path
    name: str
    rel_dir: Path
    shader: str = ""

    resolved: dict[str, Path] = field(default_factory=dict)
    constants: dict[str, tuple[float, float, float, float]] = field(default_factory=dict)
    #: How each role was found ("vmat", "sibling", "literal") - shown in the table.
    sources: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    translucent: bool = False
    alphatest: bool = False
    roughness_constant: Optional[float] = None
    metallic_constant: Optional[float] = None

    selfillum: bool = False
    selfillum_tint: Optional[tuple[float, float, float]] = None
    selfillum_brightness: Optional[float] = None
    selfillum_scale: Optional[float] = None
    selfillum_flow_amount: Optional[float] = None
    selfillum_flow_speed: Optional[float] = None
    selfillum_flow_frequency: Optional[float] = None
    selfillum_overall_scale: Optional[float] = None
    glass_tint: Optional[tuple[float, float, float]] = None
    glass_max_opacity: Optional[float] = None
    glass_fresnel_exponent: Optional[float] = None

    tintable: bool = False
    model_tint_amount: Optional[float] = None

    def has(self, role: str) -> bool:
        return role in self.resolved or role in self.constants

    @property
    def buildable(self) -> bool:
        """Whether a colour map (or literal) exists - the one hard requirement."""
        return self.has("color")

    def to_source(self, *, include_selfillum: bool = True,
                  include_tint: bool = True) -> MaterialSource:
        """Bridge to the material builder's input record.

        `include_selfillum` gates the glow mask on `self.selfillum`: a Source 2
        emission texture does not always mean Source 1 self-illumination is the
        right call, so a mask found without the flag is dropped rather than
        silently lighting the prop up.

        Alyx's vr_complex feature metadata defines brightness in exposure stops:
        pow(2, brightness) * scale * SrgbGammaToLinear(tint). Other shader families
        keep their existing linear brightness semantics.
        """
        glow = include_selfillum and self.selfillum
        tint = include_tint and self.tintable
        glow_brightness = self.selfillum_brightness
        glow_tint = self.selfillum_tint
        if self.shader == "vr_complex.vfx":
            glow_brightness = 2.0 ** (self.selfillum_brightness or 0.0)
            glow_brightness *= (self.selfillum_scale
                                if self.selfillum_scale is not None else 1.0)
            if glow_tint is not None:
                glow_tint = tuple(component / 12.92 if component <= 0.04045
                                  else ((component + 0.055) / 1.055) ** 2.4
                                  for component in glow_tint)

        return MaterialSource(
            color=self.resolved.get("color"),
            normal=self.resolved.get("normal"),
            ao=self.resolved.get("ao"),
            roughness=self.resolved.get("roughness"),
            metallic=self.resolved.get("metallic"),
            translucency=self.resolved.get("translucency"),
            selfillum=self.resolved.get("selfillum") if glow else None,
            tintmask=self.resolved.get("tintmask") if tint else None,
            heightmap=self.resolved.get("heightmap"),
            color_constant=self.constants.get("color"),
            ao_constant=self.constants.get("ao"),
            translucency_constant=self.constants.get("translucency"),
            selfillum_constant=(self.constants.get("selfillum") if glow else None),
            roughness_constant=self.roughness_constant,
            metallic_constant=self.metallic_constant,
            selfillum_tint=glow_tint,
            selfillum_brightness=glow_brightness,
            selfillum_flow_phase=self.resolved.get("selfillum_flow_phase") if glow else None,
            selfillum_flow_waveform=self.resolved.get("selfillum_flow_waveform") if glow else None,
            selfillum_flow_amount=self.selfillum_flow_amount,
            selfillum_flow_speed=self.selfillum_flow_speed,
            selfillum_flow_frequency=self.selfillum_flow_frequency,
            selfillum_overall_scale=self.selfillum_overall_scale,
            glass_tint=self.glass_tint,
            glass_max_opacity=self.glass_max_opacity,
            glass_fresnel_exponent=self.glass_fresnel_exponent,
            model_tint_amount=self.model_tint_amount,
        )


class VmatParser:
    """Parses vmat text and resolves its texture references to real files."""

    def __init__(self, texture_root: Optional[Path] = None):
        #: Root that game-relative texture paths are joined against.
        self.texture_root = Path(texture_root) if texture_root else None

    # -- helpers -------------------------------------------------------

    @staticmethod
    def _tokens(stem: str) -> list[str]:
        return [token.lower() for token in re.split(r"[\s._-]+", stem) if token]

    @staticmethod
    def parse_rgba(raw: str) -> Optional[tuple[float, float, float, float]]:
        """Parse a bracketed literal like `[1.0 1.0 1.0 0.0]` into RGBA.

        Three-component literals are padded with alpha 1.0; anything past four
        components is dropped.
        """
        inner = raw.strip().lstrip("[").rstrip("]")
        parts = [part for part in inner.replace(",", " ").split() if part]
        if len(parts) < 3:
            return None
        try:
            values = [float(part) for part in parts[:4]]
        except ValueError:
            return None
        while len(values) < 4:
            values.append(1.0)
        return (values[0], values[1], values[2], values[3])

    def _identity_tokens(self, vmat_path: Path) -> list[list[str]]:
        """The token prefixes a sibling texture of this vmat may start with.

        The vmat's own stem is the identity, kept whole: names like
        `combine_metal_base` legitimately use "metal" and "base" as descriptors,
        and stripping them leaves "combine", which would match every other
        combine_* material in the folder.

        A vmat may also *be* named after a role - `railing_color.vmat` next to
        `railing_normal.png` - so a second identity with the trailing role tokens
        removed is offered as a fallback.
        """
        every = {token for tokens in ROLE_TOKENS.values() for token in tokens}
        tokens = self._tokens(vmat_path.stem)
        identities = [tokens]
        trimmed = list(tokens)
        while trimmed and trimmed[-1] in every:
            trimmed.pop()
        if trimmed and trimmed != tokens:
            identities.append(trimmed)
        return identities

    def _sibling_for(self, vmat_path: Path, role: str) -> Optional[Path]:
        """Find a texture beside the vmat whose filename claims `role`.

        A candidate qualifies when its tokens *start with* the vmat's identity
        and the remaining tokens name the role: `glow_sign.vmat` accepts
        `glow_sign_selfillum.png` because the leftover token is a self-illum
        word. Matching on shared tokens instead would pull in textures from any
        material that happened to use the same descriptor.

        A tie between two equally good candidates returns nothing. Picking one
        arbitrarily would silently bake the wrong texture into the material,
        which is worse than reporting the role as missing.
        """
        role_tokens = set(ROLE_TOKENS.get(role, ()))
        if not role_tokens:
            return None
        identities = self._identity_tokens(vmat_path)

        try:
            candidates = list(vmat_path.parent.iterdir())
        except OSError:
            return None

        scored: list[tuple[float, Path]] = []
        for candidate in candidates:
            if not candidate.is_file():
                continue
            if candidate.suffix.lower() not in textures.IMAGE_EXTENSIONS:
                continue

            tokens = self._tokens(candidate.stem)
            best: Optional[float] = None
            for index, identity in enumerate(identities):
                if len(tokens) < len(identity) or tokens[:len(identity)] != identity:
                    continue
                extra = tokens[len(identity):]
                if not role_tokens.intersection(extra):
                    continue
                # A longer identity is a more specific match, the untrimmed one
                # beats the fallback, and fewer leftover tokens is tighter.
                score = 100.0 + len(identity) * 5.0 - index * 10.0 - len(extra)
                best = score if best is None else max(best, score)
            if best is not None:
                scored.append((best, candidate))

        if not scored:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)
        top = scored[0][0]
        if sum(1 for score, _ in scored if abs(score - top) < 1e-6) > 1:
            return None
        return scored[0][1]

    def _resolve(self, raw: Optional[str], vmat_path: Path,
                 warnings: list[str]) -> Optional[Path]:
        """Resolve authored paths or an unambiguous decoded VRF hash-name copy."""
        if not raw:
            return None
        value = raw.strip()
        # A bracketed value is a constant, not a path - handled by the caller.
        if value.startswith(("[", "{")):
            return None

        requested = Path(value.replace("\\", "/"))
        candidate = requested
        if not candidate.is_absolute() and self.texture_root is not None:
            candidate = self.texture_root / candidate
        if candidate.is_file():
            return candidate
        # VRF writes game-relative paths while the exported PNG usually sits
        # next to the vmat. The basename was chosen deliberately, so try it.
        local = vmat_path.parent / requested.name
        if local.is_file():
            return local
        directories = {candidate.parent, local.parent}
        if self.texture_root is not None and not requested.is_absolute():
            hoisted = Path(*(part for part in requested.parts
                             if part.lower() != "materials"))
            directories.add((self.texture_root / hoisted).parent)
        pattern = re.compile(
            re.escape(requested.stem + "_" + requested.suffix.lstrip("."))
            + r"_[0-9a-f]{8}", re.IGNORECASE)
        matches: set[Path] = set()
        for directory in directories:
            try:
                for image in directory.iterdir():
                    if (image.is_file()
                            and image.suffix.lower() in textures.IMAGE_EXTENSIONS
                            and pattern.fullmatch(image.stem)):
                        matches.add(image.resolve())
            except OSError:
                continue
        if len(matches) == 1:
            return next(iter(matches))
        if matches:
            warnings.append(f"ambiguous: {raw} has {len(matches)} decoded hash-name copies")
            return None
        warnings.append(f"missing: {raw}")
        return None

    # -- parsing -------------------------------------------------------

    def parse(self, vmat_path: Any, root: Optional[Path] = None) -> VmatEntry:
        """Parse one vmat file into a :class:`VmatEntry`."""
        path = Path(vmat_path)
        text = path.read_text(encoding="utf-8", errors="ignore")

        raw_keys: dict[str, str] = {}
        for key, value in _KEY_RE.findall(text):
            raw_keys.setdefault(key, value)
        flags = dict(_FLAG_RE.findall(text))
        scalars = dict(_SCALAR_RE.findall(text))
        vectors = dict(_VECTOR_RE.findall(text))

        def scalar(key: str) -> Optional[float]:
            try:
                return float(scalars[key])
            except (KeyError, ValueError):
                return None

        def vec3(key: str) -> Optional[tuple[float, float, float]]:
            rgba = self.parse_rgba(vectors[key]) if key in vectors else None
            return rgba[:3] if rgba else None

        shader_match = _SHADER_RE.search(text)
        shader = shader_match.group("value").lower() if shader_match else ""

        # Source 2 has many blend flags (F_TRANSLUCENT, F_ADDITIVE_BLEND,
        # F_ADDITIVE_BLEND_OVER_ALPHA, F_BLEND_MODE...). Any set flag whose name
        # implies blending counts.
        translucent = (
            any(value == "1" and ("TRANSLUCENT" in key or ("BLEND" in key and key != "F_BLEND"))
                for key, value in flags.items())
            or any(name in shader for name in TRANSLUCENT_SHADERS))
        alphatest = flags.get("F_ALPHA_TEST") == "1"

        entry = VmatEntry(
            path=path,
            name=path.stem,
            rel_dir=_relative_dir(path, root),
            shader=shader,
            translucent=translucent,
            alphatest=alphatest,
            roughness_constant=scalar("g_flRoughness"),
            metallic_constant=scalar("g_flMetalness"),
            selfillum_tint=vec3("g_vSelfIllumTint"),
            selfillum_brightness=scalar("g_flSelfIllumBrightness"),
            selfillum_scale=scalar("g_flSelfIllumScale"),
            selfillum_flow_amount=scalar("g_flSelfIllumFlowAmount"),
            selfillum_flow_speed=scalar("g_flSelfIllumFlowSpeed"),
            selfillum_flow_frequency=scalar("g_flSelfIllumFlowFrequency"),
            selfillum_overall_scale=scalar("g_flSelfIllumOverallScale"),
            glass_max_opacity=scalar("g_flGlassFresnelMaxOpacity"),
            glass_fresnel_exponent=scalar("g_flGlassFresnelExponent"),
            model_tint_amount=scalar("g_flModelTintAmount"),
        )
        if shader == "vr_glass.vfx":
            glass_tint = self.parse_rgba(raw_keys.get("GlassTintColor", ""))
            entry.glass_tint = glass_tint[:3] if glass_tint else None
        if "TextureColorA" in raw_keys:
            if entry.roughness_constant is None:
                entry.roughness_constant = scalar("g_flRoughnessA")
            if entry.metallic_constant is None:
                entry.metallic_constant = scalar("g_flMetalnessA")
        if "TextureColorB" in raw_keys:
            entry.warnings.append("adapted: retained the authored base material layer; secondary blend/triplanar layer and its dynamic transition are unsupported by the target shader")

        for role, aliases in KEY_ALIASES.items():
            raw = next((raw_keys[key] for key in aliases if key in raw_keys), None)
            is_literal = bool(raw) and raw.strip().startswith(("[", "{"))
            if is_literal:
                constant = self.parse_rgba(raw)
                if constant is not None:
                    if role in CONSTANT_ROLES:
                        entry.constants[role] = constant
                        entry.sources[role] = "literal"
                    elif role in ("roughness", "metallic"):
                        setattr(entry, f"{role}_constant", constant[0])
                        entry.sources[role] = "literal"
                continue
            warning_count = len(entry.warnings)
            resolved = self._resolve(raw, path, entry.warnings)
            ambiguous = any(warning.startswith("ambiguous: ")
                            for warning in entry.warnings[warning_count:])

            if resolved is not None:
                entry.sources[role] = "vmat"
            else:
                # The tintmask role tokens include the generic "mask", so its
                # sibling search only runs when the vmat asked for a tint mask.
                allow_siblings = (not ambiguous and
                                  (role != "tintmask" or flags.get("F_TINT_MASK") == "1") and
                                  (role != "translucency" or raw is not None
                                   or entry.translucent or entry.alphatest))
                sibling = self._sibling_for(path, role) if allow_siblings else None
                if sibling is not None:
                    resolved = sibling
                    entry.sources[role] = "sibling"
                    if raw and not is_literal:
                        entry.warnings.append(
                            f"using sibling {sibling.name} for {role}")

            if resolved is not None:
                entry.resolved[role] = resolved

        self._derive_intent(entry, flags)
        return entry

    @staticmethod
    def _derive_intent(entry: VmatEntry, flags: dict[str, str]) -> None:
        """Fill in the transparency / glow / tint decisions the flags imply."""
        if entry.translucent or entry.alphatest:
            if not entry.has("translucency") and entry.glass_max_opacity is None:
                entry.warnings.append(
                    "transparent but no opacity map; the colour alpha will be used")
        elif "translucency" in entry.resolved:
            # An opacity texture with no blend flag: the texture's existence is
            # the authored signal. Without flipping the flag the alpha gets baked
            # into the basetexture and then silently ignored at runtime.
            entry.translucent = True

        entry.selfillum = flags.get("F_SELF_ILLUM") == "1" or entry.has("selfillum")
        if entry.selfillum and not entry.has("selfillum"):
            entry.warnings.append("F_SELF_ILLUM set but no glow mask found")

        # g_flModelTintAmount 0 opts a material out even when the flag is set;
        # Source 2 defaults the amount to 1 when the key is absent entirely.
        entry.tintable = (flags.get("F_TINT_MASK") == "1"
                          or "tintmask" in entry.resolved)
        if entry.model_tint_amount is not None and entry.model_tint_amount <= 0.0:
            entry.tintable = False
        if entry.tintable and "tintmask" not in entry.resolved:
            entry.warnings.append(
                "F_TINT_MASK set but no mask found; the whole surface will tint")

        # Alyx's "the entity tint *is* the colour" skins author TextureColor as a
        # literal, and that literal is what Source 2 compiles into g_tColor - so
        # it must beat sibling auto-detect. The sibling colour map belongs to the
        # non-tintable skin, and using it renders the ported skin as a duplicate
        # of skin 0 with muddy multiply-colouring instead of a clean tintable base.
        if entry.tintable and entry.sources.get("color") == "sibling":
            entry.warnings.append(
                "tintable skin using a sibling colour map - check it is not "
                "the untinted variant")


# ----------------------------------------------------------------------
# Scanning
# ----------------------------------------------------------------------

def find_vmats(root: Any, recursive: bool = True) -> list[Path]:
    """Every `.vmat` under `root`, sorted."""
    folder = Path(root)
    if not folder.is_dir():
        return []
    pattern = "**/*.vmat" if recursive else "*.vmat"
    return sorted(path for path in folder.glob(pattern) if path.is_file())


def parse_all(paths: Iterable[Any], *, root: Optional[Path] = None,
              texture_root: Optional[Path] = None) -> list[VmatEntry]:
    """Parse a batch of vmats, skipping the unreadable ones."""
    parser = VmatParser(texture_root)
    entries: list[VmatEntry] = []
    for path in paths:
        try:
            entries.append(parser.parse(path, root))
        except OSError:
            continue
    return entries


def _relative_dir(path: Path, root: Optional[Path]) -> Path:
    if root is None:
        return Path()
    try:
        return path.parent.relative_to(root)
    except ValueError:
        return Path()


def resolve_transparency(entry: VmatEntry, mode: str) -> tuple[bool, bool]:
    """Apply a transparency override to a parsed entry.

    `auto` keeps whatever the flags said. The forced modes only apply to
    materials that already showed *some* transparency intent - adding it to a
    material authored opaque would reinterpret its basetexture alpha, which in a
    tintable material is the tint mask, not an opacity channel. `opaque` strips
    it regardless, which is the escape hatch for a vmat that asked for blending
    it does not need.
    """
    mode = (mode or "auto").lower()
    if mode == "auto":
        return entry.translucent, entry.alphatest
    if mode == "opaque":
        return False, False
    had_intent = (entry.translucent or entry.alphatest
                  or "translucency" in entry.resolved)
    if not had_intent:
        return entry.translucent, entry.alphatest
    if mode == "translucent":
        return True, False
    if mode == "alphatest":
        return False, True
    return entry.translucent, entry.alphatest


def split_prefix(prefix: str) -> tuple[str, str]:
    """Split a user prefix into (material-path override, filename prefix).

    A prefix containing a slash is path-shaped: everything up to the last slash
    overrides the material path, the remainder prefixes the filename. This is
    what lets a user type `models/riggs/hlvr/` and have both the VMT's
    `$basetexture` and the file's location follow, since Source 1 only finds a
    material at `<game>/materials/<material_path>/<name>.vmt`.

    ================================  =========================
    Input                             Result
    ================================  =========================
    ``''``                            ``('', '')``
    ``'foo_'``                        ``('', 'foo_')``
    ``'models/riggs/hlvr/'``          ``('models/riggs/hlvr', '')``
    ``'models/riggs/hlvr'``           ``('models/riggs', 'hlvr')``
    ``'models\\\\foo\\\\'``           ``('models/foo', '')``
    ================================  =========================
    """
    if "/" not in prefix and "\\" not in prefix:
        return "", prefix
    head, _, tail = prefix.replace("\\", "/").rpartition("/")
    return head, tail
