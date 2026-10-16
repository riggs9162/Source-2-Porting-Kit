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

Ported from the v2 tool (`v2/app/utils/vrf_runner.py`) with the process calls
rewired onto :mod:`s2kit.core.process`, which adds cancellation: setting the
job's cancel event kills the VRF process instead of waiting for it to finish.
The parsing, filtering and reorganisation logic is unchanged - it is proven
against real Half-Life: Alyx exports.

Framework-free by design: no Flet, no Qt. It backs the GUI tool, a headless
CLI, and the tests equally.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple, Union

from s2kit.core import process


IMAGE_SUFFIXES = {".png", ".tga", ".jpg", ".jpeg", ".exr", ".hdr"}
GLTF_SUFFIXES = {".gltf", ".glb"}
# Source 2 compiled-texture artifacts. Useless once the .png is decoded —
# the Source 1 porting pipeline reads the .png and writes .vtf, so anything
# in this set just clutters materialsrc/.
COMPILED_TEXTURE_SUFFIXES = {".vtex", ".vtex_c"}
LogFn = Callable[[str], None]

# Windows caps a CreateProcess command line at 32767 chars. RERL-derived
# filter lists can massively exceed that on multi-subtree exports (a 4-path
# Alyx xen port resolves ~1500 refs -> WinError 206 at Popen), so filter
# lists are pruned against their covering prefixes and, as a last resort,
# split across several VRF invocations.
_MAX_CMDLINE = 30000

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


def expand_vpk_filters(vpk_filepaths: Iterable[str]) -> List[str]:
    """Expand and dedupe several internal VPK paths into one `-f` token list.

    Each `models/...` path also pulls its parallel `materials/...` subtree
    (see `expand_vpk_filter`). Empty/whitespace entries are dropped. Order of
    first appearance is preserved.
    """
    expanded = [expand_vpk_filter(fp) for fp in vpk_filepaths if fp and fp.strip()]
    return _merge_filters(expanded)


@dataclass
class ExportJob:
    """One VRF invocation: a single `-i` input archive/file/folder plus the
    internal VPK path filters to pull from it.

    `vpk_filepaths` is empty for loose `.vmdl_c`/folder inputs (no `-f`) and
    for a whole-archive `.vpk` input. Multiple entries are merged into one
    `-f` filter so VRF opens the archive only once.
    """
    input_path: Path
    vpk_filepaths: List[str] = field(default_factory=list)


def parse_vrf_copy_url(text: str) -> Optional[Tuple[str, str]]:
    """Parse VRF's right-click "Copy URL" string into `(vpk_path, internal)`.

    The format is `vpk:<vpk_path>:<internal_resource_path>`, e.g.::

        vpk:S:/SteamLibrary/.../hlvr/pak01_dir.vpk:models/props/foo.vmdl_c

    The archive path itself contains a drive-letter colon (`S:`), so the split
    is anchored on the `.vpk:` boundary rather than the first colon. Returns
    `None` if `text` isn't a (well-formed) VRF URL — the internal path is
    normalised to forward slashes with any leading slash stripped.
    """
    s = text.strip().strip('"').strip()
    if s[:4].lower() != "vpk:":
        return None
    body = s[4:]
    idx = body.lower().find(".vpk:")
    if idx < 0:
        return None
    vpk_path = body[:idx + 4]                      # include the ".vpk"
    internal = body[idx + 5:].strip().replace("\\", "/").lstrip("/")
    if not vpk_path or not internal:
        return None
    return (vpk_path, internal)


def _vpk_key(p: Path) -> str:
    """Case/separator-normalised key for grouping entries that name the same
    `.vpk` archive (Windows paths are case-insensitive)."""
    return os.path.normcase(os.path.normpath(str(p)))


def build_export_jobs(
    entries: Iterable[str],
    default_input: Optional[Path] = None,
) -> Tuple[List[ExportJob], List[str]]:
    """Resolve a batch list of path-ish strings into deduped `ExportJob`s.

    Each entry may be one of:

    * a VRF copy-URL `vpk:<vpk>:<internal>` — grouped under that archive;
    * a bare internal path `models/...` — paired with `default_input` when
      that input is a `.vpk` (the archive the path lives in);
    * a filesystem path to a `.vmdl_c` (or other) file, a `.vpk`, or a folder.

    Entries naming the same `.vpk` are merged into a single job (their filters
    combined) so the archive is opened once. Blank lines and `#` comments are
    ignored. Returns `(jobs, warnings)`; `warnings` collects entries that
    couldn't be resolved (e.g. a bare internal path with no VPK context).
    """
    jobs: List[ExportJob] = []
    warnings: List[str] = []
    vpk_jobs: Dict[str, ExportJob] = {}
    default_is_vpk = default_input is not None and is_vpk(default_input)

    def _vpk_job(vpk_path: Path) -> ExportJob:
        key = _vpk_key(vpk_path)
        job = vpk_jobs.get(key)
        if job is None:
            job = ExportJob(input_path=vpk_path)
            vpk_jobs[key] = job
            jobs.append(job)
        return job

    def _add_filter(job: ExportJob, internal: str):
        if internal and internal not in job.vpk_filepaths:
            job.vpk_filepaths.append(internal)

    for raw in entries:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        parsed = parse_vrf_copy_url(line)
        if parsed is not None:
            vpk_path, internal = parsed
            _add_filter(_vpk_job(Path(vpk_path)), internal)
            continue
        if line[:4].lower() == "vpk:":
            warnings.append(f"Malformed VRF URL (expected vpk:<archive>.vpk:<path>): {line}")
            continue

        candidate = line.strip('"')
        p = Path(candidate)
        if is_vpk(p):
            _vpk_job(p)                       # whole-archive job, no filter
            continue
        if p.exists():
            jobs.append(ExportJob(input_path=p))   # loose file or folder
            continue

        # Not a URL and not an on-disk path: treat as a bare internal VPK path
        # and pair it with the default input archive if we have one.
        norm = candidate.replace("\\", "/").lstrip("/")
        if default_is_vpk and "/" in norm:
            assert default_input is not None
            _add_filter(_vpk_job(default_input), norm)
            continue

        warnings.append(f"Could not resolve entry (no matching file or VPK context): {line}")

    return jobs, warnings


