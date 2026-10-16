"""
Sound processing: format conversion, Source loop points, and the optimisation
passes GMod content optimisers apply.

Everything routes through one ffmpeg invocation per file - decoding into Python
and re-encoding (what pydub does) costs two ffmpeg runs plus a full copy of the
samples through the interpreter, and buys nothing here.

The optimisation passes are modelled on the techniques used by fOptimizer,
gm_content_optimizer and GMod-Optimization-Tool, with the settings they
converged on (Vorbis around q4/96k, `loudnorm=I=-14:TP=-1.5:LRA=11`,
`-map_metadata -1`, -55 dBFS trailing-silence trim). The important addition is
that loop metadata is treated as sacred: a WAV carrying `smpl` or `cue ` chunks
drives Source's looping ambience, and re-encoding it to Vorbis silently breaks
that. Those files are detected and skipped unless the caller insists.

Framework-free: no Flet, no Qt.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
import struct
import subprocess
import threading
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from s2kit.core import process

logger = logging.getLogger(__name__)

LogFn = Callable[[str], None]

#: Extensions GMod can actually play.
SOUND_EXTENSIONS = {".wav", ".mp3", ".ogg"}
#: Everything ffmpeg will read for us, including formats worth converting away.
INPUT_EXTENSIONS = SOUND_EXTENSIONS | {".flac", ".m4a", ".aac", ".opus", ".wma",
                                        ".aif", ".aiff"}

#: Sample rates Source's audio engine handles without resampling at runtime.
SOURCE_SAMPLE_RATES = (44100, 22050, 11025)

#: The loudness target the GMod optimisers settled on. -14 LUFS is the
#: streaming-platform norm and keeps headroom for Source's own mixing.
LOUDNORM_FILTER = "loudnorm=I=-14.0:TP=-1.5:LRA=11"

#: Trailing-silence detection, matching gm_content_optimizer's thresholds.
SILENCE_DB = -55.0
SILENCE_MIN_MS = 50
FADE_OUT_MS = 200

#: Files that reference sounds by name and may need rewriting after a convert.
REFERENCE_EXTENSIONS = {".lua", ".txt", ".json", ".vmt", ".cfg", ".res"}


class AudioError(RuntimeError):
    """A sound could not be read or converted (with a user-facing reason)."""


# ----------------------------------------------------------------------
# Probing
# ----------------------------------------------------------------------

@dataclass
class SoundInfo:
    """What we know about one sound file before touching it."""

    path: Path
    size: int = 0
    duration: float = 0.0
    sample_rate: int = 0
    channels: int = 0
    bit_depth: int = 0
    codec: str = ""
    bitrate: int = 0
    has_loop: bool = False
    loop_start: Optional[int] = None
    loop_end: Optional[int] = None
    cue_points: int = 0

    @property
    def extension(self) -> str:
        return self.path.suffix.lower()

    @property
    def protected(self) -> bool:
        """Loop or cue metadata that a re-encode would destroy."""
        return self.has_loop or self.cue_points > 0


def probe(path: Path, *, ffmpeg_override: str = "",
          cancel: Optional[threading.Event] = None) -> SoundInfo:
    """Read a sound's format and any Source loop metadata it carries."""
    target = Path(path)
    info = SoundInfo(path=target)
    try:
        info.size = target.stat().st_size
    except OSError as exc:
        raise AudioError(f"Cannot read {target.name}: {exc}") from exc

    if target.suffix.lower() == ".wav":
        _probe_wav(target, info)

    payload = _ffprobe(target, ffmpeg_override, cancel)
    if payload:
        stream = (payload.get("streams") or [{}])[0]
        container = payload.get("format") or {}
        info.duration = float(stream.get("duration")
                              or container.get("duration") or info.duration or 0.0)
        info.sample_rate = int(stream.get("sample_rate") or info.sample_rate or 0)
        info.channels = int(stream.get("channels") or info.channels or 0)
        info.codec = str(stream.get("codec_name") or info.codec or "")
        info.bitrate = int(stream.get("bit_rate") or container.get("bit_rate") or 0)
    return info


