"""
Audio probing and waveform rendering for the sound player.

WAV is read with the stdlib so the common case needs nothing installed.
Everything else goes through ffmpeg when it is on PATH (the same dependency
the audio tools already rely on). Metadata comes from ffprobe when available,
otherwise from the container header.

Playback itself is handled by the UI layer: Flet's bundled desktop client has
no audio control, so the viewer offers scrub-free transport through the OS
default player, plus the waveform for visual inspection. If the optional
`flet-audio` extension is importable the viewer upgrades to in-app playback.
"""

from __future__ import annotations

import io
import json
import logging
import subprocess
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class AudioInfo:
    """What we know about a sound file."""

    duration: float = 0.0
    sample_rate: int = 0
    channels: int = 0
    bit_depth: int = 0
    codec: str = ""
    bitrate: int = 0
    frames: int = 0
    loop_start: Optional[int] = None
    loop_end: Optional[int] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def duration_text(self) -> str:
        if self.duration <= 0:
            return "-"
        minutes, seconds = divmod(self.duration, 60)
        return f"{int(minutes)}:{seconds:05.2f}"

    def rows(self) -> list[tuple[str, str]]:
        rows = [
            ("Duration", self.duration_text()),
            ("Sample rate", f"{self.sample_rate} Hz" if self.sample_rate else "-"),
            ("Channels", str(self.channels or "-")),
        ]
        if self.bit_depth:
            rows.append(("Bit depth", f"{self.bit_depth}-bit"))
        if self.codec:
            rows.append(("Codec", self.codec))
        if self.bitrate:
            rows.append(("Bitrate", f"{self.bitrate // 1000} kbps"))
        if self.frames:
            rows.append(("Samples", f"{self.frames:,}"))
        if self.loop_start is not None:
            rows.append(("Loop start", f"{self.loop_start:,} samples"))
        if self.loop_end is not None:
            rows.append(("Loop end", f"{self.loop_end:,} samples"))
        rows.extend((key, str(value)) for key, value in self.extra.items())
        return rows


class AudioLoadError(RuntimeError):
    """Raised when a sound file cannot be read (with a user-facing reason)."""


def _ffmpeg(name: str = "ffmpeg", override: str = "") -> Optional[str]:
    """Locate ffmpeg/ffprobe, honouring the preference override."""
    import shutil

    if override:
        candidate = Path(override)
        if candidate.is_file():
            if candidate.stem.lower() == name:
                return str(candidate)
            sibling = candidate.with_name(f"{name}{candidate.suffix}")
            if sibling.is_file():
                return str(sibling)
    return shutil.which(name)