def build_vrf_command(
    vrf_exe: Path,
    input_path: Path,
    output_dir: Path,
    gltf_format: str = "glb",
    recursive: bool = True,
    threads: int = 4,
    vpk_filepath: Optional[Union[str, List[str]]] = None,
    extra_filters: Optional[List[str]] = None,
    mode: str = "models",
) -> List[str]:
    """Compose the Source2Viewer-CLI argv. Pure function — easy to unit-test.

    `vpk_filepath` is the VRF `-f` filter — a path prefix inside the VPK
    (e.g. "models/props_c17/"), or a list of them to extract several models
    from one archive in a single pass. Only meaningful when `input_path` is a
    .vpk.

    `extra_filters` is a list of additional path prefixes to merge into `-f`
    (e.g. parent dirs of references discovered via a `-b RERL` pre-pass).

    `mode` selects what gets iterated as primary targets:
      * "models" — .vmdl_c to GLTF/GLB plus their vmats + textures (the
        `--gltf_export_*` flags and `gltf_format` only apply here);
      * "vmats"  — .vmat_c materials plus their vtex textures, no GLTF.
    """
    if mode not in {"models", "vmats"}:
        raise VrfRunnerError(f"mode must be 'models' or 'vmats' (got {mode!r})")

    args: List[str] = [
        str(vrf_exe),
        "-i", str(input_path),
        "-o", str(output_dir),
        "-d",
    ]
    if mode == "models":
        if gltf_format not in {"gltf", "glb"}:
            raise VrfRunnerError(f"gltf_format must be 'gltf' or 'glb' (got {gltf_format!r})")
        # `--gltf_export_materials` is required to get a `materials` array in
        # the glTF with proper vmat names — without it VRF emits a mesh with no
        # material reference at all, and the SMD converter falls back to a
        # literal "material" name (which then breaks the downstream MDL's
        # $cdmaterials lookup). The flag's downside is hash-suffix duplicate
        # PNGs and channel-split textures dropped next to the .gltf — those get
        # cleaned up by `purge_stray_images_next_to_gltf` post-extraction.
        args += [
            "--gltf_export_format", gltf_format,
            "--gltf_export_animations",
            "--gltf_export_materials",
        ]
        entities = "vmdl_c,vmat_c,vtex_c"
    else:
        # VMAT-only port: iterate materials + their textures. No .vmdl_c, so no
        # GLTF is written and there are no stray images next to a .gltf.
        entities = "vmat_c,vtex_c"
    args += [
        "--threads", str(threads),
        "-e", entities,
    ]
    if recursive and input_path.is_dir():
        args.append("--recursive")
    if is_vpk(input_path) and vpk_filepath:
        paths = [vpk_filepath] if isinstance(vpk_filepath, str) else list(vpk_filepath)
        merged = _merge_filters(expand_vpk_filters(paths), extra_filters or [])
        if merged:
            args += ["-f", ",".join(merged)]
    return args


def _merge_filters(*filter_groups: Iterable[str]) -> List[str]:
    """Flatten + dedupe `-f` filter strings, preserving first-seen order.
    Tokens covered by another token's prefix are dropped (see
    `_prune_covered_filters`)."""
    seen: set = set()
    out: List[str] = []
    for group in filter_groups:
        for raw in group:
            for token in str(raw).split(","):
                t = token.strip().replace("\\", "/")
                if t and t not in seen:
                    seen.add(t)
                    out.append(t)
    return _prune_covered_filters(out)