def _probe_wav(path: Path, info: SoundInfo) -> None:
    """Fill in what the stdlib and the RIFF chunks can tell us for free."""
    try:
        with wave.open(str(path), "rb") as handle:
            info.channels = handle.getnchannels()
            info.sample_rate = handle.getframerate()
            info.bit_depth = handle.getsampwidth() * 8
            frames = handle.getnframes()
            info.duration = frames / info.sample_rate if info.sample_rate else 0.0
            info.codec = "pcm"
    except (wave.Error, OSError):
        pass

    loop, cues = read_wav_loop(path)
    if loop is not None:
        info.has_loop = True
        info.loop_start, info.loop_end = loop
    info.cue_points = cues


def _ffprobe(path: Path, override: str,
             cancel: Optional[threading.Event]) -> Optional[dict]:
    binary = process.which("ffprobe", override=_sibling(override, "ffprobe"))
    if binary is None:
        return None
    args = [str(binary), "-v", "error", "-print_format", "json", "-show_format",
            "-show_streams", "-select_streams", "a:0", str(path)]
    try:
        code, output = process.capture(args, cancel=cancel, timeout=60)
    except process.ProcessError:
        return None
    if code != 0:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return None


def _sibling(override: str, name: str) -> str:
    """Given a path to ffmpeg, find ffprobe next to it (and vice versa)."""
    if not override:
        return ""
    candidate = Path(override)
    if candidate.stem.lower() == name:
        return str(candidate)
    sibling = candidate.with_name(f"{name}{candidate.suffix}")
    return str(sibling) if sibling.is_file() else ""


def ffmpeg_binary(override: str = "") -> Path:
    """Locate ffmpeg, preferring the configured path."""
    found = process.which("ffmpeg", override=override)
    if found is None:
        raise AudioError(
            "ffmpeg was not found. Install it and put it on PATH, or set its "
            "location in Preferences > External programs."
        )
    return found


# ----------------------------------------------------------------------
# WAV loop / cue chunks
# ----------------------------------------------------------------------

def _iter_chunks(data: bytes) -> Iterable[tuple[bytes, int, int]]:
    """Yield `(chunk_id, start, end)` for each RIFF chunk after the header."""
    position = 12
    length = len(data)
    while position + 8 <= length:
        chunk_id = data[position:position + 4]
        size = int.from_bytes(data[position + 4:position + 8], "little")
        end = position + 8 + size + (size % 2)
        if end <= position:
            break
        yield chunk_id, position, end
        position = end


def read_wav_loop(path: Path) -> tuple[Optional[tuple[int, int]], int]:
    """Return `((loop_start, loop_end) or None, cue_point_count)`."""
    try:
        data = path.read_bytes()
    except OSError:
        return None, 0
    if not data.startswith(b"RIFF"):
        return None, 0

    loop: Optional[tuple[int, int]] = None
    cues = 0
    for chunk_id, start, end in _iter_chunks(data):
        body = data[start + 8:end]
        if chunk_id == b"smpl" and len(body) >= 36 + 24:
            try:
                loop_count = struct.unpack_from("<I", body, 28)[0]
                if loop_count:
                    begin, finish = struct.unpack_from("<II", body, 36 + 8)
                    loop = (begin, finish)
            except struct.error:
                continue
        elif chunk_id == b"cue " and len(body) >= 4:
            try:
                cues = struct.unpack_from("<I", body, 0)[0]
            except struct.error:
                cues = 0
    return loop, cues


def strip_chunk(data: bytes, chunk_id: bytes) -> bytes:
    """Remove every chunk with this id, so we never write a duplicate."""
    out = data
    while True:
        for current, start, end in _iter_chunks(out):
            if current == chunk_id:
                out = out[:start] + out[end:]
                break
        else:
            return out


def build_smpl_chunk(sample_rate: int, loop_start: int, loop_end: int) -> bytes:
    """A RIFF `smpl` chunk describing one forward loop."""
    period = int(1_000_000_000 / sample_rate) if sample_rate else 0
    header = struct.pack(
        "<IIIIIIIII",
        0,          # manufacturer
        0,          # product
        period,     # sample period, nanoseconds
        60,         # MIDI unity note (middle C)
        0,          # MIDI pitch fraction
        0,          # SMPTE format
        0,          # SMPTE offset
        1,          # loop count
        0,          # sampler-specific data
    )
    loop = struct.pack(
        "<IIIIII",
        0,           # cue point id
        0,           # type: forward
        loop_start,
        loop_end,
        0,           # fraction
        0,           # play count, 0 = infinite
    )
    body = header + loop
    return b"smpl" + struct.pack("<I", len(body)) + body


