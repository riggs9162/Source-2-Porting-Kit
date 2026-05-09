"""
VRF Export CLI

Batch-decompile Source 2 .vmdl_c files to GLTF/GLB plus a `materials/` tree
of VMATs and texture PNGs, stripping the stray decoded images that VRF drops
next to the .gltf.

Examples:
  # Single model
  vrf-export --in C:\\game\\models\\prop.vmdl_c --out C:\\out

  # Folder, recursive
  vrf-export --in C:\\game\\models --out C:\\out --threads 8

  # VPK + path filter (extract a subtree from a VPK archive)
  vrf-export --in S:\\HL-Alyx\\game\\hlvr\\pak01_dir.vpk \\
             --vpk-path "models/props_c17/" --out C:\\out

  # Override saved VRF path
  vrf-export --vrf C:\\tools\\Source2Viewer-CLI.exe --in foo.vmdl_c --out C:\\out
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `app.*` imports work whether this is run as a module or a script.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.utils.vrf_runner import (  # noqa: E402
    VrfRunnerError,
    resolve_vrf_executable,
    run_vrf_export,
)


def _load_saved_vrf_path() -> str:
    """Read settings.json directly — avoids pulling Qt into the CLI."""
    try:
        from app.core.settings import Settings  # noqa: WPS433
        return Settings().get_vrf_cli_path()
    except Exception:
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Decompile Source 2 models to GLTF + materials/ tree (no stray PNGs).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--in", "--input", dest="input_path", required=True,
                        help="Path to a .vmdl_c file, a folder, or a .vpk archive")
    parser.add_argument("--out", "--output", dest="output_dir", required=True,
                        help="Output folder")
    parser.add_argument("--vrf", dest="vrf_path", default=None,
                        help="Path to Source2Viewer-CLI.exe (overrides saved setting)")
    parser.add_argument("--vpk-path", dest="vpk_filepath", default=None,
                        help="Path prefix inside the VPK to extract (e.g. 'models/props_c17/'). "
                             "Only meaningful when --in is a .vpk archive.")
    parser.add_argument("--format", dest="gltf_format", default="glb",
                        choices=["glb", "gltf"],
                        help="GLTF format (default: glb)")
    parser.add_argument("--threads", type=int, default=4,
                        help="VRF worker threads (default: 4)")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false",
                        help="Disable recursive folder walk (only meaningful for folder input)")
    parser.add_argument("--keep-stray-images", action="store_true",
                        help="Keep the decoded PNG/TGA files VRF drops next to the .gltf")

    args = parser.parse_args()

    try:
        vrf_exe = resolve_vrf_executable(
            explicit=args.vrf_path,
            settings_path=_load_saved_vrf_path() or None,
        )
    except VrfRunnerError as e:
        print(f"[Error] {e}", file=sys.stderr)
        return 2

    print(f"[Info] Using VRF: {vrf_exe}")

    try:
        rc = run_vrf_export(
            vrf_exe=vrf_exe,
            input_path=Path(args.input_path),
            output_dir=Path(args.output_dir),
            gltf_format=args.gltf_format,
            recursive=args.recursive,
            threads=args.threads,
            keep_stray_images=args.keep_stray_images,
            vpk_filepath=args.vpk_filepath,
            on_log=print,
        )
    except VrfRunnerError as e:
        print(f"[Error] {e}", file=sys.stderr)
        return 2

    return rc


if __name__ == "__main__":
    sys.exit(main())
