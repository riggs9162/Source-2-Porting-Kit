"""
PBR Texture Processing Utilities

Core algorithms for converting PBR (Physically Based Rendering) textures
from Source 2 format to Source 1 format with proper material properties.
"""

from typing import Optional
import numpy as np


def apply_ao_to_color(
    color_linear: np.ndarray,
    ao: Optional[np.ndarray],
    ao_strength: float = 0.5
) -> np.ndarray:
    """
    Apply ambient occlusion to base color in linear space
    
    Uses physically correct darkening formula:
    color_final = color_linear * (1 - ao_strength * (1 - ao))
    
    This darkens by ao_strength when ao=0 (occluded), and not at all when ao=1 (lit).
    
    Args:
        color_linear: RGB color in linear space (height, width, 3)
        ao: Ambient occlusion map (height, width, channels) or None
        ao_strength: AO darkening strength [0, 2.0]
    
    Returns:
        RGB color with AO applied in linear space
    """
    if ao is None:
        return color_linear
    
    # Extract first channel (AO is grayscale)
    ao_value = ao[:, :, 0] if ao.ndim > 2 else ao
    
    # Apply physically correct AO formula
    ao_factor = 1.0 - ao_strength * (1.0 - ao_value)
    ao_factor = np.clip(ao_factor, 0.0, 1.0)
    
    # Apply to RGB channels
    result = color_linear * ao_factor[:, :, np.newaxis]
    return result


def create_metallic_mask(metallic: Optional[np.ndarray], height: int, width: int) -> np.ndarray:
    """
    Create metallic mask for $blendtintbybasealpha
    
    Args:
        metallic: Metallic map (height, width, channels) or None
        height: Target height if metallic is None
        width: Target width if metallic is None
    
    Returns:
        Metallic mask as 2D array (height, width)
        White (1.0) = metal, Black (0.0) = dielectric
    """
    if metallic is not None:
        mask = metallic[:, :, 0] if metallic.ndim > 2 else metallic
        return np.clip(mask, 0.0, 1.0)
    else:
        # No metallic input = fully dielectric (black = 0.0)
        return np.zeros((height, width), dtype=np.float32)


def compute_envmap_mask(
    metallic: Optional[np.ndarray],
    roughness: Optional[np.ndarray],
    ao: Optional[np.ndarray],
    height: int,
    width: int
) -> np.ndarray:
    """
    Calculate environment map mask for reflections
    
    Formula: envmap_mask = metal × (1 - roughness) × ao
    
    This controls cubemap reflection intensity:
    - Metal surfaces (metal=1) reflect strongly
    - Smooth surfaces (roughness=0) reflect sharply
    - Exposed areas (ao=1) reflect fully
    
    Args:
        metallic: Metallic map or None
        roughness: Roughness map or None
        ao: Ambient occlusion map or None
        height: Target height
        width: Target width
    
    Returns:
        Environment map mask as 2D array (height, width)
    """
    # Start with fully lit (1.0)
    envmap_mask = np.ones((height, width), dtype=np.float32)
    
    # Multiply by metallic (white=metal reflects, black=dielectric doesn't)
    if metallic is not None:
        metal_value = metallic[:, :, 0] if metallic.ndim > 2 else metallic
        envmap_mask *= metal_value
    else:
        # No metal input = no reflections (dielectric)
        envmap_mask *= 0.0
    
    # Multiply by smoothness (1 - roughness) for reflection strength
    if roughness is not None:
        rough_value = roughness[:, :, 0] if roughness.ndim > 2 else roughness
        smoothness = 1.0 - rough_value
        smoothness = np.clip(smoothness, 0.0, 1.0)
        envmap_mask *= smoothness
    else:
        # No roughness input = moderate smoothness (0.5)
        envmap_mask *= 0.5
    
    # Multiply by AO (dark areas don't receive reflections)
    if ao is not None:
        ao_value = ao[:, :, 0] if ao.ndim > 2 else ao
        envmap_mask *= ao_value
    # If no AO, assume fully lit (multiply by 1.0, no change)
    
    # Final clamp
    envmap_mask = np.clip(envmap_mask, 0.0, 1.0)
    
    return envmap_mask