def write_wav_loop(path: Path, loop_start: int = 0,
                   loop_end: Optional[int] = None) -> tuple[int, int]:
    """Embed a `smpl` loop chunk in a WAV. Returns the loop range written.

    `loop_end=None` loops the whole file. Any existing `smpl` chunk is replaced
    rather than appended to, because Source reads the first one it finds.
    """
    with wave.open(str(path), "rb") as handle:
        sample_rate = handle.getframerate()
        frames = handle.getnframes()
    if frames <= 1:
        raise AudioError(f"{path.name} is too short to loop")

    if loop_end is None or loop_end <= 0 or loop_end >= frames:
        loop_end = frames - 1
    loop_start = max(0, min(loop_start, loop_end - 1))

    data = strip_chunk(path.read_bytes(), b"smpl")
    updated = bytearray(data)
    updated.extend(build_smpl_chunk(sample_rate, loop_start, loop_end))
    updated[4:8] = struct.pack("<I", len(updated) - 8)   # RIFF size
    path.write_bytes(bytes(updated))
    return loop_start, loop_end


# ----------------------------------------------------------------------
# ffmpeg operations
# ----------------------------------------------------------------------

@dataclass
class EncodeSpec:
    """How to encode one output file."""

    codec: str = "libvorbis"          # libvorbis | libmp3lame | pcm_s16le | copy
    quality: Optional[float] = 4.0    # Vorbis -q:a; None to use bitrate
    bitrate: Optional[int] = None     # kbps
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    normalise: bool = False
    strip_metadata: bool = True
    trim_silence: bool = False
    fade_out_ms: int = FADE_OUT_MS
    extra_filters: Sequence[str] = field(default_factory=tuple)

    def filters(self, trim_at: Optional[float] = None) -> list[str]:
        """The `-af` chain for this spec, in the order ffmpeg should apply it."""
        chain: list[str] = []
        if trim_at is not None and trim_at > 0:
            chain.append(f"atrim=end={trim_at:.3f}")
            if self.fade_out_ms > 0:
                start = max(0.0, trim_at - self.fade_out_ms / 1000.0)
                chain.append(f"afade=t=out:st={start:.3f}:"
                             f"d={self.fade_out_ms / 1000.0:.3f}")
        if self.normalise:
            chain.append(LOUDNORM_FILTER)
        chain.extend(self.extra_filters)
        return chain


def encode(source: Path, destination: Path, spec: EncodeSpec, *,
           ffmpeg_override: str = "", trim_at: Optional[float] = None,
           cancel: Optional[threading.Event] = None,
           on_log: Optional[LogFn] = None) -> None:
    """Run one ffmpeg encode. Raises :class:`AudioError` on failure."""
    binary = ffmpeg_binary(ffmpeg_override)
    destination.parent.mkdir(parents=True, exist_ok=True)

    args: list[str] = [str(binary), "-hide_banner", "-loglevel", "error",
                       "-nostdin", "-y", "-i", str(source), "-vn"]
    filters = spec.filters(trim_at)
    if filters:
        args += ["-af", ",".join(filters)]
    if spec.strip_metadata:
        args += ["-map_metadata", "-1"]
    if spec.sample_rate:
        args += ["-ar", str(spec.sample_rate)]
    if spec.channels:
        args += ["-ac", str(spec.channels)]

    if spec.codec == "copy":
        args += ["-c:a", "copy"]
    else:
        args += ["-c:a", spec.codec]
        if spec.codec == "libvorbis" and spec.quality is not None:
            args += ["-q:a", f"{spec.quality:g}"]
        elif spec.bitrate:
            args += ["-b:a", f"{spec.bitrate}k"]
    args.append(str(destination))

    lines: list[str] = []
    try:
        code = process.run_streamed(args, lines.append, cancel=cancel,
                                    echo_command=False)
    except process.ProcessError as exc:
        raise AudioError(str(exc)) from exc
    if code != 0:
        message = "\n".join(lines).strip() or f"ffmpeg exited with code {code}"
        raise AudioError(message)
    if on_log and lines:
        for line in lines:
            on_log(line)


