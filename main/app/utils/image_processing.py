"""
Image Processing Utilities

Reusable utilities for loading, converting, and manipulating images
for texture processing workflows.
"""

import os
from typing import Optional
import numpy as np
from PIL import Image


def load_image(path: str) -> Optional[np.ndarray]:
    """
    Load image and convert to linear RGB float array
    
    Args:
        path: Path to image file
    
    Returns:
        numpy array in shape (height, width, channels) with float32 dtype,
        or None if loading fails
    """
    if not path or not os.path.exists(path):
        return None
    
    try:
        img = Image.open(path)
        
        # CRITICAL FIX: Handle 16-bit images (I;16, I;16B, etc.) that PIL might load
        # These have values 0-65535 instead of 0-255, which breaks our normalization
        if img.mode.startswith('I;16'):
            # Convert 16-bit to 8-bit first
            img = img.point(lambda x: x / 256).convert('RGB')
        
        # Convert to RGBA if needed
        if img.mode != 'RGBA':
            if img.mode == 'RGB':
                # Add alpha channel
                alpha = Image.new('L', img.size, 255)
                img.putalpha(alpha)
            else:
                img = img.convert('RGBA')
        
        # Convert to numpy array - PIL RGBA mode is always uint8
        raw_data = np.array(img)
        
        # SAFETY CHECK: Ensure we got uint8 data
        if raw_data.dtype != np.uint8:
            import warnings
            warnings.warn(
                f"load_image: PIL returned {raw_data.dtype} instead of uint8 for {path}. "
                f"Value range: [{raw_data.min()}, {raw_data.max()}]. Converting..."
            )
            # Normalize whatever we got to 0-255 range, then convert to uint8
            if raw_data.max() > 255:
                raw_data = (raw_data / raw_data.max() * 255).astype(np.uint8)
            else:
                raw_data = raw_data.astype(np.uint8)
        
        # Now normalize to [0, 1] float
        data = raw_data.astype(np.float32) / 255.0
        
        # Validate range - should always be [0, 1] after division
        if data.max() > 1.0 or data.min() < 0.0:
            import warnings
            warnings.warn(
                f"Loaded image has invalid range: [{data.min():.3f}, {data.max():.3f}]. "
                f"Expected [0, 1]. Clamping values. File: {path}"
            )
            data = np.clip(data, 0.0, 1.0)
        
        return data
    except Exception as e:
        print(f"[Error] Failed to load image {path}: {str(e)}")
        return None


def resize_to_match(data: Optional[np.ndarray], target_height: int, target_width: int, name: str = "image") -> Optional[np.ndarray]:
    """
    Resize image data to match target dimensions if needed
    
    Args:
        data: Image data as numpy array
        target_height: Target height in pixels
        target_width: Target width in pixels
        name: Name of the image for logging
    
    Returns:
        Resized image data or original if no resize needed, or None if input is None
    """
    if data is None:
        return None
    
    if data.shape[:2] != (target_height, target_width):
        print(f"[Warning] Resizing {name} from {data.shape[1]}x{data.shape[0]} to {target_width}x{target_height}")
        
        # Convert to uint8 for PIL
        uint8_data = (np.clip(data, 0.0, 1.0) * 255.0).astype(np.uint8)
        
        # Determine PIL mode based on shape
        if data.ndim == 2:
            # Single channel grayscale
            img = Image.fromarray(uint8_data, mode='L')
        elif data.shape[2] == 1:
            # Single channel in (H, W, 1) format
            img = Image.fromarray(uint8_data[:, :, 0], mode='L')
        elif data.shape[2] == 3:
            # RGB
            img = Image.fromarray(uint8_data, mode='RGB')
        elif data.shape[2] == 4:
            # RGBA
            img = Image.fromarray(uint8_data, mode='RGBA')
        else:
            raise ValueError(f"Unsupported number of channels: {data.shape[2]}")
        
        # Resize with high-quality filter
        img = img.resize((target_width, target_height), Image.LANCZOS)
        
        # Convert back to float32, preserving channel count
        resized = np.array(img, dtype=np.float32) / 255.0
        
        # Ensure output has same dimensionality as input
        if data.ndim == 3 and resized.ndim == 2:
            resized = resized[:, :, np.newaxis]
        
        return resized
    
    return data


def srgb_to_linear(srgb: np.ndarray) -> np.ndarray:
    """
    Convert sRGB to linear color space
    
    Uses proper sRGB to linear conversion formula:
    - linear = srgb / 12.92 for srgb <= 0.04045
    - linear = ((srgb + 0.055) / 1.055) ^ 2.4 otherwise
    
    Args:
        srgb: Image data in sRGB color space [0, 1]
    
    Returns:
        Image data in linear color space [0, 1]
    """
    linear = np.where(
        srgb <= 0.04045,
        srgb / 12.92,
        np.power((srgb + 0.055) / 1.055, 2.4)
    )
    return linear


def linear_to_srgb(linear: np.ndarray) -> np.ndarray:
    """
    Convert linear to sRGB color space
    
    Uses proper linear to sRGB conversion formula:
    - srgb = linear * 12.92 for linear <= 0.0031308
    - srgb = 1.055 * linear ^ (1/2.4) - 0.055 otherwise
    
    Args:
        linear: Image data in linear color space [0, 1]
    
    Returns:
        Image data in sRGB color space [0, 1]
    """
    srgb = np.where(
        linear <= 0.0031308,
        linear * 12.92,
        1.055 * np.power(linear, 1.0 / 2.4) - 0.055
    )
    return srgb


def create_default_map(height: int, width: int, value: float = 0.5) -> np.ndarray:
    """
    Create a default grayscale map filled with a constant value
    
    Args:
        height: Height in pixels
        width: Width in pixels
        value: Fill value [0, 1]
    
    Returns:
        RGBA numpy array with constant grayscale value
    """
    data = np.full((height, width, 4), value, dtype=np.float32)
    return data


def extract_channel(data: np.ndarray, channel: int = 0) -> np.ndarray:
    """
    Extract a single channel from image data
    
    Args:
        data: Image data (height, width, channels)
        channel: Channel index (0=R, 1=G, 2=B, 3=A)
    
    Returns:
        Single channel as 2D array (height, width)
    """
    if data.ndim == 2:
        return data
    return data[:, :, channel]


def to_uint8(data: np.ndarray, clip: bool = True) -> np.ndarray:
    """
    Convert float [0, 1] image data to uint8 [0, 255]
    
    Args:
        data: Float image data in range [0, 1]
        clip: Whether to clip values to [0, 1] before conversion (default: True)
    
    Returns:
        uint8 image data in range [0, 255]
        
    Note:
        Always use clip=True for normal maps to avoid channel overflow errors
    """
    # Check for invalid input BEFORE clipping
    if data.max() > 1.0 or data.min() < 0.0:
        import warnings
        warnings.warn(
            f"to_uint8: Input data out of range [{data.min():.3f}, {data.max():.3f}]. "
            f"Values should be [0, 1]. {'Clipping enabled.' if clip else 'Clipping DISABLED - will overflow!'}"
        )
    
    if clip:
        data = np.clip(data, 0.0, 1.0)
    
    # Convert to uint8, ensuring result is in valid range
    result = (data * 255.0).astype(np.uint8)
    
    # Final safety check
    if result.max() > 255 or result.min() < 0:
        import warnings
        warnings.warn(
            f"to_uint8: OUTPUT out of range [{result.min()}, {result.max()}]! This should NEVER happen!"
        )
    
    return result
