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

  # Several exact models from one VPK (repeat --vpk-path)
  vrf-export --in S:\\HL-Alyx\\game\\hlvr\\pak01_dir.vpk --out C:\\out \\
             --vpk-path models/props/a.vmdl_c --vpk-path models/props/b.vmdl_c

  # VRF "Copy URL" strings (right-click a model in Source2Viewer); --in not needed.
  # The archive is embedded in the URL; repeat --url for several models.
  vrf-export --out C:\\out \\
             --url "vpk:S:/.../hlvr/pak01_dir.vpk:models/props/foo.vmdl_c"

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
    ExportJob,
    VrfRunnerError,
    build_export_jobs,
    is_vpk,
    resolve_vrf_executable,
    run_vrf_export,
)


def _job_label(job: ExportJob) -> str:
    name = job.input_path.name
    if not job.vpk_filepaths:
        return name
    if len(job.vpk_filepaths) == 1:
        return f"{name} : {job.vpk_filepaths[0]}"
    return f"{name} : {len(job.vpk_filepaths)} paths"


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
    parser.add_argument("--in", "--input", dest="input_path", default=None,
                        help="Path to a .vmdl_c file, a folder, or a .vpk archive. "
                             "Optional when --url is given.")
    parser.add_argument("--out", "--output", dest="output_dir", required=True,
                        help="Output folder")
    parser.add_argument("--vrf", dest="vrf_path", default=None,
                        help="Path to Source2Viewer-CLI.exe (overrides saved setting)")
    parser.add_argument("--vpk-path", dest="vpk_filepath", action="append", default=None,
                        help="Path prefix or exact file inside the VPK to extract "
                             "(e.g. 'models/props_c17/'). Repeatable. Only meaningful "
                             "when --in is a .vpk archive.")
    parser.add_argument("--url", dest="urls", action="append", default=None,
                        help="A VRF 'Copy URL' string "
                             "(vpk:<archive>.vpk:<path>). Repeatable; --in not required.")
    parser.add_argument("--kind", dest="mode", default="models",
                        choices=["models", "vmats"],
                        help="Port type: 'models' (.vmdl_c -> GLTF + materials, "
                             "the default) or 'vmats' (.vmat_c materials + textures only)")
    parser.add_argument("--format", dest="gltf_format", default="glb",
                        choices=["glb", "gltf"],
                        help="GLTF format (default: glb). Only used for --kind models.")
    parser.add_argument("--threads", type=int, default=4,
                        help="VRF worker threads (default: 4)")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false",
                        help="Disable recursive folder walk (only meaningful for folder input)")
    parser.add_argument("--keep-stray-images", action="store_true",
                        help="Keep the decoded PNG/TGA files VRF drops next to the .gltf")
    parser.add_argument("--flatten-materials-root", action="store_true",
                        help="Move ALL exported images and .vmat files into a flat "
                             "materialsrc/ root afterwards (overrides --keep-stray-images)")

    args = parser.parse_args()

    if not args.input_path and not args.urls:
        print("[Error] Provide --in and/or at least one --url.", file=sys.stderr)
        return 2

    # Assemble the entry list the same way the GUI does: --url strings and bare
    # --vpk-path entries, plus --in folded in (as its own loose/whole-archive
    # job, or as the .vpk context for the bare --vpk-path entries).
    entries: list = list(args.urls or [])
    entries += list(args.vpk_filepath or [])
    default_input = None
    if args.input_path:
        ip = Path(args.input_path)
        if is_vpk(ip):
            default_input = ip
            if not entries:                       # nothing specific → whole archive
                entries.append(args.input_path)
        else:
            if args.vpk_filepath:
                print("[Warn] --vpk-path ignored (--in is not a .vpk).", file=sys.stderr)
            entries.append(args.input_path)

    jobs, warnings = build_export_jobs(entries, default_input=default_input)
    for w in warnings:
        print(f"[Warn] {w}", file=sys.stderr)
    if not jobs:
        print("[Error] No valid models resolved from the given inputs.", file=sys.stderr)
        return 2

    try:
        vrf_exe = resolve_vrf_executable(
            explicit=args.vrf_path,
            settings_path=_load_saved_vrf_path() or None,
        )
    except VrfRunnerError as e:
        print(f"[Error] {e}", file=sys.stderr)
        return 2

    print(f"[Info] Using VRF: {vrf_exe}")
    print(f"[Info] {len(jobs)} export job(s):")
    for job in jobs:
        print(f"  - {_job_label(job)}")

    output_dir = Path(args.output_dir)
    worst_rc = 0
    for i, job in enumerate(jobs, start=1):
        label = _job_label(job)
        print(f"[Info] [{i}/{len(jobs)}] {label}")
        try:
            rc = run_vrf_export(
                vrf_exe=vrf_exe,
                input_path=job.input_path,
                output_dir=output_dir,
                gltf_format=args.gltf_format,
                recursive=args.recursive,
                threads=args.threads,
                keep_stray_images=args.keep_stray_images,
                flatten_materials_root=args.flatten_materials_root,
                mode=args.mode,
                vpk_filepath=job.vpk_filepaths or None,
                on_log=print,
            )
        except VrfRunnerError as e:
            print(f"[Error] {label}: {e}", file=sys.stderr)
            worst_rc = worst_rc or 2
            continue
        if rc != 0:
            worst_rc = rc

    return worst_rc


if __name__ == "__main__":
    sys.exit(main())