def detect_trailing_silence(path: Path, *, ffmpeg_override: str = "",
                            threshold_db: float = SILENCE_DB,
                            minimum_ms: int = SILENCE_MIN_MS,
                            cancel: Optional[threading.Event] = None
                            ) -> Optional[float]:
    """Seconds into the file where trailing silence begins, or None.

    Uses ffmpeg's `silencedetect` rather than decoding samples into Python.
    Only silence running to the end of the file counts - a gap in the middle is
    part of the sound. Note that ffmpeg emits a `silence_end` at EOF as well as
    at a real end, so "it ended" is not enough to call a stretch interior; the
    end has to line up with the file duration.
    """
    binary = ffmpeg_binary(ffmpeg_override)
    args = [str(binary), "-hide_banner", "-nostdin", "-i", str(path),
            "-af", f"silencedetect=noise={threshold_db}dB:"
                   f"d={minimum_ms / 1000.0:g}",
            "-f", "null", "-"]
    lines: list[str] = []
    try:
        process.run_streamed(args, lines.append, cancel=cancel,
                             echo_command=False)
    except process.ProcessError as exc:
        raise AudioError(str(exc)) from exc

    text = "\n".join(lines)
    starts = [float(value)
              for value in re.findall(r"silence_start:\s*(-?[\d.]+)", text)]
    ends = [float(value)
            for value in re.findall(r"silence_end:\s*([\d.]+)", text)]
    if not starts:
        return None

    duration = _parse_duration(text)
    last_start = starts[-1]
    if len(ends) >= len(starts):
        # The final stretch ended. Only trailing if it ended at EOF.
        if duration is None or abs(ends[-1] - duration) > 0.05:
            return None

    if last_start <= 0:
        return None                    # the whole file is silent; leave it alone
    if duration is not None and duration - last_start < minimum_ms / 1000.0:
        return None                    # not enough silence to be worth cutting
    return last_start


def _parse_duration(text: str) -> Optional[float]:
    """Pull `Duration: HH:MM:SS.ss` out of ffmpeg's banner."""
    match = re.search(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)", text)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


# ----------------------------------------------------------------------
# Optimisation
# ----------------------------------------------------------------------

@dataclass
class OptimiseOptions:
    """What the optimiser is allowed to do to each file."""

    to_ogg: bool = True
    quality: float = 4.0
    bitrate: Optional[int] = None
    max_sample_rate: int = 44100
    force_mono: bool = False
    mono_below_seconds: float = 0.0      # 0 disables the duration rule
    normalise: bool = False
    strip_metadata: bool = True
    trim_silence: bool = False
    keep_original_name: bool = False     # write .ogg data under the old name
    skip_looping: bool = True
    minimum_saving: float = 0.02         # keep the original unless 2% smaller
    dry_run: bool = True
    delete_source: bool = True

    #: Re-encode files that are already Ogg Vorbis. Off by default: Vorbis to
    #: Vorbis is a generational transcode - it decodes lossy audio and throws
    #: more away - so it is only worth it when the source is genuinely
    #: oversized. Those files still get the lossless metadata strip.
    reencode_ogg: bool = False
    #: When re-encoding OGGs, only bother with ones above this bitrate (kbps).
    #: 0 re-encodes every OGG. 160 catches bloated music while leaving
    #: already-lean effects alone.
    ogg_bitrate_floor: int = 160


@dataclass
class OptimiseResult:
    """What happened to one sound."""

    path: Path
    status: str = "optimised"     # optimised | skipped | failed | unchanged
    reason: str = ""
    output: Optional[Path] = None
    before: int = 0
    after: int = 0
    info: Optional[SoundInfo] = None
    actions: list[str] = field(default_factory=list)
    #: True when `after` is a projection rather than a measured file size.
    estimated: bool = False
    #: True when the audio was stream-copied rather than re-encoded.
    lossless: bool = False

    @property
    def saved(self) -> int:
        return max(0, self.before - self.after) if self.after else 0

    @property
    def ratio(self) -> float:
        return (self.after / self.before) if self.before and self.after else 1.0


