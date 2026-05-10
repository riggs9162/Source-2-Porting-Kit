"""
ValveResourceFormat (Source2Viewer-CLI) runner.

Wraps the VRF CLI to export a single .vmdl_c file or a folder of compiled
Source 2 assets to GLTF/GLB plus a `materials/` tree (VMATs and their texture
PNGs). Strips the few "stray" decoded image files VRF drops next to the .gltf.

The user picks a project/addon root as `-o`; the tool produces a Source 1
sibling layout inside it:

    <project>/
      modelsrc/models/myprop/myprop.gltf
      materialsrc/models/myprop/myprop.vmat
      materialsrc/models/myprop/*.png

Image files sitting in the same folder as a .gltf/.glb (VRF's stray decoded
images) are deleted before reorganization; anything under `materials/` is
left alone.

This module is intentionally Qt-free so it can back both the GUI tool and the
standalone CLI script.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple


IMAGE_SUFFIXES = {".png", ".tga", ".jpg", ".jpeg", ".exr", ".hdr"}
GLTF_SUFFIXES = {".gltf", ".glb"}
# Source 2 compiled-texture artifacts. Useless once the .png is decoded —
# the Source 1 porting pipeline reads the .png and writes .vtf, so anything
# in this set just clutters materialsrc/.
COMPILED_TEXTURE_SUFFIXES = {".vtex", ".vtex_c"}
LogFn = Callable[[str], None]

# Match Source 2 resource paths in VRF's `-b RERL` stdout.
# References are written with their source extension (e.g. ".vmat", ".vtex"),
# not the compiled "_c" suffix.
_RERL_PATH_RE = re.compile(
    r"([A-Za-z][\w./\-]*?/[\w./\-]+\.(?:vmat|vtex|vmdl|vmesh|vphys|vsnd|vsndevts))",
    re.IGNORECASE,
)

# Capture any quoted path-shaped string in a .vmat that ends in an image or
# vtex extension — covers both `"TextureColor" "...png"` and the `g_t*` keys
# under the `"Compiled Textures"` block (whose values are hash-suffix .vtex
# paths matching the actual on-disk filenames).
_VMAT_TEXTURE_RE = re.compile(
    r'"([^"\s][^"]*?\.(?:png|tga|jpg|jpeg|exr|hdr|vtex))"',
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

    # `--gltf_export_materials` is required to get a `materials` array in the
    # glTF with proper vmat names — without it VRF emits a mesh with no
    # material reference at all, and the SMD converter falls back to a literal
    # "material" name (which then breaks the downstream MDL's $cdmaterials
    # lookup). The flag's downside is hash-suffix duplicate PNGs and channel-
    # split textures dropped next to the .gltf — those get cleaned up by
    # `purge_stray_images_next_to_gltf` post-extraction.
    args: List[str] = [
        str(vrf_exe),
        "-i", str(input_path),
        "-o", str(output_dir),
        "-d",
        "--gltf_export_format", gltf_format,
        "--gltf_export_animations",
        "--gltf_export_materials",
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


# Per-file header in `-b DATA` stdout: `[2/3] models/.../foo.vmdl_c`.
_DATA_FILE_HEADER_RE = re.compile(
    r'^\[\d+/\d+\]\s+(\S+\.vmdl_c)\s*$',
    re.MULTILINE,
)
# Each `m_materialGroups` entry: `m_name = "..."` followed shortly by
# `m_materials = [ resource:"...", ... ]`. Greedy-but-bounded.
_MATERIAL_GROUP_RE = re.compile(
    r'm_name\s*=\s*"(?P<name>[^"]*)"[^{]*?'
    r'm_materials\s*=\s*\[(?P<mats>[^\]]*)\]',
    re.DOTALL,
)
_RESOURCE_REF_RE = re.compile(r'resource:"([^"]+)"')


def _extract_material_groups_block(text: str) -> List[Tuple[str, List[str]]]:
    """Find the `m_materialGroups = [...]` array in a single file's data
    block and return the per-group `(name, [vmat_refs])` tuples.

    Bracket-depth-aware so nested `m_materials = [...]` arrays don't fool
    us into stopping early.
    """
    start = text.find("m_materialGroups")
    if start < 0:
        return []
    bracket_start = text.find("[", start)
    if bracket_start < 0:
        return []
    depth = 1
    i = bracket_start + 1
    while i < len(text) and depth > 0:
        c = text[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                break
        i += 1
    block = text[bracket_start + 1:i]
    groups: List[Tuple[str, List[str]]] = []
    for m in _MATERIAL_GROUP_RE.finditer(block):
        name = m.group("name")
        mats = _RESOURCE_REF_RE.findall(m.group("mats"))
        groups.append((name, mats))
    return groups


def _parse_material_groups_per_file(text: str) -> Dict[str, List[Tuple[str, List[str]]]]:
    """Split `-b DATA` multi-file stdout by `[N/M] <path>` headers and
    extract material groups from each section. Paths are normalized to
    forward slashes.
    """
    sections: Dict[str, str] = {}
    cur_path: Optional[str] = None
    buf: List[str] = []
    for line in text.splitlines():
        m = _DATA_FILE_HEADER_RE.match(line)
        if m:
            if cur_path is not None:
                sections[cur_path] = "\n".join(buf)
            cur_path = m.group(1).replace("\\", "/")
            buf = []
        elif cur_path is not None:
            buf.append(line)
    if cur_path is not None:
        sections[cur_path] = "\n".join(buf)

    result: Dict[str, List[Tuple[str, List[str]]]] = {}
    for path, body in sections.items():
        groups = _extract_material_groups_block(body)
        if groups:
            result[path] = groups
    return result


def dump_material_groups(
    vrf_exe: Path,
    input_path: Path,
    vpk_filepath: str,
    on_log: LogFn = print,
) -> Dict[str, List[Tuple[str, List[str]]]]:
    """Run VRF `-b DATA` on .vmdl_c entries matching the filter and parse
    out each model's `m_materialGroups`. Returns a `{vmdl_path: groups}`
    map where `groups` is `[(group_name, [vmat_refs]), ...]`. The first
    group is always the default — Source 2 convention.

    No-op-safe: returns `{}` on non-zero exit and logs the failure.
    """
    args = [
        str(vrf_exe),
        "-i", str(input_path),
        "-f", vpk_filepath,
        "-e", "vmdl_c",
        "-b", "DATA",
    ]
    on_log("Dumping material groups via `-b DATA`...")
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
        on_log(f"Material-groups dump failed to launch: {e}")
        return {}
    if proc.returncode != 0:
        on_log(f"Material-groups dump exited with code {proc.returncode}; continuing without skin info")
        return {}
    return _parse_material_groups_per_file(proc.stdout or "")


def _vmat_stem(ref: str) -> str:
    """Basename of a vmat reference without directory or `.vmat` extension."""
    p = ref.replace("\\", "/").strip().rstrip("/")
    sep = p.rfind("/")
    if sep >= 0:
        p = p[sep + 1:]
    if p.lower().endswith(".vmat"):
        p = p[:-5]
    return p


def write_skin_sidecars(
    staging_dir: Path,
    material_groups: Dict[str, List[Tuple[str, List[str]]]],
    on_log: LogFn = print,
) -> int:
    """For each .vmdl_c with multiple material groups, write a JSON sidecar
    at `<staging>/<vmdl_path_stem>.skins.json` — next to the .gltf VRF will
    drop. Models with one or zero groups are skipped (no skins to declare).

    Returns the number of sidecars written.
    """
    written = 0
    for vmdl_path, groups in material_groups.items():
        if len(groups) <= 1:
            continue
        base = vmdl_path
        for ext in (".vmdl_c", ".vmdl"):
            if base.lower().endswith(ext):
                base = base[: -len(ext)]
                break
        sidecar = staging_dir / (base + ".skins.json")
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "skins.v1",
            "skins": [
                {
                    "name": name,
                    "materials": [_vmat_stem(m) for m in mats],
                }
                for name, mats in groups
            ],
        }
        sidecar.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        written += 1
    on_log(f"Wrote {written} skin sidecar(s)")
    return written


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


def hoist_inner_materials_to_top(staging_dir: Path, on_log: LogFn = print) -> int:
    """Move every `<staging>/models/<rest>/materials/<...>` tree up to
    `<staging>/materials/models/<rest>/<...>`.

    Some Source 2 games (Half-Life Alyx) ship materials inside each model's
    own folder rather than the parallel `materials/<model_path>/` tree. VRF
    extracts them where they live in the VPK, so they land under `models/`
    in our staging dir. Without this hoist they'd either:
      - end up under `modelsrc/.../materials/` (wrong tree), or
      - get rmtree'd by `purge_inner_materials_dirs` (data loss).

    Run this before reorganize so the existing materials-tree move picks them
    up. Returns the number of files actually relocated.
    """
    models_root = staging_dir / "models"
    if not models_root.is_dir():
        return 0
    materials_top = staging_dir / "materials"
    targets = [p for p in models_root.rglob("materials") if p.is_dir()]
    moved_total = 0
    for src in targets:
        # rel_parent is the model's path relative to staging, e.g.
        # "models/props_combine/combine_lockers". The dest mirrors that under
        # materials/, giving "<staging>/materials/models/props_combine/...".
        rel_parent = src.parent.relative_to(staging_dir)
        dest = materials_top / rel_parent
        moved_total += _move_tree_into(src, dest, on_log)
    if moved_total:
        on_log(f"Hoisted {moved_total} file(s) from inner models/.../materials/ to top-level materials/")
    return moved_total


def reorganize_to_project_layout(
    staging_dir: Path,
    output_dir: Path,
    on_log: LogFn = print,
) -> None:
    """Lay VRF's staged output out as a Source 1 addon project root:

        <staging>/models/<rest>     -> <output>/modelsrc/<rest>
        <staging>/materials/<rest>  -> <output>/materialsrc/<rest>

    Source and target are separate so we don't sweep up the user's existing
    `<output>/models/` or `<output>/materials/` (compiled MDL/VTF/VMT trees
    that live alongside their source counterparts in a Source 1 addon).

    The inner `models/` and `materials/` levels are dropped — redundant under
    folders already named `modelsrc/` and `materialsrc/`. Merges into existing
    target folders if they're already there.
    """
    models_src = staging_dir / "models"
    if models_src.is_dir():
        target = output_dir / "modelsrc"
        moved = _move_tree_into(models_src, target, on_log)
        on_log(f"Reorganized {moved} model file(s) -> {target}")
    else:
        on_log("No models/ directory to reorganize")

    materials_src = staging_dir / "materials"
    if materials_src.is_dir():
        target = output_dir / "materialsrc"
        moved = _move_tree_into(materials_src, target, on_log)
        on_log(f"Reorganized {moved} material file(s) -> {target}")
    else:
        on_log("No materials/ directory to reorganize")


def purge_inner_materials_dirs(modelsrc_dir: Path, on_log: LogFn = print) -> int:
    """Recursively rmtree any folder literally named `materials` under
    `modelsrc_dir`. VRF sometimes writes a per-model `materials/` next to
    each .gltf — those are duplicates of the canonical materials tree we've
    already moved to `materialsrc/`, so they're pure noise.

    Returns the number of directories removed.
    """
    if not modelsrc_dir.is_dir():
        return 0
    targets = [p for p in modelsrc_dir.rglob("materials") if p.is_dir()]
    removed = 0
    for d in targets:
        try:
            shutil.rmtree(d)
            removed += 1
        except OSError as e:
            on_log(f"Could not remove {d}: {e}")
    on_log(f"Removed {removed} stray materials/ folder(s) inside {modelsrc_dir.name}/")
    return removed


def _normalize_vmat_ref(ref: str) -> Optional[Tuple[str, str]]:
    """Convert a vmat texture reference to a `(dir, stem)` tuple lowercase,
    matching the layout of files in `materialsrc/`.

    Strips every `materials/` segment from the path: the leading one for
    parallel-tree references like `materials/models/x/foo.vtex`, and any
    mid-path one for Alyx-style inline references like
    `models/x/y/materials/foo.png` (the hoist drops that segment when moving
    files into `materialsrc/`). The extension is dropped so a `.vtex`
    reference matches the `.png` VRF actually decoded.

    Returns None for unusable inputs.
    """
    p = ref.replace("\\", "/").strip().strip('"').lower().lstrip("/")
    # Repeatedly strip `materials/` wherever it appears as a path segment.
    while True:
        if p.startswith("materials/"):
            p = p[len("materials/"):]
        elif "/materials/" in p:
            p = p.replace("/materials/", "/", 1)
        else:
            break
    dot = p.rfind(".")
    if dot > 0:
        p = p[:dot]
    if not p:
        return None
    sep = p.rfind("/")
    if sep < 0:
        return ("", p)
    return (p[:sep], p[sep + 1:])


def _collect_vmat_references(materialsrc: Path) -> Set[Tuple[str, str]]:
    """Walk every .vmat under `materialsrc`, regex out the `Texture*` paths,
    return the set of (dir, stem) keys (relative to materialsrc, lowercased).
    """
    refs: Set[Tuple[str, str]] = set()
    for vmat in materialsrc.rglob("*.vmat"):
        if not vmat.is_file():
            continue
        try:
            text = vmat.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in _VMAT_TEXTURE_RE.findall(text):
            key = _normalize_vmat_ref(raw)
            if key is not None:
                refs.add(key)
    return refs


def purge_unreferenced_textures(materialsrc_dir: Path, on_log: LogFn = print) -> int:
    """Delete image files under `materialsrc_dir` that aren't referenced by
    any .vmat in the same tree.

    Match key is `(relative_dir, stem)` lowercase — extension-agnostic, so
    a vmat reference to `foo.vtex` matches a `foo.png` on disk (VRF decodes
    the texture format on extraction). .vmat files themselves are never
    deleted — only image files (`IMAGE_SUFFIXES`).

    Returns the number of files deleted. No-op when the dir has no .vmat
    files (treats empty reference set as "unknown, don't delete anything").
    """
    if not materialsrc_dir.is_dir():
        return 0
    refs = _collect_vmat_references(materialsrc_dir)
    if not refs:
        on_log("No .vmat references found; skipping unreferenced-texture purge")
        return 0
    deleted = 0
    for img in materialsrc_dir.rglob("*"):
        if not img.is_file():
            continue
        if img.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        rel = img.relative_to(materialsrc_dir).as_posix().lower()
        dot = rel.rfind(".")
        stem_path = rel[:dot] if dot > 0 else rel
        sep = stem_path.rfind("/")
        key = (stem_path[:sep], stem_path[sep + 1:]) if sep >= 0 else ("", stem_path)
        if key in refs:
            continue
        try:
            img.unlink()
            deleted += 1
        except OSError as e:
            on_log(f"Could not delete {img}: {e}")
    on_log(f"Removed {deleted} unreferenced texture file(s) from {materialsrc_dir.name}/")
    return deleted


_COMPILED_TEXTURES_BLOCK_RE = re.compile(
    r'"Compiled Textures"\s*\{([^{}]*)\}',
    re.IGNORECASE | re.DOTALL,
)


def purge_compiled_texture_images(materialsrc_dir: Path, on_log: LogFn = print) -> int:
    """Delete the image files referenced by each .vmat's `"Compiled Textures"`
    block. Those entries point at hash-suffix `.vtex` paths (the compiled
    form), which VRF decodes to hash-suffix `.png` files alongside the clean-
    name versions referenced by `Texture*` keys. Both PNGs decode from the
    same vtex_c, so the hash-suffix copies are duplicates of the canonical
    textures the Source 1 pipeline actually consumes.

    Returns the number of files deleted.
    """
    if not materialsrc_dir.is_dir():
        return 0
    targets: Set[Tuple[str, str]] = set()
    for vmat in materialsrc_dir.rglob("*.vmat"):
        if not vmat.is_file():
            continue
        try:
            text = vmat.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for block in _COMPILED_TEXTURES_BLOCK_RE.findall(text):
            for raw in _VMAT_TEXTURE_RE.findall(block):
                key = _normalize_vmat_ref(raw)
                if key is not None:
                    targets.add(key)
    if not targets:
        on_log("No 'Compiled Textures' references found")
        return 0
    deleted = 0
    for img in materialsrc_dir.rglob("*"):
        if not img.is_file():
            continue
        if img.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        rel = img.relative_to(materialsrc_dir).as_posix().lower()
        dot = rel.rfind(".")
        stem_path = rel[:dot] if dot > 0 else rel
        sep = stem_path.rfind("/")
        key = (stem_path[:sep], stem_path[sep + 1:]) if sep >= 0 else ("", stem_path)
        if key not in targets:
            continue
        try:
            img.unlink()
            deleted += 1
        except OSError as e:
            on_log(f"Could not delete {img}: {e}")
    on_log(f"Deleted {deleted} image file(s) referenced in 'Compiled Textures' blocks")
    return deleted


def purge_compiled_textures(materialsrc_dir: Path, on_log: LogFn = print) -> int:
    """Delete `.vtex` / `.vtex_c` files anywhere under `materialsrc_dir`.

    These are the compiled-texture descriptors vmats reference (e.g.
    `"TextureColor" "materials/.../foo.vtex"`). The Source 1 porting pipeline
    consumes the decoded `.png` instead, so these are dead weight.

    Returns the number of files deleted.
    """
    if not materialsrc_dir.is_dir():
        return 0
    deleted = 0
    for f in materialsrc_dir.rglob("*"):
        if not f.is_file():
            continue
        if f.suffix.lower() not in COMPILED_TEXTURE_SUFFIXES:
            continue
        try:
            f.unlink()
            deleted += 1
        except OSError as e:
            on_log(f"Could not delete {f}: {e}")
    on_log(f"Removed {deleted} compiled-texture file(s) from {materialsrc_dir.name}/")
    return deleted


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

    # VRF gets its own staging folder under the project root so it never
    # touches the user's existing `<output>/models/` or `<output>/materials/`
    # (compiled MDL/VTF/VMT trees in a Source 1 addon). Same filesystem as the
    # final destination, so the post-extraction moves are O(1) renames.
    staging_dir = output_dir / ".vrf_staging"
    if staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)
    staging_dir.mkdir(parents=True, exist_ok=True)

    extra_filters: List[str] = []
    if is_vpk(input_path) and vpk_filepath:
        on_log("Resolving model references via `-b RERL`...")
        first_refs = dump_rerl(
            vrf_exe=vrf_exe,
            input_path=input_path,
            vpk_filepath=expand_vpk_filter(vpk_filepath),
            entity_filter="vmdl_c",
            on_log=on_log,
        )
        # Recurse one level: RERL each discovered .vmat to find its .vtex
        # textures by exact path. Without this, we'd have to fall back to
        # parent-dir filters and accidentally pull every unrelated texture
        # sharing the .vmat's folder.
        vmat_refs = [r for r in first_refs if r.lower().endswith(".vmat")]
        second_refs: List[str] = []
        if vmat_refs:
            on_log(f"Resolving texture references from {len(vmat_refs)} material(s)...")
            second_refs = dump_rerl(
                vrf_exe=vrf_exe,
                input_path=input_path,
                vpk_filepath=",".join(vmat_refs),
                entity_filter="vmat_c",
                on_log=on_log,
            )
        seen: set = set()
        all_refs: List[str] = []
        for r in first_refs + second_refs:
            if r not in seen:
                seen.add(r)
                all_refs.append(r)
        if all_refs:
            on_log(f"Found {len(all_refs)} referenced resource(s):")
            for r in all_refs:
                on_log(f"  - {r}")
            extra_filters = all_refs
        else:
            on_log("No references resolved; falling back to parallel-path filter only.")

    args = build_vrf_command(
        vrf_exe=vrf_exe,
        input_path=input_path,
        output_dir=staging_dir,
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
        purge_stray_images_next_to_gltf(staging_dir, on_log)
    else:
        on_log("Skipping stray-image cleanup (keep_stray_images=True)")

    # Source 2 material groups → Source 1 $texturegroup. Dump them now (while
    # still operating against the VPK) and write JSON sidecars next to each
    # .gltf in staging so reorganize carries them into modelsrc/ for the SMD
    # batch tool to consume.
    if is_vpk(input_path) and vpk_filepath:
        mg = dump_material_groups(
            vrf_exe=vrf_exe,
            input_path=input_path,
            vpk_filepath=expand_vpk_filter(vpk_filepath),
            on_log=on_log,
        )
        if mg:
            write_skin_sidecars(staging_dir, mg, on_log)

    # Half-Life Alyx-style materials live inside each model's folder; route
    # them into the top-level materials/ before reorganize so they end up in
    # materialsrc/ instead of being purged with the stray inner materials/.
    hoist_inner_materials_to_top(staging_dir, on_log)
    reorganize_to_project_layout(staging_dir, output_dir, on_log)
    shutil.rmtree(staging_dir, ignore_errors=True)

    purge_inner_materials_dirs(output_dir / "modelsrc", on_log)
    purge_unreferenced_textures(output_dir / "materialsrc", on_log)
    purge_compiled_texture_images(output_dir / "materialsrc", on_log)
    purge_compiled_textures(output_dir / "materialsrc", on_log)
    return rc