def _prune_covered_filters(filters: List[str]) -> List[str]:
    """Drop `-f` tokens made redundant by another token in the list.

    VRF's filter is a starts-with match, so a token that itself starts with
    another token can never widen the matched set — an exact
    `models/props/xen/foo.vmdl` next to `models/props/xen/` adds nothing.
    This is what keeps RERL-derived ref lists (1500+ entries on a xen-scale
    port) off the Windows command-line length limit. Case-insensitive
    (Source 2 resource paths are compiled lowercase, and this module keys
    VPK paths case-insensitively too); order-preserving.
    """
    lowered = [f.lower() for f in filters]
    out: List[str] = []
    for i, tok in enumerate(filters):
        covered = False
        for j, other in enumerate(lowered):
            if j == i:
                continue
            if lowered[i].startswith(other) and (len(other) < len(lowered[i]) or j < i):
                covered = True
                break
        if not covered:
            out.append(tok)
    return out


def _split_filters_for_cmdline(
    base_args: List[str],
    filters: List[str],
    limit: int = _MAX_CMDLINE,
) -> List[List[str]]:
    """Split `-f` tokens into chunks so each `base_args + ["-f", <joined>]`
    command line stays under `limit` characters. One chunk in the common
    case; [] for an empty filter list.
    """
    if not filters:
        return []
    budget = limit - len(subprocess.list2cmdline(base_args + ["-f", ""]))
    chunks: List[List[str]] = []
    cur: List[str] = []
    cur_len = 0
    for tok in filters:
        added = len(tok) + (1 if cur else 0)   # +1 joining comma
        if cur and cur_len + added > budget:
            chunks.append(cur)
            cur, cur_len = [], 0
            added = len(tok)
        cur.append(tok)
        cur_len += added
    if cur:
        chunks.append(cur)
    return chunks


def _refs_outside_filters(refs: List[str], filters: List[str]) -> List[str]:
    """Keep only refs not already matched by one of the `-f` filters.

    A ref under a requested path gets pulled by that prefix filter anyway,
    so listing it individually just bloats the command line — only
    out-of-tree refs (shared textures like materials/xen/tile/) need their
    own entry.
    """
    lowered = [f.lower() for f in filters]
    return [r for r in refs if not any(r.lower().startswith(f) for f in lowered)]


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


def _run_block_dump(
    vrf_exe: Path,
    input_path: Path,
    vpk_filepath: str,
    entity_filter: str,
    block: str,
    on_log: LogFn,
    cancel: Optional[threading.Event] = None,
) -> Optional[str]:
    """Run VRF `-b <block>` over the filtered entries and return the combined
    stdout, splitting the filter list across several VRF processes when a
    single command line would exceed the Windows length limit.

    Returns None when nothing could be dumped (failed to launch, or every
    pass exited non-zero); partial output from a multi-pass run is still
    returned, with the failed passes logged.
    """
    filters = [t.strip() for t in vpk_filepath.split(",") if t.strip()]
    base = [
        str(vrf_exe),
        "-i", str(input_path),
        "-e", entity_filter,
        "-b", block,
    ]
    chunks = _split_filters_for_cmdline(base, filters)
    if len(chunks) > 1:
        on_log(f"Filter list needs {len(chunks)} `-b {block}` passes (command-line length limit)")
    outs: List[str] = []
    failed = 0
    for chunk in chunks:
        args = base + ["-f", ",".join(chunk)]
        on_log("Pre-pass: " + " ".join(_quote(a) for a in args))
        try:
            code, output = process.capture(args, cancel=cancel)
        except process.ProcessError as exc:
            on_log(f"`-b {block}` dump failed to launch: {exc}")
            return None
        if code != 0:
            failed += 1
            on_log(f"`-b {block}` pass exited with code {code}")
            continue
        outs.append(output)
    if failed and not outs:
        return None
    return "\n".join(outs)


def dump_material_groups(
    vrf_exe: Path,
    input_path: Path,
    vpk_filepath: str,
    on_log: LogFn = print,
    cancel: Optional[threading.Event] = None,
) -> Dict[str, List[Tuple[str, List[str]]]]:
    """Run VRF `-b DATA` on .vmdl_c entries matching the filter and parse
    out each model's `m_materialGroups`. Returns a `{vmdl_path: groups}`
    map where `groups` is `[(group_name, [vmat_refs]), ...]`. The first
    group is always the default — Source 2 convention.

    No-op-safe: returns `{}` when the dump fails and logs the failure.
    """
    on_log("Dumping material groups via `-b DATA`...")
    out = _run_block_dump(vrf_exe, input_path, vpk_filepath, "vmdl_c", "DATA",
                          on_log, cancel)
    if out is None:
        on_log("Material-groups dump failed; continuing without skin info")
        return {}
    return _parse_material_groups_per_file(out)


