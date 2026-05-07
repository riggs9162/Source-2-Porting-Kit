"""
VMT (Valve Material Type) Generation Utilities

Helper functions for generating Source 1 VMT material files
with proper shader parameters and texture references.
"""

import os
from typing import Optional, Dict, Any
from datetime import datetime


SOURCE1_TARGET_CAPABILITIES = {
    "hl2": {
        "phong_albedo_tint": False,
        "phong_exponent_factor": False,
        "envmap_fresnel_float": False,
        "envmap_lightscale": False,
        "blend_tint_by_base_alpha": False,
        "lightmapped_phong": False,
    },
    "source2013_sp": {
        "phong_albedo_tint": False,
        "phong_exponent_factor": False,
        "envmap_fresnel_float": False,
        "envmap_lightscale": False,
        "blend_tint_by_base_alpha": False,
        "lightmapped_phong": False,
    },
    "source2013_mp": {
        "phong_albedo_tint": True,
        "phong_exponent_factor": True,
        "envmap_fresnel_float": True,
        "envmap_lightscale": True,
        "blend_tint_by_base_alpha": True,
        "lightmapped_phong": True,
    },
    "gmod": {
        "phong_albedo_tint": True,
        "phong_exponent_factor": True,
        "envmap_fresnel_float": True,
        "envmap_lightscale": True,
        "blend_tint_by_base_alpha": True,
        "lightmapped_phong": True,
    },
    "gmod_x86_64": {
        "phong_albedo_tint": True,
        "phong_exponent_factor": True,
        "envmap_fresnel_float": True,
        "envmap_lightscale": True,
        "blend_tint_by_base_alpha": True,
        "lightmapped_phong": True,
    },
    "tf2": {
        "phong_albedo_tint": True,
        "phong_exponent_factor": True,
        "envmap_fresnel_float": True,
        "envmap_lightscale": True,
        "blend_tint_by_base_alpha": True,
        "lightmapped_phong": True,
    },
    "l4d2": {
        "phong_albedo_tint": True,
        "phong_exponent_factor": True,
        "envmap_fresnel_float": True,
        "envmap_lightscale": True,
        "blend_tint_by_base_alpha": True,
        "lightmapped_phong": True,
    },
}


