"""
Image Processing Utilities

Reusable utilities for loading, converting, and manipulating images
for texture processing workflows.
"""

import os
from typing import Optional
import numpy as np
from PIL import Image


# Row-block height used by the chunked transfer functions below. Big enough
# that numpy's per-call overhead is irrelevant, small enough that a block's
# temporaries stay in the low tens of MiB even for a 4096-wide RGB buffer.
_CHUNK_ROWS = 256


def _open_source_image(path: str) -> Optional[Image.Image]:
    """Open `path` and normalise 16-bit modes down to 8-bit.

    Shared by `load_image` and `load_scalar_image`. Returns None for a missing
    path so callers treat "absent" and "unreadable" the same way.
    """
    if not path or not os.path.exists(path):
        return None

    img = Image.open(path)

    # CRITICAL FIX: Handle 16-bit images (I;16, I;16B, etc.) that PIL might load
    # These have values 0-65535 instead of 0-255, which breaks our normalization
    if img.mode.startswith('I;16'):
        # Convert 16-bit to 8-bit first
        img = img.point(lambda x: x / 256).convert('RGB')

    return img


def scaled_size(height: int, width: int, max_edge: int) -> Optional[tuple]:
    """Return the (height, width) that fits `max_edge`, or None if it already does.

    Single definition of the downscale arithmetic so a load-time cap lands on
    exactly the same dimensions as the working-resolution clamp that would
    otherwise have been applied after the fact.
    """
    if max_edge <= 0:
        return None
    long_edge = max(height, width)
    if long_edge <= max_edge:
        return None
    scale = max_edge / float(long_edge)
    return max(1, int(round(height * scale))), max(1, int(round(width * scale)))


def _downscale_image(img: Image.Image, max_edge: int) -> Image.Image:
    """Downscale a PIL image so its longest edge is ≤ max_edge, aspect preserved.

    Done here, in 8-bit PIL space, rather than on the decoded float32 array: a
    4096² RGBA float32 buffer is 256 MiB, while the same image as PIL bytes is
    64 MiB and the downscaled result is a quarter of that again. Resizing on
    load is what keeps a parallel batch of 4K materials inside RAM.

    RGB and alpha are resized separately for the reason documented in
    `resize_to_match` — a combined RGBA resize premultiplies RGB by alpha and
    destroys normal data wherever the packed mask is 0.
    """
    target = scaled_size(img.size[1], img.size[0], max_edge)
    if target is None:
        return img
    new_height, new_width = target

    # Palette and bilevel images can't be resampled meaningfully in their own
    # mode — PIL silently drops to nearest-neighbour — so promote them first.
    if img.mode not in ('L', 'RGB', 'RGBA'):
        img = img.convert('RGBA')

    if img.mode == 'RGBA':
        rgb = img.convert('RGB').resize((new_width, new_height), Image.LANCZOS)
        alpha = img.getchannel('A').resize((new_width, new_height), Image.LANCZOS)
        rgb.putalpha(alpha)
        return rgb
    return img.resize((new_width, new_height), Image.LANCZOS)


def load_image(path: str, max_edge: int = 0) -> Optional[np.ndarray]:
    """
    Load image and convert to linear RGB float array

    Args:
        path: Path to image file
        max_edge: Optional cap on the longest edge, applied while the image is
            still 8-bit PIL data. 0 = load at native resolution.

    Returns:
        numpy array in shape (height, width, channels) with float32 dtype,
        or None if loading fails
    """
    if not path or not os.path.exists(path):
        return None

    try:
        img = _open_source_image(path)
        img = _downscale_image(img, max_edge)

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