def dump_phys_blocks(
    vrf_exe: Path,
    input_path: Path,
    vpk_filepath: str,
    on_log: LogFn = print,
    cancel: Optional[threading.Event] = None,
) -> Dict[str, str]:
    """Run VRF `-b PHYS` on .vmdl_c entries matching the filter and return
    `{vmdl_path: kv3_text}` for models that actually carry physics parts.

    The PHYS block is the lossless physics source for back-porting: per-bone
    shapes, bind poses and Rubikon joints with real limits, none of which
    survive into the merged `_physics.glb`. No-op-safe: returns `{}` on failure.
    """
    on_log("Dumping physics (PHYS) blocks via `-b PHYS`...")
    out = _run_block_dump(vrf_exe, input_path, vpk_filepath, "vmdl_c", "PHYS",
                          on_log, cancel)
    if out is None:
        on_log("PHYS dump failed; continuing without physics data")
        return {}

    sections: Dict[str, str] = {}
    cur_path: Optional[str] = None
    buf: List[str] = []
    for line in out.splitlines():
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

    # keep only models with real parts (props without physics print nothing
    # useful; empty PHYS blocks would just confuse the SMD batch tool)
    return {p: t for p, t in sections.items() if "m_parts" in t and "m_flMass" in t}


def _model_dump_sections(text: str, fallback: str = "") -> Dict[str, str]:
    """Split a VRF multi-model dump, retaining all repeated MDAT blocks."""
    matches = list(_DATA_FILE_HEADER_RE.finditer(text))
    if not matches:
        return {fallback: text} if fallback and "<!-- kv3" in text else {}
    return {
        match.group(1).replace("\\", "/"):
        text[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(text)]
        for index, match in enumerate(matches)
    }


def dump_model_metadata(
    vrf_exe: Path,
    input_path: Path,
    vpk_filepath: str,
    on_log: LogFn = print,
    cancel: Optional[threading.Event] = None,
    *,
    data_dump: Optional[str] = None,
) -> Dict[str, object]:
    """Read DATA/MDAT/ANIM/ASEQ authoring fields into compact model sidecars."""
    from s2kit.pipelines.model_metadata import parse_model_metadata

    on_log("Reading authored hitboxes, attachments and animation metadata...")
    fallback = str(input_path.name) if input_path.suffix.lower() == ".vmdl_c" else ""
    dumps: Dict[str, Dict[str, str]] = {}
    for block in ("DATA", "MDAT", "ANIM", "ASEQ"):
        text = data_dump if block == "DATA" and data_dump is not None else _run_block_dump(
            vrf_exe, input_path, vpk_filepath, "vmdl_c", block, on_log, cancel)
        dumps[block] = _model_dump_sections(text or "", fallback)
        if block == "DATA" and len(dumps[block]) == 1:
            fallback = next(iter(dumps[block]))
    paths = dict.fromkeys(path for blocks in dumps.values() for path in blocks)
    result = {}
    for path in paths:
        try:
            metadata = parse_model_metadata(*(dumps[block].get(path, "")
                                              for block in ("DATA", "MDAT", "ANIM", "ASEQ")))
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            on_log(f"{path}: authored metadata could not be read: {exc}")
            continue
        result[path] = metadata
        for warning in metadata.warnings:
            on_log(f"{path}: {warning}")
    return result


def write_metadata_sidecars(staging_dir: Path, metadata: Dict[str, object],
                            on_log: LogFn = print) -> int:
    """Place `.metadata.json` beside the glTF for the native model porter."""
    from s2kit.pipelines.model_metadata import write_model_metadata

    for path, value in metadata.items():
        base = path[:-7] if path.lower().endswith(".vmdl_c") else path.removesuffix(".vmdl")
        relative = Path(base + ".metadata.json")
        if relative.is_absolute() or ".." in relative.parts:
            raise VrfRunnerError(f"Unsafe model metadata path: {path}")
        write_model_metadata(staging_dir / relative, value)
    on_log(f"Wrote {len(metadata)} authored model metadata sidecar(s)")
    return len(metadata)


def write_phys_sidecars(
    staging_dir: Path,
    phys_blocks: Dict[str, str],
    on_log: LogFn = print,
) -> int:
    """Write each model's PHYS KV3 text as `<stem>_phys.txt` next to the
    .gltf VRF drops, so reorganize carries it into modelsrc/ where a collision
    builder can read the real per-bone shapes and joint limits.

    Returns the number of sidecars written.
    """
    written = 0
    for vmdl_path, text in phys_blocks.items():
        base = vmdl_path
        for ext in (".vmdl_c", ".vmdl"):
            if base.lower().endswith(ext):
                base = base[: -len(ext)]
                break
        sidecar = staging_dir / (base + "_phys.txt")
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(text, encoding="utf-8")
        written += 1
    on_log(f"Wrote {written} physics sidecar(s)")
    return written


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
    cancel: Optional[threading.Event] = None,
) -> List[str]:
    """Run VRF with `-b RERL` to dump external references for the matching
    entries inside a VPK. Returns deduped reference paths (with source
    extensions like `.vmat`, not `.vmat_c`).

    No-op-safe: returns [] when the dump fails and logs the failure.
    """
    out = _run_block_dump(vrf_exe, input_path, vpk_filepath, entity_filter,
                          "RERL", on_log, cancel)
    if out is None:
        on_log("RERL pre-pass failed; continuing without references")
        return []
    return _parse_rerl_paths(out)


