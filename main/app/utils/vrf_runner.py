"""
ValveResourceFormat (Source2Viewer-CLI) runner.

Wraps the VRF CLI to export a single .vmdl_c file or a folder of compiled
Source 2 assets to GLTF/GLB plus a `materials/` tree (VMATs and their texture
PNGs). Strips the few "stray" decoded image files VRF drops next to the .gltf.

The desired end state per model is:

    output/
      models/myprop/myprop.gltf            <- model (only this; no .vmdl text)
      materials/models/myprop/myprop.vmat
      materials/models/myprop/*.png

Image files sitting in the same folder as a .gltf/.glb are deleted; anything
under `materials/` is left alone.

This module is intentionally Qt-free so it can back both the GUI tool and the
standalone CLI script.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable, List, Optional


IMAGE_SUFFIXES = {".png", ".tga", ".jpg", ".jpeg", ".exr", ".hdr"}
GLTF_SUFFIXES = {".gltf", ".glb"}
LogFn = Callable[[str], None]

# Match Source 2 resource paths in VRF's `-b RERL` stdout.
# References are written with their source extension (e.g. ".vmat", ".vtex"),
# not the compiled "_c" suffix.
_RERL_PATH_RE = re.compile(
    r"([A-Za-z][\w./\-]*?/[\w./\-]+\.(?:vmat|vtex|vmdl|vmesh|vphys|vsnd|vsndevts))",
    re.IGNORECASE,
)


class VrfRunnerError(RuntimeError):
    """Raised for setup failures (missing binary, bad input, etc.)."""


def _looks_like_cli_binary(path: Path) -> bool:
    """
    Source2Viewer ships two binaries: the GUI (Source2Viewer.exe) and the CLI
    (Source2Viewer-CLI.exe). Pointing this tool at the GUI produces confusing
    "File '-i' does not exist" output because the GUI treats each argv token
    as a file to open. Filename-based heuristic rejects the obvious mistake.
    """
    return "cli" in path.stem.lower()


def resolve_vrf_executable(
    explicit: Optional[str | os.PathLike] = None,
    settings_path: Optional[str | os.PathLike] = None,
) -> Path:
    """
    Resolution order: explicit arg -> settings_path -> PATH lookup.

    Raises VrfRunnerError if none of those resolve to an existing CLI binary.
    """
    candidates: List[Optional[str]] = [
        str(explicit) if explicit else None,
        str(settings_path) if settings_path else None,
    ]
    for c in candidates:
        if c:
            p = Path(c)
            if p.is_file():
                if not _looks_like_cli_binary(p):
                    raise VrfRunnerError(
                        f"'{p.name}' looks like the GUI build of Source 2 Viewer. "
                        f"This tool needs the command-line build (Source2Viewer-CLI.exe), "
                        f"which is a separate download on the VRF releases page: "
                        f"https://github.com/ValveResourceFormat/ValveResourceFormat/releases"
                    )
                return p

    found = shutil.which("Source2Viewer-CLI") or shutil.which("Source2Viewer-CLI.exe")
    if found:
        return Path(found)

    raise VrfRunnerError(
        "Could not locate Source2Viewer-CLI.exe. Pass an explicit path, set "
        "'vrf_cli_path' in settings, or put it on your PATH. Note: this is "
        "the CLI build, a separate download from the GUI Source2Viewer.exe."
    )


def is_vpk(p: Path) -> bool:
    return p.suffix.lower() == ".vpk"


def expand_vpk_filter(vpk_filepath: str) -> str:
    """Auto-add the parallel `materials/<path>` so VMATs + textures get pulled.

    Source convention: a model at `models/X/foo.vmdl_c` references materials
    under `materials/models/X/...`. VRF won't write source `.vmat` files
    unless `vmat_c` is iterated as a primary target via `-e`/`-f`, so we
    extend the user's `-f` filter to cover both subtrees.

    No-op when the user already supplied a non-`models/` path.
    """
    p = vpk_filepath.replace("\\", "/").lstrip("/")
    paths = [p]
    if p.startswith("models/"):
        paths.append("materials/" + p)
    return ",".join(paths)


def build_vrf_command(
    vrf_exe: Path,
    input_path: Path,
    output_dir: Path,
    gltf_format: str = "glb",
    recursive: bool = True,
    threads: int = 4,
    vpk_filepath: Optional[str] = None,
    extra_filters: Optional[List[str]] = None,
) -> List[str]:
    """Compose the Source2Viewer-CLI argv. Pure function — easy to unit-test.

    `vpk_filepath` is the VRF `-f` filter — a path prefix inside the VPK
    (e.g. "models/props_c17/"). Only meaningful when `input_path` is a .vpk.

    `extra_filters` is a list of additional path prefixes to merge into `-f`
    (e.g. parent dirs of references discovered via a `-b RERL` pre-pass).
    """
    if gltf_format not in {"gltf", "glb"}:
        raise VrfRunnerError(f"gltf_format must be 'gltf' or 'glb' (got {gltf_format!r})")

    # NOTE: deliberately NOT passing --gltf_export_materials. That flag pulls
    # textures via VRF's glTF embedding pipeline, which writes:
    #   - hash-suffix duplicates of every texture (e.g. `foo_color_png_<hash>.png`)
    #   - glTF-adapted channel splits (e.g. `foo_<hash>_metal.png`)
    # Our `-e vtex_c` already extracts the canonical PNGs, and the user's
    # downstream pipeline reads materials from the .vmat files directly.
    args: List[str] = [
        str(vrf_exe),
        "-i", str(input_path),
        "-o", str(output_dir),
        "-d",
        "--gltf_export_format", gltf_format,
        "--threads", str(threads),
        "-e", "vmdl_c,vmat_c,vtex_c",
    ]
    if recursive and input_path.is_dir():
        args.append("--recursive")
    if is_vpk(input_path) and vpk_filepath:
        merged = _merge_filters([expand_vpk_filter(vpk_filepath)], extra_filters or [])
        args += ["-f", ",".join(merged)]
    return args


def _merge_filters(*filter_groups: Iterable[str]) -> List[str]:
    """Flatten + dedupe `-f` filter strings, preserving first-seen order."""
    seen: set = set()
    out: List[str] = []
    for group in filter_groups:
        for raw in group:
            for token in str(raw).split(","):
                t = token.strip().replace("\\", "/")
                if t and t not in seen:
                    seen.add(t)
                    out.append(t)
    return out


def _parse_rerl_paths(stdout: str) -> List[str]:
    """Extract referenced resource paths from a `-b RERL` stdout dump.

    VRF prints each reference on its own line; we match anything that looks
    like a Source 2 resource path with a known extension. Order-preserving
    dedupe.
    """
    seen: set = set()
    out: List[str] = []
    for match in _RERL_PATH_RE.findall(stdout):
        norm = match.replace("\\", "/")
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def _parent_dir(path: str) -> str:
    """Forward-slash directory part with trailing slash; '' if no slash."""
    p = path.replace("\\", "/")
    idx = p.rfind("/")
    return p[: idx + 1] if idx >= 0 else ""


def dump_rerl(
    vrf_exe: Path,
    input_path: Path,
    vpk_filepath: str,
    entity_filter: str = "vmdl_c",
    on_log: LogFn = print,
) -> List[str]:
    """Run VRF with `-b RERL` to dump external references for the matching
    entries inside a VPK. Returns deduped reference paths (with source
    extensions like `.vmat`, not `.vmat_c`).

    No-op-safe: returns [] on non-zero exit and logs the failure.
    """
    args = [
        str(vrf_exe),
        "-i", str(input_path),
        "-f", vpk_filepath,
        "-e", entity_filter,
        "-b", "RERL",
    ]
    on_log("Pre-pass: " + " ".join(_quote(a) for a in args))
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as e:
        on_log(f"RERL pre-pass failed to launch: {e}")
        return []
    if proc.returncode != 0:
        on_log(f"RERL pre-pass exited with code {proc.returncode}; continuing without references")
        return []
    return _parse_rerl_paths(proc.stdout or "")


def _stream_subprocess(args: List[str], on_log: LogFn) -> int:
    """Run a subprocess and forward each stdout line to on_log. Returns rc."""
    on_log("Running: " + " ".join(_quote(a) for a in args))
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        on_log(line.rstrip())
    return proc.wait()


def _quote(arg: str) -> str:
    return f'"{arg}"' if " " in arg else arg


def purge_stray_images_next_to_gltf(root: Path, on_log: LogFn = print) -> int:
    """
    Delete image files in the same folder as any .gltf/.glb. Non-recursive
    per folder. Files under any folder named 'materials' are never touched.

    Returns the number of files deleted.
    """
    if not root.exists():
        return 0

    gltf_dirs = {
        p.parent
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in GLTF_SUFFIXES
    }
    deleted = 0
    for d in gltf_dirs:
        if _under_materials(d):
            continue
        for entry in d.iterdir():
            if entry.is_file() and entry.suffix.lower() in IMAGE_SUFFIXES:
                try:
                    entry.unlink()
                    deleted += 1
                except OSError as e:
                    on_log(f"Could not delete {entry}: {e}")
    on_log(f"Removed {deleted} stray image file(s) next to GLTF outputs")
    return deleted


def _under_materials(p: Path) -> bool:
    """True if any path component is exactly 'materials' (case-insensitive)."""
    return any(part.lower() == "materials" for part in p.parts)


def _move_tree_into(src_dir: Path, target_dir: Path, on_log: LogFn) -> int:
    """Move every file under `src_dir` into `target_dir`, preserving relative
    structure and overwriting on conflict. Removes `src_dir` when done.

    Returns the count of files actually moved.
    """
    if not src_dir.is_dir():
        return 0
    target_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for src_file in list(src_dir.rglob("*")):
        if not src_file.is_file():
            continue
        rel = src_file.relative_to(src_dir)
        dest = target_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            if dest.exists():
                dest.unlink()
            shutil.move(str(src_file), str(dest))
            moved += 1
        except OSError as e:
            on_log(f"Could not move {src_file}: {e}")
    shutil.rmtree(src_dir, ignore_errors=True)
    return moved


def reorganize_materials_to_sibling(
    output_dir: Path,
    on_log: LogFn = print,
) -> Optional[Path]:
    """Move `<output>/materials/<rest>` -> `<output>.parent/materialsrc/<rest>`.

    Source 1 convention: model sources live under `modelsrc/`, materials live
    under a sibling `materialsrc/`. VRF writes everything under `-o`, so we
    relocate the materials tree post-extraction. The inner `materials/` level
    is dropped — it's redundant under a folder already named `materialsrc/`.

    Merges into an existing `materialsrc/` if one is already there. No-op when
    `<output>/materials/` doesn't exist.
    """
    src = output_dir / "materials"
    if not src.is_dir():
        on_log("No materials/ directory to reorganize")
        return None

    target = output_dir.parent / "materialsrc"
    moved = _move_tree_into(src, target, on_log)
    on_log(f"Reorganized {moved} material file(s) -> {target}")
    return target


def flatten_models_root(output_dir: Path, on_log: LogFn = print) -> bool:
    """Move `<output>/models/<rest>` -> `<output>/<rest>`.

    Drops the redundant `models/` level when the output is already named like
    `modelsrc/`. No-op when `<output>/models/` doesn't exist.
    """
    src = output_dir / "models"
    if not src.is_dir():
        on_log("No models/ directory to flatten")
        return False
    moved = _move_tree_into(src, output_dir, on_log)
    on_log(f"Flattened {moved} model file(s) into {output_dir}")
    return True


def run_vrf_export(
    vrf_exe: Path,
    input_path: Path,
    output_dir: Path,
    gltf_format: str = "glb",
    recursive: bool = True,
    threads: int = 4,
    keep_stray_images: bool = False,
    vpk_filepath: Optional[str] = None,
    on_log: LogFn = print,
) -> int:
    """
    Run a full export: GLTF + materials/ tree, then optionally clean up stray
    image files next to the .gltf outputs.

    `vpk_filepath` is a path prefix inside a VPK (e.g. "models/props_c17/")
    — only used when `input_path` is a .vpk file.

    Returns the VRF process exit code (0 on success).
    """
    if not vrf_exe.is_file():
        raise VrfRunnerError(f"VRF executable not found: {vrf_exe}")
    if not input_path.exists():
        raise VrfRunnerError(f"Input path does not exist: {input_path}")

    output_dir.mkdir(parents=True, exist_ok=True)

    extra_filters: List[str] = []
    if is_vpk(input_path) and vpk_filepath:
        on_log("Resolving model references via `-b RERL`...")
        refs = dump_rerl(
            vrf_exe=vrf_exe,
            input_path=input_path,
            vpk_filepath=expand_vpk_filter(vpk_filepath),
            entity_filter="vmdl_c",
            on_log=on_log,
        )
        if refs:
            on_log(f"Found {len(refs)} referenced resource(s):")
            for r in refs:
                on_log(f"  - {r}")
            extra_filters = sorted({_parent_dir(r) for r in refs if _parent_dir(r)})
        else:
            on_log("No references resolved; falling back to parallel-path filter only.")

    args = build_vrf_command(
        vrf_exe=vrf_exe,
        input_path=input_path,
        output_dir=output_dir,
        gltf_format=gltf_format,
        recursive=recursive,
        threads=threads,
        vpk_filepath=vpk_filepath,
        extra_filters=extra_filters,
    )
    rc = _stream_subprocess(args, on_log)
    if rc != 0:
        on_log(f"VRF exited with code {rc}")
        return rc

    if not keep_stray_images:
        purge_stray_images_next_to_gltf(output_dir, on_log)
    else:
        on_log("Skipping stray-image cleanup (keep_stray_images=True)")

    reorganize_materials_to_sibling(output_dir, on_log)
    flatten_models_root(output_dir, on_log)
    return rc