def load_scalar_image(path: str, max_edge: int = 0) -> Optional[np.ndarray]:
    """Load a single-channel map (AO / roughness / metallic / mask) as 2-D float32.

    Identical normalisation to `load_image`, except the result keeps one
    channel instead of being expanded to RGBA. Every consumer of these maps
    reads channel 0 anyway (`data[:, :, 0] if data.ndim > 2 else data`), so the
    values are the same while the array costs a quarter of the memory — the
    difference between 64 MiB and 256 MiB per map at 4K, which is what pushes
    a parallel batch of 4K materials into MemoryError.

    Args:
        path: Path to image file
        max_edge: Optional cap on the longest edge, applied while the image is
            still 8-bit PIL data. 0 = load at native resolution.

    Returns:
        numpy array in shape (height, width) with float32 dtype in [0, 1],
        or None if loading fails
    """
    try:
        img = _open_source_image(path)
        if img is None:
            return None

        # Palette images have to go through RGB so we sample the palette's red
        # channel — converting straight to L would luminance-average it instead,
        # which is not what the RGBA path this replaces used to do.
        if img.mode in ('P', 'PA'):
            img = img.convert('RGB')
        elif img.mode not in ('L', 'RGB', 'RGBA'):
            img = img.convert('L')
        if img.mode != 'L':
            img = img.getchannel(0)
        # Channel extraction first, so the resize below runs on one band
        # instead of four.
        img = _downscale_image(img, max_edge)

        raw_data = np.asarray(img)
        if raw_data.dtype != np.uint8:
            raw_data = np.clip(raw_data, 0, 255).astype(np.uint8)

        return raw_data.astype(np.float32) / 255.0
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

        # CRITICAL: PIL's resize on combined RGBA buffers premultiplies RGB
        # by alpha during interpolation, which zeros out RGB wherever alpha
        # is 0. For normal maps where the alpha channel carries the phong
        # mask (mostly 0 in flat dielectric areas), this destroys the flat
        # tangent-space normal data and produces all-black regions in the
        # encoded VTF. For translucent color textures it similarly trashes
        # the RGB in transparent regions. Split RGB and alpha, resize each
        # independently, then recombine.
        if data.ndim == 2:
            img = Image.fromarray(uint8_data, mode='L').resize(
                (target_width, target_height), Image.LANCZOS
            )
            resized = np.array(img, dtype=np.float32) / 255.0
        elif data.shape[2] == 1:
            img = Image.fromarray(uint8_data[:, :, 0], mode='L').resize(
                (target_width, target_height), Image.LANCZOS
            )
            resized = np.array(img, dtype=np.float32) / 255.0
        elif data.shape[2] == 3:
            img = Image.fromarray(uint8_data, mode='RGB').resize(
                (target_width, target_height), Image.LANCZOS
            )
            resized = np.array(img, dtype=np.float32) / 255.0
        elif data.shape[2] == 4:
            rgb_img = Image.fromarray(uint8_data[:, :, :3], mode='RGB').resize(
                (target_width, target_height), Image.LANCZOS
            )
            alpha_img = Image.fromarray(uint8_data[:, :, 3], mode='L').resize(
                (target_width, target_height), Image.LANCZOS
            )
            rgb_arr = np.array(rgb_img, dtype=np.float32) / 255.0
            alpha_arr = np.array(alpha_img, dtype=np.float32)[:, :, np.newaxis] / 255.0
            resized = np.concatenate([rgb_arr, alpha_arr], axis=2)
        else:
            raise ValueError(f"Unsupported number of channels: {data.shape[2]}")

        # Ensure output has same dimensionality as input
        if data.ndim == 3 and resized.ndim == 2:
            resized = resized[:, :, np.newaxis]

        return resized

    return data


def srgb_to_linear(srgb: np.ndarray, out: Optional[np.ndarray] = None) -> np.ndarray:
    """
    Convert sRGB to linear color space

    Uses proper sRGB to linear conversion formula:
    - linear = srgb / 12.92 for srgb <= 0.04045
    - linear = ((srgb + 0.055) / 1.055) ^ 2.4 otherwise

    Evaluated in row blocks rather than as one `np.where` over the whole image:
    `np.where` materialises *both* branches at full size, so a 4096² RGB buffer
    used to spike ~5 full-size float32 temporaries (~1 GiB) for a 192 MiB
    result. Blocking caps the temporaries at a few MiB with identical output.

    Args:
        srgb: Image data in sRGB color space [0, 1]
        out: Optional destination array; pass the input itself to convert
             in place and avoid a second full-size allocation

    Returns:
        Image data in linear color space [0, 1]
    """
    src = np.asarray(srgb, dtype=np.float32)
    result = np.empty_like(src) if out is None else out

    for lo in range(0, src.shape[0], _CHUNK_ROWS):
        hi = lo + _CHUNK_ROWS
        block = src[lo:hi]
        dst = result[lo:hi]
        b_high = block > 0.04045
        # The high branch is computed first because `dst` may alias `block`
        # when the caller asked for an in-place conversion.
        high = block + 0.055
        high /= 1.055
        np.power(high, 2.4, out=high)
        np.divide(block, 12.92, out=dst)
        np.copyto(dst, high, where=b_high)

    return result


def linear_to_srgb(linear: np.ndarray, out: Optional[np.ndarray] = None) -> np.ndarray:
    """
    Convert linear to sRGB color space

    Uses proper linear to sRGB conversion formula:
    - srgb = linear * 12.92 for linear <= 0.0031308
    - srgb = 1.055 * linear ^ (1/2.4) - 0.055 otherwise

    Blocked for the same reason as `srgb_to_linear`.

    Args:
        linear: Image data in linear color space [0, 1]
        out: Optional destination array; pass the input itself to convert
             in place and avoid a second full-size allocation

    Returns:
        Image data in sRGB color space [0, 1]
    """
    src = np.asarray(linear, dtype=np.float32)
    result = np.empty_like(src) if out is None else out

    for lo in range(0, src.shape[0], _CHUNK_ROWS):
        hi = lo + _CHUNK_ROWS
        block = src[lo:hi]
        dst = result[lo:hi]
        b_high = block > 0.0031308
        high = np.power(block, 1.0 / 2.4)
        high *= 1.055
        high -= 0.055
        np.multiply(block, 12.92, out=dst)
        np.copyto(dst, high, where=b_high)

    return result


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
    
    # Blocked for the same reason as the transfer functions above: the naive
    # `(np.clip(data, 0, 1) * 255).astype(np.uint8)` builds two full-size
    # float32 temporaries on top of the result.
    result = np.empty(data.shape, dtype=np.uint8)
    for lo in range(0, data.shape[0], _CHUNK_ROWS):
        hi = lo + _CHUNK_ROWS
        block = data[lo:hi]
        scaled = np.clip(block, 0.0, 1.0) if clip else block
        result[lo:hi] = scaled * 255.0

    # Final safety check
    if result.max() > 255 or result.min() < 0:
        import warnings
        warnings.warn(
            f"to_uint8: OUTPUT out of range [{result.min()}, {result.max()}]! This should NEVER happen!"
        )
    
    return result
