"""
FakePBR Tool - Command Line Interface

Convert Source 2 PBR textures to Source 1 materials via CLI
"""

import argparse
import os
import sys
from pathlib import Path
import glob

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.fake_pbr_tool import FakePBRProcessor, PBRInputs, ProcessingOptions


def auto_detect_inputs(input_folder: str, stem: str) -> PBRInputs:
    """
    Auto-detect source images by suffix
    
    Looks for files matching:
    - {stem}_color.* or {stem}_albedo.*
    - {stem}_normal.*
    - {stem}_ao.* or {stem}_ambient.*
    - {stem}_rough.* or {stem}_roughness.*
    - {stem}_metal.* or {stem}_metallic.* or {stem}_metalness.*
    """
    inputs = PBRInputs()
    
    # Define search patterns
    patterns = {
        'color': [f"{stem}_color.*", f"{stem}_albedo.*", f"{stem}_diffuse.*"],
        'normal': [f"{stem}_normal.*", f"{stem}_norm.*"],
        'ao': [f"{stem}_ao.*", f"{stem}_ambient.*", f"{stem}_occlusion.*"],
        'roughness': [f"{stem}_rough.*", f"{stem}_roughness.*"],
        'metallic': [f"{stem}_metal.*", f"{stem}_metallic.*", f"{stem}_metalness.*"]
    }
    
    # Search for each type
    for input_type, pattern_list in patterns.items():
        for pattern in pattern_list:
            matches = glob.glob(os.path.join(input_folder, pattern))
            if matches:
                setattr(inputs, input_type, matches[0])
                print(f"[Auto-detect] Found {input_type}: {os.path.basename(matches[0])}")
                break
    
    return inputs


def process_batch(
    input_folder: str,
    output_folder: str,
    options: ProcessingOptions,
    material_path: str = "materials"
):
    """
    Process all materials in a folder
    
    Looks for sets of textures with common stems
    """
    print(f"\n{'='*60}")
    print(f"Batch Processing: {input_folder}")
    print(f"{'='*60}\n")
    
    # Find all potential base textures
    color_patterns = ["*_color.*", "*_albedo.*", "*_diffuse.*"]
    base_files = []
    
    for pattern in color_patterns:
        base_files.extend(glob.glob(os.path.join(input_folder, pattern)))
    
    if not base_files:
        print("[Error] No color/albedo textures found in input folder")
        return
    
    # Extract stems
    stems = set()
    for base_file in base_files:
        basename = os.path.basename(base_file)
        # Remove extension and suffix
        for suffix in ['_color', '_albedo', '_diffuse']:
            if suffix in basename:
                stem = basename.split(suffix)[0]
                stems.add(stem)
                break
    
    print(f"[Info] Found {len(stems)} material(s) to process\n")
    
    # Process each material
    success_count = 0
    fail_count = 0
    
    processor = FakePBRProcessor(options)
    
    for stem in sorted(stems):
        print(f"\n--- Processing: {stem} ---")
        
        # Auto-detect inputs
        inputs = auto_detect_inputs(input_folder, stem)
        
        if not inputs.color:
            print(f"[Warning] No color map found for {stem}, skipping")
            fail_count += 1
            continue
        
        if not inputs.normal:
            print(f"[Warning] No normal map found for {stem}, skipping")
            fail_count += 1
            continue
        
        # Process material
        success, message = processor.process_material(
            inputs,
            output_folder,
            stem,
            material_path
        )
        
        if success:
            print(message)
            success_count += 1
        else:
            print(f"[Error] {message}")
            fail_count += 1
    
    processor.shutdown()
    
    print(f"\n{'='*60}")
    print(f"Batch Complete: {success_count} succeeded, {fail_count} failed")
    print(f"{'='*60}\n")


def process_single(
    inputs: PBRInputs,
    output_folder: str,
    material_name: str,
    options: ProcessingOptions,
    material_path: str = "materials"
):
    """Process a single material"""
    print(f"\n{'='*60}")
    print(f"Processing Material: {material_name}")
    print(f"{'='*60}\n")
    
    processor = FakePBRProcessor(options)
    
    success, message = processor.process_material(
        inputs,
        output_folder,
        material_name,
        material_path
    )
    
    processor.shutdown()
    
    print(f"\n{message}\n")
    
    return 0 if success else 1