def plan_encode(info: SoundInfo, options: OptimiseOptions) -> tuple[EncodeSpec,
                                                                    list[str]]:
    """Decide how a file should be re-encoded, and describe why."""
    actions: list[str] = []
    spec = EncodeSpec(strip_metadata=options.strip_metadata,
                      normalise=options.normalise,
                      trim_silence=options.trim_silence)

    if options.to_ogg:
        spec.codec = "libvorbis"
        spec.quality = options.quality if options.bitrate is None else None
        spec.bitrate = options.bitrate
        if info.extension != ".ogg":
            actions.append(f"{info.extension.lstrip('.')} -> ogg")
        else:
            actions.append("re-encode")
    else:
        spec.codec = "copy"
        spec.quality = None

    cap = options.max_sample_rate
    if cap and info.sample_rate and info.sample_rate > cap:
        target = max(rate for rate in SOURCE_SAMPLE_RATES if rate <= cap) \
            if any(rate <= cap for rate in SOURCE_SAMPLE_RATES) else cap
        spec.sample_rate = target
        actions.append(f"{info.sample_rate} -> {target} Hz")

    wants_mono = options.force_mono or (
        options.mono_below_seconds > 0
        and 0 < info.duration <= options.mono_below_seconds
    )
    if wants_mono and info.channels > 1:
        spec.channels = 1
        actions.append("stereo -> mono")

    if options.normalise:
        actions.append("loudnorm")
    if options.strip_metadata:
        actions.append("strip metadata")
    if options.trim_silence:
        actions.append("trim trailing silence")
    return spec, actions


#: Rough Vorbis quality -> kbps for 44.1 kHz stereo, from the Xiph table.
_VORBIS_KBPS = {-1: 45, 0: 64, 1: 80, 2: 96, 3: 112, 4: 128, 5: 160,
                6: 192, 7: 224, 8: 256, 9: 320, 10: 500}


def estimate_encoded_size(info: SoundInfo, spec: EncodeSpec,
                          trim_at: Optional[float] = None) -> int:
    """Predict the encoded size in bytes, for the dry run.

    A dry run that reports "0 saved" is useless, and actually encoding every
    file would defeat the point of a dry run, so this projects from the target
    bitrate. Vorbis is VBR and content-dependent, so treat it as an estimate -
    it is typically within about 15% for game audio.
    """
    duration = trim_at if trim_at else info.duration
    if duration <= 0:
        return info.size

    if spec.codec == "copy":
        return info.size
    if spec.codec == "pcm_s16le":
        rate = spec.sample_rate or info.sample_rate or 44100
        channels = spec.channels or info.channels or 2
        return int(duration * rate * channels * 2) + 44

    if spec.bitrate:
        kbps = float(spec.bitrate)
    elif spec.codec == "libvorbis" and spec.quality is not None:
        low = _VORBIS_KBPS[max(-1, min(10, int(spec.quality)))]
        high = _VORBIS_KBPS[max(-1, min(10, int(spec.quality) + 1))]
        kbps = low + (high - low) * (spec.quality - int(spec.quality))
    else:
        kbps = 128.0

    # The Xiph table is for 44.1 kHz stereo; mono and lower rates cost less.
    channels = spec.channels or info.channels or 2
    if channels == 1:
        kbps *= 0.6
    rate = spec.sample_rate or info.sample_rate or 44100
    if rate < 44100:
        kbps *= max(0.35, rate / 44100.0)
    return int(duration * kbps * 1000 / 8)


def _transcode_verdict(info: SoundInfo, options: OptimiseOptions
                       ) -> tuple[bool, str]:
    """Should this file be stream-copied instead of re-encoded?

    Returns `(lossless_only, reason)`. Only Ogg Vorbis sources are candidates:
    everything else is either lossless (WAV) or a format we want to move away
    from (MP3), so re-encoding those is the whole point.
    """
    if info.extension != ".ogg" or not options.to_ogg:
        return False, ""

    if not options.reencode_ogg:
        reason = "already OGG"
    else:
        floor = options.ogg_bitrate_floor
        if floor and info.bitrate and info.bitrate / 1000.0 <= floor:
            reason = (f"already OGG at {info.bitrate // 1000} kbps, at or "
                      f"under the {floor} kbps floor")
        else:
            return False, ""

    # A stream copy cannot resample or downmix, so say what is being left on
    # the table rather than quietly not doing it.
    forgone: list[str] = []
    if (options.max_sample_rate and info.sample_rate
            and info.sample_rate > options.max_sample_rate):
        forgone.append(f"still {info.sample_rate} Hz")
    wants_mono = options.force_mono or (
        options.mono_below_seconds > 0
        and 0 < info.duration <= options.mono_below_seconds)
    if wants_mono and info.channels > 1:
        forgone.append("still stereo")
    if forgone:
        reason += f" - {', '.join(forgone)}; re-encoding would fix that"
    return True, reason