def _capabilities_for(target_branch: str) -> Dict[str, bool]:
    return SOURCE1_TARGET_CAPABILITIES.get(target_branch, SOURCE1_TARGET_CAPABILITIES["hl2"])


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _format_float(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _format_vec3(values) -> str:
    return "[" + " ".join(_format_float(float(v)) for v in values) + "]"


def _stats_value(stats: Optional[Any], name: str, default: float) -> float:
    if stats is None:
        return default
    return float(getattr(stats, name, default))


def generate_fakepbr_vmt(
    output_path: str,
    material_name: str,
    material_path: str,
    *,
    shader: str = "VertexLitGeneric",
    target_branch: str = "gmod",
    envmap: str = "env_cubemap",
    stats: Optional[Any] = None,
    has_envmap_mask: bool = True,
    custom_params: Optional[Dict[str, Any]] = None,
    tint_mode_used: str = "off"
) -> bool:
    """Generate a stock-shader Fake PBR VMT using Phong + envmap fakery."""
    caps = _capabilities_for(target_branch)
    is_vertex_lit = shader == "VertexLitGeneric"
    is_lightmapped = shader == "LightmappedGeneric"

    avg_roughness = _stats_value(stats, "avg_roughness", 0.5)
    avg_metallic = _stats_value(stats, "avg_metallic", 0.0)
    smoothness = _clamp(1.0 - avg_roughness, 0.0, 1.0)
    b_is_rough_dielectric = bool(getattr(stats, "b_is_rough_dielectric", False)) if stats else False

    phong_boost = _clamp(96.0 * smoothness ** 2.0 + 2.0, 2.0, 128.0)
    envmaptint_scalar = _clamp(0.02 + 0.5 * smoothness ** 2.0, 0.02, 0.5)

    if avg_metallic > 0.5 and avg_roughness < 0.35:
        phong_fresnel = "[0.5 4 8]"
    elif avg_metallic > 0.5:
        phong_fresnel = "[0.5 1 4]"
    elif avg_roughness < 0.35:
        phong_fresnel = "[0.1 0.4 1]"
    else:
        phong_fresnel = "[0.5 1 8]"

    base_texture = f"{material_path}/{material_name}_color"
    bumpmap = f"{material_path}/{material_name}_normal"
    phong_texture = f"{material_path}/{material_name}_phong"
    envmask = f"{material_path}/{material_name}_envmask"

    params: Dict[str, Any] = {
        "\"$basetexture\"": base_texture,
        "\"$bumpmap\"": bumpmap,
    }

    if is_vertex_lit or (is_lightmapped and caps.get("lightmapped_phong", False)):
        params.update({
            "\"$phong\"": "1",
            "\"$phongexponenttexture\"": phong_texture,
            "\"$phongboost\"": _format_float(phong_boost),
            "\"$phongfresnelranges\"": phong_fresnel,
        })
        if caps.get("phong_albedo_tint", False):
            params["\"$phongalbedotint\""] = "1"
        else:
            params["\"$phongtint\""] = "[1 1 1]"
        if caps.get("phong_exponent_factor", False):
            params["\"$phongexponentfactor\""] = "90"

    if not b_is_rough_dielectric:
        params.update({
            "\"$envmap\"": envmap,
            "\"$envmaptint\"": _format_vec3([envmaptint_scalar] * 3),
            "\"$envmapcontrast\"": "0.5",
            "\"$envmapsaturation\"": "0.8",
            "\"$normalmapalphaenvmapmask\"": "1",
        })
        if has_envmap_mask:
            params["\"$envmapmask\""] = envmask
        if caps.get("envmap_fresnel_float", False):
            params["\"$envmapfresnel\""] = "1.0"
        if is_lightmapped and caps.get("envmap_lightscale", False):
            params["\"$envmaplightscale\""] = "1"

    if is_vertex_lit:
        params.update({
            "\"$rimlight\"": "1",
            "\"$rimlightexponent\"": "4",
            "\"$rimlightboost\"": "0.75",
            "\"$rimmask\"": "1",
            "\"$halflambert\"": "0",
            "\"$model\"": "1",
        })

    if custom_params:
        params.update(custom_params)

    section_order = [
        ("Textures", ["\"$basetexture\"", "\"$bumpmap\"", "\"$phongexponenttexture\""]),
        ("Phong", ["\"$phong\"", "\"$phongboost\"", "\"$phongexponentfactor\"", "\"$phongtint\"", "\"$phongalbedotint\"", "\"$phongfresnelranges\""]),
        ("Environment Map", ["\"$envmap\"", "\"$envmapmask\"", "\"$envmaptint\"", "\"$envmapcontrast\"", "\"$envmapsaturation\"", "\"$envmapfresnel\"", "\"$normalmapalphaenvmapmask\"", "\"$envmaplightscale\""]),
        ("Rimlight", ["\"$rimlight\"", "\"$rimlightexponent\"", "\"$rimlightboost\"", "\"$rimmask\""]),
        ("Self-Illumination", ["\"$selfillum\"", "\"$selfillummask\"", "\"$selfillumtint\"", "\"$selfillummaskscale\""]),
        ("Emissive Blend", [
            "\"$EmissiveBlendEnabled\"",
            "\"$EmissiveBlendStrength\"",
            "\"$EmissiveBlendTexture\"",
            "\"$EmissiveBlendBaseTexture\"",
            "\"$EmissiveBlendFlowTexture\"",
            "\"$EmissiveBlendTint\"",
            "\"$EmissiveBlendScrollVector\"",
        ]),
        ("Surface", ["\"$halflambert\"", "\"$model\""]),
    ]

    emitted = set()
    lines = [
        "// Generated by Source 2 Porting Kit — Fake PBR VMT",
        f"// Material: {material_path}/{material_name}",
        f"// Target: {target_branch}; Shader: {shader}",
        f"// Fake-PBR approximation: metallic={avg_metallic:.3f}; roughness={avg_roughness:.3f}; not energy-conserving",
        f"// Phong tint mode: {tint_mode_used}",
        f"// Date: {datetime.utcnow().isoformat(timespec='seconds')}Z",
        "",
        f'"{shader}"',
        "{",
    ]

    def append_param(key: str):
        if key not in params:
            return
        lines.append(f'    {key} "{params[key]}"')
        emitted.add(key)

    for section, keys in section_order:
        available = [k for k in keys if k in params]
        if not available:
            continue
        lines.append("")
        lines.append(f"    // {section}")
        for key in available:
            append_param(key)

    extra_keys = [k for k in params.keys() if k not in emitted]
    if extra_keys:
        lines.append("")
        lines.append("    // Custom parameters")
        for key in extra_keys:
            append_param(key)

    lines.append("}")
    content = "\n".join(lines) + "\n"

    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception as e:
        print(f"[Error] Failed to write Fake PBR VMT: {e}")
        return False


def generate_pbr_vmt(
    output_path: str,
    material_name: str,
    material_path: str,
    shader: str = "VertexLitGeneric",
    custom_params: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Generate a VMT file with PBR-style parameters for Source 1
    
    Creates a VMT with proper Phong, envmap, and rimlight settings
    optimized for converted PBR materials.
    
    Texture channel mapping (expected):
    - {material_name}_color.vtf:
      * RGB: Base color (sRGB, with AO baked in)
      * Alpha: Metallic mask (for $blendtintbybasealpha)
    
    - {material_name}_normal.vtf:
      * RGB: Normal map (tangent space)
      * Alpha: Envmap mask (for $normalmapalphaenvmapmask)
    
    - {material_name}_phong.vtf:
      * RGB: Gloss/phong exponent (grayscale, linear)
      * Alpha: Rimlight mask (for $rimmask)
    
    Args:
        output_path: Full path to output VMT file
        material_name: Base name of the material (without extension)
        material_path: Relative path in materials folder (e.g., "models/props")
        shader: Shader type (default "VertexLitGeneric")
        custom_params: Optional dict of additional VMT parameters to include
    
    Returns:
        True if successful, False otherwise
    """
    # Build texture paths
    base_texture = f"{material_path}/{material_name}_color"
    bumpmap = f"{material_path}/{material_name}_normal"
    phong_texture = f"{material_path}/{material_name}_phong"
    
    # Default parameters for PBR-converted materials (merged with any custom overrides)
    params = {
        "\"$basetexture\"": base_texture,
        "\"$bumpmap\"": bumpmap,
        "\"$phong\"": "1",
        "\"$phongexponenttexture\"": phong_texture,
        "\"$phongboost\"": "1",
        "\"$phongexponent\"": "8",
        "\"$phongalbedotint\"": "1",
        "\"$phongfresnelranges\"": "[1 1 1]",
        "\"$rimlight\"": "1",
        "\"$rimlightboost\"": "8",
        "\"$rimmask\"": "1",
        "\"$model\"": "1",
        "\"$color\"": "[1 1 1]",
        "\"$color2\"": "[0.5 0.5 0.5]",
        "\"$blendtintbybasealpha\"": "1"
    }

    # Apply custom params as overrides/extra keys
    merged = dict(params)
    if custom_params:
        merged.update(custom_params)

    # Known keys in display order for pretty grouping
    texture_keys = [
        "\"$basetexture\"",
        "\"$bumpmap\"",
        "\"$phongexponenttexture\"",
    ]
    phong_keys = [
        "\"$phong\"",
        "\"$phongboost\"",
        "\"$phongexponent\"",
        "\"$phongalbedotint\"",
        "\"$phongfresnelranges\"",
    ]
    rimlight_keys = [
        "\"$rimlight\"",
        "\"$rimlightboost\"",
        "\"$rimmask\"",
    ]
    misc_keys = [
        "\"$model\"",
        "\"$color\"",
        "\"$color2\"",
        "\"$blendtintbybasealpha\"",
    ]
    default_order = texture_keys + phong_keys + rimlight_keys + misc_keys

    # Build VMT content with header and spacing
    header_comment = [
        f"// Generated by Source 2 Porting Kit — FakePBR VMT",
        f"// Material: {material_path}/{material_name}",
        f"// Date: {datetime.utcnow().isoformat(timespec='seconds')}Z",
        ""
    ]

    vmt_lines = header_comment + [f'"{shader}"', '{']

    def _append_param(key: str, value: Any):
        if isinstance(value, (str, float, int)):
            vmt_lines.append(f'    {key} "{value}"')
        else:
            vmt_lines.append(f'    {key} {value}')

    # Section: Textures
    vmt_lines.append("    // Textures")
    for k in texture_keys:
        if k in merged:
            _append_param(k, merged[k])

    vmt_lines.append("")
    # Section: Phong
    vmt_lines.append("    // Phong")
    for k in phong_keys:
        if k in merged:
            _append_param(k, merged[k])

    vmt_lines.append("")
    # Section: Rimlight
    vmt_lines.append("    // Rimlight")
    for k in rimlight_keys:
        if k in merged:
            _append_param(k, merged[k])

    vmt_lines.append("")
    # Section: Misc
    vmt_lines.append("    // Misc")
    for k in misc_keys:
        if k in merged:
            _append_param(k, merged[k])

    # Any custom keys not in defaults
    extra_keys = [k for k in merged.keys() if k not in default_order]
    if extra_keys:
        vmt_lines.append("")
        vmt_lines.append("    // Custom parameters")
        for k in extra_keys:
            _append_param(k, merged[k])

    vmt_lines.append('}')
    vmt_content = '\n'.join(vmt_lines) + '\n'
    
    # Write to file
    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, 'w') as f:
            f.write(vmt_content)
        return True
    except Exception as e:
        print(f"[Error] Failed to write VMT file: {str(e)}")
        return False


def generate_simple_vmt(
    output_path: str,
    base_texture_path: str,
    shader: str = "VertexLitGeneric",
    params: Optional[Dict[str, Any]] = None
) -> bool:
    """
    Generate a simple VMT file with custom parameters
    
    More flexible VMT generator for non-PBR or custom materials.
    
    Args:
        output_path: Full path to output VMT file
        base_texture_path: Path to base texture (relative to materials folder)
        shader: Shader type
        params: Dict of VMT parameters
    
    Returns:
        True if successful, False otherwise
    """
    if params is None:
        params = {}
    
    # Ensure basetexture is set
    if "\"$basetexture\"" not in params:
        params["\"$basetexture\""] = base_texture_path

    # Build VMT content (with simple header and spacing)
    header_comment = [
        f"// Generated by Source 2 Porting Kit — Simple VMT",
        f"// Shader: {shader}",
        f"// Basetexture: {base_texture_path}",
        ""
    ]
    vmt_lines = header_comment + [f'"{shader}"', '{']
    vmt_lines.append("    // Parameters")
    for key, value in params.items():
        if isinstance(value, str):
            vmt_lines.append(f'    {key} "{value}"')
        else:
            vmt_lines.append(f'    {key} {value}')
    vmt_lines.append('}')
    vmt_content = '\n'.join(vmt_lines) + '\n'
    
    # Write to file
    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, 'w') as f:
            f.write(vmt_content)
        return True
    except Exception as e:
        print(f"[Error] Failed to write VMT file: {str(e)}")
        return False


def generate_unlit_vmt(output_path: str, base_texture_path: str) -> bool:
    """
    Generate an unlit VMT (no lighting calculations)
    
    Useful for emissive materials, UI elements, etc.
    
    Args:
        output_path: Full path to output VMT file
        base_texture_path: Path to base texture
    
    Returns:
        True if successful, False otherwise
    """
    params = {
        "\"$basetexture\"": base_texture_path,
        "\"$model\"": "1",
        "\"$nocull\"": "1",
        "\"$selfillum\"": "1"
    }
    
    return generate_simple_vmt(output_path, base_texture_path, "UnlitGeneric", params)


def generate_transparent_vmt(
    output_path: str,
    base_texture_path: str,
    translucent: bool = True
) -> bool:
    """
    Generate a VMT for transparent/translucent materials
    
    Args:
        output_path: Full path to output VMT file
        base_texture_path: Path to base texture (should have alpha)
        translucent: True for translucent (blended), False for alpha test
    
    Returns:
        True if successful, False otherwise
    """
    params = {
        "\"$basetexture\"": base_texture_path,
        "\"$model\"": "1"
    }
    
    if translucent:
        params["\"$translucent\""] = "1"
    else:
        params["\"$alphatest\""] = "1"
        params["\"$alphatestreference\""] = "1"

    return generate_simple_vmt(output_path, base_texture_path, "VertexLitGeneric", params)


def generate_exopbr_vmt(
    output_path: str,
    material_name: str,
    material_path: str,
    *,
    basetexture_path: Optional[str] = None,
    texture1_path: Optional[str] = None,
    texture2_path: Optional[str] = None,
    texture3_path: Optional[str] = None,
    emissionscale: float = 0.0,
    parallaxscale: float = 0.0,
    alphablend: bool = False
) -> bool:
    """
    Generate a VMT for the ExoPBR screenspace_general_8tex shader.

    Shader: screenspace_general_8tex

    Texture mapping:
    - $basetexture: Color (RGB), Alpha = opacity
    - $texture1: ARM map (R=AO, G=Roughness, B=Metallic, A=Height for parallax)
    - $texture2: Normal map (DirectX Y- format, ATI2N recommended)
    - $texture3: Emission texture (RGB), enabled with $emissionscale

    Parameters:
    - $emissionscale: Enables and scales emission from $texture3
    - $blendemissionbycolor: If emission is enabled, allows tinting by vertex color
    - $parallaxscale: Enables and scales parallax occlusion from $texture1 alpha
    - $alphablend: Enables partial opacity using base texture alpha
    """

    # Default texture paths fall back to {material_path}/{material_name}_suffix
    base_tex = basetexture_path or f"{material_path}/{material_name}_base"
    tex1 = texture1_path or f"{material_path}/{material_name}_arm"
    tex2 = texture2_path or f"{material_path}/{material_name}_normal"

    lines = [
        f'"screenspace_general_8tex"',
        "{",
        f'    "$basetexture" "{base_tex}"',
        f'    "$texture1" "{tex1}"',
        f'    "$texture2" "{tex2}"',
    ]

    # Add optional $texture3 for emission
    if texture3_path:
        lines.append(f'    "$texture3" "{texture3_path}"')

    # Add emission scale if non-zero
    if emissionscale and emissionscale != 0.0:
        lines.append(f'    "$emissionscale" {emissionscale}')
        lines.append('    "$blendemissionbycolor" 1')  # Enable color tinting of emission
    
    # Add parallax scale if non-zero
    if parallaxscale and parallaxscale != 0.0:
        lines.append(f'    "$parallaxscale" {parallaxscale}')

    # Add alphablend if enabled
    if alphablend:
        lines.append('    "$alphablend" 1')

    lines.extend(
        [
            "",
            '    "$model" 1',
            '    "$cull" 1',
            "",
            '    "Proxies" {',
            "        ExoPBR {}",
            "    }",
            "}",
        ]
    )

    content = "\n".join(lines) + "\n"

    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception as e:
        print(f"[Error] Failed to write ExoPBR VMT: {e}")
        return False