def roughness_to_gloss(roughness: Optional[np.ndarray], gamma: float = 2.2, height: int = 512, width: int = 512) -> np.ndarray:
    """
    Convert roughness to gloss/phong exponent
    
    Formula: gloss = (1 - roughness) ^ gamma
    
    Roughness range [0-1]:
    - 0.0 (smooth) → gloss = 1.0 (tight, sharp highlights)
    - 0.5 (medium) → gloss ≈ 0.22 (moderate highlights)
    - 1.0 (rough)  → gloss = 0.0 (diffuse, no highlights)
    
    Args:
        roughness: Roughness map or None (defaults to 0.5)
        gamma: Power curve for gloss conversion (default 2.2)
        height: Default height if roughness is None
        width: Default width if roughness is None
    
    Returns:
        Gloss map as 2D array (height, width)
    """
    if roughness is None:
        # Use default: roughness = 0.5 (medium)
        rough_value = np.full((height, width), 0.5, dtype=np.float32)
    else:
        # Extract first channel (roughness is grayscale)
        rough_value = roughness[:, :, 0] if roughness.ndim > 2 else roughness
    
    # Apply the formula: gloss = (1 - roughness) ^ gamma
    gloss = 1.0 - rough_value
    gloss = np.power(gloss, gamma)
    
    # Clamp to valid range [0, 1]
    gloss = np.clip(gloss, 0.0, 1.0)
    
    return gloss


def compute_rimlight_mask(gloss: np.ndarray, ao: Optional[np.ndarray]) -> np.ndarray:
    """
    Compute rimlight mask from gloss and AO
    
    Formula: rimlight_mask = gloss × ao
    - Gloss (smooth surfaces) = more rimlight
    - AO (exposed areas) = more rimlight
    
    Args:
        gloss: Gloss/smoothness map (height, width)
        ao: Ambient occlusion map or None
    
    Returns:
        Rimlight mask as 2D array (height, width)
    """
    if ao is None:
        # No AO provided, use gloss directly
        return np.clip(gloss, 0.0, 1.0)
    
    # Extract AO channel
    ao_value = ao[:, :, 0] if ao.ndim > 2 else ao
    ao_value = np.clip(ao_value, 0.0, 1.0)
    
    # Combine gloss and AO
    rimlight_mask = gloss * ao_value
    rimlight_mask = np.clip(rimlight_mask, 0.0, 1.0)
    
    return rimlight_mask


def process_base_texture(
    color: np.ndarray,
    ao: Optional[np.ndarray],
    metallic: Optional[np.ndarray],
    ao_strength: float = 0.5
) -> np.ndarray:
    """
    Process base color texture with AO and metallic mask
    
    Pipeline:
    1. Convert color to linear RGB
    2. Apply AO with physically correct darkening
    3. Convert back to sRGB
    4. Preserve original alpha channel from color texture
    
    Args:
        color: RGBA color texture
        ao: Ambient occlusion map (optional)
        metallic: Metallic map (optional)
        ao_strength: AO darkening strength [0, 2.0]
    
    Returns:
        RGBA uint8 array with:
        - RGB: sRGB color with AO baked
        - Alpha: Preserved from original color texture (fully opaque if not present)
    """
    from .image_processing import srgb_to_linear, linear_to_srgb, to_uint8
    
    # Extract RGB channels
    rgb = color[:, :, :3]
    height, width = rgb.shape[:2]
    
    # Preserve original alpha channel from color texture
    if color.shape[2] >= 4:
        alpha = color[:, :, 3]
    else:
        # If no alpha channel in source, default to fully opaque
        alpha = np.ones((height, width), dtype=np.float32)
    
    # Convert to linear space for correct math
    rgb_linear = srgb_to_linear(rgb)
    
    # Apply AO
    rgb_linear = apply_ao_to_color(rgb_linear, ao, ao_strength)
    
    # Convert back to sRGB for VTF export
    rgb_srgb = linear_to_srgb(rgb_linear)
    rgb_srgb = np.clip(rgb_srgb, 0.0, 1.0)
    
    # Combine into RGBA with preserved alpha
    rgba = np.dstack([rgb_srgb, alpha])
    
    # Convert to uint8
    return to_uint8(rgba)


