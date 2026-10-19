"""
VTF Resizer.

Batch-shrinks oversized VTFs in place. Source 1 wants power-of-two texture
dimensions, and a folder of 4K materials ported from Source 2 will blow past any
sensible video-memory budget long before it looks better for it.

Resizing is done by decoding the largest mip, resampling with Lanczos, and
re-encoding at the file's original format and flags - so a normal map stays a
normal map and an sRGB basetexture stays sRGB.
"""

from __future__ import annotations

from s2kit.definitions.vtf_resize import SIZES

import shutil
from pathlib import Path

from s2kit.core.jobs import JobContext
from s2kit.media.probe import human_size
from s2kit.pipelines import textures
from s2kit.definitions.vtf_resize import VtfResizer as _VtfResizerDefinition


class VtfResizer(_VtfResizerDefinition):
    """Cap VTF dimensions across a folder."""


    def run_job(self, ctx: JobContext) -> None:
        params = ctx.params
        files = self._collect(ctx)
        if not files:
            ctx.warn("No .vtf files found.")
            ctx.summary("Nothing to resize")
            return

        max_size = int(params["max_size"])
        preserve = bool(params["preserve_aspect"])
        backup = bool(params["backup"])
        dry_run = bool(params["dry_run"])
        ctx.info(f"{len(files)} VTF file(s), cap {max_size}x{max_size}"
                 + ("  [dry run]" if dry_run else ""))

        def resize(path: Path) -> dict:
            ctx.raise_if_cancelled()
            before = _size(path)
            width, height = textures.read_vtf_size(path)
            target_width, target_height = _target(width, height, max_size,
                                                  preserve)
            if (target_width, target_height) == (width, height):
                return {"path": path, "status": "within budget",
                        "from": f"{width}x{height}", "to": "",
                        "before": before, "after": before}
            if dry_run:
                return {"path": path, "status": "would resize",
                        "from": f"{width}x{height}",
                        "to": f"{target_width}x{target_height}",
                        "before": before, "after": before}

            if backup:
                spare = path.with_suffix(path.suffix + ".bak")
                if not spare.exists():
                    shutil.copy2(path, spare)
            textures.resize_vtf(path, target_width, target_height)
            return {"path": path, "status": "resized",
                    "from": f"{width}x{height}",
                    "to": f"{target_width}x{target_height}",
                    "before": before, "after": _size(path)}

        outcome = ctx.map(files, resize, workers=int(ctx.prefs.get("workers", 4)),
                          label=lambda path: path.name, unit="file",
                          log_each=False)
        rows = [row for row in outcome.results if row is not None]
        changed = [row for row in rows
                   if row["status"] in ("resized", "would resize")]

        before = sum(row["before"] for row in rows)
        after = sum(row["after"] for row in rows)
        ctx.metric("Files", len(rows))
        ctx.metric("Resized", len(changed),
                   tone="good" if changed else "neutral")
        ctx.metric("Before", human_size(before))
        ctx.metric("After", human_size(after))
        if before > after:
            ctx.metric("Saved", human_size(before - after), tone="good")
        if outcome.failed:
            ctx.metric("Failed", outcome.failed, tone="bad")

        ctx.table("Textures", ["File", "Result", "From", "To", "Size", "Path"],
                  [[row["path"].name, row["status"], row["from"], row["to"],
                    human_size(row["after"]), str(row["path"])] for row in rows],
                  path_column=5)

        if bool(ctx.prefs.get("preview_results", True)) and not dry_run:
            for row in changed[:int(ctx.prefs.get("preview_limit", 8))]:
                ctx.artifact(row["path"], label=row["path"].name, kind="image")

        verb = "would resize" if dry_run else "resized"
        summary = (f"{verb} {len(changed)}/{len(rows)} file(s), "
                   f"{human_size(before)} -> {human_size(after)}")
        (ctx.error if outcome.failed and not changed else ctx.success)(summary)
        ctx.summary(summary)

    # ------------------------------------------------------------------

    @staticmethod
    def _collect(ctx: JobContext) -> list[Path]:
        import os

        if str(ctx.params["source"]) == "files":
            raw = str(ctx.params["files"] or "")
            return [Path(part) for part in raw.split(os.pathsep)
                    if part.strip() and part.lower().endswith(".vtf")]
        folder = str(ctx.params["folder"] or "")
        if not folder:
            raise ValueError("Pick a folder of VTF files")
        root = Path(folder)
        pattern = "**/*.vtf" if bool(ctx.params["recursive"]) else "*.vtf"
        return sorted(root.glob(pattern))


def _floor_power_of_two(value: int) -> int:
    if value <= 1:
        return 1
    return 1 << (value.bit_length() - 1)


def _target(width: int, height: int, max_size: int,
            preserve_aspect: bool) -> tuple[int, int]:
    if width <= max_size and height <= max_size:
        return width, height
    if preserve_aspect:
        scale = max_size / max(width, height)
        return (min(max_size, _floor_power_of_two(max(1, round(width * scale)))),
                min(max_size, _floor_power_of_two(max(1, round(height * scale)))))
    return (min(max_size, _floor_power_of_two(width)),
            min(max_size, _floor_power_of_two(height)))


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
