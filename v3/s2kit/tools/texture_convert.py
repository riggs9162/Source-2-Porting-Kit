"""
Texture Converter.

Batch-converts textures (including VTF, via sourcepp) to PNG or TGA with an
optional resolution clamp and channel split. Small, real, and it exercises the
whole tool contract: multi-file input, output files registered as artefacts,
per-tool preferences, and previews that open in the texture viewer.
"""

from __future__ import annotations

from s2kit.definitions.texture_convert import SUPPORTED

import os
from pathlib import Path

from s2kit.core.jobs import JobContext
from s2kit.media import images, probe
from s2kit.definitions.texture_convert import TextureConverter as _TextureConverterDefinition


class TextureConverter(_TextureConverterDefinition):
    """Batch texture conversion with per-channel export."""


    def run_job(self, ctx: JobContext) -> None:
        params = ctx.params
        output_folder = Path(str(params["output_folder"]))
        dry_run = bool(params["dry_run"])
        fmt = str(params["format"]).lower()
        clamp = int(params["clamp"])
        channel = str(params["channel"])
        split = bool(params["split_channels"])
        overwrite = bool(params["overwrite"])

        sources = self._collect_sources(ctx)
        if not sources:
            ctx.warn("No supported textures found.")
            ctx.summary("Nothing to convert")
            return

        if not dry_run:
            output_folder.mkdir(parents=True, exist_ok=True)
        ctx.info(f"{len(sources):,} texture(s) -> {output_folder} as .{fmt}"
                 + ("  [dry run]" if dry_run else ""))

        flatten = bool(ctx.prefs.get("flatten_output", True))
        collision = str(ctx.prefs.get("naming", "suffix"))
        preview = bool(ctx.prefs.get("preview_results", True))
        preview_limit = int(ctx.prefs.get("preview_limit", 12))

        root = self._input_root(ctx)
        rows: list[list[object]] = []
        converted = failed = skipped = 0
        previewed = 0

        for index, source in enumerate(sources, start=1):
            ctx.raise_if_cancelled()
            ctx.progress(index, len(sources))
            ctx.status(f"{index}/{len(sources)}  {source.name}")

            try:
                png, info = images.export_texture(
                    source,
                    max_size=clamp,
                    channel=channel,
                )
            except Exception as exc:
                failed += 1
                ctx.error(f"{source.name}: {exc}")
                rows.append([source.name, "-", "failed", str(exc)[:80], str(source)])
                continue

            target = self._target_path(source, root, output_folder, fmt, flatten)
            if target.exists() and not overwrite:
                if collision == "skip":
                    skipped += 1
                    ctx.warn(f"{source.name}: target exists, skipped")
                    rows.append([source.name, f"{info.width}x{info.height}",
                                 "skipped", "target exists", str(target)])
                    continue
                if collision == "suffix":
                    target = _unique(target)

            if dry_run:
                converted += 1
                rows.append([source.name, f"{info.width}x{info.height}",
                             "would write", info.format, str(target)])
                ctx.info(f"{source.name} -> {target.name} (dry run)")
                continue

            try:
                self._write(png, target, fmt)
                if split:
                    self._write_channels(source, target, fmt, ctx)
            except Exception as exc:
                failed += 1
                ctx.error(f"{target.name}: {exc}")
                rows.append([source.name, f"{info.width}x{info.height}",
                             "failed", str(exc)[:80], str(target)])
                continue

            converted += 1
            rows.append([source.name, f"{info.width}x{info.height}", "written",
                         info.format, str(target)])
            ctx.debug(f"{source.name} -> {target}")
            if preview and previewed < preview_limit:
                previewed += 1
                ctx.artifact(target, label=target.name, kind="image")

        ctx.metric("Converted", converted, tone="good" if converted else "neutral")
        if skipped:
            ctx.metric("Skipped", skipped, tone="warn")
        if failed:
            ctx.metric("Failed", failed, tone="bad")
        ctx.metric("Inputs", len(sources))

        ctx.table("Conversions", ["Source", "Size", "Result", "Detail", "Output"],
                  rows, path_column=4)

        verb = "would convert" if dry_run else "converted"
        message = f"{verb} {converted}/{len(sources)}"
        if failed:
            message += f", {failed} failed"
        if skipped:
            message += f", {skipped} skipped"
        (ctx.error if failed and not converted else ctx.success)(message)
        ctx.summary(message)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _collect_sources(self, ctx: JobContext) -> list[Path]:
        if str(ctx.params["source"]) == "files":
            raw = str(ctx.params["input_files"] or "")
            return [Path(part) for part in raw.split(os.pathsep) if part.strip()]
        folder = str(ctx.params["input_folder"] or "")
        if not folder:
            raise ValueError("Pick an input folder")
        return probe.find_files(folder, extensions=probe.IMAGE_EXTENSIONS,
                                recursive=bool(ctx.params["recursive"]))

    def _input_root(self, ctx: JobContext) -> Path:
        if str(ctx.params["source"]) == "files":
            raw = str(ctx.params["input_files"] or "")
            first = raw.split(os.pathsep)[0] if raw else "."
            return Path(first).parent
        return Path(str(ctx.params["input_folder"] or "."))

    def _target_path(self, source: Path, root: Path, output: Path, fmt: str,
                     flatten: bool) -> Path:
        if flatten:
            return output / f"{source.stem}.{fmt}"
        try:
            relative = source.relative_to(root)
        except ValueError:
            relative = Path(source.name)
        return output / relative.with_suffix(f".{fmt}")

    def _write(self, png: bytes, target: Path, fmt: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "png":
            target.write_bytes(png)
            return
        # Re-encode through Pillow for the formats that are not already PNG.
        import io

        from PIL import Image

        with Image.open(io.BytesIO(png)) as image:
            image.convert("RGB").save(target, format=fmt.upper())

    def _write_channels(self, source: Path, target: Path, fmt: str,
                        ctx: JobContext) -> None:
        thumbs = images.channel_thumbnails(source, size=1 << 14)
        for name, data in thumbs.items():
            channel_target = target.with_name(
                f"{target.stem}_{name.lower()}.{fmt}")
            self._write(data, channel_target, fmt)
            ctx.debug(f"channel {name} -> {channel_target.name}")


def _unique(target: Path) -> Path:
    """`name.png` -> `name_1.png` until the path is free."""
    index = 1
    candidate = target
    while candidate.exists():
        candidate = target.with_name(f"{target.stem}_{index}{target.suffix}")
        index += 1
    return candidate
