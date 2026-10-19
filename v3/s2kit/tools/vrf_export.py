"""
VRF Batch Export.

Drives Source2Viewer-CLI to pull models or materials out of a Source 2 VPK (or
a loose file/folder) and lay them out as a Source 1 project:

    <project>/modelsrc/models/foo/foo.glb
    <project>/materialsrc/models/foo/foo.vmat + textures

This is the front of the porting pipeline - everything downstream starts from
what it writes - so it is the first v2 tool ported to v3. The heavy lifting
lives in :mod:`s2kit.pipelines.vrf`; this file is the schema plus the job.
"""

from __future__ import annotations

from s2kit.definitions.vrf_export import _MODELS_BATCH_HINT

import re
import time
from pathlib import Path
from typing import Optional

from s2kit.core.jobs import JobCancelled, JobContext
from s2kit.media.probe import human_size
from s2kit.pipelines import vrf
from s2kit.definitions.vrf_export import VrfBatchExport as _VrfBatchExportDefinition

#: VRF prints one of these per file it writes; the ones ending in a primary
#: suffix (.glb/.gltf per model, .vmat per material) drive the progress bar.
_DUMP_LINE = re.compile(r'^--- Dump written to "(.+)"\s*$')


class VrfBatchExport(_VrfBatchExportDefinition):
    """Batch VRF extraction with reference resolution and project layout."""


    # ------------------------------------------------------------------

    def validate(self, values: dict) -> dict[str, str]:
        source = values.get("source")
        key = {"vpk": "vpk", "file": "input_file", "folder": "input_folder"}[source]
        if not str(values.get(key) or "").strip():
            return {key: "Pick an input first"}
        return {}

    def run_job(self, ctx: JobContext) -> None:
        params = ctx.params
        mode = str(params["mode"])
        output_dir = Path(str(params["output"]))
        exe = self._resolve_executable(ctx)
        ctx.info(f"Source2Viewer-CLI: {exe}")

        default_input = self._input_path(params)
        entries = [line for line in str(params["batch"] or "").splitlines()
                   if line.strip()]
        jobs, warnings = (vrf.build_export_jobs(entries, default_input)
                          if entries
                          else ([vrf.ExportJob(input_path=default_input)], []))
        for warning in warnings:
            ctx.warn(warning)
        if not jobs:
            raise ValueError("Nothing to export - every batch entry was skipped")

        ctx.info(f"{len(jobs)} export job(s), mode={mode}, output={output_dir}")
        before = _snapshot(output_dir)

        rows: list[list[object]] = []
        worst = 0
        counter = _DumpCounter(ctx, mode, jobs, params)

        for index, job in enumerate(jobs, start=1):
            ctx.raise_if_cancelled()
            label = _job_label(job)
            ctx.status(f"[{index}/{len(jobs)}] {label}")
            ctx.info(f"[{index}/{len(jobs)}] {label}")
            counter.begin_job(index - 1)
            started = time.time()

            try:
                code = vrf.run_vrf_export(
                    vrf_exe=exe,
                    input_path=job.input_path,
                    output_dir=output_dir,
                    gltf_format=str(params["format"]),
                    recursive=bool(params["recursive"]),
                    threads=int(params["threads"]),
                    keep_stray_images=bool(params["keep_stray_images"]),
                    flatten_materials_root=bool(params["flatten_materials"]),
                    mode=mode,
                    vpk_filepath=job.vpk_filepaths or None,
                    on_log=counter.on_log,
                    cancel=ctx.cancel_event,
                )
            except JobCancelled:
                raise
            except vrf.VrfRunnerError as exc:
                ctx.error(f"{label}: {exc}")
                rows.append([label, "failed", "-", str(exc)[:120]])
                worst = worst or 2
                counter.end_job()
                continue

            elapsed = f"{time.time() - started:.1f}s"
            if code != 0:
                ctx.error(f"{label}: VRF exited with code {code}")
                rows.append([label, "failed", elapsed, f"exit code {code}"])
                worst = code
            else:
                ctx.success(f"{label}: done")
                rows.append([label, "ok", elapsed, ""])
            counter.end_job()

        self._report(ctx, output_dir, before, rows, worst, len(jobs))

    # ------------------------------------------------------------------
    # Pieces
    # ------------------------------------------------------------------

    def _resolve_executable(self, ctx: JobContext) -> Path:
        """The CLI from the global preference, else PATH."""
        configured = str(ctx.prefs.get("vrf_cli_path", "") or "")
        try:
            return vrf.resolve_vrf_executable(settings_path=configured or None)
        except vrf.VrfRunnerError as exc:
            raise vrf.VrfRunnerError(
                f"{exc}\n\nSet it in Preferences > External programs > "
                "Source2Viewer-CLI."
            ) from exc

    @staticmethod
    def _input_path(params: dict) -> Path:
        key = {"vpk": "vpk", "file": "input_file",
               "folder": "input_folder"}[str(params["source"])]
        return Path(str(params[key]))

    def _report(self, ctx: JobContext, output_dir: Path,
                before: dict[Path, float], rows: list[list[object]],
                worst: int, job_count: int) -> None:
        """Turn what landed on disk into metrics, tables and previews."""
        written = _new_files(output_dir, before)
        models = [p for p in written if p.suffix.lower() in (".glb", ".gltf")]
        materials = [p for p in written if p.suffix.lower() == ".vmat"]
        textures = [p for p in written if p.suffix.lower() in (".png", ".tga")]
        sidecars = [p for p in written
                    if p.name.endswith(("_skins.json", "_phys.txt"))]
        total_bytes = sum(_size(p) for p in written)

        ctx.metric("Models", len(models), tone="good" if models else "neutral")
        ctx.metric("Materials", len(materials))
        ctx.metric("Textures", len(textures))
        ctx.metric("Written", human_size(total_bytes))
        if sidecars:
            ctx.metric("Sidecars", len(sidecars), tone="good")

        failed = sum(1 for row in rows if row[1] == "failed")
        if failed:
            ctx.metric("Failed jobs", failed, tone="bad")

        ctx.table("Export jobs", ["Job", "Result", "Time", "Detail"], rows)

        if bool(ctx.prefs.get("list_outputs", True)) and written:
            limit = int(ctx.prefs.get("output_table_limit", 400))
            listed = sorted(written, key=lambda p: str(p).lower())[:limit]
            if len(written) > len(listed):
                ctx.warn(f"Listing the first {limit} of {len(written)} written "
                         "files (raise the limit in this tool's Settings).")
            ctx.table(
                f"Written files ({len(written)})",
                ["File", "Kind", "Size", "Path"],
                [[p.name, _kind_of(p, output_dir), human_size(_size(p)), str(p)]
                 for p in listed],
                path_column=3,
            )

        if bool(ctx.prefs.get("preview_exports", True)):
            limit = int(ctx.prefs.get("preview_limit", 8))
            for path in (models + materials + textures)[:limit]:
                ctx.artifact(path, label=path.name)

        if bool(ctx.prefs.get("reveal_when_done", False)) and written:
            ctx.info(f"Output: {output_dir}")

        if not written:
            ctx.warn("VRF reported success but no new files appeared. Check "
                     "the paths in the batch box against the archive.")

        summary = (f"{len(models)} model(s), {len(materials)} material(s), "
                   f"{len(textures)} texture(s) from {job_count} job(s)")
        if worst:
            summary += f" - {failed} job(s) failed"
            ctx.error(summary)
        else:
            ctx.success(summary)
        ctx.summary(summary)


