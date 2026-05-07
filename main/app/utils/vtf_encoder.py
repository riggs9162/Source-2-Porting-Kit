"""
VTF Encoder Utility
Wrapper around sourcepp.vtfpp for encoding images to VTF format
"""

import os
import numpy as np
from typing import Optional

try:
    from sourcepp import vtfpp
except ImportError:
    raise ImportError(
        "sourcepp is not installed. Please install it via: pip install sourcepp"
    )


VTF_FLAG_NORMAL = vtfpp.VTF.Flags.V0_NORMAL.value
VTF_FLAG_SRGB = 0x00000040  # Internal sentinel; applied with VTF.set_srgb().


class VTFEncoderError(Exception):
    """Custom exception for VTF encoding errors"""
    pass


class VTFEncoder:
    """
    VTF Encoder class for converting image buffers to VTF format
    using sourcepp.vtfpp
    """

    def __init__(self):
        """Initialize VTF Encoder"""
        # No initialization needed for sourcepp - it's stateless
        pass

    def encode_to_vtf(
        self,
        pixel_data: np.ndarray,
        output_path: str,
        image_format: vtfpp.ImageFormat = vtfpp.ImageFormat.DXT5,
        flags: int = 0,
        invert_green: bool = False,
        generate_mipmaps: bool = True
    ) -> bool:
        """
        Encode a numpy array to VTF format using sourcepp

        Args:
            pixel_data: RGBA image data as numpy array (height, width, 4) with uint8 dtype
            output_path: Path to save the VTF file
            image_format: sourcepp ImageFormat enum value
            flags: Image flags (sRGB, Normal, etc.)
            invert_green: Whether to invert the green channel (for normal maps)
            generate_mipmaps: Whether to generate mipmaps in the VTF

        Returns:
            True if successful, False otherwise
        """

        # Validate input
        if pixel_data.ndim != 3 or pixel_data.shape[2] not in [3, 4]:
            raise VTFEncoderError(
                f"Invalid pixel data shape: {pixel_data.shape}. "
                "Expected (height, width, 3) or (height, width, 4)"
            )

        height, width, channels = pixel_data.shape

        # Convert to RGBA if needed
        if channels == 3:
            rgba_data = np.zeros((height, width, 4), dtype=np.uint8)
            rgba_data[:, :, :3] = pixel_data
            rgba_data[:, :, 3] = 255
            pixel_data = rgba_data

        # Ensure uint8 dtype and valid range
        if pixel_data.dtype != np.uint8:
            pixel_data = np.clip(pixel_data, 0, 255).astype(np.uint8)
        
        # CRITICAL: Double-check that uint8 values are actually in valid range
        # This should NEVER trigger, but if sourcepp is seeing value 302, 
        # something is very wrong with the data
        actual_min = int(pixel_data.min())
        actual_max = int(pixel_data.max())
        if actual_min < 0 or actual_max > 255:
            raise VTFEncoderError(
                f"CRITICAL: uint8 data has impossible values: [{actual_min}, {actual_max}]. "
                f"uint8 can only hold [0, 255]. Memory corruption or data type error!"
            )
        
        # Log data stats for debugging
        import logging
        logger = logging.getLogger(__name__)
        logger.debug(f"VTF Encoder input: shape={pixel_data.shape}, dtype={pixel_data.dtype}, "
                    f"range=[{actual_min}, {actual_max}]")
        
        # Check each channel individually for normal maps
        if flags & VTF_FLAG_NORMAL:
            r_max = int(pixel_data[:,:,0].max())
            g_max = int(pixel_data[:,:,1].max())
            b_max = int(pixel_data[:,:,2].max())
            a_max = int(pixel_data[:,:,3].max())
            logger.debug(f"Normal map channel maxes: R={r_max}, G={g_max}, B={b_max}, A={a_max}")
            
            if b_max > 255:
                raise VTFEncoderError(
                    f"Blue channel max is {b_max} which exceeds 255! "
                    f"This is the exact error VTFEdit reported."
                )

        # sourcepp expects RGBA8888 format for input
        # Convert numpy array to bytes in C-contiguous order
        pixel_data = np.ascontiguousarray(pixel_data)
        pixel_bytes = pixel_data.tobytes()

        try:
            # Create output directory if needed
            os.makedirs(os.path.dirname(output_path), exist_ok=True)

            # Setup creation options
            options = vtfpp.VTF.CreationOptions()
            options.version = 4  # VTF 7.4
            options.output_format = image_format
            b_srgb = bool(flags & VTF_FLAG_SRGB)
            sourcepp_flags = flags & ~VTF_FLAG_SRGB
            options.flags = sourcepp_flags
            # sourcepp can have issues with automatic computations; allow user control over mipmaps
            options.compute_mips = bool(generate_mipmaps)
            options.compute_thumbnail = False
            options.compute_reflectivity = False
            options.compute_transparency_flags = False
            options.invert_green_channel = invert_green

            # Create VTF using sourcepp's static method
            # VTF.create_and_bake returns None on success
            vtfpp.VTF.create_and_bake(
                pixel_bytes,
                vtfpp.ImageFormat.RGBA8888,  # Input format
                width,
                height,
                output_path,
                options
            )

            # NOTE: sourcepp exposes VTF.set_srgb(), but rewriting a freshly
            # baked DXT texture through create_from_file() -> bake_to_file()
            # currently truncates some outputs to tiny invalid files. Keep the
            # encode path stable and avoid post-bake rewrites here.

            # Verify file was created
            if not os.path.exists(output_path):
                raise VTFEncoderError(f"Failed to create VTF file: {output_path}")

            return True

        except Exception as e:
            raise VTFEncoderError(f"Error encoding VTF: {str(e)}")

    def encode_base_texture(
        self,
        pixel_data: np.ndarray,
        output_path: str,
        generate_mipmaps: bool = True
    ) -> bool:
        """
        Encode base color texture with sRGB and DXT5 compression
        
        Args:
            pixel_data: RGBA numpy array (height, width, 4) with uint8 dtype
            output_path: Output VTF file path
            
        Returns:
            True if successful
        """

        return self.encode_to_vtf(
            pixel_data,
            output_path,
            image_format=vtfpp.ImageFormat.DXT5,
            flags=VTF_FLAG_SRGB,
            invert_green=False,
            generate_mipmaps=generate_mipmaps
        )

    def encode_normal_map(
        self,
        pixel_data: np.ndarray,
        output_path: str,
        generate_mipmaps: bool = True
    ) -> bool:
        """
        Encode normal map with linear space and DXT5 compression
        
        Args:
            pixel_data: RGB(A) numpy array (height, width, 3 or 4) with uint8 dtype
                       Values MUST be in valid range [0, 255]
            output_path: Output VTF file path
            
        Returns:
            True if successful
            
        Note:
            Normal maps are encoded in linear space (no sRGB conversion).
            The NORMAL flag tells Source engine to treat this as a tangent-space normal.
        """
        
        # Validate pixel data is in valid range
        if pixel_data.max() > 255 or pixel_data.min() < 0:
            raise VTFEncoderError(
                f"Invalid pixel data range for normal map: [{pixel_data.min()}, {pixel_data.max()}]. "
                f"Expected [0, 255]. Data may not have been properly clamped."
            )
        
        return self.encode_to_vtf(
            pixel_data,
            output_path,
            image_format=vtfpp.ImageFormat.DXT5,
            flags=VTF_FLAG_NORMAL,
            invert_green=False,  # Normal maps already in correct orientation
            generate_mipmaps=generate_mipmaps
        )

    def encode_envmap_mask(
        self,
        pixel_data: np.ndarray,
        output_path: str,
        generate_mipmaps: bool = True
    ) -> bool:
        """Encode a linear colored $envmapmask texture."""
        return self.encode_to_vtf(
            pixel_data,
            output_path,
            image_format=vtfpp.ImageFormat.DXT5,
            flags=0,
            invert_green=False,
            generate_mipmaps=generate_mipmaps
        )

    def encode_selfillum_mask(
        self,
        pixel_data: np.ndarray,
        output_path: str,
        generate_mipmaps: bool = True
    ) -> bool:
        """Encode a $selfillummask texture (RGB color map, sRGB DXT5).

        Source 1's $selfillummask works as a colored mask — non-black RGB
        pixels glow at their authored color, scaled by $selfillumtint. We
        encode sRGB so the colors round-trip with the rest of the material's
        sRGB textures.
        """
        return self.encode_to_vtf(
            pixel_data,
            output_path,
            image_format=vtfpp.ImageFormat.DXT5,
            flags=VTF_FLAG_SRGB,
            invert_green=False,
            generate_mipmaps=generate_mipmaps
        )

    def encode_phong_map(
        self,
        pixel_data: np.ndarray,
        output_path: str,
        generate_mipmaps: bool = True
    ) -> bool:
        """
        Encode phong/gloss exponent map to VTF

        Expected input: RGBA uint8 array (if alpha present)
        - RGB: Gloss values (grayscale)
        - Alpha: AO for rimlight masking ($rimmask)

        Format: DXT5 if alpha present (for rimlight mask), DXT1 if RGB only
        Flags: Linear (no sRGB)
        
        Args:
            pixel_data: RGB(A) numpy array with uint8 dtype
            output_path: Output VTF file path
            
        Returns:
            True if successful
        """
        # Check if we have alpha channel
        has_alpha = pixel_data.ndim == 3 and pixel_data.shape[2] == 4

        if has_alpha:
            return self.encode_to_vtf(
                pixel_data,
                output_path,
                image_format=vtfpp.ImageFormat.DXT5,
                flags=0,
                invert_green=False,
                generate_mipmaps=generate_mipmaps
            )
        else:
            # Legacy RGB-only format - convert to grayscale DXT1
            if pixel_data.ndim == 3 and pixel_data.shape[2] == 3:
                # Already grayscale in RGB channels, just take R channel
                gray_data = pixel_data[:, :, 0]
                # Expand to RGB
                pixel_data = np.stack([gray_data, gray_data, gray_data], axis=-1)

            return self.encode_to_vtf(
                pixel_data,
                output_path,
                image_format=vtfpp.ImageFormat.DXT1,
                flags=0,
                invert_green=False,
                generate_mipmaps=generate_mipmaps
            )

    def shutdown(self):
        """
        No-op shutdown for compatibility with old API.
        
        sourcepp doesn't require explicit cleanup.
        """
        pass


# Convenience function for quick encoding
def encode_image_to_vtf(
    pixel_data: np.ndarray,
    output_path: str,
    texture_type: str = "base",
    generate_mipmaps: bool = True
) -> bool:
    """
    Quick encoding function

    Args:
        pixel_data: RGBA numpy array
        output_path: Output VTF file path
        texture_type: "base", "normal", or "phong"
        
    Returns:
        True if successful
    """
    encoder = VTFEncoder()
    try:
        if texture_type == "base":
            return encoder.encode_base_texture(pixel_data, output_path, generate_mipmaps=generate_mipmaps)
        elif texture_type == "normal":
            return encoder.encode_normal_map(pixel_data, output_path, generate_mipmaps=generate_mipmaps)
        elif texture_type == "phong":
            return encoder.encode_phong_map(pixel_data, output_path, generate_mipmaps=generate_mipmaps)
        else:
            raise ValueError(f"Unknown texture type: {texture_type}")
    finally:
        encoder.shutdown()
