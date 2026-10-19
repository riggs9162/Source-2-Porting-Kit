"""Shared material and texture defaults without conversion dependencies."""

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".tga", ".bmp", ".tif", ".tiff")

BRANCH_LABELS: tuple[tuple[str, str], ...] = (
    ("gmod", "Garry's Mod"),
    ("l4d2", "Left 4 Dead 2"),
    ("tf2", "Team Fortress 2"),
    ("source2013_mp", "Source 2013 multiplayer"),
    ("source2013_sp", "Source 2013 singleplayer"),
    ("hl2", "Half-Life 2 (no modern parameters)"),
)

GLOW_MODES: tuple[tuple[str, str], ...] = (
    ("selfillum", "$selfillum (classic, breaks with tint/translucency)"),
    ("emissiveblend", "Emissive Blend (recommended)"),
    ("emissiveblend_basecolor", "Emissive Blend from the base colour"),
)

#: Residual diffuse light compensates for stock Source 1 cubemap limitations.
METAL_DIFFUSE_SUPPRESSION = 0.92

#: Colored metals retain their hue through specular tint rather than diffuse relief.
COLORED_METAL_RELIEF = 0.0

MISSING_METAL_MODES: tuple[tuple[str, str], ...] = (
    ("off", "Leave it missing (no envmap, no metal darkening)"),
    ("full", "Fully metallic"),
    ("albedo_r", "The albedo's red channel"),
    ("albedo_g", "The albedo's green channel"),
    ("albedo_b", "The albedo's blue channel"),
)