def main():
    parser = argparse.ArgumentParser(
        description="FakePBR Tool - Convert Source 2 PBR textures to Source 1 materials",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Process single material with explicit inputs
  fakepbr --color base.png --normal norm.png --out ./materials --stem my_mat
  
  # Batch process all materials in folder
  fakepbr --in ./textures --out ./materials --batch
  
  # Auto-detect inputs by stem
  fakepbr --in ./textures --out ./materials --stem metal_panel
  
  # Adjust processing options
  fakepbr --in ./textures --out ./materials --stem prop \
          --ao-strength 0.7 --gloss-gamma 2.5 --invert-green
        """
    )
    
    # Input options
    input_group = parser.add_argument_group('Input Options')
    input_group.add_argument('--in', '--input', dest='input_folder',
                           help='Input folder for batch processing or auto-detection')
    input_group.add_argument('--color', '--albedo',
                           help='Path to color/albedo map')
    input_group.add_argument('--normal',
                           help='Path to normal map')
    input_group.add_argument('--ao', '--ambient',
                           help='Path to ambient occlusion map')
    input_group.add_argument('--roughness', '--rough',
                           help='Path to roughness map')
    input_group.add_argument('--metallic', '--metal',
                           help='Path to metallic map')
    
    # Output options
    output_group = parser.add_argument_group('Output Options')
    output_group.add_argument('--out', '--output', dest='output_folder', required=True,
                            help='Output folder for generated files')
    output_group.add_argument('--stem', '--name',
                            help='Material name/stem for output files')
    output_group.add_argument('--material-path', default='materials',
                            help='Relative material path in VMT (default: materials)')
    output_group.add_argument('--target-branch', default='gmod',
                            choices=['hl2', 'source2013_sp', 'source2013_mp', 'gmod', 'gmod_x86_64', 'tf2', 'l4d2'],
                            help='Source 1 target branch for VMT feature gating (default: gmod)')
    output_group.add_argument('--envmap', default='env_cubemap',
                            help='VMT $envmap value (default: env_cubemap)')
    
    # Processing options
    proc_group = parser.add_argument_group('Processing Options')
    proc_group.add_argument('--ao-strength', type=float, default=0.7,
                          help='AO bake strength (default: 0.7)')
    proc_group.add_argument('--gloss-gamma', type=float, default=2.0,
                          help='Roughness-to-exponent gamma (default: 2.0)')
    proc_group.add_argument('--invert-green', action='store_true',
                          help='Invert normal map green channel (DirectX mode)')
    
    # Mode options
    parser.add_argument('--batch', action='store_true',
                       help='Batch process all materials in input folder')
    
    args = parser.parse_args()
    
    # Create processing options
    options = ProcessingOptions(
        ao_strength=args.ao_strength,
        gloss_gamma=args.gloss_gamma,
        invert_green=args.invert_green,
        target_branch=args.target_branch,
        envmap=args.envmap
    )
    
    # Determine mode
    if args.batch:
        # Batch mode
        if not args.input_folder:
            print("[Error] --in/--input is required for batch mode")
            return 1
        
        process_batch(
            args.input_folder,
            args.output_folder,
            options,
            args.material_path
        )
        return 0
    
    elif args.input_folder and args.stem:
        # Auto-detect mode
        inputs = auto_detect_inputs(args.input_folder, args.stem)
        
        if not inputs.color or not inputs.normal:
            print("[Error] Could not auto-detect required textures (color and normal)")
            print("Please specify inputs manually with --color and --normal")
            return 1
        
        return process_single(
            inputs,
            args.output_folder,
            args.stem,
            options,
            args.material_path
        )
    
    elif args.color and args.normal:
        # Manual mode
        if not args.stem:
            print("[Error] --stem/--name is required when specifying inputs manually")
            return 1
        
        inputs = PBRInputs(
            color=args.color,
            normal=args.normal,
            ao=args.ao,
            roughness=args.roughness,
            metallic=args.metallic
        )
        
        return process_single(
            inputs,
            args.output_folder,
            args.stem,
            options,
            args.material_path
        )
    
    else:
        parser.print_help()
        return 1


if __name__ == '__main__':
    sys.exit(main())