def optimise_file(path: Path, options: OptimiseOptions, *,
                  ffmpeg_override: str = "", on_log: LogFn = print,
                  cancel: Optional[threading.Event] = None) -> OptimiseResult:
    """Apply the optimisation passes to one sound.

    Never raises for asset problems - a failure comes back as a result with
    `status="failed"` so a batch keeps going.
    """
    source = Path(path)
    result = OptimiseResult(path=source)
    try:
        info = probe(source, ffmpeg_override=ffmpeg_override, cancel=cancel)
    except AudioError as exc:
        result.status = "failed"
        result.reason = str(exc)
        return result

    result.info = info
    result.before = info.size

    if options.skip_looping and info.protected:
        result.status = "skipped"
        result.reason = ("carries loop/cue metadata Source needs - re-encoding "
                         "would drop it")
        return result

    lossless_only, why = _transcode_verdict(info, options)
    if lossless_only and not options.strip_metadata:
        result.status = "unchanged"
        result.reason = why
        result.after = result.before
        return result

    spec, actions = plan_encode(info, options)
    if lossless_only:
        # Stream-copy: keep the encoded audio byte for byte and only drop the
        # container's tags. No filters are possible in this mode, so loudnorm
        # and the silence trim do not apply.
        spec = EncodeSpec(codec="copy", quality=None, strip_metadata=True)
        actions = [f"strip metadata only ({why})"]
    result.actions = actions
    result.lossless = lossless_only

    trim_at: Optional[float] = None
    if options.trim_silence:
        try:
            trim_at = detect_trailing_silence(source,
                                              ffmpeg_override=ffmpeg_override,
                                              cancel=cancel)
        except AudioError as exc:
            on_log(f"{source.name}: silence detection failed ({exc})")
        if trim_at is not None:
            result.actions.append(f"trim at {trim_at:.2f}s")

    suffix = ".ogg" if options.to_ogg else info.extension
    destination = source.with_suffix(suffix)
    if options.dry_run:
        result.status = "optimised"
        result.estimated = True
        if lossless_only:
            # A stream copy saves whatever the tags weighed, which cannot be
            # known without running it. Report it honestly rather than
            # guessing, and skip the saving threshold.
            result.after = result.before
            result.reason = "would strip metadata losslessly"
            return result
        result.after = estimate_encoded_size(info, spec, trim_at)
        result.reason = f"would write {destination.name}"
        if result.after >= result.before * (1 - options.minimum_saving):
            result.status = "unchanged"
            result.reason = "estimated saving is below the threshold"
            result.after = result.before
        return result

    temporary = source.with_name(f"{source.stem}.s2kit-tmp{suffix}")
    try:
        encode(source, temporary, spec, ffmpeg_override=ffmpeg_override,
               trim_at=trim_at, cancel=cancel)
    except AudioError as exc:
        temporary.unlink(missing_ok=True)
        result.status = "failed"
        result.reason = str(exc)
        return result

    try:
        result.after = temporary.stat().st_size
    except OSError:
        result.after = 0

    # Optimising is supposed to make things smaller. A short clip re-encoded at
    # a higher effective bitrate than it started with would otherwise grow.
    if result.before and result.after >= result.before * (1 - options.minimum_saving):
        temporary.unlink(missing_ok=True)
        result.status = "unchanged"
        result.reason = (f"re-encode saved less than "
                         f"{options.minimum_saving * 100:.0f}%")
        result.after = result.before
        return result

    final = source if options.keep_original_name else destination
    try:
        if final != source and final.exists():
            final.unlink()
        os.replace(temporary, final)
        if options.delete_source and source != final and source.exists():
            source.unlink()
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        result.status = "failed"
        result.reason = f"could not replace {final.name}: {exc}"
        return result

    result.output = final
    return result


# ----------------------------------------------------------------------
# Reference rewriting + duplicate detection
# ----------------------------------------------------------------------