def _stream_subprocess(args: List[str], on_log: LogFn,
                       cancel: Optional[threading.Event] = None) -> int:
    """Run VRF and forward each output line to `on_log`. Returns the exit code.

    Launch failures surface as VrfRunnerError so a batch worker fails the one
    job and moves on instead of aborting the whole run. Cancellation is left
    to propagate - the tool turns it into a cancelled job.
    """
    try:
        return process.run_streamed(args, on_log, cancel=cancel)
    except process.ProcessError as exc:
        raise VrfRunnerError(str(exc)) from exc


def _quote(arg: str) -> str:
    """Quote an argument for display in a log line."""
    return process.quote(arg)


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


def detect_nested_package_dirs(staging_dir: Path) -> List[str]:
    """Identify Source 2 "addon package" layouts in the staging tree: a
    package dir under `models/` that contains BOTH a `models/` and a
    `materials/` subdir, e.g. `models/combine_horseman/{models,materials}/`.

    Garry's Mod workshop ports commonly pack a whole Source 1 addon tree
    verbatim under the Source 2 `models/` root, so the model lives at
    `models/<pkg>/models/<model>.vmdl_c` and references its materials at
    `models/<pkg>/materials/<rest>/<mat>.vmat`. The generic Alyx hoist would
    route those materials to `materialsrc/models/<pkg>/...` while the model
    stays at `modelsrc/<pkg>/models/...`, leaving the two trees structurally
    incoherent (the `models` and `<pkg>` segments end up in swapped order).

    Returned values are package paths relative to `models/` (forward-slash,
    e.g. "combine_horseman"). Detection runs on the staging tree *before* the
    hoist mangles it; the paths drive the final `flatten_nested_packages`
    step so each package collapses to a clean `<pkg>/` root shared by model
    and materials.

    The Alyx case (materials are a direct sibling of the model *file*, with
    no inner `models/` dir) is deliberately not matched here — it has no
    redundant nesting to flatten and stays on the existing hoist path.
    """
    models_root = staging_dir / "models"
    if not models_root.is_dir():
        return []
    out: List[str] = []
    seen: set = set()
    for mats_dir in models_root.rglob("materials"):
        if not mats_dir.is_dir():
            continue
        pkg_dir = mats_dir.parent
        if (pkg_dir / "models").is_dir():
            rel = pkg_dir.relative_to(models_root).as_posix()
            if rel and rel not in seen:
                seen.add(rel)
                out.append(rel)
    return out


def flatten_nested_packages(
    output_dir: Path,
    packages: Iterable[str],
    on_log: LogFn = print,
) -> int:
    """Collapse the nested addon-package layout to a clean Source 1 root for
    each package identified by `detect_nested_package_dirs`.

    Runs as the *final* step — after reorganize and all texture purges — so
    the ref-matching purges operate on the `models/`-prefixed layout they
    understand (`_normalize_vmat_ref` keeps the `models/` prefix, so flatten-
    ing earlier would make every texture look unreferenced and get deleted).
    This is a pure directory relocation:

        modelsrc/<pkg>/models/<rest>    -> modelsrc/<pkg>/<rest>
        materialsrc/models/<pkg>/<rest> -> materialsrc/<pkg>/<rest>

    leaving model and materials sharing the `<pkg>/` root with no redundant
    `models/` or `materials/` wrapper (so `$cdmaterials "<pkg>/<rest>"` lines
    up). Returns the number of packages flattened.
    """
    modelsrc = output_dir / "modelsrc"
    materialsrc = output_dir / "materialsrc"
    flattened = 0
    for raw in packages:
        rel = str(raw).strip().strip("/")
        if not rel:
            continue
        moved_any = False
        inner_models = modelsrc / Path(rel) / "models"
        if inner_models.is_dir():
            _move_tree_into(inner_models, modelsrc / Path(rel), on_log)
            moved_any = True
        pkg_materials = materialsrc / "models" / Path(rel)
        if pkg_materials.is_dir():
            _move_tree_into(pkg_materials, materialsrc / Path(rel), on_log)
            moved_any = True
        if moved_any:
            flattened += 1
    # Drop a now-empty `materialsrc/models/` wrapper left behind when every
    # material under it belonged to a flattened package. Leave it in place if
    # normal (non-package) models still live there.
    leftover = materialsrc / "models"
    if leftover.is_dir() and not any(leftover.iterdir()):
        try:
            leftover.rmdir()
        except OSError:
            pass
    if flattened:
        on_log(f"Flattened {flattened} nested package layout(s) to clean <pkg>/ roots")
    return flattened