def pack_normal_with_envmap(
    normal: np.ndarray,
    ao: Optional[np.ndarray],
    metallic: Optional[np.ndarray],
    roughness: Optional[np.ndarray]
) -> np.ndarray:
    """
    Pack normal map with environment map mask in alpha
    
    Output format:
    - RGB channels: Normal map (validated and clamped)
    - Alpha channel: envmap mask = metal × (1 - roughness) × ao
    
    Used by $normalmapalphaenvmapmask 1 in VMT
    
    Args:
        normal: RGBA normal map texture (float32 [0,1] from load_image)
        ao: Ambient occlusion map (optional)
        metallic: Metallic map (optional)
        roughness: Roughness map (optional)
    
    Returns:
        RGBA uint8 array with:
        - RGB: Normal map (validated and clamped to prevent overflow)
        - Alpha: envmap mask
        
    Note:
        Normal maps are stored in tangent space with values remapped:
        - (0.5, 0.5, 1.0) represents flat surface normal (0, 0, +Z)
        - RGB values in [0, 1] range are converted to uint8 [0, 255]
    """
    from .image_processing import to_uint8
    
    # CRITICAL: Validate input normal map data range FIRST
    # If input data is > 1.0, it indicates the image wasn't loaded correctly
    # or has been corrupted by previous processing
    if normal.max() > 1.0:
        import warnings
        warnings.warn(
            f"Normal map input data exceeds 1.0 (max={normal.max():.3f}). "
            f"This will cause VTF encoding overflow errors. Clamping to [0,1]."
        )
    
    # Extract RGB channels from normal map with AGGRESSIVE clipping
    normal_rgb = normal[:, :, :3].copy()
    # Clip IMMEDIATELY after extraction
    normal_rgb = np.clip(normal_rgb, 0.0, 1.0)
    height, width = normal_rgb.shape[:2]
    
    # CRITICAL FIX: Validate and clamp normal map to prevent VTF encoding errors
    # Ensure all values are strictly in [0, 1] range before uint8 conversion
    # This prevents "value X for channel Y is invalid" errors in VTFEdit
    normal_rgb = np.clip(normal_rgb, 0.0, 1.0)
    
    # Calculate envmap mask for alpha channel
    envmap_mask = compute_envmap_mask(metallic, roughness, ao, height, width)
    
    # Combine channels
    rgba = np.dstack([normal_rgb, envmap_mask])
    
    # Convert to uint8 with explicit clipping enabled
    return to_uint8(rgba, clip=True)


def create_phong_texture(
    roughness: Optional[np.ndarray],
    ao: Optional[np.ndarray],
    gloss_gamma: float = 2.2,
    height: int = 512,
    width: int = 512
) -> np.ndarray:
    """
    Create phong/gloss texture with rimlight mask
    
    Output format:
    - RGB: gloss map (grayscale) = (1-roughness)^gamma
    - Alpha: rimlight mask = gloss × AO
    
    Args:
        roughness: Roughness map (optional, defaults to 0.5)
        ao: Ambient occlusion map (optional)
        gloss_gamma: Power curve for gloss conversion
        height: Default height
        width: Default width
    
    Returns:
        RGBA uint8 array with:
        - RGB: gloss (grayscale)
        - Alpha: rimlight mask for $rimmask
    """
    from .image_processing import to_uint8
    
    # Get actual dimensions from inputs
    if roughness is not None:
        height, width = roughness.shape[:2]
    elif ao is not None:
        height, width = ao.shape[:2]
    
    # Convert roughness to gloss
    gloss = roughness_to_gloss(roughness, gloss_gamma, height, width)
    
    # Compute rimlight mask
    rimlight_mask = compute_rimlight_mask(gloss, ao)
    
    # Build RGBA: RGB = gloss (grayscale), A = rimlight mask
    gloss_rgba = np.dstack([gloss, gloss, gloss, rimlight_mask])
    
    # Convert to uint8 (linear space, no sRGB)
    return to_uint8(gloss_rgba)