class _DumpCounter:
    """Turns VRF's "Dump written" lines into an honest progress bar.

    A pasted batch list collapses into a single VPK job, so job-level steps
    would never move the bar. Counting primary outputs (one .glb per model,
    one .vmat per material) does - but only when the expected count is
    knowable up front. Whole-archive or prefix-filter jobs stay indeterminate
    rather than showing a fake percentage.
    """

    def __init__(self, ctx: JobContext, mode: str, jobs: list, params: dict):
        self.ctx = ctx
        self.suffixes = (".glb", ".gltf") if mode == "models" else (".vmat",)
        self.expected = [_expected_units(job, mode, bool(params["recursive"]))
                         for job in jobs]
        self.total = (sum(e for e in self.expected if e is not None)
                      if all(e is not None for e in self.expected) else 0)
        self.done = 0
        self.cap = 0
        self.job_count = len(jobs)
        self.job_index = 0
        if self.total:
            ctx.progress(0, self.total)
        elif self.job_count > 1:
            ctx.progress(0, self.job_count)

    def begin_job(self, index: int) -> None:
        self.job_index = index
        if self.total:
            self.cap += self.expected[index] or 0

    def on_log(self, message: str) -> None:
        self.ctx.info(message)
        if not self.total or self.done >= self.cap:
            return
        match = _DUMP_LINE.match(message)
        if match and match.group(1).lower().endswith(self.suffixes):
            self.done += 1
            self.ctx.progress(self.done, self.total)

    def end_job(self) -> None:
        """Snap to the job boundary so a partial job does not stall the bar."""
        if self.total:
            self.done = self.cap
            self.ctx.progress(self.done, self.total)
        elif self.job_count > 1:
            self.ctx.progress(self.job_index + 1, self.job_count)


def _expected_units(job, mode: str, recursive: bool) -> Optional[int]:
    """How many primary outputs a job should produce, or None if unknowable."""
    suffix = ".vmdl_c" if mode == "models" else ".vmat_c"
    if job.vpk_filepaths:
        if all(f.lower().endswith(suffix) for f in job.vpk_filepaths):
            return len(job.vpk_filepaths)
        return None
    path = job.input_path
    if vrf.is_vpk(path):
        return None
    if path.is_dir():
        globber = path.rglob if recursive else path.glob
        return sum(1 for _ in globber(f"*{suffix}")) or None
    return 1


def _job_label(job) -> str:
    name = job.input_path.name
    if not job.vpk_filepaths:
        return name
    if len(job.vpk_filepaths) == 1:
        return f"{name} : {job.vpk_filepaths[0]}"
    return f"{name} : {len(job.vpk_filepaths)} paths"


def _snapshot(root: Path) -> dict[Path, float]:
    """Existing files and their mtimes, so we can report only what is new."""
    if not root.is_dir():
        return {}
    snapshot: dict[Path, float] = {}
    for path in root.rglob("*"):
        if path.is_file():
            try:
                snapshot[path] = path.stat().st_mtime
            except OSError:
                continue
    return snapshot


def _new_files(root: Path, before: dict[Path, float]) -> list[Path]:
    """Files created or rewritten since the snapshot, staging excluded."""
    if not root.is_dir():
        return []
    found: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or ".vrf_staging" in path.parts:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if before.get(path) != mtime:
            found.append(path)
    return found


def _kind_of(path: Path, root: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".glb", ".gltf"):
        return "model"
    if suffix == ".vmat":
        return "material"
    if suffix in (".png", ".tga", ".jpg"):
        return "texture"
    if path.name.endswith("_skins.json"):
        return "skin sidecar"
    if path.name.endswith("_phys.txt"):
        return "phys sidecar"
    return suffix.lstrip(".") or "file"


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