def rewrite_references(root: Path, renames: dict[str, str], *,
                       extensions: Optional[set[str]] = None,
                       dry_run: bool = True,
                       on_log: LogFn = print) -> tuple[int, int]:
    """Point Lua/text references at renamed sounds. Returns `(files, edits)`.

    A `.wav` renamed to `.ogg` breaks every `sound.Play`/`EmitSound` that names
    it, so the optimisers rewrite the references too. Matching is on the
    filename and is case-insensitive, because Source paths are.
    """
    if not renames:
        return (0, 0)
    extensions = extensions or REFERENCE_EXTENSIONS
    lookup = {old.lower(): new for old, new in renames.items()}
    pattern = re.compile(
        "|".join(re.escape(name) for name in sorted(lookup, key=len, reverse=True)),
        re.IGNORECASE,
    )

    touched = edits = 0
    for candidate in Path(root).rglob("*"):
        if not candidate.is_file() or candidate.suffix.lower() not in extensions:
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        replaced, count = pattern.subn(
            lambda match: lookup[match.group(0).lower()], text)
        if not count:
            continue
        touched += 1
        edits += count
        on_log(f"{candidate.name}: {count} reference(s)")
        if not dry_run:
            try:
                candidate.write_text(replaced, encoding="utf-8")
            except OSError as exc:
                on_log(f"{candidate.name}: could not rewrite ({exc})")
    return touched, edits


def file_digest(path: Path, chunk: int = 1 << 20) -> str:
    """A content hash, for finding byte-identical duplicates."""
    digest = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


@dataclass
class DuplicateGroup:
    """Byte-identical files, with the size recorded at detection time."""

    digest: str
    size: int
    paths: list[Path]

    @property
    def wasted(self) -> int:
        """Bytes the redundant copies cost."""
        return self.size * (len(self.paths) - 1)


def find_duplicates(paths: Sequence[Path]) -> list[DuplicateGroup]:
    """Group byte-identical files by hash, cheapest test first.

    Redundancy culling is where the biggest wins usually come from in a
    scraped addon: the same footstep sound copied into a dozen folders. The
    size is captured here because the caller typically reports it after the
    files have already been converted away.
    """
    by_size: dict[int, list[Path]] = {}
    for path in paths:
        try:
            by_size.setdefault(path.stat().st_size, []).append(path)
        except OSError:
            continue

    hashed: dict[str, tuple[int, list[Path]]] = {}
    for size, group in by_size.items():
        if len(group) < 2 or size == 0:
            continue
        for path in group:
            try:
                digest = file_digest(path)
            except OSError:
                continue
            hashed.setdefault(digest, (size, []))[1].append(path)
    return [DuplicateGroup(digest=digest, size=size, paths=paths)
            for digest, (size, paths) in hashed.items() if len(paths) > 1]


def matches_sound(name: str, *, include: str = "", exclude: str = "") -> bool:
    """Match case-insensitive substrings or globs against a relative path and basename; exclusions win."""
    normalized = name.replace("\\", "/").lower()
    candidates = (normalized, normalized.rsplit("/", 1)[-1])

    def matches(raw: str) -> bool:
        patterns = [part.strip().replace("\\", "/").lower()
                    for part in re.split(r"[,;\n]", raw) if part.strip()]
        return any(
            fnmatch.fnmatchcase(candidate, pattern)
            if any(char in pattern for char in "*?[") else pattern in candidate
            for pattern in patterns for candidate in candidates
        )

    return (not include.strip() or matches(include)) and not matches(exclude)


def find_sounds(root: Path, *, recursive: bool = True,
                extensions: Optional[set[str]] = None, include: str = "",
                exclude: str = "", input_extensions: str = "") -> list[Path]:
    """Find supported sounds matching optional path patterns and source extensions, sorted for stable reporting."""
    extensions = INPUT_EXTENSIONS if extensions is None else extensions
    extensions = {extension.lower() for extension in extensions}
    if input_extensions.strip():
        requested = {f".{part.strip().lower().lstrip('.')}"
                     for part in re.split(r"[,;\s]+", input_extensions)
                     if part.strip()}
        extensions = extensions & requested
    base = Path(root)
    if not base.is_dir():
        return []
    pattern = "**/*" if recursive else "*"
    return sorted(
        (path for path in base.glob(pattern)
         if path.is_file() and path.suffix.lower() in extensions
         and matches_sound(path.relative_to(base).as_posix(),
                           include=include, exclude=exclude)),
        key=lambda path: str(path).lower(),
    )