def _prune_empty_dirs(root: Path, on_log: LogFn = print) -> None:
    """Remove empty subdirectories under `root`, bottom-up. `root` itself is
    left in place even if it ends up empty."""
    dirs = sorted(
        (p for p in root.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    )
    for d in dirs:
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass


def flatten_materials_to_root(output_dir: Path, on_log: LogFn = print) -> int:
    """Move every exported image and `.vmat` file into the root of
    `materialsrc/`, collapsing the nested directory structure into a flat pile.

    Pulls from both `modelsrc/` (VRF's stray images sitting next to each .gltf)
    and `materialsrc/` (the parallel materials tree), so every texture and
    material ends up directly under `materialsrc/`. This is why it overrides
    the stray-image option: the strays are swept into the root rather than
    kept-in-place or purged. Overwrites on name collision — mirrors the
    `_move_tree_into` convention and keeps duplicate strays from piling up
    (the canonical `materialsrc/` copy is moved last, so it wins). Empty
    subfolders left behind are pruned.

    Returns the number of files moved.
    """
    materialsrc = output_dir / "materialsrc"
    materialsrc.mkdir(parents=True, exist_ok=True)

    flatten_suffixes = IMAGE_SUFFIXES | {".vmat"}
    # (source root, suffixes to sweep). modelsrc yields images only; process it
    # first so a same-named canonical material texture overwrites the stray.
    sources: List[Tuple[Path, Set[str]]] = []
    modelsrc = output_dir / "modelsrc"
    if modelsrc.is_dir():
        sources.append((modelsrc, set(IMAGE_SUFFIXES)))
    sources.append((materialsrc, flatten_suffixes))

    moved = 0
    for src_root, suffixes in sources:
        for f in list(src_root.rglob("*")):
            if not f.is_file():
                continue
            if f.suffix.lower() not in suffixes:
                continue
            if f.parent == materialsrc:
                continue  # already at the root
            dest = materialsrc / f.name
            try:
                if dest.exists():
                    dest.unlink()
                shutil.move(str(f), str(dest))
                moved += 1
            except OSError as e:
                on_log(f"Could not move {f}: {e}")

    _prune_empty_dirs(materialsrc, on_log)
    on_log(f"Flattened {moved} image/vmat file(s) into {materialsrc.name}/ root")
    return moved


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

    Each referenced stem is also keyed against the vmat's OWN directory:
    VRF decodes a vmat's textures next to the .vmat regardless of the
    authored reference path, which can point into a different model's tree
    entirely (e.g. HLA's bodybag_cleanup.vmat lives under props/hazmat/ but
    references models/props/quarantine/materials/ textures). Without the
    same-folder key those decoded files would look unreferenced and be
    purged — orphaning the vmat. This mirrors the basename-in-vmat-folder
    fallback the VMAT parser uses when resolving textures.
    """
    refs: Set[Tuple[str, str]] = set()
    for vmat in materialsrc.rglob("*.vmat"):
        if not vmat.is_file():
            continue
        try:
            text = vmat.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        vmat_dir = vmat.parent.relative_to(materialsrc).as_posix().lower()
        if vmat_dir == ".":
            vmat_dir = ""
        for raw in _VMAT_TEXTURE_RE.findall(text):
            key = _normalize_vmat_ref(raw)
            if key is not None:
                refs.add(key)
                refs.add((vmat_dir, key[1]))
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
    """Delete compiled-image duplicates only when their authored replacement exists.

    A compiled block can contain the only decoded copy of a texture. Direct
    references to hashed names must survive, as must hashes whose clean-name
    replacement was not exported. The known VRF ``name_extension_hash`` naming
    pattern proves a replacement; uncertain names are retained. References use
    both their authored directory and the VMAT's own directory after hoisting.

    Returns the number of files deleted.
    """
    if not materialsrc_dir.is_dir():
        return 0
    images: Dict[Tuple[str, str], Path] = {}
    for img in materialsrc_dir.rglob("*"):
        if img.is_file() and img.suffix.lower() in IMAGE_SUFFIXES:
            key = _normalize_vmat_ref(img.relative_to(materialsrc_dir).as_posix())
            if key is not None:
                images[key] = img
    targets: Set[Tuple[str, str]] = set()
    direct_refs: Set[Tuple[str, str]] = set()
    replaceable: Set[Tuple[str, str]] = set()
    for vmat in materialsrc_dir.rglob("*.vmat"):
        if not vmat.is_file():
            continue
        try:
            text = vmat.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        vmat_dir = vmat.parent.relative_to(materialsrc_dir).as_posix().lower()
        if vmat_dir == ".":
            vmat_dir = ""
        authored_refs: Set[Tuple[str, str]] = set()
        authored_text = _COMPILED_TEXTURES_BLOCK_RE.sub("", text)
        for raw in _VMAT_TEXTURE_RE.findall(authored_text):
            key = _normalize_vmat_ref(raw)
            if key is not None:
                authored_refs.update((key, (vmat_dir, key[1])))
        direct_refs.update(authored_refs)
        for block in _COMPILED_TEXTURES_BLOCK_RE.findall(text):
            for raw in _VMAT_TEXTURE_RE.findall(block):
                key = _normalize_vmat_ref(raw)
                if key is not None:
                    keys = {key, (vmat_dir, key[1])}
                    targets.update(keys)
                    clean_stem = re.sub(
                        r"_(?:png|tga|psd|jpg|jpeg|tif|tiff|exr|hdr)_[0-9a-f]{8}$",
                        "", key[1], flags=re.IGNORECASE)
                    if clean_stem != key[1] and any(
                            ref[1] == clean_stem and ref in images
                            for ref in authored_refs):
                        replaceable.update(keys)
    if not targets:
        on_log("No 'Compiled Textures' references found")
        return 0
    deleted = 0
    for key, img in images.items():
        if key not in replaceable or key in direct_refs:
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
    flatten_materials_root: bool = False,
    mode: str = "models",
    vpk_filepath: Optional[Union[str, List[str]]] = None,
    on_log: LogFn = print,
    cancel: Optional[threading.Event] = None,
) -> int:
    """
    Run a full export: GLTF + materials/ tree, then optionally clean up stray
    image files next to the .gltf outputs.

    `mode` picks the port type:
      * "models" — .vmdl_c to GLTF/GLB plus their materials tree (the default);
      * "vmats"  — .vmat_c materials plus their textures only, no GLTF. Model-
        only steps (GLTF stray-image purge, skin sidecars) are skipped.

    `vpk_filepath` is a path prefix inside a VPK (e.g. "models/props_c17/"),
    or a list of them to extract several models from one archive in a single
    pass — only used when `input_path` is a .vpk file.

    `flatten_materials_root` moves every exported image and .vmat into a flat
    `materialsrc/` root as a final step; it overrides `keep_stray_images`
    (the strays are swept into the root instead of being kept or purged).

    Returns the VRF process exit code (0 on success).
    """
    if mode not in {"models", "vmats"}:
        raise VrfRunnerError(f"mode must be 'models' or 'vmats' (got {mode!r})")
    if not vrf_exe.is_file():
        raise VrfRunnerError(f"VRF executable not found: {vrf_exe}")
    if not input_path.exists():
        raise VrfRunnerError(f"Input path does not exist: {input_path}")

    # Normalise the filter to a list so the single- and multi-path callers
    # share one code path below.
    if not vpk_filepath:
        vpk_filepaths: List[str] = []
    elif isinstance(vpk_filepath, str):
        vpk_filepaths = [vpk_filepath]
    else:
        vpk_filepaths = [p for p in vpk_filepath if p and p.strip()]

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
    if is_vpk(input_path) and vpk_filepaths and mode == "models":
        prefix_filters = expand_vpk_filters(vpk_filepaths)
        on_log("Resolving model references via `-b RERL`...")
        first_refs = dump_rerl(
            vrf_exe=vrf_exe,
            input_path=input_path,
            vpk_filepath=",".join(prefix_filters),
            entity_filter="vmdl_c",
            on_log=on_log,
            cancel=cancel,
        )
        # Recurse one level: RERL each discovered .vmat to find its .vtex
        # textures by exact path. Without this, we'd have to fall back to
        # parent-dir filters and accidentally pull every unrelated texture
        # sharing the .vmat's folder. Materials under the user's own filter
        # paths are already dump targets via the prefixes, so only
        # out-of-tree vmats are named individually — that keeps this
        # command line off the Windows length limit too.
        vmat_refs = [r for r in first_refs if r.lower().endswith(".vmat")]
        second_refs: List[str] = []
        if vmat_refs:
            on_log(f"Resolving texture references from {len(vmat_refs)} material(s)...")
            second_refs = dump_rerl(
                vrf_exe=vrf_exe,
                input_path=input_path,
                vpk_filepath=",".join(_merge_filters(prefix_filters, vmat_refs)),
                entity_filter="vmat_c",
                on_log=on_log,
                cancel=cancel,
            )
        seen: set = set()
        all_refs: List[str] = []
        for r in first_refs + second_refs:
            if r not in seen:
                seen.add(r)
                all_refs.append(r)
        if all_refs:
            # In-tree refs are extracted by the prefix filters anyway; only
            # out-of-tree refs need their own -f entry (and are worth logging).
            extra_filters = _refs_outside_filters(all_refs, prefix_filters)
            on_log(
                f"Found {len(all_refs)} referenced resource(s); "
                f"{len(all_refs) - len(extra_filters)} already inside the requested paths"
            )
            if extra_filters:
                on_log(f"Adding {len(extra_filters)} out-of-tree reference(s):")
                for r in extra_filters:
                    on_log(f"  - {r}")
        else:
            on_log("No references resolved; falling back to parallel-path filter only.")
    elif is_vpk(input_path) and vpk_filepaths and mode == "vmats":
        # VMAT port: the material is the primary target, so we only need one
        # RERL level — resolve each .vmat's .vtex textures by exact path.
        prefix_filters = expand_vpk_filters(vpk_filepaths)
        on_log("Resolving texture references via `-b RERL`...")
        refs = dump_rerl(
            vrf_exe=vrf_exe,
            input_path=input_path,
            vpk_filepath=",".join(prefix_filters),
            entity_filter="vmat_c",
            on_log=on_log,
            cancel=cancel,
        )
        if refs:
            extra_filters = _refs_outside_filters(refs, prefix_filters)
            on_log(
                f"Found {len(refs)} referenced resource(s); "
                f"{len(refs) - len(extra_filters)} already inside the requested paths"
            )
            if extra_filters:
                on_log(f"Adding {len(extra_filters)} out-of-tree reference(s):")
                for r in extra_filters:
                    on_log(f"  - {r}")
        else:
            on_log("No references resolved; falling back to parallel-path filter only.")

    args = build_vrf_command(
        vrf_exe=vrf_exe,
        input_path=input_path,
        output_dir=staging_dir,
        gltf_format=gltf_format,
        recursive=recursive,
        threads=threads,
        vpk_filepath=vpk_filepaths,
        extra_filters=extra_filters,
        mode=mode,
    )
    # A huge filter list can still push one command line past the Windows
    # 32k limit (WinError 206 at Popen). Split the -f tokens across several
    # VRF passes into the same staging dir — outputs are per-file, so the
    # passes compose.
    export_cmds = [args]
    if "-f" in args:
        f_idx = args.index("-f")
        base_args = args[:f_idx] + args[f_idx + 2:]
        tokens = args[f_idx + 1].split(",")
        chunks = _split_filters_for_cmdline(base_args, tokens)
        if len(chunks) > 1:
            on_log(
                f"{len(tokens)} filter path(s) exceed one command line; "
                f"splitting the export into {len(chunks)} passes"
            )
            export_cmds = [base_args + ["-f", ",".join(c)] for c in chunks]
    rc = 0
    for i, cmd in enumerate(export_cmds, start=1):
        if len(export_cmds) > 1:
            on_log(f"Export pass {i}/{len(export_cmds)}")
        pass_rc = _stream_subprocess(cmd, on_log, cancel)
        rc = rc or pass_rc
    if rc != 0:
        on_log(f"VRF exited with code {rc}")
        return rc

    # Stray-image cleanup only applies in model mode (VMAT mode writes no
    # .gltf, so there are no strays sitting next to one).
    if mode == "models":
        if flatten_materials_root:
            on_log(
                "Flatten-to-root enabled: stray images will be swept into "
                "materialsrc/ root (stray-image option overridden)"
            )
        elif not keep_stray_images:
            purge_stray_images_next_to_gltf(staging_dir, on_log)
        else:
            on_log("Skipping stray-image cleanup (keep_stray_images=True)")

    # Source 2 material groups → Source 1 $texturegroup. Dump them now (while
    # still operating against the VPK) and write JSON sidecars next to each
    # .gltf in staging so reorganize carries them into modelsrc/ for the SMD
    # batch tool to consume. Skins are a model concept — skip in VMAT mode.
    if mode == "models" and is_vpk(input_path) and vpk_filepaths:
        model_filter = ",".join(expand_vpk_filters(vpk_filepaths))
        data_dump = _run_block_dump(vrf_exe, input_path, model_filter,
                                    "vmdl_c", "DATA", on_log, cancel)
        mg = _parse_material_groups_per_file(data_dump or "")
        if mg:
            write_skin_sidecars(staging_dir, mg, on_log)
        metadata = dump_model_metadata(vrf_exe, input_path, model_filter,
                                       on_log, cancel, data_dump=data_dump)
        if metadata:
            write_metadata_sidecars(staging_dir, metadata, on_log)
        from s2kit.pipelines.native_lods import export_native_lod_sidecars

        export_native_lod_sidecars(
            vrf_exe, input_path, staging_dir,
            _model_dump_sections(data_dump or ""), on_log, cancel)
        # Physics ground truth for ragdolls/jointed props: the PHYS block has
        # the real per-bone shapes and joint limits that the merged
        # `_physics.glb` loses. Saved as `<stem>_phys.txt` beside the glTF.
        pb = dump_phys_blocks(
            vrf_exe=vrf_exe,
            input_path=input_path,
            vpk_filepath=",".join(expand_vpk_filters(vpk_filepaths)),
            on_log=on_log,
            cancel=cancel,
        )
        if pb:
            write_phys_sidecars(staging_dir, pb, on_log)

    # Detect Source 2 addon-package layouts (models/<pkg>/{models,materials}/)
    # now, while staging still reflects the VPK structure — the hoist below is
    # about to move the inner materials/ trees up and erase the signature. The
    # actual flatten is deferred to the very end (after the purges).
    nested_packages = detect_nested_package_dirs(staging_dir)

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

    # Final cosmetic pass: collapse the nested addon-package layout to a clean
    # <pkg>/ root. Deferred to here so the ref-matching purges above ran on the
    # `models/`-prefixed layout they understand.
    if nested_packages:
        flatten_nested_packages(output_dir, nested_packages, on_log)

    # Optional final layout: collapse every image + .vmat into a flat
    # materialsrc/ root. Runs dead last, after all purges and package flattening
    # so it sweeps up whatever survived (plus the strays under modelsrc/).
    if flatten_materials_root:
        flatten_materials_to_root(output_dir, on_log)
    return rc
