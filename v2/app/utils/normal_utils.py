"""
Normal Map Utilities

Helper functions for validating and processing normal maps
to ensure they're correctly formatted for VTF encoding.
"""

import numpy as np
from typing import Optional


def validate_normal_map(normal: np.ndarray) -> np.ndarray:
    """
    Validate and normalize a normal map to ensure it's in the correct format
    
    Ensures:
    - RGB channels are in [0, 1] range
    - Values are properly clamped
    - Optional: Reconstruct Z channel if needed
    
    Args:
        normal: Normal map as float32 array (height, width, 3 or 4) in range [0, 1]
        
    Returns:
        Validated normal map with RGB channels in [0, 1]
        
    Note:
        Tangent-space normal maps typically encode:
        - Red channel: X component (tangent direction)
        - Green channel: Y component (bitangent direction)
        - Blue channel: Z component (surface normal direction)
        
        Values are remapped from [-1, 1] to [0, 1] space:
        - 0.5 (128 in uint8) represents 0.0 (no displacement)
        - 1.0 (255 in uint8) represents +1.0 (positive direction)
        - 0.0 (0 in uint8) represents -1.0 (negative direction)
    """
    # Ensure we have float32 data
    if normal.dtype != np.float32:
        normal = normal.astype(np.float32)
    
    # Extract RGB channels
    normal_rgb = normal[:, :, :3].copy()
    
    # Clamp to valid range [0, 1]
    normal_rgb = np.clip(normal_rgb, 0.0, 1.0)
    
    return normal_rgb


def reconstruct_normal_z(normal_xy: np.ndarray) -> np.ndarray:
    """
    Reconstruct Z channel from X and Y channels for tangent-space normals
    
    Uses the constraint that normal vectors have length 1:
    X² + Y² + Z² = 1
    Therefore: Z = sqrt(1 - X² - Y²)
    
    Args:
        normal_xy: Normal map with only X and Y channels (height, width, 2)
                   in [0, 1] range where 0.5 = 0.0, 1.0 = +1.0, 0.0 = -1.0
        
    Returns:
        Full RGB normal map (height, width, 3) with reconstructed Z
        
    Note:
        This is useful when normal maps only contain RG channels (2-channel compression)
        or when the Z channel needs to be verified/rebuilt.
    """
    # Convert from [0, 1] range to [-1, 1] range
    x = (normal_xy[:, :, 0] * 2.0) - 1.0
    y = (normal_xy[:, :, 1] * 2.0) - 1.0
    
    # Reconstruct Z: Z = sqrt(1 - X² - Y²)
    z_squared = 1.0 - (x * x + y * y)
    z_squared = np.maximum(z_squared, 0.0)  # Clamp to avoid sqrt of negative
    z = np.sqrt(z_squared)
    
    # Convert back to [0, 1] range
    x_normalized = (x + 1.0) * 0.5
    y_normalized = (y + 1.0) * 0.5
    z_normalized = (z + 1.0) * 0.5
    
    # Stack into RGB normal map
    normal_rgb = np.dstack([x_normalized, y_normalized, z_normalized])
    
    # Clamp final result
    normal_rgb = np.clip(normal_rgb, 0.0, 1.0)
    
    return normal_rgb


def check_normal_map_validity(normal: np.ndarray) -> tuple[bool, str]:
    """
    Check if a normal map is valid and properly formatted
    
    Args:
        normal: Normal map array to check
        
    Returns:
        Tuple of (is_valid, error_message)
        - is_valid: True if normal map is valid
        - error_message: Description of any issues found, or empty string if valid
    """
    # Check shape
    if normal.ndim not in [3, 4]:
        return False, f"Invalid shape: {normal.shape}. Expected (height, width, 3 or 4)"
    
    if normal.shape[2] < 3:
        return False, f"Not enough channels: {normal.shape[2]}. Need at least RGB"
    
    # Check data type
    if normal.dtype != np.float32:
        return False, f"Invalid dtype: {normal.dtype}. Expected float32"
    
    # Check value range
    if normal.min() < 0.0 or normal.max() > 1.0:
        return False, f"Values out of range: [{normal.min():.3f}, {normal.max():.3f}]. Expected [0.0, 1.0]"
    
    # Check for degenerate normals (all channels same value = not a real normal)
    rgb = normal[:, :, :3]
    if np.allclose(rgb[:, :, 0], rgb[:, :, 1]) and np.allclose(rgb[:, :, 1], rgb[:, :, 2]):
        # All channels are identical - might be a placeholder/error
        unique_vals = np.unique(rgb[:, :, 0])
        if len(unique_vals) == 1:
            return False, f"Degenerate normal map: all channels have same constant value ({unique_vals[0]:.3f})"
    
    return True, ""