def _no_window() -> dict[str, Any]:
    """Keep ffmpeg from flashing a console window on Windows."""
    if sys.platform == "win32":
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return {"startupinfo": info,
                "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


# ----------------------------------------------------------------------
# Probing
# ----------------------------------------------------------------------

def probe(path: Any, *, ffmpeg_override: str = "") -> AudioInfo:
    """Read metadata for a sound file. Never raises for a readable file."""
    target = Path(path)
    if not target.is_file():
        raise AudioLoadError(f"File not found: {target}")

    if target.suffix.lower() == ".wav":
        try:
            return _probe_wav(target)
        except Exception as exc:
            logger.debug("stdlib wave probe failed for %s: %s", target, exc)

    info = _probe_ffprobe(target, ffmpeg_override)
    if info is not None:
        return info
    return AudioInfo(codec=target.suffix.lstrip(".").upper(),
                     extra={"Note": "install ffmpeg for full metadata"})


def _probe_wav(path: Path) -> AudioInfo:
    with wave.open(str(path), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
        info = AudioInfo(
            duration=frames / rate if rate else 0.0,
            sample_rate=rate,
            channels=handle.getnchannels(),
            bit_depth=handle.getsampwidth() * 8,
            codec="PCM WAV",
            frames=frames,
        )
    start, end = _read_wav_loop(path)
    info.loop_start, info.loop_end = start, end
    return info


def _read_wav_loop(path: Path) -> tuple[Optional[int], Optional[int]]:
    """Pull loop points out of a WAV 'smpl' chunk (Source cue points)."""
    try:
        data = path.read_bytes()
    except OSError:
        return None, None
    index = data.find(b"smpl")
    if index < 0 or len(data) < index + 44:
        return None, None
    import struct

    try:
        loop_count = struct.unpack_from("<I", data, index + 8 + 28)[0]
        if loop_count < 1:
            return None, None
        # First loop record starts after the 36-byte smpl header body.
        base = index + 8 + 36
        start, end = struct.unpack_from("<II", data, base + 8)
        return start, end
    except (struct.error, IndexError):
        return None, None


def _probe_ffprobe(path: Path, override: str) -> Optional[AudioInfo]:
    binary = _ffmpeg("ffprobe", override)
    if not binary:
        return None
    cmd = [binary, "-v", "error", "-print_format", "json", "-show_format",
           "-show_streams", "-select_streams", "a:0", str(path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                                **_no_window())
        payload = json.loads(result.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        logger.debug("ffprobe failed for %s: %s", path, exc)
        return None

    streams = payload.get("streams") or [{}]
    stream = streams[0]
    container = payload.get("format") or {}
    return AudioInfo(
        duration=float(stream.get("duration") or container.get("duration") or 0.0),
        sample_rate=int(stream.get("sample_rate") or 0),
        channels=int(stream.get("channels") or 0),
        bit_depth=int(stream.get("bits_per_raw_sample") or 0),
        codec=str(stream.get("codec_long_name") or stream.get("codec_name") or ""),
        bitrate=int(stream.get("bit_rate") or container.get("bit_rate") or 0),
        frames=int(stream.get("duration_ts") or 0),
        extra={"Container": container.get("format_long_name", "")}
        if container.get("format_long_name") else {},
    )


# ----------------------------------------------------------------------
# Sample access + waveform rendering
# ----------------------------------------------------------------------

def read_samples(path: Any, *, max_points: int = 4000,
                 ffmpeg_override: str = "") -> tuple[Any, int]:
    """Return `(peaks, sample_rate)` where peaks is an Nx2 min/max array.

    Decoding goes through the stdlib for WAV and ffmpeg for everything else.
    The result is already reduced to `max_points` buckets, so a ten-minute
    track costs the same to draw as a one-second blip.
    """
    try:
        import numpy as np
    except ImportError as exc:
        raise AudioLoadError("numpy is required for waveform rendering") from exc

    target = Path(path)
    if target.suffix.lower() == ".wav":
        samples, rate = _decode_wav(target, np)
    else:
        samples, rate = _decode_ffmpeg(target, np, ffmpeg_override)

    if samples.size == 0:
        return np.zeros((0, 2), dtype=np.float32), rate

    buckets = max(1, min(max_points, samples.size))
    trimmed = samples[: (samples.size // buckets) * buckets]
    if trimmed.size == 0:
        trimmed = samples
        buckets = 1
    reshaped = trimmed.reshape(buckets, -1)
    peaks = np.stack([reshaped.min(axis=1), reshaped.max(axis=1)], axis=1)
    return peaks.astype("float32"), rate


def _decode_wav(path: Path, np: Any) -> tuple[Any, int]:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        raw = handle.readframes(handle.getnframes())
    dtype = {1: np.uint8, 2: np.int16, 4: np.int32}.get(width)
    if dtype is None:
        raise AudioLoadError(f"Unsupported WAV sample width: {width * 8}-bit")
    data = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    if width == 1:
        data = (data - 128.0) / 128.0
    else:
        data /= float(2 ** (width * 8 - 1))
    if channels > 1:
        usable = (data.size // channels) * channels
        data = data[:usable].reshape(-1, channels).mean(axis=1)
    return data, rate


def _decode_ffmpeg(path: Path, np: Any, override: str) -> tuple[Any, int]:
    binary = _ffmpeg("ffmpeg", override)
    if not binary:
        raise AudioLoadError(
            f"Reading {path.suffix.upper()} needs ffmpeg on PATH "
            "(or set its path in Preferences > External programs)"
        )
    rate = 22050
    cmd = [binary, "-v", "error", "-nostdin", "-i", str(path),
           "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(rate), "-"]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=180, **_no_window())
    except (OSError, subprocess.SubprocessError) as exc:
        raise AudioLoadError(f"ffmpeg could not decode the file: {exc}") from exc
    if result.returncode != 0:
        message = (result.stderr or b"").decode("utf-8", "replace").strip()
        raise AudioLoadError(message or "ffmpeg failed to decode the file")
    data = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    return data, rate


def render_waveform(path: Any, *, width: int = 900, height: int = 140,
                    colour: tuple[int, int, int] = (94, 234, 212),
                    background: tuple[int, int, int] = (18, 18, 22),
                    show_rms: bool = True,
                    ffmpeg_override: str = "") -> tuple[bytes, AudioInfo]:
    """Render a waveform PNG and return it with the file's metadata."""
    try:
        import numpy as np
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise AudioLoadError("numpy and Pillow are required for waveforms") from exc

    info = probe(path, ffmpeg_override=ffmpeg_override)
    peaks, rate = read_samples(path, max_points=width, ffmpeg_override=ffmpeg_override)
    if info.sample_rate == 0:
        info.sample_rate = rate

    canvas = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(canvas)
    middle = height // 2
    draw.line([(0, middle), (width, middle)], fill=(60, 60, 70), width=1)

    if peaks.size:
        scale = (height / 2) - 2
        columns = np.linspace(0, width - 1, peaks.shape[0]).astype(int)
        for x, (low, high) in zip(columns, peaks):
            top = middle - int(high * scale)
            bottom = middle - int(low * scale)
            if top == bottom:
                bottom = top + 1
            draw.line([(int(x), top), (int(x), bottom)], fill=colour, width=1)

        if show_rms:
            rms = np.sqrt(np.mean(peaks ** 2, axis=1))
            faded = tuple(min(255, int(c * 0.45) + 40) for c in colour)
            for x, level in zip(columns, rms):
                extent = int(level * scale)
                draw.line([(int(x), middle - extent), (int(x), middle + extent)],
                          fill=faded, width=1)

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue(), info


def convert(path: Any, destination: Any, *, ffmpeg_override: str = "",
            extra_args: Optional[list[str]] = None) -> Path:
    """Transcode a sound file with ffmpeg. Returns the written path."""
    binary = _ffmpeg("ffmpeg", ffmpeg_override)
    if not binary:
        raise AudioLoadError("ffmpeg is not available")
    out = Path(destination)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [binary, "-v", "error", "-nostdin", "-y", "-i", str(path),
           *(extra_args or []), str(out)]
    result = subprocess.run(cmd, capture_output=True, text=True, **_no_window())
    if result.returncode != 0:
        raise AudioLoadError(result.stderr.strip() or "ffmpeg failed")
    return out
